"""Backup management routes.

All routes are under /admin/backup and require the admin.view permission as a
baseline, with more specific backup.* permissions per action.
"""

import logging
from pathlib import Path

from azure.core.exceptions import AzureError, ResourceNotFoundError
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from markupsafe import Markup
from werkzeug.utils import secure_filename

from app.backup import (
    backup_location,
    delete_backup,
    downloaded_backup,
    export_to_zip,
    is_backup_name,
    list_backups,
    open_backup,
    prune_old_backups,
    restore_from_zip,
    temp_zip,
)
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.utils import audit, require_permission

log = logging.getLogger(__name__)

backup_bp = Blueprint("backup", __name__, url_prefix="/admin/backup")


def _validate_name(filename: str) -> None:
    """Abort with 404 unless *filename* is a backup name we create.

    Rejects anything else (path separators included) before any storage call.
    """
    if not is_backup_name(filename):
        abort(404)


# ── List & management page ────────────────────────────────────────────────────


@backup_bp.route("/")
@login_required
def index() -> str:
    require_permission("admin.view")

    # A storage outage must not take the whole page (and its settings form) down.
    try:
        backups = list_backups()
        backup_container = backup_location()
    except Exception as exc:
        log.error("Listing backups failed: %s", exc, exc_info=True)
        flash(f"Nepodařilo se načíst seznam záloh: {exc}", "danger")
        backups, backup_container = [], None
    settings = get_settings()
    return render_template(
        "admin/backup.html",
        backups=backups,
        settings=settings,
        backup_container=backup_container,
    )


# ── Ad-hoc backup ─────────────────────────────────────────────────────────────


@backup_bp.route("/run", methods=["POST"])
@login_required
def run_backup() -> Response:
    require_permission("backup.run")

    settings = get_settings()
    try:
        name = export_to_zip()
    except Exception as exc:
        log.error("Ad-hoc backup failed: %s", exc, exc_info=True)
        flash(f"Záloha selhala: {exc}", "danger")
        return redirect(url_for("backup.index"))

    # Pruning is housekeeping: its failure must not report the uploaded backup
    # as failed nor skip the audit entry (same rule as the scheduled backup).
    try:
        pruned = prune_old_backups(settings.backup_keep_count)
    except AzureError as exc:
        log.warning("Ad-hoc backup: pruning old backups failed: %s", exc, exc_info=True)
        flash(f"Staré zálohy se nepodařilo promazat: {exc}", "warning")
        pruned = []

    try:
        audit(
            "create",
            "Backup",
            name,
            f"Ruční záloha vytvořena: {name}",
            {"file": name, "pruned": pruned},
        )
        db.session.commit()
        flash(f"Záloha byla vytvořena: {name}", "success")
    except Exception as exc:
        log.error("Ad-hoc backup audit failed: %s", exc, exc_info=True)
        flash(f"Záloha {name} byla vytvořena, ale zápis do auditu selhal: {exc}", "warning")
    return redirect(url_for("backup.index"))


# ── Download ──────────────────────────────────────────────────────────────────


@backup_bp.route("/download/<filename>")
@login_required
def download(filename: str) -> Response:
    require_permission("backup.download")
    _validate_name(filename)
    try:
        downloader = open_backup(filename)
    except ResourceNotFoundError:
        abort(404)
    except Exception as exc:
        log.error("Download of %s failed: %s", filename, exc, exc_info=True)
        flash(f"Stažení zálohy selhalo: {exc}", "danger")
        return redirect(url_for("backup.index"))
    # Stream chunk by chunk so the archive is never held in memory.
    return Response(
        downloader.chunks(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(downloader.size),
        },
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

    _validate_name(filename)
    try:
        with downloaded_backup(filename) as path:
            _do_restore(path, filename, actor_id=current_user.id)
    except ResourceNotFoundError:
        abort(404)
    except Exception as exc:
        # _do_restore handles its own errors; this is the download failing.
        log.error("Fetching backup %s for restore failed: %s", filename, exc, exc_info=True)
        flash(f"Obnovení selhalo: {exc}", "danger")
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

    with temp_zip() as tmp:
        file.save(tmp)
        _do_restore(tmp, secure_filename(file.filename), actor_id=current_user.id)

    return redirect(url_for("backup.index"))


# ── Shared restore helper ─────────────────────────────────────────────────────


def _do_restore(zip_path: Path, name: str, actor_id: int | None) -> None:
    """Run restore_from_zip on local *zip_path* and flash success/error.

    *name* is the backup's user-facing name for audit and messages; the local
    file is a temp copy with a random name.
    """
    try:
        restore_from_zip(zip_path)
        # AuditLogEntry written *after* restore — session was wiped and reloaded.
        # The actor's UUID may not exist in the restored DB (e.g. cross-instance
        # restore where dev and prod have different user IDs), so check first.
        if actor_id is not None and db.session.get(UserAccount, actor_id) is None:
            actor_id = None
        db.session.add(
            AuditLogEntry(
                actor_id=actor_id,
                action_type="restore",
                entity_type="Backup",
                entity_id=name,
                summary=f"Databáze obnovena ze zálohy: {name}",
                changes_json={"file": name},
            )
        )
        db.session.commit()
        flash(f"Databáze byla úspěšně obnovena ze zálohy {name}.", "success")
        flash(
            Markup(
                "Následující nastavení <strong>nebyla obnovena</strong> ze zálohy "
                "a je třeba je zkontrolovat a případně nakonfigurovat ručně:"
                "<ul class='mb-0 mt-2'>"
                f"<li><a href='{url_for('app_settings.index')}'>Nastavení aplikace</a>"
                " — název organizace, časová zóna, URL aplikace, SMTP&nbsp;/&nbsp;e-mail</li>"
                f"<li><a href='{url_for('notifications.index')}'>Oznámení</a>"
                " — zapnutí/vypnutí e-mailových upozornění</li>"
                "<li>Nastavení zálohování — počet uchovávaných záloh, plánování</li>"
                "</ul>"
            ),
            "info",
        )
    except Exception as exc:
        log.error("Restore from %s failed: %s", name, exc, exc_info=True)
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

    _validate_name(filename)
    try:
        delete_backup(filename)
    except ResourceNotFoundError:
        abort(404)
    except Exception as exc:
        log.error("Delete backup %s failed: %s", filename, exc, exc_info=True)
        flash(f"Smazání selhalo: {exc}", "danger")
        return redirect(url_for("backup.index"))
    try:
        audit("delete", "Backup", filename, f"Záloha smazána: {filename}", {"file": filename})
        db.session.commit()
        flash(f"Záloha {filename} byla smazána.", "success")
    except Exception as exc:
        log.error("Delete backup %s: audit failed: %s", filename, exc, exc_info=True)
        flash(f"Záloha {filename} byla smazána, ale zápis do auditu selhal: {exc}", "warning")
    return redirect(url_for("backup.index"))


# ── Settings update ───────────────────────────────────────────────────────────


@backup_bp.route("/settings", methods=["POST"])
@login_required
def save_settings() -> Response:
    require_permission("admin.manage_settings")

    settings = get_settings()
    old = {
        "backup_keep_count": settings.backup_keep_count,
        "backup_schedule_enabled": settings.backup_schedule_enabled,
        "backup_schedule_hour": settings.backup_schedule_hour,
        "backup_schedule_minute": settings.backup_schedule_minute,
    }

    try:
        keep = int(request.form.get("backup_keep_count", "7"))
        settings.backup_keep_count = max(1, min(keep, 365))
    except ValueError:
        settings.backup_keep_count = 7

    settings.backup_schedule_enabled = "backup_schedule_enabled" in request.form

    # HH:MM in the app's configured timezone. Accept either a combined
    # ``backup_schedule_time=HH:MM`` from the <input type="time"> field, or,
    # for API/curl compatibility, individual hour + minute fields.
    time_str = request.form.get("backup_schedule_time", "").strip()
    if time_str and ":" in time_str:
        # Browsers may append seconds (HH:MM:SS) despite step="60" — ignore them.
        hour_str, minute_str = time_str.split(":")[:2]
    else:
        hour_str = request.form.get("backup_schedule_hour", "2")
        minute_str = request.form.get("backup_schedule_minute", "0")
    try:
        settings.backup_schedule_hour = max(0, min(int(hour_str), 23))
    except ValueError:
        settings.backup_schedule_hour = 2
    try:
        settings.backup_schedule_minute = max(0, min(int(minute_str), 59))
    except ValueError:
        settings.backup_schedule_minute = 0

    new = {
        "backup_keep_count": settings.backup_keep_count,
        "backup_schedule_enabled": settings.backup_schedule_enabled,
        "backup_schedule_hour": settings.backup_schedule_hour,
        "backup_schedule_minute": settings.backup_schedule_minute,
    }
    audit("edit", "AppSettings", "1", "Nastavení zálohování upraveno", {"before": old, "after": new})
    db.session.commit()
    flash("Nastavení zálohování bylo uloženo.", "success")
    return redirect(url_for("backup.index"))
