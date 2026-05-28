"""New users digest block."""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from app.digest.base import BaseBlock


class NewUsersBlock(BaseBlock):
    block_type = "new_users"
    label = "Noví uživatelé"
    description = "Přehled nově vytvořených nebo aktivovaných uživatelských účtů za zvolené časové okno."
    template = "email/digest_blocks/new_users.html"
    default_config: dict[str, Any] = {
        "title": "Noví uživatelé",
        "hours": 24,
        "show_pending_only": False,
        "max_rows": 20,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa
        from app.models.user import UserAccount

        hours = int(config.get("hours", 24))
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        max_rows = int(config.get("max_rows", 20))
        pending_only = bool(config.get("show_pending_only", False))

        q = sa.select(UserAccount).where(UserAccount.created_at >= since)
        if pending_only:
            q = q.where(UserAccount.is_active.is_(False))
        q = q.order_by(UserAccount.created_at.desc()).limit(max_rows)

        users = db_session.scalars(q).all()
        return {
            "title": config.get("title", self.default_config["title"]),
            "users": users,
            "hours": hours,
            "truncated": len(users) == max_rows,
        }
