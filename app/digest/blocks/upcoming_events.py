"""Upcoming events digest block."""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.digest.base import BaseBlock


class UpcomingEventsBlock(BaseBlock):
    block_type = "upcoming_events"
    label = "Nadcházející akce"
    description = "Seznam akcí začínajících v nejbližších N dnech s počtem neobsazených míst."
    template = "email/digest_blocks/upcoming_events.html"
    default_config: dict[str, Any] = {
        "title": "Nadcházející akce",
        "days_ahead": 7,
        "show_unfilled_only": False,
        "max_rows": 15,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa  # pylint: disable=import-outside-toplevel

        from app.models.event import Event, EventStatus  # pylint: disable=import-outside-toplevel

        now = datetime.now(timezone.utc)
        days_ahead = int(config.get("days_ahead", 7))
        until = now + timedelta(days=days_ahead)
        max_rows = int(config.get("max_rows", 15))
        unfilled_only = bool(config.get("show_unfilled_only", False))

        q = (
            sa.select(Event)
            .where(
                Event.start_datetime >= now,
                Event.start_datetime <= until,
                Event.status.in_(
                    [
                        EventStatus.PUBLISHED,
                        EventStatus.ASSIGNMENTS_OPEN,
                        EventStatus.ASSIGNMENTS_CLOSED,
                    ]
                ),
                Event.archived == sa.false(),
            )
            .order_by(Event.start_datetime.asc())
            .limit(max_rows)
        )
        events = db_session.scalars(q).all()

        rows = []
        for ev in events:
            unfilled_count = len(ev.unfilled_spots)
            if unfilled_only and unfilled_count == 0:
                continue
            rows.append(
                {
                    "event": ev,
                    "unfilled_count": unfilled_count,
                    "total_spots": len(ev.spots),
                }
            )

        return {
            "title": config.get("title", self.default_config["title"]),
            "rows": rows,
            "days_ahead": days_ahead,
        }
