from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from flask import (
    Blueprint, Response, abort, flash,
    redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.user import UserAccount, CalendarView
from app.models.role import Role
from app.models.invite import RegistrationInvite
from app.models.outbox import OutboxEmail
from app.models.audit import AuditLogEntry
from sqlalchemy import collate
from app.utils import CS_COLLATION, czech_sort_key, audit, diff_changes, external_url_for, get_or_404, require_permission
from app.config import INVITE_TOKEN_HOURS
from app.constants import MIN_PASSWORD_LENGTH

users_bp = Blueprint("users", __name__, url_prefix="/users")

_PAGE_SIZE = 30

# Phone: 9 bare digits, OR +/00 followed by 10-15 digits (spaces stripped before check)
_PHONE_RE = re.compile(r"^\d{9}$|^(\+|00)\d{10,15}$")
# Email: basic structural check — local@domain.tld
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_phone(raw: str) -> bool:
    """Return True if raw phone is empty (optional) or matches allowed formats."""
    stripped = raw.strip().replace(" ", "")
    return stripped == "" or bool(_PHONE_RE.match(stripped))


# ── Own profile ───────────────────────────────────────────────────────────────

@users_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile() -> str | Response:
    user: UserAccount = current_user  # type: ignore[assignment]

    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "profile":
            return _update_profile(user)
        if action == "password":
            return _change_password(user)
    from app.models.equipment import EquipmentItem
    issued_items = db.session.scalars(
        db.select(EquipmentItem).where(EquipmentItem.issued_to_id == user.id)
    ).all()
    from app.models.assignment import Assignment
    from app.models.event import EventSpot, Event, EventStatus
    now = datetime.now(timezone.utc)
    upcoming = db.session.scalars(
        db.select(Assignment)
        .join(Assignment.spot)
        .join(EventSpot.event)
        .where(
            Assignment.user_id == user.id,
            Event.start_datetime >= now,
            Event.status != EventStatus.CANCELLED,
        )
        .order_by(Event.start_datetime)
        .options(selectinload(Assignment.spot).selectinload(EventSpot.event))  # type: ignore[arg-type]
        .limit(10)
    ).all()
    from app.utils import external_url_for
    # Lazy-init iCal token on first profile visit.
    token_created = False
    if not user.ical_token:
        user.regenerate_ical_token()
        db.session.commit()
        token_created = True
    ical_url = external_url_for("calendar.feed", token=user.ical_token)
    return render_template(
        "users/profile.html",
        user=user,
        calendar_views=CalendarView,
        issued_items=issued_items,
        upcoming=upcoming,
        ical_url=ical_url,
        token_created=token_created,
    )


def _update_profile(user: UserAccount) -> Response:
    before: dict[str, Any] = {
        "name": user.name,
        "phone": user.phone,
        "preferred_calendar_view": user.preferred_calendar_view.value if user.preferred_calendar_view else None,
        "dashboard_horizon_days": user.dashboard_horizon_days,
        "dark_mode": user.dark_mode,
    }
    name = request.form.get("name", "").strip()
    if not name:
        flash("Jméno nesmí být prázdné.", "danger")
        return redirect(url_for("users.profile"))
    phone_raw = request.form.get("phone", "").strip()
    if not _validate_phone(phone_raw):
        flash("Neplatný formát telefonního čísla.", "danger")
        return redirect(url_for("users.profile"))
    user.name = name
    user.phone = phone_raw or None
    cv = request.form.get("preferred_calendar_view", CalendarView.LIST.value)
    try:
        user.preferred_calendar_view = CalendarView(cv)
    except ValueError:
        user.preferred_calendar_view = CalendarView.LIST
    try:
        user.dashboard_horizon_days = max(1, min(365, int(request.form.get("dashboard_horizon_days", 30))))
    except ValueError:
        user.dashboard_horizon_days = 30
    user.dark_mode = request.form.get("dark_mode") == "1"
    user.version += 1
    after: dict[str, Any] = {
        "name": user.name,
        "phone": user.phone,
        "preferred_calendar_view": user.preferred_calendar_view.value,
        "dashboard_horizon_days": user.dashboard_horizon_days,
        "dark_mode": user.dark_mode,
    }
    db.session.add(AuditLogEntry(
        actor_id=user.id,
        action_type="edit",
        entity_type="UserAccount",
        entity_id=str(user.id),
        summary=f"Uživatel {user.name} upravil svůj profil",
        changes_json=diff_changes(before, after),
    ))
    db.session.commit()
    flash("Profil byl uložen.", "success")
    return redirect(url_for("users.profile"))


def _change_password(user: UserAccount) -> Response:
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not user.check_password(current_pw):
        flash("Současné heslo je nesprávné.", "danger")
        return redirect(url_for("users.profile"))
    if len(new_pw) < MIN_PASSWORD_LENGTH:
        flash("Nové heslo musí mít alespoň 8 znaků.", "danger")
        return redirect(url_for("users.profile"))
    if new_pw != confirm:
        flash("Hesla se neshodují.", "danger")
        return redirect(url_for("users.profile"))
    user.set_password(new_pw)
    user.version += 1
    db.session.add(AuditLogEntry(
        actor_id=user.id,
        action_type="edit",
        entity_type="UserAccount",
        entity_id=str(user.id),
        summary=f"Uživatel {user.name} změnil heslo",
        changes_json={},  # passwords never logged
    ))
    db.session.commit()
    flash("Heslo bylo změněno.", "success")
    return redirect(url_for("users.profile"))


# ── iCal guides ──────────────────────────────────────────────────────────────

@users_bp.route("/profile/ical-guide/google")
@login_required
def ical_guide_google() -> str:
    return render_template("users/ical_guide_google.html")


# ── Admin: user list ──────────────────────────────────────────────────────────

@users_bp.route("/")
@login_required
def index() -> str:
    require_permission("user.view")
    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "name")
    sort_dir = request.args.get("dir", "asc")
    role_filter = request.args.get("role", "").strip()  # role name or "" for all
    show_archived = request.args.get("archived") == "1"
    if show_archived and not current_user.has_permission("user.view_archived"):
        abort(403)

    if sort not in ("name", "email", "status", "created", "last_login"):
        sort = "name"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    sort_col = {
        "name":       UserAccount.name,
        "email":      UserAccount.email,
        "status":     UserAccount.is_active,
        "created":    UserAccount.created_at,
        "last_login": UserAccount.last_login_at,
    }[sort]
    if sort == "last_login":
        order = sort_col.asc().nulls_last() if sort_dir == "asc" else sort_col.desc().nulls_last()
    else:
        order = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    from app.models.user import user_roles as user_roles_table
    query = db.select(UserAccount).order_by(order)
    if not show_archived:
        query = query.where(UserAccount.is_archived.is_(False))
    else:
        query = query.where(UserAccount.is_archived.is_(True))
    if q:
        query = query.where(
            db.or_(
                UserAccount.name.ilike(f"%{q}%"),
                UserAccount.email.ilike(f"%{q}%"),
            )
        )
    if role_filter:
        query = query.join(
            user_roles_table, UserAccount.id == user_roles_table.c.user_id
        ).join(
            Role, user_roles_table.c.role_id == Role.id
        ).where(Role.name == role_filter)

    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    users = db.session.scalars(
        query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)
    ).all()
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    roles = db.session.scalars(db.select(Role).order_by(collate(Role.name, CS_COLLATION))).all()
    return render_template(
        "users/index.html",
        users=users,
        page=page,
        total=total,
        total_pages=total_pages,
        q=q,
        roles=roles,
        sort=sort,
        sort_dir=sort_dir,
        role_filter=role_filter,
        show_archived=show_archived,
    )


@users_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_user() -> str | Response:
    """Manually create a new active user account (no invite required)."""
    require_permission("user.create")

    from app.models.qualification import Qualification
    import secrets

    all_roles = db.session.scalars(db.select(Role).order_by(collate(Role.name, CS_COLLATION))).all()
    all_qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    if request.method == "GET":
        return render_template("users/create.html", all_roles=all_roles, all_qualifications=all_qualifications)

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone_raw = request.form.get("phone", "").strip()

    if not name:
        flash("Jméno nesmí být prázdné.", "danger")
        return render_template("users/create.html", all_roles=all_roles, all_qualifications=all_qualifications)
    if not email or not _EMAIL_RE.match(email):
        flash("Zadejte platný e-mail.", "danger")
        return render_template("users/create.html", all_roles=all_roles, all_qualifications=all_qualifications)
    if not _validate_phone(phone_raw):
        flash("Neplatný formát telefonního čísla.", "danger")
        return render_template("users/create.html", all_roles=all_roles, all_qualifications=all_qualifications)

    duplicate = db.session.scalar(db.select(UserAccount).where(UserAccount.email == email))
    if duplicate:
        flash(f"E-mail {email} je již použit jiným uživatelem.", "danger")
        return render_template("users/create.html", all_roles=all_roles, all_qualifications=all_qualifications)

    # Create account with a random unusable password — user must use forgot-password to set one.
    user = UserAccount(
        name=name,
        email=email,
        phone=phone_raw or None,
        is_active=True,
    )
    user.set_password(secrets.token_hex(32))
    db.session.add(user)
    db.session.flush()  # get user.id before commit

    if current_user.has_permission("user.assign_role"):
        role_ids = [int(r) for r in request.form.getlist("role_ids")]
        _apply_role_update(user, role_ids)

    if current_user.has_permission("user.assign_qualification"):
        cred_ids = [int(c) for c in request.form.getlist("qualification_ids")]
        _apply_qualification_update(user, cred_ids)

    audit("create", "UserAccount", user.id, f"Manuálně vytvořen účet {user.name} ({user.email})", {})
    db.session.commit()
    flash(f"Uživatel {user.name} byl vytvořen. Uživatel si může nastavit heslo přes 'Zapomenuté heslo'.", "success")
    return redirect(url_for("users.detail", user_id=user.id))


@users_bp.route("/<uuid:user_id>")
@login_required
def detail(user_id: uuid.UUID) -> str:
    require_permission("user.view")
    user = get_or_404(UserAccount, user_id)
    roles = db.session.scalars(db.select(Role).order_by(collate(Role.name, CS_COLLATION))).all()
    from app.models.qualification import Qualification
    qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    return render_template("users/detail.html", user=user, all_roles=roles, all_qualifications=qualifications)


def _apply_role_update(user: UserAccount, role_ids: list[int]) -> bool:
    """Assign *role_ids* to *user* and audit only when the set actually changes.

    Returns True if the roles were modified, False if they were identical.
    Caller is responsible for version bump.
    """
    before_roles = sorted(r.name for r in user.roles)
    new_roles = db.session.scalars(
        db.select(Role).where(Role.id.in_(role_ids))
    ).all() if role_ids else []
    user.roles = list(new_roles)
    after_roles = sorted(r.name for r in user.roles)
    if before_roles == after_roles:
        return False
    audit("edit", "UserAccount", user.id, f"Role uživatele {user.name} aktualizovány",
          diff_changes({"roles": before_roles}, {"roles": after_roles}))
    return True


def _apply_qualification_update(user: UserAccount, qual_ids: list[int]) -> bool:
    """Assign *qual_ids* to *user* and audit only when the set actually changes.

    Returns True if the qualifications were modified, False if they were identical.
    Caller is responsible for version bump.
    """
    from app.models.qualification import Qualification
    before_quals = sorted((c.name for c in user.qualifications), key=czech_sort_key)
    new_creds = db.session.scalars(
        db.select(Qualification).where(
            Qualification.id.in_(qual_ids),
            Qualification.is_deleted.is_(False),
        )
    ).all() if qual_ids else []
    user.qualifications = list(new_creds)
    after_quals = sorted((c.name for c in user.qualifications), key=czech_sort_key)
    if before_quals == after_quals:
        return False
    audit("edit", "UserAccount", user.id, f"Kvalifikace uživatele {user.name} aktualizovány",
          diff_changes({"qualifications": before_quals}, {"qualifications": after_quals}))
    return True


@users_bp.route("/<uuid:user_id>/save", methods=["POST"])
@login_required
def save_user(user_id: uuid.UUID) -> Response:
    """Unified save: info + roles + qualifications + optional admin password set."""
    require_permission("user.edit_any")
    user = get_or_404(UserAccount, user_id)

    # ── Basic info ──────────────────────────────────────────────────────────
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone_raw = request.form.get("phone", "").strip()

    if not name:
        flash("Jméno nesmí být prázdné.", "danger")
        return redirect(url_for("users.detail", user_id=user_id))
    if not email:
        flash("E-mail nesmí být prázdný.", "danger")
        return redirect(url_for("users.detail", user_id=user_id))
    if not _validate_phone(phone_raw):
        flash("Neplatný formát telefonního čísla.", "danger")
        return redirect(url_for("users.detail", user_id=user_id))
    if email != user.email:
        duplicate = db.session.scalar(
            db.select(UserAccount).where(UserAccount.email == email, UserAccount.id != user.id)
        )
        if duplicate:
            flash(f"E-mail {email} je již použit jiným uživatelem.", "danger")
            return redirect(url_for("users.detail", user_id=user_id))

    info_before: dict[str, Any] = {"name": user.name, "email": user.email, "phone": user.phone}
    user.name = name
    user.email = email
    user.phone = phone_raw or None
    info_after: dict[str, Any] = {"name": user.name, "email": user.email, "phone": user.phone}
    info_changed = info_before != info_after
    if info_changed:
        audit("edit", "UserAccount", user.id, f"Admin upravil údaje uživatele {user.name}", diff_changes(info_before, info_after))

    # ── Roles ───────────────────────────────────────────────────────────────
    roles_changed = False
    if current_user.has_permission("user.assign_role"):
        role_ids = [int(r) for r in request.form.getlist("role_ids")]
        roles_changed = _apply_role_update(user, role_ids)

    # ── Qualifications ──────────────────────────────────────────────────────
    quals_changed = False
    if current_user.has_permission("user.assign_qualification"):
        cred_ids = [int(c) for c in request.form.getlist("qualification_ids")]
        quals_changed = _apply_qualification_update(user, cred_ids)

    # ── Admin password set (optional) ───────────────────────────────────────
    password_changed = False
    new_password = request.form.get("new_password", "").strip()
    if new_password:
        if len(new_password) < MIN_PASSWORD_LENGTH:
            flash("Heslo musí mít alespoň 8 znaků.", "danger")
            return redirect(url_for("users.detail", user_id=user_id))
        user.set_password(new_password)
        password_changed = True
        audit("edit", "UserAccount", user.id, f"Admin nastavil heslo pro uživatele {user.name}", {})

    if info_changed or roles_changed or quals_changed or password_changed:
        user.version += 1

    db.session.commit()
    flash("Uživatel byl uložen.", "success")
    return redirect(url_for("users.detail", user_id=user_id))


@users_bp.route("/<uuid:user_id>/activate", methods=["POST"])
@login_required
def activate(user_id: uuid.UUID) -> Response:
    require_permission("user.activate")
    user = get_or_404(UserAccount, user_id)
    user.is_active = True
    user.version += 1
    audit("edit", "UserAccount", user.id, f"Účet {user.name} ({user.email}) byl aktivován", {"is_active": [False, True]})
    db.session.commit()
    _send_activation_email(user)
    flash(f"Účet {user.name} byl aktivován.", "success")
    return redirect(request.referrer or url_for("users.index"))


@users_bp.route("/<uuid:user_id>/deactivate", methods=["POST"])
@login_required
def deactivate(user_id: uuid.UUID) -> Response:
    require_permission("user.deactivate")
    user = get_or_404(UserAccount, user_id)
    if str(user.id) == str(current_user.id):
        flash("Nelze deaktivovat vlastní účet.", "danger")
        return redirect(url_for("users.detail", user_id=user_id))
    user.is_active = False
    user.version += 1
    audit("edit", "UserAccount", user.id, f"Účet {user.name} ({user.email}) byl deaktivován", {"is_active": [True, False]})
    db.session.commit()
    flash(f"Účet {user.name} byl deaktivován.", "warning")
    return redirect(url_for("users.detail", user_id=user_id))


@users_bp.route("/<uuid:user_id>/archive", methods=["POST"])
@login_required
def archive(user_id: uuid.UUID) -> Response:
    require_permission("user.archive")
    user = get_or_404(UserAccount, user_id)
    if str(user.id) == str(current_user.id):
        flash("Nelze archivovat vlastní účet.", "danger")
        return redirect(url_for("users.detail", user_id=user_id))
    if user.is_archived:
        flash(f"Účet {user.name} je již archivován.", "warning")
        return redirect(url_for("users.detail", user_id=user_id))
    user.is_archived = True
    user.is_active = False
    user.version += 1
    audit(
        "edit", "UserAccount", user.id,
        f"Účet {user.name} ({user.email}) byl archivován",
        {"is_archived": [False, True], "is_active": [True, False]},
    )
    db.session.commit()
    flash(f"Účet {user.name} byl archivován.", "success")
    return redirect(url_for("users.index"))


@users_bp.route("/<uuid:user_id>/unarchive", methods=["POST"])
@login_required
def unarchive(user_id: uuid.UUID) -> Response:
    require_permission("user.archive")
    user = get_or_404(UserAccount, user_id)
    if not user.is_archived:
        flash(f"Účet {user.name} není archivován.", "warning")
        return redirect(url_for("users.detail", user_id=user_id))
    user.is_archived = False
    # Unarchiving does not re-activate — admin must do that explicitly.
    user.version += 1
    audit(
        "edit", "UserAccount", user.id,
        f"Účet {user.name} ({user.email}) byl obnoven z archivu",
        {"is_archived": [True, False]},
    )
    db.session.commit()
    flash(f"Účet {user.name} byl obnoven z archivu. Chcete-li obnovit přístup, aktivujte účet.", "success")
    return redirect(url_for("users.detail", user_id=user_id))


# ── Batch actions ─────────────────────────────────────────────────────────────

@users_bp.route("/batch", methods=["POST"])
@login_required
def batch_action() -> Response:
    """Apply a role action to multiple selected users at once.

    POST body:
        user_ids   — one or more user UUID strings (repeated field)
        action     — 'add_role' | 'remove_role'
        role_id    — integer role ID

    Requires user.assign_role permission.
    """
    require_permission("user.assign_role")

    raw_ids: list[str] = request.form.getlist("user_ids")
    action = request.form.get("action", "").strip()
    role_id_raw = request.form.get("role_id", "").strip()

    # --- Validate inputs ---
    if not raw_ids:
        flash("Nebyl vybrán žádný uživatel.", "warning")
        return redirect(url_for("users.index"))

    if action not in ("add_role", "remove_role"):
        flash("Neznámá akce.", "danger")
        return redirect(url_for("users.index"))

    if not role_id_raw or not role_id_raw.isdigit():
        flash("Vyberte platnou roli.", "warning")
        return redirect(url_for("users.index"))

    role = db.session.get(Role, int(role_id_raw))
    if role is None:
        flash("Role nebyla nalezena.", "danger")
        return redirect(url_for("users.index"))

    # --- Parse UUIDs ---
    user_uuids: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            user_uuids.append(uuid.UUID(raw))
        except ValueError:
            continue

    if not user_uuids:
        flash("Žádní platní uživatelé nevybrání.", "warning")
        return redirect(url_for("users.index"))

    users_list = db.session.scalars(
        db.select(UserAccount).where(UserAccount.id.in_(user_uuids))
    ).all()

    # --- Apply action ---
    changed = 0
    for user in users_list:
        current_role_ids = {r.id for r in user.roles}
        if action == "add_role":
            if role.id in current_role_ids:
                continue
            before = sorted(r.name for r in user.roles)
            user.roles = list(user.roles) + [role]
            user.version += 1
        else:  # remove_role
            if role.id not in current_role_ids:
                continue
            before = sorted(r.name for r in user.roles)
            user.roles = [r for r in user.roles if r.id != role.id]
            user.version += 1

        after = sorted(r.name for r in user.roles)
        audit("edit", "UserAccount", user.id, (
                f"Hromadná akce: {'přidána' if action == 'add_role' else 'odebrána'} "
                f"role '{role.name}' uživateli {user.name}"
            ), diff_changes({"roles": before}, {"roles": after}))
        changed += 1

    db.session.commit()

    action_label = "přidána" if action == "add_role" else "odebrána"
    flash(
        f"Role '{role.name}' byla {action_label} u {changed} uživatel(ů). "
        f"{len(users_list) - changed} přeskočeno (role již byla / nebyla přiřazena).",
        "success" if changed else "info",
    )
    return redirect(url_for("users.index"))


# ── Invites ───────────────────────────────────────────────────────────────────

@users_bp.route("/invites")
@login_required
def invites() -> str:
    require_permission("invite.create")
    items = db.session.scalars(
        db.select(RegistrationInvite)
        .options(selectinload(RegistrationInvite.outbox_email))  # type: ignore[arg-type]
        .order_by(RegistrationInvite.created_at.desc())
    ).all()
    # Pre-fill form with last invite's subject/message so admin doesn't retype
    last = db.session.scalar(
        db.select(RegistrationInvite)
        .where(RegistrationInvite.outbox_email_id.is_not(None))
        .order_by(RegistrationInvite.created_at.desc())
        .limit(1)
    )
    return render_template("users/invites.html", invites=items, last_invite=last)


@users_bp.route("/invites/create", methods=["POST"])
@login_required
def create_invite() -> Response:
    require_permission("invite.create")
    email = request.form.get("email", "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        flash("Zadejte platnou e-mailovou adresu.", "danger")
        return redirect(url_for("users.invites"))

    # Block if a user account with this email already exists
    if db.session.scalar(db.select(UserAccount).where(UserAccount.email == email)):
        flash(f"Uživatel s e-mailem {email} již má účet v systému.", "warning")
        return redirect(url_for("users.invites"))

    # Block if a valid (non-cancelled) invite already exists for this email
    existing = db.session.scalar(
        db.select(RegistrationInvite).where(
            RegistrationInvite.email == email,
            RegistrationInvite.used_at.is_(None),
            RegistrationInvite.cancelled_at.is_(None),
        )
    )
    if existing and existing.is_valid:
        flash(f"Platná pozvánka pro {email} již existuje.", "warning")
        return redirect(url_for("users.invites"))

    custom_subject = request.form.get("custom_subject", "").strip() or None
    custom_message = request.form.get("custom_message", "").strip() or None

    invite = RegistrationInvite(
        email=email,
        created_by_id=current_user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=INVITE_TOKEN_HOURS),
        custom_subject=custom_subject,
        custom_message=custom_message,
    )
    db.session.add(invite)
    db.session.flush()  # get invite.id before _queue_invite_email

    _queue_invite_email(invite)

    audit("create", "RegistrationInvite", invite.id, f"Pozvánka vytvořena a zařazena do fronty pro {email}", {})
    db.session.commit()

    flash(f"Pozvánka zařazena do fronty odchozích zpráv pro {email}.", "success")
    return redirect(url_for("users.invites"))


def _queue_invite_email(invite: RegistrationInvite) -> None:
    """Enqueue invite email into outbox and link it to the invite row."""
    from app.config import INVITE_TOKEN_HOURS as _HOURS
    from app.mail import _base_context  # noqa: PLC0415
    register_url = external_url_for("auth.register", token=invite.token)
    html_body = render_template(
        "email/invite.html",
        invite=invite,
        register_url=register_url,
        hours=_HOURS,
        **_base_context(),
    )
    subject = invite.custom_subject or "MedCover — pozvánka k registraci"
    outbox = OutboxEmail(
        to_email=invite.email,
        subject=subject,
        body="Tento e-mail obsahuje formátovaný obsah. Otevřete jej v e-mailovém klientovi s podporou HTML.",
        html_body=html_body,
    )
    db.session.add(outbox)
    db.session.flush()
    invite.outbox_email_id = outbox.id


@users_bp.route("/invites/<int:invite_id>/resend", methods=["POST"])
@login_required
def resend_invite(invite_id: int) -> Response:
    require_permission("invite.create")
    invite = get_or_404(RegistrationInvite, invite_id)
    if invite.is_used:
        flash("Tato pozvánka již byla použita.", "warning")
        return redirect(url_for("users.invites"))

    _queue_invite_email(invite)
    audit("resend", "RegistrationInvite", invite.id, f"Pozvánka pro {invite.email} znovu zařazena do fronty", {})
    db.session.commit()

    flash(f"Pozvánka pro {invite.email} byla znovu zařazena do fronty.", "success")
    return redirect(url_for("users.invites"))


@users_bp.route("/invites/<int:invite_id>/cancel", methods=["POST"])
@login_required
def cancel_invite(invite_id: int) -> Response:
    require_permission("invite.create")
    invite = get_or_404(RegistrationInvite, invite_id)
    if invite.is_used:
        flash("Tato pozvánka již byla použita — nelze zrušit.", "warning")
        return redirect(url_for("users.invites"))
    if invite.is_cancelled:
        flash("Tato pozvánka již byla zrušena.", "warning")
        return redirect(url_for("users.invites"))

    invite.cancelled_at = datetime.now(timezone.utc)
    audit("cancel", "RegistrationInvite", invite.id, f"Pozvánka pro {invite.email} zrušena", {})
    db.session.commit()

    flash(f"Pozvánka pro {invite.email} byla zrušena.", "success")
    return redirect(url_for("users.invites"))


def _send_activation_email(user: UserAccount) -> None:
    from app.mail import send_account_activated  # noqa: PLC0415
    send_account_activated(user)
