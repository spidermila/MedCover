"""Shared helpers for the events blueprint sub-modules."""

from datetime import datetime, timezone

import sqlalchemy as sa
from flask import url_for
from flask_login import current_user

from app.extensions import db
from app.models.equipment import EquipmentItem, EventEquipmentAssignment, EventEquipmentPlan
from app.models.event import Event, EventSpot, EventStatus, EventTemplate, EventType
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.utils import get_app_tz

# Valid manual lifecycle transitions: (from_status, to_status, required_permission)
TRANSITIONS: list[tuple[EventStatus, EventStatus, str]] = [
    (EventStatus.DRAFT, EventStatus.PUBLISHED, "event.publish"),
    (EventStatus.PUBLISHED, EventStatus.ASSIGNMENTS_OPEN, "event.assignments.open"),
    (EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED, "event.assignments.close"),
    (EventStatus.ASSIGNMENTS_CLOSED, EventStatus.ASSIGNMENTS_OPEN, "event.assignments.open"),
]

# Maps action name → (target_status, required_permission, valid_from_statuses)
BULK_STATE_ACTIONS: dict[str, tuple[EventStatus, str, set[EventStatus]]] = {
    "publish": (
        EventStatus.PUBLISHED,
        "event.publish",
        {EventStatus.DRAFT},
    ),
    "open_assignments": (
        EventStatus.ASSIGNMENTS_OPEN,
        "event.assignments.open",
        {EventStatus.PUBLISHED, EventStatus.ASSIGNMENTS_CLOSED},
    ),
    "cancel": (
        EventStatus.CANCELLED,
        "event.cancel",
        {EventStatus.DRAFT, EventStatus.PUBLISHED, EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED},
    ),
}

# FullCalendar event background colours by status value
STATUS_COLORS: dict[str, str] = {
    "Koncept": "#6c757d",
    "Zveřejněná": "#0d6efd",
    "Přihlášky otevřeny": "#198754",
    "Přihlášky uzavřeny": "#ffc107",
    "Dokončena": "#212529",
    "Zrušena": "#adb5bd",
}

# Bootstrap badge colour class names by status value (used in event list template)
STATUS_BADGE_COLORS: dict[str, str] = {
    "Koncept": "secondary",
    "Zveřejněná": "primary",
    "Přihlášky otevřeny": "success",
    "Přihlášky uzavřeny": "warning",
    "Dokončena": "dark",
    "Zrušena": "secondary",
}

PER_PAGE = 75


def can_view(event: Event) -> bool:
    """Check whether current_user can see *event* based on its status."""
    if event.status == EventStatus.DRAFT:
        return current_user.has_permission("event.view_draft")
    return current_user.has_permission("event.view")


def _parse_form_fields(form: dict) -> dict:
    """Extract and normalize raw field values from the event form."""
    return {
        "name": form.get("name", "").strip(),
        "master_event_id": form.get("master_event_id", "").strip(),
        "start_str": form.get("start_datetime", "").strip(),
        "end_str": form.get("end_datetime", "").strip(),
        "address": form.get("address", "").strip() or None,
        "contact_person": form.get("contact_person", "").strip() or None,
        "description": form.get("description", "").strip() or None,
        "paid": form.get("paid") == "1",
        "responsible_person_id": form.get("responsible_person_id") or None,
        "assignments_open_str": form.get("assignments_open_datetime", "").strip(),
        "event_type_str": form.get("event_type", "").strip(),
        "planned_participants_count_str": form.get("planned_participants_count", "").strip(),
    }


def _validate_event_fields(
    fields: dict,
) -> tuple[str | None, EventType, int | None, datetime | None, datetime | None, datetime | None]:
    """Validate parsed form fields and return (error, event_type, ppc, start_dt, end_dt, assignments_open_dt).

    On error, only the first element is non-None.
    """

    def _local_to_utc(s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=get_app_tz()).astimezone(timezone.utc)

    event_type_str = fields["event_type_str"]
    event_type = EventType[event_type_str] if event_type_str in EventType.__members__ else EventType.MEDICAL_COVER

    planned_participants_count: int | None = None
    if event_type == EventType.TRAINING and fields["planned_participants_count_str"]:
        try:
            planned_participants_count = int(fields["planned_participants_count_str"])
            if planned_participants_count < 0:
                return "Plánovaný počet účastníků musí být nezáporné číslo.", event_type, None, None, None, None
        except ValueError:
            return "Plánovaný počet účastníků musí být celé číslo.", event_type, None, None, None, None

    if not fields["name"]:
        return "Název akce je povinný.", event_type, None, None, None, None
    if not fields["master_event_id"]:
        return "Nadřazená akce je povinná.", event_type, None, None, None, None
    if not fields["start_str"] or not fields["end_str"]:
        return "Datum a čas začátku i konce jsou povinné.", event_type, None, None, None, None

    try:
        start_dt = _local_to_utc(fields["start_str"])
        end_dt = _local_to_utc(fields["end_str"])
    except ValueError:
        return "Neplatný formát data a času.", event_type, None, None, None, None

    if end_dt <= start_dt:
        return "Konec akce musí být po začátku.", event_type, None, None, None, None

    # Validate RP: Viewer-only users cannot be RP (AD17)
    if fields["responsible_person_id"]:
        rp_user = db.session.get(UserAccount, fields["responsible_person_id"])
        if rp_user:
            rp_role_names = {r.name for r in rp_user.roles}
            if rp_role_names <= {Role.VIEWER}:
                return (
                    (
                        f"Uživatel {rp_user.name} má pouze roli Pozorovatel a nemůže být "
                        "odpovědnou osobou. Jako OP je potřeba mít roli Člen nebo vyšší."
                    ),
                    event_type,
                    None,
                    None,
                    None,
                    None,
                )

    assignments_open_dt = None
    if fields["assignments_open_str"]:
        try:
            assignments_open_dt = _local_to_utc(fields["assignments_open_str"])
        except ValueError:
            return "Neplatný formát data otevření přihlášek.", event_type, None, None, None, None

    if assignments_open_dt is not None and assignments_open_dt >= start_dt:
        return (
            "Datum otevření přihlášek musí být před začátkem akce.",
            event_type,
            None,
            None,
            None,
            None,
        )

    return None, event_type, planned_participants_count, start_dt, end_dt, assignments_open_dt


def parse_event_form(form: dict, existing: Event | None = None) -> tuple[Event | None, str | None]:
    """Parse the event form and return (event, error_message).

    All datetime inputs are interpreted as the app-configured local time
    (AppSettings.timezone) and stored as UTC in the database.
    """
    fields = _parse_form_fields(form)
    error, event_type, planned_participants_count, start_dt, end_dt, assignments_open_dt = _validate_event_fields(
        fields
    )
    if error:
        return None, error

    assert start_dt is not None and end_dt is not None  # mypy: validated above

    kwargs = {
        "name": fields["name"],
        "master_event_id": int(fields["master_event_id"]),
        "start_datetime": start_dt,
        "end_datetime": end_dt,
        "address": fields["address"],
        "contact_person": fields["contact_person"],
        "description": fields["description"],
        "paid": fields["paid"],
        "responsible_person_id": fields["responsible_person_id"],
        "assignments_open_datetime": assignments_open_dt,
        "event_type": event_type,
        "planned_participants_count": planned_participants_count,
    }

    if existing is not None:
        for attr, val in kwargs.items():
            setattr(existing, attr, val)
        return existing, None

    event = Event(**kwargs, created_by_id=current_user.id)
    return event, None


def build_spots(event: Event, form: dict) -> None:
    """Create spots from the dynamic spot builder fields (spot_desc_N / spot_cred_N / spot_optional_N)."""
    try:
        spot_total = int(form.get("spot_total", 0) or 0)
    except ValueError, TypeError:
        spot_total = 0

    for i in range(spot_total):
        description = (form.get(f"spot_desc_{i}") or "").strip() or None
        is_optional = form.get(f"spot_optional_{i}") == "1"
        qual_ids = [int(c) for c in form.getlist(f"spot_cred_{i}") if str(c).isdigit()]
        qualifications = (
            db.session.scalars(
                db.select(Qualification).where(Qualification.id.in_(qual_ids), Qualification.is_deleted == sa.false())
            ).all()
            if qual_ids
            else []
        )
        spot = EventSpot(event_id=event.id, description=description, is_optional=is_optional)
        spot.required_qualifications = list(qualifications)
        db.session.add(spot)


def build_spots_from_template(event: Event, template: object) -> None:
    """Create event spots and equipment plans matching a template."""
    if not isinstance(template, EventTemplate):
        return
    for st in template.spot_templates:
        spot = EventSpot(event_id=event.id, description=st.description, is_optional=st.is_optional)
        spot.required_qualifications = list(st.required_qualifications)
        db.session.add(spot)
    for ep in template.equipment_plans:
        plan = EventEquipmentPlan(
            event_id=event.id,
            equipment_type_id=ep.equipment_type_id,
            quantity_required=ep.quantity_required,
        )
        db.session.add(plan)


def build_equipment_assignments(event: Event, item_ids: list[int]) -> None:
    """Create EventEquipmentAssignment records for *item_ids* on *event*.

    Silently skips unavailable items or IDs that don't resolve to a real item.
    Caller must have already flushed the event so event.id is set.
    """
    for item_id in item_ids:
        item = db.session.get(EquipmentItem, item_id)
        if item is None or not item.is_available:
            continue
        db.session.add(EventEquipmentAssignment(event_id=event.id, equipment_item_id=item.id))


def equipment_warnings_for_event(event: Event) -> list[dict]:
    """Return a list of warning dicts for items already assigned to *event*.

    Each dict has keys: item_name, status ("unavailable"|"conflict"),
    reason (str|None), conflicting_event (dict|None).
    """
    warnings: list[dict] = []
    assigned_items = [ea.equipment_item for ea in event.equipment_assignments]
    if not assigned_items:
        return warnings

    # Separate unavailable items (no DB query needed)
    available_ids: list[int] = []
    for item in assigned_items:
        if not item.is_available:
            warnings.append(
                {
                    "item_name": item.name,
                    "status": "unavailable",
                    "reason": item.unavailability_reason or "Bez udaného důvodu",
                    "conflicting_event": None,
                }
            )
        else:
            available_ids.append(item.id)

    # Single query for all conflicts across all available assigned items
    if available_ids:
        conflicts = db.session.scalars(
            db.select(EventEquipmentAssignment)
            .join(Event, EventEquipmentAssignment.event_id == Event.id)
            .where(
                EventEquipmentAssignment.equipment_item_id.in_(available_ids),
                EventEquipmentAssignment.event_id != event.id,
                Event.status != EventStatus.CANCELLED,
                Event.archived == sa.false(),
                Event.start_datetime < event.end_datetime,
                Event.end_datetime > event.start_datetime,
            )
        ).all()
        # Build item_id → name lookup
        id_to_name = {item.id: item.name for item in assigned_items}
        for c in conflicts:
            ce = c.event
            warnings.append(
                {
                    "item_name": id_to_name[c.equipment_item_id],
                    "status": "conflict",
                    "reason": None,
                    "conflicting_event": {
                        "name": ce.name,
                        "start": ce.start_datetime,
                        "end": ce.end_datetime,
                        "url": url_for("events.detail", event_id=ce.id),
                    },
                }
            )
    return warnings


def copy_spots_with_assignments(source: Event, target: Event) -> None:
    """Copy spots (+ qualifications + existing assignments) from source to target."""
    from app.models.assignment import Assignment  # pylint: disable=import-outside-toplevel

    for spot in source.spots:
        new_spot = EventSpot(
            event_id=target.id,
            description=spot.description,
            is_optional=spot.is_optional,
        )
        new_spot.required_qualifications = list(spot.required_qualifications)
        db.session.add(new_spot)
        db.session.flush()  # need new_spot.id for the assignment

        if spot.assignment is not None:
            new_assignment = Assignment(
                spot_id=new_spot.id,
                user_id=spot.assignment.user_id,
            )
            db.session.add(new_assignment)


def copy_equipment(source: Event, target: Event) -> None:
    """Copy equipment plans and assignments from source to target."""
    for plan in source.equipment_plans:
        db.session.add(
            EventEquipmentPlan(
                event_id=target.id,
                equipment_type_id=plan.equipment_type_id,
                quantity_required=plan.quantity_required,
            )
        )
    for ea in source.equipment_assignments:
        db.session.add(
            EventEquipmentAssignment(
                event_id=target.id,
                equipment_item_id=ea.equipment_item_id,
            )
        )


def validate_event_spots_config(spots: list[EventSpot]) -> str | None:
    """Validate that the spot configuration satisfies the RP-capable spot constraint.

    Returns an error message string if the configuration is invalid, or None if valid.
    """
    if not spots:
        return "Akce musí mít alespoň jednu pozici."

    mandatory_spots = [s for s in spots if not s.is_optional]
    if not mandatory_spots:
        return "Akce musí mít alespoň jednu povinnou pozici."

    for spot in mandatory_spots:
        active_required_quals = [q for q in spot.required_qualifications if not q.is_deleted]
        if any(q.can_be_rp for q in active_required_quals):
            return None

    return "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci umožňující roli zodpovědné osoby."
