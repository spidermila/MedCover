"""Failed opportunity deliveries can be retried without duplicating active deliveries."""

import pytest

from app.extensions import db
from app.models.outbox import OutboxEmail
from app.scheduler_tasks import run_send_external_opportunities
from tests.test_external_events import make_opportunity


@pytest.mark.parametrize("status", ["pending", "sent", "failed"])
def test_scheduler_requeues_only_failed_deliveries(app, external_client, status):
    event_id, _ = make_opportunity(app)
    with app.app_context():
        assert run_send_external_opportunities(db.session) == 1
        original = db.session.scalar(
            db.select(OutboxEmail).where(
                OutboxEmail.event_id == event_id,
                OutboxEmail.notification_type == "external_opportunity",
            )
        )
        original.status = status
        original.retry_count = OutboxEmail.MAX_RETRIES
        original.last_error = "Previous SMTP error"
        db.session.commit()

        assert run_send_external_opportunities(db.session) == (1 if status == "failed" else 0)
        assert run_send_external_opportunities(db.session) == 0
        rows = db.session.scalars(
            db.select(OutboxEmail).where(
                OutboxEmail.event_id == event_id,
                OutboxEmail.user_id == original.user_id,
                OutboxEmail.notification_type == "external_opportunity",
            )
        ).all()
        assert len(rows) == (2 if status == "failed" else 1)
        assert original.status == status
        assert original.retry_count == OutboxEmail.MAX_RETRIES
        assert original.last_error == "Previous SMTP error"
        if status == "failed":
            replacement = next(row for row in rows if row.id != original.id)
            assert replacement.status == "pending"
            assert replacement.retry_count == 0
            assert replacement.last_error is None
