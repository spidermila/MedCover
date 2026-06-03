"""Event equipment routes: plan add/remove, assign/unassign, availability check."""

from datetime import datetime, timezone

from flask import Response, flash, jsonify, redirect, request, url_for
from flask_login import login_required

from app.extensions import db
from app.models.equipment import EquipmentItem, EquipmentType, EventEquipmentAssignment, EventEquipmentPlan
from app.models.event import Event, EventStatus
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


# ── Event Equipment: Assignments ──────────────────────────────────────────────


@events_bp.post("/<int:event_id>/equipment/assign")
@login_required
def equipment_assign(event_id: int) -> Response:
    require_permission("event.equipment.assign")

    event = get_or_404(Event, event_id)
    if event.status == EventStatus.CANCELLED:
        flash("Zrušeným akcím nelze přiřazovat vybavení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    item_id = request.form.get("item_id", type=int)
    if not item_id:
        flash("Vyberte položku vybavení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    item = get_or_404(EquipmentItem, item_id)

    if not item.is_available:
        flash(
            f'Položka „{item.name}" je momentálně nedostupná: {item.unavailability_reason or "bez udaného důvodu"}.',
            "danger",
        )
        return redirect(url_for("events.detail", event_id=event_id))

    existing = db.session.scalar(
        db.select(EventEquipmentAssignment).where(
            EventEquipmentAssignment.event_id == event_id,
            EventEquipmentAssignment.equipment_item_id == item_id,
        )
    )
    if existing:
        flash("Tato položka je k akci již přiřazena.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    assignment = EventEquipmentAssignment(event_id=event_id, equipment_item_id=item_id)
    db.session.add(assignment)
    audit("edit", "Event", event.id, f"Přiřazena položka vybavení '{item.name}' k akci '{event.name}'")
    db.session.commit()

    flash(f'Položka „{item.name}" byla přiřazena k akci.', "success")
    return redirect(url_for("events.detail", event_id=event_id))


@events_bp.post("/<int:event_id>/equipment/unassign")
@login_required
def equipment_unassign(event_id: int) -> Response:
    require_permission("event.equipment.assign")

    event = get_or_404(Event, event_id)

    item_id = request.form.get("item_id", type=int)
    if not item_id:
        flash("Chybí položka vybavení.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    assignment = db.session.scalar(
        db.select(EventEquipmentAssignment).where(
            EventEquipmentAssignment.event_id == event_id,
            EventEquipmentAssignment.equipment_item_id == item_id,
        )
    )
    if assignment is None:
        flash("Přiřazení nenalezeno.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    item = db.session.get(EquipmentItem, item_id)
    db.session.delete(assignment)
    audit(
        "edit", "Event", event.id, f"Vrácena položka vybavení '{item.name if item else item_id}' z akce '{event.name}'"
    )
    db.session.commit()

    flash("Položka vybavení byla vrácena.", "success")
    return redirect(url_for("events.detail", event_id=event_id))


# ── Equipment Availability Check (AJAX) ───────────────────────────────────────


@events_bp.post("/equipment-check")
@login_required
def equipment_check() -> Response:
    """Check availability of equipment items for a proposed event time window.

    Request JSON:
        item_ids: list[int]
        start_datetime: ISO string
        end_datetime: ISO string
        exclude_event_id: int | null  (omit self from conflict search on edit)

    Response JSON:
        results: list of {item_id, item_name, status, reason?, conflicting_event?}
        status values: "ok" | "unavailable" | "conflict"
    """
    require_permission("equipment.view")
    data = request.get_json(silent=True) or {}
    item_ids: list[int] = data.get("item_ids", [])
    start_raw: str = data.get("start_datetime", "")
    end_raw: str = data.get("end_datetime", "")
    exclude_event_id: int | None = data.get("exclude_event_id")

    if not item_ids:
        return jsonify({"results": []})

    try:
        start_dt = datetime.fromisoformat(start_raw)
        end_dt = datetime.fromisoformat(end_raw)
        # Ensure timezone-aware for comparison with DB TIMESTAMPTZ columns
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except ValueError, TypeError:
        return jsonify({"error": "Neplatný formát datumu."}), 400

    results = []

    # Batch-load all requested items in one query
    items = db.session.scalars(db.select(EquipmentItem).where(EquipmentItem.id.in_(item_ids))).all()
    items_by_id = {item.id: item for item in items}

    available_ids: list[int] = []
    for item_id in item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            continue
        if not item.is_available:
            results.append(
                {
                    "item_id": item.id,
                    "item_name": item.name,
                    "status": "unavailable",
                    "reason": item.unavailability_reason or "Bez udaného důvodu",
                }
            )
        else:
            available_ids.append(item.id)

    # Single query for all conflicts across all available items
    if available_ids:
        conflict_filter = [
            EventEquipmentAssignment.equipment_item_id.in_(available_ids),
            Event.status != EventStatus.CANCELLED,
            Event.archived.is_(False),
            Event.start_datetime < end_dt,
            Event.end_datetime > start_dt,
        ]
        if exclude_event_id:
            conflict_filter.append(EventEquipmentAssignment.event_id != exclude_event_id)

        conflicts = db.session.scalars(
            db.select(EventEquipmentAssignment)
            .join(Event, EventEquipmentAssignment.event_id == Event.id)
            .where(*conflict_filter)
        ).all()

        conflicting_item_ids: set[int] = set()
        for c in conflicts:
            conflicting_item_ids.add(c.equipment_item_id)
            ce = c.event
            results.append(
                {
                    "item_id": c.equipment_item_id,
                    "item_name": items_by_id[c.equipment_item_id].name,
                    "status": "conflict",
                    "conflicting_event": {
                        "name": ce.name,
                        "start": ce.start_datetime.isoformat(),
                        "end": ce.end_datetime.isoformat(),
                        "url": url_for("events.detail", event_id=ce.id),
                    },
                }
            )

        # Items with no conflicts → ok
        for aid in available_ids:
            if aid not in conflicting_item_ids:
                results.append(
                    {
                        "item_id": aid,
                        "item_name": items_by_id[aid].name,
                        "status": "ok",
                    }
                )

    return jsonify({"results": results})
