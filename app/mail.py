"""
Centralised email helper for MedCover.

All public send_* functions enqueue a row into ``outbox_email`` instead of
calling the SMTP server directly.  The scheduler's ``process_email_queue``
job drains the queue at a controlled rate (MAIL_QUEUE_INTERVAL_SECONDS,
default 3 s ≈ 20 emails/minute) which keeps the app safely inside the
rate limits of any standard SMTP relay (e.g. Microsoft 365: 30/min).

Usage (inside a Flask app context):
    from app.mail import send_assignment_confirmed, send_event_published, ...

Enqueue error policy: ``IntegrityError`` (racing insert, or FK violation
from a target entity being hard-deleted mid-request) is tolerated — the
row is skipped, the session is rolled back, and the caller's business
transaction continues. Any other exception (OperationalError,
ProgrammingError, DB timeout, developer bug, template render failure
etc.) propagates to the caller so infrastructure problems surface in
logs and error trackers instead of being swallowed as WARN lines.
Callers are expected to commit their own business transaction *before*
calling any ``send_*`` helper, so a propagated exception only affects
the notification path.

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

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from flask import g, render_template, url_for
from flask_mail import Message
from sqlalchemy.exc import IntegrityError

from app.extensions import db, mail
from app.models.audit import AuditLogEntry
from app.models.event import Event
from app.models.outbox import OutboxEmail
from app.models.settings import AppSettings, get_settings
from app.models.user import UserAccount
from app.utils import external_url_for, get_app_tz

if TYPE_CHECKING:
    from app.models.assignment import Assignment

log = logging.getLogger(__name__)

# Instance identifier — set INSTANCE_ID in .env (e.g. "dev" or "prod").
# Stored on every outbox row and sent as an SMTP header so bounced/relayed
# emails can be traced back to the originating instance.
_INSTANCE_ID: str = os.environ.get("INSTANCE_ID", "")

_PLAIN_FALLBACK = "Tento e-mail obsahuje formátovaný obsah. Otevřete jej v e-mailovém klientovi s podporou HTML."

# ── Notification-delay tier boundaries (issue #268) ──────────────────────────
# Boundary semantics:
#   delta < 24h                → tier 1 (past events fall here — delta <= 0)
#   24h <= delta < 7d          → tier 2
#   7d  <= delta < 28d         → tier 3
#   delta >= 28d               → tier 4
_TIER_1_UPPER: timedelta = timedelta(hours=24)
_TIER_2_UPPER: timedelta = timedelta(days=7)
_TIER_3_UPPER: timedelta = timedelta(days=28)

# Stored in outbox_email.change_type for rows whose change_value is a
# field-level {field: [old, new]} JSON diff.
_EVENT_CHANGED_CHANGE_TYPE: str = "field_edit"

# change_type tokens for batched drain dispatch.
_ASSIGNMENT_CHANGE_TYPE: str = "assignment"
_UNFILLED_REMINDER_CHANGE_TYPE: str = "unfilled_reminder"
_DEBRIEFING_CHANGE_TYPE: str = "debriefing"


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
        "code": "event_archived",
        "settings_field": "notify_event_archived",
        "name_cs": "Akce archivována",
        "description_cs": (
            "Odesílán okamžitě přihlášeným dobrovolníkům a uživatelům s čekajícími "
            "oznámeními k akci při jejím archivování — spolu s případnými dříve odloženými změnami."
        ),
        "trigger_cs": "Akce je archivována nebo zrušena",
        "recipient_cs": "Přihlášení dobrovolníci a uživatelé s čekajícím oznámením (role: Člen)",
        "templates": ["email/event_archived.html"],
        "always_on": False,
    },
    {
        "code": "event_unarchived",
        "settings_field": "notify_event_unarchived",
        "name_cs": "Akce obnovena z archivu",
        "description_cs": "Odesílán přihlášeným dobrovolníkům při obnovení akce z archivu.",
        "trigger_cs": "Akce je obnovena z archivu",
        "recipient_cs": "Přihlášení dobrovolníci (role: Člen)",
        "templates": ["email/event_unarchived.html"],
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
        "name_cs": "Pozvánka k debriefingu",
        "description_cs": "Odesílán přihlášeným dobrovolníkům po skončení akce s odkazem na formulář debriefingu.",
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
    "event_archived": {"Member"},  # archived → notify assigned + queued users
    "event_unarchived": {"Member"},  # unarchived → notify assigned users
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

        return bool(getattr(get_settings(), settings_field, True))
    except Exception:  # noqa: BLE001
        return True  # fail open — don't suppress notifications on settings error


def _base_context() -> dict:
    """Return template context variables shared by all user-facing email templates."""

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
    except IntegrityError as exc:
        db.session.rollback()
        log.warning("Failed to enqueue mail to %s — IntegrityError, skipping: %s", to, exc)


def _merge_event_changed_payloads(
    existing_json: str,
    incoming: dict[str, list],
) -> dict[str, list] | None:
    """Merge two event_changed field-diff payloads.

    Rules:
      1. Fields present in both: keep existing's [0] (earliest old_val),
         overwrite with incoming's [1] (latest new_val).
      2. Fields present only in incoming: take incoming pair as-is.
      3. Fields present only in existing: carry forward unchanged.
      4. After merge, drop fields where str(old) == str(new) — matches the
         equality rule used by diff_changes at app/utils.py:171.
      5. If the merged dict is empty, return None (caller deletes the row).
    """
    existing = json.loads(existing_json)
    merged: dict[str, list] = dict(existing)
    for field, pair in incoming.items():
        in_old, in_new = pair[0], pair[1]
        if field in existing:
            original_old = existing[field][0]
            merged[field] = [original_old, in_new]
        else:
            merged[field] = [in_old, in_new]
    merged = {f: v for f, v in merged.items() if str(v[0]) != str(v[1])}
    return merged or None


def _merge_into_existing(
    existing: OutboxEmail,
    user: UserAccount,
    event: Event,
    notification_type: str,
    to_email: str,
    subject: str,
    body: str,
    html_body: str | None,
    change_type: str | None,
    change_value: dict[str, Any] | None,
    event_url: str,
    computed_send_after: datetime | None,
) -> datetime | None:
    """Update an already-pending OutboxEmail row.

    Returns the row's post-merge send_after, or None if the merged
    event_changed payload collapsed to empty and the row was deleted.
    """
    # merge send_after: NULL means "send immediately" (see OutboxEmail.send_after
    # and the drain queries), so if either side is immediate, immediate wins
    # otherwise take the earlier of the two future timestamps.
    merged_send_after: datetime | None
    if existing.send_after is None or computed_send_after is None:
        merged_send_after = None
    else:
        merged_send_after = min(existing.send_after, computed_send_after)
    existing.send_after = merged_send_after
    existing.to_email = to_email
    existing.subject = subject
    existing.body = body

    if (
        notification_type == "event_changed"
        and change_type == _EVENT_CHANGED_CHANGE_TYPE
        and isinstance(change_value, dict)
    ):
        if existing.change_value:
            effective_payload = _merge_event_changed_payloads(existing.change_value, change_value)
            if effective_payload is None:
                db.session.delete(existing)
                db.session.flush()
                return None
        else:
            effective_payload = change_value

        existing.change_value = json.dumps(effective_payload, ensure_ascii=False, sort_keys=True)
        existing.change_type = _EVENT_CHANGED_CHANGE_TYPE
        existing.html_body = _render_event_changed_body(user, event, effective_payload, event_url)
    else:
        existing.html_body = html_body
        existing.change_type = change_type
        if change_value is not None:
            existing.change_value = (
                json.dumps(change_value, ensure_ascii=False, sort_keys=True)
                if isinstance(change_value, dict)
                else change_value
            )

    db.session.flush()
    return existing.send_after


def enqueue_deferred(
    user: UserAccount,
    event: Event,
    notification_type: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    change_type: str | None = None,
    change_value: dict[str, Any] | None = None,
    event_url: str = "",
    immediate: bool = False,
) -> datetime | None:
    """Insert or update a pending OutboxEmail row for the deferred/batched
    event-notification pipeline (issue #268).

    Uniqueness of the pending row per (user_id, event_id, notification_type)
    is enforced by the ``uq_outbox_pending_by_user_event_type`` filtered
    unique index (see app/models/outbox.py). The
    ``WITH (UPDLOCK, HOLDLOCK, ROWLOCK)`` hint on the SELECT keeps the
    common case single-round-trip; if two workers race past the hint an
    IntegrityError is raised and this function falls back into the merge
    branch after re-loading the winning row.

    Lookup key: (user_id, event_id, notification_type, status='pending').
    On lookup hit: overwrites rendered content and takes ``min(existing,
    computed)`` on ``send_after`` (immediate — NULL — always wins over any
    future timestamp).

    On lookup miss: inserts a new row with ``send_after`` computed from
    proximity tier settings, or ``NULL`` if the request-scoped
    ``g._test_notification_immediate`` flag is set.

    Notification-gate checks must have been evaluated by the caller.

    Returns the row's final ``send_after`` value (may be ``None``).
    """

    # Recipient override.
    override = getattr(g, "_test_notification_email", None)
    to_email = override or user.email

    # Immediate-bypass: caller-supplied kwarg OR the test-form request-scoped
    # flag. g lookup tolerates "outside request context".
    if not immediate:
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

    def _insert_new() -> OutboxEmail:
        row = OutboxEmail(
            to_email=to_email,
            subject=subject,
            body=body,
            html_body=html_body,
            notification_type=notification_type,
            user_id=user.id,
            event_id=event.id,
            change_type=change_type,
            change_value=(
                json.dumps(change_value, ensure_ascii=False, sort_keys=True)
                if isinstance(change_value, dict)
                else change_value
            ),
            send_after=computed_send_after,
            instance_name=_INSTANCE_ID or None,
        )
        db.session.add(row)
        db.session.flush()
        return row

    try:
        existing = db.session.scalars(lookup).first()
        if existing is not None:
            return _merge_into_existing(
                existing,
                user,
                event,
                notification_type,
                to_email,
                subject,
                body,
                html_body,
                change_type,
                change_value,
                event_url,
                computed_send_after,
            )
        _insert_new()
        return computed_send_after

    except IntegrityError as exc:
        # A racing worker won the insert (uq_outbox_pending_by_user_event_type).
        # Roll back, re-load the winning row, and take the merge branch.
        db.session.rollback()
        log.info(
            "enqueue_deferred: race lost on unique index, retrying via merge (user_id=%s event_id=%s type=%s) — %s",
            getattr(user, "id", None),
            getattr(event, "id", None),
            notification_type,
            exc,
        )
        winner = db.session.scalars(lookup).first()
        if winner is not None:
            return _merge_into_existing(
                winner,
                user,
                event,
                notification_type,
                to_email,
                subject,
                body,
                html_body,
                change_type,
                change_value,
                event_url,
                computed_send_after,
            )
        # Very unlikely: the winner was drained-and-marked-sent between our
        # rollback and this reload. No conflict is possible now.
        _insert_new()
        return computed_send_after


# ── Assignment notifications ──────────────────────────────────────────────────


def send_assignment_confirmed(user: UserAccount, event: Event, spot_description: str | None = None) -> None:
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
        change_type=_ASSIGNMENT_CHANGE_TYPE,
        change_value={"action": "confirmed", "spot_description": spot_description or ""},
    )


def send_assignment_released(user: UserAccount, event: Event, spot_description: str | None = None) -> None:
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
        change_type=_ASSIGNMENT_CHANGE_TYPE,
        change_value={"action": "released", "spot_description": spot_description or ""},
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
    """Notify an assigned user that an event was cancelled. Enqueued for
    immediate send so any previously deferred rows for the same recipient/event
    flush together (matches send_event_archived's semantics)."""
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
        immediate=True,
    )


def send_event_archived(user: UserAccount, event: Event) -> None:
    """Notify a user that an event was archived. Enqueued for immediate send so
    any previously deferred rows for the same recipient/event flush together."""
    if not _is_notify_enabled("notify_event_archived"):
        return
    if not user_can_receive_notification(user, "event_archived"):
        return
    html = render_template(
        "email/event_archived.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_archived",
        subject=f"MedCover — Akce archivována: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
        immediate=True,
    )


def send_event_unarchived(user: UserAccount, event: Event) -> None:
    """Notify a user that an event was restored from the archive."""
    if not _is_notify_enabled("notify_event_unarchived"):
        return
    if not user_can_receive_notification(user, "event_unarchived"):
        return
    html = render_template(
        "email/event_unarchived.html",
        user_name=user.name,
        event=event,
        **_base_context(),
    )
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_unarchived",
        subject=f"MedCover — Akce obnovena z archivu: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
    )


def _flush_and_notify(event: Event, send_fn: Callable[[UserAccount, Event], None]) -> None:
    """Shared notification plumbing for event-state-change routes (archive/cancel).

    1. Force every pending outbox row for this event to send immediately
       (``send_after=NULL``) so already-queued edits go out with the
       notification instead of being stranded.
    2. Call ``send_fn(user, event)`` (immediate) for the union of currently-
       assigned users and users who have any pending outbox rows for this
       event.

    Caller is expected to commit the surrounding transaction.
    """
    # synchronize_session="fetch" is explicit on purpose: this bulk UPDATE
    # bypasses the ORM's per-object unit-of-work, so any OutboxEmail already
    # loaded into the session's identity map (e.g. a caller/test that queried
    # a row before calling this) would otherwise keep a stale in-memory
    # send_after unless the ORM is told to reconcile it. Leaving this on the
    # implicit default ("auto") happens to work today only because the WHERE
    # clause is simple equality that the "evaluate" strategy can resolve in
    # Python — a future edit to the WHERE clause (e.g. a subquery or OR) would
    # silently change that behaviour. "fetch" issues one extra SELECT to
    # identify the matched rows and always refreshes them, regardless of
    # WHERE-clause shape, so this stays correct independent of future edits.
    db.session.execute(
        sa.update(OutboxEmail)
        .where(
            OutboxEmail.event_id == event.id,
            OutboxEmail.status == "pending",
        )
        .values(send_after=None)
        .execution_options(synchronize_session="fetch")
    )

    assigned_users = [s.assignment.user for s in event.spots if s.assignment]
    recipients: dict = {u.id: u for u in assigned_users}

    pending_user_ids = db.session.scalars(
        db.select(OutboxEmail.user_id)
        .distinct()
        .where(
            OutboxEmail.event_id == event.id,
            OutboxEmail.status == "pending",
            OutboxEmail.user_id.is_not(None),
        )
    ).all()
    missing_ids = [uid for uid in pending_user_ids if uid not in recipients]
    if missing_ids:

        extra_users = db.session.scalars(db.select(UserAccount).where(UserAccount.id.in_(missing_ids))).all()
        for u in extra_users:
            recipients[u.id] = u

    for user in recipients.values():
        send_fn(user, event)


def flush_and_notify_archived(event: Event) -> None:
    """Handle notification side of archiving an event (see `_flush_and_notify`)."""
    _flush_and_notify(event, send_event_archived)


def flush_and_notify_cancelled(event: Event) -> None:
    """Handle notification side of cancelling an event (see `_flush_and_notify`)."""
    _flush_and_notify(event, send_event_cancelled)


def notify_unarchived(event: Event) -> None:
    """Notify all currently-assigned users that an event was restored/
    unarchived. Caller is expected to commit the surrounding business
    transaction before calling this, then commit again afterward to
    persist the enqueued rows (send_event_unarchived only flushes)."""
    assigned_users = [s.assignment.user for s in event.spots if s.assignment]
    for user in assigned_users:
        send_event_unarchived(user, event)


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

            parsed = datetime.fromisoformat(val)
            local = parsed.astimezone(get_app_tz())
            return local.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return val
    # Boolean fields
    if field == "paid":
        return "Ano" if val in ("True", "1", "true") else "Ne"
    return val


def _render_event_changed_body(
    user: UserAccount,
    event: Event,
    payload: dict[str, list],
    event_url: str,
) -> str:
    """Translate a field-diff payload into a rendered HTML email body.

    Called from send_event_changed (first-insert path) and the merge branch
    inside enqueue_deferred (update path).  Designed to be callable from
    drain time without modification.
    """
    if not event_url:

        event_url = external_url_for("events.detail", event_id=event.id)
    formatted: list[tuple[str, str, str]] = [
        (
            _EVENT_FIELD_LABELS.get(field, field),
            _format_event_change_value(field, pair[0]),
            _format_event_change_value(field, pair[1]),
        )
        for field, pair in payload.items()
    ]
    return render_template(
        "email/event_changed.html",
        user_name=user.name,
        event=event,
        event_url=event_url,
        changes=formatted,
        **_base_context(),
    )


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
    html_body = _render_event_changed_body(user, event, changes, event_url)
    enqueue_deferred(
        user=user,
        event=event,
        notification_type="event_changed",
        subject=f"MedCover — Změna akce: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html_body,
        change_type=_EVENT_CHANGED_CHANGE_TYPE,
        change_value=changes,
        event_url=event_url,
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
        change_type=_UNFILLED_REMINDER_CHANGE_TYPE,
        change_value={"unfilled_count": len(unfilled)},
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

    _now_utc = datetime.now(timezone.utc)
    query = (
        db.select(OutboxEmail)
        .where(
            OutboxEmail.status == "pending",
            OutboxEmail.retry_count < OutboxEmail.MAX_RETRIES,
            OutboxEmail.user_id.is_(None),  # legacy non-event rows only
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

    _settings = get_settings()
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
        mail.send(msg)
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
        subject=f"MedCover — debriefing: {event.name}",
        body=_PLAIN_FALLBACK,
        html_body=html,
        change_type=_DEBRIEFING_CHANGE_TYPE,
        change_value={"assignment_id": assignment.id},
    )


# ── Account activation ────────────────────────────────────────────────────────


def send_account_activated(user: UserAccount) -> None:
    """Enqueue an account-activation notification to the newly activated user."""

    login_url = external_url_for("auth.login")
    html_body = render_template("email/account_activated.html", user=user, login_url=login_url, **_base_context())
    _enqueue(
        user.email,
        "MedCover — váš účet byl aktivován",
        _PLAIN_FALLBACK,
        html_body=html_body,
        notification_type="account_activated",
    )


# ── Batched drain ─────────────────────────────────────────────────────────────


def _pick_trigger_batch(now_utc: datetime) -> tuple[object, str] | None:
    """Return the (user_id, to_email) pair whose oldest qualifying matured-pending
    row is earliest in the queue, or None if no batch qualifies.

    Grouping on (user_id, to_email) rather than user_id alone prevents cross-
    recipient leakage: the admin test form can set g._test_notification_email
    to redirect a row to a third-party address while user_id still points at
    the admin, so two rows sharing a user_id may have distinct to_email
    values. They must not be batched into the same email.
    """
    stmt = (
        db.select(OutboxEmail.user_id, OutboxEmail.to_email)
        .where(
            OutboxEmail.status == "pending",
            OutboxEmail.user_id.is_not(None),
            OutboxEmail.retry_count < OutboxEmail.MAX_RETRIES,
            sa.or_(
                OutboxEmail.send_after.is_(None),
                OutboxEmail.send_after <= now_utc,
            ),
        )
        .group_by(OutboxEmail.user_id, OutboxEmail.to_email)
        .order_by(sa.func.min(OutboxEmail.created_at).asc())
        .limit(1)
    )
    row = db.session.execute(stmt).first()
    if row is None:
        return None
    return row.user_id, row.to_email


def _load_batch_for_user(user_id: object, to_email: str) -> list:
    """Load and UPDLOCK-lock all pending rows for the (user_id, to_email) batch
    (matured + immature).."""
    stmt = (
        db.select(OutboxEmail)
        .where(
            OutboxEmail.status == "pending",
            OutboxEmail.user_id == user_id,
            OutboxEmail.to_email == to_email,
        )
        .order_by(OutboxEmail.created_at.asc())
        .with_hint(OutboxEmail, "WITH (UPDLOCK, ROWLOCK)")
    )
    return list(db.session.scalars(stmt).all())


def _row_to_entry(row: OutboxEmail) -> dict:
    """Translate one OutboxEmail row into a template entry dict.
    Dispatches on notification_type + change_type with defensive parsing."""
    ntype = row.notification_type or ""
    ctype = row.change_type or ""

    if ntype == "event_changed" and ctype == _EVENT_CHANGED_CHANGE_TYPE:
        try:
            payload = json.loads(row.change_value or "{}")
        except ValueError, TypeError:
            log.warning("Bad event_changed payload on outbox row id=%s", row.id)
            payload = {}
        changes = [
            (
                _EVENT_FIELD_LABELS.get(field, field),
                _format_event_change_value(field, pair[0]),
                _format_event_change_value(field, pair[1]),
            )
            for field, pair in payload.items()
        ]
        return {"type": "event_changed", "changes": changes}

    if ntype in ("assignment_confirmed", "assignment_released"):
        try:
            payload = json.loads(row.change_value or "{}")
        except ValueError, TypeError:
            log.warning("Missing/bad change_value on outbox row id=%s (type=%s)", row.id, ntype)
            payload = {}
        spot_description = payload.get("spot_description", "") or ""
        return {"type": ntype, "spot_description": spot_description}

    if ntype in (
        "event_published",
        "assignments_opened",
        "event_cancelled",
        "event_archived",
        "event_unarchived",
    ):
        return {"type": ntype}

    if ntype == "unfilled_reminder":
        try:
            payload = json.loads(row.change_value or "{}")
        except ValueError, TypeError:
            log.warning("Missing/bad change_value on outbox row id=%s (unfilled_reminder)", row.id)
            payload = {}
        try:
            count = int(payload.get("unfilled_count", 0))
        except ValueError, TypeError:
            count = 0
        return {"type": "unfilled_reminder", "unfilled_count": count}

    if ntype == "debriefing_invitation":
        try:
            payload = json.loads(row.change_value or "{}")
        except ValueError, TypeError:
            log.warning("Missing/bad change_value on outbox row id=%s (debriefing_invitation)", row.id)
            payload = {}
        assignment_id = payload.get("assignment_id")
        if assignment_id is None:
            log.warning("debriefing outbox row id=%s missing assignment_id", row.id)
            debriefing_url = ""
        else:

            debriefing_url = external_url_for("debriefing.submit", assignment_id=int(assignment_id))
        return {"type": "debriefing_invitation", "debriefing_url": debriefing_url}

    # Fallback — legacy rows or unknown types: inline pre-rendered html_body.
    log.warning(
        "Unknown notification_type=%r change_type=%r for outbox row id=%s — using legacy html_body fallback",
        ntype,
        ctype,
        row.id,
    )
    return {"type": "legacy", "legacy_html": row.html_body or ""}


def _build_event_section(event: object, rows: list) -> dict:
    """Build one event section dict for the batched email template.."""

    return {
        "event_name": event.name,  # type: ignore[attr-defined]
        "event_url": external_url_for("events.detail", event_id=event.id),  # type: ignore[attr-defined]
        "start_datetime_local": event.start_datetime.astimezone(get_app_tz()).strftime(  # type: ignore[attr-defined]
            "%d.%m.%Y %H:%M"
        ),
        "rows": [_row_to_entry(r) for r in rows],
    }


def _write_batch_failure_audit(
    to_email: str,
    subject: str,
    error_str: str,
    rows: list,
) -> None:
    """Write ONE AuditLogEntry for a batch SMTP failure. No commit."""

    try:
        db.session.add(
            AuditLogEntry(
                actor_id=None,
                action_type="email_failed",
                entity_type="OutboxEmail",
                entity_id=str(rows[0].id) if rows else None,
                summary=(f"Dávkový e-mail pro {to_email} ({len(rows)} řádků)" f" se nepodařilo odeslat: {error_str}"),
                changes_json={
                    "to": to_email,
                    "subject": subject,
                    "error": error_str,
                    "row_ids": [r.id for r in rows],
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to write batch failure audit — %s", exc)


def drain_batched_outbox() -> bool:
    """Send one batched email to one triggering recipient.

    Returns True if any state change was made (sent, dropped, failed, skipped),
    False only if no user qualified (queue empty of matured batched rows)..
    """

    now_utc = datetime.now(timezone.utc)
    trigger = _pick_trigger_batch(now_utc)
    if trigger is None:
        return False
    user_id, to_email = trigger

    rows = _load_batch_for_user(user_id, to_email)

    # rows whose event has been hard-deleted (FK set to NULL) are
    # unrecoverable: delete them outright so they don't accumulate.
    live_rows: list[OutboxEmail] = []
    deleted_orphans = 0
    for r in rows:
        if r.event_id is None:
            db.session.delete(r)
            deleted_orphans += 1
        else:
            live_rows.append(r)

    if not live_rows:
        db.session.commit()
        log.info(
            "drain_batched_outbox: user_id=%s to=%s had only deleted-event rows (%d removed)",
            user_id,
            to_email,
            deleted_orphans,
        )
        return True

    # Load recipient display name.

    user_obj = db.session.get(UserAccount, user_id)
    user_name = user_obj.name if user_obj is not None else ""

    # dev email block.
    settings = get_settings()
    if not settings.is_email_allowed(to_email):
        for r in live_rows:
            r.status = "skipped"
            r.last_error = "dev_email_block: recipient not in allowlist"
        db.session.commit()
        log.warning(
            "Batch mail suppressed (dev_email_block): user_id=%s to=%s rows=%d",
            user_id,
            to_email,
            len(live_rows),
        )
        return True

    # Load Event objects — one round trip.
    event_ids = sorted({r.event_id for r in live_rows if r.event_id is not None})
    events = db.session.scalars(db.select(Event).where(Event.id.in_(event_ids))).all()
    events_by_id = {e.id: e for e in events}

    # Group live_rows by event_id; handle rows whose event vanished between load and now.
    grouped: dict = {}
    orphan_rows: list[OutboxEmail] = []
    for r in live_rows:
        if r.event_id in events_by_id:
            grouped.setdefault(r.event_id, []).append(r)
        else:
            db.session.delete(r)
            orphan_rows.append(r)

    active_rows = [r for r in live_rows if r not in orphan_rows]
    if not active_rows:
        db.session.commit()
        return True

    # Sort event sections by event.start_datetime ASC.
    ordered_events = sorted(events_by_id.values(), key=lambda e: e.start_datetime)
    event_sections = [_build_event_section(e, grouped[e.id]) for e in ordered_events if e.id in grouped]

    # Subject line.
    if len(event_sections) == 1:
        subject = f"MedCover — Změny v akci: {ordered_events[0].name}"
    else:
        subject = f"MedCover — Souhrn změn ({len(event_sections)} akcí)"

    ctx = {
        "user_name": user_name,
        "event_sections": event_sections,
        **_base_context(),
    }
    html_body = render_template("email/event_batched.html", **ctx)

    try:
        msg = Message(subject=subject, recipients=[to_email], body=_PLAIN_FALLBACK)
        msg.html = html_body
        if _INSTANCE_ID:
            msg.extra_headers = {"X-MedCover-Instance": _INSTANCE_ID}
        mail.send(msg)
        _now = datetime.now(timezone.utc)
        for r in active_rows:
            r.status = "sent"
            r.sent_at = _now
        db.session.commit()
        log.info(
            "Batch mail sent: user_id=%s to=%s events=%d rows=%d",
            user_id,
            to_email,
            len(event_sections),
            len(active_rows),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        for r in active_rows:
            r.retry_count += 1
            r.last_error = err
            if r.retry_count >= OutboxEmail.MAX_RETRIES:
                r.status = "failed"
        _write_batch_failure_audit(to_email, subject, err, active_rows)
        db.session.commit()
        log.warning(
            "Batch mail failed (user_id=%s to=%s rows=%d) — %s",
            user_id,
            to_email,
            len(active_rows),
            exc,
        )
        return True
