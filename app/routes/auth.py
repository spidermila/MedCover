from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.constants import MIN_PASSWORD_LENGTH
from app.extensions import db
from app.models.invite import RegistrationInvite
from app.models.role import Role
from app.models.user import UserAccount
from app.models.audit import AuditLogEntry
from app.utils import external_url_for, safe_next
from app.config import LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

_RESET_SALT = "pw-reset"
_INVITE_SALT = "invite"


def _send_mail(to: str, subject: str, template: str, **ctx: Any) -> None:
    """Render an HTML email template and enqueue it via the outbox."""
    from flask import render_template as rt
    from app.mail import _enqueue, _base_context  # noqa: PLC0415

    html_body = rt(template, **_base_context(), **ctx)
    _enqueue(to, subject, "Tento e-mail obsahuje formátovaný obsah. Otevřete jej v e-mailovém klientovi s podporou HTML.", html_body=html_body)


def _make_signed_token(payload: str, salt: str, max_age_seconds: int) -> str:
    from itsdangerous import URLSafeTimedSerializer

    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    return s.dumps(payload, salt=salt)


def _load_signed_token(token: str, salt: str, max_age_seconds: int) -> str | None:
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        return s.loads(token, salt=salt, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == email))
        now = datetime.now(timezone.utc)

        if user:
            # If lockout window has expired, reset the counter automatically
            if user.login_locked_until and user.login_locked_until <= now:
                user.failed_login_attempts = 0
                user.login_locked_until = None

            # Enforce active lockout before checking the password
            if user.login_locked_until and user.login_locked_until > now:
                flash(
                    "Příliš mnoho neúspěšných pokusů o přihlášení. "
                    "Přihlášení je dočasně zablokováno. Zkuste to za chvíli.",
                    "danger",
                )
                return render_template("auth/login.html")

            if user.check_password(password):
                if user.is_archived:
                    flash("Váš účet byl archivován. Kontaktujte administrátora.", "danger")
                    return redirect(url_for("auth.login"))
                if not user.is_active:
                    flash("Váš účet čeká na aktivaci administrátorem.", "warning")
                    return redirect(url_for("auth.login"))
                # Successful login — reset lockout state
                user.failed_login_attempts = 0
                user.login_locked_until = None
                user.last_login_at = now
                db.session.commit()
                session.permanent = True
                login_user(user)
                return redirect(safe_next(request.args.get("next")))

            # Failed attempt — increment counter and possibly lock
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= LOGIN_MAX_ATTEMPTS:
                user.login_locked_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
            db.session.commit()

        flash("Nesprávný e-mail nebo heslo.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout() -> Response:
    logout_user()
    flash("Byli jste odhlášeni.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password() -> str | Response:
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == email))
        # Always show the same message to prevent user enumeration.
        flash("Pokud je e-mail registrován, byl odeslán odkaz pro obnovení hesla.", "info")
        if user and not user.is_archived:
            import secrets
            from app.config import RESET_TOKEN_MINUTES

            nonce = secrets.token_hex(16)
            user.password_reset_nonce = nonce
            token = _make_signed_token(f"{user.id}:{nonce}", _RESET_SALT, RESET_TOKEN_MINUTES * 60)
            reset_url = external_url_for("auth.reset_password", token=token)
            _send_mail(
                to=user.email,
                subject="MedCover — obnovení hesla",
                template="email/reset_password.html",
                reset_url=reset_url,
                minutes=RESET_TOKEN_MINUTES,
            )
            db.session.add(AuditLogEntry(
                actor_id=user.id,
                action_type="password_reset_requested",
                entity_type="UserAccount",
                entity_id=str(user.id),
                summary=f"Požadavek na obnovení hesla pro {user.email}",
                changes_json={},
            ))
            db.session.commit()
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str) -> str | Response:
    from app.config import RESET_TOKEN_MINUTES
    import uuid as _uuid

    payload = _load_signed_token(token, _RESET_SALT, RESET_TOKEN_MINUTES * 60)
    if not payload or ":" not in payload:
        return render_template("auth/reset_invalid.html"), 400

    user_id_str, nonce = payload.split(":", 1)
    user = db.session.get(UserAccount, _uuid.UUID(user_id_str))

    # Invalid if user not found, nonce already cleared (used), or nonce mismatch
    if not user or not user.password_reset_nonce or user.password_reset_nonce != nonce:
        return render_template("auth/reset_invalid.html"), 400

    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků.", "warning")
        elif password != password2:
            flash("Hesla se neshodují.", "warning")
        else:
            user.set_password(password)
            user.password_reset_nonce = None  # invalidate link immediately
            db.session.add(AuditLogEntry(
                actor_id=user.id,
                action_type="password_reset_completed",
                entity_type="UserAccount",
                entity_id=str(user.id),
                summary=f"Heslo obnoveno pro {user.email}",
                changes_json={},
            ))
            db.session.commit()
            flash("Heslo bylo změněno. Přihlaste se.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", min_password_length=MIN_PASSWORD_LENGTH)


@auth_bp.route("/register/<token>", methods=["GET", "POST"])
def register(token: str) -> str | Response:
    invite = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.token == token))
    if not invite or not invite.is_valid:
        flash("Pozvánka je neplatná nebo vypršela.", "danger")
        return redirect(url_for("auth.login"))

    if request.method == "GET" and invite.link_clicked_at is None:
        invite.link_clicked_at = datetime.now(timezone.utc)
        db.session.add(AuditLogEntry(
            actor_id=None,
            action_type="link_clicked",
            entity_type="RegistrationInvite",
            entity_id=str(invite.id),
            summary=f"Registrační odkaz otevřen pro {invite.email}",
            changes_json={},
        ))
        db.session.commit()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not full_name:
            flash("Zadejte celé jméno.", "warning")
        elif len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků.", "warning")

        elif password != password2:
            flash("Hesla se neshodují.", "warning")
        elif db.session.scalar(db.select(UserAccount).where(UserAccount.email == invite.email)):
            flash("Účet s tímto e-mailem již existuje.", "danger")
        else:
            user = UserAccount(email=invite.email, name=full_name, is_active=True)
            user.set_password(password)
            member_role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            if member_role:
                user.roles.append(member_role)
            invite.used_at = datetime.now(timezone.utc)
            db.session.add(user)
            db.session.flush()
            db.session.add(AuditLogEntry(
                actor_id=user.id,
                action_type="complete",
                entity_type="RegistrationInvite",
                entity_id=str(invite.id),
                summary=f"Registrace dokončena pro {invite.email} jako '{full_name}'",
                changes_json={},
            ))
            db.session.commit()
            from app.mail import send_account_activated  # noqa: PLC0415
            send_account_activated(user)
            flash("Registrace dokončena. Nyní se můžete přihlásit.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/register.html", invite=invite, min_password_length=MIN_PASSWORD_LENGTH)
