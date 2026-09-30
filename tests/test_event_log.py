"""Tests for the change log at the bottom of the event detail page."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from flask_login import login_user

from app.event_log import FIELD_LABELS, event_log, event_snapshot, labelled_changes
from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot
from app.models.user import UserAccount
from app.routes.assignments import refresh_responsible_person
from tests.conftest import _get_csrf, _make_event_with_spot, _make_rp_qual, _make_user_with_qual
from tests.test_debriefing import _VALID_FORM, _assigned_client, _setup_completed_assignment

MIGRATION = next(Path(__file__).resolve().parent.parent.glob("migrations/versions/*_link_audit_rows_to_their_event.py"))


def _entry(
    entity_type: str,
    entity_id: object,
    summary: str,
    changes: object = None,
    actor=None,
    timestamp=None,
    action: str = "edit",
) -> None:
    db.session.add(
        AuditLogEntry(
            timestamp=timestamp or datetime.now(timezone.utc),
            actor_id=actor,
            action_type=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            summary=summary,
            changes_json=changes,
        )
    )


def test_claim_appears_in_detail_log(app, member_client):
    event_id, spot_id = _make_event_with_spot(app)
    with app.app_context():
        db.session.get(EventSpot, spot_id).description = "Řidič"
        db.session.commit()

    html = member_client.get(f"/events/{event_id}").get_data(as_text=True)
    assert "Historie změn" in html
    assert "Žádné záznamy." in html

    member_client.post(f"/assignments/claim/{spot_id}")
    with app.app_context():
        assignment_id = db.session.get(EventSpot, spot_id).assignment.id
    member_client.post(f"/assignments/release/{assignment_id}")
    html = member_client.get(f"/events/{event_id}").get_data(as_text=True)

    assert "Test Member</strong>: Uživatel „Test Member“ se přihlásil na akci" in html
    assert "se odhlásil z akce „Test Event“ (pozice „Řidič“)" in html
    assert "Žádné záznamy." not in html


def test_changes_are_labelled_and_formatted(app, admin_client):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        _entry(
            "Event",
            event_id,
            "Upravena akce",
            {"start_datetime": ["2030-06-01 10:00:00+00:00", "2030-06-01 11:00:00+00:00"], "paid": [False, True]},
            actor=admin.id,
        )
        _entry("Event", event_id, "Stav", {"before": {"status": "Koncept"}, "after": {"status": "Zveřejněná"}})
        _entry("Event", event_id, "ZO", {"responsible_person_id": {"before": "None", "after": str(admin.id)}})
        _entry("Event", event_id + 1, "Cizí akce")
        me_id = db.session.get(Event, event_id).master_event_id
        _entry(
            "Event",
            event_id,
            "Různé",
            {
                "master_event_id": [None, me_id],
                "event_type": ["TRAINING", "BOGUS"],
                "qualification_requirements": [[["Řidič", 2]], []],
                "end_datetime": ["nesmysl", "nesmysl"],
                "responsible_person_id": ["None", "not-a-uuid"],
                "error": "ignored shape",
            },
        )
        db.session.commit()

        lines = event_log(db.session.get(Event, event_id))

    by_summary = {line.summary: line for line in lines}
    assert "Cizí akce" not in by_summary
    edit = by_summary["Upravena akce"]
    assert edit.actor == "Test Admin"
    # 10:00 / 11:00 UTC is 12:00 / 13:00 Prague summer time
    assert edit.changes == [("Začátek", "01.06.2030 12:00", "01.06.2030 13:00"), ("Placená akce", "ne", "ano")]
    assert by_summary["Stav"].actor == "Systém"
    assert by_summary["Stav"].changes == [("Stav", "Koncept", "Zveřejněná")]
    assert by_summary["ZO"].changes == [("Zodpovědná osoba", "—", "Test Admin")]
    assert by_summary["Různé"].changes == [
        ("Nadřazená akce", "—", "ME for Test Event"),
        ("Typ akce", "Školení", "BOGUS"),
        ("Zodpovědná osoba", "—", "not-a-uuid"),
        ("Požadované kvalifikace", "Řidič ×2", "—"),
    ]
    # An unlabelled field still shows, under its raw name, after the labelled ones.
    assert labelled_changes({"zzz_unknown": (1, 2), "name": ("A", "B")}, {}) == [
        ("Název", "A", "B"),
        ("zzz_unknown", "1", "2"),
    ]


def _run_migration() -> None:
    spec = importlib.util.spec_from_file_location("link_migration", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # The test schema already has the column and index from the model; only the data step is under test.
    migration.op = SimpleNamespace(
        get_bind=db.session.connection, add_column=lambda *a, **k: None, create_index=lambda *a, **k: None
    )
    migration.upgrade()
    db.session.commit()


def test_migration_links_assignment_and_debriefing_rows(app, admin_client):
    event_id, spot_id = _make_event_with_spot(app, name="Unikátní akce")
    with app.app_context():
        admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        assignment = Assignment(event_id=event_id, user_id=admin.id, assigned_by_id=admin.id)
        db.session.get(EventSpot, spot_id).assignment = assignment
        db.session.flush()
        assignment_id = assignment.id
        _entry("Assignment", assignment.id, "Uživatel 'A' se přihlásil na akci 'Jiná akce'")
        # Older than the assignment now holding that id (reused after a backup restore).
        _entry(
            "Assignment",
            assignment.id,
            "Uživatel 'F' se přihlásil na akci 'Jiná akce'",
            timestamp=datetime(2001, 1, 1, tzinfo=timezone.utc),
        )
        _entry("Assignment", 999999, "Uživatel 'B' se přihlásil na akci 'Unikátní akce'")
        _entry("Assignment", 999998, "Uživatel 'C' se přihlásil na akci 'Neexistující'")
        _entry("Assignment", "legacy", "Bez odkazu")
        _entry("Assignment", 999995, "Uživatel 'E' se odhlásil z akce 'Unikátní akce'")
        _entry(
            "Assignment",
            999996,
            "Uživatel 'D' se přihlásil na akci 'Unikátní akce'",
            timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        _entry("DebriefingRecord", 999997, "Debriefing odevzdán pro akci 'Unikátní akce'")
        db.session.commit()

        _run_migration()

        rows = db.session.execute(
            db.select(AuditLogEntry.summary, AuditLogEntry.entity_type, AuditLogEntry.entity_id, AuditLogEntry.event_id)
        )
        linked = {summary[:12]: (etype, eid, event) for summary, etype, eid, event in rows}

    assert linked["Uživatel 'A'"] == ("Assignment", str(assignment_id), event_id)
    assert linked["Uživatel 'B'"] == ("Assignment", "999999", event_id)
    assert linked["Uživatel 'C'"] == ("Assignment", "999998", None)
    assert linked["Uživatel 'F'"] == ("Assignment", str(assignment_id), None)
    assert linked["Debriefing o"] == ("DebriefingRecord", "999997", event_id)
    assert linked["Bez odkazu"] == ("Assignment", "legacy", None)
    assert linked["Uživatel 'E'"] == ("Assignment", "999995", event_id)
    # Older than the event carrying that name, so it belonged to a different event.
    assert linked["Uživatel 'D'"] == ("Assignment", "999996", None)


def test_migration_pairs_deleted_sign_ups_with_their_sign_off(app, admin_client):
    first_id, _ = _make_event_with_spot(app, name="Hokej")
    with app.app_context():
        me_id = db.session.get(Event, first_id).master_event_id
    second_id, _ = _make_event_with_spot(app, name="Hokej", me_id=me_id)
    later_id, _ = _make_event_with_spot(app, name="Hokej", me_id=me_id)
    with app.app_context():
        admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        start = datetime.now(timezone.utc) + timedelta(minutes=1)
        db.session.get(Event, later_id).created_at = start + timedelta(hours=1)
        steps = [
            # Sign-off from the second of two same-named events.
            ("Assignment", 990001, "create", "Uživatel 'Pavel' se přihlásil na akci 'Hokej'"),
            ("Event", second_id, "delete", "Uživatel 'Pavel' se odhlásil z akce 'Hokej'"),
            # Signed up, left and signed up again on the same event.
            ("Assignment", 990002, "create", "Uživatel 'Jana' se přihlásil na akci 'Hokej'"),
            ("Event", first_id, "delete", "Uživatel 'Jana' se odhlásil z akce 'Hokej'"),
            ("Assignment", 990003, "create", "'Admin' přiřadil 'Jana' na akci 'Hokej'"),
            ("Event", first_id, "delete", "'Admin' odhlásil 'Jana' z akce 'Hokej'"),
            # Two open sign-ups when the first sign-off comes: no way to tell which it closes.
            ("Assignment", 990004, "create", "Uživatel 'Eva' se přihlásil na akci 'Hokej'"),
            ("Assignment", 990005, "create", "Uživatel 'Eva' se přihlásil na akci 'Hokej'"),
            ("Event", first_id, "delete", "Uživatel 'Eva' se odhlásil z akce 'Hokej'"),
            ("Event", second_id, "delete", "Uživatel 'Eva' se odhlásil z akce 'Hokej'"),
            # Sign-off from a condition event sign-up, logged on the event all along.
            ("Assignment", 990007, "create", "Uživatel 'Olga' se přihlásil na akci 'Hokej'"),
            ("Event", second_id, "create", "'Admin' přihlásil/a 'Olga' na akci 'Hokej'"),
            ("Event", second_id, "delete", "Uživatel 'Olga' se odhlásil z akce 'Hokej'"),
            ("Event", first_id, "delete", "Uživatel 'Olga' se odhlásil z akce 'Hokej'"),
            # Removed automatically by a spot edit, logged right after the spot edit.
            ("Assignment", 990008, "create", "Uživatel 'Petr' se přihlásil na akci 'Hokej'"),
            ("Event", second_id, "edit", "Upravena pozice 'Zdravotník' (kvalifikace: RP)"),
            ("Assignment", 990008, "delete", "Uživatel 'Petr' automaticky odhlášen — nesplňuje nové požadavky pozice"),
            # Signed off back when sign-offs were logged on the assignment: that closes the
            # sign-up, so the later sign-off from an imported assignment must not take it.
            ("Assignment", 990009, "create", "Uživatel 'Hana' se přihlásil na akci 'Hokej'"),
            ("Assignment", 990009, "delete", "Uživatel 'Hana' se odhlásil z akce 'Hokej'"),
            ("Event", second_id, "delete", "Uživatel 'Hana' se odhlásil z akce 'Hokej'"),
            # An ordinary sign-off that happens to follow a spot edit by the same person.
            ("Assignment", 990010, "create", "Uživatel 'Lída' se přihlásil na akci 'Hokej'"),
            ("Event", second_id, "edit", "Upravena pozice 'Řidič' (kvalifikace: žádná)"),
            ("Assignment", 990010, "delete", "'Admin' odhlásil 'Lída' z akce 'Hokej'"),
            # An id reused after a backup restore: each assignment keeps its own event.
            ("Assignment", 990011, "create", "Uživatel 'Rita' se přihlásil na akci 'Hokej'"),
            ("Assignment", 990011, "delete", "Uživatel 'Rita' se odhlásil z akce 'Hokej'"),
            ("Assignment", 990011, "create", "Uživatel 'Tom' se přihlásil na akci 'Hokej'"),
            ("Event", second_id, "delete", "Uživatel 'Tom' se odhlásil z akce 'Hokej'"),
            # The sign-off is from an event created after the sign-up (copied with its assignments).
            ("Assignment", 990006, "create", "Uživatel 'Karel' se přihlásil na akci 'Hokej'"),
        ]
        for n, (entity_type, entity_id, action, summary) in enumerate(steps):
            _entry(
                entity_type, entity_id, summary, actor=admin.id, timestamp=start + timedelta(seconds=n), action=action
            )
            db.session.flush()
        _entry(
            "Event",
            later_id,
            "Uživatel 'Karel' se odhlásil z akce 'Hokej'",
            actor=admin.id,
            timestamp=start + timedelta(hours=2),
            action="delete",
        )
        db.session.commit()

        _run_migration()

        rows = db.session.execute(
            db.select(AuditLogEntry.summary, AuditLogEntry.event_id).order_by(AuditLogEntry.id)
        ).all()

    def sign_ups(name: str) -> list[int | None]:
        """Linked events of the user's sign-up and removal rows; sign-offs are left out."""
        return [event for summary, event in rows if f"'{name}'" in summary and " z akce " not in summary]

    assert sign_ups("Pavel") == [second_id]
    assert sign_ups("Jana") == [first_id] * 2
    assert sign_ups("Eva") == [None, None]
    assert sign_ups("Olga") == [first_id, second_id]
    assert sign_ups("Petr") == [second_id] * 2
    assert sign_ups("Karel") == [None]
    assert sign_ups("Hana") == [None]
    assert sign_ups("Lída") == [None]
    assert sign_ups("Rita") == [None]
    assert sign_ups("Tom") == [second_id]


def test_event_log_timestamps_are_ordered_newest_first(app, admin_client):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        for i, ts in enumerate([datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 2, tzinfo=timezone.utc)]):
            db.session.add(
                AuditLogEntry(
                    timestamp=ts, action_type="edit", entity_type="Event", entity_id=str(event_id), summary=f"z{i}"
                )
            )
        db.session.commit()
        assert [line.summary for line in event_log(db.session.get(Event, event_id))] == ["z1", "z0"]


def test_debriefing_feedback_is_not_shown(app, admin_client):
    event_id, _, assignment_id = _setup_completed_assignment(app)
    _assigned_client(app).post(f"/debriefing/{assignment_id}", data=_VALID_FORM)

    html = admin_client.get(f"/events/{event_id}").get_data(as_text=True)

    assert "Assigned Member</strong>: Debriefing odevzdán" in html
    for key in ("feedback_event", "feedback_customer", "feedback_colleagues"):
        assert _VALID_FORM[key] not in html


def test_auto_unassign_on_spot_edit_is_logged(app, admin_client):
    event_id, spot_id = _make_event_with_spot(app)
    with app.app_context():
        admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        db.session.get(EventSpot, spot_id).assignment = Assignment(
            event_id=event_id, user_id=admin.id, assigned_by_id=admin.id
        )
        db.session.commit()
    qual_id = _make_rp_qual(app)

    admin_client.post(
        f"/events/{event_id}/spots/{spot_id}/edit",
        data={
            "csrf_token": _get_csrf(admin_client, f"/events/{event_id}"),
            "qualification_ids": str(qual_id),
            "confirm_unassign": "1",
        },
    )

    html = admin_client.get(f"/events/{event_id}").get_data(as_text=True)
    assert "Uživatel „Test Admin“ automaticky odhlášen" in html
    with app.app_context():
        entry = db.session.scalar(
            db.select(AuditLogEntry).where(AuditLogEntry.summary.contains("automaticky odhlášen"))
        )
        assert (entry.entity_type, entry.event_id) == ("Assignment", event_id)


def test_responsible_person_recalculation_shows_names(app, admin_client):
    event_id, spot_id = _make_event_with_spot(app)
    user_id = _make_user_with_qual(app, "rp@test.com", _make_rp_qual(app))
    with app.test_request_context():
        user = db.session.get(UserAccount, UUID(user_id))
        login_user(user)
        db.session.get(EventSpot, spot_id).assignment = Assignment(
            event_id=event_id, user_id=user.id, assigned_by_id=user.id
        )
        event = db.session.get(Event, event_id)
        db.session.flush()
        refresh_responsible_person(event)
        db.session.commit()

        (line,) = event_log(event)
        assert line.changes == [("Zodpovědná osoba", "—", user.name)]


def test_every_edited_event_field_has_a_label(app):
    event_id, _ = _make_event_with_spot(app)
    with app.app_context():
        assert set(event_snapshot(db.session.get(Event, event_id))) <= set(FIELD_LABELS)
