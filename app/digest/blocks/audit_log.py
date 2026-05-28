"""Audit log digest block."""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from app.digest.base import BaseBlock

_ALL_ENTITY_TYPES = [
    "Event", "EventSpot", "Assignment", "UserAccount", "MasterEvent",
    "EventTemplate", "EquipmentItem", "EquipmentType", "AppSettings",
    "RegistrationInvite", "OutboxEmail",
]
_ALL_ACTION_TYPES = ["create", "edit", "delete", "status_change", "email_failed"]


class AuditLogBlock(BaseBlock):
    block_type = "audit_log"
    label = "Audit log"
    description = "Výpis záznamů z auditního logu za zvolené časové okno s volitelným filtrováním podle typu entity nebo akce."
    template = "email/digest_blocks/audit_log.html"
    default_config: dict[str, Any] = {
        "title": "Audit log",
        "hours": 24,
        "entity_types": [],       # empty = all
        "action_types": [],       # empty = all
        "max_rows": 50,
        "show_actor": True,
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        import sqlalchemy as sa
        from app.models.audit import AuditLogEntry

        hours = int(config.get("hours", 24))
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        max_rows = int(config.get("max_rows", 50))
        entity_filter: list[str] = config.get("entity_types") or []
        action_filter: list[str] = config.get("action_types") or []

        q = sa.select(AuditLogEntry).where(AuditLogEntry.timestamp >= since)
        if entity_filter:
            q = q.where(AuditLogEntry.entity_type.in_(entity_filter))
        if action_filter:
            q = q.where(AuditLogEntry.action_type.in_(action_filter))
        q = q.order_by(AuditLogEntry.timestamp.desc()).limit(max_rows)

        entries = db_session.scalars(q).all()
        return {
            "title": config.get("title", self.default_config["title"]),
            "entries": entries,
            "hours": hours,
            "show_actor": config.get("show_actor", True),
            "truncated": len(entries) == max_rows,
        }
