from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped

from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import UserAccount


class Assignment(db.Model):  # type: ignore[misc]
    __tablename__ = "assignment"

    id = db.Column(db.Integer, primary_key=True)
    spot_id = db.Column(db.Integer, db.ForeignKey("event_spot.id"), unique=True, nullable=False)
    user_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=False)
    assigned_by_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    assigned_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Tracks whether the debriefing-invitation email has been sent for this assignment.
    debriefing_email_sent = db.Column(db.Boolean, default=False, nullable=False)

    spot = db.relationship("EventSpot", back_populates="assignment")
    user: Mapped[UserAccount] = db.relationship("UserAccount", foreign_keys=[user_id], lazy="selectin")
    assigned_by: Mapped[UserAccount | None] = db.relationship("UserAccount", foreign_keys=[assigned_by_id])
    debriefing: Mapped[DebriefingRecord | None] = db.relationship(
        "DebriefingRecord", back_populates="assignment", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Assignment spot={self.spot_id} user={self.user_id}>"


class DebriefingRecord(db.Model):  # type: ignore[misc]
    """
    Confidential per-participant post-event feedback.
    Only users with debriefing.view_all (Debriefing Manager) may read these records.
    Submission is final — no editing after submit.
    """
    __tablename__ = "debriefing_record"

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), unique=True, nullable=False)
    submitted_by_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=False)

    # ── Confidential section (all participants) ───────────────────────────────
    # Overall grade: 1 = best, 5 = worst
    grade = db.Column(db.Integer, nullable=False)
    feedback_event = db.Column(db.Text, nullable=True)  # overall event evaluation
    feedback_customer = db.Column(db.Text, nullable=True)  # objednatel/organizátor evaluation
    feedback_colleagues = db.Column(db.Text, nullable=True)  # colleagues evaluation

    submitted_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    assignment = db.relationship("Assignment", back_populates="debriefing")
    submitted_by = db.relationship("UserAccount", foreign_keys=[submitted_by_id])

    def __repr__(self) -> str:
        return f"<DebriefingRecord assignment={self.assignment_id} grade={self.grade}>"
