"""Tests for event model unit tests (no HTTP, pure model logic)."""

from app.extensions import db
from app.models.event import EventStatus
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user


class TestEventStatusValues:
    def test_draft_value(self):
        assert EventStatus.DRAFT.value == "Koncept"

    def test_published_value(self):
        assert EventStatus.PUBLISHED.value == "Zveřejněná"

    def test_assignments_open_value(self):
        assert EventStatus.ASSIGNMENTS_OPEN.value == "Přihlášky otevřeny"

    def test_assignments_closed_value(self):
        assert EventStatus.ASSIGNMENTS_CLOSED.value == "Přihlášky uzavřeny"

    def test_completed_value(self):
        assert EventStatus.COMPLETED.value == "Dokončena"

    def test_cancelled_value(self):
        assert EventStatus.CANCELLED.value == "Zrušena"


class TestUserPermissions:
    def test_has_permission_returns_true_for_admin(self, app):

        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            # Reload user in same context to test permissions
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            assert loaded.has_permission("event.create") is True

    def test_has_permission_returns_false_for_viewer(self, app):

        with app.app_context():
            _make_user("viewer@test.com", "Viewer", Role.VIEWER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "viewer@test.com"))
            assert loaded.has_permission("event.create") is False

    def test_has_any_permission(self, app):

        with app.app_context():
            _make_user("admin@test.com", "Admin", Role.ADMIN)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            assert loaded.has_any_permission("event.create", "nonexistent.perm") is True

    def test_has_any_permission_all_missing(self, app):

        with app.app_context():
            _make_user("viewer@test.com", "Viewer", Role.VIEWER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "viewer@test.com"))
            assert loaded.has_any_permission("event.create", "event.edit") is False


class TestUserPassword:
    """Tests for set_password / check_password model methods."""

    def _make_unsaved_user(self) -> UserAccount:
        user = UserAccount(email="pw@test.com", name="PW User", is_active=True)
        return user

    def test_set_and_check_password_correct(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("supersecret99") is True

    def test_check_password_wrong_returns_false(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("wrongpassword") is False

    def test_check_password_empty_returns_false(self):
        user = self._make_unsaved_user()
        user.set_password("supersecret99")
        assert user.check_password("") is False

    def test_password_is_hashed_not_stored_plaintext(self):
        user = self._make_unsaved_user()
        user.set_password("mysecretpassword")
        assert user.password_hash != "mysecretpassword"
        assert "mysecretpassword" not in (user.password_hash or "")


class TestUserGetId:
    def test_get_id_returns_string(self, app):

        with app.app_context():
            _make_user("getid@test.com", "GetId User", Role.MEMBER)
            loaded = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "getid@test.com"))
            result = loaded.get_id()
            assert isinstance(result, str)
            # Must be UUID-like (non-empty string representation of the PK)
            assert len(result) > 0
