"""Link audit rows to the event whose change log shows them.

Adds audit_log_entry.event_id. Rows on an event get its id. Sign-ups and
debriefing submissions are logged on the assignment or debriefing record, so
they are resolved through it while it still exists. A deleted assignment takes
its event from the entry that removed it: a sign-off logged on the event, or an
automatic removal after a spot edit, whose spot edit entry on the event was
written in the same request. Sign-offs logged on the assignment itself, as they
were before, carry no event and only close their own sign-up. Remaining rows
fall back to the event name quoted in the summary, but only when that name is
unique and the event already existed at the time of the entry. Anything else
stays unlinked.

Revision ID: 037b3977dcb5
Revises: f7a8b9c0d1e2
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "037b3977dcb5"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

_EVENT_NAME = re.compile(r"(?:na akci|pro akci|z akce) '(.+)'$")
_SIGN_UP = re.compile(
    r"^(?:Uživatel '(?P<self>.+)' se přihlásil|'.+' přiřadil '(?P<other>.+)') na akci '(?P<event>.+)'$"
)
_CONDITION_SIGN_UP = re.compile(r"^'.+' přihlásil/a '(?P<other>.+)' na akci '(?P<event>.+)'$")
_SIGN_OFF = re.compile(
    r"^(?:Uživatel '(?P<self>.+)' se odhlásil|'.+' odhlásil '(?P<other>.+)') z akce '(?P<event>.+)'$"
)


def upgrade():
    op.add_column("audit_log_entry", sa.Column("event_id", sa.Integer(), nullable=True))
    op.create_index("ix_audit_log_entry_event", "audit_log_entry", ["event_id", "timestamp"])

    conn = op.get_bind()
    # {id: (event id, created at)}. A backup restore resets identity counters, so an
    # id can belong to a newer row than the entry; see _existing().
    assignments = {
        aid: (eid, at) for aid, eid, at in conn.execute(sa.text("SELECT id, event_id, assigned_at FROM assignment"))
    }
    records = {
        rid: (eid, at)
        for rid, eid, at in conn.execute(
            sa.text(
                "SELECT d.id, a.event_id, d.submitted_at FROM debriefing_record d"
                " JOIN assignment a ON a.id = d.assignment_id"
            )
        )
    }
    events = conn.execute(sa.text("SELECT id, name, created_at FROM event")).all()
    name_counts = Counter(name for _, name, _ in events)
    event_by_unique_name = {name: (eid, created) for eid, name, created in events if name_counts[name] == 1}
    event_created = {eid: created for eid, _, created in events}

    rows = conn.execute(
        sa.text(
            "SELECT id, entity_type, entity_id, action_type, actor_id, summary, timestamp FROM audit_log_entry"
            " WHERE entity_type IN ('Assignment', 'DebriefingRecord', 'Event') ORDER BY timestamp, id"
        )
    ).all()
    deleted_event = _resolve_deleted_assignments(rows, assignments, event_created)

    updates = []
    for row_id, entity_type, entity_id, _, _, summary, timestamp in rows:
        if entity_type == "Event":
            if entity_id.isdigit():
                updates.append({"eid": int(entity_id), "id": row_id})
            continue
        event_id = _existing(assignments if entity_type == "Assignment" else records, entity_id, timestamp)
        if event_id is None:
            event_id = deleted_event.get(row_id)
        if event_id is None and (match := _EVENT_NAME.search(summary)):
            candidate, created = event_by_unique_name.get(match.group(1), (None, None))
            if candidate is not None and _utc(created) <= _utc(timestamp):
                event_id = candidate
        if event_id is not None:
            updates.append({"eid": event_id, "id": row_id})
    if updates:
        conn.execute(sa.text("UPDATE audit_log_entry SET event_id = :eid WHERE id = :id"), updates)


def _existing(lookup: dict, entity_id: str, timestamp: datetime) -> int | None:
    """Event of the still-existing row the entry refers to, if the row predates the entry."""
    found = lookup.get(int(entity_id)) if entity_id.isdigit() else None
    if found is not None and _utc(found[1]) <= _utc(timestamp):
        return found[0]
    return None


def _resolve_deleted_assignments(rows, assignments: dict, event_created: dict) -> dict[int, int]:
    """Return {audit row id: event id} for rows of deleted assignments told apart by their removal entry.

    Keyed by audit row rather than assignment id, because a reused id can stand for
    several deleted assignments on different events.
    """
    by_id = {row.id: row for row in rows}
    deleted_event: dict[int, int] = {}
    last_create: dict[str, object] = {}
    # Walk sign-ups and sign-offs per (user, event name) in time order. A sign-off
    # closes the one open sign-up it can belong to; when several fit it stays unpaired.
    open_sign_ups: dict[tuple[str, str], list] = defaultdict(list)
    open_conditions: set[tuple[str, str, str]] = set()
    for row in rows:
        if row.entity_type == "Assignment" and row.action_type == "create":
            last_create[row.entity_id] = row
            if _existing(assignments, row.entity_id, row.timestamp) is None and (
                match := _SIGN_UP.match(row.summary)
            ):
                open_sign_ups[_user_and_event(match)].append(row)
        elif row.entity_type == "Event" and row.action_type == "create":
            if match := _CONDITION_SIGN_UP.match(row.summary):
                open_conditions.add((*_user_and_event(match), row.entity_id))
        elif row.entity_type == "Assignment" and row.action_type == "delete":
            # Logged on the assignment, so it closes that assignment's sign-up.
            for rows_open in open_sign_ups.values():
                rows_open[:] = [s for s in rows_open if s.entity_id != row.entity_id]
            # An automatic removal is logged right after its spot edit on the event.
            sibling = by_id.get(row.id - 1)
            if (
                "automaticky odhlášen" in row.summary
                and sibling is not None
                and sibling.entity_type == "Event"
                and sibling.actor_id == row.actor_id
                and sibling.summary.startswith("Upravena pozice")
            ):
                deleted_event[row.id] = int(sibling.entity_id)
                if (create := last_create.get(row.entity_id)) is not None:
                    deleted_event[create.id] = int(sibling.entity_id)
        elif row.entity_type == "Event" and row.action_type == "delete" and (match := _SIGN_OFF.match(row.summary)):
            key = _user_and_event(match)
            if (*key, row.entity_id) in open_conditions:
                open_conditions.discard((*key, row.entity_id))
                continue
            created = event_created.get(int(row.entity_id))
            if created is None:
                continue
            # Copied and imported events bring assignments without a sign-up entry, so
            # only sign-ups made after this event existed can be the one being closed.
            candidates = [s for s in open_sign_ups[key] if _utc(created) <= _utc(s.timestamp)]
            if len(candidates) == 1:
                open_sign_ups[key].remove(candidates[0])
                deleted_event[candidates[0].id] = int(row.entity_id)
    return deleted_event


def _user_and_event(match: re.Match) -> tuple[str, str]:
    groups = match.groupdict()
    return groups.get("self") or groups["other"], groups["event"]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def downgrade():
    op.drop_index("ix_audit_log_entry_event", table_name="audit_log_entry")
    op.drop_column("audit_log_entry", "event_id")
