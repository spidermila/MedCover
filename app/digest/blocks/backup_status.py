"""Backup status digest block."""

from datetime import datetime, timezone
from typing import Any

from app.backup import list_backups
from app.digest.base import BaseBlock
from app.models.settings import get_settings


class BackupStatusBlock(BaseBlock):
    block_type = "backup_status"
    label = "Stav zálohování"
    description = (
        "Přehled posledních záloh: počet souborů, celková velikost,"
        " datum poslední zálohy a stav plánovaného zálohování."
    )
    template = "email/digest_blocks/backup_status.html"
    default_config: dict[str, Any] = {
        "title": "Stav zálohování",
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()

        data: dict[str, Any] = {
            "title": config.get("title", self.default_config["title"]),
            "backup_schedule_enabled": settings.backup_schedule_enabled,
            "backup_schedule_hour": settings.backup_schedule_hour,
            "backup_schedule_minute": settings.backup_schedule_minute,
            "backup_schedule_tz": settings.timezone,
            "backup_keep_count": settings.backup_keep_count,
        }

        # A storage outage is exactly what this block must surface, so report
        # it rather than letting the renderer drop the block.
        try:
            backups = list_backups()
        except Exception as exc:  # noqa: BLE001
            data["storage_error"] = str(exc)
            backups = []
        data["backup_count"] = len(backups)
        data["total_size_bytes"] = sum(b["size_bytes"] for b in backups)
        data["last_backup_at"] = backups[0]["created_at"] if backups else None

        if data["last_backup_at"]:
            age = datetime.now(timezone.utc) - data["last_backup_at"]
            data["last_backup_age_hours"] = int(age.total_seconds() / 3600)
            # Warn if last backup is more than 25 hours old and schedule is enabled
            data["backup_overdue"] = settings.backup_schedule_enabled and data["last_backup_age_hours"] > 25
        else:
            data["last_backup_age_hours"] = None
            data["backup_overdue"] = settings.backup_schedule_enabled

        return data
