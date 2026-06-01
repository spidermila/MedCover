from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db


class AuditLogEntry(db.Model):  # type: ignore[misc]
    __tablename__ = "audit_log_entry"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    actor_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    action_type = db.Column(db.String(32), nullable=False)  # create | edit | delete | status_change
    entity_type = db.Column(db.String(64), nullable=False)  # Event | UserAccount | Assignment | …
    entity_id = db.Column(db.String(64), nullable=False)  # PK as string
    summary = db.Column(db.Text, nullable=False)
    changes_json = db.Column(db.JSON, nullable=True)  # {field: [before, after]}

    actor = db.relationship("UserAccount", foreign_keys=[actor_id], back_populates="audit_entries")

    def __repr__(self) -> str:
        return f"<AuditLogEntry {self.action_type} {self.entity_type}:{self.entity_id}>"
