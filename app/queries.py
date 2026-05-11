"""Reusable SQLAlchemy query builders.

Centralises common ``SELECT`` patterns that previously appeared inline in
multiple route modules.  Keeping them here makes it easier to reason about
performance (eager-load shapes, ordering) and to apply changes in one place.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.extensions import db
from app.models.master_event import MasterEvent
from app.models.user import UserAccount


def active_users_query():  # type: ignore[no-untyped-def]
    """Return a :class:`Select` for active, non-archived users ordered by name.

    Caller is responsible for executing the statement (``db.session.scalars``
    or :func:`active_users_list` for the common eager-loaded list).
    """
    return (
        db.select(UserAccount)
        .where(UserAccount.is_active.is_(True))
        .where(UserAccount.is_archived.is_(False))
        .order_by(UserAccount.name)
    )


def active_users_list() -> Sequence[UserAccount]:
    """Return all active :class:`UserAccount` rows ordered by name."""
    return db.session.scalars(active_users_query()).all()


def rp_eligible_users_list() -> Sequence[UserAccount]:
    """Return active users who hold at least one qualification with can_be_rp=True."""
    from app.models.qualification import Qualification, user_qualifications as uq_table
    return db.session.scalars(
        db.select(UserAccount)
        .join(uq_table, UserAccount.id == uq_table.c.user_id)
        .join(Qualification, Qualification.id == uq_table.c.qualification_id)
        .where(
            UserAccount.is_active.is_(True),
            UserAccount.is_archived.is_(False),
            Qualification.can_be_rp.is_(True),
        )
        .distinct()
        .order_by(UserAccount.name)
    ).all()


def active_master_events_list() -> Sequence[MasterEvent]:
    """Return non-archived master events ordered (general first, then by name)."""
    return db.session.scalars(
        db.select(MasterEvent)
        .where(MasterEvent.archived.is_(False))
        .order_by(MasterEvent.is_general.desc(), MasterEvent.name)
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
    from app.models.qualification import Qualification

    all_quals: list[Qualification] = list(db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False))
    ).all())

    user_qual_ids = {q.id for q in user.qualifications if not q.is_deleted}

    # Build a mapping qual_id → set of parent IDs for fast lookup
    parents_map: dict[int, list[int]] = {q.id: [p.id for p in q.parents] for q in all_quals}

    def _user_can_fill(qual_id: int, visited: frozenset[int]) -> bool:
        if qual_id in visited:
            return False
        if qual_id in user_qual_ids:
            return True
        return any(
            _user_can_fill(pid, visited | {qual_id})
            for pid in parents_map.get(qual_id, [])
        )

    return {q.id for q in all_quals if _user_can_fill(q.id, frozenset())}
