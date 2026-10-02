"""
Výkaz práce — employee work report xlsx generation and download.

Routes
------
GET  /work-report/           — form + list of already-generated reports
POST /work-report/generate   — build xlsx, redirect back to index
GET  /work-report/download   — stream the generated file to the browser

All three accept an optional ``user_id`` to act on another person's report
(requires ``work_report.generate_any``).
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.user import UserAccount
from app.utils import audit, get_app_tz, get_or_404, require_permission
from app.work_report_generator import CZ_MONTH_NAMES, generate_work_report

work_report_bp = Blueprint("work_report", __name__, url_prefix="/work-report")

_EXPIRY_HOURS = 24


def _last_completed_month(now: datetime) -> tuple[int, int]:
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


def _target_user() -> UserAccount:
    """Resolve the report owner from the optional ``user_id`` parameter and check permissions."""
    raw = request.values.get("user_id")
    try:
        user_id = uuid.UUID(raw) if raw else current_user.id
    except ValueError:
        abort(400)
    if user_id == current_user.id:
        require_permission("work_report.generate")
        return current_user
    require_permission("work_report.generate_any")
    return get_or_404(UserAccount, user_id)


def _index_url(target: UserAccount) -> str:
    return url_for("work_report.index", user_id=None if target.id == current_user.id else target.id)


def _list_reports(user_id: str) -> list[dict]:
    """Return metadata for all non-expired xlsx files belonging to *user_id*."""
    user_dir = Path(current_app.instance_path) / "work_report" / user_id
    if not user_dir.exists():
        return []

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=_EXPIRY_HOURS)
    reports = []
    for f in sorted(user_dir.glob("*.xlsx"), reverse=True):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue  # expired — scheduler will remove it; hide from list
        try:
            year_str, month_str = f.stem.split("-")
            year, month = int(year_str), int(month_str)
        except ValueError:
            continue
        expires_at = mtime + timedelta(hours=_EXPIRY_HOURS)
        reports.append(
            {
                "year": year,
                "month": month,
                "month_name": CZ_MONTH_NAMES[month],
                "generated_at": mtime,
                "expires_at": expires_at,
            }
        )
    return reports


@work_report_bp.route("/", methods=["GET"])
@login_required
def index() -> str:
    target = _target_user()
    now = datetime.now(tz=get_app_tz())
    default_year, default_month = _last_completed_month(now)
    reports = _list_reports(str(target.id))
    return render_template(
        "work_report/index.html",
        target_user=target,
        current_year=now.year,
        current_month=now.month,
        default_year=default_year,
        default_month=default_month,
        reports=reports,
    )


@work_report_bp.route("/generate", methods=["POST"])
@login_required
def generate() -> object:
    target = _target_user()

    try:
        year = int(request.form["year"])
        month = int(request.form["month"])
    except KeyError, ValueError:
        flash("Neplatné hodnoty formuláře.", "danger")
        return redirect(_index_url(target))

    now = datetime.now(tz=get_app_tz())
    if not (2020 <= year <= now.year):
        flash("Rok je mimo povolený rozsah.", "danger")
        return redirect(_index_url(target))
    if not (1 <= month <= 12):
        flash("Měsíc musí být v rozsahu 1–12.", "danger")
        return redirect(_index_url(target))
    if (year, month) > (now.year, now.month):
        flash("Výkaz nelze vygenerovat pro budoucí měsíc.", "danger")
        return redirect(_index_url(target))

    is_own = target.id == current_user.id
    try:
        # The stored signature attests the person's own hours, so it is only
        # embedded when they generate the report themselves.
        generate_work_report(target, year, month, with_signature=is_own)
    except Exception as exc:  # pragma: no cover
        flash(f"Chyba při generování souboru: {exc}", "danger")
        return redirect(_index_url(target))

    if not is_own:
        audit(
            "export",
            "UserAccount",
            target.id,
            f"Vygenerován výkaz práce za {CZ_MONTH_NAMES[month]} {year} pro uživatele {target.name}",
        )
        db.session.commit()
    flash(f"Výkaz pro {CZ_MONTH_NAMES[month]} {year} byl vygenerován.", "success")
    return redirect(_index_url(target))


@work_report_bp.route("/download")
@login_required
def download() -> object:
    target = _target_user()

    try:
        year = int(request.args["year"])
        month = int(request.args["month"])
    except KeyError, ValueError:
        flash("Neplatné parametry.", "danger")
        return redirect(_index_url(target))

    filename = f"{year}-{month:02d}.xlsx"
    user_dir = Path(current_app.instance_path) / "work_report" / str(target.id)

    if not (user_dir / filename).exists():
        flash("Soubor nenalezen. Vygenerujte výkaz znovu.", "warning")
        return redirect(_index_url(target))

    download_name = f"výkaz práce {year}-{month:02d} {target.name}.xlsx"
    return send_from_directory(
        directory=str(user_dir),
        path=filename,
        as_attachment=True,
        download_name=download_name,
    )
