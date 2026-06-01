"""
Výkaz práce — employee work report xlsx generation and download.

Routes
------
GET  /work-report/           — form + list of already-generated reports
POST /work-report/generate   — build xlsx, redirect back to index
GET  /work-report/download   — stream the generated file to the browser
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

from app.utils import require_permission
from app.work_report_generator import CZ_MONTH_NAMES, generate_work_report

work_report_bp = Blueprint("work_report", __name__, url_prefix="/work-report")

_EXPIRY_HOURS = 24


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
    require_permission("work_report.generate")
    now = datetime.now(tz=timezone.utc)
    reports = _list_reports(str(current_user.id))
    return render_template(
        "work_report/index.html",
        current_year=now.year,
        current_month=now.month,
        reports=reports,
    )


@work_report_bp.route("/generate", methods=["POST"])
@login_required
def generate() -> object:
    require_permission("work_report.generate")

    try:
        year = int(request.form["year"])
        month = int(request.form["month"])
    except (KeyError, ValueError):
        flash("Neplatné hodnoty formuláře.", "danger")
        return redirect(url_for("work_report.index"))

    now = datetime.now(tz=timezone.utc)
    if not (2020 <= year <= now.year):
        flash("Rok je mimo povolený rozsah.", "danger")
        return redirect(url_for("work_report.index"))
    if not (1 <= month <= 12):
        flash("Měsíc musí být v rozsahu 1–12.", "danger")
        return redirect(url_for("work_report.index"))
    if (year, month) > (now.year, now.month):
        flash("Výkaz nelze vygenerovat pro budoucí měsíc.", "danger")
        return redirect(url_for("work_report.index"))

    try:
        generate_work_report(current_user, year, month)
    except Exception as exc:  # pragma: no cover
        flash(f"Chyba při generování souboru: {exc}", "danger")
        return redirect(url_for("work_report.index"))

    flash(f"Výkaz pro {CZ_MONTH_NAMES[month]} {year} byl vygenerován.", "success")
    return redirect(url_for("work_report.index"))


@work_report_bp.route("/download")
@login_required
def download() -> object:
    require_permission("work_report.generate")

    try:
        year = int(request.args["year"])
        month = int(request.args["month"])
    except (KeyError, ValueError):
        flash("Neplatné parametry.", "danger")
        return redirect(url_for("work_report.index"))

    filename = f"{year}-{month:02d}.xlsx"
    user_dir = Path(current_app.instance_path) / "work_report" / str(current_user.id)

    if not (user_dir / filename).exists():
        flash("Soubor nenalezen. Vygenerujte výkaz znovu.", "warning")
        return redirect(url_for("work_report.index"))

    download_name = f"výkaz práce {year}-{month:02d} {current_user.name}.xlsx"
    return send_from_directory(
        directory=str(user_dir),
        path=filename,
        as_attachment=True,
        download_name=download_name,
    )
