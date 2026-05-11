"""Regression tests: server-rendered form pages must never inject is-valid CSS classes.

The client-side validate.js adds is-valid / is-invalid programmatically on submit.
If the server ever renders those classes into the HTML, fields would appear green
before the user has even interacted with them.

These tests also verify that the Bootstrap "was-validated" class is never pre-applied
server-side (that would auto-green all filled fields via CSS :valid).
"""
from __future__ import annotations

import re

from app.extensions import db
from app.models.master_event import MasterEvent
from app.models.event import Event, EventStatus
from datetime import datetime, timezone, timedelta


# Matches any <input>, <textarea>, or <select> that already carries is-valid
_IS_VALID_IN_FIELD = re.compile(
    r'<(?:input|textarea|select)[^>]+class="[^"]*\bis-valid\b[^"]*"',
    re.IGNORECASE,
)
_WAS_VALIDATED_IN_FORM = re.compile(
    r'<form[^>]+class="[^"]*\bwas-validated\b[^"]*"',
    re.IGNORECASE,
)


def _assert_no_preinjected_validity(html: str, page: str) -> None:
    assert not _IS_VALID_IN_FIELD.search(html), (
        f"{page}: server rendered 'is-valid' class on a form field before user interaction"
    )
    assert not _WAS_VALIDATED_IN_FORM.search(html), (
        f"{page}: server rendered 'was-validated' on a form before user interaction"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(app) -> int:
    with app.app_context():
        me = MasterEvent(name="Validation Test ME")
        db.session.add(me)
        db.session.flush()
        now = datetime.now(timezone.utc)
        ev = Event(
            name="Validation Test Event",
            master_event_id=me.id,
            status=EventStatus.DRAFT,
            start_datetime=now + timedelta(days=1),
            end_datetime=now + timedelta(days=1, hours=4),
            version=1,
        )
        db.session.add(ev)
        db.session.commit()
        return ev.id


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestNoPreinjectedValidity:

    def test_event_create_form(self, admin_client):
        resp = admin_client.get("/events/create")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "event create")

    def test_event_edit_form(self, app, admin_client):
        eid = _make_event(app)
        resp = admin_client.get(f"/events/{eid}/edit")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "event edit")

    def test_login_form(self, client):
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "login")

    def test_register_form(self, app, client):
        from app.models.user import UserAccount
        from app.models.invite import RegistrationInvite
        from app.models.role import Role
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            inviter = UserAccount(email="reg_inviter@test.com", name="Inviter", is_active=True)
            inviter.set_password("testpass123")
            inviter.roles = [role]
            db.session.add(inviter)
            db.session.flush()
            inv = RegistrationInvite(
                email="newinvite@test.com",
                created_by_id=inviter.id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            db.session.add(inv)
            db.session.commit()
            token = inv.token
        resp = client.get(f"/auth/register/{token}")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "register")

    def test_forgot_password_form(self, client):
        resp = client.get("/auth/forgot-password")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "forgot password")

    def test_qualification_create_form(self, admin_client):
        resp = admin_client.get("/qualifications/create")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "qualification create")

    def test_master_event_create_form(self, admin_client):
        resp = admin_client.get("/master-events/create")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "master event create")

    def test_equipment_type_create_form(self, admin_client):
        resp = admin_client.get("/equipment/types/create")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "equipment type create")

    def test_user_detail_form(self, app, admin_client):
        from app.models.user import UserAccount
        with app.app_context():
            user = db.session.scalar(
                db.select(UserAccount).where(UserAccount.email == "admin@test.com")
            )
            uid = user.id
        resp = admin_client.get(f"/users/{uid}")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "user detail")

    def test_admin_app_settings_form(self, admin_client):
        resp = admin_client.get("/admin/settings/")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "app settings")

    def test_profile_form(self, admin_client):
        resp = admin_client.get("/users/profile")
        assert resp.status_code == 200
        _assert_no_preinjected_validity(resp.data.decode(), "profile")
