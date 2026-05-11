"""
Reports & Statistics blueprint.

Routes:
  GET /reports/                          — index (three report cards)
  GET /reports/user                      — own per-user report (shortcut)
  GET /reports/user/<user_id>            — per-user report
  GET /reports/master-event/<me_id>      — per-master-event report
  GET /reports/date-range                — date-range report (form + results)

All report routes accept ?format=csv to download the data as a CSV file.

Permission: report.view  (users may always view their own per-user report)
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import cast

from flask import Blueprint, Response, abort, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.utils import require_permission
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

# ── Statistics helpers ────────────────────────────────────────────────────────

_FUTURE_STATUSES = {
    EventStatus.PUBLISHED,
    EventStatus.ASSIGNMENTS_OPEN,
    EventStatus.ASSIGNMENTS_CLOSED,
}


@dataclass
class UserStats:
    """Aggregated participation statistics for one user."""

    shifts_served: int = 0
    shifts_planned: int = 0
    hours_served: Decimal = field(default_factory=lambda: Decimal("0"))
    hours_planned: Decimal = field(default_factory=lambda: Decimal("0"))
    hours_free: Decimal = field(default_factory=lambda: Decimal("0"))
    last_shift: datetime | None = None
    next_shift: datetime | None = None

    @property
    def shifts_total(self) -> int:
        return self.shifts_served + self.shifts_planned

    @property
    def hours_total(self) -> Decimal:
        return self.hours_served + self.hours_planned


def _compute_user_stats(pairs: list[tuple[Assignment, Event]], now: datetime) -> UserStats:
    """Compute UserStats from a list of (assignment, event) pairs for a single user."""
    stats = UserStats()
    for _, ev in pairs:
        if ev.status == EventStatus.CANCELLED:
            continue
        planned_h = ev.scheduled_hours
        if ev.status == EventStatus.COMPLETED:
            stats.shifts_served += 1
            # Use actual hours when available, fall back to planned hours.
            served_h = ev.actual_hours if ev.actual_hours is not None else planned_h
            stats.hours_served += served_h
            if not ev.paid:
                stats.hours_free += served_h
            if stats.last_shift is None or ev.start_datetime > stats.last_shift:
                stats.last_shift = ev.start_datetime
        elif ev.status in _FUTURE_STATUSES and ev.start_datetime > now:
            stats.shifts_planned += 1
            stats.hours_planned += planned_h
            if stats.next_shift is None or ev.start_datetime < stats.next_shift:
                stats.next_shift = ev.start_datetime
    return stats


def _build_user_stat_rows(
    pairs: list[tuple[Assignment, Event]], now: datetime
) -> list[tuple[UserAccount, UserStats]]:
    """Group (assignment, event) pairs by user and compute per-user stats."""
    user_pairs: dict[uuid.UUID, list[tuple[Assignment, Event]]] = {}
    users: dict[uuid.UUID, UserAccount] = {}
    for asgn, ev in pairs:
        uid = asgn.user_id
        if uid not in user_pairs:
            user_pairs[uid] = []
            users[uid] = asgn.user
        user_pairs[uid].append((asgn, ev))
    result = [(users[uid], _compute_user_stats(up, now)) for uid, up in user_pairs.items()]
    result.sort(key=lambda x: x[0].name)
    return result


# ── CSV helper ────────────────────────────────────────────────────────────────


def _csv_response(rows: list[list[str]], filename: str) -> Response:
    """Return rows as a downloadable CSV file."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ── Index ─────────────────────────────────────────────────────────────────────

@reports_bp.get("/")
@login_required
def index() -> str:
    require_permission("report.view")
    from app.queries import active_master_events_list
    master_events = active_master_events_list()
    return render_template("reports/index.html", master_events=master_events)


# ── Per-user report ───────────────────────────────────────────────────────────

@reports_bp.get("/user")
@login_required
def own_report() -> Response:
    return redirect(url_for("reports.user_report", user_id=current_user.id))


@reports_bp.get("/user/<uuid:user_id>")
@login_required
def user_report(user_id: uuid.UUID) -> str | Response:
    is_own = str(user_id) == str(current_user.id)
    if not is_own and not current_user.has_permission("report.view"):
        abort(403)

    user: UserAccount | None = db.session.get(UserAccount, user_id)
    if user is None:
        abort(404)

    now = datetime.now(timezone.utc)

    # Load all assignments for this user with eager-loaded spot → event
    assignments = list(db.session.scalars(
        db.select(Assignment)
        .where(Assignment.user_id == user_id)
        .options(
            selectinload(Assignment.spot).selectinload(EventSpot.event),  # type: ignore[arg-type]
        )
        .order_by(Assignment.assigned_at)
    ).unique().all())

    pairs = [(a, a.spot.event) for a in assignments if a.spot and a.spot.event]
    stats = _compute_user_stats(pairs, now)

    # Build per-event rows for the detail table
    rows = []
    for _, ev in pairs:
        rows.append({
            "event": ev,
            "planned_hours": ev.scheduled_hours,
            "actual_hours": ev.actual_hours,
        })

    if request.args.get("format") == "csv":
        csv_rows: list[list[str]] = [
            ["Statistiky"],
            ["Směny odsloužené", str(stats.shifts_served)],
            ["Směny plánované", str(stats.shifts_planned)],
            ["Směny celkem", str(stats.shifts_total)],
            ["Hodiny odsloužené", f"{stats.hours_served:.1f}"],
            ["Hodiny plánované", f"{stats.hours_planned:.1f}"],
            ["Hodiny celkem", f"{stats.hours_total:.1f}"],
            ["Hodiny celkem zdarma", f"{stats.hours_free:.1f}"],
            ["Poslední směna", stats.last_shift.strftime("%Y-%m-%d") if stats.last_shift else ""],
            ["Příští směna", stats.next_shift.strftime("%Y-%m-%d") if stats.next_shift else ""],
            [],
            ["Akce", "Začátek", "Konec", "Stav", "Plán (h)", "Skutečnost (h)"],
        ]
        for r in rows:
            ev = r["event"]
            csv_rows.append([
                ev.name,
                ev.start_datetime.strftime("%Y-%m-%d %H:%M"),
                ev.end_datetime.strftime("%Y-%m-%d %H:%M"),
                ev.status.value,
                f"{r['planned_hours']:.1f}",
                f"{r['actual_hours']:.1f}" if r["actual_hours"] is not None else "",
            ])
        safe_name = user.name.replace(" ", "_")
        return _csv_response(csv_rows, f"prehled_{safe_name}.csv")

    return render_template(
        "reports/user_report.html",
        report_user=user,
        rows=rows,
        stats=stats,
        is_own=is_own,
    )


# ── Per-Master-Event report ───────────────────────────────────────────────────

@reports_bp.get("/master-event/<int:me_id>")
@login_required
def me_report(me_id: int) -> str | Response:
    require_permission("report.view")

    master_event: MasterEvent | None = db.session.get(MasterEvent, me_id)
    if master_event is None:
        abort(404)

    now = datetime.now(timezone.utc)

    # Query 1: events (no spots relationship — counts come from SQL aggregation below)
    events: list[Event] = list(db.session.scalars(
        db.select(Event)
        .where(Event.master_event_id == me_id)
        .order_by(Event.start_datetime)
    ).all())

    event_ids = [ev.id for ev in events]

    # Query 2: spot/fill counts via SQL GROUP BY — avoids loading all EventSpot ORM objects
    spot_agg = db.session.execute(
        db.select(
            EventSpot.event_id,
            func.count(EventSpot.id).label("total_spots"),
            func.count(Assignment.id).label("filled_spots"),
        )
        .outerjoin(Assignment, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id.in_(event_ids))
        .group_by(EventSpot.event_id)
    ).all()
    spot_map: dict[int, tuple[int, int]] = {
        row.event_id: (row.total_spots, row.filled_spots) for row in spot_agg
    }

    # Query 3: assignments joined to event_spot to get (assignment, event_id) pairs
    # Assignment.user is loaded automatically via lazy="selectin"
    asgn_rows = db.session.execute(
        db.select(Assignment, EventSpot.event_id)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id.in_(event_ids))
    ).all()
    event_map = {ev.id: ev for ev in events}
    pairs = [(row.Assignment, event_map[row.event_id]) for row in asgn_rows]

    # Count events by status
    status_counts: dict[str, int] = {}
    for ev in events:
        key = ev.status.value
        status_counts[key] = status_counts.get(key, 0) + 1

    # Build per-event rows using pre-computed SQL spot counts
    rows = []
    grand_total_spots = 0
    grand_filled_spots = 0
    grand_worked_hours = Decimal("0")
    grand_patients = 0

    for ev in events:
        total_spots, filled_spots = spot_map.get(ev.id, (0, 0))
        worked_hours = ev.actual_hours or Decimal("0")
        patients = ev.post_event_count or 0

        rows.append({
            "event": ev,
            "total_spots": total_spots,
            "filled_spots": filled_spots,
            "worked_hours": worked_hours,
            "patients": patients,
        })

        grand_total_spots += total_spots
        grand_filled_spots += filled_spots
        grand_worked_hours += worked_hours
        grand_patients += patients

    # Per-user statistics
    user_stat_rows = _build_user_stat_rows(pairs, now)

    if request.args.get("format") == "csv":
        csv_rows: list[list[str]] = [["Akce", "Začátek", "Konec", "Stav", "Místa celkem", "Obsazená místa", "Odprac. hodin", "Ošetřených"]]
        for r in rows:
            csv_ev = cast(Event, r["event"])
            csv_rows.append([
                csv_ev.name,
                csv_ev.start_datetime.strftime("%Y-%m-%d %H:%M"),
                csv_ev.end_datetime.strftime("%Y-%m-%d %H:%M"),
                csv_ev.status.value,
                str(r["total_spots"]),
                str(r["filled_spots"]),
                f"{r['worked_hours']:.1f}",
                str(r["patients"]),
            ])
        csv_rows.append([])
        csv_rows.append(["Účastník", "Směny odsloužené", "Směny plánované", "Hodiny odsloužené", "Hodiny plánované", "Hodiny celkem", "Hodiny zdarma", "Poslední směna", "Příští směna"])
        for u, s in user_stat_rows:
            csv_rows.append([
                u.name,
                str(s.shifts_served),
                str(s.shifts_planned),
                f"{s.hours_served:.1f}",
                f"{s.hours_planned:.1f}",
                f"{s.hours_total:.1f}",
                f"{s.hours_free:.1f}",
                s.last_shift.strftime("%Y-%m-%d") if s.last_shift else "",
                s.next_shift.strftime("%Y-%m-%d") if s.next_shift else "",
            ])
        safe_name = master_event.name.replace(" ", "_")
        return _csv_response(csv_rows, f"prehled_ME_{safe_name}.csv")

    return render_template(
        "reports/me_report.html",
        master_event=master_event,
        events=events,
        rows=rows,
        status_counts=status_counts,
        grand_total_spots=grand_total_spots,
        grand_filled_spots=grand_filled_spots,
        grand_worked_hours=grand_worked_hours,
        grand_patients=grand_patients,
        user_stat_rows=user_stat_rows,
    )


# ── Date-range report ─────────────────────────────────────────────────────────

@reports_bp.get("/date-range")
@login_required
def date_range_report() -> str | Response:
    require_permission("report.view")

    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()

    if not from_date_str or not to_date_str:
        return render_template("reports/date_range.html", results=None, from_date=from_date_str, to_date=to_date_str)

    try:
        from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        return render_template("reports/date_range.html", results=None, from_date=from_date_str, to_date=to_date_str, error="Neplatný formát data.")

    events: list[Event] = list(db.session.scalars(
        db.select(Event)
        .where(Event.start_datetime >= from_dt)
        .where(Event.start_datetime < to_dt)
        .options(
            selectinload(Event.master_event),  # type: ignore[arg-type]
        )
        .order_by(Event.start_datetime)
    ).unique().all())

    event_ids = [ev.id for ev in events]

    # Spot/fill counts via SQL GROUP BY — avoids loading all EventSpot ORM objects
    spot_agg = db.session.execute(
        db.select(
            EventSpot.event_id,
            func.count(EventSpot.id).label("total_spots"),
            func.count(Assignment.id).label("filled_spots"),
        )
        .outerjoin(Assignment, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id.in_(event_ids))
        .group_by(EventSpot.event_id)
    ).all()
    spot_map: dict[int, tuple[int, int]] = {
        row.event_id: (row.total_spots, row.filled_spots) for row in spot_agg
    }

    # Assignments for per-user stats (Assignment.user auto-loaded via selectin)
    asgn_rows = db.session.execute(
        db.select(Assignment, EventSpot.event_id)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id.in_(event_ids))
    ).all()
    event_map = {ev.id: ev for ev in events}
    pairs = [(row.Assignment, event_map[row.event_id]) for row in asgn_rows]

    # Group by master event
    me_map: dict[int, dict] = {}
    for ev in events:
        me_id_key = ev.master_event_id
        if me_id_key not in me_map:
            me_map[me_id_key] = {
                "master_event": ev.master_event,
                "events": [],
            }
        me_map[me_id_key]["events"].append(ev)

    # Aggregate totals
    status_counts: dict[str, int] = {}
    total_spots = 0
    filled_spots = 0
    total_worked_hours = Decimal("0")
    total_patients = 0

    for ev in events:
        key = ev.status.value
        status_counts[key] = status_counts.get(key, 0) + 1
        t, f = spot_map.get(ev.id, (0, 0))
        total_spots += t
        filled_spots += f
        total_worked_hours += ev.actual_hours or Decimal("0")
        total_patients += ev.post_event_count or 0

    # Per-user statistics across the date range
    now = datetime.now(timezone.utc)
    user_stat_rows = _build_user_stat_rows(pairs, now)

    results = {
        "me_groups": list(me_map.values()),
        "status_counts": status_counts,
        "total_events": len(events),
        "total_spots": total_spots,
        "filled_spots": filled_spots,
        "total_worked_hours": total_worked_hours,
        "total_patients": total_patients,
        "user_stat_rows": user_stat_rows,
    }

    if request.args.get("format") == "csv":
        csv_rows = [["Nadřazená akce", "Akce", "Začátek", "Konec", "Stav", "Místa celkem", "Obsazená místa", "Odprac. hodin", "Ošetřených"]]
        for ev in events:
            me_name = ev.master_event.name if ev.master_event else ""
            total_s = len(ev.spots)
            filled_s = sum(1 for s in ev.spots if s.assignment is not None)
            worked_h = ev.actual_hours or Decimal("0")
            patients = ev.post_event_count or 0
            csv_rows.append([
                me_name,
                ev.name,
                ev.start_datetime.strftime("%Y-%m-%d %H:%M"),
                ev.end_datetime.strftime("%Y-%m-%d %H:%M"),
                ev.status.value,
                str(total_s),
                str(filled_s),
                f"{worked_h:.1f}",
                str(patients),
            ])
        csv_rows.append([])
        csv_rows.append(["Účastník", "Směny odsloužené", "Směny plánované", "Hodiny odsloužené", "Hodiny plánované", "Hodiny celkem", "Hodiny zdarma", "Poslední směna", "Příští směna"])
        for u, s in user_stat_rows:
            csv_rows.append([
                u.name,
                str(s.shifts_served),
                str(s.shifts_planned),
                f"{s.hours_served:.1f}",
                f"{s.hours_planned:.1f}",
                f"{s.hours_total:.1f}",
                f"{s.hours_free:.1f}",
                s.last_shift.strftime("%Y-%m-%d") if s.last_shift else "",
                s.next_shift.strftime("%Y-%m-%d") if s.next_shift else "",
            ])
        return _csv_response(csv_rows, f"prehled_{from_date_str}_{to_date_str}.csv")

    return render_template(
        "reports/date_range.html",
        results=results,
        from_date=from_date_str,
        to_date=to_date_str,
    )
