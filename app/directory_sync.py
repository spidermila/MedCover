"""Copies MedCover's users from the MemberBase directory.

The directory is the system of record for people, their Místní skupina,
status, MedCover roles and qualifications. MedCover reads it with its own
account, ``cn=medcover-sync``, whose access rules show only people holding a
MedCover role and only the attributes MedCover needs; its one write is
activating an invited person at their first MedCover login. The scheduler
syncs everyone every 15 minutes; each login first syncs the person logging in.

Holding a MedCover role is what gives a person access to MedCover. What
the directory owns is overwritten here and never edited in MedCover;
preferences, signature and iCal tokens stay local. People who drop out of
the directory's answer (access revoked, or archived in MemberBase) are
archived here: hidden from lists and pickers, sessions ended, pending emails
dropped, but their row stays so events keep showing their name. Access
granted again brings them back. When the directory returns nobody at all,
nobody is archived: that is far more likely a fault than a fact.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import ldap
import sqlalchemy as sa
from flask import current_app
from ldap.filter import escape_filter_chars
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.outbox import drop_pending_emails
from app.models.qualification import Qualification
from app.models.role import Role, directory_synced
from app.models.user import UserAccount
from app.responsible_person import refresh_responsible_people
from app.staffing import qualification_graph
from app.utils import diff_changes

log = logging.getLogger(__name__)

# Directory users log in through Keycloak; no password matches this hash.
NO_PASSWORD = "!"
PERSON_ATTRS = ["crcMemberId", "cn", "mail", "telephoneNumber", "crcMemberStatus", "crcMemberKind"]
QUALIFICATION_ATTRS = ["crcQualificationId", "cn", "description", "crcParent", "crcCanBeRp"]
# Copied user columns, compared for the audit log along with roles and qualifications.
USER_FIELDS = ("name", "email", "phone", "is_active", "is_archived", "crc_unit_id", "unit_name", "kind")

Attrs = dict[str, list[str]]


def enabled() -> bool:
    return directory_synced()


def sync(member_id: uuid.UUID | None = None) -> None:
    """Copy everyone, or only the person ``member_id`` (at their login), from
    the directory and commit."""
    base = current_app.config["LDAP_BASE_DN"]
    medcover = f"ou=medcover,ou=apps,{base}"
    person_filter = "(objectClass=crcMember)"
    if member_id is not None:
        person_filter = f"(&{person_filter}(crcMemberId={escape_filter_chars(str(member_id))}))"
    conn = _connect()
    try:
        roles = _search(conn, f"ou=roles,{medcover}", "(objectClass=crcGroup)", ["cn", "member"], ldap.SCOPE_ONELEVEL)
        people = _search(conn, base, person_filter, PERSON_ATTRS)
        if member_id is None:
            quals = _search(
                conn, f"ou=qualifications,{medcover}", "(objectClass=crcQualification)", QUALIFICATION_ATTRS
            )
            units = _search(conn, base, "(objectClass=crcUnit)", ["crcUnitId", "displayName"])
            holdings = _search(conn, base, "(objectClass=crcHolding)", ["crcQualificationRef"]) if people else {}
        else:
            # At login only the person: their Místní skupina and holdings.
            # Definitions are left to the scheduler.
            quals, units, holdings = {}, {}, {}
            for dn in people:
                units = _search(
                    conn, _parent(dn), "(objectClass=crcUnit)", ["crcUnitId", "displayName"], ldap.SCOPE_BASE
                )
                holdings = _search(conn, dn, "(objectClass=crcHolding)", ["crcQualificationRef"])
    finally:
        conn.unbind_s()
    if not roles:
        log.error("Directory sync: the directory returned no MedCover roles, so nothing was changed")
        return
    if member_id is None:
        members = {m.lower() for a in roles.values() for m in a.get("member", [])}
        if not members & people.keys():
            log.error("Directory sync: the directory returned nobody holding a MedCover role, so nothing was changed")
            return
        if holdings and not quals:
            log.error("Directory sync: the directory returned holdings but no qualifications, so nothing was changed")
            return
        by_id, touched = _sync_qualifications(quals)
    else:
        by_id, touched = _linked_qualifications(), False
    if _sync_people(people, roles, units, holdings, by_id, member_id) or touched:
        # Who may be an event's responsible person may have changed.
        refresh_responsible_people()
    db.session.commit()


def _connect() -> Any:
    cfg = current_app.config
    conn = ldap.initialize(cfg["LDAP_URI"])
    conn.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
    conn.set_option(ldap.OPT_REFERRALS, 0)
    conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
    conn.set_option(ldap.OPT_TIMEOUT, 30)
    if cfg["LDAP_CA_CERT"]:
        conn.set_option(ldap.OPT_X_TLS_CACERTFILE, cfg["LDAP_CA_CERT"])
    conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
    conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
    try:
        conn.simple_bind_s(f"cn=medcover-sync,ou=services,{cfg['LDAP_BASE_DN']}", cfg["LDAP_SYNC_PASSWORD"])
    except ldap.LDAPError:
        conn.unbind_s()
        raise
    return conn


def activate_invited(member_id: uuid.UUID) -> bool:
    """First MedCover login of an invited MedCover user, whom Keycloak has just
    accepted and MedCover would let in but for the status. The directory lets
    the sync account change only "invited" into "active" (and the time of the
    change). Returns whether it did; False also when the person is not
    invited, or MemberBase changed or moved them meanwhile (the next copy then
    shows their real status). Other directory errors propagate."""
    filterstr = (
        f"(&(objectClass=crcMember)(crcMemberStatus=invited)(crcMemberId={escape_filter_chars(str(member_id))}))"
    )
    now = datetime.now(UTC).strftime("%Y%m%d%H%M%SZ")
    conn = _connect()
    try:
        for dn in _search(conn, current_app.config["LDAP_BASE_DN"], filterstr, ["crcMemberId"]):
            conn.modify_s(
                dn,
                [
                    (ldap.MOD_DELETE, "crcMemberStatus", [b"invited"]),
                    (ldap.MOD_ADD, "crcMemberStatus", [b"active"]),
                    (ldap.MOD_REPLACE, "crcStatusChangedAt", [now.encode()]),
                ],
            )
            log.info("Directory sync: %s activated at their first MedCover login", dn)
            return True
    except ldap.INSUFFICIENT_ACCESS, ldap.NO_SUCH_ATTRIBUTE, ldap.NO_SUCH_OBJECT:
        # Refused (access rules older than the activation), or no longer
        # invited or moved by MemberBase since the search.
        log.warning("Directory sync: %s could not be activated", member_id, exc_info=True)
    finally:
        conn.unbind_s()
    return False


def _search(
    conn: Any, base: str, filterstr: str, attrs: list[str], scope: int = ldap.SCOPE_SUBTREE
) -> dict[str, Attrs]:
    """Lower-case DN → attributes. A base the sync may not see gives nothing.

    ponytail: one unpaged search each; the directory's size limit (5000)
    is far above a district's people and holdings, page it if that changes.
    """
    try:
        found = conn.search_s(base, scope, filterstr, attrs)
    except ldap.NO_SUCH_OBJECT:
        return {}
    return {dn.lower(): {k: [v.decode() for v in vs] for k, vs in a.items()} for dn, a in found if dn}


def _first(attrs: Attrs, name: str) -> str | None:
    values = attrs.get(name)
    return values[0] if values else None


def _parent(dn: str) -> str:
    return dn.split(",", 1)[1]


def _audit(action: str, entity_type: str, entity_id: object, summary: str, changes: dict | None = None) -> None:
    db.session.add(
        AuditLogEntry(
            actor_id=None,
            action_type=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            summary=summary,
            changes_json=changes,
        )
    )


def _linked_qualifications() -> dict[str, Qualification]:
    return {
        q.crc_qualification_id: q
        for q in db.session.scalars(db.select(Qualification).where(Qualification.crc_qualification_id.is_not(None)))
    }


def _sync_qualifications(quals: dict[str, Attrs]) -> tuple[dict[str, Qualification], bool]:
    """Upsert the MedCover qualification definitions. Returns crcQualificationId
    → row, and whether anything that decides a responsible person changed.

    A live MedCover qualification whose ID the directory does not know (created
    after the export) is linked by name, also in place of a deleted copy that
    holds the ID. A definition that cannot be written (e.g. a name another
    MedCover qualification still has) or a parent that would close a cycle is
    skipped and logged, so the rest still sync.

    ponytail: a definition removed from the directory stays in MedCover (its
    holders lose it); deleting it here would need the spot clean-up of the
    „Kvalifikace“ delete page.
    """
    by_id = _linked_qualifications()
    created: set[str] = set()
    touched = False
    directory_ids = {_first(attrs, "crcQualificationId") for attrs in quals.values()}
    unlinked = {
        q.name.lower(): q
        for q in db.session.scalars(db.select(Qualification).where(Qualification.is_deleted == sa.false()))
        if q.crc_qualification_id not in directory_ids
    }
    for attrs in quals.values():
        qid = _first(attrs, "crcQualificationId") or ""
        name = _first(attrs, "cn") or qid
        after = {
            "crc_qualification_id": qid,
            "name": name,
            "description": _first(attrs, "description"),
            "can_be_rp": _first(attrs, "crcCanBeRp") == "TRUE",
            "is_deleted": False,  # the directory has it, so it is live
        }
        try:
            with db.session.begin_nested():
                qual = by_id.get(qid)
                if qual is not None and qual.is_deleted and name.lower() in unlinked:
                    qual.crc_qualification_id = None  # the deleted copy lets go of the ID
                    db.session.flush()
                    qual = None
                qual = qual or unlinked.pop(name.lower(), None)
                if qual is None:
                    qual = Qualification(**after)
                    db.session.add(qual)
                    db.session.flush()
                    created.add(qid)
                    _audit("create", "Qualification", qual.id, f"Kvalifikace „{name}“ převzata z Evidence členů")
                else:
                    changes = diff_changes({k: getattr(qual, k) for k in after}, after)
                    if changes:
                        for key, value in after.items():
                            setattr(qual, key, value)
                        qual.deleted_at = None
                        db.session.flush()
                        _audit(
                            "edit", "Qualification", qual.id, f"Kvalifikace „{name}“ upravena z Evidence členů", changes
                        )
                        touched = touched or bool({"can_be_rp", "is_deleted"} & changes.keys())
            by_id[qid] = qual
        except SQLAlchemyError:
            log.exception("Directory sync: qualification %s could not be copied; skipped", qid)
    for attrs in quals.values():
        qual = by_id.get(_first(attrs, "crcQualificationId") or "")
        if qual is None:
            continue
        before = sorted(p.name for p in qual.parents)
        try:
            with db.session.begin_nested():
                qual.parents = [by_id[p] for p in attrs.get("crcParent", []) if p in by_id]
                db.session.flush()
                qualification_graph()  # rejects a cycle
        except ValueError:
            log.error("Directory sync: the parents of qualification %s would close a cycle; kept", qual.name)
            continue
        now = sorted(p.name for p in qual.parents)
        if now != before and qual.crc_qualification_id not in created:
            touched = True
            _audit(
                "edit",
                "Qualification",
                qual.id,
                f"Kvalifikace „{qual.name}“ upravena z Evidence členů",
                {"parents": [before, now]},
            )
    return by_id, touched


def _snapshot(user: UserAccount) -> dict:
    return {k: getattr(user, k) for k in USER_FIELDS} | {
        "roles": sorted(r.name for r in user.roles),
        "qualifications": sorted(q.name for q in user.qualifications),
    }


# Changes that can decide who may be an event's responsible person.
RP_FIELDS = {"roles", "qualifications", "is_active", "is_archived"}


def _record(user: UserAccount, before: dict) -> bool:
    """Audit what the sync changed; people it deactivated lose their sessions
    and pending emails. True when a responsible person may have to change."""
    changes = diff_changes(before, _snapshot(user))
    if not changes:
        return False
    user.version = (user.version or 0) + 1
    if before["is_active"] and not user.is_active:
        user.end_sessions()
        drop_pending_emails(user.id)
    _audit("edit", "UserAccount", user.id, f"Uživatel {user.name} aktualizován z Evidence členů", changes)
    return bool(RP_FIELDS & changes.keys())


def _sync_people(
    people: dict[str, Attrs],
    roles: dict[str, Attrs],
    units: dict[str, Attrs],
    holdings: dict[str, Attrs],
    by_id: dict[str, Qualification],
    only: uuid.UUID | None,
) -> bool:
    """Copy the people; True when a responsible person may have to change."""
    role_members = {_first(a, "cn"): {m.lower() for m in a.get("member", [])} for a in roles.values()}
    local_roles = list(db.session.scalars(db.select(Role)))
    held: dict[str, list[Qualification]] = {}
    for dn, attrs in holdings.items():
        qual = by_id.get(_first(attrs, "crcQualificationRef") or "")
        if qual is not None:
            held.setdefault(_parent(dn), []).append(qual)
    query = db.select(UserAccount)
    if only is not None:
        query = query.where(UserAccount.id == only)
    users = {u.id: u for u in db.session.scalars(query)}
    # Emails in use, to skip a person whose new email another account still has.
    emails = db.select(UserAccount.id, UserAccount.email)
    if only is not None:
        emails = emails.where(UserAccount.email.in_([_first(a, "mail") or "" for a in people.values()]))
    taken = {e.lower(): i for i, e in db.session.execute(emails)}
    touched = False

    seen: set[uuid.UUID] = set()
    for dn, attrs in people.items():
        try:
            member_id = uuid.UUID(_first(attrs, "crcMemberId") or "")
        except ValueError:
            log.warning("Directory sync: %s has no valid crcMemberId; skipped", dn)
            continue
        seen.add(member_id)
        email = _first(attrs, "mail") or ""
        # ponytail: two people swapping emails are both skipped for good; a
        # swap needs a temporary address, add it if that ever happens.
        if not email or taken.get(email.lower(), member_id) != member_id:
            log.warning("Directory sync: %s has no email or one another MedCover account uses; skipped", member_id)
            continue
        user_roles = [r for r in local_roles if dn in role_members.get(r.slug, set())]
        try:
            with db.session.begin_nested():
                old_email, changed = _copy_person(
                    users.get(member_id), member_id, dn, attrs, units, user_roles, held.get(dn, [])
                )
        except SQLAlchemyError:
            log.exception("Directory sync: %s could not be copied; skipped", member_id)
            continue
        touched = touched or changed
        taken.pop(old_email, None)
        taken[email.lower()] = member_id

    for member_id, user in users.items():
        if member_id not in seen and (user.is_active or not user.is_archived or user.roles):
            before = _snapshot(user)
            user.is_active = False
            user.is_archived = True
            user.roles = []
            touched = _record(user, before) or touched
    return touched


def _copy_person(
    user: UserAccount | None,
    member_id: uuid.UUID,
    dn: str,
    attrs: Attrs,
    units: dict[str, Attrs],
    user_roles: list[Role],
    quals: list[Qualification],
) -> tuple[str | None, bool]:
    """Create or update one person. Returns their previous email (lower case)
    and whether a responsible person may have to change."""
    email = _first(attrs, "mail") or ""
    unit = units.get(_parent(dn), {})
    status = _first(attrs, "crcMemberStatus")
    if user is None:
        user = UserAccount(id=member_id, password_hash=NO_PASSWORD, email=email)
        db.session.add(user)
        before = None
    else:
        before = _snapshot(user)
    old_email = user.email.lower() if before else None
    user.name = _first(attrs, "cn") or email
    user.email = email
    user.phone = _first(attrs, "telephoneNumber")
    user.is_active = status == "active" and bool(user_roles)
    user.is_archived = status == "former"
    user.crc_unit_id = _first(unit, "crcUnitId")
    user.unit_name = _first(unit, "displayName")
    user.kind = _first(attrs, "crcMemberKind")
    user.roles = user_roles
    user.qualifications = quals
    if before is None:
        db.session.flush()
        _audit("create", "UserAccount", user.id, f"Uživatel {user.name} převzat z Evidence členů")
        return None, True
    changed = _record(user, before)
    db.session.flush()
    return old_email, changed
