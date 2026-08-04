"""
Debriefing blueprint — post-event feedback submitted per assignment.

A debriefing record captures confidential feedback from each participant
after an event is completed. Only users with debriefing.view_all
(Debriefing Manager role) may read confidential responses.

The responsible person (RP) additionally updates the event with actual
start/end times and the count of patients treated (počet ošetřených).

Submission is final — records cannot be edited once submitted.

Routes:
  GET/POST /debriefing/<assignment_id>  — submit debriefing (own only)
  GET      /debriefing/manage           — list all records (Debriefing Manager)
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.assignment import Assignment, DebriefingRecord
from app.models.event import Event, EventSpot, EventStatus, EventType
from app.utils import audit, diff_changes, get_app_tz, get_or_404, quick_date_ranges, require_permission

debriefing_bp = Blueprint("debriefing", __name__, url_prefix="/debriefing")


# ── Submit a debriefing ───────────────────────────────────────────────────────


def _parse_grade(raw: str) -> tuple[int, str | None]:
    """Validate the 1–5 grade. Return (grade, error_message)."""
    try:
        grade = int(raw)
    except ValueError:
        return 0, "Hodnocení musí být číslo od 1 do 5."
    if grade not in range(1, 6):
        return 0, "Hodnocení musí být číslo od 1 do 5."
    return grade, None


def _parse_rp_actuals(
    form: dict,
    event_type: EventType,
) -> tuple[datetime | None, datetime | None, int | None, list[str]]:
    """Parse and validate the RP-only actual start/end and post-event count.

    For MEDICAL_COVER: actual start/end and post_event_count are required.
    For TRAINING: all fields are optional.
    For PRESENTATION: this function should not be called (no RP section).

    Returns (actual_start_utc, actual_end_utc, post_event_count, errors).
    """
    tz = get_app_tz()

    errors: list[str] = []
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    post_event_count: int | None = None

    is_training = event_type == EventType.TRAINING

    start_str = form.get("actual_start_datetime", "").strip()
    end_str = form.get("actual_end_datetime", "").strip()

    if start_str:
        try:
            actual_start = datetime.fromisoformat(start_str).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError, TypeError:
            errors.append("Zadejte platný skutečný čas začátku.")
    elif not is_training:
        errors.append("Zadejte platný skutečný čas začátku.")

    if end_str:
        try:
            actual_end = datetime.fromisoformat(end_str).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError, TypeError:
            errors.append("Zadejte platný skutečný čas konce.")
    elif not is_training:
        errors.append("Zadejte platný skutečný čas konce.")

    if actual_start and actual_end and actual_end <= actual_start:
        errors.append("Čas konce musí být po čase začátku.")

    count_str = form.get("post_event_count", "").strip()
    if count_str:
        try:
            post_event_count = int(count_str)
            if post_event_count < 0 or post_event_count > 999:
                raise ValueError
        except ValueError:
            label = "Počet účastníků" if is_training else "Počet ošetřených"
            errors.append(f"{label} musí být celé číslo (0 nebo více).")
    elif not is_training:
        errors.append("Počet ošetřených musí být celé číslo (0 nebo více).")

    return actual_start, actual_end, post_event_count, errors


def _apply_rp_actuals_to_event(
    event: Event,
    actual_start: datetime | None,
    actual_end: datetime | None,
    post_event_count: int | None,
) -> None:
    """Update event with RP-supplied actuals and write an audit entry."""
    before = {
        "actual_start_datetime": str(event.actual_start_datetime),
        "actual_end_datetime": str(event.actual_end_datetime),
        "post_event_count": event.post_event_count,
    }
    event.actual_start_datetime = actual_start
    event.actual_end_datetime = actual_end
    event.post_event_count = post_event_count
    event.version += 1
    audit(
        "edit",
        "Event",
        str(event.id),
        f"Aktuální časy a výsledný počet aktualizovány pro akci '{event.name}'",
        diff_changes(
            before,
            {
                "actual_start_datetime": str(actual_start),
                "actual_end_datetime": str(actual_end),
                "post_event_count": post_event_count,
            },
        ),
    )


@debriefing_bp.route("/<int:assignment_id>", methods=["GET", "POST"])
@login_required
def submit(assignment_id: int) -> str | Response:
    assignment = get_or_404(Assignment, assignment_id)
    event: Event = assignment.spot.event

    # Only the assigned user may submit their own debriefing
    if assignment.user_id != current_user.id:
        abort(403)

    # Debriefing only allowed after event is Completed
    if event.status != EventStatus.COMPLETED:
        flash("Debriefing lze vyplnit až po dokončení akce.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    # Submission is final — show read-only view if already submitted
    if assignment.debriefing is not None:
        return render_template(
            "debriefing/submitted.html",
            assignment=assignment,
            event=event,
            record=assignment.debriefing,
            EventType=EventType,
        )

    is_rp = event.responsible_person_id == current_user.id
    # PRESENTATION events have no RP section
    has_rp_section = is_rp and event.event_type != EventType.PRESENTATION

    if request.method != "POST":
        return render_template(
            "debriefing/submit.html",
            assignment=assignment,
            event=event,
            is_rp=is_rp,
            has_rp_section=has_rp_section,
            EventType=EventType,
        )

    # ── Validate ──────────────────────────────────────────────────────────────
    errors: list[str] = []
    grade, grade_err = _parse_grade(request.form.get("grade", "").strip())
    if grade_err:
        errors.append(grade_err)

    feedback_event = request.form.get("feedback_event", "").strip() or None
    feedback_customer = request.form.get("feedback_customer", "").strip() or None
    feedback_colleagues = request.form.get("feedback_colleagues", "").strip() or None

    actual_start: datetime | None = None
    actual_end: datetime | None = None
    post_event_count: int | None = None
    if has_rp_section:
        actual_start, actual_end, post_event_count, rp_errors = _parse_rp_actuals(request.form, event.event_type)
        errors.extend(rp_errors)

    if errors:
        for e in errors:
            flash(e, "danger")
        return render_template(
            "debriefing/submit.html",
            assignment=assignment,
            event=event,
            is_rp=is_rp,
            has_rp_section=has_rp_section,
            EventType=EventType,
        )

    # ── Persist confidential record ───────────────────────────────────────────
    record = DebriefingRecord(
        assignment_id=assignment_id,
        submitted_by_id=current_user.id,
        grade=grade,
        feedback_event=feedback_event,
        feedback_customer=feedback_customer,
        feedback_colleagues=feedback_colleagues,
    )
    db.session.add(record)
    db.session.flush()
    audit("create", "DebriefingRecord", str(record.id), f"Debriefing odevzdán pro akci '{event.name}'")

    # Apply RP actuals when at least start/end are set (all optional for TRAINING)
    if has_rp_section and (actual_start or actual_end or post_event_count is not None):
        _apply_rp_actuals_to_event(event, actual_start, actual_end, post_event_count)

    db.session.commit()
    flash("Debriefing byl úspěšně odevzdán. Děkujeme.", "success")
    return redirect(url_for("events.detail", event_id=event.id))


# ── Debriefing management (Debriefing Manager only) ───────────────────────────


@debriefing_bp.get("/manage")
@login_required
def manage() -> str:
    require_permission("debriefing.view_all")

    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()

    query = (
        db.select(Event)
        .where(Event.status == EventStatus.COMPLETED)
        .order_by(Event.start_datetime.desc())
        # Eager-load the whole spot → assignment → debriefing chain so the
        # template's selectattr('debriefing') / rejectattr('debriefing')
        # filters don't fire one lazy SELECT per assignment.
        .options(
            selectinload(Event.spots).selectinload(EventSpot.assignment).selectinload(Assignment.debriefing),
        )
    )

    if from_date_str:
        try:
            from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.where(Event.start_datetime >= from_dt)
        except ValueError:
            from_date_str = ""

    if to_date_str:
        try:
            to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.where(Event.start_datetime < to_dt)
        except ValueError:
            to_date_str = ""

    events_with_debriefings = db.session.scalars(query).all()

    return render_template(
        "debriefing/manage.html",
        events=events_with_debriefings,
        from_date=from_date_str,
        to_date=to_date_str,
        quick_ranges=quick_date_ranges(),
    )


# ── Event debriefing detail (Debriefing Manager only) ─────────────────────────


@debriefing_bp.get("/event/<int:event_id>")
@login_required
def event_overview(event_id: int) -> str:
    require_permission("debriefing.view_all")

    event = get_or_404(Event, event_id)

    assignments = [s.assignment for s in event.spots if s.assignment is not None]
    return render_template("debriefing/event_overview.html", event=event, assignments=assignments)
