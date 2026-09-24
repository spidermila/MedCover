"""Login through Keycloak (AUTH_MODE=oidc) and back-channel logout.

Keycloak is not running. Most tests replace Authlib's calls to it; the
"real Authlib" tests replace only Keycloak's HTTP answers, so state, nonce,
PKCE and ID-token validation run for real. Tokens are signed with a key
generated here.
"""

import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.integrations.base_client import OAuthError
from flask import Flask, redirect
from joserfc import jwt
from joserfc.jwk import RSAKey

from app import oidc
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _login, _make_user

ISSUER = "http://localhost:8180/realms/crc"
KEY = RSAKey.generate_key(2048, parameters={"kid": "test"})
JWKS = {"keys": [KEY.as_dict(private=False)]}


@pytest.fixture
def oidc_app(app: Flask, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setitem(app.config, "AUTH_MODE", "oidc")
    monkeypatch.setitem(app.config, "KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
    monkeypatch.setattr(oidc.oauth.keycloak, "fetch_jwk_set", lambda force=False: JWKS)
    return app


@pytest.fixture
def redirects(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record authorize_redirect calls; answer like Authlib, with the internal URL."""
    calls: list[dict[str, Any]] = []

    def authorize_redirect(redirect_uri: str, **kwargs: Any) -> Any:
        calls.append({"redirect_uri": redirect_uri, **kwargs})
        return redirect("http://keycloak:8080/realms/crc/protocol/openid-connect/auth?client_id=medcover")

    monkeypatch.setattr(oidc.oauth.keycloak, "authorize_redirect", authorize_redirect)
    return calls


def _token(monkeypatch: pytest.MonkeyPatch, **claims: Any) -> list[dict[str, Any]]:
    """Make the code exchange return an ID token with these claims."""
    calls: list[dict[str, Any]] = []

    def authorize_access_token(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"id_token": "the-id-token", "userinfo": {"sub": "kc-sub-1", **claims}}

    monkeypatch.setattr(oidc.oauth.keycloak, "authorize_access_token", authorize_access_token)
    return calls


def _user(app: Flask, role: str = Role.MEMBER, **fields: Any) -> uuid.UUID:
    with app.app_context():
        user = _make_user("oidc@test.com", "OIDC User", role)
        for name, value in fields.items():
            setattr(user, name, value)
        db.session.commit()
        return user.id


def _logout_token(key: RSAKey = KEY, **overrides: Any) -> str:
    claims = {
        "iss": ISSUER,
        "aud": "medcover",
        "sub": "kc-sub-1",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "jti": str(uuid.uuid4()),
        "sid": "kc-session",
        "events": {oidc.BACKCHANNEL_LOGOUT_EVENT: {}},
        **overrides,
    }
    return jwt.encode({"alg": "RS256", "kid": "test"}, {k: v for k, v in claims.items() if v is not None}, key)


# ── Configuration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"AUTH_MODE": "ldap"}, "AUTH_MODE must be one of"),
        ({"AUTH_MODE": "oidc", "OIDC_CLIENT_SECRET": "", "KEYCLOAK_INTERNAL_URL": "http://kc"}, "OIDC_CLIENT_SECRET"),
        (
            {"AUTH_MODE": "oidc", "OIDC_CLIENT_SECRET": "s", "KEYCLOAK_INTERNAL_URL": "http://kc", "TESTING": False},
            "absolute https:// KEYCLOAK_PUBLIC_URL",
        ),
        (
            {
                "AUTH_MODE": "oidc",
                "OIDC_CLIENT_SECRET": "s",
                "KEYCLOAK_INTERNAL_URL": "http://kc",
                "KEYCLOAK_PUBLIC_URL": "http://sso.example.org",
                "TESTING": False,
            },
            "absolute https:// KEYCLOAK_PUBLIC_URL",
        ),
    ],
)
def test_init_app_rejects_bad_config(app: Flask, overrides: dict[str, str], message: str) -> None:
    other = Flask(__name__)
    other.config.update(app.config, **overrides)
    with pytest.raises(RuntimeError, match=message):
        oidc.init_app(other)


def test_init_app_accepts_complete_oidc_config(app: Flask) -> None:
    other = Flask(__name__)
    other.config.update(app.config, AUTH_MODE="oidc", OIDC_CLIENT_SECRET="s", KEYCLOAK_INTERNAL_URL="http://kc")
    oidc.init_app(other)
    assert "oidc.callback" in other.view_functions


def test_init_app_accepts_https_public_url_in_production(app: Flask) -> None:
    other = Flask(__name__)
    other.config.update(
        app.config,
        AUTH_MODE="oidc",
        OIDC_CLIENT_SECRET="s",
        KEYCLOAK_INTERNAL_URL="http://kc",
        KEYCLOAK_PUBLIC_URL="https://sso.example.org",
        TESTING=False,
    )
    oidc.init_app(other)
    assert "oidc.callback" in other.view_functions


def test_role_slug_matches_directory_names() -> None:
    assert [oidc.role_slug(n) for n in (Role.ADMIN, Role.DEBRIEFING_MANAGER)] == ["admin", "debriefing-manager"]


def test_oidc_routes_are_absent_in_local_mode(client: Any) -> None:
    assert client.get("/auth/callback").status_code == 404
    assert client.post("/auth/backchannel-logout").status_code == 404


# ── Login ────────────────────────────────────────────────────────────────────


def test_login_redirects_to_public_keycloak_with_pkce_client(oidc_app: Flask, client: Any, redirects: list) -> None:
    resp = client.get("/auth/login?next=/events/")
    assert resp.status_code == 302
    assert resp.location.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    assert redirects == [{"redirect_uri": "http://localhost/auth/callback"}]
    with client.session_transaction() as sess:
        assert sess["oidc_next"] == "/events/"


def test_login_ignores_foreign_next(oidc_app: Flask, client: Any, redirects: list) -> None:
    client.get("/auth/login?next=https://evil.example/")
    with client.session_transaction() as sess:
        assert sess["oidc_next"] == "/dashboard"


def test_callback_logs_in_and_takes_roles_from_token(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _user(oidc_app, Role.MEMBER)
    calls = _token(monkeypatch, crc_member_id=str(user_id), medcover_roles=["coordinator", "debriefing-manager"])
    with client.session_transaction() as sess:
        sess["oidc_next"] = "/events/"

    resp = client.get("/auth/callback?code=c&state=s")

    assert resp.status_code == 302 and resp.location == "/events/"
    assert calls == [
        {
            "claims_options": {
                "iss": {"essential": True, "value": ISSUER},
                "aud": {"essential": True, "value": "medcover"},
            }
        }
    ]
    with oidc_app.app_context():
        user = db.session.get(UserAccount, user_id)
        assert sorted(r.name for r in user.roles) == [Role.COORDINATOR, Role.DEBRIEFING_MANAGER]
        assert user.oidc_sub == "kc-sub-1" and user.last_login_at is not None
    with client.session_transaction() as sess:
        assert sess["_user_id"] == str(user_id)
        assert sess["session_epoch"] == 0 and sess["oidc_id_token"] == "the-id-token"
    assert client.get("/users/profile").status_code == 200


@pytest.mark.parametrize(
    "member_id, roles, fields",
    [
        ("user", [], {}),  # no MedCover role
        ("user", ["memberbase-admin"], {}),  # only roles MedCover does not know
        ("user", ["member"], {"is_archived": True}),
        ("user", ["member"], {"is_active": False}),  # deactivated by an admin
        ("00000000-0000-4000-8000-000000000001", ["member"], {}),  # not in MedCover yet
        ("not-a-uuid", ["member"], {}),
        (None, ["member"], {}),
    ],
)
def test_callback_refuses(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch, member_id: str | None, roles: list, fields: dict
) -> None:
    user_id = _user(oidc_app, Role.MEMBER, **fields)
    claims: dict[str, Any] = {"medcover_roles": roles}
    if member_id is not None:
        claims["crc_member_id"] = str(user_id) if member_id == "user" else member_id
    _token(monkeypatch, **claims)

    resp = client.get("/auth/callback?code=c&state=s")

    assert resp.status_code == 403
    assert "Přihlášení odmítnuto" in resp.text
    assert "id_token_hint=the-id-token" in resp.text
    with client.session_transaction() as sess:
        assert "_user_id" not in sess
    with oidc_app.app_context():
        assert db.session.get(UserAccount, user_id).oidc_sub is None


def test_callback_error_when_logged_in_returns_to_next(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. the person cancelled an account action."""
    _logged_in(oidc_app, client, monkeypatch)
    with client.session_transaction() as sess:
        sess["oidc_next"] = "/users/profile"
    resp = client.get("/auth/callback?error=access_denied")
    assert resp.status_code == 302 and resp.location == "/users/profile"


def test_callback_error_when_logged_out_does_not_loop(oidc_app: Flask, client: Any) -> None:
    resp = client.get("/auth/callback?error=unauthorized_client")
    assert resp.status_code == 400 and "Přihlášení se nezdařilo" in resp.text


@pytest.mark.parametrize(
    "error", [OAuthError(error="invalid_grant"), oidc.JoseError("bad token"), oidc.requests.ConnectionError()]
)
def test_callback_failure_offers_retry(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch, error) -> None:
    def authorize_access_token(**kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(oidc.oauth.keycloak, "authorize_access_token", authorize_access_token)
    resp = client.get("/auth/callback?code=c&state=s")
    assert resp.status_code == 400
    assert "Přihlášení se nezdařilo" in resp.text and 'href="/auth/login"' in resp.text


def test_login_keeps_only_the_newest_login_state(oidc_app: Flask, client: Any, redirects: list) -> None:
    with client.session_transaction() as sess:
        sess["_state_keycloak_older"] = {"exp": 100}
        sess["_state_keycloak_newer"] = {"exp": 200}
        sess["_state_keycloak_oldest"] = {"exp": 50}
        sess["other"] = 1
    client.get("/auth/login")  # the fake authorize_redirect adds no state of its own
    with client.session_transaction() as sess:
        assert sorted(k for k in sess if k.startswith("_state_")) == ["_state_keycloak_newer"]
        assert sess["other"] == 1


def test_keycloak_client_uses_pkce_and_a_timeout() -> None:
    kwargs = oidc.oauth.keycloak.client_kwargs
    assert kwargs["code_challenge_method"] == "S256" and kwargs["default_timeout"] == 10
    assert oidc.oauth.keycloak._server_metadata_url.endswith("/realms/crc/.well-known/openid-configuration")


def test_local_password_pages_are_gone(oidc_app: Flask, client: Any) -> None:
    assert client.get("/auth/forgot-password").status_code == 404
    assert client.get("/auth/reset-password/x").status_code == 404
    assert client.get("/auth/register/x").status_code == 404


def test_local_password_login_still_works_in_local_mode(app: Flask, client: Any) -> None:
    _user(app)
    _login(client, "oidc@test.com")
    assert client.get("/users/profile").status_code == 200


# ── Logged in ────────────────────────────────────────────────────────────────


def _logged_in(app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    user_id = _user(app)
    _token(monkeypatch, crc_member_id=str(user_id), medcover_roles=["member"])
    client.get("/auth/callback?code=c&state=s")
    return user_id


def test_logout_ends_keycloak_session(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _logged_in(oidc_app, client, monkeypatch)
    resp = client.get("/auth/logout")
    assert resp.location.startswith(f"{ISSUER}/protocol/openid-connect/logout?client_id=medcover&")
    assert "post_logout_redirect_uri=http%3A%2F%2Flocalhost%2Fauth%2Flogin" in resp.location
    assert "id_token_hint=the-id-token" in resp.location
    assert client.get("/users/profile").status_code == 302


def test_profile_offers_keycloak_account_actions(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _logged_in(oidc_app, client, monkeypatch)
    page = client.get("/users/profile").text
    assert "Zabezpečení účtu" in page and "current_password" not in page
    for action in oidc.KC_ACTIONS:
        assert f"/auth/account/{action}" in page


def test_profile_ignores_local_password_change(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = _logged_in(oidc_app, client, monkeypatch)
    form = {
        "action": "password",
        "current_password": "testpass123",
        "new_password": "x" * 20,
        "confirm_password": "x" * 20,
    }
    assert client.post("/users/profile", data=form).status_code == 200
    with oidc_app.app_context():
        assert db.session.get(UserAccount, user_id).check_password("testpass123")


def test_account_action_starts_keycloak_action(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch, redirects: list
) -> None:
    _logged_in(oidc_app, client, monkeypatch)
    assert client.get("/auth/account/CONFIGURE_TOTP").status_code == 302
    assert redirects[-1]["kc_action"] == "CONFIGURE_TOTP"
    with client.session_transaction() as sess:
        assert sess["oidc_next"] == "/users/profile"
    assert client.get("/auth/account/delete_account").status_code == 404


# ── Back-channel logout ──────────────────────────────────────────────────────


def test_backchannel_logout_ends_sessions(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = _logged_in(oidc_app, client, monkeypatch)
    keycloak = oidc_app.test_client()

    resp = keycloak.post("/auth/backchannel-logout", data={"logout_token": _logout_token()})

    assert resp.status_code == 200
    with oidc_app.app_context():
        assert db.session.get(UserAccount, user_id).session_epoch == 1
    assert client.get("/users/profile").status_code == 302


def test_backchannel_logout_for_unknown_person_is_accepted(oidc_app: Flask, client: Any) -> None:
    assert (
        client.post("/auth/backchannel-logout", data={"logout_token": _logout_token(sub="nobody")}).status_code == 200
    )


@pytest.mark.parametrize(
    "token",
    [
        lambda: "",
        lambda: "not.a.jwt",
        lambda: _logout_token(key=RSAKey.generate_key(2048, parameters={"kid": "test"})),
        lambda: _logout_token(aud="memberbase"),
        lambda: _logout_token(exp=int(time.time()) - 600),
        lambda: _logout_token(sub=None),
        lambda: _logout_token(events=None),
        lambda: _logout_token(events={"other": {}}),
        lambda: _logout_token(iss="http://localhost:8180/realms/other"),
        lambda: _logout_token(nonce="n"),
    ],
)
def test_backchannel_logout_rejects_bad_tokens(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch, token
) -> None:
    user_id = _logged_in(oidc_app, client, monkeypatch)
    resp = oidc_app.test_client().post("/auth/backchannel-logout", data={"logout_token": token()})
    assert resp.status_code == 400
    with oidc_app.app_context():
        assert db.session.get(UserAccount, user_id).session_epoch == 0
    assert client.get("/users/profile").status_code == 200


def test_end_session_url_without_id_token(oidc_app: Flask) -> None:
    with oidc_app.test_request_context("/"):
        assert "id_token_hint" not in oidc.end_session_url()


def test_backchannel_logout_accepts_small_clock_skew(oidc_app: Flask, client: Any) -> None:
    token = _logout_token(iat=int(time.time()) + 5)
    assert client.post("/auth/backchannel-logout", data={"logout_token": token}).status_code == 200


def test_backchannel_logout_refetches_keys_only_for_unknown_key_id(
    oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rotated = RSAKey.generate_key(2048, parameters={"kid": "rotated"})
    fetches: list[bool] = []

    def fetch_jwk_set(force: bool = False) -> dict:
        fetches.append(force)
        return {"keys": [KEY.as_dict(private=False)] + ([rotated.as_dict(private=False)] if force else [])}

    monkeypatch.setattr(oidc.oauth.keycloak, "fetch_jwk_set", fetch_jwk_set)
    monkeypatch.setattr(oidc, "_jwks_refreshed_at", 0.0)
    assert client.post("/auth/backchannel-logout", data={"logout_token": _logout_token()}).status_code == 200
    assert fetches == [False]
    token = jwt.encode({"alg": "RS256", "kid": "rotated"}, jwt.decode(_logout_token(), KEY).claims, rotated)
    assert client.post("/auth/backchannel-logout", data={"logout_token": token}).status_code == 200
    assert fetches == [False, False, True]
    # Another unknown key id right after: no new fetch, so it cannot be used to hammer Keycloak.
    stranger = RSAKey.generate_key(2048, parameters={"kid": "stranger"})
    token = jwt.encode({"alg": "RS256", "kid": "stranger"}, jwt.decode(_logout_token(), KEY).claims, stranger)
    assert client.post("/auth/backchannel-logout", data={"logout_token": token}).status_code == 400
    assert fetches == [False, False, True, False]


def test_backchannel_logout_works_with_csrf_protection(oidc_app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(oidc_app.config, "WTF_CSRF_ENABLED", True)
    resp = oidc_app.test_client().post("/auth/backchannel-logout", data={"logout_token": _logout_token()})
    assert resp.status_code == 200


# ── Other login paths and session revocation ────────────────────────────────


def test_local_login_after_backchannel_logout_still_works(app: Flask, client: Any) -> None:
    """Rolling back to AUTH_MODE=local must not lock out people whose epoch moved on."""
    _user(app, session_epoch=3)
    _login(client, "oidc@test.com")
    assert client.get("/users/profile").status_code == 200


def test_switching_auth_mode_ends_open_sessions(app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _user(app)
    _login(client, "oidc@test.com")
    assert client.get("/users/profile").status_code == 200

    monkeypatch.setitem(app.config, "AUTH_MODE", "oidc")

    assert client.get("/users/profile").status_code == 302


def test_deactivation_ends_open_sessions(app: Flask, admin_client: Any) -> None:
    user_id = _user(app)
    member = app.test_client()
    _login(member, "oidc@test.com")
    assert member.get("/users/profile").status_code == 200

    admin_client.post(f"/users/{user_id}/deactivate")

    assert member.get("/users/profile").status_code == 302


def test_archiving_ends_open_sessions(app: Flask, admin_client: Any) -> None:
    user_id = _user(app)
    member = app.test_client()
    _login(member, "oidc@test.com")

    admin_client.post(f"/users/{user_id}/archive")

    assert member.get("/users/profile").status_code == 302


def test_invitations_are_off(app: Flask, admin_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "/users/invites" in admin_client.get("/users/").text
    monkeypatch.setitem(app.config, "AUTH_MODE", "oidc")  # after the password login of admin_client
    assert admin_client.get("/users/invites").status_code == 404
    assert admin_client.post("/users/invites/create", data={"email": "x@test.com"}).status_code == 404
    assert "/users/invites" not in admin_client.get("/users/").text


def test_manual_accounts_and_admin_passwords_are_off(
    app: Flask, admin_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _user(app)
    monkeypatch.setitem(app.config, "AUTH_MODE", "oidc")
    assert admin_client.get("/users/create").status_code == 404
    assert "/users/create" not in admin_client.get("/users/").text
    assert 'name="new_password"' not in admin_client.get(f"/users/{user_id}").text
    with app.app_context():
        user = db.session.get(UserAccount, user_id)
        form = {"name": user.name, "email": user.email, "version": user.version, "new_password": "x" * 20}
        form |= {"role_ids": [r.id for r in user.roles]}
    admin_client.post(f"/users/{user_id}/save", data=form)
    with app.app_context():
        assert db.session.get(UserAccount, user_id).check_password("testpass123")


# ── Real Authlib: state, nonce, PKCE and ID-token checks ─────────────────────


@pytest.fixture
def keycloak_http(oidc_app: Flask, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for Keycloak's HTTP answers only; the ID token is signed with KEY."""
    internal = "http://keycloak:8080/realms/crc"
    metadata = {
        "issuer": internal,
        "authorization_endpoint": f"{internal}/protocol/openid-connect/auth",
        "token_endpoint": f"{internal}/protocol/openid-connect/token",
        "jwks_uri": f"{internal}/protocol/openid-connect/certs",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    monkeypatch.setattr(oidc.oauth.keycloak, "load_server_metadata", lambda: metadata)
    exchange: dict[str, Any] = {"claims": {}}

    def fetch_access_token(**params: Any) -> dict[str, Any]:
        exchange["params"] = params
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "medcover", "sub": "kc-sub-1", "iat": now, "exp": now + 300}
        claims |= {"nonce": exchange["nonce"], **exchange["claims"]}
        id_token = jwt.encode({"alg": "RS256", "kid": "test"}, claims, KEY)
        return {"access_token": "at", "token_type": "Bearer", "id_token": id_token}

    monkeypatch.setattr(oidc.oauth.keycloak, "fetch_access_token", fetch_access_token)
    return exchange


def _start_login(client: Any, exchange: dict[str, Any]) -> dict[str, str]:
    resp = client.get("/auth/login")
    query = {k: v[0] for k, v in parse_qs(urlsplit(resp.location).query).items()}
    exchange["nonce"] = query["nonce"]
    return query


def test_real_authlib_login(oidc_app: Flask, client: Any, keycloak_http: dict[str, Any]) -> None:
    user_id = _user(oidc_app)
    keycloak_http["claims"] = {"crc_member_id": str(user_id), "medcover_roles": ["member"]}
    query = _start_login(client, keycloak_http)
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    assert query["redirect_uri"] == "http://localhost/auth/callback" and query["client_id"] == "medcover"

    resp = client.get(f"/auth/callback?code=the-code&state={query['state']}")

    assert resp.status_code == 302
    assert keycloak_http["params"]["code"] == "the-code" and keycloak_http["params"]["code_verifier"]
    assert client.get("/users/profile").status_code == 200


@pytest.mark.parametrize(
    "tamper",
    [
        lambda q, x: x.update(nonce="other-nonce"),
        lambda q, x: x["claims"].update(iss="http://evil:8180/realms/crc"),
        lambda q, x: x["claims"].update(aud="memberbase", azp="medcover"),
        lambda q, x: x["claims"].update(exp=int(time.time()) - 600),
        lambda q, x: q.update(state="forged-state"),
    ],
)
def test_real_authlib_rejects(oidc_app: Flask, client: Any, keycloak_http: dict[str, Any], tamper) -> None:
    user_id = _user(oidc_app)
    keycloak_http["claims"] = {"crc_member_id": str(user_id), "medcover_roles": ["member"]}
    query = _start_login(client, keycloak_http)
    tamper(query, keycloak_http)

    resp = client.get(f"/auth/callback?code=the-code&state={query['state']}")

    assert resp.status_code == 400
    assert client.get("/users/profile").status_code == 302


def test_refused_relogin_ends_the_open_session(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Roles removed in the directory, then an account action logs in again."""
    user_id = _logged_in(oidc_app, client, monkeypatch)
    other_device = oidc_app.test_client()
    _token(monkeypatch, crc_member_id=str(user_id), medcover_roles=["member"])
    other_device.get("/auth/callback?code=c&state=s")
    _token(monkeypatch, crc_member_id=str(user_id), medcover_roles=[])

    assert client.get("/auth/callback?code=c&state=s").status_code == 403

    assert client.get("/users/profile").status_code == 302
    assert other_device.get("/users/profile").status_code == 302


def test_role_change_at_login_is_audited(oidc_app: Flask, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = _user(oidc_app, Role.MEMBER)
    with oidc_app.app_context():
        version = db.session.get(UserAccount, user_id).version
    _token(monkeypatch, crc_member_id=str(user_id), medcover_roles=["admin"])
    client.get("/auth/callback?code=c&state=s")
    client.get("/auth/callback?code=c&state=s")  # same roles again: no second entry

    with oidc_app.app_context():
        entries = db.session.scalars(db.select(AuditLogEntry).where(AuditLogEntry.entity_id == str(user_id))).all()
        assert [(e.actor_id, e.changes_json) for e in entries] == [(user_id, {"roles": [["Member"], ["Admin"]]})]
        assert db.session.get(UserAccount, user_id).version == version + 1
