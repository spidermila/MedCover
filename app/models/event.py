import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped

from app.extensions import db

if TYPE_CHECKING:
    from app.models.assignment import Assignment
    from app.models.qualification import Qualification
    from app.models.user import UserAccount


class ReminderScheduleMixin:
    """Shared ``reminder_hours()`` logic for models that carry a ``reminder_schedule`` column."""

    reminder_schedule: str | None  # declared on concrete model

    def reminder_hours(self) -> list[int]:
        """Parse ``reminder_schedule`` (comma-separated ints) into a list of hour offsets."""
        if not self.reminder_schedule:
            return [24]
        return [int(h) for h in self.reminder_schedule.split(",") if h.strip().isdigit()]


class EventStatus(str, enum.Enum):
    DRAFT = "Koncept"
    PUBLISHED = "Zveřejněná"
    ASSIGNMENTS_OPEN = "Přihlášky otevřeny"
    ASSIGNMENTS_CLOSED = "Přihlášky uzavřeny"
    COMPLETED = "Dokončena"
    CANCELLED = "Zrušena"


class EventType(str, enum.Enum):
    MEDICAL_COVER = "Zdravotní dozor"
    TRAINING = "Školení"
    PRESENTATION = "Prezentační akce"


# M2M: EventSpot ↔ Qualification (required qualifications for a spot)
spot_qualifications = db.Table(
    "spot_qualifications",
    db.Column("spot_id", db.Integer, db.ForeignKey("event_spot.id"), primary_key=True),
    db.Column("qualification_id", db.Integer, db.ForeignKey("qualification.id"), primary_key=True),
)

# M2M: EventSpotTemplate ↔ Qualification
spot_template_qualifications = db.Table(
    "spot_template_qualifications",
    db.Column("spot_template_id", db.Integer, db.ForeignKey("event_spot_template.id"), primary_key=True),
    db.Column("qualification_id", db.Integer, db.ForeignKey("qualification.id"), primary_key=True),
)


class EventTemplate(ReminderScheduleMixin, db.Model):  # type: ignore[misc]
    __tablename__ = "event_template"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    event_type = db.Column(
        db.Enum(EventType, name="event_type_enum"),
        default=EventType.MEDICAL_COVER,
        nullable=False,
    )
    # Reminder schedule: list of hours-before-start values, stored as comma-separated ints
    # e.g. "24,48" means send reminders 24h and 48h before start
    reminder_schedule = db.Column(db.String(255), nullable=True, default="24")
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
    # Optimistic locking — increment on every write; catch StaleDataError → HTTP 409
    version = db.Column(db.Integer, default=1, nullable=False)

    spot_templates = db.relationship(
        "EventSpotTemplate",
        back_populates="template",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    equipment_plans = db.relationship(
        "EventTemplateEquipmentPlan",
        back_populates="template",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<EventTemplate {self.name}>"


class EventSpotTemplate(db.Model):  # type: ignore[misc]
    __tablename__ = "event_spot_template"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("event_template.id"), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    is_optional = db.Column(db.Boolean, default=False, nullable=False)

    template = db.relationship("EventTemplate", back_populates="spot_templates")
    required_qualifications: Mapped[list[Qualification]] = db.relationship(
        "Qualification", secondary=spot_template_qualifications, lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<EventSpotTemplate {self.id} for template {self.template_id}>"


class Event(ReminderScheduleMixin, db.Model):  # type: ignore[misc]
    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    master_event_id = db.Column(db.Integer, db.ForeignKey("master_event.id"), nullable=False)
    event_type = db.Column(
        db.Enum(EventType, name="event_type_enum"),
        default=EventType.MEDICAL_COVER,
        nullable=False,
    )
    status = db.Column(
        db.Enum(EventStatus, name="event_status_enum"),
        default=EventStatus.DRAFT,
        nullable=False,
    )
    archived = db.Column(db.Boolean, default=False, nullable=False)
    start_datetime = db.Column(db.DateTime(timezone=True), nullable=False)
    end_datetime = db.Column(db.DateTime(timezone=True), nullable=False)
    # When null: assignments open immediately on publish; when set: auto-open at this datetime
    assignments_open_datetime = db.Column(db.DateTime(timezone=True), nullable=True)
    address = db.Column(db.String(500), nullable=True)
    contact_person = db.Column(db.String(255), nullable=True)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    description = db.Column(db.Text, nullable=True)
    responsible_person_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    created_by_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    # Reminder schedule inherited from template or set manually (hours before start, comma-separated)
    reminder_schedule = db.Column(db.String(255), nullable=True, default="24")
    # Tracks sent reminders: JSON dict mapping hours-offset str → ISO sent_at timestamp.
    # e.g. {"24": "2026-05-28T17:00:00+00:00"} means the 24h reminder was already sent.
    reminder_sent_json = db.Column(db.JSON, nullable=True, default=dict)
    # ── Post-event actuals (filled during debriefing by responsible person) ───
    actual_start_datetime = db.Column(db.DateTime(timezone=True), nullable=True)
    actual_end_datetime = db.Column(db.DateTime(timezone=True), nullable=True)
    # Plánovaný počet účastníků — only for TRAINING events (set at create/edit time)
    planned_participants_count = db.Column(db.Integer, nullable=True)
    # Post-event count metric — meaning depends on event_type:
    #   MEDICAL_COVER → počet ošetřených pacientů
    #   TRAINING      → skutečný počet účastníků
    #   PRESENTATION  → not used (always None)
    post_event_count = db.Column(db.Integer, nullable=True)
    # Optimistic locking — increment on every write; catch StaleDataError → HTTP 409
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

    master_event = db.relationship("MasterEvent", back_populates="events")
    responsible_person = db.relationship(
        "UserAccount", foreign_keys=[responsible_person_id], back_populates="rp_events"
    )
    created_by = db.relationship("UserAccount", foreign_keys=[created_by_id], back_populates="created_events")
    spots: Mapped[list[EventSpot]] = db.relationship(
        "EventSpot",
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="EventSpot.is_optional, EventSpot.id",
    )
    equipment_plans = db.relationship("EventEquipmentPlan", back_populates="event", cascade="all, delete-orphan")
    equipment_assignments = db.relationship(
        "EventEquipmentAssignment", back_populates="event", cascade="all, delete-orphan"
    )

    # ── Derived staffing status ─────────────────────────────────────────────
    @property
    def total_spots(self) -> int:
        return len(self.spots)

    @property
    def filled_spots(self) -> int:
        return sum(1 for s in self.spots if s.assignment is not None)

    @property
    def mandatory_total_spots(self) -> int:
        return sum(1 for s in self.spots if not s.is_optional)

    @property
    def mandatory_filled_spots(self) -> int:
        return sum(1 for s in self.spots if not s.is_optional and s.assignment is not None)

    @property
    def optional_total_spots(self) -> int:
        return sum(1 for s in self.spots if s.is_optional)

    @property
    def unfilled_spots(self) -> list[EventSpot]:
        """Return mandatory spots that have no assignment."""
        return [s for s in self.spots if not s.is_optional and s.assignment is None]

    @property
    def scheduled_hours(self) -> Decimal:
        """Planned duration in hours derived from start_datetime / end_datetime, rounded to 1 dp."""
        delta = self.end_datetime - self.start_datetime
        return Decimal(str(round(delta.total_seconds() / 3600, 1)))

    @property
    def actual_hours(self) -> Decimal | None:
        """Actual duration in hours derived from RP-submitted actual start/end times, rounded to 1 dp."""
        if self.actual_start_datetime and self.actual_end_datetime:
            delta = self.actual_end_datetime - self.actual_start_datetime
            return Decimal(str(round(delta.total_seconds() / 3600, 1)))
        return None

    @property
    def billable_hours(self) -> Decimal:
        """Hours to use for reporting: actual if debriefed, otherwise scheduled."""
        return self.actual_hours if self.actual_hours is not None else self.scheduled_hours

    @property
    def is_unfilled(self) -> bool:
        """True when at least one mandatory spot has no assignment."""
        return bool(self.unfilled_spots)

    @property
    def staffing_status(self) -> str:
        mandatory_total = self.mandatory_total_spots
        mandatory_filled = self.mandatory_filled_spots
        if mandatory_total == 0:
            return "Žádné pozice"
        if mandatory_filled == 0:
            return "Neobsazeno"
        if mandatory_filled < mandatory_total:
            return "Částečně obsazeno"
        return "Plně obsazeno"

    @property
    def is_centrally_coordinated(self) -> bool:
        """True if this event belongs to a master event with an assigned coordinator."""
        return self.master_event is not None and self.master_event.coordinator_id is not None

    def user_can_manage_assignments(self, user: UserAccount) -> bool:
        """Return True if the user may assign/unassign other users on this event.

        Grants this ability when:
        1. The user holds the ``event.assign_other`` permission (admins/coordinators), OR
        2. ALL of the following:
           a) the user is RP-eligible (holds a qualification with can_be_rp),
           b) the user is currently assigned to a spot on this event,
           c) the event's master event has NO coordinator assigned
              (coordinated master events are managed centrally from SPOT).
        """
        if user.has_permission("event.assign_other"):
            return True
        if not user.is_rp_eligible():
            return False
        # Check: ME has no coordinator (exception rule from issue #255)
        if self.is_centrally_coordinated:
            return False
        # Check: user is assigned to this event
        return any(s.assignment is not None and s.assignment.user_id == user.id for s in self.spots)

    def __repr__(self) -> str:
        return f"<Event {self.id}: {self.name} [{self.status}]>"


class EventSpot(db.Model):  # type: ignore[misc]
    __tablename__ = "event_spot"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    is_optional = db.Column(db.Boolean, default=False, nullable=False)
    # Optimistic locking — used in combination with with_for_update() for assignment
    version = db.Column(db.Integer, default=1, nullable=False)

    event = db.relationship("Event", back_populates="spots")
    required_qualifications: Mapped[list[Qualification]] = db.relationship(
        "Qualification", secondary=spot_qualifications, lazy="selectin"
    )
    assignment: Mapped[Assignment | None] = db.relationship(
        "Assignment",
        back_populates="spot",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def is_eligible(self, user: UserAccount) -> bool:
        """Return True if the user holds qualifications satisfying all spot requirements."""
        active_reqs = [q for q in self.required_qualifications if not q.is_deleted]
        if not active_reqs:
            return True  # no active qualification requirement — anyone can fill it
        user_quals = set(user.qualifications)
        for required in active_reqs:
            if not any(required.can_be_filled_by(uq) for uq in user_quals):
                return False
        return True

    def is_eligible_for(self, fillable_qual_ids: set[int]) -> bool:
        """Fast eligibility check using a pre-computed fillable qualification ID set.

        Prefer this over :meth:`is_eligible` when checking many spots for the same
        user — call :func:`app.queries.user_fillable_qual_ids` once and pass the
        result here to avoid repeated recursive hierarchy traversals.
        """
        active_reqs = [q for q in self.required_qualifications if not q.is_deleted]
        if not active_reqs:
            return True
        return all(q.id in fillable_qual_ids for q in active_reqs)

    def __repr__(self) -> str:
        return f"<EventSpot {self.id} event={self.event_id}>"
