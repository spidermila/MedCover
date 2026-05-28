"""User feedback digest block."""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from app.digest.base import BaseBlock


class FeedbackSummaryBlock(BaseBlock):
    block_type = "feedback_summary"
    label = "Zpětná vazba"
    description = "Výpis zpráv zpětné vazby odeslaných uživateli za zvolené časové okno."
    template = "email/digest_blocks/feedback_summary.html"
    default_config: dict[str, Any] = {
        "title": "Zpětná vazba",
        "hours": 24,
        "max_rows": 20,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa
        from app.models.feedback import UserFeedback

        hours = int(config.get("hours", 24))
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        max_rows = int(config.get("max_rows", 20))

        q = (
            sa.select(UserFeedback)
            .where(UserFeedback.submitted_at >= since)
            .order_by(UserFeedback.submitted_at.desc())
            .limit(max_rows)
        )
        items = db_session.scalars(q).all()
        return {
            "title": config.get("title", self.default_config["title"]),
            "items": items,
            "hours": hours,
            "truncated": len(items) == max_rows,
        }
