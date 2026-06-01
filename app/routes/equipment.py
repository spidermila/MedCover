"""
Equipment Inventory blueprint.

Permissions:
  equipment.view              — list types and items (all roles)
  equipment_type.create/edit/delete — admin only
  equipment_item.create/edit/delete — admin only
  equipment_item.issue_personal     — admin only
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import collate

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.equipment import EquipmentCategory, EquipmentItem, EquipmentItemStatus, EquipmentType
from app.models.user import UserAccount
from app.queries import active_users_list
from app.utils import CS_COLLATION, audit, check_version_conflict, diff_changes, get_or_404, require_permission

equipment_bp = Blueprint("equipment", __name__, url_prefix="/equipment")


# ── Types: List / Index ───────────────────────────────────────────────────────


@equipment_bp.get("/")
@login_required
def index() -> str:
    require_permission("equipment.view")

    types = db.session.scalars(
        db.select(EquipmentType).order_by(EquipmentType.category, collate(EquipmentType.name, CS_COLLATION))
    ).all()
    return render_template("equipment/index.html", types=types)


# ── Types: Create ─────────────────────────────────────────────────────────────


@equipment_bp.route("/types/create", methods=["GET", "POST"])
@login_required
def type_create() -> str | Response:
    require_permission("equipment_type.create")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        category_val = request.form.get("category", "")

        if not name:
            flash("Název typu vybavení je povinný.", "danger")
            return render_template("equipment/type_form.html", categories=EquipmentCategory, edit=False)

        try:
            category = EquipmentCategory(category_val)
        except ValueError:
            flash("Neplatná kategorie.", "danger")
            return render_template("equipment/type_form.html", categories=EquipmentCategory, edit=False)

        if db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == name)):
            flash("Typ vybavení s tímto názvem již existuje.", "danger")
            return render_template("equipment/type_form.html", categories=EquipmentCategory, edit=False)

        et = EquipmentType(name=name, description=description, category=category)
        db.session.add(et)
        db.session.flush()
        audit("create", "EquipmentType", str(et.id), f"Vytvořen typ vybavení '{et.name}'")
        db.session.commit()

        flash(f'Typ vybavení „{et.name}" byl vytvořen.', "success")
        return redirect(url_for("equipment.index"))

    return render_template("equipment/type_form.html", categories=EquipmentCategory, edit=False)


# ── Types: Edit ───────────────────────────────────────────────────────────────


@equipment_bp.route("/types/<int:type_id>/edit", methods=["GET", "POST"])
@login_required
def type_edit(type_id: int) -> str | Response:
    require_permission("equipment_type.edit")

    et = get_or_404(EquipmentType, type_id)

    if request.method == "POST":
        if check_version_conflict(et, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template("equipment/type_form.html", et=et, categories=EquipmentCategory, edit=True)

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        category_val = request.form.get("category", "")

        if not name:
            flash("Název typu vybavení je povinný.", "danger")
            return render_template("equipment/type_form.html", et=et, categories=EquipmentCategory, edit=True)

        try:
            category = EquipmentCategory(category_val)
        except ValueError:
            flash("Neplatná kategorie.", "danger")
            return render_template("equipment/type_form.html", et=et, categories=EquipmentCategory, edit=True)

        conflict = db.session.scalar(
            db.select(EquipmentType).where(EquipmentType.name == name, EquipmentType.id != type_id)
        )
        if conflict:
            flash("Typ vybavení s tímto názvem již existuje.", "danger")
            return render_template("equipment/type_form.html", et=et, categories=EquipmentCategory, edit=True)

        before = {"name": et.name, "description": et.description, "category": et.category.value}
        et.name = name
        et.description = description
        et.category = category
        et.version += 1

        audit(
            "edit",
            "EquipmentType",
            str(et.id),
            f"Upraven typ vybavení '{et.name}'",
            diff_changes(before, {"name": et.name, "description": et.description, "category": et.category.value}),
        )
        db.session.commit()

        flash(f'Typ vybavení „{et.name}" byl uložen.', "success")
        return redirect(url_for("equipment.index"))

    return render_template("equipment/type_form.html", et=et, categories=EquipmentCategory, edit=True)


# ── Types: Delete ─────────────────────────────────────────────────────────────


@equipment_bp.post("/types/<int:type_id>/delete")
@login_required
def type_delete(type_id: int) -> Response:
    require_permission("equipment_type.delete")

    et = get_or_404(EquipmentType, type_id)

    if et.items:
        flash("Nelze smazat typ vybavení, který má přiřazené položky.", "danger")
        return redirect(url_for("equipment.index"))

    audit("delete", "EquipmentType", str(et.id), f"Smazán typ vybavení '{et.name}'")
    db.session.delete(et)
    db.session.commit()

    flash(f'Typ vybavení „{et.name}" byl smazán.', "success")
    return redirect(url_for("equipment.index"))


# ── Items: List ───────────────────────────────────────────────────────────────


@equipment_bp.get("/items/")
@login_required
def items() -> str:
    require_permission("equipment.view")

    type_filter = request.args.get("type_id", type=int)
    issued_filter = request.args.get("issued")  # "yes" | "no" | None

    query = db.select(EquipmentItem).order_by(collate(EquipmentItem.name, CS_COLLATION))
    if type_filter:
        query = query.where(EquipmentItem.type_id == type_filter)
    if issued_filter == "yes":
        query = query.where(EquipmentItem.issued_to_id.isnot(None))
    elif issued_filter == "no":
        query = query.where(EquipmentItem.issued_to_id.is_(None))

    equipment_items = db.session.scalars(query).all()
    types = db.session.scalars(db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))).all()

    active_users: list[UserAccount] = []
    if current_user.has_permission("equipment_item.issue_personal"):
        active_users = list(active_users_list())

    return render_template(
        "equipment/items.html",
        equipment_items=equipment_items,
        types=types,
        type_filter=type_filter,
        issued_filter=issued_filter,
        active_users=active_users,
    )


# ── Items: Create ─────────────────────────────────────────────────────────────


@equipment_bp.route("/items/create", methods=["GET", "POST"])
@login_required
def item_create() -> str | Response:
    require_permission("equipment_item.create")

    types = db.session.scalars(db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        type_id = request.form.get("type_id", type=int)
        serial_number = request.form.get("serial_number", "").strip() or None
        home_location = request.form.get("home_location", "").strip() or None
        notes = request.form.get("notes", "").strip() or None

        if not name:
            flash("Název položky vybavení je povinný.", "danger")
            return render_template("equipment/item_form.html", types=types, edit=False)
        if not type_id:
            flash("Typ vybavení je povinný.", "danger")
            return render_template("equipment/item_form.html", types=types, edit=False)

        et = db.session.get(EquipmentType, type_id)
        if et is None:
            flash("Neplatný typ vybavení.", "danger")
            return render_template("equipment/item_form.html", types=types, edit=False)

        item = EquipmentItem(
            name=name,
            type_id=type_id,
            serial_number=serial_number,
            home_location=home_location,
            notes=notes,
        )
        db.session.add(item)
        db.session.flush()
        audit("create", "EquipmentItem", str(item.id), f"Vytvořena položka vybavení '{item.name}'")
        db.session.commit()

        flash(f'Položka vybavení „{item.name}" byla vytvořena.', "success")
        return redirect(url_for("equipment.items"))

    return render_template(
        "equipment/item_form.html",
        types=types,
        edit=False,
        can_modify_availability=False,
        EquipmentItemStatus=EquipmentItemStatus,
    )


# ── Items: Edit ───────────────────────────────────────────────────────────────


@equipment_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def item_edit(item_id: int) -> str | Response:
    require_permission("equipment_item.edit")

    item = get_or_404(EquipmentItem, item_id)
    can_modify_availability = current_user.has_permission("equipment_item.availability_modify")

    types = db.session.scalars(db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))).all()

    if request.method == "POST":
        if check_version_conflict(item, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template(
                "equipment/item_form.html",
                item=item,
                types=types,
                edit=True,
                can_modify_availability=can_modify_availability,
                EquipmentItemStatus=EquipmentItemStatus,
            )

        name = request.form.get("name", "").strip()
        type_id = request.form.get("type_id", type=int)
        serial_number = request.form.get("serial_number", "").strip() or None
        home_location = request.form.get("home_location", "").strip() or None
        notes = request.form.get("notes", "").strip() or None

        if not name:
            flash("Název položky vybavení je povinný.", "danger")
            return render_template(
                "equipment/item_form.html",
                item=item,
                types=types,
                edit=True,
                can_modify_availability=can_modify_availability,
                EquipmentItemStatus=EquipmentItemStatus,
            )

        before = {
            "name": item.name,
            "type_id": item.type_id,
            "serial_number": item.serial_number,
            "home_location": item.home_location,
            "notes": item.notes,
            "status": item.status.name if item.status else None,
        }

        item.name = name
        item.type_id = type_id
        item.serial_number = serial_number
        item.home_location = home_location
        item.notes = notes

        if can_modify_availability:
            new_status_raw = request.form.get("status", "AVAILABLE")
            new_status = (
                EquipmentItemStatus[new_status_raw]
                if new_status_raw in EquipmentItemStatus.__members__
                else EquipmentItemStatus.AVAILABLE
            )
            unavailability_reason = request.form.get("unavailability_reason", "").strip() or None
            unavailability_since_raw = request.form.get("unavailability_since", "").strip()
            if new_status == EquipmentItemStatus.UNAVAILABLE and unavailability_since_raw:
                try:
                    unavailability_since: datetime | None = datetime.fromisoformat(unavailability_since_raw).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    unavailability_since = None
            elif new_status == EquipmentItemStatus.UNAVAILABLE:
                unavailability_since = item.unavailability_since or datetime.now(timezone.utc)
            else:
                unavailability_since = None
                unavailability_reason = None

            item.status = new_status
            item.unavailability_reason = unavailability_reason
            item.unavailability_since = unavailability_since

        item.version += 1

        after = {
            "name": item.name,
            "type_id": item.type_id,
            "serial_number": item.serial_number,
            "home_location": item.home_location,
            "notes": item.notes,
            "status": item.status.name if item.status else None,
        }
        audit(
            "edit",
            "EquipmentItem",
            str(item.id),
            f"Upravena položka vybavení '{item.name}'",
            diff_changes(before, after),
        )
        db.session.commit()

        flash(f'Položka vybavení „{item.name}" byla uložena.', "success")
        return redirect(url_for("equipment.items"))

    return render_template(
        "equipment/item_form.html",
        item=item,
        types=types,
        edit=True,
        can_modify_availability=can_modify_availability,
        EquipmentItemStatus=EquipmentItemStatus,
    )


# ── Items: Delete ─────────────────────────────────────────────────────────────


@equipment_bp.post("/items/<int:item_id>/delete")
@login_required
def item_delete(item_id: int) -> Response:
    require_permission("equipment_item.delete")

    item = get_or_404(EquipmentItem, item_id)

    if item.issued_to_id is not None:
        flash("Nelze smazat položku, která je aktuálně vydána.", "danger")
        return redirect(url_for("equipment.items"))

    if item.event_assignments:
        active = [a for a in item.event_assignments if a.returned_at is None]
        if active:
            flash("Nelze smazat položku, která je přiřazena k akci.", "danger")
            return redirect(url_for("equipment.items"))

    audit("delete", "EquipmentItem", str(item.id), f"Smazána položka vybavení '{item.name}'")
    db.session.delete(item)
    db.session.commit()

    flash(f'Položka vybavení „{item.name}" byla smazána.', "success")
    return redirect(url_for("equipment.items"))


# ── Items: Issue / Return ─────────────────────────────────────────────────────


@equipment_bp.post("/items/<int:item_id>/issue")
@login_required
def item_issue(item_id: int) -> Response:
    require_permission("equipment_item.issue_personal")

    item = get_or_404(EquipmentItem, item_id)

    if item.issued_to_id is not None:
        flash("Položka je již vydána.", "danger")
        return redirect(url_for("equipment.items"))

    user_id = request.form.get("user_id")
    if not user_id:
        flash("Uživatel je povinný.", "danger")
        return redirect(url_for("equipment.items"))

    user = db.session.get(UserAccount, user_id)
    if user is None:
        flash("Uživatel nebyl nalezen.", "danger")
        return redirect(url_for("equipment.items"))

    item.issued_to_id = user.id
    item.issued_at = datetime.now(timezone.utc)
    item.version += 1
    audit(
        "edit",
        "EquipmentItem",
        str(item.id),
        f"Vydána osobní položka '{item.name}' uživateli '{user.name}'",
        {"issued_to": [None, str(user.id)]},
    )
    db.session.commit()

    flash(f'Položka „{item.name}" byla vydána uživateli {user.name}.', "success")
    return redirect(url_for("equipment.items"))


@equipment_bp.post("/items/<int:item_id>/return")
@login_required
def item_return(item_id: int) -> Response:
    require_permission("equipment_item.issue_personal")

    item = get_or_404(EquipmentItem, item_id)

    if item.issued_to_id is None:
        flash("Položka není vydána.", "danger")
        return redirect(url_for("equipment.items"))

    old_user_id = str(item.issued_to_id)
    item.issued_to_id = None
    item.issued_at = None
    item.version += 1
    audit(
        "edit",
        "EquipmentItem",
        str(item.id),
        f"Vrácena osobní položka '{item.name}'",
        {"issued_to": [old_user_id, None]},
    )
    db.session.commit()

    flash(f'Položka „{item.name}" byla vrácena.', "success")
    return redirect(url_for("equipment.items"))


@equipment_bp.post("/items/<int:item_id>/take")
@login_required
def item_take(item_id: int) -> Response:
    """Issue item to the currently logged-in user in one click."""
    require_permission("equipment_item.issue_personal")

    item = get_or_404(EquipmentItem, item_id)

    if item.issued_to_id is not None:
        flash("Položka je již vydána.", "danger")
        return redirect(url_for("equipment.items"))

    item.issued_to_id = current_user.id
    item.issued_at = datetime.now(timezone.utc)
    item.version += 1
    audit(
        "edit",
        "EquipmentItem",
        str(item.id),
        f"Vydána osobní položka '{item.name}' uživateli '{current_user.name}' (vzít s sebou)",
        {"issued_to": [None, str(current_user.id)]},
    )
    db.session.commit()

    flash(f'Položka „{item.name}" byla vydána vám.', "success")
    return redirect(url_for("equipment.items"))
