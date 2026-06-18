from datetime import datetime, timezone

from sqlalchemy.orm import Mapped

from app.extensions import db

# Self-referential M2M: qualification hierarchy
# qualification_parents.parent_id → qualifications whose holders CAN SUBSTITUTE for qualification_id
# e.g. Doctor (parent) can fill a First Aider (child) spot
qualification_parents = db.Table(
    "qualification_parents",
    db.Column("qualification_id", db.Integer, db.ForeignKey("qualification.id"), primary_key=True),
    db.Column("parent_id", db.Integer, db.ForeignKey("qualification.id"), primary_key=True),
)

# M2M: UserAccount ↔ Qualification
user_qualifications = db.Table(
    "user_qualifications",
    db.Column("user_id", db.Uuid, db.ForeignKey("user_account.id"), primary_key=True),
    db.Column("qualification_id", db.Integer, db.ForeignKey("qualification.id"), primary_key=True),
)


class Qualification(db.Model):  # type: ignore[misc]
    __tablename__ = "qualification"
    __table_args__ = (
        # Partial unique index: name must be unique among non-deleted qualifications only,
        # so a new qualification can be created with the same name after soft-delete.
        db.Index(
            "ix_qualification_name_active_unique",
            "name",
            unique=True,
            mssql_where=db.text("is_deleted = 0"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=True)
    can_be_rp = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Qualifications that can substitute for this one (e.g. Doctor is a parent of First Aider)
    parents: Mapped[list[Qualification]] = db.relationship(
        "Qualification",
        secondary=qualification_parents,
        primaryjoin="Qualification.id == qualification_parents.c.qualification_id",
        secondaryjoin="Qualification.id == qualification_parents.c.parent_id",
        backref="children",
        lazy="selectin",
    )

    holders = db.relationship(
        "UserAccount",
        secondary=user_qualifications,
        back_populates="qualifications",
        lazy="dynamic",
    )

    def can_be_filled_by(self, qualification: Qualification, _visited: frozenset[int] | None = None) -> bool:
        """Return True if a holder of `qualification` can fill a spot requiring self.

        Walks up the parent chain: if qualification is self, or qualification can fill
        any of self's parents (which can directly substitute for self), return True.
        _visited guards against cycles in the qualification hierarchy.
        """
        if _visited is None:
            _visited = frozenset()
        if self.id in _visited:
            return False
        _visited = _visited | {self.id}
        if qualification.id == self.id:
            return True
        return any(parent.can_be_filled_by(qualification, _visited) for parent in self.parents)

    def soft_delete(self) -> None:
        """Mark this qualification as deleted. Does not cascade — caller handles references."""
        self.is_deleted = True
        self.deleted_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return f"<Qualification {self.name}>"
