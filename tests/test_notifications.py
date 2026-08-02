"""Tests for the notification catalog and toggle route (/admin/notifications/)."""

import json
from datetime import datetime, timezone

from app.extensions import db
from app.mail import (
    _EVENT_CHANGED_CHANGE_TYPE,
    NOTIFICATION_CATALOG,
    _format_event_change_value,
    _is_notify_enabled,
    send_event_changed,
)
from app.models.audit import AuditLogEntry
from app.models.event import Event
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import AppSettings, get_settings
from app.models.user import UserAccount
from tests.conftest import _get_csrf

# ── Catalog structure ─────────────────────────────────────────────────────────


class TestNotificationCatalog:
    def test_catalog_is_nonempty(self):
        assert len(NOTIFICATION_CATALOG) > 0

    def test_all_entries_have_required_keys(self):
        required = {
            "code",
            "settings_field",
            "name_cs",
            "description_cs",
            "trigger_cs",
            "recipient_cs",
            "templates",
            "always_on",
        }
        for entry in NOTIFICATION_CATALOG:
            assert required.issubset(entry.keys()), f"Missing keys in {entry}"

    def test_always_on_entries_have_no_settings_field(self):
        for entry in NOTIFICATION_CATALOG:
            if entry["always_on"]:
                assert entry["settings_field"] is None, f"always_on entry {entry['code']} must have settings_field=None"

    def test_togglable_entries_have_settings_field(self):
        for entry in NOTIFICATION_CATALOG:
            if not entry["always_on"]:
                assert (
                    entry["settings_field"] is not None
                ), f"togglable entry {entry['code']} must have a settings_field"

    def test_known_codes_present(self):
        codes = {e["code"] for e in NOTIFICATION_CATALOG}
        expected = {
            "assignment_confirmed",
            "assignment_released",
            "event_published",
            "assignments_opened",
            "event_cancelled",
            "event_archived",
            "event_unarchived",
            "unfilled_reminder",
            "debriefing_invitation",
            "account_activated",
            "auth",
            "admin_digest",
        }
        assert expected.issubset(codes)


# ── GET ───────────────────────────────────────────────────────────────────────


class TestNotificationsGet:
    def test_admin_can_view(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        assert resp.status_code == 200
        assert "E-mailová oznámení".encode() in resp.data

    def test_non_admin_forbidden(self, client):
        resp = client.get("/admin/notifications/")
        assert resp.status_code in (302, 403)

    def test_catalog_codes_rendered(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        for entry in NOTIFICATION_CATALOG:
            assert entry["code"].encode() in resp.data

    def test_always_on_badge_rendered(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        assert "Vždy".encode() in resp.data


# ── POST (toggle) ─────────────────────────────────────────────────────────────


class TestNotificationsToggle:
    def test_disable_assignment_notifications(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/notifications/")
        # POST without notify_assignment → it should be set to False
        resp = admin_client.post(
            "/admin/notifications/",
            data={
                "csrf_token": csrf,
                "notify_event_published": "on",
                "notify_assignments_opened": "on",
                "notify_event_cancelled": "on",
                "notify_event_archived": "on",
                "notify_event_unarchived": "on",
                "notify_event_changed": "on",
                "notify_unfilled_reminder": "on",
                "notify_debriefing": "on",
                # notify_assignment intentionally omitted
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.notify_assignment is False
            assert settings.notify_event_published is True
            assert settings.notify_event_archived is True
            assert settings.notify_event_unarchived is True

    def test_enable_all_succeeds(self, app, admin_client):
        csrf = _get_csrf(admin_client, "/admin/notifications/")
        resp = admin_client.post(
            "/admin/notifications/",
            data={
                "csrf_token": csrf,
                "notify_assignment": "on",
                "notify_event_published": "on",
                "notify_assignments_opened": "on",
                "notify_event_cancelled": "on",
                "notify_event_archived": "on",
                "notify_event_unarchived": "on",
                "notify_event_changed": "on",
                "notify_unfilled_reminder": "on",
                "notify_debriefing": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            settings = db.session.get(AppSettings, 1)
            assert settings.notify_assignment is True
            assert settings.notify_debriefing is True
            assert settings.notify_event_archived is True
            assert settings.notify_event_unarchived is True

    def test_save_flashes_success(self, admin_client):
        csrf = _get_csrf(admin_client, "/admin/notifications/")
        resp = admin_client.post(
            "/admin/notifications/",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        assert "Nastavení oznámení bylo uloženo".encode() in resp.data

    def test_toggle_creates_audit_log(self, app, admin_client):

        csrf = _get_csrf(admin_client, "/admin/notifications/")
        admin_client.post(
            "/admin/notifications/",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "AppSettings")
                .order_by(AuditLogEntry.id.desc())
                .limit(1)
            ).first()
            assert entry is not None
            assert "oznámení" in entry.summary.lower()


# ── _is_notify_enabled helper ─────────────────────────────────────────────────


class TestIsNotifyEnabled:
    def test_returns_true_when_enabled(self, app):
        with app.app_context():
            settings = get_settings()
            settings.notify_assignment = True
            db.session.commit()
            assert _is_notify_enabled("notify_assignment") is True

    def test_returns_false_when_disabled(self, app):
        with app.app_context():
            settings = get_settings()
            settings.notify_assignment = False
            db.session.commit()
            assert _is_notify_enabled("notify_assignment") is False

    def test_unknown_field_defaults_true(self, app):
        with app.app_context():
            assert _is_notify_enabled("notify_nonexistent_field") is True


# ── event_changed catalog & send function ─────────────────────────────────────


class TestEventChangedNotification:
    def test_catalog_has_event_changed(self):

        codes = [e["code"] for e in NOTIFICATION_CATALOG]
        assert "event_changed" in codes

    def test_event_changed_has_settings_field(self):

        entry = next(e for e in NOTIFICATION_CATALOG if e["code"] == "event_changed")
        assert entry["settings_field"] == "notify_event_changed"
        assert not entry["always_on"]

    def test_send_event_changed_enqueues_when_enabled(self, app):
        """When notify_event_changed is on, an outbox row is created."""

        with app.app_context():
            settings = get_settings()
            settings.notify_event_changed = True
            db.session.commit()

            me = MasterEvent(name="ME for notify test")
            db.session.add(me)
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(
                email="member_notify_test@example.com",
                name="Test Member",
                is_active=True,
            )
            user.set_password("testpass")
            user.roles = [role]
            db.session.add(user)
            db.session.flush()

            event = Event(
                name="Notify Test Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 7, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 7, 1, 17, 0, tzinfo=timezone.utc),
                created_by_id=user.id,
            )
            db.session.add(event)
            db.session.commit()

            before_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            send_event_changed(user, event, {"name": ["Stará akce", "Nová akce"]})

            after_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            assert after_count == before_count + 1

    def test_send_event_changed_skipped_when_disabled(self, app):
        """When notify_event_changed is off, no outbox row is created."""

        with app.app_context():
            settings = get_settings()
            settings.notify_event_changed = False
            db.session.commit()

            me = MasterEvent(name="ME for notify test 2")
            db.session.add(me)
            db.session.flush()

            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(
                email="member_notify_disabled@example.com",
                name="Test Member 2",
                is_active=True,
            )
            user.set_password("testpass")
            user.roles = [role]
            db.session.add(user)
            db.session.flush()

            event = Event(
                name="No Notify Event",
                master_event_id=me.id,
                start_datetime=datetime(2030, 8, 1, 9, 0, tzinfo=timezone.utc),
                end_datetime=datetime(2030, 8, 1, 17, 0, tzinfo=timezone.utc),
                created_by_id=user.id,
            )
            db.session.add(event)
            db.session.commit()

            before_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            send_event_changed(user, event, {"name": ["Old", "New"]})

            after_count = db.session.scalar(
                db.select(db.func.count(OutboxEmail.id)).where(OutboxEmail.notification_type == "event_changed")
            )
            assert after_count == before_count  # nothing enqueued

    def test_format_change_value_datetime(self, app):

        with app.app_context():
            result = _format_event_change_value("start_datetime", "2026-06-01 08:00:00+00:00")
            # Should display in Prague time (UTC+2 in summer)
            assert "01.06.2026" in result
            assert "10:00" in result  # UTC+2

    def test_format_change_value_bool_paid(self, app):

        with app.app_context():
            assert _format_event_change_value("paid", "True") == "Ano"
            assert _format_event_change_value("paid", "False") == "Ne"

    def test_format_change_value_none(self, app):

        with app.app_context():
            assert _format_event_change_value("name", None) == "—"
            assert _format_event_change_value("name", "None") == "—"


# ── Test notification route ───────────────────────────────────────────────────


def _make_event_for_test(app):
    """Create a minimal published event for test notification use."""

    with app.app_context():
        me = MasterEvent(name="ME test-notif-route")
        db.session.add(me)
        db.session.flush()
        role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
        user = UserAccount(email="tnr_creator@test.cz", name="TNR Creator", is_active=True)
        user.set_password("x")
        user.roles = [role]
        db.session.add(user)
        db.session.flush()
        event = Event(
            name="Test Notif Route Event",
            master_event_id=me.id,
            start_datetime=datetime(2031, 1, 1, 9, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2031, 1, 1, 17, 0, tzinfo=timezone.utc),
            created_by_id=user.id,
        )
        db.session.add(event)
        db.session.commit()
        return event.id


class TestNotificationTestRoute:
    def test_invalid_code_redirects(self, admin_client):
        resp = admin_client.post(
            "/admin/notifications/test/unknown_code",
            data={"test_email": "a@b.com", "test_event_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/admin/notifications/" in resp.headers["Location"]

    def test_missing_email_redirects(self, admin_client):
        resp = admin_client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "", "test_event_id": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 302

    def test_non_admin_forbidden(self, client):
        resp = client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "x@y.com"},
        )
        assert resp.status_code in (302, 403)

    def test_assignment_confirmed_enqueues_to_test_email(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        resp = admin_client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "tester@example.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
            assert after == before + 1
            row = db.session.scalar(db.select(OutboxEmail).order_by(OutboxEmail.id.desc()).limit(1))
            assert row.to_email == "tester@example.com"

    def test_event_changed_enqueues_to_test_email(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        resp = admin_client.post(
            "/admin/notifications/test/event_changed",
            data={"test_email": "tester2@example.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
            assert after == before + 1

    def test_assignment_released_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        admin_client.post(
            "/admin/notifications/test/assignment_released",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_event_published_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        admin_client.post(
            "/admin/notifications/test/event_published",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_event_cancelled_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        admin_client.post(
            "/admin/notifications/test/event_cancelled",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_event_archived_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        resp = admin_client.post(
            "/admin/notifications/test/event_archived",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_event_unarchived_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        resp = admin_client.post(
            "/admin/notifications/test/event_unarchived",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_unfilled_reminder_enqueues(self, app, admin_client):

        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        admin_client.post(
            "/admin/notifications/test/unfilled_reminder",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        assert after == before + 1

    def test_debriefing_no_assignment_warns(self, app, admin_client):
        """Debriefing test without any assignment flashes a warning."""
        event_id = _make_event_for_test(app)
        resp = admin_client.post(
            "/admin/notifications/test/debriefing_invitation",
            data={"test_email": "t@test.com", "test_event_id": str(event_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "přihlášení".encode() in resp.data

    def test_no_event_in_db_warns(self, admin_client):
        """With no events in DB, test notification flashes a warning."""
        resp = admin_client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "t@test.com", "test_event_id": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"akci" in resp.data


# ── Delay tier settings + admin page block ──────────────────────────────────


class TestNotificationDelayTierDefaults:
    """AppSettings delay tier columns have the expected defaults."""

    def test_delay_tier_defaults(self, app):
        with app.app_context():
            settings = get_settings()
            assert settings.notify_delay_under_24h_min == 5
            assert settings.notify_delay_1_7_days_min == 60
            assert settings.notify_delay_1_4_weeks_min == 360
            assert settings.notify_delay_over_month_min == 1440


class TestNotificationsDelayCard:
    """Admin page renders the read-only delay card."""

    def test_card_header_present(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        assert resp.status_code == 200
        assert "Zpoždění notifikací".encode() in resp.data

    def test_all_tier_labels_present(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        assert b"Do 24 hodin do akce" in resp.data
        assert "1–7 dní do akce".encode() in resp.data
        assert "1–4 týdny do akce".encode() in resp.data
        assert "Více než měsíc do akce".encode() in resp.data

    def test_human_friendly_values_present(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        body = resp.data.decode()
        assert "5 min" in body
        assert "1 h" in body
        assert "6 h" in body
        assert "24 h" in body

    def test_card_has_editable_inputs(self, admin_client):
        """The delay card contains four number inputs pre-filled with defaults."""
        resp = admin_client.get("/admin/notifications/")
        body = resp.data.decode()
        defaults = {
            "notify_delay_under_24h_min": 5,
            "notify_delay_1_7_days_min": 60,
            "notify_delay_1_4_weeks_min": 360,
            "notify_delay_over_month_min": 1440,
        }
        for field, default in defaults.items():
            assert f'name="{field}"' in body
            assert f'value="{default}"' in body
        assert "Uložit zpoždění" in body


class TestDelayTierSave:
    """Editable delay tier POST handler. Covers."""

    _DEFAULTS: dict[str, int] = {
        "notify_delay_under_24h_min": 5,
        "notify_delay_1_7_days_min": 60,
        "notify_delay_1_4_weeks_min": 360,
        "notify_delay_over_month_min": 1440,
    }

    def _valid_form(self, csrf: str, **overrides: object) -> dict[str, str]:
        data: dict[str, str] = {
            "csrf_token": csrf,
            "notify_delay_under_24h_min": "5",
            "notify_delay_1_7_days_min": "60",
            "notify_delay_1_4_weeks_min": "360",
            "notify_delay_over_month_min": "1440",
        }
        data.update({k: str(v) for k, v in overrides.items()})
        return data

    def _get_csrf_for_form(self, client) -> str:
        return _get_csrf(client, "/admin/notifications/")

    def test_card_has_editable_inputs(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        body = resp.data.decode()
        for field, default in self._DEFAULTS.items():
            assert f'name="{field}"' in body
            assert f'value="{default}"' in body
        assert "Uložit zpoždění" in body

    def test_post_delay_tiers_success(self, app, admin_client):
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(
                csrf,
                notify_delay_under_24h_min=10,
                notify_delay_1_7_days_min=120,
                notify_delay_1_4_weeks_min=480,
                notify_delay_over_month_min=2880,
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nastavení zpoždění notifikací bylo uloženo".encode() in resp.data
        with app.app_context():
            s = get_settings()
            assert s.notify_delay_under_24h_min == 10
            assert s.notify_delay_1_7_days_min == 120
            assert s.notify_delay_1_4_weeks_min == 480
            assert s.notify_delay_over_month_min == 2880

    def test_post_delay_tiers_audit_log(self, app, admin_client):
        csrf = self._get_csrf_for_form(admin_client)
        admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min=15),
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "AppSettings")
                .order_by(AuditLogEntry.id.desc())
                .limit(1)
            ).first()
            assert entry is not None
            assert entry.action_type == "edit"
            assert entry.entity_id == "1"
            assert entry.summary == "Nastavení zpoždění notifikací bylo upraveno."
            assert "notify_delay_under_24h_min" in (entry.changes_json or {})

    def test_post_delay_tiers_out_of_range(self, app, admin_client):
        with app.app_context():
            before_val = get_settings().notify_delay_1_7_days_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_1_7_days_min=20161),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "nesmí překročit 20 160 minut".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_1_7_days_min == before_val

    def test_post_delay_tiers_non_integer(self, app, admin_client):
        with app.app_context():
            before_val = get_settings().notify_delay_1_7_days_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_1_7_days_min="abc"),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "musí být celé číslo".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_1_7_days_min == before_val

    def test_post_delay_tiers_partial_input(self, app, admin_client):
        """An empty field flashes and no persistence occurs."""
        with app.app_context():
            before = {f: getattr(get_settings(), f) for f in self._DEFAULTS}
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min=""),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "nesmí být prázdná".encode() in resp.data
        with app.app_context():
            s = get_settings()
            for field, val in before.items():
                assert getattr(s, field) == val, f"{field} was unexpectedly changed"

    def test_post_delay_tiers_permission_denied(self, app, member_client):
        with app.app_context():
            before_val = get_settings().notify_delay_under_24h_min
        csrf = _get_csrf(member_client, "/dashboard")
        resp = member_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min=99),
            follow_redirects=False,
        )
        assert resp.status_code in (302, 403)
        with app.app_context():
            assert get_settings().notify_delay_under_24h_min == before_val

    # --- no-op save still writes audit + fires flash -------------------
    def test_post_delay_tiers_noop_audit(self, app, admin_client):
        """Submitting the same four values as current DB still audits."""
        with app.app_context():
            s = get_settings()
            noop_data = {f: getattr(s, f) for f in self._DEFAULTS}
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data={"csrf_token": csrf, **{k: str(v) for k, v in noop_data.items()}},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nastavení zpoždění notifikací bylo uloženo".encode() in resp.data
        with app.app_context():
            entry = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "AppSettings")
                .where(AuditLogEntry.summary == "Nastavení zpoždění notifikací bylo upraveno.")
                .order_by(AuditLogEntry.id.desc())
                .limit(1)
            ).first()
            assert entry is not None
            assert entry.action_type == "edit"

    # --- whitespace-only rejected as empty ----------------------------
    def test_post_delay_tiers_whitespace_only(self, app, admin_client):
        """Whitespace-only value treated as empty."""
        with app.app_context():
            before_val = get_settings().notify_delay_under_24h_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min="   "),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "nesmí být prázdná".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_under_24h_min == before_val

    # --- exactly at max (20160) accepted ------------------------------
    def test_post_delay_tiers_boundary_max(self, app, admin_client):
        """Value 20160 is accepted (on the boundary)."""
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min=20160),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nastavení zpoždění notifikací bylo uloženo".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_under_24h_min == 20160

    # --- exactly at min (1) accepted ----------------------------------
    def test_post_delay_tiers_boundary_min(self, app, admin_client):
        """Value 1 is accepted (on the minimum boundary)."""
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(
                csrf,
                notify_delay_under_24h_min=1,
                notify_delay_1_7_days_min=1,
                notify_delay_1_4_weeks_min=1,
                notify_delay_over_month_min=1,
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nastavení zpoždění notifikací bylo uloženo".encode() in resp.data
        with app.app_context():
            s = get_settings()
            assert s.notify_delay_under_24h_min == 1
            assert s.notify_delay_1_7_days_min == 1
            assert s.notify_delay_1_4_weeks_min == 1
            assert s.notify_delay_over_month_min == 1

    # --- decimal string (5.5) rejected as non-integer ------------------
    def test_post_delay_tiers_decimal_rejected(self, app, admin_client):
        """'5.5' is rejected as non-parseable integer."""
        with app.app_context():
            before_val = get_settings().notify_delay_1_4_weeks_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_1_4_weeks_min="5.5"),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "musí být celé číslo".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_1_4_weeks_min == before_val

    # --- zero and negative rejected (below minimum) --------------------------
    def test_post_delay_tiers_zero_rejected(self, app, admin_client):
        """0 is below minimum (1)."""
        with app.app_context():
            before_val = get_settings().notify_delay_over_month_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_over_month_min=0),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "alespoň 1 minuta".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_over_month_min == before_val

    def test_post_delay_tiers_negative_rejected(self, app, admin_client):
        """Negative value is below minimum."""
        with app.app_context():
            before_val = get_settings().notify_delay_under_24h_min
        csrf = self._get_csrf_for_form(admin_client)
        resp = admin_client.post(
            "/admin/notifications/delay-tiers",
            data=self._valid_form(csrf, notify_delay_under_24h_min=-5),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "alespoň 1 minuta".encode() in resp.data
        with app.app_context():
            assert get_settings().notify_delay_under_24h_min == before_val


# ── "Odeslat okamžitě" checkbox ─────────────────────────────────────────────


class TestTestNotificationImmediate:
    """Send_immediately checkbox + flash message."""

    def test_checkbox_present_default_unchecked(self, admin_client):
        resp = admin_client.get("/admin/notifications/")
        body = resp.data.decode("utf-8")
        assert 'name="send_immediately"' in body
        assert "Odeslat okamžitě (přeskočit zpoždění)" in body
        # Must not be pre-checked by default
        assert 'id="test_send_immediately" checked' not in body
        assert 'name="send_immediately" value="1" checked' not in body

    def test_post_without_immediate_creates_deferred_row(self, app, admin_client):
        event_id = _make_event_for_test(app)
        resp = admin_client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "deferred@test.cz", "test_event_id": str(event_id), "send_immediately": "0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "zařazeno do fronty" in body
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail)
                .where(OutboxEmail.to_email == "deferred@test.cz")
                .order_by(OutboxEmail.id.desc())
                .limit(1)
            )
            assert row is not None
            assert row.send_after is not None

    def test_post_with_immediate_creates_null_send_after(self, app, admin_client):
        event_id = _make_event_for_test(app)
        resp = admin_client.post(
            "/admin/notifications/test/assignment_confirmed",
            data={"test_email": "immediate@test.cz", "test_event_id": str(event_id), "send_immediately": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "okamžitě" in body
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail)
                .where(OutboxEmail.to_email == "immediate@test.cz")
                .order_by(OutboxEmail.id.desc())
                .limit(1)
            )
            assert row is not None
            assert row.send_after is None


# ── test-form event_changed produces structured payload ────────


class TestTestFormEventChangedPayload:
    """Test-form POST for event_changed creates a valid row."""

    def test_deferred_row_has_valid_payload(self, app, admin_client):
        """Deferred mode creates row with field_edit change_type and non-empty JSON."""
        event_id = _make_event_for_test(app)
        with app.app_context():
            before = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
        resp = admin_client.post(
            "/admin/notifications/test/event_changed",
            data={"test_email": "ac13@test.cz", "test_event_id": str(event_id), "send_immediately": "0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            after = db.session.scalar(db.select(db.func.count(OutboxEmail.id)))
            assert after == before + 1
            row = db.session.scalar(
                db.select(OutboxEmail)
                .where(OutboxEmail.to_email == "ac13@test.cz")
                .order_by(OutboxEmail.id.desc())
                .limit(1)
            )
            assert row is not None
            assert row.change_type == _EVENT_CHANGED_CHANGE_TYPE
            payload = json.loads(row.change_value)
            assert isinstance(payload, dict) and payload
            assert "description" in payload
            assert payload["description"] == ["—", "Zkušební oznámení"]

    def test_immediate_row_has_null_send_after(self, app, admin_client):
        """Immediate mode creates row with send_after=NULL."""
        event_id = _make_event_for_test(app)
        resp = admin_client.post(
            "/admin/notifications/test/event_changed",
            data={"test_email": "ac14@test.cz", "test_event_id": str(event_id), "send_immediately": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            row = db.session.scalar(
                db.select(OutboxEmail)
                .where(OutboxEmail.to_email == "ac14@test.cz")
                .order_by(OutboxEmail.id.desc())
                .limit(1)
            )
            assert row is not None
            assert row.send_after is None
            assert row.change_type == _EVENT_CHANGED_CHANGE_TYPE
            payload = json.loads(row.change_value)
            assert isinstance(payload, dict) and payload
