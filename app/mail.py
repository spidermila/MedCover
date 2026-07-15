"""
Centralised email helper for MedCover.

All public send_* functions enqueue a row into ``outbox_email`` instead of
calling the SMTP server directly.  The scheduler's ``process_email_queue``
job drains the queue at a controlled rate (MAIL_QUEUE_INTERVAL_SECONDS,
default 3 s ≈ 20 emails/minute) which keeps the app safely inside the
rate limits of any standard SMTP relay (e.g. Microsoft 365: 30/min).

Usage (inside a Flask app context):
    from app.mail import send_assignment_confirmed, send_event_published, ...

All functions are fire-and-forget — exceptions are logged, never raised.

NOTIFICATION CATALOG
--------------------
NOTIFICATION_CATALOG is the authoritative list of all email notification types
in the application.  It is used by the admin notification management page
(/admin/notifications/) to display the catalog and toggle enable/disable flags.

When adding a new send_* function:
  1. Add an entry to NOTIFICATION_CATALOG (see existing entries for structure).
  2. If the notification is togglable, add a ``notify_<code>`` boolean column
     to AppSettings and a corresponding entry in the catalog's ``settings_field``.
  3. Call ``_is_notify_enabled(code)`` at the top of the new send_* function.
  4. Pass ``notification_type=code`` to ``_enqueue()``.
  5. Update DEVOPS.md and CHANGELOG.md.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa
from flask import g, render_template

from app.extensions import db
from app.models.outbox import OutboxEmail

if TYPE_CHECKING:
    from app.models.assignment import Assignment
    from app.models.event import Event
    from app.models.settings import AppSettings
    from app.models.user import UserAccount

log = logging.getLogger(__name__)

# Instance identifier — set INSTANCE_ID in .env (e.g. "dev" or "prod").
# Stored on every outbox row and sent as an SMTP header so bounced/relayed
# emails can be traced back to the originating instance.
_INSTANCE_ID: str = os.environ.get("INSTANCE_ID", "")

_PLAIN_FALLBACK = "Tento e-mail obsahuje formátovaný obsah. Otevřete jej v e-mailovém klientovi s podporou HTML."

# ── Notification-delay tier boundaries (issue #268) ──────────────────────────
# Boundary semantics (REQUIREMENTS FR-4):
#   delta < 24h                → tier 1 (past events fall here — delta <= 0)
#   24h <= delta < 7d          → tier 2
#   7d  <= delta < 28d         → tier 3
#   delta >= 28d               → tier 4
_TIER_1_UPPER: timedelta = timedelta(hours=24)
_TIER_2_UPPER: timedelta = timedelta(days=7)
_TIER_3_UPPER: timedelta = timedelta(days=28)


def _compute_send_after(event_start: datetime, now: datetime, settings: AppSettings) -> datetime:
    """Return the future UTC datetime at which a notification for an event
    starting at *event_start* should be sent, given the tier delays in *settings*.

    *event_start* and *now* must both be tz-aware UTC (``tzinfo=timezone.utc``).
    """
    delta = event_start - now
    if delta < _TIER_1_UPPER:
        minutes = settings.notify_delay_under_24h_min
    elif delta < _TIER_2_UPPER:
        minutes = settings.notify_delay_1_7_days_min
    elif delta < _TIER_3_UPPER:
        minutes = settings.notify_delay_1_4_weeks_min
    else:
        minutes = settings.notify_delay_over_month_min
    return now + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Notification catalog — single source of truth (AD17)
# ---------------------------------------------------------------------------
# Each entry describes one notification type.  Fields:
#   code          : str  — unique key; must match the settings_field suffix and
#                          what is stored in OutboxEmail.notification_type
#   settings_field: str|None — AppSettings attribute name; None = always-on
#   name_cs       : str  — display name (Czech)
#   description_cs: str  — one-sentence description (Czech)
#   trigger_cs    : str  — when is this sent (Czech)
#   recipient_cs  : str  — who receives it (Czech)
#   templates     : list[str] — email template filenames
#   always_on     : bool — if True, cannot be disabled (auth / admin flows)
# ---------------------------------------------------------------------------
NOTIFICATION_CATALOG: list[dict] = [
    {
        "code": "assignment_confirmed",
        "settings_field": "notify_assignment",
        "name_cs": "Přihlášení na službu",
        "description_cs": (
            "Odesílán dobrovolníkovi při přihlášení na místo ve službě (jím samotným nebo koordinátorem)."
        ),
        "trigger_cs": "Přihlášení na místo ve službě",
        "recipient_cs": "Přihlášený dobrovolník (role: Člen)",
        "templates": ["email/assignment_confirmed.html"],
        "always_on": False,
    },
    {
        "code": "assignment_released",
        "settings_field": "notify_assignment",
        "name_cs": "Odhlášení ze služby",
        "description_cs": "Odesílán dobrovolníkovi při odhlášení z místa ve službě (jím samotným nebo koordinátorem).",
        "trigger_cs": "Odhlášení z místa ve službě",
        "recipient_cs": "Odhlášený dobrovolník (role: Člen)",
        "templates": ["email/assignment_released.html"],
        "always_on": False,
    },
    {
        "code": "event_published",
        "settings_field": "notify_event_published",
        "name_cs": "Nová akce zveřejněna",
        "description_cs": "Odesílán všem aktivním členům a koordinátorům při zveřejnění akce.",
        "trigger_cs": "Akce přejde do stavu Zveřejněno",
        "recipient_cs": "Všichni aktivní uživatelé (role: Koordinátor, Člen)",
        "templates": ["email/event_published.html"],
        "always_on": False,
    },
    {
        "code": "assignments_opened",
        "settings_field": "notify_assignments_opened",
        "name_cs": "Otevřeny přihlášky na akci",
        "description_cs": "Odesílán všem aktivním členům a koordinátorům při otevření přihlášek na akci.",
        "trigger_cs": "Akce přejde do stavu Přihlášky otevřeny",
        "recipient_cs": "Všichni aktivní uživatelé (role: Koordinátor, Člen)",
        "templates": ["email/assignments_opened.html"],
        "always_on": False,
    },
    {
        "code": "event_cancelled",
        "settings_field": "notify_event_cancelled",
        "name_cs": "Akce zrušena",
        "description_cs": "Odesílán přihlášeným dobrovolníkům při zrušení akce.",
        "trigger_cs": "Akce je zrušena",
        "recipient_cs": "Přihlášení dobrovolníci (role: Člen)",
        "templates": ["email/event_cancelled.html"],
        "always_on": False,
    },
    {
        "code": "event_changed",
        "settings_field": "notify_event_changed",
        "name_cs": "Změna údajů akce",
        "description_cs": "Odesílán přihlášeným dobrovolníkům při změně údajů akce (název, čas, místo, popis apod.).",
        "trigger_cs": "Uložení změny akce (editace existující akce)",
        "recipient_cs": "Přihlášení dobrovolníci (role: Člen)",
        "templates": ["email/event_changed.html"],
        "always_on": False,
    },
    {
        "code": "unfilled_reminder",
        "settings_field": "notify_unfilled_reminder",
        "name_cs": "Připomínka nevyplněných míst",
        "description_cs": (
            "Plánovačem odesílán koordinátorovi/zodpovědné osobě, pokud na akci zbývají nevyplněná místa."
        ),
        "trigger_cs": "Automaticky plánovačem (periodická kontrola)",
        "recipient_cs": "Tvůrce akce a zodpovědná osoba (role: Koordinátor, Člen)",
        "templates": ["email/unfilled_spots_reminder.html"],
        "always_on": False,
    },
    {
        "code": "debriefing_invitation",
        "settings_field": "notify_debriefing",
        "name_cs": "Pozvánka k výjezdové zprávě",
        "description_cs": "Odesílán přihlášeným dobrovolníkům po skončení akce s odkazem na formulář výjezdové zprávy.",
        "trigger_cs": "Akce přejde do stavu Dokončeno",
        "recipient_cs": "Přihlášení dobrovolníci (role: Člen)",
        "templates": ["email/debriefing_invitation.html"],
        "always_on": False,
    },
    {
        "code": "account_activated",
        "settings_field": None,
        "name_cs": "Aktivace účtu",
        "description_cs": "Odesílán uživateli, jehož účet byl aktivován administrátorem.",
        "trigger_cs": "Aktivace uživatelského účtu administrátorem",
        "recipient_cs": "Aktivovaný uživatel",
        "templates": ["email/account_activated.html"],
        "always_on": True,
    },
    {
        "code": "auth",
        "settings_field": None,
        "name_cs": "Pozvánka / obnova hesla",
        "description_cs": "Systémové e-maily pro ověření identity: pozvánky do systému a odkaz na obnovu hesla.",
        "trigger_cs": "Odeslání pozvánky administrátorem nebo žádost uživatele o obnovu hesla",
        "recipient_cs": "Pozvaný uživatel / žadatel o obnovu hesla",
        "templates": ["email/invite.html", "email/reset_password.html"],
        "always_on": True,
    },
    {
        "code": "admin_digest",
        "settings_field": None,
        "name_cs": "Admin přehled (digest)",
        "description_cs": "Pravidelný souhrnný e-mail pro administrátory. Konfigurován v sekci Admin → Digesty.",
        "trigger_cs": "Plánovač dle konfigurace DigestSchedule (Admin → Digesty)",
        "recipient_cs": "Nakonfigurovaní příjemci digestu (role: Admin)",
        "templates": ["generováno dynamicky"],
        "always_on": True,
    },
]

# ---------------------------------------------------------------------------
# Role-based notification gating (AD17)
# ---------------------------------------------------------------------------

# Roles whose members may receive each notification category.
# "auth" (invite / password-reset / activation) is exempt — always allowed.
_NOTIFICATION_ALLOWED_ROLES: dict[str, set[str]] = {
    "admin_digest": {"Admin"},
    "event_published": {"Coordinator", "Member"},
    "assignments_opened": {"Coordinator", "Member"},
    "assignment": {"Member"},  # confirmed, released
    "unfilled_reminder": {"Coordinator", "Member"},  # reminder to coordinator / RP
    "event_cancelled": {"Member"},  # cancelled → notify assigned users
    "event_changed": {"Member"},  # event details changed → notify assigned users
}


def user_can_receive_notification(user: UserAccount, notification_type: str) -> bool:
    """Return True if *user* is eligible for a notification of *notification_type*.

    Rules (AD17):
    - Viewer-only users receive no operational emails (only auth emails).
    - Users with any non-Viewer role are subject to the per-category role map.
    - "auth" category is always True for all users (invite, reset, activation).
    - During a test notification (g._test_notification_email set), always True so
      that the admin tester can preview any notification regardless of their own role.
    """
    if _is_test_notification():
        return True
    if notification_type == "auth":
        return True

    user_role_names: set[str] = {r.name for r in user.roles}

    # Viewer-only → no operational emails
    if user_role_names <= {"Viewer"}:
        return False

    allowed: set[str] = _NOTIFICATION_ALLOWED_ROLES.get(notification_type, set())
    return bool(user_role_names & allowed)


def _is_test_notification() -> bool:
    """Return True when a test notification override is active for this request."""
    try:
        return bool(getattr(g, "_test_notification_email", None))
    except RuntimeError:
        return False


def _is_notify_enabled(settings_field: str) -> bool:
    """Return False if the admin has disabled this notification type in AppSettings.

    Always returns True when a test notification override is active so that
    disabled notifications can still be previewed via the test send feature.
    """
    if _is_test_notification():
        return True
    try:
        from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

        return bool(getattr(get_settings(), settings_field, True))
    except Exception:  # noqa: BLE001
        return True  # fail open — don't suppress notifications on settings error


def _base_context() -> dict:
    """Return template context variables shared by all user-facing email templates."""
    from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel
    from app.utils import external_url_for  # pylint: disable=import-outside-toplevel

    try:
        org_name = get_settings().org_name or "MedCover"
    except Exception:  # noqa: BLE001
        org_name = "MedCover"
    return {"org_name": org_name, "url_for_external": external_url_for}


def _enqueue(
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    notification_type: str | None = None,
) -> None:
    """Insert a pending email row.  Must be called inside a Flask app context
    and inside an active DB session (the caller's transaction is fine).

    If ``g._test_notification_email`` is set (by the test-notification route),
    the recipient is overridden so the email goes to the tester instead.
    """
    override = getattr(g, "_test_notification_email", None)
    if override:
        to = override
    try:
        db.session.add(
            OutboxEmail(
                to_email=to,
                subject=subject,
                body=body,
                html_body=html_body,
                notification_type=notification_type,
                instance_name=_INSTANCE_ID or None,
            )
        )
        db.session.flush()  # assign id without a separate commit
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to enqueue mail to %s — %s", to, exc)


def enqueue_deferred(
    user: UserAccount,
    event: Event,
    notification_type: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    change_type: str | None = None,
    change_value: str | None = None,
) -> datetime | None:
    """Insert or update a pending OutboxEmail row for the deferred/batched
    event-notification pipeline (issue #268).

    Lookup key: (user_id, event_id, notification_type, status='pending').
    On lookup hit: overwrites rendered content and takes ``min(existing,
    computed)`` on ``send_after`` (immediate — NULL — always wins over any
    future timestamp per FR-8).

    On lookup miss: inserts a new row with ``send_after`` computed from
    proximity tier settings, or ``NULL`` if the request-scoped
    ``g._test_notification_immediate`` flag is set.

    Notification-gate checks must have been evaluated by the caller (FR-3).

    Returns the row's final ``send_after`` value (may be ``None``).
    """
    from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

    # Recipient override (FR-2 / FR-9).
    override = getattr(g, "_test_notification_email", None)
    to_email = override or user.email

    # Immediate-bypass flag (FR-6). Tolerates "outside request context".
    try:
        immediate = bool(getattr(g, "_test_notification_immediate", False))
    except RuntimeError:
        immediate = False

    computed_send_after: datetime | None
    if immediate:
        computed_send_after = None
    else:
        now_utc = datetime.now(timezone.utc)
        computed_send_after = _compute_send_after(event.start_datetime, now_utc, get_settings())

    try:
        lookup = (
            db.select(OutboxEmail)
            .where(
                OutboxEmail.user_id == user.id,
                OutboxEmail.event_id == event.id,
                OutboxEmail.notification_type == notification_type,
                OutboxEmail.status == "pending",
            )
            .limit(1)
            .with_hint(OutboxEmail, "WITH (UPDLOCK, HOLDLOCK, ROWLOCK)")
        )
        existing = db.session.scalars(lookup).first()

        if existing is not None:
            # FR-8 — merge send_after: immediate wins; else min().
            if existing.send_after is None:
                merged_send_after: datetime | None = None
            elif computed_send_after is None:
                merged_send_after = None
            else:
                merged_send_after = min(existing.send_after, computed_send_after)
            existing.send_after = merged_send_after
            existing.to_email = to_email
            existing.subject = subject
            existing.body = body
            existing.html_body = html_body
            existing.change_type = change_type
            existing.change_value = change_value
            db.session.flush()
            return existing.send_after

        row = OutboxEmail(
            to_email=to_email,
            subject=subject,
            body=body,
            html_body=html_body,
            notification_type=notification_type,
            user_id=user.id,
            event_id=event.id,
            change_type=change_type,
            change_value=change_value,
            send_after=computed_send_after,
            instance_name=_INSTANCE_ID or None,
        )
        db.session.add(row)
        db.session.flush()
        return computed_send_after
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to enqueue_deferred (user_id=%s event_id=%s type=%s) — %s",
            getattr(user, "id", None),
            getattr(event, "id", None),
            notification_type,
            exc,
        )
        return None


def _guarded_send(
    setting: str,
    notif_type: str,
    user: UserAccount,
    subject: str,
    template: str,
    notification_type: str,
    **ctx: object,
) -> None:
    """Guard + render + enqueue in one call.

    Checks the global setting toggle and the per-user preference, then renders
    *template* with ``user_name`` + ``_base_context()`` + any extra **ctx**
    kwargs, and enqueues the email.  Covers the common pattern shared by the
    majority of ``send_*`` functions.
    """
    if not _is_notify_enabled(setting):
        return
    if not user_can_receive_notification(user, notif_type):
        return
    html = render_template(template, user_name=user.name, **_base_context(), **ctx)
    _enqueue(user.email, subject, _PLAIN_FALLBACK, html_body=html, notification_type=notification_type)


# ── Assignment notifications ──────────────────────────────────────────────────


def send_assignment_confirmed(user: UserAccount, event: Event) -> None:
    """Notify a user that their spot assignment was confirmed."""
    if not _is_notify_enabled("notify_assignment"):
        return
    if not user_can_receive_notification(user, "assignment"):
        return
    html = render_template(
        "email/assignment_confirmed.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="assignment_confirmed",
        subject=f"MedCover — Přihlášení na akci: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


def send_assignment_released(user: UserAccount, event: Event) -> None:
    """Notify a user that their assignment was released (by themselves or coordinator)."""
    if not _is_notify_enabled("notify_assignment"):
        return
    if not user_can_receive_notification(user, "assignment"):
        return
    html = render_template(
        "email/assignment_released.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="assignment_released",
        subject=f"MedCover — Odhlášení z akce: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


# ── Event lifecycle notifications ─────────────────────────────────────────────


def send_event_published(user: UserAccount, event: Event) -> None:
    """Notify a user that an event they might be interested in was published."""
    if not _is_notify_enabled("notify_event_published"):
        return
    if not user_can_receive_notification(user, "event_published"):
        return
    html = render_template(
        "email/event_published.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_published",
        subject=f"MedCover — Nová akce: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


def send_assignments_opened(user: UserAccount, event: Event) -> None:
    """Notify a user that assignments opened for an event."""
    if not _is_notify_enabled("notify_assignments_opened"):
        return
    if not user_can_receive_notification(user, "assignments_opened"):
        return
    html = render_template(
        "email/assignments_opened.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="assignments_opened",
        subject=f"MedCover — Otevřeny přihlášky: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


def send_event_cancelled(user: UserAccount, event: Event) -> None:
    """Notify an assigned user that an event was cancelled."""
    if not _is_notify_enabled("notify_event_cancelled"):
        return
    if not user_can_receive_notification(user, "event_cancelled"):
        return
    html = render_template(
        "email/event_cancelled.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_cancelled",
        subject=f"MedCover — Akce zrušena: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


# Human-readable Czech labels for event fields shown in change notifications.
_EVENT_FIELD_LABELS: dict[str, str] = {
    "name": "Název akce",
    "master_event_id": "Nadřazená akce",
    "start_datetime": "Začátek",
    "end_datetime": "Konec",
    "address": "Místo konání",
    "contact_person": "Kontaktní osoba",
    "description": "Popis",
    "paid": "Placená akce",
    "responsible_person_id": "Zodpovědná osoba",
    "assignments_open_datetime": "Otevření přihlášek",
}


def _format_event_change_value(field: str, raw: object) -> str:
    """Return a human-readable Czech string for a single change value."""
    if raw is None or str(raw) in ("None", ""):
        return "—"
    val = str(raw)
    # Format ISO datetime strings to Czech local time.
    if "datetime" in field:
        try:
            from app.utils import get_app_tz  # pylint: disable=import-outside-toplevel

            parsed = datetime.fromisoformat(val)
            local = parsed.astimezone(get_app_tz())
            return local.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return val
    # Boolean fields
    if field == "paid":
        return "Ano" if val in ("True", "1", "true") else "Ne"
    return val


def send_event_changed(
    user: UserAccount,
    event: Event,
    changes: dict[str, list[object]],
    event_url: str = "",
) -> None:
    """Notify an assigned user that event details have changed.

    *changes* is the dict returned by ``diff_changes(before, after)``
    — ``{field_name: [old_value, new_value]}``.  Only called when the diff
    is non-empty.
    """
    if not _is_notify_enabled("notify_event_changed"):
        return
    if not user_can_receive_notification(user, "event_changed"):
        return
    formatted: list[tuple[str, str, str]] = [
        (
            _EVENT_FIELD_LABELS.get(field, field),
            _format_event_change_value(field, vals[0]),
            _format_event_change_value(field, vals[1]),
        )
        for field, vals in changes.items()
    ]
    html_body = render_template(
        "email/event_changed.html",
        user_name=user.name,
        event=event,
        event_url=event_url,
        changes=formatted,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_changed",
        subject=f"MedCover — Změna akce: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html_body,
    )


# ── Reminder (scheduler) ──────────────────────────────────────────────────────


def send_unfilled_spots_reminder(
    user: UserAccount,
    event: Event,
    unfilled: list,
) -> None:
    """Remind coordinator/RP that an event still has unfilled spots."""
    if not _is_notify_enabled("notify_unfilled_reminder"):
        return
    if not user_can_receive_notification(user, "unfilled_reminder"):
        return
    html = render_template(
        "email/unfilled_spots_reminder.html",
        user_name=user.name,
        coordinator_name=user.name,
        event=event,
        unfilled=unfilled,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="unfilled_reminder",
        subject=f"MedCover — Připomínka: volná místa na akci {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


# ── Admin digest ──────────────────────────────────────────────────────────────


def send_admin_digest(recipient_email: str, subject: str, html_body: str) -> None:
    """Enqueue a digest email to a single recipient.

    Plain-text fallback is a minimal message directing the user to an HTML-capable client.
    """
    plain_fallback = "Tento e-mail obsahuje formátovaný obsah. Otevřete jej v e-mailovém klientovi s podporou HTML."
    _enqueue(recipient_email, subject, plain_fallback, html_body=html_body, notification_type="admin_digest")


# ── Outbox drain (callable from tests and scheduler) ─────────────────────────


def _write_failure_audit(row: OutboxEmail) -> None:
    """Write an AuditLogEntry when an outbox email permanently fails.
    Called inside the active DB session — no commit here."""
    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel

    try:
        db.session.add(
            AuditLogEntry(
                actor_id=None,
                action_type="email_failed",
                entity_type="OutboxEmail",
                entity_id=str(row.id),
                summary=(
                    f"E-mail pro {row.to_email} se nepodařilo odeslat"
                    f" po {row.retry_count} pokusech: {row.last_error}"
                ),
                changes_json={"to": row.to_email, "subject": row.subject, "error": row.last_error},
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to write failure audit log for outbox id=%d — %s", row.id, exc)


def drain_one_outbox_email() -> bool:
    """Send the oldest pending outbox row within the current app context.

    Returns True if a row was processed (sent or failed), False if the queue
    was empty.  Designed to be called from both the scheduler and tests.
    """
    from flask_mail import Message  # pylint: disable=import-outside-toplevel

    from app.extensions import mail as _mail  # pylint: disable=import-outside-toplevel

    _now_utc = datetime.now(timezone.utc)
    query = (
        db.select(OutboxEmail)
        .where(
            OutboxEmail.status == "pending",
            OutboxEmail.retry_count < OutboxEmail.MAX_RETRIES,
            sa.or_(
                OutboxEmail.send_after.is_(None),
                OutboxEmail.send_after <= _now_utc,
            ),
        )
        .order_by(OutboxEmail.created_at.asc())
        .limit(1)
        .with_hint(OutboxEmail, "WITH (UPDLOCK, ROWLOCK, READPAST)")
    )

    row: OutboxEmail | None = db.session.scalars(query).first()

    if row is None:
        return False

    # --- Dev email block check ---
    from app.models.settings import get_settings as _get_settings  # pylint: disable=import-outside-toplevel

    _settings = _get_settings()
    if not _settings.is_email_allowed(row.to_email):
        row.status = "skipped"
        row.last_error = "dev_email_block: recipient not in allowlist"
        db.session.commit()
        log.warning(
            "Mail suppressed (dev_email_block): id=%d to=%s subject=%r",
            row.id,
            row.to_email,
            row.subject,
        )
        return True

    try:
        msg = Message(subject=row.subject, recipients=[row.to_email], body=row.body)
        if row.html_body:
            msg.html = row.html_body
        if _INSTANCE_ID:
            msg.extra_headers = {"X-MedCover-Instance": _INSTANCE_ID}
        _mail.send(msg)
        row.status = "sent"
        row.sent_at = datetime.now(timezone.utc)
        log.info("Mail sent: id=%d to=%s subject=%r", row.id, row.to_email, row.subject)
    except Exception as exc:  # noqa: BLE001
        row.retry_count += 1
        row.last_error = str(exc)
        if row.retry_count >= OutboxEmail.MAX_RETRIES:
            row.status = "failed"
            log.error(
                "Mail permanently failed: id=%d to=%s — %s (after %d retries)",
                row.id,
                row.to_email,
                exc,
                row.retry_count,
            )
            _write_failure_audit(row)
        else:
            log.warning(
                "Mail send failed (attempt %d/%d): id=%d to=%s — %s",
                row.retry_count,
                OutboxEmail.MAX_RETRIES,
                row.id,
                row.to_email,
                exc,
            )

    db.session.commit()
    return True


# ── Debriefing invitation ─────────────────────────────────────────────────────


def send_debriefing_invitation(assignment: Assignment, event: Event) -> None:
    """Send a debriefing invitation email to the assigned user."""
    user = assignment.user
    if not _is_notify_enabled("notify_debriefing"):
        return
    if not user_can_receive_notification(user, "assignment"):
        return
    from flask import url_for  # pylint: disable=import-outside-toplevel

    debriefing_url = url_for("debriefing.submit", assignment_id=assignment.id, _external=True)
    html = render_template(
        "email/debriefing_invitation.html",
        user_name=user.name,
        event=event,
        debriefing_url=debriefing_url,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="debriefing_invitation",
        subject=f"MedCover — Výjezdová zpráva: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


# ── Account activation ────────────────────────────────────────────────────────


def send_account_activated(user: UserAccount) -> None:
    """Enqueue an account-activation notification to the newly activated user."""
    from app.utils import external_url_for  # pylint: disable=import-outside-toplevel

    login_url = external_url_for("auth.login")
    html_body = render_template("email/account_activated.html", user=user, login_url=login_url, **_base_context())
    _enqueue(
        user.email,
        "MedCover — váš účet byl aktivován",
        _PLAIN_FALLBACK,
        html_body=html_body,
        notification_type="account_activated",
    )
