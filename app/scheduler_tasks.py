"""
Testable scheduler task implementations.

The scheduler (scheduler/main.py) delegates its core logic here so that
tests can call these functions directly with the test app context, without
importing or patching the scheduler module itself.
"""

import logging
from datetime import date, datetime, timedelta, timezone
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
            Event.archived == sa.false(),
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
        sa.select(UserAccount).join(UserAccount.roles).where(UserAccount.is_active == sa.true(), Role.name == "Admin")
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


def _record_failed_scheduled_backup(db_session: Any, exc: BaseException, today_local: date) -> bool:
    """Log a failed scheduled backup and mark the day as attempted.

    Always returns False so callers can ``return`` it directly.
    """
    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel
    from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

    log.error("Scheduled backup failed: %s", exc, exc_info=True)
    # The export reads through db_session; roll back before writing the audit
    # row so a session left dirty by the failure can't take the bookkeeping
    # commit down with it.
    db_session.rollback()
    # Stamp the date on failure too. The task is polled every minute, so
    # without this a persistent failure (full disk, unmounted share) would
    # retry ~1440 times a day, each writing an audit row and a traceback.
    # One attempt per local day; the error surfaces in the audit log and the
    # admin digest.
    get_settings().backup_last_scheduled_run_date = today_local
    db_session.add(
        AuditLogEntry(
            actor_id=None,
            action_type="error",
            entity_type="Backup",
            entity_id="error",
            summary=f"Automatická záloha selhala: {exc}",
            changes_json={"error": str(exc)},
        )
    )
    db_session.commit()
    return False


def run_scheduled_backup(db_session: Any, now: datetime | None = None) -> bool:
    """Run an automatic backup if scheduled backups are enabled and the time is due.

    The task is designed to be called every minute by the scheduler.  It only
    creates a backup if:
      1. backup_schedule_enabled is True in AppSettings.
      2. The current local time (app timezone) is at or after today's
         scheduled HH:MM. The "at or after" tolerance means a delayed tick
         (e.g. after a container restart mid-window) still catches up rather
         than skipping the whole day.
      3. The scheduled trigger hasn't already been attempted today (tracked
         via AppSettings.backup_last_scheduled_run_date, in the app timezone).
         A failed attempt counts: the date is stamped either way, so a broken
         backup target produces one error per day, not one per minute.
         Ad-hoc admin-triggered backups do NOT touch that field and do NOT
         suppress the scheduled run — the daily cap applies to scheduled runs
         only. Extra ad-hoc backups count against ``backup_keep_count`` and
         may cause older automatic backups to be pruned sooner.

    Args:
        db_session: An active SQLAlchemy session bound to the current app context.
        now:        Reference timestamp (default: utcnow). Override in tests.

    Returns:
        True if a backup was created, False otherwise.
    """
    from app.backup import export_to_zip, prune_old_backups  # pylint: disable=import-outside-toplevel
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
    local_now = now.astimezone(local_tz)
    scheduled_today = local_now.replace(
        hour=settings.backup_schedule_hour,
        minute=settings.backup_schedule_minute,
        second=0,
        microsecond=0,
    )
    if local_now < scheduled_today:
        log.debug(
            "Scheduled backup: skipped — too early (local=%s scheduled=%s).",
            local_now.isoformat(timespec="minutes"),
            scheduled_today.isoformat(timespec="minutes"),
        )
        return False

    # Idempotency: at most one *scheduled* backup per local date. Kept in
    # AppSettings (not derived from the filesystem) so ad-hoc admin backups
    # neither suppress the scheduled run nor let it fire twice.
    today_local = local_now.date()
    if settings.backup_last_scheduled_run_date == today_local:
        log.debug("Scheduled backup: already ran today (%s), skipping.", today_local.isoformat())
        return False

    try:
        zip_path = export_to_zip(settings.backup_dir, now=now)
    except Exception as exc:
        return _record_failed_scheduled_backup(db_session, exc, today_local)

    # Pruning is housekeeping, not part of the backup: a failure here (a file
    # locked by another process, a read-only share) must not report the
    # already-written archive as a failed backup, nor leave the run unstamped
    # and so re-exported on the next minute's tick.
    try:
        pruned = prune_old_backups(settings.backup_dir, settings.backup_keep_count)
    except OSError as exc:
        log.warning("Scheduled backup: pruning old files failed: %s", exc, exc_info=True)
        pruned = []

    try:
        log.info("Scheduled backup created: %s (pruned %d old files)", zip_path.name, len(pruned))

        settings.backup_last_scheduled_run_date = today_local
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
        return _record_failed_scheduled_backup(db_session, exc, today_local)


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
