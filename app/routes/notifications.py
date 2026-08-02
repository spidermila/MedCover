"""
Admin notification management route.

Provides a catalog of all email notification types defined in NOTIFICATION_CATALOG
and allows admins to toggle each configurable type on/off via AppSettings.
"""

import sqlalchemy as sa
from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.mail import NOTIFICATION_CATALOG
from app.models.event import Event
from app.models.outbox import OutboxEmail
from app.models.settings import get_settings
from app.utils import audit, diff_changes, get_app_tz, require_permission

notifications_bp = Blueprint("notifications", __name__, url_prefix="/admin/notifications")

_DELAY_TIER_FIELDS: list[str] = [
    "notify_delay_under_24h_min",
    "notify_delay_1_7_days_min",
    "notify_delay_1_4_weeks_min",
    "notify_delay_over_month_min",
]

_DELAY_TIER_LABELS_CS: dict[str, str] = {
    "notify_delay_under_24h_min": "Do 24 hodin do akce",
    "notify_delay_1_7_days_min": "1–7 dní do akce",
    "notify_delay_1_4_weeks_min": "1–4 týdny do akce",
    "notify_delay_over_month_min": "Více než měsíc do akce",
}

_DELAY_TIER_MIN: int = 1
_DELAY_TIER_MAX: int = 20160


def _build_toggle_groups(catalog: list[dict]) -> list[dict]:
    """Group catalog entries by settings_field for the toggle UI.

    Returns a list of dicts: {settings_field, label_cs, entries} sorted by
    first appearance in the catalog.  Always-on entries are excluded.
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for entry in catalog:
        field = entry["settings_field"]
        if field is None:
            continue
        if field not in seen:
            seen[field] = {"settings_field": field, "entries": []}
            order.append(field)
        seen[field]["entries"].append(entry)
    return [seen[f] for f in order]


@notifications_bp.route("/", methods=["GET", "POST"])
@login_required
def index() -> str | Response:
    require_permission("admin.manage_settings")
    settings = get_settings()

    # Unique togglable fields (one checkbox per field, not per catalog entry)
    togglable_fields = {
        entry["settings_field"] for entry in NOTIFICATION_CATALOG if entry["settings_field"] is not None
    }
    toggle_groups = _build_toggle_groups(NOTIFICATION_CATALOG)

    if request.method == "POST":
        before = {field: getattr(settings, field, True) for field in togglable_fields}

        for field in togglable_fields:
            setattr(settings, field, field in request.form)

        after = {field: getattr(settings, field) for field in togglable_fields}
        changes = diff_changes(before, after)

        audit("edit", "AppSettings", 1, "Nastavení e-mailových oznámení bylo upraveno.", changes)
        db.session.commit()
        flash("Nastavení oznámení bylo uloženo.", "success")
        return redirect(url_for("notifications.index"))

    return render_template(
        "admin/notifications.html",
        catalog=NOTIFICATION_CATALOG,
        toggle_groups=toggle_groups,
        settings=settings,
        recent_events=_recent_events(),
    )


@notifications_bp.route("/delay-tiers", methods=["POST"])
@login_required
def save_delay_tiers() -> Response:
    require_permission("admin.manage_settings")
    settings = get_settings()

    parsed: dict[str, int] = {}
    for field in _DELAY_TIER_FIELDS:
        label = _DELAY_TIER_LABELS_CS[field]
        raw = request.form.get(field, "").strip()

        if not raw:
            flash(f"Hodnota pro „{label}“ nesmí být prázdná.", "warning")
            return redirect(url_for("notifications.index"))

        try:
            value = int(raw)
        except ValueError:
            flash(
                f"Hodnota pro „{label}“ musí být celé číslo (zadáno: „{raw}“).",
                "warning",
            )
            return redirect(url_for("notifications.index"))

        if value < _DELAY_TIER_MIN:
            flash(
                f"Hodnota pro „{label}“ musí být alespoň 1 minuta (zadáno: {value}).",
                "warning",
            )
            return redirect(url_for("notifications.index"))

        if value > _DELAY_TIER_MAX:
            flash(
                f"Hodnota pro „{label}“ nesmí překročit 20 160 minut (zadáno: {value}).",
                "warning",
            )
            return redirect(url_for("notifications.index"))

        parsed[field] = value

    before = {f: getattr(settings, f) for f in _DELAY_TIER_FIELDS}
    for field in _DELAY_TIER_FIELDS:
        setattr(settings, field, parsed[field])
    after = {f: getattr(settings, f) for f in _DELAY_TIER_FIELDS}

    audit(
        "edit",
        "AppSettings",
        1,
        "Nastavení zpoždění notifikací bylo upraveno.",
        diff_changes(before, after),
    )
    db.session.commit()

    flash("Nastavení zpoždění notifikací bylo uloženo.", "success")
    return redirect(url_for("notifications.index"))


_RECENT_EVENTS_LIMIT: int = 100


def _recent_events() -> list[Event]:
    """Return the most recent non-archived events for the test dropdown.

    Bounded to _RECENT_EVENTS_LIMIT to keep the query and the rendered
    <select> from growing unbounded as event history accumulates over years.
    """
    return db.session.scalars(
        db.select(Event)
        .where(Event.archived == sa.false())
        .order_by(Event.start_datetime.desc())
        .limit(_RECENT_EVENTS_LIMIT)
    ).all()


# ── Notification test ─────────────────────────────────────────────────────────

_TESTABLE_CODES = {e["code"] for e in NOTIFICATION_CATALOG if e["settings_field"] is not None}


@notifications_bp.route("/test/<string:code>", methods=["POST"])
@login_required
def test_notification(code: str) -> Response:
    require_permission("admin.manage_settings")

    if code not in _TESTABLE_CODES:
        flash("Neznámý typ oznámení.", "warning")
        return redirect(url_for("notifications.index"))

    test_email = request.form.get("test_email", "").strip()
    if not test_email:
        flash("Zadejte e-mailovou adresu pro zkušební oznámení.", "warning")
        return redirect(url_for("notifications.index"))

    event_id = request.form.get("test_event_id", "")
    event: Event | None = None
    if event_id:
        try:
            event = db.session.get(Event, int(event_id))
        except ValueError, TypeError:
            event = None
    if event is None:
        event = db.session.scalar(
            db.select(Event).where(Event.archived == sa.false()).order_by(Event.start_datetime.desc())
        )
    if event is None:
        flash("Nepodařilo se najít žádnou akci pro zkušební oznámení.", "warning")
        return redirect(url_for("notifications.index"))

    import app.mail as mailer  # pylint: disable=import-outside-toplevel

    send_immediately_raw = request.form.get("send_immediately", "0")
    send_immediately = send_immediately_raw == "1"

    # Temporarily override the outbox recipient for this request.
    g._test_notification_email = test_email
    if send_immediately:
        g._test_notification_immediate = True

    try:
        if code == "assignment_confirmed":
            mailer.send_assignment_confirmed(current_user, event, spot_description="Testovací pozice")
        elif code == "assignment_released":
            mailer.send_assignment_released(current_user, event, spot_description="Testovací pozice")
        elif code == "event_published":
            mailer.send_event_published(current_user, event)
        elif code == "assignments_opened":
            mailer.send_assignments_opened(current_user, event)
        elif code == "event_cancelled":
            mailer.send_event_cancelled(current_user, event)
        elif code == "event_archived":
            mailer.send_event_archived(current_user, event)
        elif code == "event_unarchived":
            mailer.send_event_unarchived(current_user, event)
        elif code == "event_changed":
            fake_changes: dict = {"description": ["—", "Zkušební oznámení"]}
            mailer.send_event_changed(current_user, event, fake_changes)
        elif code == "unfilled_reminder":
            from app.models.event import EventSpot  # pylint: disable=import-outside-toplevel

            spots = db.session.scalars(db.select(EventSpot).where(EventSpot.event_id == event.id).limit(5)).all()
            mailer.send_unfilled_spots_reminder(current_user, event, unfilled=list(spots) or [None])
        elif code == "debriefing_invitation":
            # Build a minimal stand-in assignment for the debriefing URL
            from app.models.assignment import Assignment  # pylint: disable=import-outside-toplevel
            from app.models.event import EventSpot  # pylint: disable=import-outside-toplevel

            fake_assignment = db.session.scalar(
                db.select(Assignment)
                .join(EventSpot, Assignment.spot_id == EventSpot.id)
                .where(EventSpot.event_id == event.id)
                .limit(1)
            )
            if fake_assignment is None:
                flash("Akce nemá žádné přihlášení — nelze odeslat zkušební pozvánku k debriefingu.", "warning")
                return redirect(url_for("notifications.index"))
            mailer.send_debriefing_invitation(fake_assignment, event)
        else:
            # Safety net: _TESTABLE_CODES should always have a matching branch above.
            # Fail loudly instead of silently no-op'ing and reporting false success.
            flash(f"Zkušební oznámení pro typ '{code}' není v kódu implementováno.", "danger")
            return redirect(url_for("notifications.index"))
        db.session.commit()

        latest = db.session.scalar(
            db.select(OutboxEmail)
            .where(
                OutboxEmail.to_email == test_email,
                OutboxEmail.notification_type == code,
                OutboxEmail.event_id == event.id,
                OutboxEmail.status == "pending",
            )
            .order_by(OutboxEmail.created_at.desc())
            .limit(1)
        )
        if latest is not None and latest.send_after is not None:
            local = latest.send_after.astimezone(get_app_tz())
            flash(
                f"Zkušební oznámení ({code}) zařazeno do fronty pro {test_email} "
                f"(odloženo do {local.strftime('%d.%m.%Y %H:%M')}).",
                "success",
            )
        else:
            flash(
                f"Zkušební oznámení ({code}) bude odesláno okamžitě na {test_email}.",
                "success",
            )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(f"Zkušební oznámení se nepodařilo odeslat: {exc}", "danger")

    return redirect(url_for("notifications.index"))
