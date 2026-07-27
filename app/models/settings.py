import base64
import hashlib
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from flask import Flask, current_app

from app.extensions import db


def _fernet() -> Fernet:
    """Derive a Fernet key from the app SECRET_KEY using SHA-256.

    SHA-256 always produces exactly 32 bytes regardless of SECRET_KEY length,
    eliminating the risk of a short key being padded/truncated insecurely.
    """
    digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class AppSettings(db.Model):  # type: ignore[misc]
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)  # always id=1

    # --- Organisation ---
    org_name = db.Column(db.String(255), nullable=True)
    timezone = db.Column(db.String(64), default="Europe/Prague", nullable=False)
    # External base URL used when building absolute links in e-mails.
    # Example: "https://medcoverdev.spidermila.site"  (no trailing slash)
    app_base_url = db.Column(db.String(512), nullable=True)

    # --- SMTP ---
    smtp_server = db.Column(db.String(255), nullable=True)
    smtp_port = db.Column(db.Integer, default=587, nullable=False)
    smtp_use_tls = db.Column(db.Boolean, default=True, nullable=False)
    smtp_username = db.Column(db.String(255), nullable=True)
    smtp_password_enc = db.Column(db.Text, nullable=True)  # Fernet-encrypted
    smtp_default_sender = db.Column(db.String(255), nullable=True)

    # --- Dev / testing ---
    # When True, outgoing emails are silently suppressed unless the recipient is
    # listed in dev_email_allowlist.  Useful on staging/dev instances where real
    # user addresses were imported and must never receive notifications.
    dev_email_block = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    # Comma-separated list of email addresses that bypass the dev_email_block.
    # Example: "admin@example.com, tester@example.com"
    dev_email_allowlist = db.Column(db.Text, nullable=True)

    # --- Backup ---
    # Directory (relative to project root or absolute) where backup .zip files are stored.
    backup_dir = db.Column(db.String(512), default="backups", nullable=False, server_default="backups")
    # Maximum number of backup files to keep; oldest are pruned automatically.
    backup_keep_count = db.Column(db.Integer, default=7, nullable=False, server_default="7")
    # When True, the scheduler will create an automatic daily backup.
    backup_schedule_enabled = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    # Hour of day (0–23, server local time) at which the scheduled backup runs.
    backup_schedule_hour = db.Column(db.Integer, default=2, nullable=False, server_default="2")

    # --- Session security ---
    # How long a login session lasts (hours). Flask sets a cookie with this lifetime.
    session_timeout_hours = db.Column(db.Integer, default=24, nullable=False, server_default="24")

    # --- Notification toggles ---
    # Auth emails (invite, password reset, account activation) are always sent
    # and cannot be toggled.  The flags below control operational notifications.
    notify_assignment = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_event_published = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_assignments_opened = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_event_cancelled = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_event_changed = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_unfilled_reminder = db.Column(db.Boolean, default=True, nullable=False, server_default="true")
    notify_debriefing = db.Column(db.Boolean, default=True, nullable=False, server_default="true")

    # --- Notification delay tiers (issue #268) ---
    # Minutes to hold event-related notifications before sending, chosen by
    # proximity of event.start_datetime to now. See enqueue_deferred().
    notify_delay_under_24h_min = db.Column(db.Integer, default=5, nullable=False, server_default="5")
    notify_delay_1_7_days_min = db.Column(db.Integer, default=60, nullable=False, server_default="60")
    notify_delay_1_4_weeks_min = db.Column(db.Integer, default=360, nullable=False, server_default="360")
    notify_delay_over_month_min = db.Column(db.Integer, default=1440, nullable=False, server_default="1440")

    # --- Lifecycle ---
    setup_complete = db.Column(db.Boolean, default=False, nullable=False)
    feedback_enabled = db.Column(db.Boolean, default=True, nullable=False)
    scheduler_last_seen = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ------------------------------------------------------------------ #
    # SMTP password helpers                                                #
    # ------------------------------------------------------------------ #

    def set_smtp_password(self, plaintext: str) -> None:
        if plaintext:
            self.smtp_password_enc = _fernet().encrypt(plaintext.encode()).decode()
        else:
            self.smtp_password_enc = None

    def get_smtp_password(self) -> str | None:
        if not self.smtp_password_enc:
            return None
        try:
            return _fernet().decrypt(self.smtp_password_enc.encode()).decode()
        except Exception:
            # Key mismatch (e.g. SECRET_KEY changed) — treat as no password
            return None

    # ------------------------------------------------------------------ #
    # Convenience                                                          #
    # ------------------------------------------------------------------ #

    def is_email_allowed(self, address: str) -> bool:
        """Return True if *address* may receive email given current dev settings.

        When dev_email_block is False (default/production), all addresses are
        allowed.  When True, only addresses listed in dev_email_allowlist pass.
        """
        if not self.dev_email_block:
            return True
        if not self.dev_email_allowlist:
            return False  # block is on but allowlist is empty → block all
        allowed = {a.strip().lower() for a in self.dev_email_allowlist.split(",") if a.strip()}
        return address.strip().lower() in allowed

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_server and self.smtp_username and self.smtp_password_enc)

    def apply_to_app(self, app: Flask) -> None:
        """Push settings into Flask-Mail config so mail is sent using DB values.

        Port 465 uses implicit SSL (SMTP_SSL); port 587/25 use STARTTLS.
        Flask-Mail caches config in a _Mail state object at init_app time, so we
        must call mail.init_app(app) again after updating app.config to regenerate
        the cached state.
        """
        use_ssl = self.smtp_port == 465
        app.config["MAIL_SERVER"] = self.smtp_server
        app.config["MAIL_PORT"] = self.smtp_port
        app.config["MAIL_USE_SSL"] = use_ssl
        app.config["MAIL_USE_TLS"] = self.smtp_use_tls and not use_ssl
        app.config["MAIL_USERNAME"] = self.smtp_username
        app.config["MAIL_PASSWORD"] = self.get_smtp_password()
        app.config["MAIL_DEFAULT_SENDER"] = self.smtp_default_sender

        # Reinitialise Flask-Mail so the cached _Mail state picks up new values
        from app.extensions import mail as _mail  # pylint: disable=import-outside-toplevel

        _mail.init_app(app)

    def __repr__(self) -> str:
        return f"<AppSettings org={self.org_name!r} setup_complete={self.setup_complete}>"


# ------------------------------------------------------------------ #
# Module-level helper — safe to call from any request context         #
# ------------------------------------------------------------------ #


def get_settings() -> AppSettings:
    """Return the single AppSettings row, creating it if it doesn't exist yet.

    The result is cached on ``flask.g`` for the duration of the request so that
    multiple callers within the same request share a single DB hit.  Outside a
    request context (e.g. the scheduler) the row is fetched directly each time.
    """

    def _fetch() -> AppSettings:
        row = db.session.get(AppSettings, 1)
        if row is None:
            row = AppSettings(id=1)
            db.session.add(row)
            db.session.commit()
        return row

    try:
        from flask import g  # pylint: disable=import-outside-toplevel

        if not hasattr(g, "_medcover_settings"):
            g._medcover_settings = _fetch()
        return g._medcover_settings
    except RuntimeError:
        # No active request context (scheduler, CLI) — fetch directly.
        return _fetch()
