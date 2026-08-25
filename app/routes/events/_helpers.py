"""Shared helpers for the events blueprint sub-modules."""

from datetime import datetime, timezone

import sqlalchemy as sa
from flask import flash, url_for
from flask_login import current_user
from markupsafe import Markup
from sqlalchemy import collate

from app.extensions import db
from app.models.equipment import EquipmentType, EventEquipmentPlan
from app.models.event import Event, EventSpot, EventStatus, EventType
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from app.queries import available_quantity_for_type
from app.utils import CS_COLLATION, get_app_tz

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


def parse_equipment_plans_from_form(form: dict) -> list[tuple[int, int]]:
    """Return list of (type_id, quantity) pairs from the form's eq_* fields."""
    try:
        total = int(form.get("eq_total", 0) or 0)
    except ValueError:
        total = 0
    plans: list[tuple[int, int]] = []
    seen: set[int] = set()
    for i in range(total):
        raw_type = form.get(f"eq_type_id_{i}", "")
        raw_qty = form.get(f"eq_qty_{i}", "1")
        if not raw_type or not str(raw_type).isdigit():
            continue
        type_id = int(raw_type)
        if type_id in seen:
            et = db.session.get(EquipmentType, type_id)
            type_name = et.name if et else str(type_id)
            flash(f"Typ vybavení „{type_name}“ byl zadán vícekrát — zachován první výskyt.", "warning")
            continue
        try:
            qty = max(1, int(raw_qty))
        except ValueError:
            et = db.session.get(EquipmentType, type_id)
            type_name = et.name if et else str(type_id)
            flash(f"Množství pro typ vybavení „{type_name}“ nebylo možné načíst — použita hodnota 1.", "warning")
            qty = 1
        plans.append((type_id, qty))
        seen.add(type_id)
    return plans


def check_equipment_conflicts(
    plans: list[tuple[int, int]],
    start_dt: datetime,
    end_dt: datetime,
    exclude_event_id: int | None = None,
) -> list[Markup]:
    """Check each (type_id, qty) plan against available stock.

    Returns a list of Markup flash messages (one per conflicting type), each
    containing a link to the first conflicting event so the user can act.
    Returns an empty list when everything fits.
    """
    errors: list[Markup] = []
    for type_id, qty in plans:
        avail = available_quantity_for_type(type_id, start_dt, end_dt, exclude_event_id=exclude_event_id)
        if avail >= qty:
            continue

        et = db.session.get(EquipmentType, type_id)
        type_name = et.name if et else str(type_id)

        # Find the events consuming stock during this window (for the link).
        conflicting = db.session.scalars(
            db.select(Event)
            .join(EventEquipmentPlan, EventEquipmentPlan.event_id == Event.id)
            .where(
                EventEquipmentPlan.equipment_type_id == type_id,
                Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
                Event.archived == sa.false(),
                Event.start_datetime < end_dt,
                Event.end_datetime > start_dt,
                *([] if exclude_event_id is None else [Event.id != exclude_event_id]),
            )
            .order_by(Event.start_datetime)
            .limit(3)
        ).all()

        msg = Markup("Nedostatek vybavení — typ „{name}“: požadováno {qty} ks, k dispozici {avail} ks.").format(
            name=type_name, qty=qty, avail=max(0, avail)
        )
        if conflicting:
            links = Markup(", ").join(
                Markup('<a href="{}">{}</a>').format(url_for("events.detail", event_id=e.id), e.name)
                for e in conflicting
            )
            msg = msg + Markup(" Konflikt s: ") + links + Markup(".")
        errors.append(msg)
    return errors


def apply_equipment_plans(event: Event, plans: list[tuple[int, int]]) -> None:
    """Replace the event's equipment plans with *plans* (type_id, qty pairs).

    Deletes removed rows, upserts the rest.  Caller must flush/commit.
    """
    existing = {p.equipment_type_id: p for p in event.equipment_plans}
    wanted = {type_id for type_id, _ in plans}

    # Remove plans no longer in the list
    for type_id, plan in list(existing.items()):
        if type_id not in wanted:
            db.session.delete(plan)

    # Upsert remaining
    for type_id, qty in plans:
        if type_id in existing:
            existing[type_id].quantity_required = qty
        else:
            db.session.add(EventEquipmentPlan(event_id=event.id, equipment_type_id=type_id, quantity_required=qty))


def all_equipment_types() -> list[EquipmentType]:
    """Return all equipment types ordered by name (for form selectors)."""
    return list(db.session.scalars(db.select(EquipmentType).order_by(collate(EquipmentType.name, CS_COLLATION))).all())


def copy_equipment(source: Event, target: Event) -> None:
    """Copy equipment plans from source to target."""
    for plan in source.equipment_plans:
        db.session.add(
            EventEquipmentPlan(
                event_id=target.id,
                equipment_type_id=plan.equipment_type_id,
                quantity_required=plan.quantity_required,
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
