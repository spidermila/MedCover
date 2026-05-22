from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, render_template
from flask_login import login_required, current_user
from sqlalchemy import or_

from app.extensions import db
from app.models.event import Event, EventSpot, EventStatus
from app.models.assignment import Assignment
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
    assigned_event_id_set: set = set(db.session.scalars(
        db.select(EventSpot.event_id)
        .join(Assignment, Assignment.spot_id == EventSpot.id)
        .where(Assignment.user_id == current_user.id)
    ).all())

    my_events_query = (
        db.select(Event)
        .where(
            Event.archived.is_(False),
            Event.end_datetime >= now,
            Event.start_datetime <= horizon,
            Event.status != EventStatus.CANCELLED,
            or_(
                Event.id.in_(assigned_event_id_set),
                Event.responsible_person_id == current_user.id,
                Event.created_by_id == current_user.id,
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
        if e.created_by_id == current_user.id:
            tags.append("Koordinátor")
        tagged.append((e, tags))
    return tagged, assigned_event_id_set


def _open_events_section(
    now: datetime, horizon: datetime, already_in: set[int],
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
    eligible = [
        e for e in candidates
        if any(s.assignment is None and s.is_eligible_for(fillable_ids) for s in e.spots)
    ]
    all_open = [
        e for e in candidates
        if any(s.assignment is None for s in e.spots)
    ]
    return eligible, all_open


def _attention_events_section(now: datetime, horizon: datetime) -> list[Event]:
    """Events a coordinator should pay attention to."""
    if not current_user.has_any_permission("event.publish", "event.assignments.open"):
        return []

    events = list(db.session.scalars(
        db.select(Event)
        .where(
            Event.archived.is_(False),
            Event.status.in_([EventStatus.DRAFT, EventStatus.PUBLISHED, EventStatus.ASSIGNMENTS_OPEN]),
            Event.start_datetime <= horizon,
            Event.end_datetime >= now,
        )
        .order_by(Event.start_datetime)
    ).all())
    return [
        e for e in events
        if e.status in (EventStatus.DRAFT, EventStatus.PUBLISHED)
        or (e.status == EventStatus.ASSIGNMENTS_OPEN and e.mandatory_filled_spots < e.mandatory_total_spots)
    ]


def _missing_rp_events_section(now: datetime) -> list[Event]:
    """Events in the next 7 days without a responsible person."""
    if not current_user.has_any_permission("event.publish", "event.assignments.open"):
        return []
    rp_horizon = now + timedelta(days=7)
    return list(db.session.scalars(
        db.select(Event)
        .where(
            Event.archived.is_(False),
            Event.status.notin_([EventStatus.DRAFT, EventStatus.CANCELLED]),
            Event.responsible_person_id == None,  # noqa: E711
            Event.start_datetime >= now,
            Event.start_datetime <= rp_horizon,
        )
        .order_by(Event.start_datetime)
    ).all())


def _pending_debriefings_section() -> list[Assignment]:
    """Assignments where the user has a completed event but no debriefing yet."""
    if not current_user.has_permission("debriefing.submit_own"):
        return []
    from app.models.assignment import DebriefingRecord
    return list(db.session.scalars(
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
    ).all())


@main_bp.route("/dashboard")
@login_required
def dashboard() -> str:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=current_user.dashboard_horizon_days)

    my_events, assigned_ids = _my_events_section(now, horizon)
    open_events, open_events_all = _open_events_section(now, horizon, assigned_ids)

    pending_activations: list[UserAccount] = []
    if current_user.has_permission("user.activate"):
        pending_activations = list(db.session.scalars(
            db.select(UserAccount)
            .where(UserAccount.is_active.is_(False))
            .where(UserAccount.is_archived.is_(False))
            .order_by(UserAccount.created_at)
        ).all())

    return render_template(
        "main/dashboard.html",
        my_events=my_events,
        open_events=open_events,
        open_events_all=open_events_all,
        attention_events=_attention_events_section(now, horizon),
        pending_activations=pending_activations,
        missing_rp_events=_missing_rp_events_section(now),
        pending_debriefings=_pending_debriefings_section(),
        horizon_days=current_user.dashboard_horizon_days,
        EventStatus=EventStatus,
    )
