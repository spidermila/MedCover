"""
Equipment Inventory blueprint.

Permissions:
  equipment.view              — list types and items (all roles)
  equipment_type.create/edit/delete — admin only
  equipment_item.create/edit/delete — admin only
  equipment_item.issue_personal     — admin only
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from markupsafe import Markup
from sqlalchemy import collate

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.equipment import (
    EquipmentItem,
    EquipmentType,
    EventEquipmentPlan,
)
from app.models.event import Event, EventStatus
from app.models.user import UserAccount
from app.queries import active_users_list, available_quantity_for_type
from app.utils import (
    CS_COLLATION,
    audit,
    check_version_conflict,
    diff_changes,
    get_app_tz,
    get_or_404,
    require_permission,
)

equipment_bp = Blueprint("equipment", __name__, url_prefix="/equipment")


# ── Types: List / Index ───────────────────────────────────────────────────────


@equipment_bp.get("/")
@login_required
def index() -> str:
    require_permission("equipment.view")

    types = db.session.scalars(db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))).all()
    return render_template("equipment/index.html", types=types)


# ── Types: Create ─────────────────────────────────────────────────────────────


@equipment_bp.route("/types/create", methods=["GET", "POST"])
@login_required
def type_create() -> str | Response:
    require_permission("equipment_type.create")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None

        if not name:
            flash("Název typu vybavení je povinný.", "danger")
            return render_template("equipment/type_form.html", edit=False)

        if db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == name)):
            flash("Typ vybavení s tímto názvem již existuje.", "danger")
            return render_template("equipment/type_form.html", edit=False)

        et = EquipmentType(name=name, description=description)
        db.session.add(et)
        db.session.flush()
        audit("create", "EquipmentType", str(et.id), f"Vytvořen typ vybavení '{et.name}'")
        db.session.commit()

        flash(f'Typ vybavení „{et.name}" byl vytvořen.', "success")
        return redirect(url_for("equipment.index"))

    return render_template("equipment/type_form.html", edit=False)


# ── Types: Edit ───────────────────────────────────────────────────────────────


@equipment_bp.route("/types/<int:type_id>/edit", methods=["GET", "POST"])
@login_required
def type_edit(type_id: int) -> str | Response:
    require_permission("equipment_type.edit")

    et = get_or_404(EquipmentType, type_id)

    if request.method == "POST":
        if check_version_conflict(et, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template("equipment/type_form.html", et=et, edit=True)

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None

        if not name:
            flash("Název typu vybavení je povinný.", "danger")
            return render_template("equipment/type_form.html", et=et, edit=True)

        conflict = db.session.scalar(
            db.select(EquipmentType).where(EquipmentType.name == name, EquipmentType.id != type_id)
        )
        if conflict:
            flash("Typ vybavení s tímto názvem již existuje.", "danger")
            return render_template("equipment/type_form.html", et=et, edit=True)

        before = {"name": et.name, "description": et.description}
        et.name = name
        et.description = description
        et.version += 1

        audit(
            "edit",
            "EquipmentType",
            str(et.id),
            f"Upraven typ vybavení '{et.name}'",
            diff_changes(before, {"name": et.name, "description": et.description}),
        )
        db.session.commit()

        flash(f'Typ vybavení „{et.name}" byl uložen.', "success")
        return redirect(url_for("equipment.index"))

    return render_template("equipment/type_form.html", et=et, edit=True)


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

    return render_template("equipment/item_form.html", types=types, edit=False)


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
            )

        before = {
            "name": item.name,
            "type_id": item.type_id,
            "serial_number": item.serial_number,
            "home_location": item.home_location,
            "notes": item.notes,
            "unavailability_since": str(item.unavailability_since),
        }

        item.name = name
        item.type_id = type_id
        item.serial_number = serial_number
        item.home_location = home_location
        item.notes = notes

        if can_modify_availability:

            def _parse_dt(raw: str) -> datetime | None:
                raw = raw.strip()
                if not raw:
                    return None
                try:
                    return datetime.fromisoformat(raw).replace(tzinfo=get_app_tz()).astimezone(timezone.utc)
                except ValueError:
                    return None

            was_available = item.is_available
            item.unavailability_reason = request.form.get("unavailability_reason", "").strip() or None
            item.unavailability_since = _parse_dt(request.form.get("unavailability_since", ""))
            item.unavailability_until = _parse_dt(request.form.get("unavailability_until", ""))

        item.version += 1
        after = {
            "name": item.name,
            "type_id": item.type_id,
            "serial_number": item.serial_number,
            "home_location": item.home_location,
            "notes": item.notes,
            "unavailability_since": str(item.unavailability_since),
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

        # Warn about shortages if the item just entered a maintenance window.
        if can_modify_availability and was_available and not item.is_available:
            _flash_unavailability_shortage_warning(item)

        return redirect(url_for("equipment.items"))

    return render_template(
        "equipment/item_form.html", item=item, types=types, edit=True, can_modify_availability=can_modify_availability
    )


# ── Shared availability helpers ───────────────────────────────────────────────


def _flash_unavailability_shortage_warning(item: EquipmentItem) -> None:
    """Flash a warning if the item's maintenance window leaves future events short.

    Only events that overlap with [unavailability_since, unavailability_until]
    (or all future events when until is NULL) are checked.
    """
    now = datetime.now(timezone.utc)
    query = (
        db.select(Event)
        .join(EventEquipmentPlan, EventEquipmentPlan.event_id == Event.id)
        .where(
            EventEquipmentPlan.equipment_type_id == item.type_id,
            Event.end_datetime > now,
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
        )
    )
    # Narrow to events that overlap the maintenance window.
    if item.unavailability_since is not None:
        query = query.where(Event.end_datetime > item.unavailability_since)
    if item.unavailability_until is not None:
        query = query.where(Event.start_datetime < item.unavailability_until)
    future_events = db.session.scalars(query.distinct()).all()
    short = [
        e for e in future_events if available_quantity_for_type(item.type_id, e.start_datetime, e.end_datetime) < 0
    ]
    if short:
        event_links = Markup(", ").join(
            Markup('<a href="{}">{}</a>').format(url_for("events.detail", event_id=e.id), e.name) for e in short[:3]
        )
        suffix = Markup(" a další…") if len(short) > 3 else Markup("")
        flash(
            Markup("Upozornění: označení jako nedostupné způsobí nedostatek vybavení pro {} akci/akce/akcí: ").format(
                len(short),
            )
            + event_links
            + suffix
            + Markup("."),
            "warning",
        )


# ── Items: Mark unavailable / available ──────────────────────────────────────


@equipment_bp.post("/items/<int:item_id>/mark-unavailable")
@login_required
def item_mark_unavailable(item_id: int) -> Response:
    require_permission("equipment_item.availability_modify")

    item = get_or_404(EquipmentItem, item_id)

    if check_version_conflict(item, request.form.get("version")):
        flash("Položka byla mezitím změněna jiným uživatelem.", "danger")
        return redirect(url_for("equipment.items"))

    if not item.is_available:
        flash("Položka je již označena jako nedostupná.", "warning")
        return redirect(url_for("equipment.items"))

    reason = request.form.get("reason", "").strip() or None

    def _parse_date(raw: str) -> datetime | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=get_app_tz()).astimezone(timezone.utc)
        except ValueError:
            return None

    since = _parse_date(request.form.get("since", "")) or datetime.now(timezone.utc)
    until = _parse_date(request.form.get("until", ""))

    item.unavailability_reason = reason
    item.unavailability_since = since
    item.unavailability_until = until
    item.version += 1
    until_str = until.strftime("%d.%m.%Y") if until else "neurčito"
    audit(
        "edit",
        "EquipmentItem",
        str(item.id),
        f"Položka '{item.name}' označena jako nedostupná od {since.strftime('%d.%m.%Y')} do {until_str}: {reason or '—'}",  # noqa: E501
    )
    db.session.commit()

    flash(f'Položka „{item.name}" byla označena jako nedostupná.', "success")
    _flash_unavailability_shortage_warning(item)
    return redirect(url_for("equipment.items"))


@equipment_bp.post("/items/<int:item_id>/mark-available")
@login_required
def item_mark_available(item_id: int) -> Response:
    require_permission("equipment_item.availability_modify")

    item = get_or_404(EquipmentItem, item_id)

    if check_version_conflict(item, request.form.get("version")):
        flash("Položka byla mezitím změněna jiným uživatelem.", "danger")
        return redirect(url_for("equipment.items"))

    if item.is_available:
        flash("Položka je již dostupná.", "warning")
        return redirect(url_for("equipment.items"))

    item.unavailability_reason = None
    item.unavailability_since = None
    item.unavailability_until = None
    item.version += 1
    audit("edit", "EquipmentItem", str(item.id), f"Položka '{item.name}' vrácena na sklad (dostupná)")
    db.session.commit()

    flash(f'Položka „{item.name}" je opět dostupná.', "success")
    return redirect(url_for("equipment.items"))


# ── Items: Delete ─────────────────────────────────────────────────────────────


@equipment_bp.post("/items/<int:item_id>/delete")
@login_required
def item_delete(item_id: int) -> Response:
    require_permission("equipment_item.delete")

    item = get_or_404(EquipmentItem, item_id)

    if item.issued_to_id is not None:
        flash("Nelze smazat položku, která je aktuálně vydána.", "danger")
        return redirect(url_for("equipment.items"))

    # Deletion permanently reduces the pool.  Check ALL future events where
    # this item would currently be in the pool (i.e. not in maintenance).
    # We skip events fully covered by an ongoing maintenance window because
    # the item is already absent from that window's pool — deletion has no effect.
    now = datetime.now(timezone.utc)
    pool_query = (
        db.select(Event)
        .join(EventEquipmentPlan, EventEquipmentPlan.event_id == Event.id)
        .where(
            EventEquipmentPlan.equipment_type_id == item.type_id,
            Event.end_datetime > now,
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
        )
    )
    # Only events for which the item is currently in the pool (not in maintenance).
    if not item.is_available:
        if item.unavailability_until is None:
            # Indefinite maintenance: item never in pool for any future event.
            pool_query = pool_query.where(sa.false())
        else:
            # Finite: item re-enters pool after maintenance ends.
            pool_query = pool_query.where(Event.start_datetime >= item.unavailability_until)
    future_events = db.session.scalars(pool_query.distinct()).all()
    short = (
        [e for e in future_events if available_quantity_for_type(item.type_id, e.start_datetime, e.end_datetime) < 1]
        if future_events
        else []
    )
    if short:
        event_links = Markup(", ").join(
            Markup('<a href="{}">{}</a>').format(url_for("events.detail", event_id=e.id), e.name) for e in short[:3]
        )
        suffix = Markup(" a další…") if len(short) > 3 else Markup("")
        flash(
            Markup("Nelze smazat: smazání by způsobilo nedostatek vybavení pro {} akci/akcí: ").format(len(short))
            + event_links
            + suffix
            + Markup(". Označte položku jako nedostupnou místo smazání."),
            "danger",
        )
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
