"""
Event blueprint package.

Lifecycle state machine:
  Draft → Published (manual, event.publish)
  Published → Assignments Open (manual trigger or automatic via scheduler)
  Assignments Open → Assignments Closed (manual or auto when fully staffed)
  Assignments Closed → Assignments Open (manual re-open)
  Assignments Closed → Completed (automatic after end_datetime passes)
  Any non-Completed → Cancelled (manual, event.cancel)
  Cancelled → Draft (manual restore, event.restore)
  Completed cannot be cancelled.

Permissions:
  event.view         — view published+ events
  event.view_draft   — view draft events
  event.create       — create events
  event.edit         — edit events
  event.publish      — Draft → Published
  event.assignments.open  — Published → Assignments Open (manual)
  event.assignments.close — Assignments Open → Assignments Closed
  event.cancel       — → Cancelled
  event.restore      — Cancelled → Draft
"""

from flask import Blueprint

events_bp = Blueprint("events", __name__, url_prefix="/events")

# Import sub-modules so their @events_bp route decorators are registered.
from . import crud, equipment, spots, transitions  # noqa: E402, F401
