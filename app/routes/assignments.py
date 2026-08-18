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

Service functions (shared with master_events table manager):
  do_assign_user()   — lock spot, validate, create assignment, commit
  do_unassign_user() — validate, delete assignment, commit
"""

from dataclasses import dataclass

from flask import Blueprint, Response, abort, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

import app.mail as mailer
from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.user import UserAccount
from app.utils import audit, get_or_404, require_permission

assignments_bp = Blueprint("assignments", __name__, url_prefix="/assignments")


# ── Side-effect helpers ────────────────────────────────────────────────────────


def auto_close_if_full(
    event: Event,
    *,
    context: str = "",
    allowed_from: tuple[EventStatus, ...] = (EventStatus.ASSIGNMENTS_OPEN,),
) -> None:
    """Transition event to ASSIGNMENTS_CLOSED only when every spot (mandatory + optional) is filled.

    ``context`` is inserted into the audit message so different callers can
    distinguish themselves in the log (e.g. ``"při importu"``) without
    duplicating the trigger condition.

    ``allowed_from`` lets the import path close events that were created in
    ``DRAFT`` (regular claim/release always start from ``ASSIGNMENTS_OPEN``).
    """
    if event.status in allowed_from and event.total_spots > 0 and event.filled_spots >= event.total_spots:
        event.status = EventStatus.ASSIGNMENTS_CLOSED
        event.version += 1
        suffix = f" {context}" if context else ""
        audit(
            "status_change",
            "Event",
            event.id,
            f"Přihlašování automaticky uzavřeno{suffix} — všechny pozice obsazeny",
        )


def _auto_assign_rp(event: Event, user: UserAccount) -> None:
    """If event has no RP and user is RP-eligible, assign them as RP."""
    if event.responsible_person_id is None and user.is_rp_eligible():
        event.responsible_person_id = user.id
        event.version += 1
        audit("edit", "Event", event.id, f"Zodpovědná osoba automaticky nastavena na '{user.name}'")


def _auto_clear_rp(event: Event, user: UserAccount) -> None:
    """If the leaving user is the current RP, reassign to next eligible or clear."""
    if event.responsible_person_id == user.id:
        # Find another RP-eligible person still assigned to this event
        for spot in event.spots:
            if (
                spot.assignment is not None
                and spot.assignment.user_id != user.id
                and spot.assignment.user.is_rp_eligible()
            ):
                event.responsible_person_id = spot.assignment.user.id
                event.version += 1
                new_rp = spot.assignment.user.name
                audit(
                    "edit",
                    "Event",
                    event.id,
                    f"Zodpovědná osoba automaticky přeřazena na '{new_rp}' (předchozí '{user.name}' opustil/a akci)",
                )
                break
        else:
            event.responsible_person_id = None
            event.version += 1
            audit("edit", "Event", event.id, f"Zodpovědná osoba odstraněna — '{user.name}' opustil/a akci")


def _auto_reopen_if_freed(event: Event) -> None:
    """Re-open assignments if they were closed and a spot just freed up."""
    if event.status == EventStatus.ASSIGNMENTS_CLOSED:
        event.status = EventStatus.ASSIGNMENTS_OPEN
        event.version += 1
        audit("status_change", "Event", event.id, "Přihlašování automaticky znovuotevřeno — uvolněna pozice")


# ── Service functions ──────────────────────────────────────────────────────────


@dataclass
class AssignResult:
    """Result of an assign/unassign operation."""

    ok: bool
    error: str = ""
    assignment: Assignment | None = None
    event: Event | None = None
    user: UserAccount | None = None


def do_assign_user(
    spot_id: int,
    user: UserAccount,
    assigned_by: UserAccount,
    *,
    allowed_statuses: tuple[EventStatus, ...] = (
        EventStatus.ASSIGNMENTS_OPEN,
        EventStatus.ASSIGNMENTS_CLOSED,
    ),
    check_eligibility: bool = False,
    block_centrally_coordinated: bool = False,
    duplicate_error: str | None = None,
) -> AssignResult:
    """
    Lock a spot and assign a user. Handles the full transaction.

    This is the single source of truth for assignment creation logic.
    Both the HTML routes and the JSON table-manager route call this.

    Returns AssignResult with ok=True on success, or ok=False with error message.
    """
    # User must be active and not archived
    if not user.is_active or user.is_archived:
        return AssignResult(ok=False, error="Uživatel nenalezen nebo není aktivní.")

    # Pessimistic lock: serialize concurrent claims on the same spot.
    # SQLAlchemy's mssql dialect silently drops .with_for_update(); use an
    # explicit T-SQL table hint. UNIQUE(spot_id) on Assignment is the ultimate
    # backstop, but the lock lets the second txn wait and surface a clean
    # „already taken“ message instead of an IntegrityError.
    spot = db.session.scalar(
        db.select(EventSpot).where(EventSpot.id == spot_id).with_hint(EventSpot, "WITH (UPDLOCK, ROWLOCK)")
    )
    if spot is None:
        return AssignResult(ok=False, error="Pozice nenalezena.")

    event = db.session.get(Event, spot.event_id)
    if event is None:
        return AssignResult(ok=False, error="Akce nenalezena.")

    # Validate event state
    if event.status not in allowed_statuses:
        return AssignResult(ok=False, error="Přiřazení není možné v aktuálním stavu akce.", event=event)

    if event.archived:
        return AssignResult(ok=False, error="Přiřazení není možné — akce je archivována.", event=event)

    if block_centrally_coordinated and event.is_centrally_coordinated:
        return AssignResult(ok=False, error="Tuto akci řídí koordinátor — přihlašování není povoleno.", event=event)

    # Spot must be free
    if spot.assignment is not None:
        return AssignResult(ok=False, error="Tato pozice je již obsazena.", event=event)

    # User must not already be assigned to this event
    existing = db.session.scalar(
        db.select(Assignment)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id == event.id, Assignment.user_id == user.id)
    )
    if existing:
        msg = duplicate_error or f"Uživatel {user.name} je již přihlášen na tuto akci."
        return AssignResult(ok=False, error=msg, event=event)

    # Optional eligibility check (for self-claim)
    if check_eligibility and not spot.is_eligible(user):
        return AssignResult(ok=False, error="Nemáte požadovanou kvalifikaci pro tuto pozici.", event=event)

    # Create assignment
    spot.assignment = Assignment(user_id=user.id, assigned_by_id=assigned_by.id)
    db.session.add(spot.assignment)
    db.session.flush()

    if user.id == assigned_by.id:
        summary = f"Uživatel '{user.name}' se přihlásil na akci '{event.name}'"
    else:
        summary = f"'{assigned_by.name}' přiřadil '{user.name}' na akci '{event.name}'"
    audit("create", "Assignment", spot.assignment.id, summary)
    _auto_assign_rp(event, user)
    auto_close_if_full(event)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return AssignResult(ok=False, error="Tato pozice byla právě obsazena někým jiným.", event=event)

    mailer.send_assignment_confirmed(user, event, spot_description=spot.description)
    return AssignResult(ok=True, assignment=spot.assignment, event=event, user=user)


def do_unassign_user(
    assignment: Assignment,
    *,
    unassigned_by: UserAccount | None = None,
    block_centrally_coordinated: bool = False,
) -> AssignResult:
    """
    Validate and remove an assignment. Handles the full transaction.

    This is the single source of truth for assignment removal logic.
    Both the HTML routes and the JSON table-manager route call this.

    Returns AssignResult with ok=True on success, or ok=False with error message.
    """
    event = db.session.get(Event, assignment.spot.event_id)
    if event is None:
        return AssignResult(ok=False, error="Akce nenalezena.")

    if event.status == EventStatus.COMPLETED or event.archived:
        return AssignResult(ok=False, error="Nelze odhlásit uživatele z dokončené nebo archivované akce.", event=event)

    if block_centrally_coordinated and event.is_centrally_coordinated:
        return AssignResult(ok=False, error="Tuto akci řídí koordinátor — odhlašování není povoleno.", event=event)

    user = assignment.user
    spot_description = assignment.spot.description
    if unassigned_by is not None and unassigned_by.id != user.id:
        summary = f"'{unassigned_by.name}' odhlásil '{user.name}' z akce '{event.name}'"
    else:
        summary = f"Uživatel '{user.name}' se odhlásil z akce '{event.name}'"
    audit("delete", "Assignment", assignment.id, summary)
    _auto_clear_rp(event, user)
    db.session.delete(assignment)
    _auto_reopen_if_freed(event)

    db.session.commit()

    mailer.send_assignment_released(user, event, spot_description=spot_description)
    return AssignResult(ok=True, event=event, user=user)


# ── Claim (own) ───────────────────────────────────────────────────────────────


@assignments_bp.post("/claim/<int:spot_id>")
@login_required
def claim(spot_id: int) -> Response:
    require_permission("event.assign_own")

    result = do_assign_user(
        spot_id,
        current_user,
        current_user,
        allowed_statuses=(EventStatus.ASSIGNMENTS_OPEN,),
        check_eligibility=True,
        block_centrally_coordinated=not current_user.has_permission("event.assign_other"),
        duplicate_error="Již jste přihlášeni na tuto akci.",
    )

    if not result.ok:
        if result.event is None:
            abort(404)
        flash(result.error, "warning")
        return redirect(url_for("events.detail", event_id=result.event.id))

    flash("Úspěšně přihlášeni na akci.", "success")
    return redirect(url_for("events.detail", event_id=result.event.id))


# ── Release (own) ─────────────────────────────────────────────────────────────


@assignments_bp.post("/release/<int:assignment_id>")
@login_required
def release(assignment_id: int) -> Response:
    assignment = get_or_404(Assignment, assignment_id)
    event = get_or_404(Event, assignment.spot.event_id)

    # Only own assignment unless elevated permission on this event
    if assignment.user_id != current_user.id:
        if not event.user_can_manage_assignments(current_user):
            abort(403)

    # Block self-release when ME is centrally coordinated, unless user
    # has assign_other permission (coordinators/admins can always release)
    is_self_release = assignment.user_id == current_user.id
    block_coordinated = is_self_release and not current_user.has_permission("event.assign_other")

    result = do_unassign_user(
        assignment,
        unassigned_by=current_user,
        block_centrally_coordinated=block_coordinated,
    )

    if not result.ok:
        if result.event is None:
            abort(404)
        flash(result.error, "warning")
        return redirect(url_for("events.detail", event_id=result.event.id))

    flash("Odhlášení z akce bylo úspěšné.", "success")
    return redirect(url_for("events.detail", event_id=result.event.id))


# ── Assign other ──────────────────────────────────────────────────────────────


@assignments_bp.post("/assign/<int:spot_id>")
@login_required
def assign_other(spot_id: int) -> Response:
    # Load spot to get event for permission check
    spot = db.session.get(EventSpot, spot_id)
    if spot is None:
        abort(404)
    event = get_or_404(Event, spot.event_id)

    # Permission: either event.assign_other or RP-elevated on this event
    if not event.user_can_manage_assignments(current_user):
        abort(403)

    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        flash("Vyberte uživatele.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))

    user = db.session.get(UserAccount, user_id)
    if user is None:
        flash("Uživatel nenalezen.", "danger")
        return redirect(url_for("events.detail", event_id=event.id))

    result = do_assign_user(
        spot_id,
        user,
        current_user,
    )

    if not result.ok:
        redirect_event = result.event or event
        flash(result.error, "warning")
        return redirect(url_for("events.detail", event_id=redirect_event.id))

    flash(f"Uživatel {user.name} byl přiřazen na akci.", "success")
    return redirect(url_for("events.detail", event_id=result.event.id))


# ── Unassign other ────────────────────────────────────────────────────────────


@assignments_bp.post("/unassign/<int:assignment_id>")
@login_required
def unassign_other(assignment_id: int) -> Response:
    assignment = get_or_404(Assignment, assignment_id)
    event = get_or_404(Event, assignment.spot.event_id)

    # Permission: either event.assign_other or RP-elevated on this event
    if not event.user_can_manage_assignments(current_user):
        abort(403)

    result = do_unassign_user(
        assignment,
        unassigned_by=current_user,
    )

    if not result.ok:
        if result.event is None:
            abort(404)
        flash(result.error, "warning")
        return redirect(url_for("events.detail", event_id=result.event.id))

    flash(f"Uživatel {result.user.name} byl odhlášen z akce.", "success")
    return redirect(url_for("events.detail", event_id=result.event.id))
