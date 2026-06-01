from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.extensions import db


class UserFeedback(db.Model):  # type: ignore[misc]
    __tablename__ = "user_feedback"

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    user_id = db.Column(
        db.Uuid,
        db.ForeignKey("user_account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message = db.Column(db.Text, nullable=False)
    page_url = db.Column(db.String(2048), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    screen_info = db.Column(db.String(255), nullable=True)
    app_version = db.Column(db.String(64), nullable=True)  # APP_VERSION at submission time
    submitted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("UserAccount", foreign_keys=[user_id], back_populates="feedback_entries", lazy="joined")
