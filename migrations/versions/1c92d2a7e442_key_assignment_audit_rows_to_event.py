"""Re-key assignment and debriefing audit rows to their event.

Sign-ups and debriefing submissions used to be logged against the assignment or
debriefing record id, which the event detail change log cannot find. Rows whose
assignment or record still exists are resolved through it; rows whose assignment
was deleted fall back to the event name quoted in the summary, but only when that
name is unique and the event already existed at the time of the entry. Anything
else stays as it was. Audit-log digest blocks filtering on "Assignment" are
switched to "Event" so they keep receiving sign-ups. Also indexes the audit log
by entity for the per-event change log query.

Revision ID: 1c92d2a7e442
Revises: f7a8b9c0d1e2
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "1c92d2a7e442"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

_EVENT_NAME = re.compile(r"(?:na akci|pro akci|z akce) '(.+)'$")


def upgrade():
    op.create_index("ix_audit_log_entry_entity", "audit_log_entry", ["entity_type", "entity_id", "timestamp"])

    conn = op.get_bind()
    assignment_event = dict(conn.execute(sa.text("SELECT id, event_id FROM assignment")).all())
    record_event = dict(
        conn.execute(
            sa.text("SELECT d.id, a.event_id FROM debriefing_record d JOIN assignment a ON a.id = d.assignment_id")
        ).all()
    )
    events = conn.execute(sa.text("SELECT id, name, created_at FROM event")).all()
    name_counts = Counter(name for _, name, _ in events)
    event_by_unique_name = {name: (eid, created) for eid, name, created in events if name_counts[name] == 1}

    rows = conn.execute(
        sa.text(
            "SELECT id, entity_type, entity_id, summary, timestamp FROM audit_log_entry"
            " WHERE entity_type IN ('Assignment', 'DebriefingRecord')"
        )
    ).all()
    updates = []
    for row_id, entity_type, entity_id, summary, timestamp in rows:
        lookup = assignment_event if entity_type == "Assignment" else record_event
        event_id = lookup.get(int(entity_id)) if entity_id.isdigit() else None
        if event_id is None and (match := _EVENT_NAME.search(summary)):
            candidate, created = event_by_unique_name.get(match.group(1), (None, None))
            if candidate is not None and _utc(created) <= _utc(timestamp):
                event_id = candidate
        if event_id is not None:
            updates.append({"eid": str(event_id), "id": row_id})
    if updates:
        conn.execute(
            sa.text("UPDATE audit_log_entry SET entity_type = 'Event', entity_id = :eid WHERE id = :id"), updates
        )

    blocks = conn.execute(sa.text("SELECT id, config_json FROM digest_block WHERE block_type = 'audit_log'")).all()
    for block_id, raw in blocks:
        config = json.loads(raw) if isinstance(raw, str) else raw
        types = config.get("entity_types") or []
        if "Assignment" in types:
            config["entity_types"] = list(dict.fromkeys("Event" if t == "Assignment" else t for t in types))
            conn.execute(
                sa.text("UPDATE digest_block SET config_json = :cfg WHERE id = :id"),
                {"cfg": json.dumps(config, ensure_ascii=False), "id": block_id},
            )


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def downgrade():
    # The original assignment / record ids are not kept; the re-keyed rows stay on the event.
    op.drop_index("ix_audit_log_entry_entity", table_name="audit_log_entry")
