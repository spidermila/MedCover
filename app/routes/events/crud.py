"""Event CRUD routes: index, feed, create, create_from_template, detail, edit, delete."""

from __future__ import annotations

from flask import Response, render_template, redirect, url_for, flash, request, abort, jsonify
from flask_login import login_required, current_user

from sqlalchemy import collate, func, case
from app.extensions import db
from app.models.event import Event, EventSpot, EventStatus, EventTemplate, EventType
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from app.models.equipment import EquipmentCategory, EquipmentItem, EquipmentItemStatus, EquipmentType
from app.models.qualification import Qualification
from app.models.assignment import Assignment
from app.constants import RECORD_MODIFIED_MSG
from app.utils import CS_COLLATION, audit, check_version_conflict, diff_changes, get_app_tz, get_or_404, require_permission
from app.queries import active_master_events_list, active_users_list, assignable_equipment_items, rp_eligible_users_list, user_fillable_qual_ids
import app.mail as mailer

from . import events_bp
from ._helpers import (
    PER_PAGE,
    STATUS_COLORS,
    can_view,
    parse_event_form,
    build_spots,
    build_spots_from_template,
    build_equipment_assignments,
    equipment_warnings_for_event,
)


# ── List helpers ──────────────────────────────────────────────────────────────

_ALL_STATUSES = [s.name for s in EventStatus]
_DEFAULT_STATUSES = [
    s.name for s in EventStatus
    if s not in (EventStatus.DRAFT, EventStatus.CANCELLED, EventStatus.COMPLETED)
]
_ALL_EVENT_TYPES = [t.name for t in EventType]
_VALID_SORT_COLS = {"start", "name", "status", "me_name", "total", "rp"}


def _parse_index_filters() -> dict:
    """Extract and validate all filter/sort params from the request query string."""
    show_archived = request.args.get("archived") == "1"
    page = request.args.get("page", 1, type=int)

    if "statuses" not in request.args:
        active_statuses = list(_DEFAULT_STATUSES)
    else:
        raw = request.args.get("statuses", "")
        active_statuses = [s for s in raw.split(",") if s in _ALL_STATUSES]

    sort_col = request.args.get("sort", "start")
    sort_dir = request.args.get("dir", "asc")
    if sort_col not in _VALID_SORT_COLS:
        sort_col = "start"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    me_id_param = request.args.get("me_id", "").strip()
    active_me: MasterEvent | None = None
    if me_id_param:
        active_me = db.session.get(MasterEvent, me_id_param)
        if active_me and (active_me.is_general or active_me.archived):
            active_me = None

    if "types" not in request.args:
        active_types = list(_ALL_EVENT_TYPES)
    else:
        raw_types = request.args.get("types", "")
        active_types = [t for t in raw_types.split(",") if t in _ALL_EVENT_TYPES]

    return {
        "show_archived": show_archived,
        "page": page,
        "active_statuses": active_statuses,
        "sort_col": sort_col,
        "sort_dir": sort_dir,
        "active_me": active_me,
        "active_types": active_types,
    }


def _apply_index_order(query: db.select, sort_col: str, sort_dir: str) -> db.select:  # type: ignore[name-defined, type-arg]
    """Apply ORDER BY clause to the event list query."""
    _asc = sort_dir == "asc"
    if sort_col == "name":
        return query.order_by(collate(Event.name, CS_COLLATION).asc() if _asc else collate(Event.name, CS_COLLATION).desc())
    if sort_col == "status":
        return query.order_by(Event.status.asc() if _asc else Event.status.desc())
    if sort_col == "me_name":
        me_name_expr = (
            db.select(case((MasterEvent.is_general.is_(True), None), else_=MasterEvent.name))
            .where(MasterEvent.id == Event.master_event_id)
            .correlate(Event)
            .scalar_subquery()
        )
        order_expr = me_name_expr.asc() if _asc else me_name_expr.desc()
        return query.order_by(order_expr.nulls_last())
    if sort_col == "total":
        spot_count_sq = (
            db.select(func.count(EventSpot.id))
            .where(EventSpot.event_id == Event.id, EventSpot.is_optional.is_(False))
            .correlate(Event)
            .scalar_subquery()
        )
        return query.order_by(spot_count_sq.asc() if _asc else spot_count_sq.desc())
    if sort_col == "rp":
        rp_name_sq = (
            db.select(UserAccount.name)
            .where(UserAccount.id == Event.responsible_person_id)
            .correlate(Event)
            .scalar_subquery()
        )
        order_expr = rp_name_sq.asc() if _asc else rp_name_sq.desc()
        return query.order_by(order_expr.nulls_last())
    # start (default)
    return query.order_by(Event.start_datetime.asc() if _asc else Event.start_datetime.desc())


def _build_eligible_spot_map(events: list[Event]) -> dict[int, list[tuple[int, str | None]]]:
    """For each event on the current page, find spots the user can claim."""
    if not current_user.has_permission("event.assign_own"):
        return {}

    user_assigned_spot_ids = set(db.session.scalars(
        db.select(Assignment.spot_id).where(Assignment.user_id == current_user.id)
    ).all())
    fillable_ids = user_fillable_qual_ids(current_user)
    result: dict[int, list[tuple[int, str | None]]] = {}
    for e in events:
        if e.status != EventStatus.ASSIGNMENTS_OPEN:
            continue
        eligible = [
            (s.id, s.description)
            for s in e.spots
            if s.assignment is None and s.id not in user_assigned_spot_ids
            and s.is_eligible_for(fillable_ids)
        ]
        if eligible:
            result[e.id] = eligible
    return result


# ── List ──────────────────────────────────────────────────────────────────────

@events_bp.get("/")
@login_required
def index() -> str:
    require_permission("event.view", "event.view_draft")

    f = _parse_index_filters()
    query = db.select(Event)

    if not current_user.has_permission("event.view_draft"):
        query = query.where(Event.status != EventStatus.DRAFT)
    if not f["show_archived"]:
        query = query.where(Event.archived.is_(False))
    if f["active_me"]:
        query = query.where(Event.master_event_id == f["active_me"].id)

    # Apply event type filter
    type_values = [EventType[t] for t in f["active_types"] if t in EventType.__members__]
    if not type_values:
        query = query.where(db.false())
    elif len(type_values) < len(_ALL_EVENT_TYPES):
        query = query.where(Event.event_type.in_(type_values))

    # Apply status filter
    status_values = [EventStatus[s] for s in f["active_statuses"] if s in EventStatus.__members__]
    if status_values:
        query = query.where(Event.status.in_(status_values))
    else:
        query = query.where(db.false())

    query = _apply_index_order(query, f["sort_col"], f["sort_dir"])
    pagination = db.paginate(query, page=f["page"], per_page=PER_PAGE, error_out=False)
    events = pagination.items

    active_named_mes = db.session.scalars(
        db.select(MasterEvent)
        .where(MasterEvent.is_general.is_(False), MasterEvent.archived.is_(False))
        .order_by(collate(MasterEvent.name, CS_COLLATION))
    ).all()

    event_templates: list[EventTemplate] = []
    if current_user.has_permission("event.create"):
        event_templates = list(db.session.scalars(
            db.select(EventTemplate).order_by(collate(EventTemplate.name, CS_COLLATION))
        ).all())

    return render_template(
        "events/index.html",
        events=events,
        pagination=pagination,
        show_archived=f["show_archived"],
        active_statuses=f["active_statuses"],
        default_statuses=_DEFAULT_STATUSES,
        all_statuses=_ALL_STATUSES,
        active_types=f["active_types"],
        all_event_types=_ALL_EVENT_TYPES,
        sort_col=f["sort_col"],
        sort_dir=f["sort_dir"],
        active_me=f["active_me"],
        EventStatus=EventStatus,
        EventType=EventType,
        has_draft_perm=current_user.has_permission("event.view_draft"),
        event_templates=event_templates,
        eligible_spot_map=_build_eligible_spot_map(events),
        active_named_mes=active_named_mes,
    )


# ── Calendar JSON feed ────────────────────────────────────────────────────────

@events_bp.get("/feed")
@login_required
def feed() -> Response:
    """Return events as FullCalendar-compatible JSON."""
    require_permission("event.view", "event.view_draft")

    show_archived = request.args.get("archived") == "1"

    query = db.select(Event)
    if not current_user.has_permission("event.view_draft"):
        query = query.where(Event.status != EventStatus.DRAFT)
    if not show_archived:
        query = query.where(Event.archived.is_(False))

    events = db.session.scalars(query).all()

    # Build eligible spot set for current user (same logic as index view)
    user_assigned_spot_ids: set[int] = set()
    fillable_ids: set[int] = set()
    if current_user.has_permission("event.assign_own"):
        assigned = db.session.scalars(
            db.select(Assignment).where(
                Assignment.user_id == current_user.id
            )
        ).all()
        user_assigned_spot_ids = {a.spot_id for a in assigned}
        fillable_ids = user_fillable_qual_ids(current_user)

    items = []
    for e in events:
        color = STATUS_COLORS.get(e.status.value, "#6c757d")
        eligible = False
        if current_user.has_permission("event.assign_own"):
            eligible = any(
                s.assignment is None and s.id not in user_assigned_spot_ids
                and s.is_eligible_for(fillable_ids)
                for s in e.spots
            )
        items.append({
            "id": e.id,
            "title": e.name,
            "start": e.start_datetime.isoformat(),
            "end": e.end_datetime.isoformat(),
            "url": url_for("events.detail", event_id=e.id),
            "backgroundColor": color,
            "borderColor": color,
            "textColor": "#000" if e.status.value == "Přihlášky uzavřeny" else "#fff",
            "extendedProps": {
                "status": e.status.value,
                "status_key": e.status.name,
                "filled": e.mandatory_filled_spots,
                "total": e.mandatory_total_spots,
                "rp": e.responsible_person.name if e.responsible_person else None,
                "start_local": e.start_datetime.astimezone(get_app_tz()).strftime("%d.%m.%Y %H:%M"),
                "end_local": e.end_datetime.astimezone(get_app_tz()).strftime("%d.%m.%Y %H:%M"),
                "me_name": None if e.master_event.is_general else e.master_event.name,
                "eligible": eligible,
            },
        })
    return jsonify(items)


# ── Create ────────────────────────────────────────────────────────────────────

@events_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("event.create")

    master_events = active_master_events_list()
    users = rp_eligible_users_list()
    all_qualifications = db.session.scalars(db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))).all()
    equipment_groups = assignable_equipment_items() if current_user.has_permission("event.equipment.assign") else []

    if request.method == "POST":
        event, error = parse_event_form(request.form)
        if error or event is None:
            flash(error or "Chyba formuláře.", "danger")
            return render_template(
                "events/create.html",
                master_events=master_events,
                users=users,
                all_qualifications=all_qualifications,
                equipment_groups=equipment_groups,
                EventType=EventType,
            )

        quick_publish = request.form.get("action") == "quick_publish"
        if quick_publish:
            if not current_user.has_permission("event.publish") or \
               not current_user.has_permission("event.assignments.open"):
                abort(403)
            from datetime import datetime, timezone
            event.status = EventStatus.ASSIGNMENTS_OPEN
            event.assignments_open_datetime = datetime.now(timezone.utc)

        db.session.add(event)
        db.session.flush()

        template_id_str = request.form.get("template_id", "").strip()
        if template_id_str:

            tmpl = db.session.get(EventTemplate, int(template_id_str))
            if tmpl:
                build_spots_from_template(event, tmpl)
            else:
                build_spots(event, request.form)
        else:
            build_spots(event, request.form)

        if current_user.has_permission("event.equipment.assign"):
            selected_ids = request.form.getlist("equipment_item_ids", type=int)
            build_equipment_assignments(event, selected_ids)

        audit("create", "Event", event.id, f"Vytvořena akce '{event.name}'")
        db.session.commit()

        if quick_publish:
            flash("Akce byla vytvořena a přihlášky okamžitě otevřeny.", "success")
        else:
            flash("Akce byla vytvořena.", "success")
        return redirect(url_for("events.detail", event_id=event.id))

    return render_template(
        "events/create.html",
        master_events=master_events,
        users=users,
        all_qualifications=all_qualifications,
        equipment_groups=equipment_groups,
        EventType=EventType,
    )


# ── Create from template ──────────────────────────────────────────────────────

@events_bp.get("/create-from-template/<int:template_id>")
@login_required
def create_from_template(template_id: int) -> str | Response:
    require_permission("event.create")
    tmpl = get_or_404(EventTemplate, template_id)

    master_events = active_master_events_list()
    users = rp_eligible_users_list()
    all_qualifications = db.session.scalars(db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))).all()
    equipment_groups = assignable_equipment_items() if current_user.has_permission("event.equipment.assign") else []

    return render_template(
        "events/create.html",
        master_events=master_events,
        users=users,
        template=tmpl,
        all_qualifications=all_qualifications,
        equipment_groups=equipment_groups,
        EventType=EventType,
    )


# ── Detail ────────────────────────────────────────────────────────────────────

@events_bp.get("/<int:event_id>")
@login_required
def detail(event_id: int) -> str | Response:
    event = get_or_404(Event, event_id)
    if not can_view(event):
        abort(403)

    eligible_users: list[UserAccount] = []
    can_assign = event.user_can_manage_assignments(current_user)
    if can_assign:
        eligible_users = list(active_users_list())

    # When ME has a coordinator, self-claim/release is blocked for regular members
    me_coordinated = (
        event.master_event is not None
        and event.master_event.coordinator_id is not None
        and not current_user.has_permission("event.assign_other")
    )

    all_equipment_types = db.session.scalars(
        db.select(EquipmentType)
        .where(EquipmentType.category != EquipmentCategory.PERSONAL)
        .order_by(collate(EquipmentType.name, CS_COLLATION))
    ).all()
    assigned_item_ids = {ea.equipment_item_id for ea in event.equipment_assignments}
    if assigned_item_ids:
        available_equipment_items = db.session.scalars(
            db.select(EquipmentItem).where(
                EquipmentItem.id.notin_(assigned_item_ids),
                EquipmentItem.equipment_type.has(EquipmentType.category != EquipmentCategory.PERSONAL),
            ).order_by(collate(EquipmentItem.name, CS_COLLATION))
        ).all()
    else:
        available_equipment_items = db.session.scalars(
            db.select(EquipmentItem).where(
                EquipmentItem.equipment_type.has(EquipmentType.category != EquipmentCategory.PERSONAL),
            ).order_by(collate(EquipmentItem.name, CS_COLLATION))
        ).all()

    all_qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    # Precompute for JS eligibility check: for each qualification R, which qualification IDs can fill it?
    # fillers_map[R.id] = {R.id} ∪ fillers of each of R's parents (transitively)
    def _fillers(qual: Qualification, _visited: frozenset[int] = frozenset()) -> set[int]:
        if qual.id in _visited:
            return set()
        _visited = _visited | {qual.id}
        result = {qual.id}
        for parent in qual.parents:
            result |= _fillers(parent, _visited)
        return result

    fillers_map = {str(c.id): list(_fillers(c)) for c in all_qualifications}

    # Users currently assigned to this event who are RP-eligible (for set_rp dropdown)
    rp_eligible_attendees: list[UserAccount] = []
    if current_user.has_any_permission("event.set_responsible_person"):
        for spot in event.spots:
            if spot.assignment and spot.assignment.user.is_rp_eligible():
                rp_eligible_attendees.append(spot.assignment.user)

    return render_template(
        "events/detail.html",
        event=event,
        EventStatus=EventStatus,
        EventType=EventType,
        EquipmentItemStatus=EquipmentItemStatus,
        eligible_users=eligible_users,
        can_assign=can_assign,
        me_coordinated=me_coordinated,
        all_equipment_types=all_equipment_types,
        available_equipment_items=available_equipment_items,
        all_qualifications=all_qualifications,
        fillers_map=fillers_map,
        rp_eligible_attendees=rp_eligible_attendees,
        equipment_warnings=equipment_warnings_for_event(event),
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@events_bp.route("/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def edit(event_id: int) -> str | Response:
    require_permission("event.edit")

    event = get_or_404(Event, event_id)

    if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
        flash("Dokončené nebo zrušené akce nelze upravovat.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    master_events = active_master_events_list()
    users = rp_eligible_users_list()

    if request.method == "POST":
        if check_version_conflict(event, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template("events/edit.html", event=event, master_events=master_events, users=users, EventType=EventType)

        # Snapshot before mutation
        before = {
            "name": event.name,
            "master_event_id": event.master_event_id,
            "event_type": event.event_type.name,
            "start_datetime": str(event.start_datetime),
            "end_datetime": str(event.end_datetime),
            "address": event.address,
            "contact_person": event.contact_person,
            "description": event.description,
            "paid": event.paid,
            "responsible_person_id": str(event.responsible_person_id),
            "assignments_open_datetime": str(event.assignments_open_datetime),
            "planned_participants_count": event.planned_participants_count,
        }

        updated, error = parse_event_form(request.form, existing=event)
        if error:
            flash(error, "danger")
            return render_template("events/edit.html", event=event, master_events=master_events, users=users, EventType=EventType)

        after = {
            "name": event.name,
            "master_event_id": event.master_event_id,
            "event_type": event.event_type.name,
            "start_datetime": str(event.start_datetime),
            "end_datetime": str(event.end_datetime),
            "address": event.address,
            "contact_person": event.contact_person,
            "description": event.description,
            "paid": event.paid,
            "responsible_person_id": str(event.responsible_person_id),
            "assignments_open_datetime": str(event.assignments_open_datetime),
            "planned_participants_count": event.planned_participants_count,
        }

        event.version += 1
        audit("edit", "Event", event.id, f"Upravena akce '{event.name}'", diff_changes(before, after))
        db.session.commit()

        # Notify assigned users about the change (only if something actually changed).
        actual_changes = diff_changes(before, after)
        if actual_changes:
            from app.utils import external_url_for
            event_url = external_url_for("events.detail", event_id=event.id)
            assigned_users = [
                spot.assignment.user
                for spot in event.spots
                if spot.assignment is not None
            ]
            for u in assigned_users:
                mailer.send_event_changed(u, event, actual_changes, event_url=event_url)
            db.session.commit()  # commit the enqueued outbox rows

        flash("Akce byla uložena.", "success")
        return redirect(url_for("events.detail", event_id=event.id))

    return render_template("events/edit.html", event=event, master_events=master_events, users=users, EventType=EventType)


# ── Delete ────────────────────────────────────────────────────────────────────

@events_bp.post("/<int:event_id>/delete")
@login_required
def delete_event(event_id: int) -> Response:
    require_permission("event.delete_draft")

    is_ajax = request.headers.get("X-CSRFToken") and request.accept_mimetypes.accept_json

    event = get_or_404(Event, event_id)
    if event.status != EventStatus.DRAFT:
        if is_ajax:
            return jsonify({"ok": False, "error": "Smazat lze pouze akce ve stavu Koncept."}), 400
        flash("Smazat lze pouze akce ve stavu Koncept.", "danger")
        return redirect(url_for("events.detail", event_id=event_id))

    me_id = event.master_event_id
    name = event.name
    audit("delete", "Event", event.id, f"Akce '{name}' smazána (byla ve stavu Koncept)")
    db.session.delete(event)
    db.session.commit()

    if is_ajax:
        return jsonify({"ok": True})
    flash(f'Akce \u201e{name}\u201c byla smazána.', "success")
    if me_id:
        return redirect(url_for("master_events.detail", me_id=me_id))
    return redirect(url_for("events.index"))
