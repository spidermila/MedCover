"""
Admin application settings route — lets admins edit org info and SMTP config
after the initial setup wizard has completed.
"""

import pytz
from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from flask_mail import Message

from app.extensions import db, mail
from app.models.settings import get_settings
from app.utils import audit, diff_changes, require_permission

app_settings_bp = Blueprint("app_settings", __name__, url_prefix="/admin/settings")

_ALL_TIMEZONES = pytz.common_timezones


_SETTINGS_FIELDS = [
    "org_name",
    "timezone",
    "app_base_url",
    "feedback_enabled",
    "dev_email_block",
    "dev_email_allowlist",
    "smtp_server",
    "smtp_port",
    "smtp_use_tls",
    "smtp_username",
    "smtp_default_sender",
    "session_timeout_hours",
]


def _parse_settings_form(form: dict) -> dict:
    """Extract settings values from the POST form."""
    return {
        "org_name": form.get("org_name", "").strip() or None,
        "timezone": form.get("timezone", "Europe/Prague"),
        "app_base_url": form.get("app_base_url", "").strip().rstrip("/") or None,
        "feedback_enabled": "feedback_enabled" in form,
        "dev_email_block": "dev_email_block" in form,
        "dev_email_allowlist": form.get("dev_email_allowlist", "").strip() or None,
        "smtp_server": form.get("smtp_server", "").strip() or None,
        "smtp_port": int(form.get("smtp_port") or 587),
        "smtp_use_tls": "smtp_use_tls" in form,
        "smtp_username": form.get("smtp_username", "").strip() or None,
        "smtp_default_sender": form.get("smtp_default_sender", "").strip() or None,
        "session_timeout_hours": int(form.get("session_timeout_hours") or 24),
    }


def _settings_snapshot(settings: object) -> dict:
    """Build a before/after snapshot dict (no secrets)."""
    return {f: getattr(settings, f) for f in _SETTINGS_FIELDS}


@app_settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index() -> str | Response:
    require_permission("admin.manage_settings")
    settings = get_settings()

    if request.method != "POST":
        return render_template("admin/app_settings.html", settings=settings, timezones=_ALL_TIMEZONES)

    action = request.form.get("action", "save")
    vals = _parse_settings_form(request.form)

    # --- Validate ---
    if vals["timezone"] not in pytz.all_timezones_set:
        flash("Neplatná časová zóna.", "warning")
        return render_template("admin/app_settings.html", settings=settings, timezones=_ALL_TIMEZONES)

    if vals["app_base_url"] and not (
        vals["app_base_url"].startswith("http://") or vals["app_base_url"].startswith("https://")
    ):
        flash("Základní URL aplikace musí začínat http:// nebo https://.", "warning")
        return render_template("admin/app_settings.html", settings=settings, timezones=_ALL_TIMEZONES)

    if vals["session_timeout_hours"] < 1 or vals["session_timeout_hours"] > 8760:
        flash("Platnost přihlášení musí být mezi 1 a 8760 hodinami.", "warning")
        return render_template("admin/app_settings.html", settings=settings, timezones=_ALL_TIMEZONES)

    before = _settings_snapshot(settings)

    # --- Apply changes ---
    for field in _SETTINGS_FIELDS:
        setattr(settings, field, vals[field])
    new_pw = request.form.get("smtp_password", "")
    if new_pw:
        settings.set_smtp_password(new_pw)
    db.session.flush()

    after = _settings_snapshot(settings)

    if action == "test":
        db.session.commit()
        settings.apply_to_app(current_app._get_current_object())
        if not settings.smtp_configured:
            flash("Před odesláním testovacího e-mailu vyplňte SMTP nastavení.", "warning")
        else:
            try:
                msg = Message(
                    subject="MedCover — testovací e-mail",
                    recipients=[settings.smtp_username],
                    body="Tento e-mail potvrzuje, že SMTP nastavení v MedCover je funkční.",
                )
                mail.send(msg)
                flash("Testovací e-mail byl odeslán na adresu " + (settings.smtp_username or ""), "success")
            except Exception as exc:
                flash(f"Odeslání se nezdařilo: {exc}", "danger")
        return render_template("admin/app_settings.html", settings=settings, timezones=_ALL_TIMEZONES)

    audit("edit", "AppSettings", 1, "Nastavení aplikace bylo upraveno.", diff_changes(before, after))
    db.session.commit()
    settings.apply_to_app(current_app._get_current_object())
    flash("Nastavení bylo uloženo.", "success")
    return redirect(url_for("app_settings.index"))
