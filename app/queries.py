"""Reusable SQLAlchemy query builders.

Centralises common ``SELECT`` patterns that previously appeared inline in
multiple route modules.  Keeping them here makes it easier to reason about
performance (eager-load shapes, ordering) and to apply changes in one place.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import collate, func

if TYPE_CHECKING:
    from datetime import datetime

from app.extensions import db
from app.models.equipment import (
    EquipmentItem,
    EventEquipmentPlan,
)
from app.models.event import Event, EventStatus
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
    """Return how many SHARED items of *type_id* are still free for the given window.

    Positive = surplus; zero = exact fit; negative = shortage.

    An item is *available* for [start_dt, end_dt) when it is not in a maintenance
    window that overlaps the query window.  Issued items are included — the person
    carrying the item may bring it to the event.  Personal-category types are
    always excluded.
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
