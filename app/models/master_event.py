from datetime import datetime, timezone

from app.extensions import db


class MasterEvent(db.Model):  # type: ignore[misc]
    __tablename__ = "master_event"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    coordinator_id = db.Column(db.Uuid, db.ForeignKey("user_account.id"), nullable=True)
    is_general = db.Column(db.Boolean, default=False, nullable=False)  # built-in General ME
    archived = db.Column(db.Boolean, default=False, nullable=False)
    # Optimistic-lock counter enforced via SQLAlchemy version_id_col.
    # Callers must bump before commit; stale committed values raise StaleDataError.
    version = db.Column(db.Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
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

    coordinator = db.relationship(
        "UserAccount", foreign_keys=[coordinator_id], back_populates="coordinated_master_events"
    )
    events = db.relationship("Event", back_populates="master_event", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<MasterEvent {self.name}>"
