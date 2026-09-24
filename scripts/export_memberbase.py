"""
Export MedCover users, roles and qualifications as LDIF for the MemberBase
directory.

People keep their MedCover UUID as crcMemberId and are placed in one
Místní skupina (re-assign them in MemberBase afterwards). They get no
password: active users become "new" and set one from the invitation that
MemberBase's „Pozvánky“ page sends; users awaiting activation become "inactive"; archived
users become "former" and get no roles. Qualification IDs are derived from
the MedCover IDs, so re-running the export gives the same entries.

A person already in the directory with the same email but another
crcMemberId (e.g. the bootstrap admin) is re-keyed to the MedCover UUID
instead of being added; their status and roles stay as they are. Keycloak
links users by crcMemberId, so it re-imports them and they enrol their second
factor again.

Loading with ``ldapmodify -c`` skips what already exists, so the export can
be loaded again after new users appear in MedCover. It adds but never
updates or removes: change existing people in MemberBase.

Usage (the output contains member data; keep it out of any repository):
    umask 077
    docker exec medcover-openldap-1 ldapsearch -Y EXTERNAL -Q -LLL -o ldif-wrap=no \\
        -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ -b <base DN> '(objectClass=crcMember)' \\
        crcMemberId mail > /tmp/existing.ldif
    python scripts/export_memberbase.py --base-dn <base DN> --unit <unit slug> \\
        --existing /tmp/existing.ldif --output /tmp/medcover.ldif
    docker exec -i medcover-openldap-1 ldapmodify -Y EXTERNAL -Q -c \\
        -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ < /tmp/medcover.ldif
"""

import argparse
import base64
import os
import sys
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models.qualification import Qualification
from app.models.user import UserAccount

# Fixed namespace for crcQualificationId = uuid5(namespace, MedCover id).
QUALIFICATION_NAMESPACE = uuid.UUID("11fac960-c2df-46cd-a74e-686fbe95cf6c")

Attrs = dict[str, list[str]]


def _line(attr: str, value: str) -> str:
    """One LDIF attribute line; base64 unless the value is a SAFE-STRING (RFC 2849)."""
    safe = value.isascii() and value.isprintable() and not value.startswith((" ", ":", "<")) and not value.endswith(" ")
    return f"{attr}: {value}" if safe else f"{attr}:: {base64.b64encode(value.encode()).decode()}"


def _add(dn: str, attrs: Attrs) -> str:
    lines = [_line("dn", dn), "changetype: add"]
    lines += [_line(attr, v) for attr, values in attrs.items() for v in values]
    return "\n".join(lines)


def _modify(dn: str, op: str, attr: str, values: list[str]) -> str:
    return "\n".join([_line("dn", dn), "changetype: modify", f"{op}: {attr}", *(_line(attr, v) for v in values), "-"])


def qualification_id(qual: Qualification) -> str:
    return str(uuid.uuid5(QUALIFICATION_NAMESPACE, str(qual.id)))


def holding_id(member_id: str, qual_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(member_id), qual_id))


def split_name(full_name: str) -> tuple[str, str]:
    """Same rule as MemberBase: the surname is the last word."""
    parts = full_name.split()
    return " ".join(parts[:-1]), parts[-1]


def read_existing(path: str) -> dict[str, tuple[str, str]]:
    """Unwrapped ldapsearch output → lower-case email: (DN, crcMemberId)."""
    found: dict[str, tuple[str, str]] = {}
    with open(path, encoding="utf-8") as f:
        for block in f.read().split("\n\n"):
            values = {}
            for line in block.splitlines():
                attr, sep, value = line.partition(":")
                if value.startswith(":"):  # "attr:: <base64>" for values that are not plain ASCII
                    values[attr] = base64.b64decode(value[1:].strip()).decode()
                elif sep:
                    values[attr] = value.strip()
            if "mail" in values:
                found[values["mail"].lower()] = (values["dn"], values["crcMemberId"])
    return found


def status(user: UserAccount) -> str:
    if user.is_archived:
        return "former"
    return "new" if user.is_active else "inactive"


def export(base_dn: str, unit: str, existing: dict[str, tuple[str, str]]) -> tuple[list[str], list[str]]:
    """LDIF records and a list of re-keyed people."""
    unit_dn = f"ou={unit},ou=units,{base_dn}"
    medcover = f"ou=medcover,ou=apps,{base_dn}"
    records: list[str] = []
    rekeyed: list[str] = []

    quals = db.session.scalars(db.select(Qualification).where(Qualification.is_deleted == sa.false())).all()
    for qual in quals:
        qid = qualification_id(qual)
        records.append(
            _add(
                f"crcQualificationId={qid},ou=qualifications,{medcover}",
                {
                    "objectClass": ["crcQualification", "crcMedCoverQualification"],
                    "crcQualificationId": [qid],
                    "cn": [qual.name],
                    "description": [qual.description] if qual.description else [],
                    "crcParent": [qualification_id(p) for p in qual.parents if not p.is_deleted],
                    "crcCanBeRp": ["TRUE" if qual.can_be_rp else "FALSE"],
                },
            )
        )

    now = datetime.now(UTC).strftime("%Y%m%d%H%M%SZ")
    members: list[str] = []
    role_members: dict[str, list[str]] = {}
    for user in db.session.scalars(db.select(UserAccount).order_by(UserAccount.name)).all():
        member_id = str(user.id)
        old_dn, old_id = existing.get(user.email.lower(), (None, None))
        if old_dn:
            dn = f"uid={member_id},{old_dn.split(',', 1)[1]}"
            if old_id != member_id:
                # ponytail: grants naming the old crcMemberId are not rewritten; none exist before go-live
                modrdn = [_line("dn", old_dn), "changetype: modrdn", f"newrdn: uid={member_id}", "deleteoldrdn: 1"]
                records.append("\n".join(modrdn))
                records.append(_modify(dn, "replace", "crcMemberId", [member_id]))
                rekeyed.append(user.email)
        else:
            dn = f"uid={member_id},{unit_dn}"
            given, surname = split_name(user.name)
            records.append(
                _add(
                    dn,
                    {
                        "objectClass": ["inetOrgPerson", "crcMember"],
                        "uid": [member_id],
                        "crcMemberId": [member_id],
                        "cn": [user.name],
                        "givenName": [given] if given else [],
                        "sn": [surname],
                        "mail": [user.email],
                        "telephoneNumber": [user.phone] if user.phone else [],
                        "crcMemberStatus": [status(user)],
                        "crcMemberKind": ["member"],
                        "crcStatusChangedAt": [now],
                    },
                )
            )
            members.append(dn)
        for qual in user.qualifications:
            if not qual.is_deleted:
                qid = qualification_id(qual)
                hid = holding_id(member_id, qid)
                records.append(
                    _add(
                        f"crcHoldingId={hid},{dn}",
                        {"objectClass": ["crcHolding"], "crcHoldingId": [hid], "crcQualificationRef": [qid]},
                    )
                )
        if not user.is_archived:
            for role in user.roles:
                role_members.setdefault(role.name.lower().replace(" ", "-"), []).append(dn)

    # One value per record: with ldapmodify -c an existing value fails only its own record.
    records += [_modify(f"cn=members,{unit_dn}", "add", "member", [dn]) for dn in members]
    for role, dns in sorted(role_members.items()):
        records += [_modify(f"cn={role},ou=roles,{medcover}", "add", "member", [dn]) for dn in dns]
    return records, rekeyed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-dn", required=True, help="directory base DN, e.g. dc=example,dc=org")
    parser.add_argument("--unit", required=True, help="slug of the Místní skupina everyone is placed in")
    parser.add_argument("--existing", required=True, help="unwrapped ldapsearch output of crcMemberId and mail")
    parser.add_argument("--output", required=True, help="LDIF file to write")
    args = parser.parse_args()

    with create_app().app_context():
        records, rekeyed = export(args.base_dn, args.unit, read_existing(args.existing))
    # Member data: readable by the owner only.
    with open(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write("\n\n".join(records) + "\n")
    print(f"Wrote {len(records)} records to {args.output}")
    for email in rekeyed:
        print(f"Re-keyed existing person {email} to the MedCover UUID")


if __name__ == "__main__":
    main()
