"""
Import blueprint — batch import of events from a JSON payload.

Routes:
  GET  /import/events/           paste form
  POST /import/events/preview    validate JSON + show editable preview
  POST /import/events/confirm    create events in one transaction
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from sqlalchemy import collate
from app.utils import CS_COLLATION, audit, get_app_tz, require_permission
from app.models.assignment import Assignment, DebriefingRecord
from app.models.event import Event, EventSpot, EventStatus, EventType
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount

import_bp = Blueprint("import_events", __name__, url_prefix="/import")

# ── Helpers ───────────────────────────────────────────────────────────────────


def _require_import_permission() -> None:
    """Abort 403 unless the current user may import events."""
    require_permission("admin.manage_settings")


def _match_responsible_person(
    name_str: str | None,
    users: list[UserAccount],
) -> tuple[UserAccount | None, str]:
    """Try to match a name string to a UserAccount.

    Returns (user_or_None, confidence) where confidence is one of:
      "exact"   – matched by exact full name
      "iexact"  – matched case-insensitively
      "reversed"– matched by reversing "Lastname Firstname" → "Firstname Lastname"
      "none"    – no match found
    """
    if not name_str or not name_str.strip():
        return None, "none"

    name_str = name_str.strip()

    # Build lookup maps
    by_name: dict[str, UserAccount] = {u.name: u for u in users}
    by_name_lower: dict[str, UserAccount] = {u.name.lower(): u for u in users}

    # 1. Exact
    if name_str in by_name:
        return by_name[name_str], "exact"

    # 2. Case-insensitive
    if name_str.lower() in by_name_lower:
        return by_name_lower[name_str.lower()], "iexact"

    # 3. Reversed — safety net for any name that arrives in the wrong order
    parts = name_str.split()
    if len(parts) == 2:
        reversed_name = f"{parts[1]} {parts[0]}"
        if reversed_name in by_name:
            return by_name[reversed_name], "reversed"
        if reversed_name.lower() in by_name_lower:
            return by_name_lower[reversed_name.lower()], "reversed"

    return None, "none"


def _process_import_users(
    form: dict,
    user_count: int,
) -> tuple[int, int, dict[str, UserAccount], dict[str, UserAccount]]:
    """Create new users from the import form.

    Returns (created_count, skipped_count, by_name_map, by_email_map).
    The maps include both pre-existing and newly created users for idempotency.
    """
    def _qual_by_substr(keyword: str) -> Qualification | None:
        kw = keyword.lower()
        return db.session.scalars(
            db.select(Qualification).where(
                Qualification.is_deleted.is_(False),
                db.func.lower(Qualification.name).contains(kw),
            )
        ).first()

    user_zdravotnik_qual = _qual_by_substr("zdravotník") or _qual_by_substr("zdravotnik")
    user_zelenac_qual = _qual_by_substr("zelenáč") or _qual_by_substr("zelenac")
    member_role = db.session.scalars(
        db.select(Role).where(Role.name == "Member")
    ).first()

    pre_existing = list(db.session.scalars(db.select(UserAccount)).all())
    existing_by_name: dict[str, UserAccount] = {u.name.lower(): u for u in pre_existing}
    existing_by_email: dict[str, UserAccount] = {
        u.email.lower(): u for u in pre_existing if u.email
    }

    created = 0
    skipped = 0

    for i in range(user_count):
        uprefix = f"user_{i}_"
        db_id = form.get(f"{uprefix}db_id", "").strip()

        if db_id:
            skipped += 1
            continue

        include = form.get(f"{uprefix}include") == "1"
        if not include:
            skipped += 1
            continue

        name = form.get(f"{uprefix}name", "").strip()
        email = form.get(f"{uprefix}email", "").strip().lower()
        phone = form.get(f"{uprefix}phone", "").strip() or None
        is_zdravotnik = form.get(f"{uprefix}is_zdravotnik") == "1"

        if not name or not email:
            skipped += 1
            continue

        if name.lower() in existing_by_name or email.lower() in existing_by_email:
            skipped += 1
            continue

        new_user = UserAccount(name=name, email=email, phone=phone, is_active=True)
        import_as_archived = form.get(f"{uprefix}archived") == "1"
        if import_as_archived:
            new_user.is_archived = True
            new_user.is_active = False
        new_user.set_password(secrets.token_urlsafe(32))
        if member_role:
            new_user.roles = [member_role]
        u_qual = user_zdravotnik_qual if is_zdravotnik else user_zelenac_qual
        if u_qual:
            new_user.qualifications = [u_qual]

        db.session.add(new_user)
        db.session.flush()

        existing_by_name[name.lower()] = new_user
        if email:
            existing_by_email[email.lower()] = new_user

        audit("import", "UserAccount", new_user.id, f"Uživatel importován z Google Sheets: {name}", None)
        created += 1

    return created, skipped, existing_by_name, existing_by_email


def _create_spots_and_assignments(
    event: Event,
    form: dict,
    prefix: str,
    responsible_person_id: str | None,
    name_to_user: dict[str, UserAccount],
    is_past: bool,
    _get_int: Any,
    global_zdravotnik_qual_id: int | None,
    global_zelenac_qual_id: int | None,
    _qual: Any,
) -> int:
    """Create spots, assignments, and debriefings for a single imported event.

    Returns the number of auto-generated debriefings.
    """
    _IMPORT_DEBRIEFING_NOTE = (
        "importovaný historický dozor - tento debriefing byl "
        "vygenerován aplikací během importu"
    )

    signup_count_str = form.get(f"{prefix}signup_count", "0")
    signup_count = int(signup_count_str) if signup_count_str.isdigit() else 0
    signup_names = [
        form.get(f"{prefix}signup_{j}", "").strip()
        for j in range(signup_count)
    ]
    signup_names = [n for n in signup_names if n]

    total_people = len(signup_names) + (1 if responsible_person_id else 0)

    zdravotnik_qual_id = _get_int(f"{prefix}zdravotnik_qual_id") or global_zdravotnik_qual_id
    zelenac_qual_id = _get_int(f"{prefix}zelenac_qual_id") or global_zelenac_qual_id
    zdravotnik_qual = _qual(zdravotnik_qual_id)
    zelenac_qual = _qual(zelenac_qual_id)

    if total_people <= 3:
        spots_def: list[tuple[str, bool, Qualification | None]] = [
            ("Zdravotník", False, zdravotnik_qual),
            ("Zelenáč", False, zelenac_qual),
            ("Zelenáč", True, zelenac_qual),
        ]
    else:
        spots_def = [("Zdravotník", False, zdravotnik_qual)]
        for _ in signup_names:
            spots_def.append(("Zelenáč", False, zelenac_qual))

    zdravotnik_spot: EventSpot | None = None
    zelenac_spots: list[EventSpot] = []

    for desc, is_optional, qual in spots_def:
        spot = EventSpot(
            event_id=event.id,
            description=desc,
            is_optional=is_optional,
        )
        if qual:
            spot.required_qualifications = [qual]
        db.session.add(spot)
        if desc == "Zdravotník" and zdravotnik_spot is None:
            zdravotnik_spot = spot
        else:
            zelenac_spots.append(spot)

    db.session.flush()

    auto_debriefings = 0

    # RP → Zdravotník spot
    if responsible_person_id and zdravotnik_spot:
        rp_user_obj = db.session.get(UserAccount, responsible_person_id)
        if rp_user_obj:
            rp_assignment = Assignment(
                spot_id=zdravotnik_spot.id,
                user_id=rp_user_obj.id,
                assigned_by_id=current_user.id,
            )
            db.session.add(rp_assignment)
            if rp_user_obj.is_rp_eligible():
                event.responsible_person_id = rp_user_obj.id
            if is_past:
                db.session.flush()
                db.session.add(DebriefingRecord(
                    assignment_id=rp_assignment.id,
                    submitted_by_id=current_user.id,
                    grade=3,
                    feedback_event=_IMPORT_DEBRIEFING_NOTE,
                ))
                auto_debriefings += 1

    # Each signup → Zelenáč spots in order
    for j, signup_name in enumerate(signup_names):
        if j >= len(zelenac_spots):
            break
        signup_user = name_to_user.get(signup_name.lower())
        if signup_user:
            signup_assignment = Assignment(
                spot_id=zelenac_spots[j].id,
                user_id=signup_user.id,
                assigned_by_id=current_user.id,
            )
            db.session.add(signup_assignment)
            if is_past:
                db.session.flush()
                db.session.add(DebriefingRecord(
                    assignment_id=signup_assignment.id,
                    submitted_by_id=current_user.id,
                    grade=3,
                    feedback_event=_IMPORT_DEBRIEFING_NOTE,
                ))
                auto_debriefings += 1

    return auto_debriefings


def _parse_json_payload(raw: str) -> tuple[list[Any], list[Any], str | None]:
    """Parse raw JSON and normalise v1/v2 format.

    Returns (events_list, users_list, error_html).  When error_html is not None
    the caller should return it directly as the response.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        flash(f"Neplatný JSON: {exc}", "danger")
        return [], [], render_template("import/events.html", json_data=raw)

    if isinstance(payload, list):
        return payload, [], None
    if isinstance(payload, dict) and "events" in payload:
        return payload.get("events", []), payload.get("users", []), None

    flash(
        "Neplatný formát JSON: očekáváno pole akcí nebo objekt s klíčem 'events'.",
        "danger",
    )
    return [], [], render_template("import/events.html", json_data=raw)


def _build_users_preview(payload_users: list[Any], all_users: list[UserAccount]) -> list[dict[str, Any]]:
    """Build preview rows for the users section of the import."""
    db_by_name: dict[str, UserAccount] = {u.name.lower(): u for u in all_users}
    db_by_email: dict[str, UserAccount] = {u.email.lower(): u for u in all_users if u.email}
    rows: list[dict[str, Any]] = []

    for pu in payload_users:
        if not isinstance(pu, dict):
            continue
        gs_name = str(pu.get("gs_name", "")).strip()
        name = str(pu.get("name", "")).strip()
        email = str(pu.get("email") or "").strip().lower() or None
        phone = str(pu.get("phone") or "").strip() or None
        is_zdravotnik = bool(pu.get("is_zdravotnik", False))

        existing_user: UserAccount | None = None
        match_reason = "none"
        if name.lower() in db_by_name:
            existing_user = db_by_name[name.lower()]
            match_reason = "name"
        elif email and email.lower() in db_by_email:
            existing_user = db_by_email[email.lower()]
            match_reason = "email"

        rows.append({
            "gs_name": gs_name,
            "name": name,
            "email": email,
            "phone": phone,
            "is_zdravotnik": is_zdravotnik,
            "existing": existing_user,
            "match_reason": match_reason,
            "is_archived": existing_user.is_archived if existing_user else False,
        })
    return rows


def _existing_event_pairs() -> set[tuple[str, str]]:
    """Return set of (name, date_str) pairs for duplicate detection."""
    result: set[tuple[str, str]] = set()
    for ev in db.session.scalars(db.select(Event.name, Event.start_datetime)).all():
        dt = ev[1]
        if dt:
            date_part = dt[:10] if isinstance(dt, str) else dt.strftime("%Y-%m-%d")
        else:
            date_part = ""
        result.add((ev[0], date_part))
    return result


def _parse_datetime(date_str: str, time_str: str | None, tz: ZoneInfo) -> datetime:
    """Combine a YYYY-MM-DD date and HH:MM time string into a UTC-aware datetime."""
    t = time_str or "00:00"
    local = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    return local.astimezone(timezone.utc)


def _validate_row(row: Any, idx: int) -> tuple[dict | None, list[str]]:
    """Validate a single event dict from the JSON payload.

    Returns (cleaned_dict, errors).  If errors is non-empty, cleaned_dict is None.
    """
    errors: list[str] = []

    if not isinstance(row, dict):
        return None, [f"Řádek {idx + 1}: není objekt (dict)."]

    name = str(row.get("name", "")).strip()
    if not name:
        errors.append("Chybí název akce.")

    date_str = str(row.get("date", "")).strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        errors.append(f"Neplatné datum: '{date_str}' (očekáváno YYYY-MM-DD).")

    start_time = row.get("start_time")
    if start_time is not None:
        try:
            datetime.strptime(str(start_time), "%H:%M")
        except ValueError:
            errors.append(f"Neplatný čas začátku: '{start_time}'.")

    end_time = row.get("end_time")
    if end_time is not None:
        try:
            datetime.strptime(str(end_time), "%H:%M")
        except ValueError:
            errors.append(f"Neplatný čas konce: '{end_time}'.")

    if errors:
        return None, errors

    return {
        "name": name,
        "date": date_str,
        "start_time": start_time,
        "end_time": end_time,
        "location": str(row.get("location") or "").strip() or None,
        "paid": bool(row.get("paid", False)),
        "responsible_person": str(row.get("responsible_person") or "").strip() or None,
        "contact_person": str(row.get("contact_person") or "").strip() or None,
        "description": str(row.get("description") or "").strip(),
        "time_missing": bool(row.get("time_missing", False)),
        "cancelled": bool(row.get("cancelled", False)),
        "signups": [
            str(s).strip()
            for s in row.get("signups", [])
            if isinstance(s, str) and str(s).strip()
        ],
    }, []


# ── Routes ────────────────────────────────────────────────────────────────────

@import_bp.get("/events/")
@login_required
def events_paste() -> str:
    _require_import_permission()
    return render_template("import/events.html")


@import_bp.post("/events/preview")
@login_required
def events_preview() -> str | Response:
    _require_import_permission()

    raw = request.form.get("json_data", "").strip()
    if not raw:
        flash("Vložte JSON data.", "warning")
        return redirect(url_for("import_events.events_paste"))

    payload_events, payload_users, error_html = _parse_json_payload(raw)
    if error_html is not None:
        return error_html

    # ── Load DB lookups ─────────────────────────────────────────────────────
    all_users = list(db.session.scalars(
        db.select(UserAccount).order_by(collate(UserAccount.name, CS_COLLATION))
    ).all())
    active_users = [u for u in all_users if u.is_active]
    master_events = list(db.session.scalars(
        db.select(MasterEvent).order_by(collate(MasterEvent.name, CS_COLLATION))
    ).all())
    qualifications = list(db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all())

    users_preview_rows = _build_users_preview(payload_users, all_users)

    existing_events = _existing_event_pairs()

    # ── Validate each row + match RP ─────────────────────────────────────────
    parse_errors: list[tuple[int, list[str]]] = []
    preview_rows: list[dict[str, Any]] = []

    for idx, raw_row in enumerate(payload_events):
        cleaned, errs = _validate_row(raw_row, idx)
        if errs or cleaned is None:
            parse_errors.append((idx + 1, errs))
            continue
        assert cleaned is not None  # help mypy narrow past the guard

        rp_user, rp_confidence = _match_responsible_person(cleaned["responsible_person"], active_users)

        warnings: list[str] = []
        if cleaned["time_missing"]:
            warnings.append("Čas akce nebyl zadán — akce bude vytvořena bez pozic.")
        if cleaned["responsible_person"] and rp_confidence == "none":
            rp_name = cleaned["responsible_person"]
            warnings.append(
                f'Zodpovědná osoba "{rp_name}" nebyla nalezena v databázi. Přiřaďte ručně.'
            )
        elif rp_confidence in ("iexact", "reversed"):
            rp_name_found = rp_user.name if rp_user else ""
            warnings.append(
                f'Zodpovědná osoba spárována přibližně: "{rp_name_found}". Zkontrolujte.'
            )

        is_duplicate = (cleaned["name"], cleaned["date"]) in existing_events
        if is_duplicate:
            warnings.append(
                "Akce se stejným názvem a datem již v databázi existuje."
            )

        preview_rows.append({
            **cleaned,
            "rp_user": rp_user,
            "rp_confidence": rp_confidence,
            "warnings": warnings,
            "is_duplicate": is_duplicate,
        })

    if parse_errors:
        flash(
            f"V importních datech bylo nalezeno {len(parse_errors)} chybných řádků. "
            "Opravte chyby a vložte data znovu.",
            "danger",
        )
        return render_template(
            "import/events.html",
            json_data=raw,
            parse_errors=parse_errors,
        )

    # ── Guess default qualifications by name ──────────────────────────────
    def _qual_by_name(keyword: str) -> Qualification | None:
        keyword_lower = keyword.lower()
        return next(
            (q for q in qualifications if keyword_lower in q.name.lower()),
            None,
        )

    default_zdravotnik_qual = _qual_by_name("zdravotník") or _qual_by_name("zdravotnik")
    default_zelenac_qual = _qual_by_name("zelenáč") or _qual_by_name("zelenac")

    return render_template(
        "import/preview.html",
        preview_rows=preview_rows,
        users_preview_rows=users_preview_rows,
        users=active_users,
        master_events=master_events,
        qualifications=qualifications,
        default_zdravotnik_qual=default_zdravotnik_qual,
        default_zelenac_qual=default_zelenac_qual,
        total=len(preview_rows),
        users_new=sum(1 for u in users_preview_rows if u["existing"] is None),
        users_existing=sum(1 for u in users_preview_rows if u["existing"] is not None),
        warnings_count=sum(1 for r in preview_rows if r["warnings"]),
        duplicates_count=sum(1 for r in preview_rows if r["is_duplicate"]),
    )


@import_bp.post("/events/confirm")
@login_required
def events_confirm() -> Response:
    _require_import_permission()

    form = request.form

    def _get_int(key: str) -> int | None:
        val = form.get(key, "").strip()
        return int(val) if val.isdigit() else None

    global_zdravotnik_qual_id = _get_int("global_zdravotnik_qual_id")
    global_zelenac_qual_id = _get_int("global_zelenac_qual_id")
    global_master_event_id = _get_int("global_master_event_id")

    qual_cache: dict[int, Qualification] = {}
    me_cache: dict[int, MasterEvent] = {}

    def _qual(qid: int | None) -> Qualification | None:
        if qid is None:
            return None
        if qid not in qual_cache:
            q = db.session.get(Qualification, qid)
            if q:
                qual_cache[qid] = q
        return qual_cache.get(qid)

    def _me(mid: int | None) -> MasterEvent | None:
        if mid is None:
            return None
        if mid not in me_cache:
            m = db.session.get(MasterEvent, mid)
            if m:
                me_cache[mid] = m
        return me_cache.get(mid)

    count_str = form.get("event_count", "0")
    try:
        event_count = int(count_str)
    except ValueError:
        flash("Neplatný počet akcí.", "danger")
        return redirect(url_for("import_events.events_paste"))

    user_count_str = form.get("user_count", "0")
    user_count = int(user_count_str) if user_count_str.isdigit() else 0

    try:
        created_users, skipped_users, _, _ = _process_import_users(form, user_count)

        # Build comprehensive name→user map (all DB users incl. newly created).
        all_users_now = list(db.session.scalars(db.select(UserAccount)).all())
        name_to_user: dict[str, UserAccount] = {u.name.lower(): u for u in all_users_now}

        created = 0
        skipped = 0
        auto_debriefings = 0

        for i in range(event_count):
            prefix = f"ev_{i}_"

            include = form.get(f"{prefix}include") == "1"
            if not include:
                skipped += 1
                continue

            name = form.get(f"{prefix}name", "").strip()
            date_str = form.get(f"{prefix}date", "")
            start_time_str = form.get(f"{prefix}start_time", "").strip() or None
            end_time_str = form.get(f"{prefix}end_time", "").strip() or None
            location = form.get(f"{prefix}location", "").strip() or None
            paid = form.get(f"{prefix}paid") == "1"
            contact_person = form.get(f"{prefix}contact_person", "").strip() or None
            description = form.get(f"{prefix}description", "").strip() or None
            time_missing = form.get(f"{prefix}time_missing") == "1"
            cancelled = form.get(f"{prefix}cancelled") == "1"

            row_me_id = _get_int(f"{prefix}master_event_id") or global_master_event_id
            master_event = _me(row_me_id)
            if not master_event:
                flash(f"Řádek {i + 1} ({name}): nadřazená akce není vybrána. Import přerušen.", "danger")
                db.session.rollback()
                return redirect(url_for("import_events.events_paste"))

            rp_id_str = form.get(f"{prefix}responsible_person_id", "").strip()
            if rp_id_str.startswith("name:"):
                rp_name_lookup = rp_id_str[5:].strip()
                rp_from_name = name_to_user.get(rp_name_lookup.lower())
                responsible_person_id: str | None = str(rp_from_name.id) if rp_from_name else None
            else:
                responsible_person_id = rp_id_str or None

            start_dt = _parse_datetime(date_str, start_time_str, get_app_tz())
            if end_time_str:
                end_dt = _parse_datetime(date_str, end_time_str, get_app_tz())
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(hours=2)

            is_past = end_dt < datetime.now(timezone.utc)
            if cancelled:
                event_status = EventStatus.CANCELLED
            elif is_past:
                event_status = EventStatus.COMPLETED
            else:
                event_status = EventStatus.DRAFT

            event = Event(
                name=name,
                master_event_id=master_event.id,
                status=event_status,
                event_type=EventType.MEDICAL_COVER,
                start_datetime=start_dt,
                end_datetime=end_dt,
                actual_start_datetime=start_dt if is_past else None,
                actual_end_datetime=end_dt if is_past else None,
                address=location,
                contact_person=contact_person,
                paid=paid,
                description=description,
                responsible_person_id=responsible_person_id,
                created_by_id=current_user.id,
            )
            db.session.add(event)
            db.session.flush()

            if not time_missing and not cancelled:
                auto_debriefings += _create_spots_and_assignments(
                    event, form, prefix, responsible_person_id, name_to_user,
                    is_past, _get_int, global_zdravotnik_qual_id, global_zelenac_qual_id, _qual,
                )

            audit("import", "Event", event.id, f"Akce importována z Google Sheets: {name}", None)
            created += 1

        db.session.commit()

    except Exception as exc:
        db.session.rollback()
        flash(f"Import selhal: {exc}", "danger")
        return redirect(url_for("import_events.events_paste"))

    parts = [f"vytvořeno {created} akcí"]
    if skipped:
        parts.append(f"přeskočeno {skipped}")
    if created_users:
        parts.append(f"vytvořeno {created_users} uživatelů")
    if skipped_users:
        parts.append(f"přeskočeno {skipped_users} uživatelů (existovali)")
    if auto_debriefings:
        parts.append(f"automaticky vytvořeno {auto_debriefings} debriefingů pro historické akce")
    flash(f"Import dokončen: {', '.join(parts)}.", "success")
    return redirect(url_for("events.index"))
