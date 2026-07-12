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

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import sqlalchemy as sa
from flask import Blueprint, Response, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from app.printout_generator import generate_printout
from app.queries import active_master_events_list, active_users_list
from app.utils import czech_sort_key, get_app_tz, quick_date_ranges, require_permission

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _quick_ranges() -> list[tuple[str, str, str]]:
    """Delegate to the shared helper in app.utils."""
    return quick_date_ranges()


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
    """Compute UserStats from a list of (assignment, event) pairs for a single user.

    Note: ``next_shift`` is NOT set here — call ``_resolve_next_shifts``
    afterwards so the value reflects the true global next assignment.
    """
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
    return stats


def _build_user_stat_rows(pairs: list[tuple[Assignment, Event]], now: datetime) -> list[tuple[UserAccount, UserStats]]:
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
    _resolve_next_shifts(result, now)
    result.sort(key=lambda x: czech_sort_key(x[0].name))
    return result


def _resolve_next_shifts(rows: list[tuple[UserAccount, UserStats]], now: datetime) -> None:
    """Set ``next_shift`` on each UserStats via a single global query.

    ``next_shift`` always reflects the user's true next future assignment,
    regardless of whatever date-range or ME filter the report applies.
    Used by all three report types (user, ME, date-range).
    """
    user_ids = [u.id for u, _ in rows]
    if not user_ids:
        return

    next_shifts: dict[uuid.UUID, datetime] = {
        row[0]: row[1]
        for row in db.session.execute(
            db.select(Assignment.user_id, func.min(Event.start_datetime))
            .join(EventSpot, Assignment.spot_id == EventSpot.id)
            .join(Event, EventSpot.event_id == Event.id)
            .where(
                Assignment.user_id.in_(user_ids),
                Event.status.in_(_FUTURE_STATUSES),
                Event.start_datetime > now,
            )
            .group_by(Assignment.user_id)
        ).all()
    }
    for _user, stats in rows:
        stats.next_shift = next_shifts.get(_user.id)


# ── CSV helper ────────────────────────────────────────────────────────────────


def _csv_response(rows: list[list[str]], filename: str) -> Response:
    """Return rows as a downloadable CSV file (UTF-8 with BOM for Excel)."""
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM so Excel auto-detects encoding
    writer = csv.writer(buf)
    writer.writerows(rows)
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _spot_and_assignment_data(event_ids: list[int], events: list[Event]) -> tuple[
    dict[int, tuple[int, int]],
    list[tuple[Assignment, Event]],
]:
    """Shared queries for spot aggregation and assignment pairs.

    Returns (spot_map, pairs) where:
      spot_map: event_id → (total_spots, filled_spots)
      pairs: list of (Assignment, Event) tuples
    """
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
    spot_map: dict[int, tuple[int, int]] = {row.event_id: (row.total_spots, row.filled_spots) for row in spot_agg}

    asgn_rows = db.session.execute(
        db.select(Assignment, EventSpot.event_id)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id.in_(event_ids))
    ).all()
    event_map = {ev.id: ev for ev in events}
    pairs = [(row.Assignment, event_map[row.event_id]) for row in asgn_rows]

    return spot_map, pairs


def _build_event_rows(
    events: list[Event],
    spot_map: dict[int, tuple[int, int]],
) -> tuple[list[dict], int, int, Decimal, int]:
    """Build per-event row dicts and accumulate grand totals.

    Returns (rows, grand_total_spots, grand_filled_spots, grand_worked_hours, grand_patients).
    """
    rows = []
    grand_total_spots = 0
    grand_filled_spots = 0
    grand_worked_hours = Decimal("0")
    grand_patients = 0

    for ev in events:
        total_spots, filled_spots = spot_map.get(ev.id, (0, 0))
        worked_hours = ev.actual_hours or Decimal("0")
        patients = ev.post_event_count or 0

        rows.append(
            {
                "event": ev,
                "total_spots": total_spots,
                "filled_spots": filled_spots,
                "worked_hours": worked_hours,
                "patients": patients,
            }
        )

        grand_total_spots += total_spots
        grand_filled_spots += filled_spots
        grand_worked_hours += worked_hours
        grand_patients += patients

    return rows, grand_total_spots, grand_filled_spots, grand_worked_hours, grand_patients


def _user_stat_csv_rows(user_stat_rows: list[tuple[UserAccount, UserStats]]) -> list[list[str]]:
    """Build CSV rows for the per-user statistics section."""
    header = [
        "Účastník",
        "Směny odsloužené",
        "Směny plánované",
        "Hodiny odsloužené",
        "Hodiny plánované",
        "Hodiny celkem",
        "Hodiny zdarma",
        "Poslední směna",
        "Příští směna",
    ]
    rows = [header]
    for u, s in user_stat_rows:
        rows.append(
            [
                u.name,
                str(s.shifts_served),
                str(s.shifts_planned),
                f"{s.hours_served:.1f}",
                f"{s.hours_planned:.1f}",
                f"{s.hours_total:.1f}",
                f"{s.hours_free:.1f}",
                s.last_shift.astimezone(get_app_tz()).strftime("%Y-%m-%d") if s.last_shift else "",
                s.next_shift.astimezone(get_app_tz()).strftime("%Y-%m-%d") if s.next_shift else "",
            ]
        )
    return rows


# ── Index ─────────────────────────────────────────────────────────────────────


@reports_bp.get("/")
@login_required
def index() -> str:
    require_permission("report.view")
    master_events = active_master_events_list()
    all_users = active_users_list() if current_user.has_permission("report.view") else []
    return render_template("reports/index.html", master_events=master_events, all_users=all_users)


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

    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()

    from_dt: datetime | None = None
    to_dt: datetime | None = None
    date_error: str | None = None

    if from_date_str or to_date_str:
        try:
            if from_date_str:
                from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if to_date_str:
                to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            date_error = "Neplatný formát data."
            from_date_str = ""
            to_date_str = ""

    now = datetime.now(timezone.utc)

    # Load assignments for this user with eager-loaded spot → event
    query = (
        db.select(Assignment)
        .where(Assignment.user_id == user_id)
        .join(Assignment.spot)
        .join(EventSpot.event)
        .options(
            selectinload(Assignment.spot).selectinload(EventSpot.event),  # type: ignore[arg-type]
        )
        .order_by(Event.start_datetime)
    )
    if from_dt:
        query = query.where(Event.start_datetime >= from_dt)
    if to_dt:
        query = query.where(Event.start_datetime < to_dt)

    assignments = list(db.session.scalars(query).unique().all())

    pairs = [(a, a.spot.event) for a in assignments if a.spot and a.spot.event]
    stats = _compute_user_stats(pairs, now)
    _resolve_next_shifts([(user, stats)], now)

    # Build per-event rows for the detail table
    rows = []
    for _, ev in pairs:
        rows.append(
            {
                "event": ev,
                "planned_hours": ev.scheduled_hours,
                "actual_hours": ev.actual_hours,
            }
        )

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
            [
                "Poslední směna",
                stats.last_shift.astimezone(get_app_tz()).strftime("%Y-%m-%d") if stats.last_shift else "",
            ],
            [
                "Příští směna",
                stats.next_shift.astimezone(get_app_tz()).strftime("%Y-%m-%d") if stats.next_shift else "",
            ],
            [],
            ["Akce", "Začátek", "Konec", "Stav", "Plán (h)", "Skutečnost (h)"],
        ]
        for r in rows:
            ev = r["event"]
            csv_rows.append(
                [
                    ev.name,
                    ev.start_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                    ev.end_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                    ev.status.value,
                    f"{r['planned_hours']:.1f}",
                    f"{r['actual_hours']:.1f}" if r["actual_hours"] is not None else "",
                ]
            )
        safe_name = user.name.replace(" ", "_")
        date_suffix = f"_{from_date_str}_{to_date_str}" if from_date_str or to_date_str else ""
        return _csv_response(csv_rows, f"prehled_{safe_name}{date_suffix}.csv")

    return render_template(
        "reports/user_report.html",
        report_user=user,
        rows=rows,
        stats=stats,
        is_own=is_own,
        from_date=from_date_str,
        to_date=to_date_str,
        date_error=date_error,
        quick_ranges=_quick_ranges(),
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

    events: list[Event] = list(
        db.session.scalars(db.select(Event).where(Event.master_event_id == me_id).order_by(Event.start_datetime)).all()
    )
    event_ids = [ev.id for ev in events]

    spot_map, pairs = _spot_and_assignment_data(event_ids, events)

    status_counts: dict[str, int] = {}
    for ev in events:
        key = ev.status.value
        status_counts[key] = status_counts.get(key, 0) + 1

    rows, grand_total_spots, grand_filled_spots, grand_worked_hours, grand_patients = _build_event_rows(
        events, spot_map
    )
    user_stat_rows = _build_user_stat_rows(pairs, now)

    if request.args.get("format") == "csv":
        csv_rows: list[list[str]] = [
            ["Akce", "Začátek", "Konec", "Stav", "Místa celkem", "Obsazená místa", "Odprac. hodin", "Ošetřených"]
        ]
        for r in rows:
            csv_ev = cast(Event, r["event"])
            csv_rows.append(
                [
                    csv_ev.name,
                    csv_ev.start_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                    csv_ev.end_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                    csv_ev.status.value,
                    str(r["total_spots"]),
                    str(r["filled_spots"]),
                    f"{r['worked_hours']:.1f}",
                    str(r["patients"]),
                ]
            )
        csv_rows.append([])
        csv_rows.extend(_user_stat_csv_rows(user_stat_rows))
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


def _aggregate_date_range(
    events: list[Event],
    spot_map: dict[int, tuple[int, int]],
    pairs: list[tuple[Assignment, Event]],
) -> dict:
    """Build the results dict for a date-range report."""
    me_map: dict[int, dict] = {}
    for ev in events:
        me_id_key = ev.master_event_id
        if me_id_key not in me_map:
            me_map[me_id_key] = {"master_event": ev.master_event, "events": []}
        me_map[me_id_key]["events"].append(ev)

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

    now = datetime.now(timezone.utc)
    user_stat_rows = _build_user_stat_rows(pairs, now)

    return {
        "me_groups": list(me_map.values()),
        "status_counts": status_counts,
        "total_events": len(events),
        "total_spots": total_spots,
        "filled_spots": filled_spots,
        "total_worked_hours": total_worked_hours,
        "total_patients": total_patients,
        "user_stat_rows": user_stat_rows,
    }


def _date_range_csv(
    events: list[Event],
    spot_map: dict[int, tuple[int, int]],
    user_stat_rows: list[tuple[UserAccount, UserStats]],
) -> list[list[str]]:
    """Build CSV rows for the date-range report."""
    rows: list[list[str]] = [
        [
            "Nadřazená akce",
            "Akce",
            "Začátek",
            "Konec",
            "Stav",
            "Místa celkem",
            "Obsazená místa",
            "Odprac. hodin",
            "Ošetřených",
        ]
    ]
    for ev in events:
        me_name = ev.master_event.name if ev.master_event else ""
        t_s, f_s = spot_map.get(ev.id, (0, 0))
        rows.append(
            [
                me_name,
                ev.name,
                ev.start_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                ev.end_datetime.astimezone(get_app_tz()).strftime("%Y-%m-%d %H:%M"),
                ev.status.value,
                str(t_s),
                str(f_s),
                f"{ev.actual_hours or Decimal('0'):.1f}",
                str(ev.post_event_count or 0),
            ]
        )
    rows.append([])
    rows.extend(_user_stat_csv_rows(user_stat_rows))
    return rows


@reports_bp.get("/date-range")
@login_required
def date_range_report() -> str | Response:
    require_permission("report.view")

    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()

    if not from_date_str or not to_date_str:
        return render_template(
            "reports/date_range.html",
            results=None,
            from_date=from_date_str,
            to_date=to_date_str,
            quick_ranges=_quick_ranges(),
        )
    try:
        from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        return render_template(
            "reports/date_range.html",
            results=None,
            from_date=from_date_str,
            to_date=to_date_str,
            error="Neplatný formát data.",
            quick_ranges=_quick_ranges(),
        )

    events: list[Event] = list(
        db.session.scalars(
            db.select(Event)
            .where(Event.start_datetime >= from_dt)
            .where(Event.start_datetime < to_dt)
            .options(selectinload(Event.master_event))  # type: ignore[arg-type]
            .order_by(Event.start_datetime)
        )
        .unique()
        .all()
    )

    spot_map, pairs = _spot_and_assignment_data([ev.id for ev in events], events)
    results = _aggregate_date_range(events, spot_map, pairs)

    if request.args.get("format") == "csv":
        csv_rows = _date_range_csv(events, spot_map, results["user_stat_rows"])
        return _csv_response(csv_rows, f"prehled_{from_date_str}_{to_date_str}.csv")

    return render_template(
        "reports/date_range.html",
        results=results,
        from_date=from_date_str,
        to_date=to_date_str,
        quick_ranges=_quick_ranges(),
    )


# ── Printout (Excel export) ───────────────────────────────────────────────────


@reports_bp.route("/printout", methods=["GET", "POST"])
@login_required
def printout() -> str | Response:
    require_permission("report.view")

    master_events = active_master_events_list()

    if request.method == "GET":
        return render_template("reports/printout.html", master_events=master_events)

    from_date_str = request.form.get("from_date", "").strip()
    to_date_str = request.form.get("to_date", "").strip()
    me_id_str = request.form.get("me_id", "").strip()

    has_dates = bool(from_date_str or to_date_str)

    if not has_dates and not me_id_str:
        flash("Zadejte alespoň datum nebo nadřazenou akci.", "danger")
        return render_template("reports/printout.html", master_events=master_events)

    from_dt = to_dt = None
    if has_dates:
        try:
            from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            flash("Neplatné datum — vyplňte obě pole nebo obě nechte prázdná.", "danger")
            return render_template("reports/printout.html", master_events=master_events)

        if from_dt >= to_dt:
            flash("Datum 'od' musí být před datem 'do'.", "danger")
            return render_template("reports/printout.html", master_events=master_events)

    query = (
        db.select(Event)
        .where(Event.status != EventStatus.DRAFT)
        .where(Event.archived == sa.false())
        .options(
            selectinload(Event.spots).selectinload(EventSpot.required_qualifications),
            selectinload(Event.spots).selectinload(EventSpot.assignment).selectinload(Assignment.user),
        )
        .order_by(Event.start_datetime)
    )

    if from_dt and to_dt:
        query = query.where(Event.start_datetime >= from_dt).where(Event.start_datetime < to_dt)

    if me_id_str:
        query = query.where(Event.master_event_id == int(me_id_str))

    events = db.session.scalars(query).unique().all()

    if not events:
        flash("Žádné akce nevyhovovaly zadaným filtrům.", "warning")
        return render_template("reports/printout.html", master_events=master_events)

    me_name: str | None = None
    if me_id_str:
        me = db.session.get(MasterEvent, int(me_id_str))
        me_name = me.name if me else None

    date_range = f"{from_date_str} – {to_date_str}" if has_dates else "vše"
    wb = generate_printout(list(events), date_range, me_name)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"sestava_{from_date_str}_{to_date_str}.xlsx"
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
