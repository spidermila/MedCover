from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.extensions import db

if TYPE_CHECKING:
    from app.models.event import Event  # noqa: F401
    from app.models.user import UserAccount  # noqa: F401


class EquipmentType(db.Model):  # type: ignore[misc]
    __tablename__ = "equipment_type"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    version = db.Column(db.Integer, default=1, nullable=False)
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

    items = db.relationship("EquipmentItem", back_populates="equipment_type", lazy="selectin")
    event_plans = db.relationship("EventEquipmentPlan", back_populates="equipment_type", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<EquipmentType {self.name}>"


class EquipmentItem(db.Model):  # type: ignore[misc]
    __tablename__ = "equipment_item"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    type_id = db.Column(db.Integer, db.ForeignKey("equipment_type.id"), nullable=False)
    serial_number = db.Column(db.String(100), nullable=True)
    home_location = db.Column(db.String(255), nullable=True)
    issued_to_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    unavailability_reason = db.Column(db.Text, nullable=True)
    unavailability_since = db.Column(db.DateTime(timezone=True), nullable=True)
    unavailability_until = db.Column(db.DateTime(timezone=True), nullable=True)
    version = db.Column(db.Integer, default=1, nullable=False)
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

    equipment_type = db.relationship("EquipmentType", back_populates="items", lazy="selectin")
    issued_to = db.relationship("UserAccount", foreign_keys=[issued_to_id], back_populates="issued_equipment")

    @property
    def is_available(self) -> bool:
        """True when no active maintenance window covers the current moment."""
        now = datetime.now(timezone.utc)

        def _utc(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        if self.unavailability_since is None:
            return True
        if _utc(self.unavailability_since) > now:
            return True  # maintenance hasn't started yet
        if self.unavailability_until is not None and _utc(self.unavailability_until) <= now:
            return True  # maintenance window has ended
        return False

    def __repr__(self) -> str:
        return f"<EquipmentItem {self.name}>"


class EventEquipmentPlan(db.Model):  # type: ignore[misc]
    __tablename__ = "event_equipment_plan"

    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), primary_key=True, nullable=False)
    equipment_type_id = db.Column(db.Integer, db.ForeignKey("equipment_type.id"), primary_key=True, nullable=False)
    quantity_required = db.Column(db.Integer, nullable=False, default=1)

    event = db.relationship("Event", back_populates="equipment_plans")
    equipment_type = db.relationship("EquipmentType", back_populates="event_plans", lazy="selectin")

    def __repr__(self) -> str:
        return f"<EventEquipmentPlan event={self.event_id} type={self.equipment_type_id}>"


class EventTemplateEquipmentPlan(db.Model):  # type: ignore[misc]
    __tablename__ = "event_template_equipment_plan"

    template_id = db.Column(db.Integer, db.ForeignKey("event_template.id"), primary_key=True, nullable=False)
    equipment_type_id = db.Column(db.Integer, db.ForeignKey("equipment_type.id"), primary_key=True, nullable=False)
    quantity_required = db.Column(db.Integer, nullable=False, default=1)

    template = db.relationship("EventTemplate", back_populates="equipment_plans")
    equipment_type = db.relationship("EquipmentType", lazy="selectin")

    def __repr__(self) -> str:
        return f"<EventTemplateEquipmentPlan template={self.template_id} type={self.equipment_type_id}>"
