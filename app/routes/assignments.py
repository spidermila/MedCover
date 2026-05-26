"""
Assignment blueprint — spot claim/release with pessimistic locking.

CONCURRENCY — CRITICAL (AD12):
  Every claim uses SELECT FOR UPDATE on the EventSpot row to prevent two
  users from simultaneously claiming the same spot. The check-then-write
  sequence is atomic within a single DB transaction.

Routes:
  POST /assignments/claim/<spot_id>            — claim a spot (own)
  POST /assignments/release/<assignment_id>    — release own assignment
  POST /assignments/assign/<spot_id>           — admin/coordinator assigns a user
  POST /assignments/unassign/<assignment_id>   — admin/coordinator unassigns a user
"""

from __future__ import annotations

from flask import Blueprint, Response, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.utils import audit, get_or_404, require_permission
from app.models.event import Event, EventSpot, EventStatus
from app.models.assignment import Assignment
from app.models.user import UserAccount
import app.mail as mailer

assignments_bp = Blueprint("assignments", __name__, url_prefix="/assignments")


def _auto_close_if_full(event: Event) -> None:
    """Transition event to ASSIGNMENTS_CLOSED when all mandatory spots are filled."""
    if (event.status == EventStatus.ASSIGNMENTS_OPEN
            and event.mandatory_total_spots > 0
            and event.mandatory_filled_spots >= event.mandatory_total_spots):
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        event.version += 1
        audit("status_change", "Event", event.id, "Přihlašování automaticky uzavřeno — všechny pozice obsazeny")


def _auto_assign_rp(event: Event, user: UserAccount) -> None:
    """If event has no RP and user is RP-eligible, assign them as RP."""
    if event.responsible_person_id is None and user.is_rp_eligible():
        event.responsible_person_id = user.id
        event.version += 1
        audit("edit", "Event", event.id, f"Zodpovědná osoba automaticky nastavena na '{user.name}'")


def _auto_clear_rp(event: Event, user: UserAccount) -> None:
    """If the leaving user is the current RP, clear the RP field."""
    if event.responsible_person_id == user.id:
        event.responsible_person_id = None
        event.version += 1
        audit("edit", "Event", event.id, f"Zodpovědná osoba odstraněna — '{user.name}' opustil/a akci")


# ── Claim (own) ───────────────────────────────────────────────────────────────

@assignments_bp.post("/claim/<int:spot_id>")
@login_required
def claim(spot_id: int) -> Response:
    require_permission("event.assign_own")

    # ── Pessimistic lock: SELECT FOR UPDATE ─────────────────────────────────
    spot = db.session.scalar(
        db.select(EventSpot).where(EventSpot.id == spot_id).with_for_update()
    )
    if spot is None:
        abort(404)

    event = get_or_404(Event, spot.event_id)

    # Validate event state
    if event.status != EventStatus.ASSIGNMENTS_OPEN:
        flash("Přihlašování na tuto akci není otevřeno.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    # Spot must be free
    if spot.assignment is not None:
        flash("Tato pozice je již obsazena.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    # User must not already be assigned to this event
    existing = db.session.scalar(
        db.select(Assignment)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id == event.id, Assignment.user_id == current_user.id)
    )
    if existing:
        flash("Již jste přihlášeni na tuto akci.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    # Eligibility check
    if not spot.is_eligible(current_user):
        flash("Nemáte požadovanou kvalifikaci pro tuto pozici.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    spot.assignment = Assignment(
        user_id=current_user.id,
        assigned_by_id=current_user.id,
    )
    db.session.add(spot.assignment)
    db.session.flush()
    audit("create", "Assignment", spot.assignment.id, f"Uživatel '{current_user.name}' se přihlásil na akci '{event.name}'")
    _auto_assign_rp(event, current_user)
    _auto_close_if_full(event)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Tato pozice byla právě obsazena někým jiným.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    flash("Úspěšně přihlášeni na akci.", "success")
    mailer.send_assignment_confirmed(current_user, event)
    return redirect(url_for("events.detail", event_id=event.id))


# ── Release (own) ─────────────────────────────────────────────────────────────

@assignments_bp.post("/release/<int:assignment_id>")
@login_required
def release(assignment_id: int) -> Response:
    assignment = get_or_404(Assignment, assignment_id)

    # Only own assignment unless assigning-other permission
    if assignment.user_id != current_user.id:
        if not current_user.has_permission("event.assign_other"):
            abort(403)

    event = get_or_404(Event, assignment.spot.event_id)

    # Cannot release after event is completed
    if event.status == EventStatus.COMPLETED:
        flash("Nelze se odhlásit z dokončené akce.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    event_id = event.id
    audit("delete", "Assignment", assignment.id, f"Uživatel '{assignment.user.name}' se odhlásil z akce '{event.name}'")
    _auto_clear_rp(event, assignment.user)
    db.session.delete(assignment)

    # Re-open assignments if they were closed and a spot just freed up
    if event.status == EventStatus.ASSIGNMENTS_CLOSED:
        event.status = EventStatus.ASSIGNMENTS_OPEN
        event.version += 1
        audit("status_change", "Event", event.id, "Přihlašování automaticky znovuotevřeno — uvolněna pozice")

    db.session.commit()

    flash("Odhlášení z akce bylo úspěšné.", "success")
    mailer.send_assignment_released(current_user, event)
    return redirect(url_for("events.detail", event_id=event_id))


# ── Assign other ──────────────────────────────────────────────────────────────

@assignments_bp.post("/assign/<int:spot_id>")
@login_required
def assign_other(spot_id: int) -> Response:
    require_permission("event.assign_other")

    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        flash("Vyberte uživatele.", "warning")
        return redirect(request.referrer or url_for("events.index"))

    user = db.session.get(UserAccount, user_id)
    if user is None or not user.is_active or user.is_archived:
        flash("Uživatel nenalezen nebo není aktivní.", "danger")
        return redirect(request.referrer or url_for("events.index"))

    # ── Pessimistic lock ────────────────────────────────────────────────────
    spot = db.session.scalar(
        db.select(EventSpot).where(EventSpot.id == spot_id).with_for_update()
    )
    if spot is None:
        abort(404)

    event = get_or_404(Event, spot.event_id)

    if event.status not in (EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED):
        flash("Přiřazení není možné v aktuálním stavu akce.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    if spot.assignment is not None:
        flash("Tato pozice je již obsazena.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    existing = db.session.scalar(
        db.select(Assignment)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id == event.id, Assignment.user_id == user.id)
    )
    if existing:
        flash(f"Uživatel {user.name} je již přihlášen na tuto akci.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    spot.assignment = Assignment(
        user_id=user.id,
        assigned_by_id=current_user.id,
    )
    db.session.add(spot.assignment)
    db.session.flush()
    audit("create", "Assignment", spot.assignment.id, f"Koordinátor přiřadil '{user.name}' na akci '{event.name}'")
    _auto_assign_rp(event, user)
    _auto_close_if_full(event)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Tato pozice byla právě obsazena někým jiným.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    flash(f"Uživatel {user.name} byl přiřazen na akci.", "success")
    mailer.send_assignment_confirmed(user, event)
    return redirect(url_for("events.detail", event_id=event.id))


# ── Unassign other ────────────────────────────────────────────────────────────

@assignments_bp.post("/unassign/<int:assignment_id>")
@login_required
def unassign_other(assignment_id: int) -> Response:
    require_permission("event.assign_other")

    assignment = get_or_404(Assignment, assignment_id)

    event = get_or_404(Event, assignment.spot.event_id)
    if event.status == EventStatus.COMPLETED:
        flash("Nelze odhlásit uživatele z dokončené akce.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    event_id = event.id
    audit("delete", "Assignment", assignment.id, f"Koordinátor odhlásil '{assignment.user.name}' z akce '{event.name}'")
    _auto_clear_rp(event, assignment.user)
    db.session.delete(assignment)

    if event.status == EventStatus.ASSIGNMENTS_CLOSED:
        event.status = EventStatus.ASSIGNMENTS_OPEN
        event.version += 1

    db.session.commit()

    flash(f"Uživatel {assignment.user.name} byl odhlášen z akce.", "success")
    mailer.send_assignment_released(assignment.user, event)
    return redirect(url_for("events.detail", event_id=event_id))
