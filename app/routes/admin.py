import socket
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import collate

from app.extensions import db
from app.models.feedback import UserFeedback
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.utils import CS_COLLATION, get_or_404, require_permission

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_PAGE_SIZE = 50

# Response-time thresholds
_DB_WARN_MS = 200  # warn above 200 ms
_SMTP_WARN_MS = 1000  # warn above 1 s


def _check_db_health() -> dict:
    """Probe DB and return {status, ms, error}."""
    try:
        t0 = time.monotonic()
        db.session.execute(db.text("SELECT 1"))
        ms = int((time.monotonic() - t0) * 1000)
        status = "warning" if ms > _DB_WARN_MS else "ok"
        return {"status": status, "ms": ms, "error": None}
    except Exception as exc:
        return {"status": "error", "ms": None, "error": type(exc).__name__}


def _check_scheduler(settings: object, now: datetime) -> dict:
    """Check scheduler heartbeat and return {status, last, age_s}."""
    sched_last = settings.scheduler_last_seen  # type: ignore[attr-defined]
    if sched_last is None:
        return {"status": "unknown", "last": None, "age_s": None}
    age_s = int((now - sched_last).total_seconds())
    if age_s < 30:
        status = "ok"
    elif age_s < 300:
        status = "warning"
    else:
        status = "error"
    return {"status": status, "last": sched_last, "age_s": age_s}


def _check_smtp(settings: object) -> dict:
    """Probe SMTP and return {configured, status, ms, error}."""
    if not settings.smtp_configured:  # type: ignore[attr-defined]
        return {"configured": False, "status": "unconfigured", "ms": None, "error": None}
    try:
        t0 = time.monotonic()
        with socket.create_connection(  # type: ignore[attr-defined]
            (settings.smtp_server, settings.smtp_port), timeout=3
        ):
            pass
        ms = int((time.monotonic() - t0) * 1000)
        status = "warning" if ms > _SMTP_WARN_MS else "ok"
        return {"configured": True, "status": status, "ms": ms, "error": None}
    except TimeoutError:
        return {
            "configured": True,
            "status": "error",
            "ms": None,
            "error": f"Spojení na {settings.smtp_server}:{settings.smtp_port} vypršelo (timeout 3 s)",
        }  # type: ignore[attr-defined]
    except OSError as exc:
        return {"configured": True, "status": "error", "ms": None, "error": str(exc)}


def _admin_statistics(now: datetime) -> dict:
    """Collect user/event/outbox/audit statistics for the admin dashboard."""
    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel
    from app.models.event import Event, EventStatus  # pylint: disable=import-outside-toplevel
    from app.models.outbox import OutboxEmail  # pylint: disable=import-outside-toplevel

    user_total = db.session.scalar(
        db.select(db.func.count()).select_from(UserAccount).where(UserAccount.is_archived.is_(False))
    )
    user_active = db.session.scalar(
        db.select(db.func.count())
        .select_from(UserAccount)
        .where(UserAccount.is_active.is_(True), UserAccount.is_archived.is_(False))
    )

    event_counts = {
        s.value: db.session.scalar(db.select(db.func.count()).select_from(Event).where(Event.status == s))
        for s in EventStatus
    }

    cutoff_24h = now - timedelta(hours=24)
    outbox_pending = db.session.scalar(
        db.select(db.func.count())
        .select_from(OutboxEmail)
        .where(OutboxEmail.status == "pending", OutboxEmail.created_at >= cutoff_24h)
    )
    outbox_failed = db.session.scalar(
        db.select(db.func.count())
        .select_from(OutboxEmail)
        .where(OutboxEmail.status == "failed", OutboxEmail.created_at >= cutoff_24h)
    )
    outbox_sent = db.session.scalar(
        db.select(db.func.count())
        .select_from(OutboxEmail)
        .where(OutboxEmail.status == "sent", OutboxEmail.created_at >= cutoff_24h)
    )
    outbox_last = db.session.scalar(db.select(OutboxEmail).order_by(OutboxEmail.created_at.desc()).limit(1))
    outbox_last_sent = db.session.scalar(
        db.select(OutboxEmail.sent_at).where(OutboxEmail.status == "sent").order_by(OutboxEmail.sent_at.desc()).limit(1)
    )

    recent_audit = db.session.scalars(db.select(AuditLogEntry).order_by(AuditLogEntry.timestamp.desc()).limit(8)).all()

    feedback_count = db.session.scalar(db.select(db.func.count()).select_from(UserFeedback))

    return {
        "user_total": user_total,
        "user_active": user_active,
        "user_pending": user_total - user_active,
        "user_archived": db.session.scalar(
            db.select(db.func.count()).select_from(UserAccount).where(UserAccount.is_archived.is_(True))
        ),
        "event_total": sum(event_counts.values()),
        "event_counts": event_counts,
        "outbox_pending": outbox_pending,
        "outbox_failed": outbox_failed,
        "outbox_sent": outbox_sent,
        "outbox_last_status": outbox_last.status if outbox_last else None,
        "outbox_last_sent": outbox_last_sent,
        "recent_audit": recent_audit,
        "feedback_count": feedback_count,
    }


@admin_bp.route("/")
@login_required
def index() -> str:
    require_permission("admin.view")

    settings = get_settings()
    now = datetime.now(timezone.utc)

    db_health = _check_db_health()
    sched = _check_scheduler(settings, now)
    smtp = _check_smtp(settings)
    stats = _admin_statistics(now)

    return render_template(
        "admin/index.html",
        db_status=db_health["status"],
        db_ms=db_health["ms"],
        db_error=db_health["error"],
        sched_status=sched["status"],
        sched_last=sched["last"],
        sched_age_s=sched["age_s"],
        smtp_configured=smtp["configured"],
        smtp_status=smtp["status"],
        smtp_ms=smtp["ms"],
        smtp_error=smtp["error"],
        settings=settings,
        now=now,
        **stats,
    )


# ── Permission matrix ─────────────────────────────────────────────────────────


@admin_bp.route("/permissions")
@login_required
def permissions() -> str:
    require_permission("admin.view")

    from app.models.role import ALL_PERMISSIONS, ROLE_PERMISSIONS  # pylint: disable=import-outside-toplevel

    role_names = list(ROLE_PERMISSIONS.keys())  # Admin, Coordinator, Member, Viewer
    return render_template(
        "admin/permissions.html",
        all_permissions=ALL_PERMISSIONS,
        role_names=role_names,
        role_permissions=ROLE_PERMISSIONS,
    )


# ── Audit log ─────────────────────────────────────────────────────────────────


@admin_bp.route("/audit-log/")
@login_required
def audit_log_list() -> str:
    require_permission("admin.view")

    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel

    page = request.args.get("page", 1, type=int)
    f_entity_type = request.args.get("entity_type", "").strip()
    f_actor_id = request.args.get("actor_id", "").strip()
    f_action_type = request.args.get("action_type", "").strip()
    f_date_from = request.args.get("date_from", "").strip()
    f_date_to = request.args.get("date_to", "").strip()
    f_q = request.args.get("q", "").strip()

    query = db.select(AuditLogEntry).order_by(AuditLogEntry.timestamp.desc())

    if f_entity_type:
        query = query.where(AuditLogEntry.entity_type == f_entity_type)
    if f_actor_id:
        query = query.where(AuditLogEntry.actor_id == f_actor_id)
    if f_action_type:
        query = query.where(AuditLogEntry.action_type == f_action_type)
    if f_date_from:
        try:
            dt_from = datetime.strptime(f_date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.where(AuditLogEntry.timestamp >= dt_from)
        except ValueError:
            pass
    if f_date_to:
        try:
            dt_to = datetime.strptime(f_date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # include the full day
            query = query.where(AuditLogEntry.timestamp < dt_to + timedelta(days=1))
        except ValueError:
            pass
    if f_q:
        query = query.where(AuditLogEntry.summary.ilike(f"%{f_q}%"))

    # Paginate manually: count + offset/limit
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    entries = db.session.scalars(query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)).all()
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    # Distinct values for filter dropdowns
    entity_types = db.session.scalars(
        db.select(AuditLogEntry.entity_type).distinct().order_by(AuditLogEntry.entity_type)
    ).all()
    action_types = db.session.scalars(
        db.select(AuditLogEntry.action_type).distinct().order_by(AuditLogEntry.action_type)
    ).all()
    actors = db.session.scalars(
        db.select(UserAccount)
        .where(
            UserAccount.id.in_(db.select(AuditLogEntry.actor_id).where(AuditLogEntry.actor_id.isnot(None)).distinct())
        )
        .order_by(collate(UserAccount.name, CS_COLLATION))
    ).all()

    return render_template(
        "admin/audit_log_list.html",
        entries=entries,
        page=page,
        total=total,
        total_pages=total_pages,
        entity_types=entity_types,
        action_types=action_types,
        actors=actors,
        f_entity_type=f_entity_type,
        f_actor_id=f_actor_id,
        f_action_type=f_action_type,
        f_date_from=f_date_from,
        f_date_to=f_date_to,
        f_q=f_q,
    )


@admin_bp.route("/audit-log/<int:entry_id>")
@login_required
def audit_log_detail(entry_id: int) -> str:
    require_permission("admin.view")

    from app.models.audit import AuditLogEntry  # pylint: disable=import-outside-toplevel

    entry = get_or_404(AuditLogEntry, entry_id)

    return render_template("admin/audit_log_detail.html", entry=entry)
