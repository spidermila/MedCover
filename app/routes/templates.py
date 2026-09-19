"""
Event Template CRUD blueprint.

Permissions:
  event_template.view    — list templates
  event_template.create  — create templates
  event_template.edit    — edit templates
  event_template.delete  — delete templates
"""

import sqlalchemy as sa
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import collate

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.equipment import EquipmentType, EventTemplateEquipmentPlan
from app.models.event import EventTemplate, EventType
from app.models.qualification import Qualification
from app.routes.events._helpers import apply_condition_plan
from app.staffing import condition_plan_from_form
from app.utils import CS_COLLATION, audit, check_version_conflict, diff_changes, get_or_404, require_permission

templates_bp = Blueprint("templates", __name__, url_prefix="/templates")


def _rebuild_equipment_plans(template: EventTemplate, form: dict) -> None:
    """Delete existing equipment plans and recreate from form data."""
    for ep in list(template.equipment_plans):
        db.session.delete(ep)
    db.session.flush()
    for key, val in form.items():
        if key.startswith("equip_qty_"):
            try:
                type_id = int(key.split("equip_qty_")[1])
                qty = int(val)
            except ValueError, IndexError:
                continue
            if qty > 0:
                ep = EventTemplateEquipmentPlan(
                    template_id=template.id,
                    equipment_type_id=type_id,
                    quantity_required=qty,
                )
                db.session.add(ep)


# ── List ──────────────────────────────────────────────────────────────────────


@templates_bp.get("/")
@login_required
def index() -> str:
    require_permission("event_template.view")

    all_templates = db.session.scalars(
        db.select(EventTemplate).order_by(collate(EventTemplate.name, CS_COLLATION))
    ).all()
    return render_template("templates/index.html", templates=all_templates)


# ── Create ────────────────────────────────────────────────────────────────────


@templates_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("event_template.create")

    qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    equipment_types = db.session.scalars(
        db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))
    ).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        paid = request.form.get("paid") == "1"
        event_type_str = request.form.get("event_type", "").strip()
        event_type = EventType[event_type_str] if event_type_str in EventType.__members__ else EventType.MEDICAL_COVER

        if not name:
            flash("Název šablony je povinný.", "danger")
            return render_template(
                "templates/form.html",
                template=None,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        if db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == name)):
            flash("Šablona s tímto názvem již existuje.", "danger")
            return render_template(
                "templates/form.html",
                template=None,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        try:
            plan = condition_plan_from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "templates/form.html",
                template=None,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        tmpl = EventTemplate(
            name=name,
            description=description,
            paid=paid,
            event_type=event_type,
        )
        apply_condition_plan(tmpl, plan)
        db.session.add(tmpl)
        db.session.flush()
        _rebuild_equipment_plans(tmpl, request.form)

        audit("create", "EventTemplate", tmpl.id, f"Vytvořena šablona akce '{tmpl.name}'")
        db.session.commit()

        flash(f"Šablona „{tmpl.name}“ byla vytvořena.", "success")
        return redirect(url_for("templates.index"))

    return render_template(
        "templates/form.html",
        template=None,
        qualifications=qualifications,
        equipment_types=equipment_types,
        EventType=EventType,
    )


# ── Edit ──────────────────────────────────────────────────────────────────────


@templates_bp.route("/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
def edit(template_id: int) -> str | Response:
    require_permission("event_template.edit")

    tmpl = get_or_404(EventTemplate, template_id)

    qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    equipment_types = db.session.scalars(
        db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))
    ).all()

    if request.method == "POST":
        if check_version_conflict(tmpl, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template(
                "templates/form.html",
                template=tmpl,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        paid = request.form.get("paid") == "1"
        event_type_str = request.form.get("event_type", "").strip()
        event_type = EventType[event_type_str] if event_type_str in EventType.__members__ else EventType.MEDICAL_COVER

        if not name:
            flash("Název šablony je povinný.", "danger")
            return render_template(
                "templates/form.html",
                template=tmpl,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        conflict = db.session.scalar(
            db.select(EventTemplate).where(EventTemplate.name == name, EventTemplate.id != template_id)
        )
        if conflict:
            flash("Šablona s tímto názvem již existuje.", "danger")
            return render_template(
                "templates/form.html",
                template=tmpl,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        before = {
            "name": tmpl.name,
            "description": tmpl.description,
            "paid": tmpl.paid,
            "event_type": tmpl.event_type.name,
            "requirements": [(r.qualification_id, r.minimum_count) for r in tmpl.qualification_requirements],
        }

        try:
            plan = condition_plan_from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "templates/form.html",
                template=tmpl,
                qualifications=qualifications,
                equipment_types=equipment_types,
                EventType=EventType,
            )

        tmpl.name = name
        tmpl.description = description
        tmpl.paid = paid
        tmpl.event_type = event_type
        tmpl.version += 1

        apply_condition_plan(tmpl, plan)
        _rebuild_equipment_plans(tmpl, request.form)

        after = {
            "name": tmpl.name,
            "description": tmpl.description,
            "paid": tmpl.paid,
            "event_type": tmpl.event_type.name,
            "requirements": plan[2],
        }

        audit(
            "edit",
            "EventTemplate",
            tmpl.id,
            f"Upravena šablona akce '{tmpl.name}'",
            diff_changes(before, after),
        )
        db.session.commit()

        flash(f"Šablona „{tmpl.name}“ byla uložena.", "success")
        return redirect(url_for("templates.index"))

    return render_template(
        "templates/form.html",
        template=tmpl,
        qualifications=qualifications,
        equipment_types=equipment_types,
        EventType=EventType,
    )


# ── Delete ────────────────────────────────────────────────────────────────────


@templates_bp.post("/<int:template_id>/delete")
@login_required
def delete(template_id: int) -> Response:
    require_permission("event_template.delete")

    tmpl = get_or_404(EventTemplate, template_id)

    name = tmpl.name
    audit("delete", "EventTemplate", tmpl.id, f"Smazána šablona akce '{name}'")
    db.session.delete(tmpl)
    db.session.commit()

    flash(f"Šablona „{name}“ byla smazána.", "success")
    return redirect(url_for("templates.index"))
