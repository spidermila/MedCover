"""User activity digest block — audit log entries per user."""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from app.digest.base import BaseBlock


class UserActivityBlock(BaseBlock):
    block_type = "user_activity"
    label = "Aktivita uživatelů"
    description = "Počet záznamů v auditním logu na uživatele za zvolené časové okno. Seřazeno od nejaktivnějšího."
    template = "email/digest_blocks/user_activity.html"
    default_config: dict[str, Any] = {
        "title": "Aktivita uživatelů",
        "hours": 24,
        "max_rows": 10,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa
        from app.models.audit import AuditLogEntry
        from app.models.user import UserAccount

        hours = int(config.get("hours", 24))
        max_rows = int(config.get("max_rows", 10))
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        rows = db_session.execute(
            sa.select(AuditLogEntry.actor_id, sa.func.count().label("cnt"))
            .where(
                AuditLogEntry.actor_id.is_not(None),
                AuditLogEntry.timestamp >= since,
            )
            .group_by(AuditLogEntry.actor_id)
            .order_by(sa.desc("cnt"))
            .limit(max_rows)
        ).all()

        # Bulk-load names in one query
        actor_ids = [r.actor_id for r in rows]
        users_by_id: dict[Any, str] = {}
        if actor_ids:
            users = db_session.scalars(
                sa.select(UserAccount).where(UserAccount.id.in_(actor_ids))
            ).all()
            users_by_id = {u.id: u.name for u in users}

        entries = [
            {"name": users_by_id.get(r.actor_id, str(r.actor_id)), "count": r.cnt}
            for r in rows
        ]

        return {
            "title": config.get("title", self.default_config["title"]),
            "entries": entries,
            "hours": hours,
            "truncated": len(rows) == max_rows,
        }
