"""iCal calendar feed — per-user subscription endpoint.

GET  /calendar/<token>.ics  — public, no login required.
POST /calendar/regenerate    — authenticated, user.edit_own permission.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import selectinload
from flask import Blueprint, Response, abort, redirect, url_for
from flask_login import current_user, login_required
from icalendar import Calendar, Event as ICalEvent

from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot, EventStatus
from app.models.user import UserAccount
from app.utils import audit, external_url_for, get_app_tz, require_permission

log = logging.getLogger(__name__)

calendar_bp = Blueprint("calendar", __name__, url_prefix="/calendar")

# Events in these statuses are excluded from the feed.
_EXCLUDED_STATUSES = {EventStatus.CANCELLED, EventStatus.COMPLETED}


@calendar_bp.route("/<token>.ics")
def feed(token: str) -> Response:
    """Return an iCal feed for the user identified by *token*.

    The route is intentionally public (no ``@login_required``) so that
    calendar apps can subscribe without OAuth.  The 64-char hex token acts
    as a shared secret — treat it like a password.
    """
    user: UserAccount | None = db.session.scalar(
        sa.select(UserAccount).where(UserAccount.ical_token == token)
    )
    if user is None or not user.is_active:
        abort(404)

    assignments = db.session.scalars(
        sa.select(Assignment)
        .join(Assignment.spot)
        .join(EventSpot.event)
        .where(
            Assignment.user_id == user.id,
            Event.status.notin_([s.value for s in _EXCLUDED_STATUSES]),
        )
        .options(
            selectinload(Assignment.spot).selectinload(EventSpot.event)  # type: ignore[arg-type]
        )
    ).all()

    cal = Calendar()
    cal.add("prodid", "-//MedCover//MedCover//CS")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"MedCover – {user.name}")
    cal.add("x-wr-caldesc", "Vaše akce v systému MedCover")
    cal.add("x-wr-timezone", str(get_app_tz()))
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("refresh-interval;value=duration", "PT4H")
    cal.add("x-published-ttl", "PT4H")

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

    ics_bytes = cal.to_ical()
    return Response(
        ics_bytes,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=medcover.ics",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@calendar_bp.route("/regenerate", methods=["POST"])
@login_required
def regenerate() -> Response:
    """Regenerate the iCal token for the current user.

    Requires ``user.edit_own`` permission.  Immediately invalidates any
    existing calendar subscriptions using the old token.
    """
    require_permission("user.edit_own")
    old_token = current_user.ical_token
    current_user.regenerate_ical_token()
    audit(
        "update",
        "UserAccount",
        str(current_user.id),
        f"Uživatel {current_user.email} vygeneroval nový iCal token.",
        changes={"ical_token": {"before": bool(old_token), "after": True}},
    )
    db.session.commit()
    log.info("iCal token regenerated for user %s", current_user.email)
    return redirect(url_for("users.profile", _anchor="ical"))
