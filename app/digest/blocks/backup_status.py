"""Backup status digest block."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.digest.base import BaseBlock


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
        from app.backup import list_backups  # pylint: disable=import-outside-toplevel
        from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

        settings = get_settings()
        backup_dir = Path(settings.backup_dir)

        data: dict[str, Any] = {
            "title": config.get("title", self.default_config["title"]),
            "backup_schedule_enabled": settings.backup_schedule_enabled,
            "backup_schedule_hour": settings.backup_schedule_hour,
            "backup_keep_count": settings.backup_keep_count,
            "backup_dir": str(backup_dir),
        }

        backups = list_backups(backup_dir)
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
