"""Admin digest configuration, preview and send routes."""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db

log = logging.getLogger(__name__)

bp = Blueprint("admin_digest", __name__, url_prefix="/admin/digest")

_FREQUENCY_OPTIONS = [
    (6, "Každých 6 hodin"),
    (12, "Každých 12 hodin"),
    (24, "Jednou denně"),
    (48, "Každé 2 dny"),
    (72, "Každé 3 dny"),
    (168, "Jednou týdně"),
]


def _require_digest_perm() -> None:
    if not current_user.has_permission("admin.manage_digest"):
        abort(403)


# ── Settings page ─────────────────────────────────────────────────────────────


@bp.route("/", methods=["GET"])
@login_required
def index() -> str:
    _require_digest_perm()
    from app.digest.registry import BLOCK_REGISTRY
    from app.models.digest import get_digest_schedule
    from app.models.settings import get_settings

    schedule = get_digest_schedule()
    return render_template(
        "admin/digest/index.html",
        schedule=schedule,
        frequency_options=_FREQUENCY_OPTIONS,
        hour_options=list(range(24)),
        block_registry=BLOCK_REGISTRY,
        app_timezone=get_settings().timezone,
    )


@bp.route("/save", methods=["POST"])
@login_required
def save() -> Response:
    _require_digest_perm()
    from app.models.digest import get_digest_schedule

    schedule = get_digest_schedule()

    client_version = request.form.get("version", type=int, default=0)
    if client_version != schedule.version:
        flash("Nastavení bylo mezitím změněno — načtěte stránku znovu.", "danger")
        return redirect(url_for("admin_digest.index"))

    schedule.enabled = bool(request.form.get("enabled"))
    schedule.frequency_hours = int(request.form.get("frequency_hours", 24))
    schedule.preferred_hour = max(0, min(23, int(request.form.get("preferred_hour", 7))))
    schedule.email_subject = request.form.get("email_subject", "").strip() or "MedCover — Přehledový e-mail"
    schedule.header_html = request.form.get("header_html", "").strip() or None
    schedule.footer_html = request.form.get("footer_html", "").strip() or None
    schedule.version += 1

    db.session.commit()
    flash("Nastavení přehledového e-mailu bylo uloženo.", "success")
    return redirect(url_for("admin_digest.index"))


# ── Block config ──────────────────────────────────────────────────────────────

_MAX_INSTANCES_PER_TYPE = 5


@bp.route("/blocks/add", methods=["POST"])
@login_required
def add_block() -> Response:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.digest.registry import BLOCK_REGISTRY
    from app.models.digest import DigestBlock, get_digest_schedule

    block_type = request.form.get("block_type", "").strip()
    if block_type not in BLOCK_REGISTRY:
        flash("Neplatný typ bloku.", "danger")
        return redirect(url_for("admin_digest.index"))

    cls = BLOCK_REGISTRY[block_type]
    schedule = get_digest_schedule()

    count = (
        db.session.scalar(
            sa.select(sa.func.count())
            .select_from(DigestBlock)
            .where(
                DigestBlock.digest_schedule_id == schedule.id,
                DigestBlock.block_type == block_type,
            )
        )
        or 0
    )
    if count >= _MAX_INSTANCES_PER_TYPE:
        flash(f'Blok "{cls.label}" lze přidat nejvýše {_MAX_INSTANCES_PER_TYPE}×.', "danger")
        return redirect(url_for("admin_digest.index"))

    max_order = (
        db.session.scalar(
            sa.select(sa.func.max(DigestBlock.sort_order)).where(DigestBlock.digest_schedule_id == schedule.id)
        )
        or 0
    )

    db.session.add(
        DigestBlock(
            digest_schedule_id=schedule.id,
            block_type=block_type,
            enabled=True,
            sort_order=max_order + 1,
            config_json=dict(cls.default_config),
        )
    )
    db.session.commit()
    flash(f'Blok "{cls.label}" byl přidán.', "success")
    return redirect(url_for("admin_digest.index"))


@bp.route("/blocks/<int:block_id>/save", methods=["POST"])
@login_required
def save_block(block_id: int) -> Response:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.digest.registry import BLOCK_REGISTRY
    from app.models.digest import DigestBlock, get_digest_schedule

    schedule = get_digest_schedule()
    block = db.session.scalar(
        sa.select(DigestBlock)
        .where(
            DigestBlock.id == block_id,
            DigestBlock.digest_schedule_id == schedule.id,
        )
        .with_for_update()
    )
    if block is None:
        abort(404)

    client_version = request.form.get("version", type=int, default=0)
    if client_version != block.version:
        flash("Nastavení bloku bylo mezitím změněno — načtěte stránku znovu.", "danger")
        return redirect(url_for("admin_digest.index"))

    block.enabled = bool(request.form.get("enabled"))
    cls = BLOCK_REGISTRY[block.block_type]
    new_config = dict(block.config_json or cls.default_config)

    _merge_block_config(block.block_type, new_config, request.form)

    block.config_json = new_config
    block.version += 1
    db.session.commit()
    flash(f'Blok "{cls.label}" byl uložen.', "success")
    return redirect(url_for("admin_digest.index"))


@bp.route("/blocks/<int:block_id>/delete", methods=["POST"])
@login_required
def delete_block(block_id: int) -> Response:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.digest.registry import BLOCK_REGISTRY
    from app.models.digest import DigestBlock, get_digest_schedule

    schedule = get_digest_schedule()
    block = db.session.scalar(
        sa.select(DigestBlock).where(
            DigestBlock.id == block_id,
            DigestBlock.digest_schedule_id == schedule.id,
        )
    )
    if block is None:
        abort(404)

    cls = BLOCK_REGISTRY.get(block.block_type)
    label = cls.label if cls else block.block_type
    db.session.delete(block)
    db.session.commit()
    flash(f'Blok "{label}" byl odstraněn.', "success")
    return redirect(url_for("admin_digest.index"))


def _merge_block_config(block_type: str, config: dict[str, object], form: Any) -> None:
    """Write form values into config dict for the given block type."""
    config["title"] = form.get("title", config.get("title", "")).strip()

    if block_type == "server_stats":
        for key in (
            "show_user_count",
            "show_event_count",
            "show_db_size",
            "show_table_sizes",
            "show_scheduler_heartbeat",
            "show_outbox_pending",
            "show_outbox_peak",
        ):
            config[key] = bool(form.get(key))
        config["peak_hours"] = max(1, form.get("peak_hours", type=int) or 24)
        config["max_table_rows"] = max(1, min(50, form.get("max_table_rows", type=int) or 5))

    elif block_type == "audit_log":
        config["hours"] = max(1, form.get("hours", type=int) or 24)
        config["max_rows"] = max(1, min(200, form.get("max_rows", type=int) or 50))
        config["show_actor"] = bool(form.get("show_actor"))
        config["entity_types"] = form.getlist("entity_types")
        config["action_types"] = form.getlist("action_types")

    elif block_type == "upcoming_events":
        config["days_ahead"] = max(1, form.get("days_ahead", type=int) or 7)
        config["max_rows"] = max(1, min(100, form.get("max_rows", type=int) or 15))
        config["show_unfilled_only"] = bool(form.get("show_unfilled_only"))

    elif block_type == "new_users":
        config["hours"] = max(1, form.get("hours", type=int) or 24)
        config["max_rows"] = max(1, min(100, form.get("max_rows", type=int) or 20))
        config["show_pending_only"] = bool(form.get("show_pending_only"))

    elif block_type == "feedback_summary":
        config["hours"] = max(1, form.get("hours", type=int) or 24)
        config["max_rows"] = max(1, min(100, form.get("max_rows", type=int) or 20))

    elif block_type == "user_activity":
        config["hours"] = max(1, form.get("hours", type=int) or 24)
        config["max_rows"] = max(1, min(100, form.get("max_rows", type=int) or 10))

    elif block_type == "free_text":
        config["content"] = form.get("content", "")


# ── Block enable toggle ───────────────────────────────────────────────────────


@bp.route("/blocks/<int:block_id>/toggle", methods=["POST"])
@login_required
def toggle_block(block_id: int) -> dict[str, object]:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.models.digest import DigestBlock, get_digest_schedule

    schedule = get_digest_schedule()
    block = db.session.scalar(
        sa.select(DigestBlock)
        .where(
            DigestBlock.id == block_id,
            DigestBlock.digest_schedule_id == schedule.id,
        )
        .with_for_update()
    )
    if block is None:
        abort(404)

    block.enabled = not block.enabled
    block.version += 1
    db.session.commit()
    return {"ok": True, "enabled": block.enabled}


# ── Block reorder ─────────────────────────────────────────────────────────────


@bp.route("/blocks/reorder", methods=["POST"])
@login_required
def reorder_blocks() -> dict[str, bool]:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.models.digest import DigestBlock

    ids: list[int] = request.get_json(silent=True) or []
    if len(ids) > 50:
        abort(400)
    for i, block_id in enumerate(ids):
        db.session.execute(sa.update(DigestBlock).where(DigestBlock.id == block_id).values(sort_order=i))
    db.session.commit()
    return {"ok": True}


# ── Preview ───────────────────────────────────────────────────────────────────


@bp.route("/preview")
@login_required
def preview() -> Response:
    _require_digest_perm()
    import html as _html

    from app.digest.renderer import render_digest

    digest_html = render_digest(db.session)
    # Sandbox the digest content in an iframe to prevent XSS from admin-controlled
    # free_text / header_html / footer_html fields executing in the browser context.
    escaped = _html.escape(digest_html, quote=True)
    wrapper = (
        '<!DOCTYPE html><html><body style="margin:0">'
        f'<iframe srcdoc="{escaped}"'
        ' style="width:100%;height:100vh;border:none;" sandbox></iframe>'
        "</body></html>"
    )
    return Response(wrapper, content_type="text/html; charset=utf-8")


# ── Send test ─────────────────────────────────────────────────────────────────


@bp.route("/send-test", methods=["POST"])
@login_required
def send_test() -> Response:
    _require_digest_perm()
    from app.digest.renderer import render_digest
    from app.mail import send_admin_digest
    from app.models.digest import get_digest_schedule

    email = request.form.get("test_email", "").strip()
    if not email:
        flash("Zadejte e-mailovou adresu.", "danger")
        return redirect(url_for("admin_digest.index"))

    schedule = get_digest_schedule()
    html = render_digest(db.session)
    send_admin_digest(email, f"[TEST] {schedule.email_subject}", html)
    db.session.commit()
    flash(f"Testovací přehledový e-mail byl zařazen do fronty pro {email}.", "success")
    return redirect(url_for("admin_digest.index"))


# ── Send now (to all admins) ──────────────────────────────────────────────────


@bp.route("/send-now", methods=["POST"])
@login_required
def send_now() -> Response:
    _require_digest_perm()
    import sqlalchemy as sa

    from app.digest.renderer import render_digest
    from app.mail import send_admin_digest, user_can_receive_notification
    from app.models.digest import get_digest_schedule
    from app.models.user import UserAccount

    schedule = get_digest_schedule()
    html = render_digest(db.session)

    recipients = db.session.scalars(
        sa.select(UserAccount).where(UserAccount.is_active.is_(True)).where(UserAccount.is_archived.is_(False))
    ).all()

    count = 0
    for user in recipients:
        if user_can_receive_notification(user, "admin_digest"):
            send_admin_digest(user.email, schedule.email_subject, html)
            count += 1

    db.session.commit()
    flash(f"Přehledový e-mail byl zařazen do fronty pro {count} příjemce.", "success")
    return redirect(url_for("admin_digest.index"))
