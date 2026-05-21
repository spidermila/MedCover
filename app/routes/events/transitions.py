"""Event lifecycle transition routes: transition, cancel, restore, split."""

from __future__ import annotations

from datetime import datetime

from flask import Response, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models.event import Event, EventStatus
from app.models.user import UserAccount
from app.utils import audit, get_or_404, require_permission
import app.mail as mailer

from . import events_bp
from ._helpers import (
    TRANSITIONS,
    copy_spots_with_assignments,
    copy_equipment,
)


# ── Lifecycle transitions ─────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/transition")
@login_required
def transition(event_id: int) -> Response:
    event = get_or_404(Event, event_id)

    target = request.form.get("target_status")
    try:
        target_status = EventStatus(target)
    except ValueError:
        abort(400)

    allowed = next(
        (t for t in TRANSITIONS if t[0] == event.status and t[1] == target_status),
        None,
    )
    if allowed is None:
        flash("Tento přechod stavu není povolen.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    if not current_user.has_permission(allowed[2]):
        abort(403)

    event.status = target_status
    event.version += 1
    audit("status_change", "Event", event.id, f"Stav akce '{event.name}' změněn na '{target_status.value}'", {
        "before": {"status": event.status.value},
        "after": {"status": target_status.value},
    })
    db.session.commit()

    # Email notifications
    if target_status == EventStatus.PUBLISHED:
        active_users = db.session.scalars(
            db.select(UserAccount)
            .where(UserAccount.is_active.is_(True))
            .where(UserAccount.is_archived.is_(False))
        ).all()
        for u in active_users:
            mailer.send_event_published(u, event)
    elif target_status == EventStatus.ASSIGNMENTS_OPEN:
        active_users = db.session.scalars(
            db.select(UserAccount)
            .where(UserAccount.is_active.is_(True))
            .where(UserAccount.is_archived.is_(False))
        ).all()
        for u in active_users:
            mailer.send_assignments_opened(u, event)
    elif target_status == EventStatus.COMPLETED:
        # Send debriefing invitations to everyone who held a spot on this event.
        for spot in event.spots:
            if spot.assignment is not None and not spot.assignment.debriefing_email_sent:
                mailer.send_debriefing_invitation(spot.assignment, event)
                spot.assignment.debriefing_email_sent = True
        db.session.commit()

    flash(f"Stav akce byl změněn na {target_status.value}.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


@events_bp.post("/<int:event_id>/cancel")
@login_required
def cancel(event_id: int) -> Response:
    require_permission("event.cancel")

    event = get_or_404(Event, event_id)
    if event.status == EventStatus.COMPLETED:
        flash("Dokončené akce nelze zrušit.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    event.status = EventStatus.CANCELLED
    event.archived = True
    event.version += 1
    audit("status_change", "Event", event.id, f"Akce '{event.name}' zrušena a archivována")

    # Notify all assigned users before commit so we still have spot data
    assigned_users = [
        s.assignment.user
        for s in event.spots if s.assignment
    ]
    db.session.commit()

    for user in assigned_users:
        mailer.send_event_cancelled(user, event)

    flash("Akce byla zrušena.", "warning")
    return redirect(url_for("events.index"))


@events_bp.post("/<int:event_id>/restore")
@login_required
def restore(event_id: int) -> Response:
    require_permission("event.restore")

    event = get_or_404(Event, event_id)
    if event.status != EventStatus.CANCELLED:
        flash("Pouze zrušené akce lze obnovit.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    event.status = EventStatus.DRAFT
    event.archived = False
    event.version += 1
    audit("status_change", "Event", event.id, f"Akce '{event.name}' obnovena do stavu Koncept")
    db.session.commit()

    flash("Akce byla obnovena.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


# ── Split event ───────────────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/split")
@login_required
def split_event(event_id: int) -> Response:
    """Split an event into two contiguous parts at a given datetime."""
    require_permission("event.create")

    event = get_or_404(Event, event_id)

    if event.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
        flash("Dokončené nebo zrušené akce nelze rozdělit.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    raw_date = request.form.get("split_date", "").strip()
    raw_time = request.form.get("split_time", "").strip()
    if not raw_date or not raw_time:
        flash("Zadejte datum a čas rozdělení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    try:
        from app.utils import get_app_tz  # noqa: PLC0415
        tz = get_app_tz()
        split_dt = datetime.fromisoformat(f"{raw_date}T{raw_time}").replace(tzinfo=tz)
    except ValueError:
        flash("Neplatný formát data nebo času rozdělení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    if not (event.start_datetime < split_dt < event.end_datetime):
        flash("Čas rozdělení musí být mezi začátkem a koncem akce.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    original_end = event.end_datetime
    original_name = event.name

    # Shorten the source event and rename it "… 1/2"
    event.end_datetime = split_dt
    event.name = f"{original_name} 1/2"
    event.version += 1
    audit("edit", "Event", event.id,
          f"Akce rozdělena — konec zkrácen na {split_dt.isoformat()} (část 1/2)",
          {"end_datetime": {"before": original_end.isoformat(), "after": split_dt.isoformat()},
           "name": {"before": original_name, "after": event.name}})

    # Create the second part
    part2 = Event(
        name=f"{original_name} 2/2",
        master_event_id=event.master_event_id,
        event_type=event.event_type,
        status=EventStatus.ASSIGNMENTS_OPEN,
        start_datetime=split_dt,
        end_datetime=original_end,
        address=event.address,
        contact_person=event.contact_person,
        paid=event.paid,
        description=event.description,
        responsible_person_id=event.responsible_person_id,
        created_by_id=current_user.id,
        reminder_schedule=event.reminder_schedule,
    )
    db.session.add(part2)
    db.session.flush()

    copy_spots_with_assignments(event, part2)
    copy_equipment(event, part2)

    audit("create", "Event", part2.id,
          f"Akce '{part2.name}' vytvořena rozdělením akce '{original_name}' (část 2/2)")

    db.session.commit()

    flash(f"Akce byla rozdělena. Vznikla nová akce '{part2.name}' s otevřenými přihláškami.", "success")
    return redirect(url_for("events.detail", event_id=part2.id))
