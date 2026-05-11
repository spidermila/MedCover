from __future__ import annotations

import os
import time as _time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import click
from flask import Flask, redirect, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse
from .extensions import db, migrate, login_manager, mail as _flask_mail, csrf
from .config import config_by_name

_PRAGUE_TZ = ZoneInfo("Europe/Prague")
# Computed once at import/startup; used as a cache-busting version for static files.
_STARTUP_TS: str = str(int(_time.time()))


def create_app(
    config_name: str | None = None,
    db_url: str | None = None,
) -> Flask:
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    # Allow callers (e.g. pytest-xdist workers) to override the DB URL before
    # the extension engines are initialised.
    if db_url is not None:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    _flask_mail.init_app(app)
    csrf.init_app(app)

    # Import models here so Flask-Migrate discovers all tables
    with app.app_context():
        from . import models  # noqa: F401

        # Apply SMTP settings from DB immediately so the scheduler (which never
        # handles HTTP requests and therefore never triggers before_request) gets
        # a correctly configured Flask-Mail on startup.
        try:
            from .models.settings import get_settings
            _settings = get_settings()
            if _settings and _settings.smtp_configured:
                _settings.apply_to_app(app)
        except Exception:
            pass  # DB not ready yet (first migration run)

    from .routes import register_blueprints
    register_blueprints(app)
    register_cli_commands(app)

    @app.context_processor
    def _inject_app_config() -> dict:
        """Inject app config and feature flags into all templates."""
        try:
            from .models.settings import get_settings as _gs
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
        """Convert a UTC datetime to Europe/Prague local time and format it."""
        if dt is None:
            return "—"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PRAGUE_TZ).strftime(fmt)

    _CZECH_DAY_ABBR = ["po", "út", "st", "čt", "pá", "so", "ne"]

    @app.template_filter("czechday")
    def czechday_filter(dt: datetime | None) -> str:
        """Return Czech two-letter weekday abbreviation (po/út/st/čt/pá/so/ne)."""
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _CZECH_DAY_ABBR[dt.astimezone(_PRAGUE_TZ).weekday()]

    @app.template_filter("cznum")
    def cznum_filter(value: object, decimals: int = 1, strip: bool = False) -> str:
        """Format a number using Czech locale: comma as decimal separator.

        strip=True removes trailing zeros after the decimal point
        (e.g. 2.0 → '2', 2.5 → '2,5').
        """
        try:
            formatted = f"{float(value):.{decimals}f}"  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "—"
        if strip:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted.replace(".", ",")

    @app.template_global()
    def audit_entity_url(entity_type: str, entity_id: str) -> str | None:
        """Return a URL to view the given entity, or None if no page exists."""
        try:
            eid_int = int(entity_id)
        except (ValueError, TypeError):
            eid_int = None

        if entity_type == "Event" and eid_int is not None:
            return url_for("events.detail", event_id=eid_int)
        if entity_type == "MasterEvent" and eid_int is not None:
            return url_for("master_events.detail", me_id=eid_int)
        if entity_type == "EquipmentItem" and eid_int is not None:
            return url_for("equipment.item_edit", item_id=eid_int)
        if entity_type == "EquipmentType" and eid_int is not None:
            return url_for("equipment.type_edit", type_id=eid_int)
        if entity_type == "EventTemplate" and eid_int is not None:
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

    @app.after_request
    def _add_security_headers(response: WerkzeugResponse) -> WerkzeugResponse:
        """Add Content-Security-Policy and other security headers."""
        if not app.config.get("TESTING") and not app.config.get("DEBUG"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
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
        from .models.settings import get_settings

        # Allow static files, setup pages, and health probe through unconditionally
        if request.endpoint and (
            request.endpoint.startswith("setup.")
            or request.endpoint == "static"
            or request.endpoint == "main.health"
        ):
            return None

        try:
            settings = get_settings()
        except Exception:
            # DB not ready yet (e.g. running migrations) — let it through
            return None

        if not settings.setup_complete:
            return redirect(url_for("setup.step1"))

        # Keep Flask-Mail config in sync with DB — skip reinit when SMTP settings unchanged.
        fingerprint = (
            settings.smtp_server, settings.smtp_port, settings.smtp_use_tls,
            settings.smtp_username, settings.smtp_password_enc, settings.smtp_default_sender,
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
        import sys
        from sqlalchemy import inspect as sa_inspect

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
        import socket
        import time
        import sys
        from flask_mail import Message
        from app.models.settings import get_settings
        from app.extensions import mail as _flask_mail

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
