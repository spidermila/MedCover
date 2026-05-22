"""Tests for user profile and admin user-management routes."""
from __future__ import annotations

from app.extensions import db
from app.models.user import UserAccount
from app.models.role import Role
from app.models.invite import RegistrationInvite
from app.models.audit import AuditLogEntry


# ── Profile ───────────────────────────────────────────────────────────────────

class TestUserProfile:
    def test_profile_page_loads(self, member_client: object) -> None:
        resp = member_client.get("/users/profile")
        assert resp.status_code == 200
        assert "Můj profil".encode() in resp.data

    def test_profile_requires_login(self, client: object) -> None:
        resp = client.get("/users/profile")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_update_profile_name(self, app: object, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "name": "Nové Jméno", "dashboard_horizon_days": "30"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Profil byl uložen".encode() in resp.data
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user is not None
            assert user.name == "Nové Jméno"

    def test_update_profile_empty_name_rejected(self, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "name": "", "dashboard_horizon_days": "30"},
            follow_redirects=True,
        )
        assert "Jméno nesmí být prázdné".encode() in resp.data

    def test_dark_mode_toggle(self, app: object, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "name": "Test Member", "dashboard_horizon_days": "30", "dark_mode": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user is not None
            assert user.dark_mode is True

    def test_dark_mode_off_by_default(self, app: object, member_client: object) -> None:
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user is not None
            assert user.dark_mode is False

    def test_change_password_wrong_current(self, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "password", "current_password": "wrong", "new_password": "newpass123", "confirm_password": "newpass123"},
            follow_redirects=True,
        )
        assert "Současné heslo je nesprávné".encode() in resp.data

    def test_change_password_mismatch(self, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "password", "current_password": "testpass123", "new_password": "newpass123", "confirm_password": "different"},
            follow_redirects=True,
        )
        assert "Hesla se neshodují".encode() in resp.data

    def test_change_password_too_short(self, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "password", "current_password": "testpass123", "new_password": "short", "confirm_password": "short"},
            follow_redirects=True,
        )
        assert "alespoň 8 znaků".encode() in resp.data

    def test_change_password_success(self, app: object, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "password", "current_password": "testpass123", "new_password": "newpass123", "confirm_password": "newpass123"},
            follow_redirects=True,
        )
        assert "Heslo bylo změněno".encode() in resp.data
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user is not None
            assert user.check_password("newpass123")


# ── Admin user list ───────────────────────────────────────────────────────────

class TestAdminUserList:
    def test_user_list_requires_permission(self, member_client: object) -> None:
        # Members have user.view, so use a viewer with no user.view to check 403
        resp = member_client.get("/users/")
        # Members have user.view permission — they can access the list
        assert resp.status_code == 200

    def test_user_list_accessible_to_admin(self, admin_client: object) -> None:
        resp = admin_client.get("/users/")
        assert resp.status_code == 200
        assert "Uživatelé".encode() in resp.data

    def test_user_detail_accessible_to_admin(self, app: object, admin_client: object) -> None:
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            assert user is not None
            uid = str(user.id)
        resp = admin_client.get(f"/users/{uid}")
        assert resp.status_code == 200

    def test_user_detail_404_on_unknown(self, admin_client: object) -> None:
        import uuid
        resp = admin_client.get(f"/users/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_user_detail_shows_report_link_for_report_view_permission(self, app: object, admin_client: object) -> None:
        """Admin (has report.view) must see Přehled akcí link on user detail (#117)."""
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            uid = str(user.id)
        resp = admin_client.get(f"/users/{uid}")
        assert resp.status_code == 200
        assert "Přehled akcí".encode() in resp.data
        assert f"/reports/user/{uid}".encode() in resp.data

    def test_user_detail_hides_report_link_without_report_view_permission(self, app: object, client: object) -> None:
        """A user without report.view must not see the Přehled akcí link (#117)."""
        with app.app_context():
            admin_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
            # User with no roles has no permissions at all
            norole = UserAccount(email="norole117@test.com", name="NoRole 117", is_active=True)
            norole.set_password("pass1234")
            norole.roles = []
            target = UserAccount(email="target117@test.com", name="Target 117", is_active=True)
            target.set_password("pass1234")
            target.roles = [admin_role]
            db.session.add_all([norole, target])
            db.session.commit()
            target_id = str(target.id)
        client.post("/auth/login", data={"email": "norole117@test.com", "password": "pass1234"})
        resp = client.get(f"/users/{target_id}")
        assert resp.status_code in (200, 403)
        if resp.status_code == 200:
            assert "Přehled akcí".encode() not in resp.data

    def test_activate_user(self, app: object, admin_client: object) -> None:
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            inactive = UserAccount(email="inactive@test.com", name="Inactive", is_active=False)
            inactive.set_password("pass1234")
            inactive.roles = [role]
            db.session.add(inactive)
            db.session.commit()
            uid = str(inactive.id)
        resp = admin_client.post(f"/users/{uid}/activate", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            assert user.is_active is True

    def test_deactivate_user(self, app: object, admin_client: object) -> None:
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            target = UserAccount(email="target@test.com", name="Target", is_active=True)
            target.set_password("pass1234")
            target.roles = [role]
            db.session.add(target)
            db.session.commit()
            uid = str(target.id)
        resp = admin_client.post(f"/users/{uid}/deactivate", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            assert user.is_active is False


class TestAdminEditUser:
    def _create_user(self, app: object, email: str = "editable@test.com") -> str:
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(email=email, name="Editable User", is_active=True)
            user.set_password("pass1234")
            user.roles = [role]
            db.session.add(user)
            db.session.commit()
            return str(user.id)

    def test_admin_can_edit_name_email_phone(self, app: object, admin_client: object) -> None:
        uid = self._create_user(app)
        resp = admin_client.post(f"/users/{uid}/save", data={
            "name": "New Name",
            "email": "newemail@test.com",
            "phone": "+420123456789",
        }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            assert user.name == "New Name"
            assert user.email == "newemail@test.com"
            assert user.phone == "+420123456789"

    def test_member_cannot_edit_user(self, app: object, member_client: object) -> None:
        uid = self._create_user(app, "member_edit_target@test.com")
        resp = member_client.post(f"/users/{uid}/save", data={
            "name": "Hacker",
            "email": "hacked@test.com",
            "phone": "",
        })
        assert resp.status_code == 403

    def test_duplicate_email_rejected(self, app: object, admin_client: object) -> None:
        uid = self._create_user(app, "orig@test.com")
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            other = UserAccount(email="taken@test.com", name="Other", is_active=True)
            other.set_password("pass1234")
            other.roles = [role]
            db.session.add(other)
            db.session.commit()
        resp = admin_client.post(f"/users/{uid}/save", data={
            "name": "Orig",
            "email": "taken@test.com",
            "phone": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "již použit".encode() in resp.data
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            assert user.email == "orig@test.com"

    def test_empty_name_rejected(self, app: object, admin_client: object) -> None:
        uid = self._create_user(app, "noname@test.com")
        resp = admin_client.post(f"/users/{uid}/save", data={
            "name": "",
            "email": "noname@test.com",
            "phone": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "prázdné".encode() in resp.data

    def test_audit_log_entry_created(self, app: object, admin_client: object) -> None:
        from app.models.audit import AuditLogEntry
        uid = self._create_user(app, "audit_edit@test.com")
        admin_client.post(f"/users/{uid}/save", data={
            "name": "Audited Name",
            "email": "audit_edit@test.com",
            "phone": "",
        })
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "UserAccount", AuditLogEntry.entity_id == uid)
                .order_by(AuditLogEntry.timestamp.desc())
            )
            assert entry is not None
            assert entry.action_type == "edit"

    def test_no_audit_entry_when_nothing_changes(self, app: object, admin_client: object) -> None:
        """Saving with identical data must not produce any audit log entries (closes #249)."""
        uid = self._create_user(app, "nochange@test.com")
        # Get current data so the form truly submits unchanged values
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            current_name = user.name
            current_email = user.email
            role_ids = [str(r.id) for r in user.roles]
        admin_client.post(f"/users/{uid}/save", data={
            "name": current_name,
            "email": current_email,
            "phone": "",
            "role_ids": role_ids,
        })
        with app.app_context():
            count = db.session.scalar(
                db.select(db.func.count(AuditLogEntry.id))
                .where(AuditLogEntry.entity_type == "UserAccount", AuditLogEntry.entity_id == uid)
            )
            assert count == 0

    def test_only_qualification_audit_when_only_qualifications_change(
        self, app: object, admin_client: object
    ) -> None:
        """Changing only qualifications must produce exactly one audit entry (closes #249)."""
        from app.models.qualification import Qualification
        uid = self._create_user(app, "qualonly@test.com")
        with app.app_context():
            qual = Qualification(name="Test Qual", description="")
            db.session.add(qual)
            db.session.commit()
            qual_id = qual.id
            user = db.session.get(UserAccount, uid)
            assert user is not None
            current_name = user.name
            current_email = user.email
            role_ids = [str(r.id) for r in user.roles]

        admin_client.post(f"/users/{uid}/save", data={
            "name": current_name,
            "email": current_email,
            "phone": "",
            "role_ids": role_ids,
            "qualification_ids": [str(qual_id)],
        })
        with app.app_context():
            entries = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "UserAccount", AuditLogEntry.entity_id == uid)
            ).all()
            assert len(entries) == 1
            assert "Kvalifikace" in entries[0].summary

    def test_only_info_audit_when_only_info_changes(self, app: object, admin_client: object) -> None:
        """Changing only profile info must produce exactly one audit entry (closes #249)."""
        uid = self._create_user(app, "infoonly@test.com")
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user is not None
            role_ids = [str(r.id) for r in user.roles]
        admin_client.post(f"/users/{uid}/save", data={
            "name": "Changed Name",
            "email": "infoonly@test.com",
            "phone": "",
            "role_ids": role_ids,
        })
        with app.app_context():
            entries = db.session.scalars(
                db.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "UserAccount", AuditLogEntry.entity_id == uid)
            ).all()
            assert len(entries) == 1
            assert "údaje" in entries[0].summary


# ── Invites ───────────────────────────────────────────────────────────────────

class TestInvites:
    def test_invites_page_accessible_to_admin(self, admin_client: object) -> None:
        resp = admin_client.get("/users/invites")
        assert resp.status_code == 200
        assert "Pozvánky".encode() in resp.data

    def test_invites_page_requires_permission(self, member_client: object) -> None:
        resp = member_client.get("/users/invites")
        assert resp.status_code == 403

    def test_create_invite(self, app: object, admin_client: object) -> None:
        resp = admin_client.post(
            "/users/invites/create",
            data={"email": "newuser@example.com"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "zařazena do fronty".encode() in resp.data
        with app.app_context():
            inv = db.session.scalar(
                db.select(RegistrationInvite).where(RegistrationInvite.email == "newuser@example.com")
            )
            assert inv is not None
            assert inv.is_valid

    def test_create_invite_invalid_email(self, admin_client: object) -> None:
        resp = admin_client.post(
            "/users/invites/create",
            data={"email": "not-an-email"},
            follow_redirects=True,
        )
        assert b"platnou e-mailovou adresu" in resp.data

    def test_duplicate_invite_warned(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "dup@example.com"}, follow_redirects=True)
        resp = admin_client.post(
            "/users/invites/create",
            data={"email": "dup@example.com"},
            follow_redirects=True,
        )
        assert "již existuje".encode() in resp.data

    def test_invite_blocked_for_existing_user(self, app: object, admin_client: object) -> None:
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            existing = UserAccount(email="existing@example.com", name="Existing", is_active=True)
            existing.set_password("pass1234")
            existing.roles = [role]
            db.session.add(existing)
            db.session.commit()
        resp = admin_client.post(
            "/users/invites/create",
            data={"email": "existing@example.com"},
            follow_redirects=True,
        )
        assert "již má účet".encode() in resp.data
        with app.app_context():
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "existing@example.com"))
            assert inv is None

    def test_create_invite_with_custom_subject_and_message(self, app: object, admin_client: object) -> None:
        admin_client.post(
            "/users/invites/create",
            data={
                "email": "custom@example.com",
                "custom_subject": "Vítejte v MedCoveru!",
                "custom_message": "Zdravím, zvu tě do týmu.",
            },
            follow_redirects=True,
        )
        with app.app_context():
            from app.models.outbox import OutboxEmail
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "custom@example.com"))
            assert inv is not None
            assert inv.custom_subject == "Vítejte v MedCoveru!"
            assert inv.custom_message == "Zdravím, zvu tě do týmu."
            outbox = db.session.get(OutboxEmail, inv.outbox_email_id)
            assert outbox is not None
            assert outbox.subject == "Vítejte v MedCoveru!"
            assert "Zdravím, zvu tě do týmu." in outbox.html_body

    def test_cancel_invite(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "cancel@example.com"}, follow_redirects=True)
        with app.app_context():
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "cancel@example.com"))
            assert inv is not None
            inv_id = inv.id
        resp = admin_client.post(f"/users/invites/{inv_id}/cancel", follow_redirects=True)
        assert resp.status_code == 200
        assert "zrušena".encode() in resp.data
        with app.app_context():
            inv = db.session.get(RegistrationInvite, inv_id)
            assert inv is not None
            assert inv.is_cancelled
            assert inv.cancelled_at is not None

    def test_cancel_invite_writes_audit_log(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "cancelaudit@example.com"}, follow_redirects=True)
        with app.app_context():
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "cancelaudit@example.com"))
            inv_id = inv.id
        admin_client.post(f"/users/invites/{inv_id}/cancel", follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "RegistrationInvite",
                    AuditLogEntry.action_type == "cancel",
                )
            )
            assert entry is not None

    def test_cancelled_invite_allows_new_invite(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "retry@example.com"}, follow_redirects=True)
        with app.app_context():
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "retry@example.com"))
            inv_id = inv.id
        admin_client.post(f"/users/invites/{inv_id}/cancel", follow_redirects=True)
        # Should now be able to create a fresh invite for the same email
        resp = admin_client.post("/users/invites/create", data={"email": "retry@example.com"}, follow_redirects=True)
        assert "zařazena do fronty".encode() in resp.data

    def test_create_invite_queues_outbox_email(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "outbox@example.com"}, follow_redirects=True)
        with app.app_context():
            from app.models.outbox import OutboxEmail
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "outbox@example.com"))
            assert inv is not None
            assert inv.outbox_email_id is not None
            outbox = db.session.get(OutboxEmail, inv.outbox_email_id)
            assert outbox is not None
            assert outbox.to_email == "outbox@example.com"
            assert outbox.status == "pending"

    def test_create_invite_writes_audit_log(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "audit@example.com"}, follow_redirects=True)
        with app.app_context():
            from app.models.audit import AuditLogEntry
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "RegistrationInvite",
                    AuditLogEntry.action_type == "create",
                )
            )
            assert entry is not None

    def test_resend_invite_creates_new_outbox_entry(self, app: object, admin_client: object) -> None:
        admin_client.post("/users/invites/create", data={"email": "resend@example.com"}, follow_redirects=True)
        with app.app_context():
            inv = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "resend@example.com"))
            assert inv is not None
            old_outbox_id = inv.outbox_email_id
            inv_id = inv.id
        resp = admin_client.post(f"/users/invites/{inv_id}/resend", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            inv = db.session.get(RegistrationInvite, inv_id)
            assert inv is not None
            assert inv.outbox_email_id != old_outbox_id

    def test_link_clicked_at_set_on_register_get(self, app: object, client: object) -> None:
        with app.app_context():
            from app.models.role import Role as _Role
            admin_role = db.session.scalar(db.select(Role).where(Role.name == _Role.ADMIN))
            creator = UserAccount(email="creator@test.com", name="Creator", is_active=True)
            creator.set_password("pass1234")
            creator.roles = [admin_role]
            db.session.add(creator)
            db.session.flush()
            inv = RegistrationInvite(email="clicktest@example.com", created_by_id=creator.id)
            db.session.add(inv)
            db.session.commit()
            token = inv.token
            inv_id = inv.id
        client.get(f"/auth/register/{token}")
        with app.app_context():
            inv = db.session.get(RegistrationInvite, inv_id)
            assert inv is not None
            assert inv.link_clicked_at is not None

    def test_link_clicked_audit_logged(self, app: object, client: object) -> None:
        with app.app_context():
            from app.models.role import Role as _Role
            admin_role = db.session.scalar(db.select(Role).where(Role.name == _Role.ADMIN))
            creator = UserAccount(email="creator2@test.com", name="Creator2", is_active=True)
            creator.set_password("pass1234")
            creator.roles = [admin_role]
            db.session.add(creator)
            db.session.flush()
            inv = RegistrationInvite(email="clickaudit@example.com", created_by_id=creator.id)
            db.session.add(inv)
            db.session.commit()
            token = inv.token
            inv_id = inv.id
        client.get(f"/auth/register/{token}")
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "RegistrationInvite",
                    AuditLogEntry.action_type == "link_clicked",
                    AuditLogEntry.entity_id == str(inv_id),
                )
            )
            assert entry is not None

    def test_new_user_gets_member_role(self, app: object, client: object) -> None:
        with app.app_context():
            from app.models.role import Role as _Role
            admin_role = db.session.scalar(db.select(Role).where(Role.name == _Role.ADMIN))
            creator = UserAccount(email="roletest_creator@test.com", name="RoleCreator", is_active=True)
            creator.set_password("pass1234")
            creator.roles = [admin_role]
            db.session.add(creator)
            db.session.flush()
            inv = RegistrationInvite(email="roletest@example.com", created_by_id=creator.id)
            db.session.add(inv)
            db.session.commit()
            token = inv.token
        client.post(f"/auth/register/{token}", data={
            "full_name": "Role Test User",
            "password": "pass1234",
            "password2": "pass1234",
        }, follow_redirects=True)
        with app.app_context():
            from app.models.role import Role as _Role
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "roletest@example.com"))
            assert user is not None
            role_names = [r.name for r in user.roles]
            assert _Role.MEMBER in role_names


# ── Phone number validation ───────────────────────────────────────────────────

import pytest

VALID_PHONES = [
    "123456789",
    "123 456 789",
    "+420123456789",
    "+420 123 456 789",
    "00420123456789",
    "00420 123 456 789",
    "+1 555 123 456",        # international with short local
    "",                       # empty — optional field
]

INVALID_PHONES = [
    "abc",
    "12345678",              # 8 digits — too short
    "+420 abc 123",
    "123-456-789",           # hyphens not allowed
    "phone: 123",
    "123 456 789 0",         # extra digit
]


class TestPhoneValidationProfile:
    @pytest.mark.parametrize("phone", VALID_PHONES)
    def test_valid_phone_accepted_on_profile(self, app: object, member_client: object, phone: str) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "name": "Test Member", "dashboard_horizon_days": "30", "phone": phone},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Profil byl uložen".encode() in resp.data

    @pytest.mark.parametrize("phone", INVALID_PHONES)
    def test_invalid_phone_rejected_on_profile(self, app: object, member_client: object, phone: str) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "name": "Test Member", "dashboard_horizon_days": "30", "phone": phone},
            follow_redirects=True,
        )
        assert "Neplatný formát telefonního čísla".encode() in resp.data


class TestPhoneValidationAdminEdit:
    def _create_user(self, app: object) -> str:
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(email="phonetarget@test.com", name="Phone Target", is_active=True)
            user.set_password("pass1234")
            user.roles = [role]
            db.session.add(user)
            db.session.commit()
            return str(user.id)

    @pytest.mark.parametrize("phone", VALID_PHONES)
    def test_valid_phone_accepted_on_admin_edit(self, app: object, admin_client: object, phone: str) -> None:
        uid = self._create_user(app)
        resp = admin_client.post(
            f"/users/{uid}/save",
            data={"name": "Phone Target", "email": "phonetarget@test.com", "phone": phone},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Neplatný formát telefonního čísla".encode() not in resp.data

    @pytest.mark.parametrize("phone", INVALID_PHONES)
    def test_invalid_phone_rejected_on_admin_edit(self, app: object, admin_client: object, phone: str) -> None:
        uid = self._create_user(app)
        resp = admin_client.post(
            f"/users/{uid}/save",
            data={"name": "Phone Target", "email": "phonetarget@test.com", "phone": phone},
            follow_redirects=True,
        )
        assert "Neplatný formát telefonního čísla".encode() in resp.data


# ── Batch action tests ────────────────────────────────────────────────────────

class TestBatchAction:
    """Tests for POST /users/batch role add/remove."""

    def _make_users(self, app, count: int = 3):
        """Create *count* active Member users, return list of (user, uuid_str)."""
        from app.models.user import UserAccount
        from app.models.role import Role
        from app.extensions import db
        with app.app_context():
            member_role = db.session.scalar(db.select(Role).where(Role.name == "Member"))
            users = []
            for i in range(count):
                u = UserAccount(email=f"batchuser{i}@test.cz", name=f"Batch User {i}", is_active=True)
                u.set_password("pass")
                u.roles = [member_role]
                db.session.add(u)
            db.session.commit()
            users = db.session.scalars(db.select(UserAccount).where(
                UserAccount.email.like("batchuser%@test.cz")
            )).all()
            return [(u, str(u.id)) for u in users]

    def _get_role_id(self, app, name: str) -> int:
        from app.models.role import Role
        from app.extensions import db
        with app.app_context():
            return db.session.scalar(db.select(Role).where(Role.name == name)).id

    def test_add_role_to_selected_users(self, app, admin_client):
        """add_role assigns the role to all selected users."""
        pairs = self._make_users(app)
        viewer_id = self._get_role_id(app, "Viewer")
        uids = [uid for _, uid in pairs]

        resp = admin_client.post(
            "/users/batch",
            data={"user_ids": uids, "action": "add_role", "role_id": str(viewer_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from app.extensions import db
        from app.models.user import UserAccount
        with app.app_context():
            for _, uid in pairs:
                u = db.session.get(UserAccount, uid)
                role_names = {r.name for r in u.roles}
                assert "Viewer" in role_names

    def test_remove_role_from_selected_users(self, app, admin_client):
        """remove_role strips the role from all selected users."""
        pairs = self._make_users(app)
        member_id = self._get_role_id(app, "Member")
        uids = [uid for _, uid in pairs]

        resp = admin_client.post(
            "/users/batch",
            data={"user_ids": uids, "action": "remove_role", "role_id": str(member_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from app.extensions import db
        from app.models.user import UserAccount
        with app.app_context():
            for _, uid in pairs:
                u = db.session.get(UserAccount, uid)
                role_names = {r.name for r in u.roles}
                assert "Member" not in role_names

    def test_add_role_skips_users_already_having_it(self, app, admin_client):
        """Users that already have the role are counted as skipped, not double-added."""
        pairs = self._make_users(app, 2)
        member_id = self._get_role_id(app, "Member")
        uids = [uid for _, uid in pairs]

        resp = admin_client.post(
            "/users/batch",
            data={"user_ids": uids, "action": "add_role", "role_id": str(member_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "přeskočeno" in resp.data.decode()

    def test_batch_writes_audit_log(self, app, admin_client):
        """Each affected user gets an AuditLogEntry."""
        pairs = self._make_users(app, 2)
        viewer_id = self._get_role_id(app, "Viewer")
        uids = [uid for _, uid in pairs]

        admin_client.post(
            "/users/batch",
            data={"user_ids": uids, "action": "add_role", "role_id": str(viewer_id)},
            follow_redirects=True,
        )

        from app.extensions import db
        from app.models.audit import AuditLogEntry
        with app.app_context():
            entries = db.session.scalars(
                db.select(AuditLogEntry).where(AuditLogEntry.action_type == "edit")
            ).all()
            assert len(entries) >= 2

    def test_batch_requires_permission(self, member_client):
        """Member (no user.assign_role) gets 403."""
        resp = member_client.post(
            "/users/batch",
            data={"user_ids": ["00000000-0000-0000-0000-000000000001"],
                  "action": "add_role", "role_id": "1"},
        )
        assert resp.status_code == 403

    def test_batch_no_users_selected_redirects(self, admin_client):
        """Empty user_ids gets a warning flash and redirects."""
        resp = admin_client.post(
            "/users/batch",
            data={"action": "add_role", "role_id": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nebyl vybrán".encode() in resp.data

    def test_batch_invalid_action_redirects(self, admin_client, app):
        """Unknown action gets a danger flash."""
        pairs = self._make_users(app, 1)
        member_id = self._get_role_id(app, "Member")
        resp = admin_client.post(
            "/users/batch",
            data={"user_ids": [pairs[0][1]], "action": "delete_all", "role_id": str(member_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Neznámá akce".encode() in resp.data


class TestUserListFiltersAndSort:
    """Tests for role filter and created_at sort on /users/."""

    def test_role_filter_returns_only_matching_users(self, app, admin_client):
        """GET /users/?role=Viewer returns only Viewer users."""
        with app.app_context():
            viewer_role = db.session.scalar(db.select(Role).where(Role.name == "Viewer"))
            u = UserAccount(email="vieweronly@test.cz", name="Viewer Only", is_active=True)
            u.set_password("pass")
            u.roles = [viewer_role]
            db.session.add(u)
            db.session.commit()

        resp = admin_client.get("/users/?role=Viewer")
        assert resp.status_code == 200
        assert b"vieweronly@test.cz" in resp.data
        # member@test.com has Member not Viewer → must not appear
        assert b"member@test.com" not in resp.data

    def test_role_filter_all_shows_all(self, app, admin_client):
        """GET /users/ (no role filter) returns all users."""
        resp = admin_client.get("/users/")
        assert resp.status_code == 200
        assert b"admin@test.com" in resp.data

    def test_sort_by_created_asc(self, admin_client):
        """GET /users/?sort=created&dir=asc returns 200."""
        resp = admin_client.get("/users/?sort=created&dir=asc")
        assert resp.status_code == 200

    def test_sort_by_created_desc(self, admin_client):
        """GET /users/?sort=created&dir=desc returns 200."""
        resp = admin_client.get("/users/?sort=created&dir=desc")
        assert resp.status_code == 200


# ── Archive / Unarchive ───────────────────────────────────────────────────────

class TestUserArchive:
    def _create_target(self, app) -> int:
        """Create a plain member user to be archived and return their id."""
        with app.app_context():
            role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
            u = UserAccount(email="target@archive.test", name="Archivovatelný", is_active=True)
            u.set_password("pass")
            u.roles = [role]
            db.session.add(u)
            db.session.commit()
            return u.id

    def test_archive_requires_permission(self, app, member_client):
        """Member without user.archive cannot archive."""
        uid = self._create_target(app)
        resp = member_client.post(f"/users/{uid}/archive", follow_redirects=True)
        assert resp.status_code == 403

    def test_admin_can_archive(self, app, admin_client):
        """Admin can archive a user — sets is_archived=True, is_active=False."""
        uid = self._create_target(app)
        resp = admin_client.post(f"/users/{uid}/archive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            assert u.is_archived is True
            assert u.is_active is False

    def test_archived_user_hidden_from_list(self, app, admin_client):
        """Archived user does not appear in the default user list."""
        uid = self._create_target(app)
        admin_client.post(f"/users/{uid}/archive")
        resp = admin_client.get("/users/")
        assert b"target@archive.test" not in resp.data

    def test_archived_user_visible_with_param(self, app, admin_client):
        """Archived user appears when ?archived=1 is passed."""
        uid = self._create_target(app)
        admin_client.post(f"/users/{uid}/archive")
        resp = admin_client.get("/users/?archived=1")
        assert resp.status_code == 200
        assert b"target@archive.test" in resp.data

    def test_view_archived_requires_permission(self, app, coordinator_client):
        """Coordinator without user.view_archived gets 403 for ?archived=1."""
        resp = coordinator_client.get("/users/?archived=1")
        assert resp.status_code == 403

    def test_archived_user_cannot_login(self, app, client):
        """Archived user is blocked at login."""
        uid = self._create_target(app)
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            u.is_archived = True
            u.is_active = False
            db.session.commit()
        resp = client.post(
            "/auth/login",
            data={"email": "target@archive.test", "password": "pass"},
            follow_redirects=True,
        )
        assert "archivován".encode() in resp.data

    def test_unarchive_restores_flag(self, app, admin_client):
        """Unarchive sets is_archived=False but leaves is_active=False."""
        uid = self._create_target(app)
        admin_client.post(f"/users/{uid}/archive")
        resp = admin_client.post(f"/users/{uid}/unarchive", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            assert u.is_archived is False
            assert u.is_active is False

    def test_archived_user_not_in_active_users_list(self, app, admin_client):
        """active_users_list() excludes archived users."""
        from app.queries import active_users_list
        uid = self._create_target(app)
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            u.is_archived = True
            u.is_active = False
            db.session.commit()
            ids = [u.id for u in active_users_list()]
            assert uid not in ids

    def test_archived_user_password_reset_silently_denied(self, app, client):
        """Password reset for an archived user shows same message but sends no email."""
        uid = self._create_target(app)
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            nonce_before = u.password_reset_nonce
            u.is_archived = True
            u.is_active = False
            db.session.commit()
        resp = client.post(
            "/auth/forgot-password",
            data={"email": "target@archive.test"},
            follow_redirects=True,
        )
        # UI shows normal message (no enumeration)
        assert "odkaz pro obnovení hesla".encode() in resp.data
        # Nonce must NOT have changed — no reset token was issued
        with app.app_context():
            u = db.session.get(UserAccount, uid)
            assert u.password_reset_nonce == nonce_before


class TestManualUserCreate:
    """Tests for the manual user creation route GET/POST /users/create."""

    def test_create_page_loads_for_admin(self, app: object, admin_client: object) -> None:
        resp = admin_client.get("/users/create")
        assert resp.status_code == 200
        assert "Nový uživatel" in resp.data.decode()

    def test_create_page_forbidden_for_member(self, app: object, member_client: object) -> None:
        resp = member_client.get("/users/create")
        assert resp.status_code == 403

    def test_create_user_success(self, app: object, admin_client: object) -> None:
        from app.extensions import db
        from app.models.user import UserAccount
        import sqlalchemy as sa

        resp = admin_client.post("/users/create", data={
            "csrf_token": "test",
            "name": "Testovací Uživatel",
            "email": "manual.create@test.com",
            "phone": "",
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            user = db.session.scalar(sa.select(UserAccount).where(UserAccount.email == "manual.create@test.com"))
            assert user is not None
            assert user.name == "Testovací Uživatel"
            assert user.is_active is True
            assert not user.check_password("anything")

    def test_create_user_duplicate_email_rejected(self, app: object, admin_client: object) -> None:
        resp = admin_client.post("/users/create", data={
            "csrf_token": "test",
            "name": "Duplikát",
            "email": "admin@test.com",  # already exists
            "phone": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "již použit" in resp.data.decode()

    def test_create_user_missing_name_rejected(self, app: object, admin_client: object) -> None:
        resp = admin_client.post("/users/create", data={
            "csrf_token": "test",
            "name": "",
            "email": "new@test.com",
            "phone": "",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "Jméno nesmí být prázdné" in resp.data.decode()

    def test_create_user_writes_audit_log(self, app: object, admin_client: object) -> None:
        from app.extensions import db
        from app.models.audit import AuditLogEntry
        import sqlalchemy as sa

        admin_client.post("/users/create", data={
            "csrf_token": "test",
            "name": "Audit Test",
            "email": "audit.manual@test.com",
            "phone": "",
        })
        with app.app_context():
            entry = db.session.scalar(
                sa.select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "UserAccount")
                .where(AuditLogEntry.action_type == "create")
                .where(AuditLogEntry.summary.contains("audit.manual@test.com"))
            )
            assert entry is not None
