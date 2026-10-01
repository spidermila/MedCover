"""Tests for the change log at the bottom of the event detail page."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from flask_login import login_user

from app.event_log import event_log
from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.digest import DigestBlock, DigestSchedule
from app.models.event import Event, EventSpot
from app.models.user import UserAccount
from app.routes.assignments import refresh_responsible_person
from tests.conftest import _get_csrf, _make_event_with_spot, _make_rp_qual, _make_user_with_qual
from tests.test_debriefing import _VALID_FORM, _assigned_client, _setup_completed_assignment

MIGRATION = next(
    Path(__file__).resolve().parent.parent.glob("migrations/versions/*_key_assignment_audit_rows_to_event.py")
)


def _entry(
    entity_type: str, entity_id: object, summary: str, changes: object = None, actor=None, timestamp=None
) -> None:
    db.session.add(
        AuditLogEntry(
            timestamp=timestamp or datetime.now(timezone.utc),
            actor_id=actor,
            action_type="edit",
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
                "zzz_unknown": [1, 2],
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
    assert edit.changes == [("Začátek", "01.06.2030 12:00", "01.06.2030 13:00"), ("Placená", "ne", "ano")]
    assert by_summary["Stav"].actor == "Systém"
    assert by_summary["Stav"].changes == [("Stav", "Koncept", "Zveřejněná")]
    assert by_summary["ZO"].changes == [("Zodpovědná osoba", "—", "Test Admin")]
    assert by_summary["Různé"].changes == [
        ("Nadřazená akce", "—", "ME for Test Event"),
        ("Typ", "Školení", "BOGUS"),
        ("Zodpovědná osoba", "—", "not-a-uuid"),
        ("Požadované kvalifikace", "Řidič ×2", "—"),
        ("zzz_unknown", "1", "2"),
    ]


def test_migration_rekeys_assignment_and_debriefing_rows(app, admin_client):
    event_id, spot_id = _make_event_with_spot(app, name="Unikátní akce")
    spec = importlib.util.spec_from_file_location("rekey_migration", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with app.app_context():
        admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
        assignment = Assignment(event_id=event_id, user_id=admin.id, assigned_by_id=admin.id)
        db.session.get(EventSpot, spot_id).assignment = assignment
        db.session.flush()
        _entry("Assignment", assignment.id, "Uživatel 'A' se přihlásil na akci 'Jiná akce'")
        _entry("Assignment", 999999, "Uživatel 'B' se přihlásil na akci 'Unikátní akce'")
        _entry("Assignment", 999998, "Uživatel 'C' se přihlásil na akci 'Neexistující'")
        _entry("Assignment", "legacy", "Bez odkazu")
        _entry("Assignment", 999995, "Uživatel 'E' se odhlásil z akce 'Unikátní akce'")
        schedule = DigestSchedule()
        db.session.add(schedule)
        db.session.flush()
        filtered = DigestBlock(
            digest_schedule_id=schedule.id,
            block_type="audit_log",
            config_json={"entity_types": ["Assignment", "Event", "UserAccount"]},
        )
        unfiltered = DigestBlock(digest_schedule_id=schedule.id, block_type="audit_log", config_json={"hours": 24})
        db.session.add_all([filtered, unfiltered])
        _entry(
            "Assignment",
            999996,
            "Uživatel 'D' se přihlásil na akci 'Unikátní akce'",
            timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        _entry("DebriefingRecord", 999997, "Debriefing odevzdán pro akci 'Unikátní akce'")
        db.session.commit()

        # The test schema already has the index from the model; only the data step is under test.
        migration.op = SimpleNamespace(get_bind=db.session.connection, create_index=lambda *a, **k: None)
        migration.upgrade()
        db.session.commit()

        db.session.expire_all()
        assert filtered.config_json == {"entity_types": ["Event", "UserAccount"]}
        assert unfiltered.config_json == {"hours": 24}
        rows = db.session.execute(db.select(AuditLogEntry.summary, AuditLogEntry.entity_type, AuditLogEntry.entity_id))
        keyed = {summary[:12]: (etype, eid) for summary, etype, eid in rows}

    assert keyed["Uživatel 'A'"] == ("Event", str(event_id))
    assert keyed["Uživatel 'B'"] == ("Event", str(event_id))
    assert keyed["Uživatel 'C'"] == ("Assignment", "999998")
    assert keyed["Debriefing o"] == ("Event", str(event_id))
    assert keyed["Bez odkazu"] == ("Assignment", "legacy")
    assert keyed["Uživatel 'E'"] == ("Event", str(event_id))
    # Older than the event carrying that name, so it belonged to a different event.
    assert keyed["Uživatel 'D'"] == ("Assignment", "999996")


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
