from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from flask import Blueprint, Response, jsonify, render_template
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import (
    EquipmentItem,
    EventEquipmentPlan,
)
from app.models.event import Event, EventSpot, EventStatus
from app.models.user import UserAccount
from app.queries import user_fillable_qual_ids

main_bp = Blueprint("main", __name__)


@main_bp.get("/health")
def health() -> tuple[Response, int]:
    """Liveness + readiness probe. Returns 200 if DB is reachable, 503 otherwise."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"status": "ok"}), 200
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 503


@main_bp.get("/changelog")
@login_required
def changelog() -> str:
    """Render the application changelog (Czech, visible to all logged-in users)."""
    return render_template("main/changelog.html")


def _my_events_section(now: datetime, horizon: datetime) -> tuple[list[tuple[Event, list[str]]], set[int]]:
    """Build the 'Moje akce' section and return (tagged_events, assigned_event_id_set)."""
    assigned_event_id_set: set = set(
        db.session.scalars(
            db.select(EventSpot.event_id)
            .join(Assignment, Assignment.spot_id == EventSpot.id)
            .where(Assignment.user_id == current_user.id)
        ).all()
    )

    my_events_query = (
        db.select(Event)
        .where(
            Event.archived == sa.false(),
            Event.end_datetime >= now,
            Event.start_datetime <= horizon,
            Event.status != EventStatus.CANCELLED,
            or_(
                Event.id.in_(assigned_event_id_set),
                Event.responsible_person_id == current_user.id,
            ),
        )
        .order_by(Event.start_datetime)
    )
    if not current_user.has_permission("event.view_draft"):
        my_events_query = my_events_query.where(Event.status != EventStatus.DRAFT)

    my_events_raw = sorted(
        db.session.scalars(my_events_query).all(),
        key=lambda e: e.start_datetime,
    )

    tagged: list[tuple[Event, list[str]]] = []
    for e in my_events_raw:
        tags = []
        if e.id in assigned_event_id_set:
            tags.append("Přihlášen")
        if e.responsible_person_id == current_user.id:
            tags.append("Zodpovědná osoba")
        tagged.append((e, tags))
    return tagged, assigned_event_id_set


def _open_events_section(
    now: datetime,
    horizon: datetime,
    already_in: set[int],
) -> tuple[list[Event], list[Event]]:
    """Build the open-signups section: (eligible_events, all_open_events)."""
    if not current_user.has_permission("event.assign_own"):
        return [], []

    candidates = db.session.scalars(
        db.select(Event)
        .where(
            Event.status == EventStatus.ASSIGNMENTS_OPEN,
            Event.start_datetime <= horizon,
            Event.end_datetime >= now,
            Event.id.notin_(already_in),
        )
        .order_by(Event.start_datetime)
    ).all()

    fillable_ids = user_fillable_qual_ids(current_user)
    eligible = [e for e in candidates if any(s.assignment is None and s.is_eligible_for(fillable_ids) for s in e.spots)]
    all_open = [e for e in candidates if any(s.assignment is None for s in e.spots)]
    return eligible, all_open


def _attention_events_section(now: datetime, horizon: datetime) -> list[Event]:
    """Events a coordinator should pay attention to."""
    if not current_user.has_any_permission("event.publish", "event.assignments.open"):
        return []

    events = list(
        db.session.scalars(
            db.select(Event)
            .where(
                Event.archived == sa.false(),
                Event.status.in_([EventStatus.DRAFT, EventStatus.PUBLISHED, EventStatus.ASSIGNMENTS_OPEN]),
                Event.start_datetime <= horizon,
                Event.end_datetime >= now,
            )
            .order_by(Event.start_datetime)
        ).all()
    )
    return [
        e
        for e in events
        if e.status in (EventStatus.DRAFT, EventStatus.PUBLISHED)
        or (e.status == EventStatus.ASSIGNMENTS_OPEN and e.mandatory_filled_spots < e.mandatory_total_spots)
    ]


def _equipment_shortage_events(now: datetime, horizon: datetime) -> list[tuple[Event, str, int, int, int]]:
    """Events in the planning horizon with an equipment shortage.

    Returns list of (event, type_name, required, available, extra_count) tuples.
    One entry per event: the worst (first) shortage plus a count of additional
    shortages so the dashboard can show "… a N další".
    """
    if not current_user.has_any_permission("event.equipment.plan", "event.view"):
        return []

    events_with_plans = db.session.scalars(
        db.select(Event)
        .options(selectinload(Event.equipment_plans))  # type: ignore[arg-type]
        .where(
            Event.start_datetime > now,
            Event.start_datetime <= horizon,
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
            Event.id.in_(db.select(EventEquipmentPlan.event_id)),
        )
        .order_by(Event.start_datetime)
    ).all()

    if not events_with_plans:
        return []

    # Batch: pool per type across ALL affected windows would be complex since each
    # event has its own window.  Instead gather distinct type IDs and compute pool
    # counts per type for each unique event window in two aggregate queries each.
    # For the dashboard we cap at the first shortage per event to keep it readable.
    event_ids = [e.id for e in events_with_plans]
    type_ids = list({p.equipment_type_id for e in events_with_plans for p in e.equipment_plans})

    # Fetch all items for the relevant types with their maintenance windows.
    # One query; we compute per-event pool sizes in Python to correctly handle
    # expired maintenance windows (unavailability_since set but until <= event.start).
    item_rows = db.session.execute(
        db.select(
            EquipmentItem.type_id,
            EquipmentItem.unavailability_since,
            EquipmentItem.unavailability_until,
        ).where(EquipmentItem.type_id.in_(type_ids))
    ).all()
    items_by_type: dict[int, list[tuple]] = defaultdict(list)
    for item in item_rows:
        items_by_type[item.type_id].append((item.unavailability_since, item.unavailability_until))

    def _pool_for_window(type_id: int, start: datetime, end: datetime) -> int:
        """Count items of *type_id* not in maintenance during [start, end)."""
        count = 0
        for since, until in items_by_type.get(type_id, []):
            if since is None:
                count += 1  # no maintenance scheduled
            elif since >= end:
                count += 1  # maintenance starts after event ends
            elif until is not None and until <= start:
                count += 1  # maintenance ended before event starts
        return count

    # Committed quantities from ALL overlapping events (including other dashboard events).
    # We include dashboard events here and exclude only the current event in the inner
    # loop below — otherwise two horizon-events competing for the same type would
    # each exclude the other and neither would be flagged as short.
    committed_rows = db.session.execute(
        db.select(
            EventEquipmentPlan.event_id,
            EventEquipmentPlan.equipment_type_id,
            func.sum(EventEquipmentPlan.quantity_required).label("committed"),
        )
        .join(Event, Event.id == EventEquipmentPlan.event_id)
        .where(
            EventEquipmentPlan.equipment_type_id.in_(type_ids),
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
            Event.end_datetime > now,
        )
        .group_by(EventEquipmentPlan.event_id, EventEquipmentPlan.equipment_type_id)
    ).all()
    # Build: committed_windows[type_id] = [(event_id, event_start, event_end, qty)]
    committed_windows: dict[int, list[tuple]] = defaultdict(list)
    event_times: dict[int, tuple] = {e.id: (e.start_datetime, e.end_datetime) for e in events_with_plans}
    other_event_ids = list({r.event_id for r in committed_rows} - set(event_ids))
    if other_event_ids:
        other_events = db.session.execute(
            db.select(Event.id, Event.start_datetime, Event.end_datetime).where(Event.id.in_(other_event_ids))
        ).all()
        for oe in other_events:
            event_times[oe.id] = (oe.start_datetime, oe.end_datetime)
    for r in committed_rows:
        s, e = event_times.get(r.event_id, (None, None))
        if s and e:
            committed_windows[r.equipment_type_id].append((r.event_id, s, e, r.committed))

    result = []
    for event in events_with_plans:
        first: tuple[Event, str, int, int] | None = None
        extra_count = 0
        for plan in event.equipment_plans:
            tid = plan.equipment_type_id
            pool = _pool_for_window(tid, event.start_datetime, event.end_datetime)
            committed = sum(
                qty
                for (eid, s, e, qty) in committed_windows.get(tid, [])
                if eid != event.id and s < event.end_datetime and e > event.start_datetime
            )
            avail = pool - committed
            if avail < plan.quantity_required:
                if first is None:
                    first = (event, plan.equipment_type.name, plan.quantity_required, max(0, avail))
                else:
                    extra_count += 1
        if first is not None:
            result.append((*first, extra_count))
    return result


def _missing_rp_events_section(now: datetime) -> list[Event]:
    """Events in the next 7 days without a responsible person."""
    if not current_user.has_any_permission("event.publish", "event.assignments.open"):
        return []
    rp_horizon = now + timedelta(days=7)
    return list(
        db.session.scalars(
            db.select(Event)
            .where(
                Event.archived == sa.false(),
                Event.status.notin_([EventStatus.DRAFT, EventStatus.CANCELLED]),
                Event.responsible_person_id == None,  # noqa: E711
                Event.start_datetime >= now,
                Event.start_datetime <= rp_horizon,
            )
            .order_by(Event.start_datetime)
        ).all()
    )


def _pending_debriefings_section() -> list[Assignment]:
    """Assignments where the user has a completed event but no debriefing yet."""
    if not current_user.has_permission("debriefing.submit_own"):
        return []
    from app.models.assignment import DebriefingRecord  # pylint: disable=import-outside-toplevel

    return list(
        db.session.scalars(
            db.select(Assignment)
            .join(EventSpot, Assignment.spot_id == EventSpot.id)
            .join(Event, EventSpot.event_id == Event.id)
            .outerjoin(DebriefingRecord, DebriefingRecord.assignment_id == Assignment.id)
            .where(
                Assignment.user_id == current_user.id,
                Event.status == EventStatus.COMPLETED,
                DebriefingRecord.id == None,  # noqa: E711
            )
            .order_by(Event.start_datetime.desc())
        ).all()
    )


@main_bp.route("/dashboard")
@login_required
def dashboard() -> str:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=current_user.dashboard_horizon_days)

    my_events, assigned_ids = _my_events_section(now, horizon)
    open_events, open_events_all = _open_events_section(now, horizon, assigned_ids)

    pending_activations: list[UserAccount] = []
    if current_user.has_permission("user.activate"):
        pending_activations = list(
            db.session.scalars(
                db.select(UserAccount)
                .where(UserAccount.is_active == sa.false())
                .where(UserAccount.is_archived == sa.false())
                .order_by(UserAccount.created_at)
            ).all()
        )

    return render_template(
        "main/dashboard.html",
        my_events=my_events,
        open_events=open_events,
        open_events_all=open_events_all,
        attention_events=_attention_events_section(now, horizon),
        equipment_shortage_events=_equipment_shortage_events(now, horizon),
        pending_activations=pending_activations,
        missing_rp_events=_missing_rp_events_section(now),
        pending_debriefings=_pending_debriefings_section(),
        horizon_days=current_user.dashboard_horizon_days,
        EventStatus=EventStatus,
    )
