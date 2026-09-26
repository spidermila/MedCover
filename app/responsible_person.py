"""Keeping each open event's responsible person („Zodpovědná osoba“) eligible
after participation, qualifications, roles or account status change."""

from app.extensions import db
from app.models.event import Event, EventStatus
from app.utils import audit


def refresh_responsible_person(event: Event) -> None:
    eligible = [a.user for a in event.assignments if a.user.is_rp_eligible()]
    if any(u.id == event.responsible_person_id for u in eligible):
        return
    person_id = eligible[0].id if eligible else None
    if event.responsible_person_id != person_id:
        event.responsible_person_id = person_id
        event.version += 1
        audit("edit", "Event", event.id, "Zodpovědná osoba přepočtena podle účasti a aktivních kvalifikací")


def refresh_responsible_people() -> None:
    """Re-check every open event, e.g. after changes for many people at once."""
    # Batch-load participants and their current qualifications, including after bulk unlinking.
    db.session.flush()
    for event in db.session.scalars(
        db.select(Event)
        .where(Event.status.not_in((EventStatus.COMPLETED, EventStatus.CANCELLED)))
        .order_by(Event.id)
        .with_hint(Event, "WITH (UPDLOCK, HOLDLOCK, ROWLOCK)")
        .execution_options(populate_existing=True)
    ).all():
        refresh_responsible_person(event)
