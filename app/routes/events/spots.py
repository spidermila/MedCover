"""Event spot management routes: add, edit, delete spots; set RP; bulk actions."""

from __future__ import annotations

from flask import Response, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models.event import Event, EventSpot, EventStatus
from app.models.user import UserAccount
from app.models.qualification import Qualification
from app.models.assignment import Assignment
from app.utils import audit, get_or_404, require_permission
import app.mail as mailer

from . import events_bp
from ._helpers import BULK_ACTIONS


# ── Bulk lifecycle actions ────────────────────────────────────────────────────

@events_bp.post("/bulk")
@login_required
def bulk_action() -> Response:
    action = request.form.get("action", "")
    if action not in BULK_ACTIONS:
        abort(400)

    target_status, perm, valid_from = BULK_ACTIONS[action]

    if not current_user.has_permission(perm):
        abort(403)

    raw_ids = request.form.getlist("event_ids")
    try:
        event_ids = [int(x) for x in raw_ids if x.isdigit()]
    except ValueError:
        abort(400)

    if not event_ids:
        flash("Žádné akce nebyly vybrány.", "warning")
        return redirect(url_for("events.index"))

    changed = 0
    skipped = 0

    for eid in event_ids:
        event = db.session.get(Event, eid)
        if event is None or event.status not in valid_from:
            skipped += 1
            continue
        prev_status = event.status.value
        event.status = target_status
        if target_status == EventStatus.CANCELLED:
            event.archived = True
        event.version += 1
        audit("status_change", "Event", event.id,
              f"Hromadná akce: stav akce '{event.name}' změněn na '{target_status.value}'",
              {"before": {"status": prev_status}, "after": {"status": target_status.value}})
        changed += 1

    db.session.commit()

    msg = f"Změněno {changed} akcí."
    if skipped:
        msg += f" Přeskočeno {skipped} (nevhodný stav nebo nenalezeno)."
    flash(msg, "success" if changed else "warning")
    return redirect(url_for("events.index"))


# ── Add spot ──────────────────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/spots/add")
@login_required
def add_spot(event_id: int) -> Response:
    require_permission("event.edit")
    event = get_or_404(Event, event_id)

    description = request.form.get("description", "").strip() or None
    is_optional = request.form.get("is_optional") == "1"
    try:
        quantity = max(1, min(10, int(request.form.get("quantity", 1))))
    except (ValueError, TypeError):
        quantity = 1
    qual_ids = [int(c) for c in request.form.getlist("qualification_ids") if c.isdigit()]
    qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.id.in_(qual_ids), Qualification.is_deleted.is_(False))
    ).all() if qual_ids else []

    for _ in range(quantity):
        spot = EventSpot(event_id=event_id, description=description, is_optional=is_optional)
        spot.required_qualifications = list(qualifications)
        db.session.add(spot)

    event.version += 1
    opt_flag = " (volitelná)" if is_optional else ""
    qual_names = ", ".join(c.name for c in qualifications) if qualifications else "žádná"
    audit("edit", "Event", event.id, f"Přidáno {quantity}× pozice '{description or '—'}'{opt_flag} (kvalifikace: {qual_names})")
    db.session.commit()

    flash(f"{'Pozice přidány' if quantity > 1 else 'Místo přidáno'}.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


# ── Edit spot ─────────────────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/spots/<int:spot_id>/edit")
@login_required
def edit_spot(event_id: int, spot_id: int) -> Response:
    require_permission("event.edit")
    spot = db.session.get(EventSpot, spot_id)
    if spot is None or spot.event_id != event_id:
        abort(404)
    event = get_or_404(Event, event_id)

    description = request.form.get("description", "").strip() or None
    qual_ids = [int(c) for c in request.form.getlist("qualification_ids") if c.isdigit()]
    qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.id.in_(qual_ids), Qualification.is_deleted.is_(False))
    ).all() if qual_ids else []
    confirm_unassign = request.form.get("confirm_unassign") == "1"

    # Check if the assigned user would become ineligible under the new credentials
    unassign_needed = False
    if spot.assignment:
        assigned_user = spot.assignment.user
        # Temporarily simulate new creds to check eligibility
        old_creds = spot.required_qualifications
        spot.required_qualifications = list(qualifications)
        if not spot.is_eligible(assigned_user):  # type: ignore[arg-type]
            if not confirm_unassign:
                spot.required_qualifications = old_creds
                flash(
                    "Pozice nezměněna! Změna kvalifikací pro tuto pozici vyžaduje zaškrtnutí "
                    "potvrzovacího políčka ve formuláři úpravy pozice, protože je na pozici "
                    f"přihlášen uživatel {assigned_user.name}, který nesplňuje nové požadavky.",
                    "warning",
                )
                return redirect(url_for("events.detail", event_id=event_id) + f"#edit-spot-{spot_id}")
            unassign_needed = True
        else:
            spot.required_qualifications = old_creds  # reset — will be set properly below

    spot.description = description
    spot.is_optional = request.form.get("is_optional") == "1"
    spot.required_qualifications = list(qualifications)
    event.version += 1
    opt_flag = " (volitelná)" if spot.is_optional else ""
    qual_names = ", ".join(c.name for c in qualifications) if qualifications else "žádná"
    audit("edit", "Event", event.id, f"Upravena pozice '{description or '—'}'{opt_flag} (kvalifikace: {qual_names})")

    if unassign_needed:
        assignment = spot.assignment
        unassigned_user = assignment.user
        audit("delete", "Assignment", assignment.id, f"Uživatel '{unassigned_user.name}' automaticky odhlášen — nesplňuje nové požadavky pozice")
        db.session.delete(assignment)
        db.session.flush()

    db.session.commit()

    if unassign_needed:
        mailer.send_assignment_released(unassigned_user, event)
        flash(f"Pozice upravena. Uživatel {unassigned_user.name} byl automaticky odhlášen.", "warning")
    else:
        flash("Pozice upravena.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


# ── Delete spot ───────────────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/spots/<int:spot_id>/delete")
@login_required
def delete_spot(event_id: int, spot_id: int) -> Response:
    require_permission("event.edit")
    spot = db.session.get(EventSpot, spot_id)
    if spot is None or spot.event_id != event_id:
        abort(404)
    if spot.assignment is not None:
        flash("Obsazenou pozici nelze smazat.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    db.session.delete(spot)
    event = get_or_404(Event, event_id)
    event.version += 1
    audit("edit", "Event", event.id, f"Odstraněna pozice z akce '{event.name}'")
    db.session.commit()

    flash("Místo odstraněno.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


# ── Set Responsible Person ────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/set_rp")
@login_required
def set_rp(event_id: int) -> Response:
    """Manually assign a responsible person from RP-eligible attendees."""
    require_permission("event.set_responsible_person")

    event = get_or_404(Event, event_id)

    user_id_str = request.form.get("user_id", "").strip()
    if not user_id_str:
        flash("Vyberte zodpovědnou osobu.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    import uuid as _uuid
    try:
        user_id = _uuid.UUID(user_id_str)
    except ValueError:
        abort(400)

    user = db.session.get(UserAccount, user_id)
    if user is None or not user.is_active or user.is_archived:
        flash("Uživatel nenalezen nebo není aktivní.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    if not user.is_rp_eligible():
        flash("Tento uživatel nemá potřebnou kvalifikaci pro roli zodpovědné osoby.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    # User must currently occupy a spot on this event
    assigned = db.session.scalar(
        db.select(Assignment)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id == event_id, Assignment.user_id == user_id)
    )
    if assigned is None:
        flash("Vybraný uživatel nemá obsazenou pozici na této akci.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    old_rp = event.responsible_person_id
    event.responsible_person_id = user_id
    event.version += 1
    audit("edit", "Event", event_id, f"Zodpovědná osoba nastavena na '{user.name}'", {"responsible_person_id": {"before": str(old_rp), "after": str(user_id)}})
    db.session.commit()

    flash(f"{user.name} — zodpovědná osoba nastavena.", "success")
    return redirect(url_for("events.detail", event_id=event_id))
