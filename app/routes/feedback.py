"""
Feedback blueprint — user feedback submission and admin management.

Routes:
  GET  /feedback/        feedback form page
  POST /feedback/submit  submit feedback (login required)
  GET  /admin/feedback/  list all feedback (admin.view permission required)
  POST /admin/feedback/<uuid>/delete  delete a feedback entry (admin.view required)
"""

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.feedback import UserFeedback
from app.models.settings import get_settings
from app.utils import get_or_404, require_permission, safe_next

feedback_bp = Blueprint("feedback", __name__)


def _require_feedback_enabled() -> None:
    """Abort with 404 if the feedback feature has been disabled by an admin."""
    if not get_settings().feedback_enabled:
        abort(404)


# ── Submit ─────────────────────────────────────────────────────────────────────


@feedback_bp.get("/feedback/")
@login_required
def feedback_form() -> str:
    """Render the feedback submission form."""
    _require_feedback_enabled()
    from_url = request.args.get("from", "")
    return render_template(
        "feedback/submit.html",
        page_url=from_url,
        cancel_url=safe_next(from_url, url_for("main.dashboard")),
    )


@feedback_bp.post("/feedback/submit")
@login_required
def feedback_submit() -> Response:
    """Save submitted feedback to the database."""
    _require_feedback_enabled()
    message = request.form.get("message", "").strip()
    if not message:
        flash("Zpráva nesmí být prázdná.", "warning")
        return redirect(url_for("feedback.feedback_form"))

    page_url = request.form.get("page_url", "").strip() or None
    user_agent = request.form.get("user_agent", "").strip() or None
    screen_info = request.form.get("screen_info", "").strip() or None
    app_version = current_app.config.get("APP_VERSION") or None

    entry = UserFeedback(
        user_id=current_user.id,
        message=message,
        page_url=page_url,
        user_agent=user_agent,
        screen_info=screen_info,
        app_version=app_version,
    )
    db.session.add(entry)
    db.session.commit()

    flash("Děkujeme za zpětnou vazbu!", "success")
    # Return to the page the user came from, or dashboard (safe_next guards against open redirect)
    return redirect(safe_next(page_url))


# ── Admin ──────────────────────────────────────────────────────────────────────


@feedback_bp.get("/admin/feedback/")
@login_required
def feedback_list() -> str:
    """List all feedback entries (admin only)."""
    require_permission("admin.view")

    entries = list(db.session.scalars(db.select(UserFeedback).order_by(UserFeedback.submitted_at.desc())).all())
    return render_template("feedback/list.html", entries=entries)


@feedback_bp.post("/admin/feedback/<uuid:entry_id>/delete")
@login_required
def feedback_delete(entry_id: object) -> Response:
    """Delete a feedback entry (admin only). Blocked when DEV_LOGIN_ENABLED is True."""
    require_permission("admin.view")

    if current_app.config.get("DEV_LOGIN_ENABLED"):
        flash("Mazání zpětné vazby je v testovacím prostředí zakázáno.", "warning")
        return redirect(url_for("feedback.feedback_list"))

    entry = get_or_404(UserFeedback, entry_id)

    summary = f"Smazána zpětná vazba od {entry.user.name if entry.user else 'neznámý'}"
    db.session.delete(entry)

    log = AuditLogEntry(
        actor_id=current_user.id,
        action_type="delete",
        entity_type="UserFeedback",
        entity_id=str(entry_id),
        summary=summary,
    )
    db.session.add(log)
    db.session.commit()

    flash("Zpětná vazba byla smazána.", "success")
    return redirect(url_for("feedback.feedback_list"))
