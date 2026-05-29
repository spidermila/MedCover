from __future__ import annotations

import enum
import secrets
import uuid
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from flask_login import UserMixin
from sqlalchemy.orm import Mapped
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash

from app.extensions import db

if TYPE_CHECKING:
    from app.models.assignment import Assignment
    from app.models.assignment import DebriefingRecord
    from app.models.audit import AuditLogEntry
    from app.models.equipment import EquipmentItem
    from app.models.event import Event
    from app.models.feedback import UserFeedback
    from app.models.invite import RegistrationInvite
    from app.models.master_event import MasterEvent
    from app.models.qualification import Qualification
    from app.models.role import Role


class CalendarView(str, enum.Enum):
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    LIST = "list"


# Many-to-many: UserAccount ↔ Role
user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Uuid, db.ForeignKey("user_account.id"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("role.id"), primary_key=True),
)


class UserAccount(UserMixin, db.Model):  # type: ignore[misc]
    __tablename__ = "user_account"

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    is_archived = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    preferred_calendar_view = db.Column(
        db.Enum(CalendarView, name="calendar_view_enum"),
        default=CalendarView.LIST,
        nullable=False,
    )
    dashboard_horizon_days = db.Column(db.Integer, default=30, nullable=False, server_default="30")
    dark_mode = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    # Optimistic locking — increment on every write; catch StaleDataError → HTTP 409
    version = db.Column(db.Integer, default=1, nullable=False)
    # Single-use password reset: nonce is set when a reset link is issued and
    # cleared after successful password change. Old links become invalid immediately.
    password_reset_nonce = db.Column(db.String(64), nullable=True)
    # iCal subscription token — 64-char hex, public but unguessable.
    # Regenerating it immediately invalidates any existing calendar subscriptions.
    ical_token = db.Column(db.String(64), nullable=True, unique=True, index=True)
    # Brute-force protection: track consecutive failed logins per account.
    # After LOGIN_MAX_ATTEMPTS failures the account is locked for LOGIN_LOCKOUT_MINUTES.
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False, server_default="0")
    login_locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    roles: Mapped[list[Role]] = db.relationship(
        "Role",
        secondary=user_roles,
        back_populates="users",
        lazy="selectin",
    )

    qualifications: Mapped[list[Qualification]] = db.relationship(
        "Qualification",
        secondary="user_qualifications",
        back_populates="holders",
        lazy="selectin",
    )

    # ── Inverse relationships (back_populates) ────────────────────────────────
    assignments: Mapped[list[Assignment]] = db.relationship(
        "Assignment", foreign_keys="Assignment.user_id", back_populates="user", lazy="noload",
    )
    assignments_made: Mapped[list[Assignment]] = db.relationship(
        "Assignment", foreign_keys="Assignment.assigned_by_id", back_populates="assigned_by", lazy="noload",
    )
    submitted_debriefings: Mapped[list[DebriefingRecord]] = db.relationship(
        "DebriefingRecord", foreign_keys="DebriefingRecord.submitted_by_id", back_populates="submitted_by", lazy="noload",
    )
    audit_entries: Mapped[list[AuditLogEntry]] = db.relationship(
        "AuditLogEntry", foreign_keys="AuditLogEntry.actor_id", back_populates="actor", lazy="noload",
    )
    issued_equipment: Mapped[list[EquipmentItem]] = db.relationship(
        "EquipmentItem", foreign_keys="EquipmentItem.issued_to_id", back_populates="issued_to", lazy="noload",
    )
    feedback_entries: Mapped[list[UserFeedback]] = db.relationship(
        "UserFeedback", foreign_keys="UserFeedback.user_id", back_populates="user", lazy="noload",
    )
    created_invites: Mapped[list[RegistrationInvite]] = db.relationship(
        "RegistrationInvite", foreign_keys="RegistrationInvite.created_by_id", back_populates="created_by", lazy="noload",
    )
    rp_events: Mapped[list[Event]] = db.relationship(
        "Event", foreign_keys="Event.responsible_person_id", back_populates="responsible_person", lazy="noload",
    )
    created_events: Mapped[list[Event]] = db.relationship(
        "Event", foreign_keys="Event.created_by_id", back_populates="created_by", lazy="noload",
    )
    coordinated_master_events: Mapped[list[MasterEvent]] = db.relationship(
        "MasterEvent", foreign_keys="MasterEvent.coordinator_id", back_populates="coordinator", lazy="noload",
    )

    def regenerate_ical_token(self) -> str:
        """Generate a new iCal subscription token, invalidating the previous one."""
        self.ical_token = secrets.token_hex(32)
        return self.ical_token

    def get_or_create_ical_token(self) -> str:
        """Return existing token or lazily create one on first call."""
        if not self.ical_token:
            self.regenerate_ical_token()
        return self.ical_token

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def has_permission(self, code: str) -> bool:
        return any(p.code == code for role in self.roles for p in role.permissions)

    def has_any_permission(self, *codes: str) -> bool:
        owned = {p.code for role in self.roles for p in role.permissions}
        return bool(owned & set(codes))

    def is_rp_eligible(self) -> bool:
        """Return True if the user holds any qualification with can_be_rp=True."""
        return any(q.can_be_rp for q in self.qualifications)

    # Flask-Login: use str(uuid) as session token
    def get_id(self) -> str:
        return str(self.id)

    def __repr__(self) -> str:
        return f"<UserAccount {self.email}>"
