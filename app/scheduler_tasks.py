"""
Testable scheduler task implementations.

The scheduler (scheduler/main.py) delegates its core logic here so that
tests can call these functions directly with the test app context, without
importing or patching the scheduler module itself.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

log = logging.getLogger(__name__)


def run_send_reminders(db_session: Any, now: datetime | None = None) -> int:
    """Check all ASSIGNMENTS_OPEN events and send reminder emails where due.

    Args:
        db_session: An active SQLAlchemy session bound to the current app context.
        now:        The reference timestamp (default: utcnow). Pass an explicit
                    value in tests to control timing.

    Returns:
        Number of reminder emails enqueued.
    """
    from app.mail import send_unfilled_spots_reminder  # pylint: disable=import-outside-toplevel
    from app.models.event import Event, EventStatus  # pylint: disable=import-outside-toplevel

    if now is None:
        now = datetime.now(timezone.utc)

    events = db_session.scalars(
        sa.select(Event).where(
            Event.status == EventStatus.ASSIGNMENTS_OPEN,
            Event.archived.is_(False),
            Event.start_datetime > now,
        )
    ).all()

    total_sent = 0
    for event in events:
        unfilled = event.unfilled_spots
        if not unfilled:
            continue

        sent_map: dict = event.reminder_sent_json or {}
        changed = False

        for hours in event.reminder_hours():
            key = str(hours)
            if key in sent_map:
                continue  # already sent for this offset
            window_open_at = event.start_datetime - timedelta(hours=hours)
            if now < window_open_at:
                continue  # not yet time

            # Collect unique recipient User objects: RP and/or ME coordinator
            recipients: set = set()
            if event.responsible_person:
                recipients.add(event.responsible_person)
            if event.master_event and event.master_event.coordinator:
                recipients.add(event.master_event.coordinator)

            for user in recipients:
                send_unfilled_spots_reminder(user, event, unfilled)
                log.info("Reminder sent for event id=%s (%sh before) to %s", event.id, hours, user.email)
                total_sent += 1

            sent_map[key] = now.isoformat()
            changed = True

        if changed:
            event.reminder_sent_json = sent_map
            db_session.commit()

    return total_sent


def run_record_metrics(db_session: Any, now: datetime | None = None) -> None:
    """Record a snapshot of current outbox queue depth for peak tracking.

    Called by the scheduler every ~15 minutes.  Rows older than 30 days
    are pruned in the same call.
    """
    from app.models.digest import DigestMetricSnapshot  # pylint: disable=import-outside-toplevel
    from app.models.outbox import OutboxEmail  # pylint: disable=import-outside-toplevel

    if now is None:
        now = datetime.now(timezone.utc)

    pending = (
        db_session.scalar(sa.select(sa.func.count()).select_from(OutboxEmail).where(OutboxEmail.status == "pending"))
        or 0
    )

    db_session.add(
        DigestMetricSnapshot(
            snapshot_at=now,
            metric_name="outbox_pending_count",
            metric_value=float(pending),
        )
    )

    cutoff = now - timedelta(days=30)
    db_session.execute(sa.delete(DigestMetricSnapshot).where(DigestMetricSnapshot.snapshot_at < cutoff))
    db_session.commit()
    log.debug("Metric snapshot: outbox_pending_count=%d", pending)


def run_admin_digest(db_session: Any, now: datetime | None = None) -> bool:
    """Send the admin digest if it is due according to DigestSchedule.

    Returns True if the digest was enqueued, False if skipped.
    """
    from app.digest.renderer import render_digest  # pylint: disable=import-outside-toplevel
    from app.mail import send_admin_digest  # pylint: disable=import-outside-toplevel
    from app.models.digest import get_digest_schedule  # pylint: disable=import-outside-toplevel
    from app.models.role import Role  # pylint: disable=import-outside-toplevel
    from app.models.user import UserAccount  # pylint: disable=import-outside-toplevel

    if now is None:
        now = datetime.now(timezone.utc)

    schedule = get_digest_schedule()
    from app.utils import get_app_tz  # pylint: disable=import-outside-toplevel

    local_tz = get_app_tz()

    if not schedule.enabled:
        log.info("Admin digest: skipped — digest disabled.")
        return False

    local_now = now.astimezone(local_tz)

    if schedule.frequency_hours >= 24:
        # Daily+: fire only at the configured local hour, and only once per calendar day.
        if local_now.hour != schedule.preferred_hour:
            log.debug(
                "Admin digest: skipped — hour mismatch (now=%d preferred=%d).",
                local_now.hour,
                schedule.preferred_hour,
            )
            return False
        if schedule.last_sent_at is not None:
            if schedule.last_sent_at.astimezone(local_tz).date() >= local_now.date():
                log.info(
                    "Admin digest: skipped — already sent today (last_sent=%s).",
                    schedule.last_sent_at.astimezone(local_tz).date(),
                )
                return False
    else:
        # Sub-daily: ignore hour gate, use elapsed-time check only.
        if schedule.last_sent_at is not None:
            elapsed = (now - schedule.last_sent_at).total_seconds()
            if elapsed < schedule.frequency_hours * 3600:
                log.debug(
                    "Admin digest: skipped — %.0fs elapsed, need %.0fs.",
                    elapsed,
                    schedule.frequency_hours * 3600,
                )
                return False

    eligible = db_session.scalars(
        sa.select(UserAccount).join(UserAccount.roles).where(UserAccount.is_active.is_(True), Role.name == "Admin")
    ).all()

    if not eligible:
        log.info("Admin digest: no eligible recipients, skipping.")
        schedule.last_sent_at = now
        db_session.commit()
        return False

    html = render_digest(db_session)
    for user in eligible:
        send_admin_digest(user.email, schedule.email_subject, html)
        log.info("Admin digest enqueued for %s", user.email)

    schedule.last_sent_at = now
    db_session.commit()
    return True


def run_scheduled_backup(db_session: Any, now: datetime | None = None) -> bool:
    """Run an automatic backup if scheduled backups are enabled and it is the right hour.

    The task is designed to be called every hour by the scheduler.  It only
    creates a backup if:
      1. backup_schedule_enabled is True in AppSettings.
      2. The current UTC hour matches backup_schedule_hour.
      3. No backup file already exists for today (prevents double-runs on
         scheduler restarts).

    Args:
        db_session: An active SQLAlchemy session bound to the current app context.
        now:        Reference timestamp (default: utcnow). Override in tests.

    Returns:
        True if a backup was created, False otherwise.
    """
    from app.backup import export_to_zip, list_backups, prune_old_backups  # pylint: disable=import-outside-toplevel
    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel
    from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

    if now is None:
        now = datetime.now(timezone.utc)

    settings = get_settings()
    if not settings.backup_schedule_enabled:
        log.debug("Scheduled backup: skipped — backup schedule disabled.")
        return False

    from app.utils import get_app_tz  # pylint: disable=import-outside-toplevel

    local_tz = get_app_tz()
    local_hour = now.astimezone(local_tz).hour
    if local_hour != settings.backup_schedule_hour:
        log.debug(
            "Scheduled backup: skipped — hour mismatch (now=%d scheduled=%d).",
            local_hour,
            settings.backup_schedule_hour,
        )
        return False

    # Skip if a backup was already created today (UTC date) to avoid duplicates.
    today_prefix = f"medcover_backup_{now.strftime('%Y%m%d')}_"
    existing = list_backups(settings.backup_dir)
    if any(b["name"].startswith(today_prefix) for b in existing):
        log.debug("Scheduled backup: already have a backup for today, skipping.")
        return False

    try:
        zip_path = export_to_zip(settings.backup_dir, now=now)
        pruned = prune_old_backups(settings.backup_dir, settings.backup_keep_count)
        log.info("Scheduled backup created: %s (pruned %d old files)", zip_path.name, len(pruned))

        db_session.add(
            AuditLogEntry(
                actor_id=None,
                action_type="create",
                entity_type="Backup",
                entity_id=zip_path.name,
                summary=f"Automatická záloha vytvořena: {zip_path.name}",
                changes_json={"file": zip_path.name, "pruned": [p.name for p in pruned]},
            )
        )
        db_session.commit()
        return True
    except Exception as exc:
        log.error("Scheduled backup failed: %s", exc, exc_info=True)
        db_session.add(
            AuditLogEntry(
                actor_id=None,
                action_type="error",
                entity_type="Backup",
                entity_id="error",
                changes_json={"error": str(exc)},
            )
        )
        db_session.commit()
        return False


def cleanup_work_report_files(instance_path: str, now: datetime | None = None) -> int:
    """Delete generated employee work report xlsx files older than 1 day.

    Files are stored under  <instance_path>/work_report/<user_id>/<year>-<MM>.xlsx.
    Returns the number of files removed.
    """
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    work_report_root = Path(instance_path) / "work_report"
    if not work_report_root.exists():
        return 0

    removed = 0
    for xlsx_file in work_report_root.rglob("*.xlsx"):
        mtime = datetime.fromtimestamp(xlsx_file.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            try:
                xlsx_file.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover
                log.warning("Could not remove old work report file %s: %s", xlsx_file, exc)

    if removed:
        log.info("Cleaned up %d old work report file(s).", removed)
    return removed
