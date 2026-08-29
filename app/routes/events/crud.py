"""Event CRUD routes: index, feed, create, create_from_template, detail, edit, delete."""

import io
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Response, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, collate, func
from sqlalchemy.orm import selectinload

import app.mail as mailer
from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import (
    EquipmentItem,
    EquipmentType,
    EventEquipmentPlan,
)
from app.models.event import Event, EventSpot, EventStatus, EventTemplate, EventType
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.user import UserAccount
from app.printout_generator import generate_printout
from app.queries import (
    active_master_events_list,
    active_users_list,
    in_maintenance_during,
    rp_eligible_users_list,
    user_fillable_qual_ids,
)
from app.utils import (
    CS_COLLATION,
    audit,
    bind_form_version,
    check_version_conflict,
    commit_or_stale,
    diff_changes,
    get_app_tz,
    get_or_404,
    order_by_nulls_last,
    require_permission,
)

from . import events_bp
from ._helpers import (
    PER_PAGE,
    STATUS_BADGE_COLORS,
    STATUS_COLORS,
    all_equipment_types,
    apply_equipment_plans,
    build_spots,
    can_view,
    check_equipment_conflicts,
    parse_equipment_plans_from_form,
    parse_event_form,
    validate_event_spots_config,
)

# ── List helpers ──────────────────────────────────────────────────────────────

_ALL_STATUSES = [s.name for s in EventStatus]
_DEFAULT_STATUSES = [
    s.name for s in EventStatus if s not in (EventStatus.DRAFT, EventStatus.CANCELLED, EventStatus.COMPLETED)
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
        if active_me and active_me.archived:
            active_me = None

    if "types" not in request.args:
        active_types = list(_ALL_EVENT_TYPES)
    else:
        raw_types = request.args.get("types", "")
        active_types = [t for t in raw_types.split(",") if t in _ALL_EVENT_TYPES]

    for_me = request.args.get("for_me") == "1" and current_user.has_permission("event.assign_own")

    return {
        "show_archived": show_archived,
        "page": page,
        "active_statuses": active_statuses,
        "sort_col": sort_col,
        "sort_dir": sort_dir,
        "active_me": active_me,
        "active_types": active_types,
        "for_me": for_me,
    }


def _apply_index_order(
    query: db.select,  # type: ignore[name-defined, type-arg]
    sort_col: str,
    sort_dir: str,
) -> db.select:  # type: ignore[name-defined, type-arg]
    """Apply ORDER BY clause to the event list query."""
    _asc = sort_dir == "asc"
    if sort_col == "name":
        return query.order_by(
            collate(Event.name, CS_COLLATION).asc() if _asc else collate(Event.name, CS_COLLATION).desc()
        )
    if sort_col == "status":
        return query.order_by(Event.status.asc() if _asc else Event.status.desc())
    if sort_col == "me_name":
        me_name_expr = (
            db.select(case((MasterEvent.is_general == sa.true(), None), else_=MasterEvent.name))
            .where(MasterEvent.id == Event.master_event_id)
            .correlate(Event)
            .scalar_subquery()
        )
        return query.order_by(*order_by_nulls_last(me_name_expr, descending=not _asc))
    if sort_col == "total":
        spot_count_sq = (
            db.select(func.count(EventSpot.id))
            .where(EventSpot.event_id == Event.id, EventSpot.is_optional == sa.false())
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
        return query.order_by(*order_by_nulls_last(rp_name_sq, descending=not _asc))
    # start (default)
    return query.order_by(Event.start_datetime.asc() if _asc else Event.start_datetime.desc())


def _build_eligible_spot_map(events: list[Event]) -> dict[int, list[tuple[int, str | None, list[str], bool]]]:
    """For each event on the current page, find spots the user can claim."""
    if not current_user.has_permission("event.assign_own"):
        return {}

    user_assigned_spot_ids = set(
        db.session.scalars(db.select(Assignment.spot_id).where(Assignment.user_id == current_user.id)).all()
    )
    fillable_ids = user_fillable_qual_ids(current_user)
    result: dict[int, list[tuple[int, str | None, list[str], bool]]] = {}
    for e in events:
        if e.status != EventStatus.ASSIGNMENTS_OPEN:
            continue
        eligible = [
            (
                s.id,
                s.description,
                [q.name for q in s.required_qualifications if not q.is_deleted],
                s.is_optional,
            )
            for s in e.spots
            if s.assignment is None and s.id not in user_assigned_spot_ids and s.is_eligible_for(fillable_ids)
        ]
        if eligible:
            result[e.id] = eligible
    return result


def _eligible_event_ids_for_user(user: UserAccount) -> list[int]:
    """Return event IDs where the user has at least one unoccupied, claimable spot.

    Mirrors _build_eligible_spot_map: only ASSIGNMENTS_OPEN events, uses
    EventSpot.is_eligible_for as the single source of truth for deleted-qual
    filtering and fillable-qual checks, and excludes spots already held by
    the user.
    """
    fillable_ids = user_fillable_qual_ids(user)
    user_assigned_spot_ids = set(
        db.session.scalars(db.select(Assignment.spot_id).where(Assignment.user_id == user.id)).all()
    )

    events = db.session.scalars(db.select(Event).where(Event.status == EventStatus.ASSIGNMENTS_OPEN)).all()

    eligible_event_ids = {
        e.id
        for e in events
        if any(
            s.assignment is None and s.id not in user_assigned_spot_ids and s.is_eligible_for(fillable_ids)
            for s in e.spots
        )
    }

    return list(eligible_event_ids) if eligible_event_ids else [-1]


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
        query = query.where(Event.archived == sa.false())
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

    if f["for_me"]:
        eligible_ids = _eligible_event_ids_for_user(current_user)
        query = query.where(Event.id.in_(eligible_ids))

    query = _apply_index_order(query, f["sort_col"], f["sort_dir"])
    pagination = db.paginate(query, page=f["page"], per_page=PER_PAGE, error_out=False)
    events = pagination.items

    active_named_mes = db.session.scalars(
        db.select(MasterEvent)
        .where(MasterEvent.archived == sa.false())
        .order_by(collate(MasterEvent.name, CS_COLLATION))
    ).all()

    event_templates: list[EventTemplate] = []
    if current_user.has_permission("event.create"):
        event_templates = list(
            db.session.scalars(db.select(EventTemplate).order_by(collate(EventTemplate.name, CS_COLLATION))).all()
        )

    return render_template(
        "events/index.html",
        events=events,
        pagination=pagination,
        show_archived=f["show_archived"],
        active_statuses=f["active_statuses"],
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
        status_colors=STATUS_BADGE_COLORS,
        for_me=f["for_me"],
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
        query = query.where(Event.archived == sa.false())

    events = db.session.scalars(query).all()

    # Build eligible spot set for current user (same logic as index view)
    user_assigned_spot_ids: set[int] = set()
    fillable_ids: set[int] = set()
    if current_user.has_permission("event.assign_own"):
        assigned = db.session.scalars(db.select(Assignment).where(Assignment.user_id == current_user.id)).all()
        user_assigned_spot_ids = {a.spot_id for a in assigned}
        fillable_ids = user_fillable_qual_ids(current_user)

    items = []
    for e in events:
        color = STATUS_COLORS.get(e.status.value, "#6c757d")
        eligible = False
        if current_user.has_permission("event.assign_own"):
            eligible = any(
                s.assignment is None and s.id not in user_assigned_spot_ids and s.is_eligible_for(fillable_ids)
                for s in e.spots
            )
        items.append(
            {
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
            }
        )
    return jsonify(items)


# ── Create ────────────────────────────────────────────────────────────────────


@events_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("event.create")

    master_events = active_master_events_list()
    users = rp_eligible_users_list()
    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    eq_types = all_equipment_types()

    def _render_create(**extra: object) -> str:
        return render_template(
            "events/form.html",
            mode="create",
            master_events=master_events,
            users=users,
            all_qualifications=all_qualifications,
            all_equipment_types=eq_types,
            EventType=EventType,
            **extra,
        )

    if request.method == "POST":
        event, error = parse_event_form(request.form)
        if error or event is None:
            flash(error or "Chyba formuláře.", "danger")
            return _render_create()

        quick_publish = request.form.get("action") == "quick_publish"
        if quick_publish:
            if not current_user.has_permission("event.publish") or not current_user.has_permission(
                "event.assignments.open"
            ):
                abort(403)

            event.status = EventStatus.ASSIGNMENTS_OPEN
            event.assignments_open_datetime = datetime.now(timezone.utc)

        db.session.add(event)
        db.session.flush()

        # Spots always come from the form — the template pre-fill renders them
        # into the form on GET, so POST always contains the (possibly adjusted) rows.
        build_spots(event, request.form)

        db.session.flush()
        spot_error = validate_event_spots_config(list(event.spots))
        if spot_error:
            db.session.rollback()
            flash(spot_error, "danger")
            return _render_create()

        # Parse and validate equipment plans submitted in the form.
        eq_plans = parse_equipment_plans_from_form(request.form)
        # Acquire UPDLOCK on each planned type row in a consistent order before
        # the availability check to prevent TOCTOU races. HOLDLOCK holds the
        # locks to end-of-transaction. SQLAlchemy's mssql dialect silently
        # drops .with_for_update(), so use an explicit T-SQL table hint.
        if eq_plans:
            db.session.scalars(
                db.select(EquipmentType)
                .where(EquipmentType.id.in_(sorted({t for t, _ in eq_plans})))
                .order_by(EquipmentType.id)
                .with_hint(EquipmentType, "WITH (UPDLOCK, HOLDLOCK, ROWLOCK)")
            ).all()
        eq_errors = check_equipment_conflicts(eq_plans, event.start_datetime, event.end_datetime)
        if eq_errors:
            db.session.rollback()
            for msg in eq_errors:
                flash(msg, "danger")
            return _render_create()

        apply_equipment_plans(event, eq_plans)
        db.session.flush()

        audit("create", "Event", event.id, f"Vytvořena akce '{event.name}'")
        db.session.commit()

        if quick_publish:
            flash("Akce byla vytvořena a přihlášky okamžitě otevřeny.", "success")
        else:
            flash("Akce byla vytvořena.", "success")
        return redirect(url_for("events.detail", event_id=event.id))

    return _render_create()


# ── Create from template ──────────────────────────────────────────────────────


@events_bp.get("/create-from-template/<int:template_id>")
@login_required
def create_from_template(template_id: int) -> str | Response:
    require_permission("event.create")
    tmpl = get_or_404(EventTemplate, template_id)

    master_events = active_master_events_list()
    users = rp_eligible_users_list()
    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    return render_template(
        "events/form.html",
        mode="create",
        master_events=master_events,
        users=users,
        template=tmpl,
        all_qualifications=all_qualifications,
        all_equipment_types=all_equipment_types(),
        # Pre-fill spot rows from the template's spot templates.
        template_spot_prefill=[
            {
                "desc": st.description or "",
                "optional": st.is_optional,
                "qual_ids": [q.id for q in st.required_qualifications],
            }
            for st in tmpl.spot_templates
        ],
        # Pre-fill equipment rows from the template's plans.
        template_eq_plans=[(p.equipment_type_id, p.quantity_required) for p in tmpl.equipment_plans],
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

    # Users already assigned to a spot on this event (for picker filtering)
    assigned_user_ids: set[int] = {spot.assignment.user_id for spot in event.spots if spot.assignment is not None}

    # When ME has a coordinator, self-claim/release is blocked for regular members
    me_coordinated = (
        event.master_event is not None
        and event.master_event.coordinator_id is not None
        and not current_user.has_permission("event.assign_other")
    )

    eq_types_for_detail = db.session.scalars(
        db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))
    ).all()
    # Type-level availability: two queries total (one COUNT, one SUM) regardless
    # of how many equipment types are planned.
    planned_type_ids = [p.equipment_type_id for p in event.equipment_plans]
    equipment_availability: dict[int, int] = {}
    if planned_type_ids:
        # Total available items per type (SHARED; maintenance-window-aware)
        pool_rows = db.session.execute(
            db.select(EquipmentItem.type_id, func.count(EquipmentItem.id).label("cnt"))
            .where(
                EquipmentItem.type_id.in_(planned_type_ids),
                ~in_maintenance_during(event.start_datetime, event.end_datetime),
            )
            .group_by(EquipmentItem.type_id)
        ).all()
        pool: dict[int, int] = {row.type_id: row.cnt for row in pool_rows}

        # Committed quantities from other overlapping non-cancelled/completed events
        committed_rows = db.session.execute(
            db.select(EventEquipmentPlan.equipment_type_id, func.sum(EventEquipmentPlan.quantity_required).label("qty"))
            .join(Event, Event.id == EventEquipmentPlan.event_id)
            .where(
                EventEquipmentPlan.equipment_type_id.in_(planned_type_ids),
                EventEquipmentPlan.event_id != event.id,
                Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
                Event.archived == sa.false(),
                Event.start_datetime < event.end_datetime,
                Event.end_datetime > event.start_datetime,
            )
            .group_by(EventEquipmentPlan.equipment_type_id)
        ).all()
        committed: dict[int, int] = {row.equipment_type_id: int(row.qty) for row in committed_rows}

        for type_id in planned_type_ids:
            equipment_availability[type_id] = pool.get(type_id, 0) - committed.get(type_id, 0)

    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
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
        now=datetime.now(timezone.utc),
        EventStatus=EventStatus,
        EventType=EventType,
        eligible_users=eligible_users,
        assigned_user_ids=assigned_user_ids,
        can_assign=can_assign,
        me_coordinated=me_coordinated,
        all_equipment_types=eq_types_for_detail,
        equipment_availability=equipment_availability,
        all_qualifications=all_qualifications,
        fillers_map=fillers_map,
        rp_eligible_attendees=rp_eligible_attendees,
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
    eq_types = all_equipment_types()
    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    def _render_edit(**extra: object) -> str:
        return render_template(
            "events/form.html",
            mode="edit",
            event=event,
            master_events=master_events,
            users=users,
            all_qualifications=all_qualifications,
            all_equipment_types=eq_types,
            EventType=EventType,
            **extra,
        )

    if request.method == "POST":
        if check_version_conflict(event, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return _render_edit()
        bind_form_version(event, request.form.get("version"))

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
            return _render_edit()

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

        # Validate and apply equipment plans.
        eq_plans = parse_equipment_plans_from_form(request.form)
        # Acquire UPDLOCK on each planned type row in a consistent order before
        # the availability check to prevent TOCTOU races. HOLDLOCK holds the
        # locks to end-of-transaction. SQLAlchemy's mssql dialect silently
        # drops .with_for_update(), so use an explicit T-SQL table hint.
        if eq_plans:
            db.session.scalars(
                db.select(EquipmentType)
                .where(EquipmentType.id.in_(sorted({t for t, _ in eq_plans})))
                .order_by(EquipmentType.id)
                .with_hint(EquipmentType, "WITH (UPDLOCK, HOLDLOCK, ROWLOCK)")
            ).all()
        eq_errors = check_equipment_conflicts(
            eq_plans, event.start_datetime, event.end_datetime, exclude_event_id=event.id
        )
        if eq_errors:
            db.session.rollback()
            db.session.refresh(event)
            for msg in eq_errors:
                flash(msg, "danger")
            return _render_edit()

        apply_equipment_plans(event, eq_plans)

        # Rebuild spots only when the user explicitly changed them in the form.
        if request.form.get("spots_changed") == "1":
            for spot in list(event.spots):
                db.session.delete(spot)
            db.session.flush()
            build_spots(event, request.form)
            db.session.flush()
            spot_error = validate_event_spots_config(list(event.spots))
            if spot_error:
                db.session.rollback()
                db.session.refresh(event)
                flash(spot_error, "danger")
                return _render_edit()

        event.version += 1
        audit("edit", "Event", event.id, f"Upravena akce '{event.name}'", diff_changes(before, after))
        if (resp := commit_or_stale(url_for("events.detail", event_id=event.id))) is not None:
            return resp

        # Notify assigned users about the change (only if something actually changed).
        actual_changes = diff_changes(before, after)
        if actual_changes:
            assigned_users = [spot.assignment.user for spot in event.spots if spot.assignment is not None]
            for u in assigned_users:
                mailer.send_event_changed(u, event, actual_changes)
            db.session.commit()  # commit the enqueued outbox rows

        flash("Akce byla uložena.", "success")
        return redirect(url_for("events.detail", event_id=event.id))

    return _render_edit()


# ── Archive (soft-delete) ─────────────────────────────────────────────────────


@events_bp.post("/<int:event_id>/archive")
@login_required
def archive_event(event_id: int) -> Response:
    is_ajax = request.headers.get("X-CSRFToken") and request.accept_mimetypes.accept_json

    event = get_or_404(Event, event_id)

    if event.status == EventStatus.DRAFT:
        require_permission("event.archive_draft")
    else:
        require_permission("event.archive")

    if event.archived:
        if is_ajax:
            return jsonify({"ok": False, "error": "Akce je již archivována."}), 400
        flash("Akce je již archivována.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    me_id = event.master_event_id
    name = event.name
    event.archived = True
    event.version += 1
    audit("archive", "Event", event.id, f"Akce '{name}' archivována")
    db.session.commit()

    mailer.flush_and_notify_archived(event)
    db.session.commit()

    if is_ajax:
        return jsonify({"ok": True})
    flash(f"Akce „{name}“ byla archivována.", "success")
    if me_id:
        return redirect(url_for("master_events.detail", me_id=me_id))
    return redirect(url_for("events.index"))


# ── Printout (bulk xlsx export) ───────────────────────────────────────────────


@events_bp.post("/printout")
@login_required
def events_printout() -> Response:
    require_permission("report.view")

    event_ids = [int(x) for x in request.form.getlist("event_ids") if x.isdecimal()]
    if not event_ids:
        flash("Nevybrány žádné akce.", "warning")
        return redirect(url_for("events.index"))

    events = (
        db.session.scalars(
            db.select(Event)
            .where(Event.id.in_(event_ids))
            .where(Event.status != EventStatus.DRAFT)
            .where(Event.archived == sa.false())
            .options(
                selectinload(Event.spots).selectinload(EventSpot.required_qualifications),
                selectinload(Event.spots).selectinload(EventSpot.assignment).selectinload(Assignment.user),
            )
            .order_by(Event.start_datetime)
        )
        .unique()
        .all()
    )

    if not events:
        flash(
            "Žádné z vybraných akcí nelze zahrnout do sestavy (koncepty a archivované akce jsou vyloučeny).", "warning"
        )
        return redirect(url_for("events.index"))

    tz = get_app_tz()
    first_date = events[0].start_datetime.astimezone(tz).strftime("%d.%m.%Y")
    last_date = events[-1].start_datetime.astimezone(tz).strftime("%d.%m.%Y")
    date_range = f"{first_date} – {last_date}" if first_date != last_date else first_date

    wb = generate_printout(list(events), date_range, me_name=None)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = 'attachment; filename="sestava_vybrane.xlsx"'
    return response


# ── Unarchive (restore from archive) ─────────────────────────────────────────


@events_bp.post("/<int:event_id>/unarchive")
@login_required
def unarchive_event(event_id: int) -> Response:
    require_permission("event.unarchive")

    event = get_or_404(Event, event_id)

    if not event.archived:
        flash("Akce není archivována.", "warning")
        return redirect(url_for("events.detail", event_id=event_id))

    name = event.name
    event.archived = False
    event.version += 1
    audit("unarchive", "Event", event.id, f"Akce '{name}' obnovena z archivu")
    db.session.commit()

    mailer.notify_unarchived(event)
    db.session.commit()

    flash(f"Akce „{name}“ byla obnovena z archivu.", "success")
    return redirect(url_for("events.detail", event_id=event_id))
