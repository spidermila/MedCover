"""Add color column to event and migrate existing color tags from description.

Revision ID: e1f2a3b4c5d6
Revises: a1f2e3d4c5b6
Create Date: 2026-06-09

"""

import re

import sqlalchemy as sa
from alembic import op

revision = 'e1f2a3b4c5d6'
down_revision = 'a1f2e3d4c5b6'
branch_labels = None
depends_on = None

_TM_COLOR_RE = re.compile(r'\[color:(#[0-9A-Fa-f]{6})\]', re.IGNORECASE)


def upgrade() -> None:
    op.add_column('event', sa.Column('color', sa.String(50), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, description FROM event WHERE description LIKE '%[color:%'")
    ).fetchall()

    for row in rows:
        event_id, description = row[0], row[1]
        if not description:
            continue
        m = _TM_COLOR_RE.search(description)
        if not m:
            continue
        color = m.group(1).upper()
        clean_description = _TM_COLOR_RE.sub('', description).strip() or None
        conn.execute(
            sa.text(
                "UPDATE event SET color = :color, description = :description WHERE id = :id"
            ),
            {'color': color, 'description': clean_description, 'id': event_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, description, color FROM event WHERE color IS NOT NULL")
    ).fetchall()

    for row in rows:
        event_id, description, color = row[0], row[1], row[2]
        tag = f'[color:{color.upper()}]'
        if description:
            new_description = f'{description} {tag}'
        else:
            new_description = tag
        conn.execute(
            sa.text("UPDATE event SET description = :description WHERE id = :id"),
            {'description': new_description, 'id': event_id},
        )

    op.drop_column('event', 'color')
