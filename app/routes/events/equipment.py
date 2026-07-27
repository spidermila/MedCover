"""Event equipment routes: plan add/remove and type-level availability check."""

import sqlalchemy as sa
from flask import Response, flash, redirect, request, url_for
from flask_login import login_required
from markupsafe import Markup

from app.extensions import db
from app.models.equipment import EquipmentType, EventEquipmentPlan
from app.models.event import Event, EventStatus
from app.queries import available_quantity_for_type
from app.utils import audit, get_or_404, require_permission

from . import events_bp

# ── Event Equipment: Plan ─────────────────────────────────────────────────────


@events_bp.post("/<int:event_id>/equipment/plan")
@login_required
def equipment_plan_add(event_id: int) -> Response:
    require_permission("event.equipment.plan")

    event = get_or_404(Event, event_id)
    if event.status == EventStatus.CANCELLED:
        flash("Zrušeným akcím nelze plánovat vybavení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    type_id = request.form.get("type_id", type=int)
    quantity = request.form.get("quantity", 1, type=int)
    if not type_id or quantity < 1:
        flash("Zadejte platný typ a množství.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    et = get_or_404(EquipmentType, type_id)

    # exclude_event_id already excludes this event's own committed quantity from
    # the calculation, so `available` is exactly what remains after other events.
    available = available_quantity_for_type(
        type_id,
        event.start_datetime,
        event.end_datetime,
        exclude_event_id=event_id,
    )

    if quantity > available:
        msg = Markup("Nedostatek vybavení: typ „{name}” má k dispozici {avail} ks (požadováno {qty}).").format(
            name=et.name, avail=available, qty=quantity
        )
        conflicting = db.session.scalars(
            db.select(Event)
            .join(EventEquipmentPlan, EventEquipmentPlan.event_id == Event.id)
            .where(
                EventEquipmentPlan.equipment_type_id == type_id,
                Event.id != event_id,
                Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
                Event.archived == sa.false(),
                Event.start_datetime < event.end_datetime,
                Event.end_datetime > event.start_datetime,
            )
            .order_by(Event.start_datetime)
            .limit(3)
        ).all()
        if conflicting:
            links = Markup(", ").join(
                Markup('<a href="{}">{}</a>').format(url_for("events.detail", event_id=c.id), c.name)
                for c in conflicting
            )
            msg = msg + Markup(" Konflikt s: ") + links + Markup(".")
        flash(msg, "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    existing = db.session.get(EventEquipmentPlan, (event_id, type_id))
    if existing:
        existing.quantity_required = quantity
    else:
        db.session.add(
            EventEquipmentPlan(
                event_id=event_id,
                equipment_type_id=type_id,
                quantity_required=quantity,
            )
        )

    audit("edit", "Event", event.id, f"Plán vybavení akce '{event.name}': {et.name} × {quantity}")
    db.session.commit()

    flash("Plán vybavení byl aktualizován.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


@events_bp.post("/<int:event_id>/equipment/plan/remove")
@login_required
def equipment_plan_remove(event_id: int) -> Response:
    require_permission("event.equipment.plan")

    event = get_or_404(Event, event_id)

    type_id = request.form.get("type_id", type=int)
    if not type_id:
        flash("Chybí typ vybavení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
    if plan:
        db.session.delete(plan)
        audit("edit", "Event", event.id, f"Odstraněn typ vybavení z plánu akce '{event.name}'")
        db.session.commit()

    flash("Plán vybavení byl aktualizován.", "success")
    return redirect(url_for("events.detail", event_id=event_id))
