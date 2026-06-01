"""iCal calendar feed — per-user subscription endpoints.

GET  /calendar/<token>.ics      — personal feed (user's own assignments).
GET  /calendar/all/<token>.ics  — all non-archived events feed.
POST /calendar/regenerate       — regenerate personal iCal token.
POST /calendar/regenerate-all   — regenerate all-events iCal token.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, Response, abort, redirect, url_for
from flask_login import current_user, login_required
from icalendar import Calendar
from icalendar import Event as ICalEvent
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.user import UserAccount
from app.utils import audit, external_url_for, get_app_tz, require_permission

log = logging.getLogger(__name__)

calendar_bp = Blueprint("calendar", __name__, url_prefix="/calendar")

# Personal feed: exclude cancelled, completed, and archived events.
_PERSONAL_EXCLUDED_STATUSES = {EventStatus.CANCELLED, EventStatus.COMPLETED}


def _make_calendar(name: str, description: str) -> Calendar:
    """Create a base iCal Calendar object with standard properties."""
    cal = Calendar()
    cal.add("prodid", "-//MedCover//MedCover//CS")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-caldesc", description)
    cal.add("x-wr-timezone", str(get_app_tz()))
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("refresh-interval;value=duration", "PT4H")
    cal.add("x-published-ttl", "PT4H")
    return cal


def _ical_response(cal: Calendar) -> Response:
    """Render a Calendar object as an HTTP response."""
    return Response(
        cal.to_ical(),
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=medcover.ics",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@calendar_bp.route("/<token>.ics")
def feed(token: str) -> Response:
    """Return an iCal feed for the user identified by *token*.

    Contains only events the user is assigned to (excluding cancelled,
    completed, and archived events).
    """
    user: UserAccount | None = db.session.scalar(sa.select(UserAccount).where(UserAccount.ical_token == token))
    if user is None or not user.is_active or user.is_archived:
        abort(404)

    assignments = db.session.scalars(
        sa.select(Assignment)
        .join(Assignment.spot)
        .join(EventSpot.event)
        .where(
            Assignment.user_id == user.id,
            Event.status.notin_(_PERSONAL_EXCLUDED_STATUSES),
            Event.archived.is_(False),
        )
        .options(selectinload(Assignment.spot).selectinload(EventSpot.event))  # type: ignore[arg-type]
    ).all()

    cal = _make_calendar(f"MedCover – {user.name}", "Vaše akce v systému MedCover")

    for assignment in assignments:
        spot = assignment.spot
        event = spot.event

        vevent = ICalEvent()
        vevent.add("uid", f"event-{event.id}@medcover")
        vevent.add("summary", event.name)
        vevent.add("dtstart", event.start_datetime.astimezone(timezone.utc))
        vevent.add("dtend", event.end_datetime.astimezone(timezone.utc))
        vevent.add("dtstamp", datetime.now(timezone.utc))

        if event.address:
            vevent.add("location", event.address)

        description_parts: list[str] = []
        if spot.description:
            description_parts.append(f"Místo: {spot.description}")
        if event.description:
            description_parts.append(event.description)
        event_url = external_url_for("events.detail", event_id=event.id)
        description_parts.append(f"Detail akce: {event_url}")
        vevent.add("description", "\n".join(description_parts))

        vevent.add("url", event_url)
        cal.add_component(vevent)

    return _ical_response(cal)


@calendar_bp.route("/all/<token>.ics")
def feed_all(token: str) -> Response:
    """Return an iCal feed with all non-archived events (except cancelled).

    Includes completed events. Each entry shows status, spots with
    qualifications and assigned users, and the responsible person.
    """
    user: UserAccount | None = db.session.scalar(sa.select(UserAccount).where(UserAccount.ical_all_token == token))
    if user is None or not user.is_active or user.is_archived:
        abort(404)

    events = db.session.scalars(
        sa.select(Event)
        .where(
            Event.archived.is_(False),
            Event.status != EventStatus.CANCELLED,
        )
        .options(
            selectinload(Event.spots).selectinload(EventSpot.assignment).selectinload(Assignment.user),  # type: ignore[arg-type]
            selectinload(Event.spots).selectinload(EventSpot.required_qualifications),  # type: ignore[arg-type]
            selectinload(Event.responsible_person),  # type: ignore[arg-type]
        )
        .order_by(Event.start_datetime)
    ).all()

    cal = _make_calendar(f"MedCover – všechny akce ({user.name})", "Všechny akce v systému MedCover")

    for event in events:
        vevent = ICalEvent()
        vevent.add("uid", f"event-{event.id}@medcover")
        vevent.add("summary", event.name)
        vevent.add("dtstart", event.start_datetime.astimezone(timezone.utc))
        vevent.add("dtend", event.end_datetime.astimezone(timezone.utc))
        vevent.add("dtstamp", datetime.now(timezone.utc))

        if event.address:
            vevent.add("location", event.address)

        description_parts: list[str] = []
        description_parts.append(f"Stav: {event.status.value}")

        if event.responsible_person:
            description_parts.append(f"Zodpovědná osoba: {event.responsible_person.name}")

        # Spots summary
        if event.spots:
            description_parts.append("")
            description_parts.append("Pozice:")
            for spot in event.spots:
                quals = ", ".join(q.name for q in spot.required_qualifications if not q.is_deleted)
                assigned = spot.assignment.user.name if spot.assignment else "neobsazeno"
                spot_desc = spot.description or "—"
                line = f"  • {spot_desc}"
                if quals:
                    line += f" [{quals}]"
                line += f" → {assigned}"
                description_parts.append(line)

        if event.description:
            description_parts.append("")
            description_parts.append(event.description)

        event_url = external_url_for("events.detail", event_id=event.id)
        description_parts.append(f"\nDetail akce: {event_url}")
        vevent.add("description", "\n".join(description_parts))

        vevent.add("url", event_url)
        cal.add_component(vevent)

    return _ical_response(cal)


@calendar_bp.route("/regenerate", methods=["POST"])
@login_required
def regenerate() -> Response:
    """Regenerate the personal iCal token for the current user."""
    require_permission("user.edit_own")
    old_token = current_user.ical_token
    current_user.regenerate_ical_token()
    audit(
        "edit",
        "UserAccount",
        str(current_user.id),
        f"Uživatel {current_user.email} vygeneroval nový iCal token.",
        changes={"ical_token": {"before": bool(old_token), "after": True}},
    )
    db.session.commit()
    log.info("iCal token regenerated for user %s", current_user.email)
    return redirect(url_for("users.profile", _anchor="ical"))


@calendar_bp.route("/regenerate-all", methods=["POST"])
@login_required
def regenerate_all() -> Response:
    """Regenerate the all-events iCal token for the current user."""
    require_permission("user.edit_own")
    old_token = current_user.ical_all_token
    current_user.regenerate_ical_all_token()
    audit(
        "edit",
        "UserAccount",
        str(current_user.id),
        f"Uživatel {current_user.email} vygeneroval nový iCal token (všechny akce).",
        changes={"ical_all_token": {"before": bool(old_token), "after": True}},
    )
    db.session.commit()
    log.info("iCal all-events token regenerated for user %s", current_user.email)
    return redirect(url_for("users.profile", _anchor="ical-all"))
