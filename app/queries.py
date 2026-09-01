"""Reusable SQLAlchemy query builders.

Centralises common ``SELECT`` patterns that previously appeared inline in
multiple route modules.  Keeping them here makes it easier to reason about
performance (eager-load shapes, ordering) and to apply changes in one place.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import collate, func

if TYPE_CHECKING:
    from datetime import datetime

from app.extensions import db
from app.models.assignment import Assignment
from app.models.equipment import (
    EquipmentItem,
    EventEquipmentPlan,
)
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from app.utils import CS_COLLATION


def active_users_query():  # type: ignore[no-untyped-def]
    """Return a :class:`Select` for active, non-archived users ordered by name.

    Caller is responsible for executing the statement (``db.session.scalars``
    or :func:`active_users_list` for the common eager-loaded list).
    """
    return (
        db.select(UserAccount)
        .where(UserAccount.is_active == sa.true())
        .where(UserAccount.is_archived == sa.false())
        .order_by(collate(UserAccount.name, CS_COLLATION))
    )


def active_users_list() -> Sequence[UserAccount]:
    """Return all active :class:`UserAccount` rows ordered by name."""
    return db.session.scalars(active_users_query()).all()


def rp_eligible_users_list() -> list[UserAccount]:
    """Return active users who hold at least one qualification with can_be_rp=True."""
    from app.models.qualification import Qualification  # pylint: disable=import-outside-toplevel
    from app.models.qualification import user_qualifications as uq_table  # pylint: disable=import-outside-toplevel

    # Subquery to get distinct user IDs (avoids PG DISTINCT + ORDER BY conflict)
    eligible_ids = (
        db.select(UserAccount.id)
        .join(uq_table, UserAccount.id == uq_table.c.user_id)
        .join(Qualification, Qualification.id == uq_table.c.qualification_id)
        .where(
            UserAccount.is_active == sa.true(),
            UserAccount.is_archived == sa.false(),
            Qualification.can_be_rp == sa.true(),
        )
        .distinct()
        .subquery()
    )
    rows = db.session.scalars(
        db.select(UserAccount)
        .where(UserAccount.id.in_(db.select(eligible_ids.c.id)))
        .order_by(collate(UserAccount.name, CS_COLLATION))
    ).all()
    return list(rows)


def active_master_events_list() -> Sequence[MasterEvent]:
    """Return non-archived master events ordered (general first, then by name)."""
    return db.session.scalars(
        db.select(MasterEvent)
        .where(MasterEvent.archived == sa.false())
        .order_by(MasterEvent.is_general.desc(), collate(MasterEvent.name, CS_COLLATION))
    ).all()


def user_fillable_qual_ids(user: UserAccount) -> set[int]:
    """Return all qualification IDs the user can fill, respecting the hierarchy.

    A user holding qualification Q can fill any spot that requires Q *or* any
    qualification for which Q is a valid substitute (i.e. Q is an ancestor of
    that qualification in the parent chain).

    This loads all non-deleted qualifications once (tiny table) and walks the
    parent graph in Python — call it once per request and pass the result set to
    :meth:`EventSpot.is_eligible_for` instead of calling the per-spot recursive
    :meth:`EventSpot.is_eligible` in a loop.
    """
    from app.models.qualification import Qualification  # pylint: disable=import-outside-toplevel

    all_quals: list[Qualification] = list(
        db.session.scalars(db.select(Qualification).where(Qualification.is_deleted == sa.false())).all()
    )

    user_qual_ids = {q.id for q in user.qualifications if not q.is_deleted}

    # Build a mapping qual_id → set of parent IDs for fast lookup
    parents_map: dict[int, list[int]] = {q.id: [p.id for p in q.parents] for q in all_quals}

    def _user_can_fill(qual_id: int, visited: frozenset[int]) -> bool:
        if qual_id in visited:
            return False
        if qual_id in user_qual_ids:
            return True
        return any(_user_can_fill(pid, visited | {qual_id}) for pid in parents_map.get(qual_id, []))

    return {q.id for q in all_quals if _user_can_fill(q.id, frozenset())}


def in_maintenance_during(start_dt: datetime, end_dt: datetime) -> sa.sql.ClauseElement:
    """Return a SQLAlchemy expression that is TRUE when an EquipmentItem row is in
    an active maintenance window overlapping [start_dt, end_dt).

    An item is in maintenance when its unavailability_since is set and the window
    [since, until) overlaps [start_dt, end_dt).  No status column needed.
    """
    return sa.and_(
        EquipmentItem.unavailability_since.is_not(None),
        EquipmentItem.unavailability_since < end_dt,
        sa.or_(EquipmentItem.unavailability_until.is_(None), EquipmentItem.unavailability_until > start_dt),
    )


def available_quantity_for_type(
    type_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_event_id: int | None = None,
) -> int:
    """Return how many items of *type_id* are still free for the given window.

    Positive = surplus; zero = exact fit; negative = shortage.

    An item is *available* for [start_dt, end_dt) when it is not in a maintenance
    window that overlaps the query window.  Issued items are included — the person
    carrying the item may bring it to the event.
    """
    total = (
        db.session.scalar(
            db.select(func.count(EquipmentItem.id)).where(
                EquipmentItem.type_id == type_id,
                ~in_maintenance_during(start_dt, end_dt),
            )
        )
        or 0
    )

    committed_q = (
        db.select(func.sum(EventEquipmentPlan.quantity_required))
        .join(Event, Event.id == EventEquipmentPlan.event_id)
        .where(
            EventEquipmentPlan.equipment_type_id == type_id,
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
            Event.start_datetime < end_dt,
            Event.end_datetime > start_dt,
        )
    )
    if exclude_event_id is not None:
        committed_q = committed_q.where(Event.id != exclude_event_id)

    committed = db.session.scalar(committed_q) or 0
    return total - committed


def _assignment_conflict_base_query() -> sa.Select:
    """Base select over Assignment→EventSpot→Event with the standard status/archive filter.

    Excludes cancelled/completed/archived events. Draft events remain included on
    purpose — an unpublished draft still represents a likely conflict once it goes
    live, matching the equipment-conflict behaviour.
    """
    return (
        db.select(Assignment.user_id, Event.id, Event.name, Event.start_datetime, Event.end_datetime)
        .join(EventSpot, EventSpot.id == Assignment.spot_id)
        .join(Event, Event.id == EventSpot.event_id)
        .where(
            Event.status.not_in([EventStatus.CANCELLED, EventStatus.COMPLETED]),
            Event.archived == sa.false(),
        )
    )


def user_ids_with_conflicting_assignments(
    start_dt: datetime,
    end_dt: datetime,
    exclude_event_id: int | None = None,
    restrict_to_user_ids: Iterable[UUID] | None = None,
) -> set[UUID]:
    """Return the set of user IDs assigned to any non-cancelled/completed event whose
    time range overlaps [start_dt, end_dt).

    Overlap uses strict inequalities so back-to-back events (one ending exactly as
    the next starts) do *not* conflict — mirrors :func:`available_quantity_for_type`.

    When *restrict_to_user_ids* is given, the query only considers assignments for
    that user set. An empty iterable short-circuits to an empty result.
    """
    if restrict_to_user_ids is not None:
        restrict_ids = list(restrict_to_user_ids)
        if not restrict_ids:
            return set()
    else:
        restrict_ids = None

    q = _assignment_conflict_base_query().where(
        Event.start_datetime < end_dt,
        Event.end_datetime > start_dt,
    )
    if exclude_event_id is not None:
        q = q.where(Event.id != exclude_event_id)
    if restrict_ids is not None:
        q = q.where(Assignment.user_id.in_(restrict_ids))
    return {row[0] for row in db.session.execute(q).all()}


def conflicting_events_for_users(
    user_ids: Iterable[UUID],
    start_dt: datetime,
    end_dt: datetime,
    exclude_event_id: int | None = None,
) -> dict[UUID, list[dict]]:
    """Return per-user list of events causing a conflict in [start_dt, end_dt).

    Each entry is a plain dict (``id``, ``name``, ``start_datetime``, ``end_datetime``)
    ordered by ``start_datetime``. Users without conflicts are omitted from the result.
    """
    ids = list(user_ids)
    if not ids:
        return {}
    q = (
        _assignment_conflict_base_query()
        .where(
            Event.start_datetime < end_dt,
            Event.end_datetime > start_dt,
            Assignment.user_id.in_(ids),
        )
        .order_by(Event.start_datetime)
    )
    if exclude_event_id is not None:
        q = q.where(Event.id != exclude_event_id)

    result: dict[UUID, list[dict]] = {}
    for user_id, event_id, event_name, start, end in db.session.execute(q).all():
        result.setdefault(user_id, []).append(
            {
                "id": event_id,
                "name": event_name,
                "start_datetime": start,
                "end_datetime": end,
            }
        )
    return result


def assignment_conflicts(
    now: datetime, user_id: UUID | None = None, *, include_drafts: bool = True
) -> list[tuple[dict, dict, dict]]:
    """Return active assignment-conflict pairs, optionally for one user.

    Completed, cancelled, archived and already-ended events are ignored. The
    query is deliberately unbounded by the dashboard horizon so a future
    conflict is never hidden by that preference.
    """
    query = (
        _assignment_conflict_base_query()
        .add_columns(UserAccount.name)
        .join(UserAccount, UserAccount.id == Assignment.user_id)
        .where(Event.end_datetime > now)
        .distinct()
        .order_by(Event.start_datetime)
    )
    if user_id is not None:
        query = query.where(Assignment.user_id == user_id)
    if not include_drafts:
        query = query.where(Event.status != EventStatus.DRAFT)

    events_by_user: dict[UUID, tuple[dict, list[dict]]] = {}
    for assignment_user_id, event_id, event_name, start, end, user_name in db.session.execute(query).all():
        user, events = events_by_user.setdefault(assignment_user_id, ({"name": user_name}, []))
        events.append({"id": event_id, "name": event_name, "start_datetime": start, "end_datetime": end})

    return [
        (user, first, second)
        for user, events in events_by_user.values()
        for index, first in enumerate(events)
        for second in events[index + 1 :]
        if second["start_datetime"] < first["end_datetime"]
    ]


def serialize_conflicts_for_template(
    conflicts_by_user: dict[UUID, list[dict]],
    event_url_for: Callable[[int], str],
) -> dict[str, list[dict]]:
    """Shape :func:`conflicting_events_for_users` / :func:`user_conflicts_across_events`
    output for the picker templates: stringified user IDs (matching Jinja's ``|string``
    key lookup) mapping to JSON-serialisable conflict dicts with ISO timestamps and a
    resolved detail URL.
    """
    return {
        str(uid): [
            {
                "name": c["name"],
                "url": event_url_for(c["id"]),
                "start": c["start_datetime"].isoformat(),
                "end": c["end_datetime"].isoformat(),
            }
            for c in conflicts
        ]
        for uid, conflicts in conflicts_by_user.items()
    }


def user_conflicts_across_events(
    events: Sequence[Event],
    restrict_to_user_ids: Iterable[UUID] | None = None,
) -> dict[int, dict[UUID, list[dict]]]:
    """Compute per-event user-conflict maps for a batch of events using one query.

    Returns ``{event_id: {user_id: [conflict_event_dict, ...], ...}, ...}``. Only
    users with at least one conflict against the given event appear. Used by the
    Table Manager to avoid an O(N) query fan-out.

    A conflict event is any non-cancelled/completed/archived event overlapping the
    displayed event's ``[start_datetime, end_datetime)`` window — with the
    displayed event itself excluded.

    When *restrict_to_user_ids* is given, only assignments for that user set are
    considered. An empty iterable short-circuits to an empty per-event mapping.
    """
    if not events:
        return {}
    if restrict_to_user_ids is not None:
        restrict_ids = list(restrict_to_user_ids)
        if not restrict_ids:
            return {e.id: {} for e in events}
    else:
        restrict_ids = None

    min_start = min(e.start_datetime for e in events)
    max_end = max(e.end_datetime for e in events)

    # Single query fetching every assignment that could conflict with *any* displayed
    # event: window must overlap [min_start, max_end). We then pair each candidate
    # against the individual displayed events in Python.
    q = _assignment_conflict_base_query().where(
        Event.start_datetime < max_end,
        Event.end_datetime > min_start,
    )
    if restrict_ids is not None:
        q = q.where(Assignment.user_id.in_(restrict_ids))
    candidates = db.session.execute(q).all()

    result: dict[int, dict[UUID, list[dict]]] = {e.id: {} for e in events}
    for user_id, cand_event_id, cand_name, cand_start, cand_end in candidates:
        for target in events:
            if cand_event_id == target.id:
                continue
            if cand_start < target.end_datetime and cand_end > target.start_datetime:
                result[target.id].setdefault(user_id, []).append(
                    {
                        "id": cand_event_id,
                        "name": cand_name,
                        "start_datetime": cand_start,
                        "end_datetime": cand_end,
                    }
                )
    for per_event in result.values():
        for conflicts in per_event.values():
            conflicts.sort(key=lambda c: c["start_datetime"])
    return result
