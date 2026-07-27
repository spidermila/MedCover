from datetime import datetime, timezone

from app.extensions import db


class OutboxEmail(db.Model):  # type: ignore[misc]
    """Persistent email outbox — all outgoing emails are queued here and sent
    by the scheduler at a throttled rate (one per MAIL_QUEUE_INTERVAL_SECONDS)."""

    __tablename__ = "outbox_email"

    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    # When set, the email is sent as HTML (html=html_body) with body as plain-text fallback.
    html_body = db.Column(db.Text, nullable=True)
    # Identifies which send_* function enqueued this email — e.g. "assignment_confirmed".
    notification_type = db.Column(db.String(64), nullable=True, index=True)
    # App instance that enqueued this email — from INSTANCE_ID env var (e.g. "dev", "prod").
    # Null for emails enqueued before this column was added.
    instance_name = db.Column(db.String(64), nullable=True, index=True)

    # ── Notification batching (issue #268) ────────────────────────────────
    # Populated by the deferred enqueue path; NULL on legacy rows
    # and on non-event / immediate notifications (invite, reset, activation,
    # admin digest). FK ondelete=SET NULL is defensive — entities are archived
    # rather than hard-deleted in practice.
    user_id = db.Column(
        db.Uuid,
        db.ForeignKey("user_account.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("event.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Symbolic tag for the kind of change carried by change_value.
    # Examples: "field_edit", "assignment", "unfilled_reminder".
    change_type = db.Column(db.String(64), nullable=True)
    # JSON-serialised payload. MSSQL has no native JSON type — we store text
    # and use json.dumps/json.loads at the application boundary.
    change_value = db.Column(db.Text, nullable=True)
    # NULL means "send immediately" (matches legacy behaviour). When set, the
    # drain skips the row until now() >= send_after.
    send_after = db.Column(db.DateTime(timezone=True), nullable=True)

    # 'pending' → being picked up by scheduler
    # 'sent'    → successfully delivered to SMTP relay
    # 'failed'  → retry_count reached MAX_RETRIES; given up
    # 'skipped' → suppressed by dev_email_block; recipient not in allowlist
    status = db.Column(db.String(16), nullable=False, default="pending", server_default="pending", index=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    last_error = db.Column(db.Text, nullable=True)

    MAX_RETRIES: int = 3

    __table_args__ = (db.Index("ix_outbox_email_status_send_after", "status", "send_after"),)

    def __repr__(self) -> str:
        return f"<OutboxEmail id={self.id} to={self.to_email!r} status={self.status}>"
