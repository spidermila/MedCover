"""Server statistics digest block."""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.digest.base import BaseBlock


class ServerStatsBlock(BaseBlock):
    block_type = "server_stats"
    label = "Servisní statistiky"
    description = (
        "Přehled systémových ukazatelů: počty uživatelů a akcí, velikost databáze, stav scheduleru a e-mailové fronty."
    )
    template = "email/digest_blocks/server_stats.html"
    default_config: dict[str, Any] = {
        "title": "Servisní statistiky",
        "show_user_count": True,
        "show_event_count": True,
        "show_db_size": True,
        "show_table_sizes": True,
        "max_table_rows": 5,
        "show_scheduler_heartbeat": True,
        "show_outbox_pending": True,
        "show_outbox_peak": True,
        "peak_hours": 24,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa  # pylint: disable=import-outside-toplevel

        from app.models.event import Event  # pylint: disable=import-outside-toplevel
        from app.models.outbox import OutboxEmail  # pylint: disable=import-outside-toplevel
        from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel
        from app.models.user import UserAccount  # pylint: disable=import-outside-toplevel

        now = datetime.now(timezone.utc)
        data: dict[str, Any] = {"title": config.get("title", self.default_config["title"])}

        if config.get("show_user_count", True):
            data["user_count"] = db_session.scalar(sa.select(sa.func.count()).select_from(UserAccount))

        if config.get("show_event_count", True):
            data["event_count"] = db_session.scalar(sa.select(sa.func.count()).select_from(Event))

        if config.get("show_db_size", True):
            try:
                row = db_session.execute(
                    sa.text("SELECT CAST(SUM(size) * 8.0 / 1024 AS DECIMAL(10,2)) FROM sys.database_files")
                ).fetchone()
                data["db_size"] = f"{row[0]} MB" if row and row[0] else "N/A"
            except Exception:  # noqa: BLE001
                data["db_size"] = "N/A"

        if config.get("show_table_sizes", True):
            try:
                max_table_rows = int(config.get("max_table_rows", 5))
                rows = db_session.execute(
                    sa.text("""
                    SELECT TOP(:limit) t.name,
                           CAST(SUM(a.total_pages) * 8.0 / 1024 AS DECIMAL(10,2)) AS size_mb
                    FROM sys.tables t
                    JOIN sys.indexes i ON t.object_id = i.object_id
                    JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id
                    JOIN sys.allocation_units a ON p.partition_id = a.container_id
                    GROUP BY t.name
                    ORDER BY SUM(a.total_pages) DESC
                    """),
                    {"limit": max_table_rows},
                ).fetchall()
                data["table_sizes"] = [(r[0], f"{r[1]} MB") for r in rows]
            except Exception:  # noqa: BLE001
                data["table_sizes"] = []

        if config.get("show_scheduler_heartbeat", True):
            settings = get_settings()
            if settings.scheduler_last_seen:
                age = now - settings.scheduler_last_seen
                data["scheduler_age_seconds"] = int(age.total_seconds())
                data["scheduler_ok"] = age.total_seconds() < 300
            else:
                data["scheduler_age_seconds"] = None
                data["scheduler_ok"] = False

        if config.get("show_outbox_pending", True):
            data["outbox_pending"] = db_session.scalar(
                sa.select(sa.func.count()).select_from(OutboxEmail).where(OutboxEmail.status == "pending")
            )

        if config.get("show_outbox_peak", True):
            # Count total emails enqueued in the window — accurate regardless
            # of how quickly the drain loop processes them.  The old approach
            # (max of periodic snapshots) always returned 0 because emails are
            # drained every 6 s, long before the 15-min snapshot window.
            peak_hours = int(config.get("peak_hours", 24))
            since = now - timedelta(hours=peak_hours)
            total = db_session.scalar(
                sa.select(sa.func.count()).select_from(OutboxEmail).where(OutboxEmail.created_at >= since)
            )
            data["outbox_peak"] = int(total) if total is not None else 0
            data["peak_hours"] = peak_hours

        return data
