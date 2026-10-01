"""Change log shown at the bottom of the event detail page, built from the audit log."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventType
from app.models.master_event import MasterEvent
from app.models.user import UserAccount
from app.utils import to_local

FIELD_LABELS = {
    "name": "Název",
    "master_event_id": "Nadřazená akce",
    "event_type": "Typ",
    "start_datetime": "Začátek",
    "end_datetime": "Konec",
    "actual_start_datetime": "Skutečný začátek",
    "actual_end_datetime": "Skutečný konec",
    "post_event_count": "Výsledný počet",
    "address": "Adresa",
    "contact_person": "Kontaktní osoba",
    "description": "Popis",
    "paid": "Placená",
    "status": "Stav",
    "responsible_person_id": "Zodpovědná osoba",
    "assignments_open_datetime": "Otevření přihlášek",
    "planned_participants_count": "Plánovaný počet účastníků",
    "minimum_participants": "Minimum účastníků",
    "maximum_participants": "Maximum účastníků",
    "qualification_requirements": "Požadované kvalifikace",
}


@dataclass
class LogLine:
    timestamp: datetime
    actor: str
    summary: str
    changes: list[tuple[str, str, str]] = field(default_factory=list)


def _pairs(changes: object) -> dict[str, tuple[object, object]]:
    """Normalise the stored change shapes into {field: (before, after)}.

    Three shapes exist: {field: [before, after]}, {field: {"before", "after"}}
    and {"before": {field: …}, "after": {field: …}}.
    """
    if not isinstance(changes, dict):
        return {}
    before, after = changes.get("before"), changes.get("after")
    if set(changes) == {"before", "after"} and isinstance(before, dict) and isinstance(after, dict):
        return {k: (before.get(k), after.get(k)) for k in {**before, **after}}
    pairs: dict[str, tuple[object, object]] = {}
    for key, value in changes.items():
        if isinstance(value, list) and len(value) == 2:
            pairs[key] = (value[0], value[1])
        elif isinstance(value, dict) and set(value) == {"before", "after"}:
            pairs[key] = (value["before"], value["after"])
    return pairs


def _format(key: str, value: object, names: dict[str, str]) -> str:
    if value is None or value in ("", "None"):
        return "—"
    if isinstance(value, bool):
        return "ano" if value else "ne"
    if key.endswith("_datetime"):
        try:
            return to_local(datetime.fromisoformat(str(value))).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return str(value)
    if key == "event_type":
        return EventType[str(value)].value if value in EventType.__members__ else str(value)
    if key == "qualification_requirements" and isinstance(value, list):
        return ", ".join(f"{name} ×{count}" for name, count in value) or "—"
    if key in ("responsible_person_id", "master_event_id"):
        return names.get(f"{key}:{value}", str(value))
    return str(value)


def _lookup_names(pairs: list[dict[str, tuple[object, object]]]) -> dict[str, str]:
    """Resolve user / master-event ids referenced in the diffs to names, one query each."""
    user_ids: set[UUID] = set()
    me_ids: set[int] = set()
    for p in pairs:
        for value in p.get("responsible_person_id", ()):
            try:
                user_ids.add(UUID(str(value)))
            except ValueError:
                pass
        for value in p.get("master_event_id", ()):
            if isinstance(value, int):
                me_ids.add(value)
    names: dict[str, str] = {}
    if user_ids:
        for uid, name in db.session.execute(
            db.select(UserAccount.id, UserAccount.name).where(UserAccount.id.in_(user_ids))
        ):
            names[f"responsible_person_id:{uid}"] = name
    if me_ids:
        for mid, name in db.session.execute(
            db.select(MasterEvent.id, MasterEvent.name).where(MasterEvent.id.in_(me_ids))
        ):
            names[f"master_event_id:{mid}"] = name
    return names


def event_log(event: Event) -> list[LogLine]:
    """Return the event's audit entries, newest first, with field changes labelled and formatted."""
    rows = db.session.execute(
        db.select(AuditLogEntry, UserAccount.name)
        .outerjoin(UserAccount, AuditLogEntry.actor_id == UserAccount.id)
        .where(AuditLogEntry.entity_type == "Event", AuditLogEntry.entity_id == str(event.id))
        .order_by(AuditLogEntry.timestamp.desc(), AuditLogEntry.id.desc())
    ).all()
    all_pairs = [_pairs(entry.changes_json) for entry, _ in rows]
    names = _lookup_names(all_pairs)
    lines = []
    for (entry, actor), pairs in zip(rows, all_pairs):
        changes = []
        for key in sorted(pairs, key=lambda k: list(FIELD_LABELS).index(k) if k in FIELD_LABELS else len(FIELD_LABELS)):
            before, after = (_format(key, v, names) for v in pairs[key])
            if before != after:
                changes.append((FIELD_LABELS.get(key, key), before, after))
        lines.append(LogLine(entry.timestamp, actor or "Systém", entry.summary, changes))
    return lines
