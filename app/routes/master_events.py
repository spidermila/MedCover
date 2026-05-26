"""
Master Event CRUD blueprint.

Permissions:
  master_event.view    — list + detail
  master_event.create  — create
  master_event.edit    — edit
  master_event.archive / master_event.unarchive — archive toggle

Table Manager (/<me_id>/table):
  Viewing requires event.view
  Spot assignment/unassignment requires event.assign_other
  Event time editing requires event.edit
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING


from flask import Blueprint, Response, jsonify, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.master_event import MasterEvent
from app.constants import RECORD_MODIFIED_MSG
from sqlalchemy import collate
from app.utils import CS_COLLATION, czech_sort_key, audit, check_version_conflict, diff_changes, get_app_tz, get_or_404, require_permission
from app.queries import active_users_list

if TYPE_CHECKING:
    from app.models.event import Event

master_events_bp = Blueprint("master_events", __name__, url_prefix="/master-events")


# ── List ─────────────────────────────────────────────────────────────────────

@master_events_bp.get("/")
@login_required
def index() -> str:
    require_permission("master_event.view")

    show_archived = request.args.get("archived") == "1"
    query = db.select(MasterEvent)
    if not show_archived:
        query = query.where(MasterEvent.archived.is_(False))
    query = query.order_by(MasterEvent.is_general.desc(), collate(MasterEvent.name, CS_COLLATION))
    master_events = db.session.scalars(query).all()

    return render_template(
        "master_events/index.html",
        master_events=master_events,
        show_archived=show_archived,
    )


# ── Create ────────────────────────────────────────────────────────────────────

@master_events_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("master_event.create")

    coordinators = active_users_list()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        coordinator_id = request.form.get("coordinator_id") or None

        if not name:
            flash("Název nadřazené akce je povinný.", "danger")
            return render_template("master_events/create.html", coordinators=coordinators)

        if db.session.scalar(db.select(MasterEvent).where(MasterEvent.name == name)):
            flash("Nadřazená akce s tímto názvem již existuje.", "danger")
            return render_template("master_events/create.html", coordinators=coordinators)

        me = MasterEvent(
            name=name,
            description=description,
            coordinator_id=coordinator_id,
        )
        db.session.add(me)
        db.session.flush()
        audit("create", "MasterEvent", me.id, f"Vytvořena nadřazená akce '{me.name}'")
        db.session.commit()

        flash(f'Nadřazená akce „{me.name}" byla vytvořena.',  'success')
        return redirect(url_for("master_events.detail", me_id=me.id))

    return render_template("master_events/create.html", coordinators=coordinators)


# ── Detail ────────────────────────────────────────────────────────────────────

@master_events_bp.get("/<int:me_id>")
@login_required
def detail(me_id: int) -> str:
    require_permission("master_event.view")

    me = get_or_404(MasterEvent, me_id)

    from app.models.event import Event, EventStatus
    events = db.session.scalars(
        db.select(Event).where(Event.master_event_id == me_id).order_by(Event.start_datetime.desc())
    ).all()

    stats = {
        "total": len(events),
        "draft": sum(1 for e in events if e.status == EventStatus.DRAFT),
        "published": sum(1 for e in events if e.status == EventStatus.PUBLISHED),
        "assignments_open": sum(1 for e in events if e.status == EventStatus.ASSIGNMENTS_OPEN),
        "assignments_closed": sum(1 for e in events if e.status == EventStatus.ASSIGNMENTS_CLOSED),
        "completed": sum(1 for e in events if e.status == EventStatus.COMPLETED),
        "cancelled": sum(1 for e in events if e.status == EventStatus.CANCELLED),
    }

    return render_template("master_events/detail.html", me=me, events=events, stats=stats)


# ── Edit ──────────────────────────────────────────────────────────────────────

@master_events_bp.route("/<int:me_id>/edit", methods=["GET", "POST"])
@login_required
def edit(me_id: int) -> str | Response:
    require_permission("master_event.edit")

    me = get_or_404(MasterEvent, me_id)

    coordinators = active_users_list()

    if request.method == "POST":
        if check_version_conflict(me, request.form.get("version")):
            flash(RECORD_MODIFIED_MSG, "danger")
            return render_template("master_events/edit.html", me=me, coordinators=coordinators)

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        coordinator_id = request.form.get("coordinator_id") or None

        if not name:
            flash("Název nadřazené akce je povinný.", "danger")
            return render_template("master_events/edit.html", me=me, coordinators=coordinators)

        conflict = db.session.scalar(
            db.select(MasterEvent).where(MasterEvent.name == name, MasterEvent.id != me_id)
        )
        if conflict:
            flash("Nadřazená akce s tímto názvem již existuje.", "danger")
            return render_template("master_events/edit.html", me=me, coordinators=coordinators)

        before = {"name": me.name, "description": me.description, "coordinator_id": str(me.coordinator_id)}
        me.name = name
        me.description = description
        me.coordinator_id = coordinator_id
        me.version += 1

        audit("edit", "MasterEvent", me.id, f"Upraven záznam nadřazené akce '{me.name}'", diff_changes(
            before,
            {"name": me.name, "description": me.description, "coordinator_id": str(me.coordinator_id)},
        ))

        db.session.commit()

        flash(f'Nadřazená akce „{me.name}" byla uložena.',  'success')
        return redirect(url_for("master_events.detail", me_id=me.id))

    return render_template("master_events/edit.html", me=me, coordinators=coordinators)


# ── Archive / Unarchive ───────────────────────────────────────────────────────

@master_events_bp.post("/<int:me_id>/archive")
@login_required
def archive(me_id: int) -> Response:
    require_permission("master_event.archive")

    me = get_or_404(MasterEvent, me_id)
    if me.is_general:
        flash("Výchozí nadřazenou akci nelze archivovat.", "danger")
        return redirect(url_for("master_events.detail", me_id=me_id))

    me.archived = True
    me.version += 1
    audit("archive", "MasterEvent", me.id, f"Nadřazená akce '{me.name}' byla archivována")
    db.session.commit()

    flash(f'Nadřazená akce „{me.name}" byla archivována.',  'success')
    return redirect(url_for("master_events.index"))


@master_events_bp.post("/<int:me_id>/unarchive")
@login_required
def unarchive(me_id: int) -> Response:
    require_permission("master_event.unarchive")

    me = get_or_404(MasterEvent, me_id)

    me.archived = False
    me.version += 1
    audit("unarchive", "MasterEvent", me.id, f"Nadřazená akce '{me.name}' byla obnovena z archivu")
    db.session.commit()

    flash(f'Nadřazená akce „{me.name}" byla obnovena z archivu.',  'success')
    return redirect(url_for("master_events.detail", me_id=me_id))


# ── Table Manager ─────────────────────────────────────────────────────────────

_ROW_COLORS = [
    "#d1ece8",
    "#cce5ff",
    "#ffd6e0",
    "#fff3cd",
    "#e2d9f3",
    "#d9f2d9",
    "#ffe5b4",
    "#c8e6fa",
]

_TM_COLOR_RE = re.compile(r'\[color:(#[0-9A-Fa-f]{6})\]', re.IGNORECASE)
_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def _parse_tm_color(description: str | None) -> str | None:
    """Return the [color:#XXXXXX] hex value embedded in an event description, or None."""
    if not description:
        return None
    m = _TM_COLOR_RE.search(description)
    return m.group(1).upper() if m else None


def _set_tm_color(description: str | None, color: str | None) -> str | None:
    """Insert/replace/remove the [color:] tag in an event description."""
    base = _TM_COLOR_RE.sub("", description or "").strip()
    if color:
        tag = f"[color:{color.upper()}]"
        return f"{base} {tag}".strip() if base else tag
    return base or None


def _build_table_rows(events: list) -> tuple[list[dict], int]:
    """Build sorted (event × qualification) rows and compute max spot column count."""
    from app.models.event import EventSpot  # noqa: F401 (type reference only)

    rows: list[dict] = []
    for event in events:
        spots_by_qual: dict[frozenset, list] = defaultdict(list)
        for spot in event.spots:
            qual_ids = frozenset(q.id for q in spot.required_qualifications if not q.is_deleted)
            spots_by_qual[qual_ids].append(spot)

        for qual_ids, spots in spots_by_qual.items():
            qual_objs = sorted(
                [q for q in spots[0].required_qualifications if not q.is_deleted],
                key=lambda q: czech_sort_key(q.name),
            )
            qual_name = ", ".join(q.name for q in qual_objs) if qual_objs else "—"
            rows.append({
                "event": event,
                "qual_ids": qual_ids,
                "qual_objs": qual_objs,
                "qual_name": qual_name,
                "spots": spots,
                "color": "",
                "event_color": _parse_tm_color(event.description),
            })

    rows.sort(key=lambda r: (
        r["event"].start_datetime.astimezone(get_app_tz()).date(),
        r["event"].name,
        r["event"].start_datetime,
        r["qual_name"],
    ))

    color_map: dict[frozenset, str] = {}
    for row in rows:
        qkey = row["qual_ids"]
        if qkey not in color_map:
            color_map[qkey] = _ROW_COLORS[len(color_map) % len(_ROW_COLORS)]
        row["color"] = color_map[qkey]

    max_spot_cols = max((len(r["spots"]) for r in rows), default=0)
    return rows, max_spot_cols


def _compute_eligible_users(rows: list[dict], all_users: list) -> None:
    """Annotate each row with ``eligible_users`` list (users who can fill those spots)."""
    from app.models.qualification import Qualification

    all_quals = db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False))
    ).all()
    parents_map: dict[int, list[int]] = {q.id: [p.id for p in q.parents] for q in all_quals}

    def _can_fill(uq_ids: set[int], target: int, visited: frozenset[int]) -> bool:
        if target in visited:
            return False
        if target in uq_ids:
            return True
        return any(_can_fill(uq_ids, pid, visited | {target}) for pid in parents_map.get(target, []))

    # Cache fillable IDs per user (computed once over the full qual graph)
    user_fillable: dict = {}
    for user in all_users:
        uq_ids = {q.id for q in user.qualifications if not q.is_deleted}
        user_fillable[user.id] = {q.id for q in all_quals if _can_fill(uq_ids, q.id, frozenset())}

    eligible_cache: dict[frozenset, list] = {}
    for row in rows:
        qkey = row["qual_ids"]
        if qkey not in eligible_cache:
            if not qkey:
                eligible_cache[qkey] = list(all_users)
            else:
                eligible_cache[qkey] = [u for u in all_users if qkey <= user_fillable[u.id]]
        row["eligible_users"] = eligible_cache[qkey]


@master_events_bp.get("/<int:me_id>/table")
@login_required
def table_manager(me_id: int) -> str:
    require_permission("event.view")

    me = get_or_404(MasterEvent, me_id)

    from app.models.event import Event
    events = db.session.scalars(
        db.select(Event)
        .where(Event.master_event_id == me_id)
        .order_by(Event.start_datetime)
    ).all()

    rows, max_spot_cols = _build_table_rows(list(events))

    # Compute how many rows each event occupies (for rowspan in utility column)
    from collections import Counter as _Counter
    event_row_spans: dict[int, int] = dict(_Counter(r["event"].id for r in rows))

    can_assign = current_user.has_permission("event.assign_other")
    can_edit_event = current_user.has_permission("event.edit")
    can_create_event = current_user.has_permission("event.create")
    can_delete_draft = current_user.has_permission("event.delete_draft")
    can_publish = current_user.has_permission("event.publish")
    can_open_assignments = current_user.has_permission("event.assignments.open")

    if can_assign:
        _compute_eligible_users(rows, list(active_users_list()))

    return render_template(
        "master_events/table_manager.html",
        me=me,
        rows=rows,
        max_spot_cols=max_spot_cols,
        event_row_spans=event_row_spans,
        can_assign=can_assign,
        can_edit_event=can_edit_event,
        can_create_event=can_create_event,
        can_delete_draft=can_delete_draft,
        can_publish=can_publish,
        can_open_assignments=can_open_assignments,
    )


@master_events_bp.post("/<int:me_id>/table/assign/<int:spot_id>")
@login_required
def table_assign(me_id: int, spot_id: int) -> Response:
    require_permission("event.assign_other")

    from app.models.event import Event, EventSpot, EventStatus
    from app.models.assignment import Assignment
    from app.models.user import UserAccount
    from app.routes.assignments import _auto_assign_rp, _auto_close_if_full

    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "Vyberte uživatele."}), 400

    user = db.session.get(UserAccount, user_id)
    if user is None or not user.is_active or user.is_archived:
        return jsonify({"ok": False, "error": "Uživatel nenalezen nebo není aktivní."}), 400

    spot = db.session.scalar(
        db.select(EventSpot).where(EventSpot.id == spot_id).with_for_update()
    )
    if spot is None:
        return jsonify({"ok": False, "error": "Pozice nenalezena."}), 404

    event = db.session.get(Event, spot.event_id)
    if event is None or event.master_event_id != me_id:
        return jsonify({"ok": False, "error": "Akce nenalezena."}), 404

    if event.status == EventStatus.COMPLETED or event.archived:
        return jsonify({"ok": False, "error": "Přiřazení není možné — akce je dokončena nebo archivována."}), 409

    if spot.assignment is not None:
        return jsonify({"ok": False, "error": "Tato pozice je již obsazena."}), 409

    existing = db.session.scalar(
        db.select(Assignment)
        .join(EventSpot, Assignment.spot_id == EventSpot.id)
        .where(EventSpot.event_id == event.id, Assignment.user_id == user.id)
    )
    if existing:
        return jsonify({"ok": False, "error": f"Uživatel {user.name} je již přihlášen na tuto akci."}), 409

    spot.assignment = Assignment(user_id=user.id, assigned_by_id=current_user.id)
    db.session.add(spot.assignment)
    db.session.flush()
    audit("create", "Assignment", spot.assignment.id,
          f"Koordinátor přiřadil '{user.name}' na akci '{event.name}' (tabulkový manažer)")
    _auto_assign_rp(event, user)
    _auto_close_if_full(event)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Tato pozice byla právě obsazena někým jiným."}), 409

    import app.mail as mailer
    mailer.send_assignment_confirmed(user, event)

    return jsonify({"ok": True, "user_name": user.name, "assignment_id": spot.assignment.id})


@master_events_bp.post("/<int:me_id>/table/unassign/<int:assignment_id>")
@login_required
def table_unassign(me_id: int, assignment_id: int) -> Response:
    require_permission("event.assign_other")

    from app.models.event import Event, EventStatus
    from app.models.assignment import Assignment
    from app.routes.assignments import _auto_clear_rp

    assignment = db.session.get(Assignment, assignment_id)
    if assignment is None:
        return jsonify({"ok": False, "error": "Přiřazení nenalezeno."}), 404

    event = db.session.get(Event, assignment.spot.event_id)
    if event is None or event.master_event_id != me_id:
        return jsonify({"ok": False, "error": "Akce nenalezena."}), 404

    if event.status == EventStatus.COMPLETED or event.archived:
        return jsonify({"ok": False, "error": "Nelze odhlásit uživatele z dokončené nebo archivované akce."}), 409

    user_name = assignment.user.name
    audit("delete", "Assignment", assignment.id,
          f"Koordinátor odhlásil '{user_name}' z akce '{event.name}' (tabulkový manažer)")
    _auto_clear_rp(event, assignment.user)
    db.session.delete(assignment)

    if event.status == EventStatus.ASSIGNMENTS_CLOSED:
        from app.models.event import EventStatus as ES
        event.status = ES.ASSIGNMENTS_OPEN
        event.version += 1

    db.session.commit()

    import app.mail as mailer
    mailer.send_assignment_released(assignment.user, event)

    return jsonify({"ok": True})


def _handle_advance_status(event: Event) -> Response:
    """Handle the advance_status field action."""
    from app.models.event import EventStatus
    _ADVANCE_MAP = {
        EventStatus.DRAFT: (EventStatus.PUBLISHED, "event.publish"),
        EventStatus.PUBLISHED: (EventStatus.ASSIGNMENTS_OPEN, "event.assignments.open"),
    }
    transition = _ADVANCE_MAP.get(event.status)
    if transition is None:
        return jsonify({"ok": False, "error": "Akce není ve stavu, který lze posunout dále."}), 400
    target_status, required_perm = transition
    if not current_user.has_permission(required_perm):
        return jsonify({"ok": False, "error": "Nemáte oprávnění pro tuto operaci."}), 403
    before_status = event.status.value
    event.status = target_status
    event.version += 1
    audit("status_change", "Event", event.id,
          f"Stav akce '{event.name}' změněn na '{target_status.value}' (tabulkový manažer)",
          diff_changes({"status": before_status}, {"status": target_status.value}))
    db.session.commit()
    import app.mail as mailer
    from app.models.user import UserAccount
    active_users = db.session.scalars(
        db.select(UserAccount)
        .where(UserAccount.is_active.is_(True))
        .where(UserAccount.is_archived.is_(False))
    ).all()
    if target_status == EventStatus.PUBLISHED:
        for u in active_users:
            mailer.send_event_published(u, event)
    elif target_status == EventStatus.ASSIGNMENTS_OPEN:
        for u in active_users:
            mailer.send_assignments_opened(u, event)
    return jsonify({"ok": True})


def _handle_shift_day(event: Event, value: str) -> Response:
    """Handle the shift_day field action."""
    try:
        delta_days = int(value)
    except ValueError:
        return jsonify({"ok": False, "error": "Neplatná hodnota posunu."}), 400
    if delta_days not in (-1, 1):
        return jsonify({"ok": False, "error": "Povolené hodnoty: -1 nebo 1."}), 400
    from datetime import timedelta
    before = {"start_datetime": event.start_datetime.isoformat(), "end_datetime": event.end_datetime.isoformat()}
    event.start_datetime += timedelta(days=delta_days)
    event.end_datetime += timedelta(days=delta_days)
    after = {"start_datetime": event.start_datetime.isoformat(), "end_datetime": event.end_datetime.isoformat()}
    event.version += 1
    audit("edit", "Event", event.id,
          f"Posunut datum akce '{event.name}' o {delta_days:+d} den (tabulkový manažer)",
          diff_changes(before, after))
    db.session.commit()
    return jsonify({"ok": True})


def _handle_shift_hour(event: Event, value: str) -> Response:
    """Handle the shift_hour field action."""
    try:
        which, delta_str = value.split(":", 1)
        delta_hours = int(delta_str)
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "Neplatná hodnota posunu hodiny."}), 400
    if which not in ("start", "end") or delta_hours not in (-1, 1):
        return jsonify({"ok": False, "error": "Neplatné parametry posunu hodiny."}), 400
    from datetime import timedelta
    attr = "start_datetime" if which == "start" else "end_datetime"
    before = {attr: getattr(event, attr).isoformat()}
    setattr(event, attr, getattr(event, attr) + timedelta(hours=delta_hours))
    after = {attr: getattr(event, attr).isoformat()}
    event.version += 1
    lbl = "začátek" if which == "start" else "konec"
    audit("edit", "Event", event.id,
          f"Posunut {lbl} akce '{event.name}' o {delta_hours:+d} hodinu (tabulkový manažer)",
          diff_changes(before, after))
    db.session.commit()
    return jsonify({"ok": True})


def _handle_datetime_field(event: Event, field: str, value: str) -> Response:
    """Handle start_datetime / end_datetime inline edits."""
    try:
        dt = datetime.fromisoformat(value).replace(tzinfo=get_app_tz()).astimezone(timezone.utc)
    except ValueError:
        return jsonify({"ok": False, "error": "Neplatný formát data a času."}), 400

    if field == "start_datetime":
        if dt >= event.end_datetime:
            return jsonify({"ok": False, "error": "Začátek musí být před koncem akce."}), 400
        changes = diff_changes({"start_datetime": event.start_datetime.isoformat()}, {"start_datetime": dt.isoformat()})
        event.start_datetime = dt
    elif field == "end_datetime":
        if dt <= event.start_datetime:
            return jsonify({"ok": False, "error": "Konec musí být po začátku akce."}), 400
        changes = diff_changes({"end_datetime": event.end_datetime.isoformat()}, {"end_datetime": dt.isoformat()})
        event.end_datetime = dt
    else:
        return jsonify({"ok": False, "error": "Neznámé pole."}), 400

    event.version += 1
    audit("edit", "Event", event.id,
          f"Upraven čas akce '{event.name}' (tabulkový manažer)",
          changes)
    db.session.commit()

    display_time = dt.astimezone(get_app_tz()).strftime("%H:%M")
    display_date = dt.astimezone(get_app_tz()).strftime("%d.%m.")
    _CZECH_DAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]
    display_day = _CZECH_DAYS[dt.astimezone(get_app_tz()).weekday()]
    from decimal import Decimal
    delta = event.end_datetime - event.start_datetime
    hours = Decimal(str(round(delta.total_seconds() / 3600, 1)))

    return jsonify({
        "ok": True,
        "display": display_time,
        "display_date": f"{display_date} {display_day}",
        "hours": str(hours).replace(".", ","),
    })


@master_events_bp.post("/<int:me_id>/table/event/<int:event_id>/update")
@login_required
def table_event_update(me_id: int, event_id: int) -> Response:
    require_permission("event.edit")

    from app.models.event import Event

    event = db.session.get(Event, event_id)
    if event is None or event.master_event_id != me_id:
        return jsonify({"ok": False, "error": "Akce nenalezena."}), 404

    field = request.form.get("field", "")
    value = request.form.get("value", "").strip()

    if field == "advance_status":
        return _handle_advance_status(event)

    if field == "color":
        color = value if _HEX_RE.match(value) else None
        event.description = _set_tm_color(event.description, color)
        event.version += 1
        audit("edit", "Event", event.id,
              f"Nastavena barva řádku (tabulkový manažer): {color or 'reset'}")
        db.session.commit()
        return jsonify({"ok": True, "color": color or ""})

    if not value:
        return jsonify({"ok": False, "error": "Hodnota nesmí být prázdná."}), 400

    if field == "name":
        if len(value) > 200:
            return jsonify({"ok": False, "error": "Název je příliš dlouhý (max 200 znaků)."}), 400
        before = {"name": event.name}
        event.name = value
        event.version += 1
        audit("edit", "Event", event.id,
              "Přejmenována akce (tabulkový manažer)",
              diff_changes(before, {"name": value}))
        db.session.commit()
        return jsonify({"ok": True, "display": value})

    if field == "shift_day":
        return _handle_shift_day(event, value)

    if field == "shift_hour":
        return _handle_shift_hour(event, value)

    return _handle_datetime_field(event, field, value)


@master_events_bp.post("/<int:me_id>/table/spots/update")
@login_required
def table_spots_update(me_id: int) -> Response:
    """Add or remove spots for a given (event, qualification) row."""
    require_permission("event.edit")

    from app.models.event import Event, EventSpot
    from app.models.qualification import Qualification
    import json

    event_id_str = request.form.get("event_id", "").strip()
    qual_ids_json = request.form.get("qual_ids_json", "[]").strip()
    new_count_str = request.form.get("new_count", "").strip()

    try:
        event_id = int(event_id_str)
    except ValueError:
        return jsonify({"ok": False, "error": "Neplatné ID akce."}), 400

    try:
        qual_ids: list[int] = [int(x) for x in json.loads(qual_ids_json)]
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Neplatné kvalifikace."}), 400

    try:
        new_count = int(new_count_str)
        if not (0 <= new_count <= 50):
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "Počet musí být celé číslo od 0 do 50."}), 400

    event = db.session.get(Event, event_id)
    if event is None or event.master_event_id != me_id:
        return jsonify({"ok": False, "error": "Akce nenalezena."}), 404

    qual_id_set = frozenset(qual_ids)
    row_spots = [
        s for s in event.spots
        if frozenset(q.id for q in s.required_qualifications if not q.is_deleted) == qual_id_set
    ]
    current_count = len(row_spots)

    if new_count == current_count:
        return jsonify({"ok": True})

    if new_count > current_count:
        qualifications = db.session.scalars(
            db.select(Qualification)
            .where(Qualification.id.in_(qual_ids), Qualification.is_deleted.is_(False))
        ).all() if qual_ids else []
        for _ in range(new_count - current_count):
            spot = EventSpot(event_id=event.id)
            spot.required_qualifications = list(qualifications)
            db.session.add(spot)
        qual_names = ", ".join(q.name for q in qualifications) if qualifications else "žádná"
        added = new_count - current_count
        event.version += 1
        audit("edit", "Event", event.id,
              f"Přidáno {added}× pozice ({qual_names}) — tabulkový manažer")
    else:
        to_remove = current_count - new_count
        unfilled = [s for s in row_spots if s.assignment is None]
        if len(unfilled) < to_remove:
            filled_blocking = to_remove - len(unfilled)
            return jsonify({
                "ok": False,
                "error": f"Nelze odebrat {to_remove} pozici/í — {filled_blocking} z nich je obsazena.",
            }), 409
        for spot in unfilled[:to_remove]:
            db.session.delete(spot)
        qual_names = ", ".join(
            q.name for q in row_spots[0].required_qualifications if not q.is_deleted
        ) if row_spots else "žádná"
        event.version += 1
        audit("edit", "Event", event.id,
              f"Odebráno {to_remove}× pozice ({qual_names}) — tabulkový manažer")

    db.session.commit()
    return jsonify({"ok": True})


@master_events_bp.post("/<int:me_id>/table/event/<int:event_id>/clone")
@login_required
def table_event_clone(me_id: int, event_id: int) -> Response:
    """Clone an event (same ME, same spots, DRAFT status, name + ' kopie')."""
    require_permission("event.create")

    from app.models.event import Event, EventSpot, EventStatus

    source = db.session.get(Event, event_id)
    if source is None or source.master_event_id != me_id:
        return jsonify({"ok": False, "error": "Akce nenalezena."}), 404

    clone = Event(
        name=f"{source.name} kopie",
        master_event_id=source.master_event_id,
        start_datetime=source.start_datetime,
        end_datetime=source.end_datetime,
        event_type=source.event_type,
        responsible_person_id=source.responsible_person_id,
        description=source.description,
        status=EventStatus.DRAFT,
    )
    db.session.add(clone)
    db.session.flush()

    for spot in source.spots:
        new_spot = EventSpot(
            event_id=clone.id,
            description=spot.description,
        )
        new_spot.required_qualifications = list(spot.required_qualifications)
        db.session.add(new_spot)

    audit("create", "Event", clone.id,
          f"Klonována akce '{source.name}' → '{clone.name}' (tabulkový manažer)")
    db.session.commit()
    return jsonify({"ok": True, "new_event_id": clone.id})
