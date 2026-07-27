import os
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from itertools import groupby as itertools_groupby
from operator import attrgetter

import click
from flask import Flask, g, redirect, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from .config import config_by_name
from .db_auth import attach_msi_token_auth, prepare_msi_auth
from .extensions import csrf, db, login_manager
from .extensions import mail as _flask_mail
from .extensions import migrate
from .models.assignment import Assignment

# Computed once at import/startup; used as a cache-busting version for static files.
_STARTUP_TS: str = str(int(_time.time()))


def create_app(
    config_name: str | None = None,
    db_url: str | None = None,
    engine_options: dict | None = None,
) -> Flask:
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    # Allow callers (e.g. pytest-xdist workers) to override the DB URL before
    # the extension engines are initialised.
    if db_url is not None:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url

    if engine_options is not None:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options

    # Azure SQL managed-identity auth: rewrite an ActiveDirectoryMsi URL to a
    # token-friendly form before the engine is built (no-op for SQL-auth URLs
    # used by dev/tests). The matching do_connect listener is attached below,
    # before any DB access. See app/db_auth.py.
    prepare_msi_auth(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    _flask_mail.init_app(app)
    csrf.init_app(app)

    attach_msi_token_auth(app, db)

    # Import models here so Flask-Migrate discovers all tables
    with app.app_context():
        from . import models  # noqa: F401 # pylint: disable=import-outside-toplevel

        # Apply SMTP settings from DB immediately so the scheduler (which never
        # handles HTTP requests and therefore never triggers before_request) gets
        # a correctly configured Flask-Mail on startup.
        try:
            from .models.settings import get_settings  # pylint: disable=import-outside-toplevel

            _settings = get_settings()
            if _settings and _settings.smtp_configured:
                _settings.apply_to_app(app)
        except Exception:
            pass  # DB not ready yet (first migration run)

    from .routes import register_blueprints  # pylint: disable=import-outside-toplevel

    register_blueprints(app)
    register_cli_commands(app)

    @app.context_processor
    def _inject_app_config() -> dict:
        """Inject app config and feature flags into all templates."""
        try:
            from .models.settings import get_settings as _gs  # pylint: disable=import-outside-toplevel

            _s = _gs()
            feedback_enabled = _s.feedback_enabled
        except Exception:
            feedback_enabled = True
        # Cache-busting version for static files: git commit hash in production,
        # process start time in dev (changes on every container/server restart).
        _git = app.config.get("GIT_COMMIT", "dev")
        static_ver: str = _git if (_git and _git != "dev") else _STARTUP_TS
        return {"config": app.config, "feedback_enabled": feedback_enabled, "static_ver": static_ver}

    @app.template_filter("localdt")
    def localdt_filter(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        """Convert a UTC datetime to app-configured local time and format it."""
        if dt is None:
            return "—"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from .utils import get_app_tz  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        return dt.astimezone(get_app_tz()).strftime(fmt)

    @app.template_filter("localdate")
    def localdate_filter(dt: datetime | None) -> str:
        """Like localdt but date-only (dd.mm.yyyy)."""
        return localdt_filter(dt, fmt="%d.%m.%Y")

    @app.template_filter("midpoint_iso")
    def midpoint_iso_filter(event: object) -> str:
        """Return the midpoint between event.start_datetime and event.end_datetime
        formatted as 'YYYY-MM-DDTHH:MM' for a datetime-local input."""
        from app.models.event import Event as _Event  # pylint: disable=import-outside-toplevel

        if not isinstance(event, _Event):
            return ""
        start = event.start_datetime
        end = event.end_datetime
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        mid = start + (end - start) / 2
        from .utils import get_app_tz  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        return mid.astimezone(get_app_tz()).strftime("%Y-%m-%dT%H:%M")

    _CZECH_DAY_ABBR = ["po", "út", "st", "čt", "pá", "so", "ne"]

    @app.template_filter("czechday")
    def czechday_filter(dt: datetime | None) -> str:
        """Return Czech two-letter weekday abbreviation (po/út/st/čt/pá/so/ne)."""
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from .utils import get_app_tz  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        return _CZECH_DAY_ABBR[dt.astimezone(get_app_tz()).weekday()]

    @app.template_filter("cznum")
    def cznum_filter(value: object, decimals: int = 1, strip: bool = False) -> str:
        """Format a number using Czech locale: comma as decimal separator.

        strip=True removes trailing zeros after the decimal point
        (e.g. 2.0 → '2', 2.5 → '2,5').
        """
        try:
            formatted = f"{float(value):.{decimals}f}"  # type: ignore[arg-type]
        except TypeError, ValueError:
            return "—"
        if strip:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted.replace(".", ",")

    @app.template_filter("groupby_safe")
    def groupby_safe_filter(value: list, attribute: str) -> list:
        """Like Jinja2 groupby but handles None attribute values without crashing."""

        getter = attrgetter(attribute)

        def key_func(item: object) -> str:
            val = getter(item)
            return val if val is not None else ""

        sorted_items = sorted(value, key=key_func)
        return [(k, list(g)) for k, g in itertools_groupby(sorted_items, key=key_func)]

    @app.template_global()
    def audit_entity_url(entity_type: str, entity_id: str) -> str | None:
        """Return a URL to view the given entity, or None if no page exists."""
        try:
            eid_int = int(entity_id)
        except ValueError, TypeError:
            eid_int = None

        if eid_int is not None:
            if entity_type == "Event":
                return url_for("events.detail", event_id=eid_int)
            if entity_type == "Assignment":
                asgn = db.session.get(Assignment, eid_int)
                if asgn and asgn.spot:
                    return url_for("events.detail", event_id=asgn.spot.event_id)
                return None
            if entity_type == "MasterEvent":
                return url_for("master_events.detail", me_id=eid_int)
            if entity_type == "EquipmentItem":
                return url_for("equipment.item_edit", item_id=eid_int)
            if entity_type == "EquipmentType":
                return url_for("equipment.type_edit", type_id=eid_int)
            if entity_type == "EventTemplate":
                return url_for("templates.edit", template_id=eid_int)
        if entity_type == "AppSettings":
            return url_for("app_settings.index")
        if entity_type == "Qualification":
            return url_for("qualifications.index")
        if entity_type == "RegistrationInvite":
            return url_for("users.invites")
        if entity_type == "UserAccount" and entity_id:
            return url_for("users.detail", user_id=entity_id)
        return None

    @app.before_request
    def _set_csp_nonce() -> None:
        g.csp_nonce = secrets.token_hex(16)

    @app.after_request
    def _add_security_headers(response: WerkzeugResponse) -> WerkzeugResponse:
        """Add Content-Security-Policy and other security headers."""
        if not app.config.get("TESTING") and not app.config.get("DEBUG"):
            nonce = getattr(g, "csp_nonce", "")
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net data:; "
                "img-src 'self' data:; "
                "connect-src 'self' https://cdn.jsdelivr.net;"
            )
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.headers.setdefault(
                "Permissions-Policy",
                "geolocation=(), microphone=(), camera=()",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    @app.before_request
    def _setup_guard() -> WerkzeugResponse | None:
        """
        Redirect to the setup wizard if initial setup is not complete.
        Skips static files, the setup blueprint itself, and auth routes
        (so the app doesn't get into a redirect loop before the DB is seeded).
        """
        from .models.settings import get_settings  # pylint: disable=import-outside-toplevel

        # Allow static files, setup pages, and health probe through unconditionally
        if request.endpoint and (
            request.endpoint.startswith("setup.") or request.endpoint == "static" or request.endpoint == "main.health"
        ):
            return None

        try:
            settings = get_settings()
        except Exception:
            # DB not ready yet (e.g. running migrations) — let it through
            return None

        if not settings.setup_complete:
            return redirect(url_for("setup.step1"))

        # Keep session lifetime in sync with DB setting.
        app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=settings.session_timeout_hours)

        # Keep Flask-Mail config in sync with DB — skip reinit when SMTP settings unchanged.
        fingerprint = (
            settings.smtp_server,
            settings.smtp_port,
            settings.smtp_use_tls,
            settings.smtp_username,
            settings.smtp_password_enc,
            settings.smtp_default_sender,
        )
        if app.config.get("_SMTP_FINGERPRINT") != fingerprint:
            settings.apply_to_app(app)
            app.config["_SMTP_FINGERPRINT"] = fingerprint
        return None

    return app


def register_cli_commands(app: Flask) -> None:
    """Register custom Flask CLI commands."""

    @app.cli.command("verify-schema")
    def verify_schema() -> None:
        """
        Verify that every SQLAlchemy model table and column exists in the DB.

        Run this after 'flask db upgrade' to catch schema drift (e.g. a migration
        that updated alembic_version but failed to apply the DDL).
        Exits non-zero and prints a clear error if anything is missing.
        """
        import sys  # pylint: disable=import-outside-toplevel

        from sqlalchemy import inspect as sa_inspect  # pylint: disable=import-outside-toplevel

        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())

        errors: list[str] = []
        table_count = 0

        for table_name, table in db.metadata.tables.items():
            if table_name == "alembic_version":
                continue

            if table_name not in existing_tables:
                errors.append(f"MISSING TABLE: {table_name}")
                continue

            table_count += 1
            existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name not in existing_cols:
                    errors.append(f"MISSING COLUMN: {table_name}.{col.name}")

        if errors:
            print("Schema verification FAILED — the following objects are missing from the DB:")
            for err in errors:
                print(f"  ✘ {err}")
            print(
                "\nPossible causes: migration was stamped but not applied "
                "('flask db stamp'), DB restored from incomplete backup, "
                "or a migration script had an error."
            )
            print("Fix: drop alembic_version and re-run 'flask db upgrade'.")
            sys.exit(1)
        else:
            print(f"Schema OK — verified {table_count} tables, all columns present.")

    @app.cli.command("send-test-email")
    @click.argument("to_email")
    def send_test_email(to_email: str) -> None:
        """Send a live test email to TO_EMAIL to verify the SMTP configuration.

        Bypasses the outbox queue and sends immediately so you get instant
        feedback on whether the SMTP credentials and relay are working.

        Example:
            docker compose exec web flask send-test-email <your-address@domain.com>
        """
        import socket  # pylint: disable=import-outside-toplevel
        import sys  # pylint: disable=import-outside-toplevel
        import time  # pylint: disable=import-outside-toplevel

        from flask_mail import Message  # pylint: disable=import-outside-toplevel

        from app.extensions import mail as _flask_mail  # pylint: disable=import-outside-toplevel
        from app.models.settings import get_settings  # pylint: disable=import-outside-toplevel

        settings = get_settings()
        if not settings.smtp_configured:
            print("✘ SMTP is not configured in AppSettings. Run the setup wizard first.")
            sys.exit(1)

        print(f"SMTP server : {settings.smtp_server}:{settings.smtp_port}")
        print(f"Username    : {settings.smtp_username}")
        print(f"TLS         : {settings.smtp_use_tls}")
        print(f"Sender      : {settings.smtp_default_sender}")
        print(f"Recipient   : {to_email}")
        print()

        # Apply current SMTP settings to app config
        settings.apply_to_app(app)

        # TCP reachability check
        print(f"1/2  Checking TCP connectivity to {settings.smtp_server}:{settings.smtp_port} …", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            with socket.create_connection((settings.smtp_server, settings.smtp_port), timeout=5):
                ms = int((time.monotonic() - t0) * 1000)
                print(f"OK ({ms} ms)")
        except (TimeoutError, OSError) as exc:
            print(f"FAILED — {exc}")
            sys.exit(1)

        # Actual SMTP send
        print("2/2  Sending test email via SMTP …", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            msg = Message(
                subject="MedCover — Test SMTP",
                recipients=[to_email],
                body=(
                    "Tento e-mail byl odeslán jako test konfigurace SMTP.\n\n"
                    f"Server:   {settings.smtp_server}:{settings.smtp_port}\n"
                    f"Odesílatel: {settings.smtp_default_sender}\n"
                    f"TLS: {'ano' if settings.smtp_use_tls else 'ne'}\n\n"
                    "Pokud jste tento e-mail obdrželi, konfigurace SMTP je správná."
                ),
            )
            _flask_mail.send(msg)
            ms = int((time.monotonic() - t0) * 1000)
            print(f"OK ({ms} ms)")
            print(f"\n✔ Test email successfully sent to {to_email}")
        except Exception as exc:  # noqa: BLE001
            ms = int((time.monotonic() - t0) * 1000)
            print(f"FAILED ({ms} ms) — {exc}")
            sys.exit(1)
