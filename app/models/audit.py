from datetime import datetime, timezone

from sqlalchemy.engine.default import DefaultExecutionContext

from app.extensions import db


def _own_event_id(context: DefaultExecutionContext) -> int | None:
    params = context.get_current_parameters()
    return int(params["entity_id"]) if params["entity_type"] == "Event" else None


class AuditLogEntry(db.Model):  # type: ignore[misc]
    __tablename__ = "audit_log_entry"
    __table_args__ = (db.Index("ix_audit_log_entry_event", "event_id", "timestamp"),)

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
    # Event whose change log shows the entry: its own id for Event rows, else set by
    # the caller. No FK, so the history outlives a deleted event.
    event_id = db.Column(db.Integer, nullable=True, default=_own_event_id)
    summary = db.Column(db.Text, nullable=False)
    changes_json = db.Column(db.JSON, nullable=True)  # {field: [before, after]}

    actor = db.relationship("UserAccount", foreign_keys=[actor_id], back_populates="audit_entries")

    def __repr__(self) -> str:
        return f"<AuditLogEntry {self.action_type} {self.entity_type}:{self.entity_id}>"
