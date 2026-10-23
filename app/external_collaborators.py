"""External opportunity policy shared by browsing, signup, and notifications."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventStatus, StaffingMode
from app.models.master_event import MasterEvent
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.staffing import condition_join_error


def external_opportunity(event: Event, now: datetime | None = None) -> bool:
    """Early opening bypasses only the configured window, never staffing/lifecycle rules."""
    return _external_opportunity(event, now or datetime.now(timezone.utc), get_settings().external_opportunity_days)


def _external_opportunity(event: Event, now: datetime, window_days: int) -> bool:
    if (
        event.archived
        or event.status != EventStatus.ASSIGNMENTS_OPEN
        or event.start_datetime <= now
        or (event.start_datetime >= now + timedelta(days=window_days) and not event.open_to_externals)
        or event.is_centrally_coordinated
    ):
        return False
    if event.staffing_mode == StaffingMode.CONDITIONS:
        summary = event.staffing_summary
        return not summary.is_staffing_sufficient and not summary.is_capacity_full
    return any(spot.assignment is None for spot in event.spots)


def external_can_participate(event: Event, user: UserAccount, now: datetime | None = None) -> bool:
    """Whether this user can claim an external opportunity under existing qualification rules."""
    if (
        not user.is_external
        or not user.is_active
        or user.is_archived
        or not external_opportunity(event, now)
        or any(assignment.user_id == user.id for assignment in event.assignments)
    ):
        return False
    if event.staffing_mode == StaffingMode.CONDITIONS:
        return condition_join_error(event, user) is None
    return any(spot.assignment is None and spot.is_eligible(user) for spot in event.spots)


def external_opportunities(now: datetime | None = None) -> list[Event]:
    """Bound candidates in SQL; evaluate condition coverage using its existing matcher."""
    now = now or datetime.now(timezone.utc)
    window_days = get_settings().external_opportunity_days
    candidates = db.session.scalars(
        db.select(Event)
        .join(Event.master_event)
        .where(
            Event.archived == sa.false(),
            Event.status == EventStatus.ASSIGNMENTS_OPEN,
            Event.start_datetime > now,
            sa.or_(
                Event.start_datetime < now + timedelta(days=window_days),
                Event.open_to_externals == sa.true(),
            ),
            MasterEvent.coordinator_id.is_(None),
        )
    ).all()
    return [event for event in candidates if _external_opportunity(event, now, window_days)]


def external_visible_event_ids(user: UserAccount, now: datetime | None = None) -> set[int]:
    opportunities = {event.id for event in external_opportunities(now)}
    own_events = db.session.scalars(
        db.select(Assignment.event_id)
        .join(Assignment.event)
        .where(Assignment.user_id == user.id, Event.status != EventStatus.DRAFT)
    ).all()
    return opportunities | set(own_events)


def filter_external_events(query: sa.Select, user: UserAccount, now: datetime | None = None) -> sa.Select:
    if user.is_external:
        return query.where(Event.id.in_(external_visible_event_ids(user, now)))
    return query
