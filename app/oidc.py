"""Login through Keycloak (OIDC authorization code + PKCE) and Keycloak's
back-channel logout. Active only when ``AUTH_MODE`` is ``"oidc"``; with the
default ``"local"`` MedCover keeps its own password login.

Keycloak decides who may log in (password, second factor, account status).
The ID token names the person by ``crc_member_id``, which is the UUID of
their ``user_account`` row, and lists their MedCover roles in
``medcover_roles``. With the directory sync on, the person is first copied
from the directory; the roles in the token then replace the local ones.
"""

import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

import ldap
import requests
from authlib.integrations.base_client import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, Flask, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import ColumnElement
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.wrappers import Response

from app import directory_sync
from app.extensions import csrf, db
from app.models.audit import AuditLogEntry
from app.models.role import Role
from app.models.user import UserAccount
from app.utils import diff_changes, external_url_for, safe_next

oidc_bp = Blueprint("oidc", __name__, url_prefix="/auth")
oauth = OAuth()

AUTH_MODES = {"local", "oidc"}
# Keycloak required actions a person can start from their profile.
KC_ACTIONS = {"UPDATE_PASSWORD", "CONFIGURE_TOTP", "webauthn-register", "webauthn-register-passwordless"}
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
# Login attempts whose Authlib state is kept in the session (parallel tabs).
KEPT_LOGIN_STATES = 2
# The back-channel endpoint is public: a token naming an unknown key refetches
# Keycloak's keys at most this often.
JWKS_REFRESH_SECONDS = 60
_jwks_refreshed_at = 0.0


def init_app(app: Flask) -> None:
    cfg = app.config
    if cfg["AUTH_MODE"] not in AUTH_MODES:
        raise RuntimeError(f"AUTH_MODE must be one of {sorted(AUTH_MODES)}, not {cfg['AUTH_MODE']!r}.")
    if cfg["AUTH_MODE"] == "oidc":
        missing = [k for k in ("OIDC_CLIENT_SECRET", "KEYCLOAK_INTERNAL_URL") if not cfg[k]]
        if missing:
            raise RuntimeError(f"AUTH_MODE=oidc requires {', '.join(missing)}.")
        # The default takes the host from the request, which a client controls;
        # it drives the login redirect and the expected token issuer.
        public = cfg["KEYCLOAK_PUBLIC_URL"]
        if not (app.debug or app.testing) and ("{" in public or not public.startswith("https://")):
            raise RuntimeError("AUTH_MODE=oidc in production requires an absolute https:// KEYCLOAK_PUBLIC_URL.")
    oauth.init_app(app)
    oauth.register(
        "keycloak",
        client_id=cfg["OIDC_CLIENT_ID"],
        client_secret=cfg["OIDC_CLIENT_SECRET"],
        server_metadata_url=f"{_internal_realm_url(app)}/.well-known/openid-configuration",
        # A stalled Keycloak must not hold a worker for ever.
        client_kwargs={"scope": "openid", "code_challenge_method": "S256", "default_timeout": 10},
    )
    app.register_blueprint(oidc_bp)


def enabled() -> bool:
    return bool(current_app.config["AUTH_MODE"] == "oidc")


@oidc_bp.before_request
def _only_in_oidc_mode() -> None:
    if not enabled():
        abort(404)


def _internal_realm_url(app: Flask) -> str:
    return f"{app.config['KEYCLOAK_INTERNAL_URL']}/realms/{app.config['KEYCLOAK_REALM']}"


def _public_realm_url() -> str:
    """Keycloak's browser-facing realm URL for the current request."""
    base = current_app.config["KEYCLOAK_PUBLIC_URL"].format(
        scheme=request.scheme, hostname=request.host.rsplit(":", 1)[0]
    )
    return f"{base}/realms/{current_app.config['KEYCLOAK_REALM']}"


def login_redirect(next_url: str | None, kc_action: str | None = None) -> Response:
    """Send the browser to Keycloak; it comes back to ``callback``."""
    session["oidc_next"] = safe_next(next_url)
    # Authlib keeps ~0.5 KB of state per started login in the cookie session and
    # prunes it only on success; keep the newest few so the cookie stays small.
    states = sorted((k for k in session if k.startswith("_state_keycloak_")), key=lambda k: session[k].get("exp", 0))
    for key in states[: max(len(states) - KEPT_LOGIN_STATES + 1, 0)]:
        del session[key]
    extra = {"kc_action": kc_action} if kc_action else {}
    resp: Response = oauth.keycloak.authorize_redirect(external_url_for("oidc.callback"), **extra)
    # The metadata comes from Keycloak's internal address; the browser needs
    # the public one. Tokens are still exchanged internally.
    resp.location = str(resp.location).replace(_internal_realm_url(current_app), _public_realm_url(), 1)
    return resp


def end_session_url(id_token: str | None = None) -> str:
    """Keycloak's logout page, which returns to MedCover's login."""
    params = {
        "client_id": current_app.config["OIDC_CLIENT_ID"],
        "post_logout_redirect_uri": external_url_for("auth.login"),
    }
    if id_token:
        params["id_token_hint"] = id_token
    return f"{_public_realm_url()}/protocol/openid-connect/logout?{urlencode(params)}"


@oidc_bp.route("/callback")
def callback() -> Response | tuple[str, int]:
    next_url = session.pop("oidc_next", None)
    if "error" in request.args and current_user.is_authenticated:
        # E.g. the person cancelled an account action in Keycloak.
        flash("Přihlášení nebo akce v účtu nebyla dokončena.", "warning")
        return redirect(safe_next(next_url))
    try:
        # Keycloak issues the ID token under the host the browser used.
        token = oauth.keycloak.authorize_access_token(
            claims_options={
                "iss": {"essential": True, "value": _public_realm_url()},
                "aud": {"essential": True, "value": current_app.config["OIDC_CLIENT_ID"]},
            }
        )
    except OAuthError, JoseError, requests.RequestException:
        # An error from Keycloak, a stale or replayed callback (Back, reload,
        # expired session), a token that fails validation, or Keycloak unreachable.
        current_app.logger.warning("OIDC callback failed", exc_info=True)
        return render_template("auth/login_refused.html", failed=True), 400
    claims = token["userinfo"]
    member_id = _uuid_or_none(claims.get("crc_member_id"))
    synced = bool(member_id) and directory_sync.enabled()
    if synced:
        _sync_person(member_id)
    user = db.session.get(UserAccount, member_id) if member_id else None
    wanted = set(claims.get("medcover_roles") or [])
    roles = [r for r in db.session.scalars(db.select(Role)) if r.slug in wanted]
    if synced and user is not None and roles and not user.is_active and not user.is_archived:
        # Only the status stands in the way: activate the person if they are
        # still invited (their first login), and copy them again.
        _sync_person(member_id, activate=True)
        db.session.refresh(user)
    if user is None or user.is_archived or not user.is_active or not roles:
        current_app.logger.info("OIDC login refused for crc_member_id=%s", claims.get("crc_member_id"))
        # A refused re-login (e.g. from an account action) must not leave the
        # person logged in with roles they no longer have.
        logout_user()
        session.clear()
        if user is not None:
            _end_sessions(UserAccount.id == user.id)
        return render_template("auth/login_refused.html", logout_url=end_session_url(token.get("id_token"))), 403
    before = sorted(r.name for r in user.roles)
    after = sorted(r.name for r in roles)
    if before != after:
        user.roles = roles
        user.version += 1
        db.session.add(
            AuditLogEntry(
                actor_id=user.id,
                action_type="edit",
                entity_type="UserAccount",
                entity_id=str(user.id),
                summary=f"Role uživatele {user.name} převzaty při přihlášení z Evidence členů",
                changes_json=diff_changes({"roles": before}, {"roles": after}),
            )
        )
    user.oidc_sub = claims["sub"]
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
    session.clear()
    session.permanent = True
    login_user(user)
    session["oidc_id_token"] = token.get("id_token", "")
    return redirect(safe_next(next_url))


def _sync_person(member_id: UUID, activate: bool = False) -> None:
    """Copy the person logging in from the directory, first activating them if
    ``activate`` and they are invited. On failure they log in with the last
    copy; the scheduler catches up."""
    try:
        if activate:
            directory_sync.activate_invited(member_id)
        directory_sync.sync(member_id)
    except ldap.LDAPError, SQLAlchemyError:
        db.session.rollback()
        current_app.logger.warning("Directory sync at login failed for %s", member_id, exc_info=True)


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


@oidc_bp.route("/account/<action>")
@login_required
def account_action(action: str) -> Response:
    """Start a Keycloak account action (password, authenticator app, passkey)
    and come back to the profile."""
    if action not in KC_ACTIONS:
        abort(404)
    return login_redirect(url_for("users.profile"), kc_action=action)


@oidc_bp.route("/backchannel-logout", methods=["POST"])
@csrf.exempt
def backchannel_logout() -> tuple[str, int]:
    """Keycloak ended a session of this person, or disabled them: end all
    their MedCover sessions by moving their session epoch on."""
    realm_suffix = f"/realms/{current_app.config['KEYCLOAK_REALM']}"
    logout_token = request.form.get("logout_token", "")
    try:
        claims = jwt.decode(logout_token, _signing_keys(logout_token), algorithms=["RS256"]).claims
        jwt.JWTClaimsRegistry(
            leeway=60,  # Keycloak and MedCover clocks are never exactly in step.
            aud={"essential": True, "value": current_app.config["OIDC_CLIENT_ID"]},
            sub={"essential": True},
            events={"essential": True},
        ).validate(claims)
    except JoseError, ValueError, requests.RequestException:
        return "", 400
    # Keycloak names itself by the host each request came in on, so only the
    # realm part of the issuer is fixed; the signature already ties the token
    # to this realm's keys.
    if (
        not str(claims.get("iss", "")).endswith(realm_suffix)
        or BACKCHANNEL_LOGOUT_EVENT not in claims["events"]
        or "nonce" in claims
    ):
        return "", 400
    # ponytail: ends every MedCover session of the person, not just the one
    # Keycloak named (sid), and a replayed token (same jti) only logs them out
    # again; per-session revocation and jti tracking need a session table.
    _end_sessions(UserAccount.oidc_sub == claims["sub"])
    return "", 200


def _end_sessions(which: ColumnElement[bool]) -> None:
    """Move the session epoch on for the matching users (atomic, no lost update)."""
    db.session.execute(db.update(UserAccount).where(which).values(session_epoch=UserAccount.session_epoch + 1))
    db.session.commit()


def _signing_keys(token: str) -> KeySet:
    """Keycloak's keys, fetched again only for a key id not seen before (rotation)."""
    global _jwks_refreshed_at  # pylint: disable=global-statement

    kid = jws.extract_compact(token.encode()).headers().get("kid")
    jwks = oauth.keycloak.fetch_jwk_set()
    known = kid in {key.get("kid") for key in jwks["keys"]}
    if not known and time.monotonic() - _jwks_refreshed_at > JWKS_REFRESH_SECONDS:
        _jwks_refreshed_at = time.monotonic()
        jwks = oauth.keycloak.fetch_jwk_set(force=True)
    return KeySet.import_key_set(jwks)
