import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections.abc import Callable

from app import create_app
from app.extensions import db
from app.mail import drain_batched_outbox, drain_one_outbox_email
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.settings import get_settings
from app.scheduler_tasks import (
    cleanup_work_report_files,
    run_admin_digest,
    run_record_metrics,
    run_scheduled_backup,
    run_send_reminders,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = create_app("production")

# Sentinel user ID used in audit log rows written by the scheduler (no human actor)
SCHEDULER_ACTOR_ID = None

INSTANCE_ID: str = os.environ.get("INSTANCE_ID", "")

# How long to wait between individual SMTP sends (seconds).
# 3 s ≈ 20 emails/min — safely under typical relay limits (e.g. MS365: 30/min).
MAIL_QUEUE_INTERVAL_SECONDS: int = int(os.environ.get("MAIL_QUEUE_INTERVAL_SECONDS", "3"))

# How often to write the scheduler heartbeat (DB row + local file), in seconds.
# Kept independent of the main loop's poll interval (below) so that polling more
# often to honor short schedule.every(...) intervals doesn't multiply DB writes.
HEARTBEAT_INTERVAL_SECONDS: int = 5


def _logged_task(name: str, fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a scheduled task to emit an INFO log line each time it fires."""

    def _wrapper() -> None:
        log.info("Task running: %s", name)
        fn()

    _wrapper.__name__ = fn.__name__
    return _wrapper


def process_email_queue() -> None:
    """Drain the outbox_email queue.

    Priority:
      1. drain_batched_outbox() — one batched email per triggering recipient.
      2. Fall through to drain_one_outbox_email() for user_id=NULL legacy rows.

    Called every MAIL_QUEUE_INTERVAL_SECONDS (default 3 s).  At most one
    SMTP send per tick — the batched drain's early return prevents double sending.
    """
    with app.app_context():
        s = get_settings()
        s.apply_to_app(app)
        # drain_batched_outbox calls external_url_for which needs a request context
        # when SERVER_NAME is not set in ProductionConfig.
        try:
            with app.test_request_context("/"):
                if not drain_batched_outbox():
                    drain_one_outbox_email()
        except Exception as exc:  # noqa: BLE001
            log.error("process_email_queue: drain failed: %s", exc, exc_info=True)


def open_assignments() -> None:
    """Auto-transition Events from Published → Assignments Open when assignments_open_datetime has passed."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        events = db.session.scalars(
            db.select(Event).where(
                Event.status == EventStatus.PUBLISHED,
                Event.assignments_open_datetime != None,  # noqa: E711
                Event.assignments_open_datetime <= now,
            )
        ).all()

        for event in events:
            event.status = EventStatus.ASSIGNMENTS_OPEN
            event.version += 1
            db.session.add(
                AuditLogEntry(
                    actor_id=SCHEDULER_ACTOR_ID,
                    action_type="status_change",
                    entity_type="Event",
                    entity_id=str(event.id),
                    summary=f"[Scheduler] Přihlašování automaticky otevřeno pro akci '{event.name}'",
                )
            )
            log.info("Opened assignments for event id=%s name=%r", event.id, event.name)

        if events:
            db.session.commit()
            log.info("open_assignments: processed %d event(s)", len(events))


def close_completed_events() -> None:
    """Auto-transition Events from Assignments Open/Closed → Completed after end_datetime."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        events = db.session.scalars(
            db.select(Event).where(
                Event.status.in_([EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED]),
                Event.end_datetime <= now,
                Event.archived == False,  # noqa: E712
            )
        ).all()

        for event in events:
            event.status = EventStatus.COMPLETED
            event.version += 1
            db.session.add(
                AuditLogEntry(
                    actor_id=SCHEDULER_ACTOR_ID,
                    action_type="status_change",
                    entity_type="Event",
                    entity_id=str(event.id),
                    summary=f"[Scheduler] Akce '{event.name}' automaticky dokončena po skončení termínu",
                )
            )
            log.info("Completed event id=%s name=%r", event.id, event.name)

        if events:
            db.session.commit()
            log.info("close_completed_events: processed %d event(s)", len(events))


def send_reminders() -> None:
    """Send unfilled-spot reminder emails for events whose reminder window has arrived.

    Delegates to app.scheduler_tasks.run_send_reminders for the core logic so
    that it can be tested without importing this module.
    """
    with app.app_context():
        run_send_reminders(db.session)


def send_admin_digest_task() -> None:
    """Send admin digest if it is due per DigestSchedule."""
    with app.app_context():
        run_admin_digest(db.session)


def scheduled_backup_task() -> None:
    """Create a daily backup if backup_schedule_enabled is True in AppSettings."""
    with app.app_context():
        run_scheduled_backup(db.session)


def record_metrics() -> None:
    """Record outbox queue depth snapshot every 15 minutes."""
    with app.app_context():
        run_record_metrics(db.session)


def cleanup_work_report() -> None:
    """Remove employee work report xlsx files older than 1 day."""
    with app.app_context():
        cleanup_work_report_files(app.instance_path)


if __name__ == "__main__":
    schedule.every(MAIL_QUEUE_INTERVAL_SECONDS).seconds.do(process_email_queue)
    schedule.every(1).minutes.do(_logged_task("open_assignments", open_assignments))
    schedule.every(1).minutes.do(_logged_task("close_completed_events", close_completed_events))
    schedule.every(5).minutes.do(_logged_task("send_reminders", send_reminders))
    schedule.every(1).hours.do(_logged_task("send_admin_digest", send_admin_digest_task))
    # Poll every minute so an HH:MM schedule can fire on the configured minute.
    # The task itself short-circuits cheaply when the time isn't due yet.
    schedule.every(1).minutes.do(_logged_task("scheduled_backup", scheduled_backup_task))
    schedule.every(15).minutes.do(_logged_task("record_metrics", record_metrics))
    schedule.every(1).hours.do(_logged_task("cleanup_work_report", cleanup_work_report))

    log.info(
        "Scheduler started (instance=%s pid=%d mail_queue_interval=%ds)",
        INSTANCE_ID or "?",
        os.getpid(),
        MAIL_QUEUE_INTERVAL_SECONDS,
    )

    _last_alive_log: float = 0.0  # monotonic timestamp of last hourly alive message
    _last_heartbeat: float = 0.0  # monotonic timestamp of last heartbeat write

    while True:
        schedule.run_pending()

        # Hourly "still alive" heartbeat in the log for easy monitoring.
        _now_mono = time.monotonic()
        if _now_mono - _last_alive_log >= 3600:
            log.info(
                "Scheduler alive (instance=%s pid=%d)",
                INSTANCE_ID or "?",
                os.getpid(),
            )
            _last_alive_log = _now_mono

        if _now_mono - _last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            # Write heartbeat so the admin dashboard can confirm the scheduler is alive
            try:
                with app.app_context():
                    s = get_settings()
                    s.scheduler_last_seen = datetime.now(timezone.utc)
                    db.session.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat write failed: %s", exc)
            # Also touch a local file so Docker healthcheck can verify without a DB query
            try:
                Path("/tmp/scheduler_heartbeat").touch()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat file touch failed: %s", exc)
            _last_heartbeat = _now_mono

        # Poll interval must stay below MAIL_QUEUE_INTERVAL_SECONDS (and every other
        # schedule.every(...) interval) — schedule.run_pending() only fires a job once
        # this loop notices it's due, so a coarser sleep here would silently cap how
        # often the mail queue (and any other job) can actually run.
        time.sleep(1)
