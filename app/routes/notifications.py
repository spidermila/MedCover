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
from app.models.settings import get_settings
from app.utils import audit, diff_changes, require_permission

notifications_bp = Blueprint("notifications", __name__, url_prefix="/admin/notifications")

_DELAY_TIER_FIELDS: list[str] = [
    "notify_delay_under_24h_min",
    "notify_delay_1_7_days_min",
    "notify_delay_1_4_weeks_min",
    "notify_delay_over_month_min",
]

_DELAY_TIER_LABELS_CS: dict[str, str] = {
    "notify_delay_under_24h_min": "Do 24 hodin do akce",
    "notify_delay_1_7_days_min": "1\u20137 dn\u00ed do akce",
    "notify_delay_1_4_weeks_min": "1\u20134 t\u00fddny do akce",
    "notify_delay_over_month_min": "V\u00edce ne\u017e m\u011bs\u00edc do akce",
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
            flash(f'Hodnota pro \u201e{label}" nesm\u00ed b\u00fdt pr\u00e1zdn\u00e1.', "warning")
            return redirect(url_for("notifications.index"))

        try:
            value = int(raw)
        except ValueError:
            flash(
                f'Hodnota pro \u201e{label}" mus\u00ed b\u00fdt cel\u00e9 \u010d\u00edslo (zad\u00e1no: \u201e{raw}").',
                "warning",
            )
            return redirect(url_for("notifications.index"))

        if value < _DELAY_TIER_MIN:
            flash(
                f'Hodnota pro \u201e{label}" mus\u00ed b\u00fdt alespo\u0148 1 minuta (zad\u00e1no: {value}).',
                "warning",
            )
            return redirect(url_for("notifications.index"))

        if value > _DELAY_TIER_MAX:
            flash(
                f'Hodnota pro \u201e{label}" nesm\u00ed p\u0159ekro\u010dit 20 160 minut (zad\u00e1no: {value}).',
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
        "Nastaven\u00ed zpo\u017ed\u011bn\u00ed notifikac\u00ed bylo upraveno.",
        diff_changes(before, after),
    )
    db.session.commit()

    flash("Nastaven\u00ed zpo\u017ed\u011bn\u00ed notifikac\u00ed bylo ulo\u017eeno.", "success")
    return redirect(url_for("notifications.index"))


def _recent_events() -> list[Event]:
    """Return the 20 most recently created non-archived events for the test dropdown."""
    return db.session.scalars(
        db.select(Event).where(Event.archived == sa.false()).order_by(Event.start_datetime.desc()).limit(20)
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
    from app.utils import external_url_for  # pylint: disable=import-outside-toplevel

    # Temporarily override the outbox recipient for this request.
    g._test_notification_email = test_email

    try:
        if code == "assignment_confirmed":
            mailer.send_assignment_confirmed(current_user, event)
        elif code == "assignment_released":
            mailer.send_assignment_released(current_user, event)
        elif code == "event_published":
            mailer.send_event_published(current_user, event)
        elif code == "assignments_opened":
            mailer.send_assignments_opened(current_user, event)
        elif code == "event_cancelled":
            mailer.send_event_cancelled(current_user, event)
        elif code == "event_changed":
            event_url = external_url_for("events.detail", event_id=event.id)
            fake_changes: dict = {"description": ["—", "Zkušební oznámení"]}
            mailer.send_event_changed(current_user, event, fake_changes, event_url=event_url)
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
        db.session.commit()
        flash(f"Zkušební oznámení ({code}) zařazeno do fronty pro {test_email}.", "success")
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(f"Zkušební oznámení se nepodařilo odeslat: {exc}", "danger")

    return redirect(url_for("notifications.index"))
