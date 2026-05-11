"""Backup management routes.

All routes are under /admin/backup and require the admin.view permission as a
baseline, with more specific backup.* permissions per action.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.extensions import db
from app.utils import audit, require_permission
from app.models.settings import get_settings
from app.models.audit import AuditLogEntry

log = logging.getLogger(__name__)

backup_bp = Blueprint("backup", __name__, url_prefix="/admin/backup")

# Filename pattern — only allow files we created to prevent path traversal.
_BACKUP_FILENAME_RE = re.compile(r"^medcover_backup_\d{8}_\d{6}_\d+\.zip$")


def _resolve_backup_dir() -> Path:
    settings = get_settings()
    backup_dir = Path(settings.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = Path(current_app.root_path).parent / backup_dir
    return backup_dir


def _safe_backup_path(filename: str) -> Path:
    """Return absolute path for *filename*, raising 404 on invalid/traversal names."""
    if not _BACKUP_FILENAME_RE.match(filename):
        abort(404)
    path = _resolve_backup_dir() / filename
    if not path.exists():
        abort(404)
    return path


# ── List & management page ────────────────────────────────────────────────────

@backup_bp.route("/")
@login_required
def index() -> str:
    require_permission("admin.view")

    from app.backup import list_backups
    backup_dir = _resolve_backup_dir()
    backups = list_backups(backup_dir)
    settings = get_settings()
    return render_template(
        "admin/backup.html",
        backups=backups,
        settings=settings,
        backup_dir=str(backup_dir),
    )


# ── Ad-hoc backup ─────────────────────────────────────────────────────────────

@backup_bp.route("/run", methods=["POST"])
@login_required
def run_backup() -> Response:
    require_permission("backup.run")

    from app.backup import export_to_zip, prune_old_backups
    backup_dir = _resolve_backup_dir()
    settings = get_settings()
    try:
        zip_path = export_to_zip(backup_dir)
        pruned = prune_old_backups(backup_dir, settings.backup_keep_count)
        audit("create", "Backup", zip_path.name, f"Ruční záloha vytvořena: {zip_path.name}", {"file": zip_path.name, "pruned": [p.name for p in pruned]})
        db.session.commit()
        flash(f"Záloha byla vytvořena: {zip_path.name}", "success")
    except Exception as exc:
        log.error("Ad-hoc backup failed: %s", exc, exc_info=True)
        flash(f"Záloha selhala: {exc}", "danger")
    return redirect(url_for("backup.index"))


# ── Download ──────────────────────────────────────────────────────────────────

@backup_bp.route("/download/<filename>")
@login_required
def download(filename: str) -> Response:
    require_permission("backup.download")
    path = _safe_backup_path(filename)
    return send_file(
        io.BytesIO(path.read_bytes()),
        download_name=filename,
        as_attachment=True,
        mimetype="application/zip",
    )


# ── Restore from stored file ──────────────────────────────────────────────────

@backup_bp.route("/restore/<filename>", methods=["POST"])
@login_required
def restore(filename: str) -> Response:
    require_permission("backup.restore")

    confirmation = request.form.get("confirmation", "").strip()
    if confirmation != "RESTORE":
        flash("Obnovení selhalo: pro potvrzení zadejte RESTORE.", "danger")
        return redirect(url_for("backup.index"))

    path = _safe_backup_path(filename)
    _do_restore(path, actor_id=current_user.id)
    return redirect(url_for("backup.index"))


# ── Upload & restore ──────────────────────────────────────────────────────────

@backup_bp.route("/upload-restore", methods=["POST"])
@login_required
def upload_restore() -> Response:
    require_permission("backup.restore")

    confirmation = request.form.get("confirmation", "").strip()
    if confirmation != "RESTORE":
        flash("Obnovení selhalo: pro potvrzení zadejte RESTORE.", "danger")
        return redirect(url_for("backup.index"))

    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Nebyl vybrán žádný soubor.", "danger")
        return redirect(url_for("backup.index"))

    if not file.filename.endswith(".zip"):
        flash("Soubor musí být ve formátu .zip.", "danger")
        return redirect(url_for("backup.index"))

    # Save uploaded file to a temp location inside backup_dir then restore.
    backup_dir = _resolve_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = backup_dir / f"_upload_{secure_filename(file.filename)}"
    try:
        file.save(str(tmp_path))
        _do_restore(tmp_path, actor_id=current_user.id)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return redirect(url_for("backup.index"))


# ── Shared restore helper ─────────────────────────────────────────────────────

def _do_restore(zip_path: Path, actor_id: int | None) -> None:
    """Run restore_from_zip and flash success/error."""
    from app.backup import restore_from_zip
    from app.models.user import UserAccount
    try:
        restore_from_zip(zip_path)
        # AuditLogEntry written *after* restore — session was wiped and reloaded.
        # The actor's UUID may not exist in the restored DB (e.g. cross-instance
        # restore where dev and prod have different user IDs), so check first.
        if actor_id is not None and db.session.get(UserAccount, actor_id) is None:
            actor_id = None
        db.session.add(AuditLogEntry(
            actor_id=actor_id,
            action_type="restore",
            entity_type="Backup",
            entity_id=zip_path.name,
            summary=f"Databáze obnovena ze zálohy: {zip_path.name}",
            changes_json={"file": zip_path.name},
        ))
        db.session.commit()
        flash(f"Databáze byla úspěšně obnovena ze zálohy {zip_path.name}.", "success")
    except Exception as exc:
        log.error("Restore from %s failed: %s", zip_path.name, exc, exc_info=True)
        flash(f"Obnovení selhalo: {exc}", "danger")


# ── Delete backup file ────────────────────────────────────────────────────────

@backup_bp.route("/delete/<filename>", methods=["POST"])
@login_required
def delete(filename: str) -> Response:
    require_permission("backup.delete")

    confirmation = request.form.get("confirmation", "").strip()
    if confirmation != "SMAZAT":
        flash("Smazání selhalo: pro potvrzení zadejte SMAZAT.", "danger")
        return redirect(url_for("backup.index"))

    path = _safe_backup_path(filename)
    try:
        path.unlink()
        audit("delete", "Backup", filename, f"Záloha smazána: {filename}", {"file": filename})
        db.session.commit()
        flash(f"Záloha {filename} byla smazána.", "success")
    except Exception as exc:
        log.error("Delete backup %s failed: %s", filename, exc, exc_info=True)
        flash(f"Smazání selhalo: {exc}", "danger")
    return redirect(url_for("backup.index"))


# ── Settings update ───────────────────────────────────────────────────────────

@backup_bp.route("/settings", methods=["POST"])
@login_required
def save_settings() -> Response:
    require_permission("admin.manage_settings")

    settings = get_settings()
    old = {
        "backup_dir": settings.backup_dir,
        "backup_keep_count": settings.backup_keep_count,
        "backup_schedule_enabled": settings.backup_schedule_enabled,
        "backup_schedule_hour": settings.backup_schedule_hour,
    }

    settings.backup_dir = request.form.get("backup_dir", "backups").strip() or "backups"
    try:
        keep = int(request.form.get("backup_keep_count", "7"))
        settings.backup_keep_count = max(1, min(keep, 365))
    except ValueError:
        settings.backup_keep_count = 7

    settings.backup_schedule_enabled = "backup_schedule_enabled" in request.form

    try:
        hour = int(request.form.get("backup_schedule_hour", "2"))
        settings.backup_schedule_hour = max(0, min(hour, 23))
    except ValueError:
        settings.backup_schedule_hour = 2

    new = {
        "backup_dir": settings.backup_dir,
        "backup_keep_count": settings.backup_keep_count,
        "backup_schedule_enabled": settings.backup_schedule_enabled,
        "backup_schedule_hour": settings.backup_schedule_hour,
    }
    audit("edit", "AppSettings", "1", "Nastavení zálohování upraveno", {"before": old, "after": new})
    db.session.commit()
    flash("Nastavení zálohování bylo uloženo.", "success")
    return redirect(url_for("backup.index"))
