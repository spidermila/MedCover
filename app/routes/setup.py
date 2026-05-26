"""
Setup wizard blueprint — runs once on first install.

Accessible only when AppSettings.setup_complete is False.
After completion, redirects all further /setup/* requests to the dashboard.
Step 3 creates the first admin account (no auth required — no users exist yet).
"""

from __future__ import annotations

import pytz
from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_user
from flask_mail import Message

from app.constants import MIN_PASSWORD_LENGTH
from app.extensions import db, mail
from app.models.settings import AppSettings, get_settings
from app.models.user import UserAccount
from app.models.role import Role, ROLE_PERMISSIONS, Permission

setup_bp = Blueprint("setup", __name__, url_prefix="/setup")

_ALL_TIMEZONES = pytz.common_timezones  # ~400 human-friendly timezone strings


def _guard(settings: AppSettings) -> Response | None:
    """Redirect away if setup is already complete."""
    if settings.setup_complete:
        return redirect(url_for("main.dashboard"))
    return None


# ------------------------------------------------------------------ #
# Step 1 — Organisation                                               #
# ------------------------------------------------------------------ #

@setup_bp.route("/", methods=["GET", "POST"])
@setup_bp.route("/step1", methods=["GET", "POST"])
def step1() -> str | Response:
    settings = get_settings()
    if (redir := _guard(settings)):
        return redir

    if request.method == "POST":
        org_name = request.form.get("org_name", "").strip()
        timezone = request.form.get("timezone", "Europe/Prague")
        if not org_name:
            flash("Zadejte název organizace.", "warning")
        elif timezone not in pytz.all_timezones_set:
            flash("Neplatná časová zóna.", "warning")
        else:
            settings.org_name = org_name
            settings.timezone = timezone
            db.session.commit()
            return redirect(url_for("setup.step2"))

    return render_template("setup/step1_org.html", settings=settings, timezones=_ALL_TIMEZONES)


# ------------------------------------------------------------------ #
# Step 2 — SMTP                                                       #
# ------------------------------------------------------------------ #

@setup_bp.route("/step2", methods=["GET", "POST"])
def step2() -> str | Response:
    settings = get_settings()
    if (redir := _guard(settings)):
        return redir

    if request.method == "POST":
        action = request.form.get("action", "next")

        # Save SMTP fields
        settings.smtp_server = request.form.get("smtp_server", "").strip() or None
        settings.smtp_port = int(request.form.get("smtp_port") or 587)
        settings.smtp_use_tls = "smtp_use_tls" in request.form
        settings.smtp_username = request.form.get("smtp_username", "").strip() or None
        new_pw = request.form.get("smtp_password", "")
        if new_pw:
            settings.set_smtp_password(new_pw)
        settings.smtp_default_sender = request.form.get("smtp_default_sender", "").strip() or None
        db.session.commit()

        # Apply to current app so the test email uses the just-saved values
        settings.apply_to_app(current_app._get_current_object())

        if action == "test":
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
                    flash("Testovací e-mail byl odeslán.", "success")
                except Exception as exc:
                    flash(f"Odeslání se nezdařilo: {exc}", "danger")
            return render_template("setup/step2_smtp.html", settings=settings)

        return redirect(url_for("setup.step3"))

    return render_template("setup/step2_smtp.html", settings=settings)


# ------------------------------------------------------------------ #
# Step 3 — First admin account                                        #
# ------------------------------------------------------------------ #

@setup_bp.route("/step3", methods=["GET", "POST"])
def step3() -> str | Response:
    settings = get_settings()
    if (redir := _guard(settings)):
        return redir

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        error = None
        if not full_name:
            error = "Zadejte celé jméno."
        elif not email:
            error = "Zadejte e-mail."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků."
        elif password != password2:
            error = "Hesla se neshodují."
        elif db.session.scalar(db.select(UserAccount).where(UserAccount.email == email)):
            error = "Účet s tímto e-mailem již existuje."

        if error:
            flash(error, "warning")
            return render_template("setup/step3_admin.html", min_password_length=MIN_PASSWORD_LENGTH)

        # Ensure Admin role and its permissions exist, plus General ME
        _ensure_roles()
        _ensure_general_me()

        admin_role = db.session.scalar(db.select(Role).where(Role.name == "Admin"))
        user = UserAccount(email=email, name=full_name, is_active=True)
        user.set_password(password)
        if admin_role:
            user.roles.append(admin_role)
        db.session.add(user)

        settings.setup_complete = True
        db.session.commit()

        login_user(user)
        flash("Nastavení dokončeno. Vítejte v MedCover!", "success")
        return redirect(url_for("setup.done"))

    return render_template("setup/step3_admin.html", min_password_length=MIN_PASSWORD_LENGTH)


@setup_bp.route("/done")
def done() -> str:
    return render_template("setup/done.html")


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _ensure_roles() -> None:
    """Idempotently create all permissions and roles (mirrors seed_dev.py logic)."""
    from app.models.role import ALL_PERMISSIONS

    for perm_data in ALL_PERMISSIONS:
        if not db.session.scalar(db.select(Permission).where(Permission.code == perm_data["code"])):
            db.session.add(Permission(code=perm_data["code"], description=perm_data.get("description")))
    db.session.flush()

    for role_name, perm_codes in ROLE_PERMISSIONS.items():
        role = db.session.scalar(db.select(Role).where(Role.name == role_name))
        if not role:
            role = Role(name=role_name)
            db.session.add(role)
            db.session.flush()
        existing_codes = {p.code for p in role.permissions}
        for code in perm_codes:
            if code not in existing_codes:
                perm = db.session.scalar(db.select(Permission).where(Permission.code == code))
                if perm:
                    role.permissions.append(perm)


def _ensure_general_me() -> None:
    """Idempotently create the built-in General master event."""
    from app.models.master_event import MasterEvent
    if not db.session.scalar(db.select(MasterEvent).where(MasterEvent.is_general.is_(True))):
        db.session.add(MasterEvent(
            name="Obecné",
            description="Výchozí nadřazená akce pro akce bez specifického zařazení.",
            is_general=True,
        ))
        db.session.flush()
