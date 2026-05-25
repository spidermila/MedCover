"""Tests for authentication routes: login, logout, forgot-password, register."""

from app.constants import MIN_PASSWORD_LENGTH
from app.extensions import db as _db
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user, _login


class TestLoginPage:
    def test_login_page_loads(self, client):
        response = client.get("/auth/login")
        assert response.status_code == 200

    def test_login_page_contains_form(self, client):
        response = client.get("/auth/login")
        assert b"email" in response.data.lower()

    def test_login_redirects_to_dashboard(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        response = client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/dashboard" in response.headers["Location"]

    def test_login_wrong_password_stays_on_login(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        response = client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "wrongpassword"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert response.request.path == "/auth/login"
        assert "Nesprávný e-mail nebo heslo".encode() in response.data

    def test_login_unknown_email_stays_on_login(self, client):
        response = client.post(
            "/auth/login",
            data={"email": "nobody@example.com", "password": "testpass123"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_login_inactive_user_rejected(self, app, client):
        with app.app_context():
            role = _db.session.scalar(_db.select(Role).where(Role.name == Role.MEMBER))
            user = UserAccount(email="inactive@example.com", name="Inactive", is_active=False)
            user.set_password("testpass123")
            user.roles = [role]
            _db.session.add(user)
            _db.session.commit()
        response = client.post(
            "/auth/login",
            data={"email": "inactive@example.com", "password": "testpass123"},
            follow_redirects=True,
        )
        # Must stay on login page with activation warning
        assert response.request.path == "/auth/login"
        assert b"aktivaci" in response.data


class TestLogout:
    def test_logout_redirects_to_login(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        _login(client, "test@example.com")
        response = client.get("/auth/logout", follow_redirects=False)
        assert response.status_code == 302

    def test_logout_without_login_redirects(self, client):
        response = client.get("/auth/logout", follow_redirects=False)
        assert response.status_code == 302


class TestProtectedRoutes:
    def test_dashboard_requires_login(self, client):
        response = client.get("/dashboard", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_events_requires_login(self, client):
        response = client.get("/events/", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_admin_requires_login(self, client):
        response = client.get("/admin/", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]


class TestForgotPassword:
    def test_forgot_password_page_loads(self, client):
        response = client.get("/auth/forgot-password")
        assert response.status_code == 200

    def test_forgot_password_with_unknown_email_does_not_error(self, client):
        """Security: must not reveal whether email exists."""
        response = client.post(
            "/auth/forgot-password",
            data={"email": "nobody@example.com"},
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestOpenRedirectProtection:
    """The ?next= parameter on login must not redirect to external URLs."""

    def test_external_next_redirects_to_dashboard(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        response = client.post(
            "/auth/login?next=https://evil.example.com/steal",
            data={"email": "test@example.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "evil.example.com" not in location
        assert "/dashboard" in location

    def test_protocol_relative_next_redirects_to_dashboard(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        response = client.post(
            "/auth/login?next=//evil.example.com/steal",
            data={"email": "test@example.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "evil.example.com" not in location

    def test_same_origin_next_is_honoured(self, app, client):
        with app.app_context():
            _make_user("test@example.com", "Test User", Role.MEMBER)
        response = client.post(
            "/auth/login?next=/events/",
            data={"email": "test@example.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/events/" in response.headers["Location"]


class TestBruteForceProtection:
    """Login lockout after repeated failed attempts."""

    def _post_login(self, client, email: str, password: str):
        return client.post(
            "/auth/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    def test_account_locked_after_max_attempts(self, app, client):
        """After LOGIN_MAX_ATTEMPTS failures the account is locked."""
        from app.config import LOGIN_MAX_ATTEMPTS
        with app.app_context():
            _make_user("lock@example.com", "Lock User", Role.MEMBER)

        for _ in range(LOGIN_MAX_ATTEMPTS):
            resp = self._post_login(client, "lock@example.com", "wrongpassword")
            assert "Nesprávný e-mail nebo heslo".encode() in resp.data

        # Next attempt should show lockout message
        resp = self._post_login(client, "lock@example.com", "wrongpassword")
        assert "zablokováno".encode() in resp.data

    def test_locked_account_rejects_correct_password(self, app, client):
        """Even the correct password is rejected while the account is locked."""
        from app.config import LOGIN_MAX_ATTEMPTS
        with app.app_context():
            _make_user("locked2@example.com", "Lock2 User", Role.MEMBER)

        for _ in range(LOGIN_MAX_ATTEMPTS):
            self._post_login(client, "locked2@example.com", "wrongpassword")

        # Correct password still rejected while locked
        resp = self._post_login(client, "locked2@example.com", "testpass123")
        assert "zablokováno".encode() in resp.data
        assert resp.request.path == "/auth/login"

    def test_successful_login_resets_counter(self, app, client):
        """A successful login clears failed_login_attempts."""
        with app.app_context():
            _make_user("good@example.com", "Good User", Role.MEMBER)

        # Two failures
        self._post_login(client, "good@example.com", "wrong")
        self._post_login(client, "good@example.com", "wrong")

        # Successful login
        resp = client.post(
            "/auth/login",
            data={"email": "good@example.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        # Check counter was reset in DB
        with app.app_context():
            user = _db.session.scalar(
                _db.select(UserAccount).where(UserAccount.email == "good@example.com")
            )
            assert user is not None
            assert user.failed_login_attempts == 0
            assert user.login_locked_until is None

    def test_failed_attempt_increments_counter(self, app, client):
        """Each failed login increments failed_login_attempts."""
        with app.app_context():
            _make_user("count@example.com", "Count User", Role.MEMBER)

        self._post_login(client, "count@example.com", "wrong")
        self._post_login(client, "count@example.com", "wrong")

        with app.app_context():
            user = _db.session.scalar(
                _db.select(UserAccount).where(UserAccount.email == "count@example.com")
            )
            assert user is not None
            assert user.failed_login_attempts == 2

    def test_unknown_email_does_not_crash(self, client):
        """Attempting to log in with an unknown email shows generic error, no crash."""
        resp = self._post_login(client, "nobody@example.com", "wrongpassword")
        assert resp.status_code == 200
        assert "Nesprávný".encode() in resp.data


class TestRegisterFlow:
    """Invite-based registration flow."""

    def _make_invite(self, app, admin_email: str = "admin@test.com") -> str:
        """Create a valid registration invite and return its token."""
        from app.models.invite import RegistrationInvite
        from app.models.role import Role
        from tests.conftest import _make_user

        with app.app_context():
            _make_user(admin_email, "Test Admin", Role.ADMIN)
            admin = _db.session.scalar(_db.select(UserAccount).where(UserAccount.email == admin_email))
            invite = RegistrationInvite(email="newuser@example.com", created_by_id=admin.id)
            _db.session.add(invite)
            _db.session.commit()
            return invite.token

    def test_invalid_token_rejected(self, client):
        response = client.get("/auth/register/invalidtoken", follow_redirects=True)
        assert response.status_code == 200
        assert "Pozvánka je neplatná".encode() in response.data

    def test_valid_token_shows_form(self, app, client):
        token = self._make_invite(app)
        response = client.get(f"/auth/register/{token}")
        assert response.status_code == 200
        assert b"newuser@example.com" in response.data

    def test_register_creates_active_user(self, app, client):
        token = self._make_invite(app)
        response = client.post(
            f"/auth/register/{token}",
            data={
                "full_name": "Nový Uživatel",
                "password": "securepass99",
                "password2": "securepass99",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Registrace dokončena".encode() in response.data
        assert "přihlásit".encode() in response.data
        with app.app_context():
            user = _db.session.scalar(_db.select(UserAccount).where(UserAccount.email == "newuser@example.com"))
            assert user is not None
            assert user.is_active is True

    def test_register_short_password_rejected(self, app, client):
        token = self._make_invite(app)
        response = client.post(
            f"/auth/register/{token}",
            data={"full_name": "Test", "password": "short", "password2": "short"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert f"{MIN_PASSWORD_LENGTH} znaků".encode() in response.data
        with app.app_context():
            user = _db.session.scalar(_db.select(UserAccount).where(UserAccount.email == "newuser@example.com"))
            assert user is None

    def test_register_mismatched_passwords_rejected(self, app, client):
        token = self._make_invite(app)
        response = client.post(
            f"/auth/register/{token}",
            data={"full_name": "Test", "password": "securepass99", "password2": "different99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Hesla se neshodují".encode() in response.data

    def test_register_missing_name_rejected(self, app, client):
        token = self._make_invite(app)
        response = client.post(
            f"/auth/register/{token}",
            data={"full_name": "", "password": "securepass99", "password2": "securepass99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "celé jméno".encode() in response.data


class TestResetPassword:
    """Password reset via signed token."""

    def _make_reset_token(self, app) -> str:
        import secrets
        from app.models.role import Role
        from tests.conftest import _make_user

        with app.app_context():
            _make_user("reset@example.com", "Reset User", Role.MEMBER)
            user = _db.session.scalar(_db.select(UserAccount).where(UserAccount.email == "reset@example.com"))
            from app.routes.auth import _make_signed_token, _RESET_SALT
            from app.config import RESET_TOKEN_MINUTES
            nonce = secrets.token_hex(16)
            user.password_reset_nonce = nonce
            _db.session.commit()
            return _make_signed_token(f"{user.id}:{nonce}", _RESET_SALT, RESET_TOKEN_MINUTES * 60)

    def test_invalid_token_shows_error_page(self, client):
        response = client.get("/auth/reset-password/badtoken")
        assert response.status_code == 400
        assert "Neplatný odkaz".encode() in response.data

    def test_valid_token_shows_form(self, app, client):
        token = self._make_reset_token(app)
        response = client.get(f"/auth/reset-password/{token}")
        assert response.status_code == 200

    def test_reset_changes_password(self, app, client):
        token = self._make_reset_token(app)
        response = client.post(
            f"/auth/reset-password/{token}",
            data={"password": "NewPassword99", "password2": "NewPassword99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Heslo bylo změněno".encode() in response.data
        with app.app_context():
            user = _db.session.scalar(_db.select(UserAccount).where(UserAccount.email == "reset@example.com"))
            assert user.check_password("NewPassword99") is True
            assert user.password_reset_nonce is None  # link invalidated

    def test_reset_link_single_use(self, app, client):
        """After a successful reset, the same token must be rejected."""
        token = self._make_reset_token(app)
        client.post(
            f"/auth/reset-password/{token}",
            data={"password": "NewPassword99", "password2": "NewPassword99"},
            follow_redirects=True,
        )
        # Second use of the same token
        response = client.get(f"/auth/reset-password/{token}")
        assert response.status_code == 400
        assert "Neplatný odkaz".encode() in response.data

    def test_reset_short_password_rejected(self, app, client):
        token = self._make_reset_token(app)
        response = client.post(
            f"/auth/reset-password/{token}",
            data={"password": "short", "password2": "short"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert f"{MIN_PASSWORD_LENGTH} znaků".encode() in response.data

    def test_reset_mismatched_passwords_rejected(self, app, client):
        token = self._make_reset_token(app)
        response = client.post(
            f"/auth/reset-password/{token}",
            data={"password": "NewPassword99", "password2": "different99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "neshodují".encode() in response.data
