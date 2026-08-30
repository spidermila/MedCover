from datetime import datetime, timezone

from app.extensions import db


class DigestSchedule(db.Model):  # type: ignore[misc]
    """Global configuration for the admin digest email.

    One row (id=1) managed via the /admin/digest settings page.
    """

    __tablename__ = "digest_schedule"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default="false")

    # How often to send (hours).  Options: 6, 12, 24, 48, 72, 168.
    frequency_hours = db.Column(db.Integer, nullable=False, default=24, server_default="24")

    # Preferred Prague (CET/CEST) hour (0–23) at which to send.
    preferred_hour = db.Column(db.Integer, nullable=False, default=7, server_default="7")

    last_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Configurable email fields — no hardcoded text in the outgoing email.
    email_subject = db.Column(
        db.String(255),
        nullable=False,
        default="MedCover — Přehledový e-mail",
        server_default="MedCover — Přehledový e-mail",
    )
    header_html = db.Column(db.Text, nullable=True)
    footer_html = db.Column(db.Text, nullable=True)

    version = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    blocks = db.relationship(
        "DigestBlock",
        back_populates="schedule",
        order_by="DigestBlock.sort_order",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<DigestSchedule enabled={self.enabled} freq={self.frequency_hours}h>"


class DigestBlock(db.Model):  # type: ignore[misc]
    """One configurable content block in the digest.

    Each row represents one block type (server_stats, audit_log, …).
    config_json holds block-specific settings including a 'title' key so
    the admin can rename any block heading.
    """

    __tablename__ = "digest_block"

    id = db.Column(db.Integer, primary_key=True)
    digest_schedule_id = db.Column(
        db.Integer,
        db.ForeignKey("digest_schedule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    block_type = db.Column(db.String(64), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True, server_default="true")
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    config_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    version = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    schedule = db.relationship("DigestSchedule", back_populates="blocks")

    def __repr__(self) -> str:
        return f"<DigestBlock {self.block_type} enabled={self.enabled} order={self.sort_order}>"


class DigestMetricSnapshot(db.Model):  # type: ignore[misc]
    """Time-series metric snapshots recorded by the scheduler.

    Used to calculate peak values (e.g. maximum outbox queue depth) over a
    rolling window for the server_stats digest block.  Rows older than 30 days
    are pruned by the scheduler.
    """

    __tablename__ = "digest_metric_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    metric_name = db.Column(db.String(64), nullable=False, index=True)
    metric_value = db.Column(db.Float, nullable=False)

    def __repr__(self) -> str:
        return f"<DigestMetricSnapshot {self.metric_name}={self.metric_value} at={self.snapshot_at}>"


def get_digest_schedule() -> DigestSchedule:
    """Return the single DigestSchedule row, creating it with defaults if absent."""
    from app.digest.registry import BLOCK_REGISTRY  # pylint: disable=import-outside-toplevel

    row = db.session.get(DigestSchedule, 1)
    if row is None:
        row = DigestSchedule(id=1)
        db.session.add(row)
        db.session.flush()
        # Seed one DigestBlock row per registered block type
        for i, (btype, cls) in enumerate(BLOCK_REGISTRY.items()):
            db.session.add(
                DigestBlock(
                    digest_schedule_id=1,
                    block_type=btype,
                    enabled=False,
                    sort_order=i,
                    config_json=dict(cls.default_config),
                )
            )
        db.session.commit()
    return row
