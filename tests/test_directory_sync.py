"""Copying MedCover's users from the MemberBase directory.

The directory is not running: ``ldap.initialize`` returns a fake that answers
searches from a dict of entries, i.e. what the sync account may see.
"""

import logging
import re
import uuid
from typing import Any

import ldap
import pytest
from flask import Flask
from sqlalchemy.exc import DataError, IntegrityError

from app import directory_sync, oidc
from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.outbox import OutboxEmail
from app.models.qualification import Qualification
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _login, _make_event_with_spot, _make_user

BASE = "dc=example,dc=org"
MEDCOVER = f"ou=medcover,ou=apps,{BASE}"
UNIT = f"ou=jedna,ou=units,{BASE}"
EXTERNAL = f"ou=external,{BASE}"
ANNA = "00000000-0000-4000-8000-00000000000a"
BOB = "00000000-0000-4000-8000-00000000000b"
QUAL_A = "10000000-0000-4000-8000-00000000000a"
QUAL_B = "10000000-0000-4000-8000-00000000000b"


def _split(inner: str) -> list[str]:
    """Top-level "(...)" parts of a filter list."""
    parts, depth, start = [], 0, 0
    for i, char in enumerate(inner):
        if char == "(":
            depth, start = depth + 1, start if depth else i
        elif char == ")":
            depth -= 1
            if not depth:
                parts.append(inner[start : i + 1])
    return parts


def _matches(filterstr: str, attrs: dict[str, list[str]]) -> bool:
    inner = filterstr[1:-1]
    if inner[0] in "&|":
        results = [_matches(part, attrs) for part in _split(inner[1:])]
        return all(results) if inner[0] == "&" else any(results)
    name, value = inner.split("=", 1)
    return value in attrs.get(name, [])


class FakeDirectory:
    def __init__(self, entries: dict[str, dict[str, list[str]]]) -> None:
        self.entries = entries
        self.options: dict[int, Any] = {}
        self.bound: tuple[str, str] | None = None
        self.unbound = False
        self.searches: list[tuple[str, str]] = []
        self.fail: Exception | None = None
        self.modified: list[tuple[str, list]] = []
        self.modify_error: Exception | None = None

    def modify_s(self, dn: str, mods: list) -> None:
        """Apply modifications all or nothing, like slapd; ``modified`` lists
        only what was applied."""
        if self.modify_error is not None:
            raise self.modify_error
        entry = self.entries[dn]
        changed = {k: list(v) for k, v in entry.items()}
        for op, attr, values in mods:
            decoded = [v.decode() for v in values]
            if op == ldap.MOD_DELETE:
                if not set(decoded) <= set(changed.get(attr, [])):
                    raise ldap.NO_SUCH_ATTRIBUTE
                changed[attr] = [v for v in changed[attr] if v not in decoded]
            elif op == ldap.MOD_ADD:
                changed[attr] = changed.get(attr, []) + decoded
            else:
                changed[attr] = decoded
        entry.clear()
        entry.update(changed)
        self.modified.append((dn, mods))

    def set_option(self, option: int, value: Any) -> None:
        self.options[option] = value

    def simple_bind_s(self, who: str, cred: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.bound = (who, cred)

    def unbind_s(self) -> None:
        self.unbound = True

    def search_s(self, base: str, scope: int, filterstr: str, attrs: list[str]) -> list[tuple[Any, Any]]:
        self.searches.append((base, filterstr))
        if base != BASE and base not in self.entries:
            raise ldap.NO_SUCH_OBJECT
        found: list[tuple[Any, Any]] = []
        for dn, entry in self.entries.items():
            if scope == ldap.SCOPE_BASE:
                inside = dn == base
            elif scope == ldap.SCOPE_ONELEVEL:
                inside = dn.split(",", 1)[-1] == base
            else:
                inside = dn.endswith("," + base)
            if inside and _matches(filterstr, entry):
                found.append((dn, {k: [v.encode() for v in entry[k]] for k in attrs if k in entry}))
        return found


def _person(member_id: str, name: str, email: str, status: str = "active", **extra: list[str]) -> dict[str, list]:
    return {
        "objectClass": ["inetOrgPerson", "crcMember"],
        "crcMemberId": [member_id],
        "cn": [name],
        "mail": [email],
        "crcMemberStatus": [status],
        "crcMemberKind": ["member"],
        **extra,
    }


def _directory(**people: dict[str, list]) -> dict[str, dict[str, list[str]]]:
    """A directory with one Místní skupina, the external users, two
    qualifications and the MedCover roles; ``people`` maps DN → entry."""
    return {
        f"ou=qualifications,{MEDCOVER}": {"objectClass": ["organizationalUnit"]},
        f"ou=roles,{MEDCOVER}": {"objectClass": ["organizationalUnit"]},
        UNIT: {"objectClass": ["organizationalUnit", "crcUnit"], "crcUnitId": ["unit-1"], "displayName": ["MS Jedna"]},
        EXTERNAL: {"objectClass": ["organizationalUnit", "crcUnit"], "crcUnitId": ["external"]},
        f"crcQualificationId={QUAL_A},ou=qualifications,{MEDCOVER}": {
            "objectClass": ["crcQualification", "crcMedCoverQualification"],
            "crcQualificationId": [QUAL_A],
            "cn": ["Lékař"],
            "crcCanBeRp": ["TRUE"],
        },
        f"crcQualificationId={QUAL_B},ou=qualifications,{MEDCOVER}": {
            "objectClass": ["crcQualification", "crcMedCoverQualification"],
            "crcQualificationId": [QUAL_B],
            "cn": ["Zdravotník"],
            "description": ["Kurz první pomoci"],
            "crcParent": [QUAL_A, "unknown-qualification"],
            "crcCanBeRp": ["FALSE"],
        },
        f"cn=member,ou=roles,{MEDCOVER}": {
            "objectClass": ["crcGroup"],
            "cn": ["member"],
            "member": [dn.upper() for dn in people],  # DNs compare without case
        },
        f"cn=coordinator,ou=roles,{MEDCOVER}": {"objectClass": ["crcGroup"], "cn": ["coordinator"]},
        **people,
    }


@pytest.fixture
def directory(app: Flask, monkeypatch: pytest.MonkeyPatch) -> FakeDirectory:
    fake = FakeDirectory({})
    monkeypatch.setitem(app.config, "AUTH_MODE", "oidc")
    monkeypatch.setitem(app.config, "LDAP_URI", "ldaps://directory")
    monkeypatch.setitem(app.config, "LDAP_CA_CERT", "/certs/ca.crt")
    monkeypatch.setitem(app.config, "LDAP_BASE_DN", BASE)
    monkeypatch.setitem(app.config, "LDAP_SYNC_PASSWORD", "sync-pw")
    monkeypatch.setattr(ldap, "initialize", lambda uri: fake)
    return fake


def _local_user(member_id: str, email: str, name: str = "Stará Anna", role: str = Role.MEMBER) -> None:
    user = UserAccount(id=uuid.UUID(member_id), email=email, name=name, is_active=True, password_hash="x")
    user.roles = [db.session.scalars(db.select(Role).where(Role.name == role)).one()]
    db.session.add(user)
    db.session.commit()


def _audit() -> list[tuple[str, str]]:
    return [(e.action_type, e.summary) for e in db.session.scalars(db.select(AuditLogEntry).order_by(AuditLogEntry.id))]


@pytest.mark.parametrize(
    "config, expected",
    [({"AUTH_MODE": "oidc"}, True), ({"AUTH_MODE": "local"}, False), ({"AUTH_MODE": "oidc", "LDAP_URI": ""}, False)],
)
def test_enabled_needs_oidc_and_a_directory(app: Flask, directory: FakeDirectory, config: dict, expected: bool) -> None:
    app.config.update(config)
    with app.app_context():
        assert directory_sync.enabled() is expected


def test_connects_over_ldaps_as_the_sync_account(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Nová Anna", "anna@example.org")})
    with app.app_context():
        directory_sync.sync()
    assert directory.bound == (f"cn=medcover-sync,ou=services,{BASE}", "sync-pw")
    assert directory.options[ldap.OPT_X_TLS_CACERTFILE] == "/certs/ca.crt"
    assert directory.options[ldap.OPT_X_TLS_REQUIRE_CERT] == ldap.OPT_X_TLS_DEMAND
    assert directory.unbound


def test_without_ca_cert_uses_the_system_store(app: Flask, directory: FakeDirectory) -> None:
    app.config["LDAP_CA_CERT"] = ""
    with app.app_context():
        directory_sync.sync()
    assert ldap.OPT_X_TLS_CACERTFILE not in directory.options


def test_creates_people_with_unit_roles_and_qualifications(app: Flask, directory: FakeDirectory) -> None:
    anna_dn, bob_dn = f"uid={ANNA},{UNIT}", f"uid={BOB},{EXTERNAL}"
    directory.entries = _directory(
        **{
            anna_dn: _person(ANNA, "Nová Anna", "anna@example.org", telephoneNumber=["+420 111 222 333"]),
            bob_dn: _person(BOB, "Externí Bob", "bob@example.org", crcMemberKind=["external"]),
            f"crcHoldingId=h1,{anna_dn}": {"objectClass": ["crcHolding"], "crcQualificationRef": [QUAL_B]},
            f"crcHoldingId=h2,{anna_dn}": {"objectClass": ["crcHolding"], "crcQualificationRef": ["unknown"]},
        }
    )
    directory.entries[f"cn=coordinator,ou=roles,{MEDCOVER}"]["member"] = [anna_dn]

    with app.app_context():
        directory_sync.sync()

        anna = db.session.get(UserAccount, uuid.UUID(ANNA))
        assert (anna.name, anna.email, anna.phone) == ("Nová Anna", "anna@example.org", "+420 111 222 333")
        assert anna.is_active and not anna.is_archived
        assert (anna.crc_unit_id, anna.unit_name, anna.kind) == ("unit-1", "MS Jedna", "member")
        assert sorted(r.name for r in anna.roles) == [Role.COORDINATOR, Role.MEMBER]
        assert [q.name for q in anna.qualifications] == ["Zdravotník"]
        assert not anna.check_password("") and not anna.check_password("!")
        bob = db.session.get(UserAccount, uuid.UUID(BOB))
        assert (bob.crc_unit_id, bob.unit_name, bob.kind, bob.phone) == ("external", None, "external", None)

        quals = {q.name: q for q in db.session.scalars(db.select(Qualification))}
        assert quals["Lékař"].can_be_rp and quals["Lékař"].crc_qualification_id == QUAL_A
        assert quals["Zdravotník"].description == "Kurz první pomoci"
        assert quals["Zdravotník"].parents == [quals["Lékař"]]
        assert _audit() == [
            ("create", "Kvalifikace „Lékař“ převzata z Evidence členů"),
            ("create", "Kvalifikace „Zdravotník“ převzata z Evidence členů"),
            ("create", "Uživatel Nová Anna převzat z Evidence členů"),
            ("create", "Uživatel Externí Bob převzat z Evidence členů"),
        ]


def test_updates_what_the_directory_owns_and_keeps_the_rest(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Nová Anna", "Anna@Example.org")})
    with app.app_context():
        _local_user(ANNA, "anna@example.org", role=Role.ADMIN)
        user = db.session.get(UserAccount, uuid.UUID(ANNA))
        user.dark_mode, user.phone = True, "+420 999"
        db.session.commit()

        directory_sync.sync()

        user = db.session.get(UserAccount, uuid.UUID(ANNA))
        assert (user.name, user.email, user.phone, user.dark_mode) == ("Nová Anna", "Anna@Example.org", None, True)
        assert [r.name for r in user.roles] == [Role.MEMBER]
        assert user.version == 2 and user.session_epoch == 0
        entry = db.session.scalars(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "UserAccount")).one()
        assert entry.summary == "Uživatel Nová Anna aktualizován z Evidence členů" and entry.actor_id is None
        assert entry.changes_json["roles"] == [[Role.ADMIN], [Role.MEMBER]]
        assert entry.changes_json["name"] == ["Stará Anna", "Nová Anna"]

        # A second run finds nothing to change.
        directory_sync.sync()
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).version == 2
        assert len(_audit()) == 3


@pytest.mark.parametrize("status, archived", [("inactive", False), ("former", True)])
def test_inactive_or_archived_people_cannot_log_in(
    app: Flask, directory: FakeDirectory, status: str, archived: bool
) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org", status)})
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
        directory_sync.sync()
        user = db.session.get(UserAccount, uuid.UUID(ANNA))
        assert not user.is_active and user.is_archived is archived
        assert user.session_epoch == 1  # their sessions end


def test_revoked_people_are_archived_but_stay_on_their_events(
    app: Flask, directory: FakeDirectory, client: Any
) -> None:
    """Their last MedCover role was removed (or they were archived in MemberBase),
    so the sync may no longer read them."""
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    event_id, spot_id = _make_event_with_spot(app, EventStatus.COMPLETED, name="Závod 2026")
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
        _local_user(BOB, "bob@example.org", "Odebraný Bob")
        _local_user(str(uuid.uuid4()), "gone@example.org", "Gone")
        gone = db.session.scalars(db.select(UserAccount).where(UserAccount.email == "gone@example.org")).one()
        gone.is_active, gone.is_archived, gone.roles = False, True, []
        db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=uuid.UUID(BOB)))
        db.session.add(OutboxEmail(to_email="bob@example.org", subject="S", body="B", user_id=uuid.UUID(BOB)))
        db.session.commit()

        directory_sync.sync()

        bob = db.session.get(UserAccount, uuid.UUID(BOB))
        assert not bob.is_active and bob.is_archived and bob.roles == [] and bob.session_epoch == 1
        assert db.session.scalars(db.select(OutboxEmail)).all() == []
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active
        summaries = [s for _, s in _audit()]
        assert "Uživatel Odebraný Bob aktualizován z Evidence členů" in summaries
        assert not any("Gone" in s for s in summaries)  # already archived
        _make_user("viewer@test.com", "Viewer", Role.VIEWER)
    app.config["AUTH_MODE"] = "local"  # log in with the test helper's password
    _login(client, "viewer@test.com")
    assert "Odebraný Bob" in client.get(f"/events/{event_id}").text
    assert "Odebraný Bob" not in client.get("/users/").text

    # Access granted again: back in MedCover.
    directory.entries = _directory(
        **{
            f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org"),
            f"uid={BOB},{UNIT}": _person(BOB, "Odebraný Bob", "bob@example.org"),
        }
    )
    with app.app_context():
        directory_sync.sync()
        bob = db.session.get(UserAccount, uuid.UUID(BOB))
        assert bob.is_active and not bob.is_archived and [r.name for r in bob.roles] == [Role.MEMBER]


def test_an_empty_answer_deactivates_nobody(
    app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture
) -> None:
    directory.entries = _directory()
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
        with caplog.at_level(logging.ERROR):
            directory_sync.sync()
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active
        assert db.session.scalars(db.select(Qualification)).all() == []
    assert "returned nobody" in caplog.text


def test_skips_people_it_cannot_copy(app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture) -> None:
    no_mail = _person(BOB, "Bez Mailu", "")
    no_mail["mail"] = []
    directory.entries = _directory(
        **{
            f"uid=bad,{UNIT}": _person("not-a-uuid", "Bad", "bad@example.org"),
            f"uid={BOB},{UNIT}": no_mail,
            f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "taken@example.org"),
        }
    )
    with app.app_context():
        _local_user(str(uuid.uuid4()), "taken@example.org", "Someone Else")
        with caplog.at_level(logging.WARNING):
            directory_sync.sync()
        assert db.session.get(UserAccount, uuid.UUID(ANNA)) is None
        assert db.session.get(UserAccount, uuid.UUID(BOB)) is None
    assert "no valid crcMemberId" in caplog.text and "another MedCover account" in caplog.text


def test_links_existing_qualifications(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    with app.app_context():
        # Exported earlier (same crcQualificationId), renamed since in MemberBase.
        db.session.add(Qualification(name="Doktor", crc_qualification_id=QUAL_A, can_be_rp=True))
        # Created in MedCover after the export: linked by name.
        db.session.add(Qualification(name="zdravotník", description="Kurz první pomoci"))
        db.session.commit()

        directory_sync.sync()

        quals = {q.crc_qualification_id: q for q in db.session.scalars(db.select(Qualification))}
        assert len(quals) == 2
        assert quals[QUAL_A].name == "Lékař"
        assert quals[QUAL_B].name == "Zdravotník" and quals[QUAL_B].parents == [quals[QUAL_A]]
        assert [s for a, s in _audit() if a != "create"] == [
            "Kvalifikace „Lékař“ upravena z Evidence členů",
            "Kvalifikace „Zdravotník“ upravena z Evidence členů",  # linked to the directory
            "Kvalifikace „Zdravotník“ upravena z Evidence členů",  # its parent
        ]


def test_one_person_is_synced_without_touching_others(app: Flask, directory: FakeDirectory) -> None:
    anna_dn = f"uid={ANNA},{UNIT}"
    directory.entries = _directory(
        **{
            anna_dn: _person(ANNA, "Anna", "anna@example.org"),
            f"crcHoldingId=h1,{anna_dn}": {"objectClass": ["crcHolding"], "crcQualificationRef": [QUAL_A]},
        }
    )
    with app.app_context():
        _local_user(BOB, "bob@example.org", "Bob")
        db.session.add(Qualification(name="Doktor", crc_qualification_id=QUAL_A))
        db.session.commit()
        directory_sync.sync(uuid.UUID(ANNA))
        assert [q.name for q in db.session.get(UserAccount, uuid.UUID(ANNA)).qualifications] == ["Doktor"]
        assert db.session.get(UserAccount, uuid.UUID(BOB)).is_active
        # Definitions are left to the scheduler: not renamed, Zdravotník not created.
        assert [q.name for q in db.session.scalars(db.select(Qualification))] == ["Doktor"]
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).unit_name == "MS Jedna"
    assert (anna_dn, "(objectClass=crcHolding)") in directory.searches
    assert (BASE, f"(&(objectClass=crcMember)(crcMemberId={ANNA}))") in directory.searches
    assert not any("crcQualification)" in f for _, f in directory.searches)


def test_one_person_the_directory_does_not_show_is_archived(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory()
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
        _local_user(BOB, "bob@example.org", "Bob")
        directory_sync.sync(uuid.UUID(ANNA))
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_archived
        assert db.session.get(UserAccount, uuid.UUID(BOB)).is_active
    assert not any(f == "(objectClass=crcHolding)" for _, f in directory.searches)


def test_a_part_the_sync_may_not_see_is_empty(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    del directory.entries[f"ou=qualifications,{MEDCOVER}"]
    with app.app_context():
        directory_sync.sync()
        assert db.session.scalars(db.select(Qualification)).all() == []
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active


def test_referrals_are_ignored() -> None:
    class Referring:
        def search_s(self, *args: Any) -> list:
            return [(None, ["ldaps://elsewhere/"])]

    assert directory_sync._search(Referring(), BASE, "(objectClass=*)", []) == {}


# ── Login ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    def authorize_access_token(**kwargs: Any) -> dict[str, Any]:
        return {"id_token": "t", "userinfo": {"sub": "kc-1", "crc_member_id": ANNA, "medcover_roles": ["member"]}}

    monkeypatch.setattr(oidc.oauth.keycloak, "authorize_access_token", authorize_access_token)


def test_login_copies_a_person_new_to_medcover(app: Flask, directory: FakeDirectory, client: Any, token: None) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    resp = client.get("/auth/callback?code=c&state=s")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess["_user_id"] == ANNA


@pytest.mark.parametrize(
    "error", [ldap.SERVER_DOWN(), IntegrityError("insert", {}, Exception("duplicate")), DataError("x", {}, Exception())]
)
def test_login_uses_the_last_copy_when_the_sync_fails(
    app: Flask, directory: FakeDirectory, client: Any, token: None, error: Exception
) -> None:
    directory.fail = error
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
    resp = client.get("/auth/callback?code=c&state=s")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess["_user_id"] == ANNA


# ── Guards and robustness ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "damage, message",
    [
        ("no roles", "no MedCover roles"),
        ("no role members", "nobody holding a MedCover role"),
        ("no qualifications", "holdings but no qualifications"),
    ],
)
def test_a_damaged_answer_changes_nothing(
    app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture, damage: str, message: str
) -> None:
    anna_dn = f"uid={ANNA},{UNIT}"
    directory.entries = _directory(
        **{
            anna_dn: _person(ANNA, "Anna", "anna@example.org"),
            f"crcHoldingId=h1,{anna_dn}": {"objectClass": ["crcHolding"], "crcQualificationRef": [QUAL_A]},
        }
    )
    if damage == "no roles":
        directory.entries = {dn: e for dn, e in directory.entries.items() if not dn.startswith("cn=")}
    elif damage == "no role members":
        directory.entries[f"cn=member,ou=roles,{MEDCOVER}"]["member"] = []
    else:
        del directory.entries[f"ou=qualifications,{MEDCOVER}"]
    with app.app_context():
        _local_user(BOB, "bob@example.org", "Bob")
        with caplog.at_level(logging.ERROR):
            directory_sync.sync()
        assert db.session.get(UserAccount, uuid.UUID(BOB)).is_active
        assert db.session.get(UserAccount, uuid.UUID(ANNA)) is None
    assert message in caplog.text


def test_no_roles_at_login_changes_nothing(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = {dn: e for dn, e in _directory().items() if not dn.startswith("cn=")}
    with app.app_context():
        _local_user(ANNA, "anna@example.org")
        directory_sync.sync(uuid.UUID(ANNA))
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active


def test_qualification_fixes_undelete_link_and_parents(app: Flask, directory: FakeDirectory) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    with app.app_context():
        lekar = Qualification(name="Lékař", crc_qualification_id=QUAL_A, can_be_rp=True)
        lekar.soft_delete()
        # An ID the directory does not know (e.g. given by the migration to a
        # qualification created after the export): linked by name.
        orphan = Qualification(name="Zdravotník", description="Kurz první pomoci", crc_qualification_id="orphan")
        db.session.add_all([lekar, orphan])
        db.session.commit()

        directory_sync.sync()

        quals = {q.crc_qualification_id: q for q in db.session.scalars(db.select(Qualification))}
        assert set(quals) == {QUAL_A, QUAL_B}
        assert not quals[QUAL_A].is_deleted and quals[QUAL_A].deleted_at is None
        assert quals[QUAL_B].parents == [quals[QUAL_A]]
        changes = [
            e.changes_json
            for e in db.session.scalars(
                db.select(AuditLogEntry).where(AuditLogEntry.entity_id == str(quals[QUAL_B].id))
            )
        ]
        assert {"crc_qualification_id": ["orphan", QUAL_B]} in changes and {"parents": [[], ["Lékař"]]} in changes


def test_a_qualification_that_cannot_be_written_is_skipped(
    app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture
) -> None:
    """Another MedCover qualification (linked to a different directory ID) still has the name."""
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    directory.entries[f"crcQualificationId={QUAL_B},ou=qualifications,{MEDCOVER}"]["cn"] = ["Lékař"]
    with app.app_context():
        with caplog.at_level(logging.ERROR):
            directory_sync.sync()
        assert [q.crc_qualification_id for q in db.session.scalars(db.select(Qualification))] == [QUAL_A]
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active
    assert f"qualification {QUAL_B} could not be copied" in caplog.text


def test_a_person_that_cannot_be_written_is_skipped(
    app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture
) -> None:
    directory.entries = _directory(
        **{
            f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org", telephoneNumber=["1" * 80]),
            f"uid={BOB},{UNIT}": _person(BOB, "Bob", "bob@example.org"),
        }
    )
    with app.app_context():
        with caplog.at_level(logging.ERROR):
            directory_sync.sync()
        assert db.session.get(UserAccount, uuid.UUID(ANNA)) is None
        assert db.session.get(UserAccount, uuid.UUID(BOB)).is_active
    assert f"{ANNA} could not be copied" in caplog.text


# ── MedCover's own user administration while the sync is on ─────────────────


def test_directory_owned_admin_is_off_while_synced(app: Flask, directory: FakeDirectory, client: Any) -> None:
    with app.app_context():
        _make_user("admin@test.com", "Admin", Role.ADMIN)
        _local_user(BOB, "bob@example.org", "Bob")
    app.config["AUTH_MODE"] = "local"
    _login(client, "admin@test.com")
    app.config["AUTH_MODE"] = "oidc"
    with client.session_transaction() as sess:
        sess["auth_mode"] = "oidc"
    page = client.get(f"/users/{BOB}").text
    assert "spravují v Evidenci členů" in page and "Deaktivovat" not in page and "Uložit uživatele" not in page
    assert 'name="user_ids"' not in client.get("/users/").text
    for path in ("save", "activate", "deactivate", "archive", "unarchive"):
        assert client.post(f"/users/{BOB}/{path}").status_code == 403
    assert client.post("/users/batch").status_code == 403
    assert client.get("/qualifications/create").status_code == 403
    assert client.get("/import/events/").status_code == 404
    resp = client.post("/users/profile", data={"action": "profile", "name": "Jiný", "phone": "bad", "dark_mode": "1"})
    assert resp.status_code == 302
    with app.app_context():
        admin = db.session.scalars(db.select(UserAccount).where(UserAccount.email == "admin@test.com")).one()
        assert admin.name == "Admin" and admin.phone is None and admin.dark_mode
    assert 'id="phone" disabled' in client.get("/users/profile").text


def test_a_parent_that_would_close_a_cycle_is_kept_out(
    app: Flask, directory: FakeDirectory, caplog: pytest.LogCaptureFixture
) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    directory.entries[f"crcQualificationId={QUAL_A},ou=qualifications,{MEDCOVER}"]["crcParent"] = [QUAL_B]
    with app.app_context():
        with caplog.at_level(logging.ERROR):
            directory_sync.sync()
        quals = {q.crc_qualification_id: q for q in db.session.scalars(db.select(Qualification))}
        # One of the two edges is kept, the one closing the cycle is not.
        assert len(quals[QUAL_A].parents) + len(quals[QUAL_B].parents) == 1
    assert "would close a cycle" in caplog.text


def test_a_live_copy_takes_the_id_from_a_deleted_one(app: Flask, directory: FakeDirectory) -> None:
    """Exported, deleted in MedCover and created again under the same name."""
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    with app.app_context():
        old = Qualification(name="Lékař", crc_qualification_id=QUAL_A)
        old.soft_delete()
        live = Qualification(name="Lékař", crc_qualification_id="given-by-the-migration")
        db.session.add_all([old, live])
        db.session.commit()
        old_id, live_id = old.id, live.id

        directory_sync.sync()

        assert db.session.get(Qualification, live_id).crc_qualification_id == QUAL_A
        assert db.session.get(Qualification, old_id).crc_qualification_id is None
        assert db.session.get(Qualification, old_id).is_deleted


def test_responsible_person_follows_qualification_changes(app: Flask, directory: FakeDirectory) -> None:
    anna_dn = f"uid={ANNA},{UNIT}"
    directory.entries = _directory(
        **{
            anna_dn: _person(ANNA, "Anna", "anna@example.org"),
            f"crcHoldingId=h1,{anna_dn}": {"objectClass": ["crcHolding"], "crcQualificationRef": [QUAL_A]},
        }
    )
    event_id, spot_id = _make_event_with_spot(app, EventStatus.ASSIGNMENTS_OPEN)
    with app.app_context():
        directory_sync.sync()
        db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=uuid.UUID(ANNA)))
        db.session.get(Event, event_id).responsible_person_id = uuid.UUID(ANNA)
        db.session.commit()

        # Anna loses the only qualification that lets her be responsible.
        del directory.entries[f"crcHoldingId=h1,{anna_dn}"]
        directory_sync.sync()

        assert db.session.get(Event, event_id).responsible_person_id is None
        entry = db.session.scalars(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "Event")).one()
        assert entry.actor_id is None


def test_a_failed_bind_closes_the_connection(app: Flask, directory: FakeDirectory) -> None:
    directory.fail = ldap.INVALID_CREDENTIALS()
    with app.app_context(), pytest.raises(ldap.INVALID_CREDENTIALS):
        directory_sync.sync()
    assert directory.unbound


# ── Activation at the first MedCover login ───────────────────────────────────


def _invited_anna(directory: FakeDirectory) -> str:
    anna_dn = f"uid={ANNA},{UNIT}"
    directory.entries = _directory(**{anna_dn: _person(ANNA, "Anna", "anna@example.org", "invited")})
    return anna_dn


def _logged_in_as(client: Any) -> str | None:
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def test_login_activates_an_invited_person(app: Flask, directory: FakeDirectory, client: Any, token: None) -> None:
    anna_dn = _invited_anna(directory)
    assert client.get("/auth/callback?code=c&state=s").status_code == 302
    assert _logged_in_as(client) == ANNA
    assert directory.entries[anna_dn]["crcMemberStatus"] == ["active"]
    ((dn, mods),) = directory.modified
    assert dn == anna_dn and mods[:2] == [
        (ldap.MOD_DELETE, "crcMemberStatus", [b"invited"]),
        (ldap.MOD_ADD, "crcMemberStatus", [b"active"]),
    ]
    op, attr, (stamp,) = mods[2]
    assert (op, attr) == (ldap.MOD_REPLACE, "crcStatusChangedAt") and re.fullmatch(rb"\d{14}Z", stamp)
    with app.app_context():
        assert db.session.get(UserAccount, uuid.UUID(ANNA)).is_active


@pytest.mark.parametrize(
    "error",
    [ldap.INSUFFICIENT_ACCESS(), ldap.NO_SUCH_OBJECT()],  # access rules predating the activation; moved meanwhile
)
def test_login_of_an_invited_person_the_directory_does_not_activate(
    app: Flask, directory: FakeDirectory, client: Any, token: None, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    anna_dn = _invited_anna(directory)
    directory.modify_error = error
    with caplog.at_level(logging.WARNING):
        assert client.get("/auth/callback?code=c&state=s").status_code == 403
    assert "could not be activated" in caplog.text and "Directory sync at login failed" not in caplog.text
    assert directory.entries[anna_dn]["crcMemberStatus"] == ["invited"]
    with app.app_context():
        assert not db.session.get(UserAccount, uuid.UUID(ANNA)).is_active


@pytest.mark.parametrize("meanwhile, status_code", [("inactive", 403), ("active", 302)])
def test_login_of_a_person_whose_status_changed_meanwhile(
    app: Flask, directory: FakeDirectory, client: Any, token: None, meanwhile: str, status_code: int
) -> None:
    """MemberBase deactivated them, or activated them at their first MemberBase
    login, between the activation's search and its write."""
    anna_dn = _invited_anna(directory)
    search = directory.search_s

    def search_then_change(*args: Any) -> list:
        found = search(*args)
        if "(crcMemberStatus=invited)" in args[2]:
            directory.entries[anna_dn]["crcMemberStatus"] = [meanwhile]
        return found

    directory.search_s = search_then_change  # type: ignore[method-assign]
    assert client.get("/auth/callback?code=c&state=s").status_code == status_code
    assert directory.entries[anna_dn]["crcMemberStatus"] == [meanwhile]
    assert directory.modified == []


def test_other_directory_errors_during_activation_fall_back(
    app: Flask, directory: FakeDirectory, client: Any, token: None, caplog: pytest.LogCaptureFixture
) -> None:
    _invited_anna(directory)
    directory.modify_error = ldap.SERVER_DOWN()
    with caplog.at_level(logging.WARNING):
        assert client.get("/auth/callback?code=c&state=s").status_code == 403
    assert "Directory sync at login failed" in caplog.text


def test_login_of_an_active_person_writes_nothing(
    app: Flask, directory: FakeDirectory, client: Any, token: None
) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org")})
    assert client.get("/auth/callback?code=c&state=s").status_code == 302
    assert directory.modified == []


@pytest.mark.parametrize("status", ["inactive", "new"])
def test_login_of_a_person_not_invited_activates_nothing(
    app: Flask, directory: FakeDirectory, client: Any, token: None, status: str
) -> None:
    directory.entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org", status)})
    assert client.get("/auth/callback?code=c&state=s").status_code == 403
    assert directory.modified == []


def test_a_damaged_answer_at_login_activates_nobody(
    app: Flask, directory: FakeDirectory, client: Any, token: None
) -> None:
    entries = _directory(**{f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org", "invited")})
    directory.entries = {dn: e for dn, e in entries.items() if not dn.startswith("cn=")}
    assert client.get("/auth/callback?code=c&state=s").status_code == 403
    assert directory.modified == []


def test_the_scheduler_does_not_activate_invited_people(app: Flask, directory: FakeDirectory) -> None:
    """Only a login (Keycloak accepted the person) activates."""
    directory.entries = _directory(
        **{
            f"uid={ANNA},{UNIT}": _person(ANNA, "Anna", "anna@example.org", "invited"),
            f"uid={BOB},{UNIT}": _person(BOB, "Bob", "bob@example.org"),
        }
    )
    with app.app_context():
        directory_sync.sync()
        assert not db.session.get(UserAccount, uuid.UUID(ANNA)).is_active
    assert directory.modified == []
