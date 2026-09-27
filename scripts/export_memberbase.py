"""
Export MedCover users, roles and qualifications as LDIF for the MemberBase
directory.

Each person goes to the Místní skupina given in the assignment CSV (or to
the external users), or to the ``--unit`` default. They keep their MedCover
UUID as crcMemberId and get no password: active users become "new" and set
one from the invitation that MemberBase's „Pozvánky“ page sends; users
awaiting activation become "inactive"; archived users become "former" and
get no roles. Qualification IDs are derived from the MedCover IDs, so
re-running the export gives the same entries.

A person already in the directory with the same email but another
crcMemberId (e.g. the bootstrap admin) is re-keyed to the MedCover UUID
instead of being added; their place, status and roles stay as they are.
Keycloak links users by crcMemberId, so it re-imports them and they enrol
their second factor again.

Loading with ``ldapmodify -c`` skips what already exists, so the export can
be loaded again after new users appear in MedCover. It adds but never
updates or removes: change existing people in MemberBase.

Usage (the files contain member data; keep them out of any repository):
    umask 077
    dump() {  # the whole directory, unwrapped
        docker exec medcover-openldap-1 ldapsearch -Y EXTERNAL -Q -LLL -o ldif-wrap=no \\
            -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ -b <base DN> \\
            objectClass crcMemberId mail member crcQualificationRef
    }

    # 1. A CSV of all users; fill in each one's Místní skupina slug, or
    #    "external" for external users. Blank rows get --unit.
    python scripts/export_memberbase.py --template /tmp/assignment.csv

    # 2. Export and load.
    dump > /tmp/existing.ldif
    python scripts/export_memberbase.py --base-dn <base DN> --assignment /tmp/assignment.csv \\
        --existing /tmp/existing.ldif --output /tmp/medcover.ldif
    docker exec -i medcover-openldap-1 ldapmodify -Y EXTERNAL -Q -c \\
        -H ldapi://%2Fvar%2Frun%2Fslapd%2Fldapi/ < /tmp/medcover.ldif

    # 3. Compare people per Místní skupina, role members and qualification
    #    holders in the directory with MedCover (exits 1 on a difference).
    dump > /tmp/loaded.ldif
    python scripts/export_memberbase.py --base-dn <base DN> --assignment /tmp/assignment.csv \\
        --existing /tmp/existing.ldif --check /tmp/loaded.ldif
"""

import argparse
import base64
import csv
import os
import sys
import uuid
from collections import Counter
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
EXTERNAL = "external"

Attrs = dict[str, list[str]]
# ("Místní skupina" | "role" | "qualification", name) → number of people
Counts = Counter[tuple[str, str]]


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
    """MedCover names are "Surname Given": the first word is the surname, the rest the given name."""
    surname, _, given = full_name.strip().partition(" ")
    return surname, given.strip()


def read_ldif(path: str) -> list[Attrs]:
    """Unwrapped ldapsearch output → entries, attribute names lower-case."""
    entries: list[Attrs] = []
    with open(path, encoding="utf-8") as f:
        for block in f.read().split("\n\n"):
            entry: Attrs = {}
            for line in block.splitlines():
                attr, sep, value = line.partition(":")
                if value.startswith(":"):  # "attr:: <base64>" for values that are not plain ASCII
                    entry.setdefault(attr.lower(), []).append(base64.b64decode(value[1:].strip()).decode())
                elif sep and not line.startswith("#"):
                    entry.setdefault(attr.lower(), []).append(value.strip())
            if "dn" in entry:
                entries.append(entry)
    return entries


def place_of(dn: str) -> str | None:
    """The Místní skupina slug (or "external") a person's DN is in; None for other entries."""
    rdns = dn.split(",")
    if len(rdns) > 2 and rdns[0].lower().startswith("uid=") and rdns[1].lower() == "ou=external":
        return EXTERNAL
    if len(rdns) > 3 and rdns[0].lower().startswith("uid=") and rdns[2].lower() == "ou=units":
        return rdns[1][3:]
    return None


def people_and_units(entries: list[Attrs]) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Lower-case email → (DN, crcMemberId), and the slugs of the Místní skupiny."""
    people = {e["mail"][0].lower(): (e["dn"][0], e["crcmemberid"][0]) for e in entries if "mail" in e}
    units = set()
    for e in entries:
        rdns = e["dn"][0].split(",")
        if len(rdns) > 2 and rdns[1].lower() == "ou=units" and rdns[0].lower().startswith("ou="):
            units.add(rdns[0][3:])
    return people, units


def write_template(path: str) -> int:
    """A CSV of all users with an empty Místní skupina column; semicolons and a BOM for Excel."""
    users = db.session.scalars(db.select(UserAccount).order_by(UserAccount.name)).all()
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8-sig", newline="") as f:
        out = csv.writer(f, delimiter=";")
        out.writerow(["email", "name", "archived", "unit"])
        out.writerows([u.email, u.name, "yes" if u.is_archived else "", ""] for u in users)
    return len(users)


def read_assignment(path: str) -> dict[str, str]:
    """Lower-case email → Místní skupina slug or "external"; rows with no unit are left out."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        dialect = csv.Sniffer().sniff(f.readline(), delimiters=";,")
        f.seek(0)
        return {
            r["email"].strip().lower(): r["unit"].strip()
            for r in csv.DictReader(f, dialect=dialect)
            if r["unit"].strip()
        }


def status(user: UserAccount) -> str:
    if user.is_archived:
        return "former"
    return "new" if user.is_active else "inactive"


def export(
    base_dn: str,
    default_unit: str | None,
    assignment: dict[str, str],
    existing: dict[str, tuple[str, str]],
    units: set[str],
) -> tuple[list[str], list[str], Counts]:
    """LDIF records, re-keyed people and the expected counts. Exits on a bad assignment."""
    medcover = f"ou=medcover,ou=apps,{base_dn}"
    records: list[str] = []
    rekeyed: list[str] = []
    counts: Counts = Counter()

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
    members: list[tuple[str, str]] = []
    role_members: dict[str, list[str]] = {}
    problems: list[str] = []
    users = db.session.scalars(db.select(UserAccount).order_by(UserAccount.name)).all()
    for email in sorted(set(assignment) - {u.email.lower() for u in users}):
        problems.append(f"{email}: not a MedCover user")
    for user in users:
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
            unit = assignment.get(user.email.lower(), default_unit)
            if unit is None:
                problems.append(f"{user.email}: no Místní skupina")
                continue
            if unit != EXTERNAL and unit not in units:
                problems.append(f"{user.email}: unknown Místní skupina {unit!r}")
                continue
            parent = f"ou={EXTERNAL},{base_dn}" if unit == EXTERNAL else f"ou={unit},ou=units,{base_dn}"
            dn = f"uid={member_id},{parent}"
            surname, given = split_name(user.name)
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
                        "crcMemberKind": [EXTERNAL if unit == EXTERNAL else "member"],
                        "crcStatusChangedAt": [now],
                    },
                )
            )
            if unit != EXTERNAL:
                members.append((parent, dn))
        counts["Místní skupina", place_of(dn) or ""] += 1
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
                counts["qualification", qual.name] += 1
        if not user.is_archived:
            for role in user.roles:
                role_members.setdefault(role.name.lower().replace(" ", "-"), []).append(dn)
    if problems:
        sys.exit("The assignment does not fit:\n" + "\n".join(problems))

    # One value per record: with ldapmodify -c an existing value fails only its own record.
    records += [_modify(f"cn=members,{unit_dn}", "add", "member", [dn]) for unit_dn, dn in members]
    for role, dns in sorted(role_members.items()):
        records += [_modify(f"cn={role},ou=roles,{medcover}", "add", "member", [dn]) for dn in dns]
        counts["role", role] = len(dns)
    return records, rekeyed, counts


def directory_counts(entries: list[Attrs], base_dn: str) -> Counts:
    """People per Místní skupina, MedCover role members and qualification holders in a directory dump."""
    names = {
        qualification_id(q): q.name
        for q in db.session.scalars(db.select(Qualification).where(Qualification.is_deleted == sa.false()))
    }
    roles = f",ou=roles,ou=medcover,ou=apps,{base_dn}".lower()
    counts: Counts = Counter()
    for e in entries:
        dn = e["dn"][0]
        classes = {c.lower() for c in e.get("objectclass", [])}
        if "crcmember" in classes:
            counts["Místní skupina", place_of(dn) or ""] += 1
        elif "crcholding" in classes:
            qid = e["crcqualificationref"][0]
            counts["qualification", names.get(qid, qid)] += 1
        elif dn.lower().endswith(roles) and "member" in e:
            counts["role", dn.split(",")[0][3:]] = len(e["member"])
    return counts


def compare(expected: Counts, found: Counts) -> list[str]:
    """One line per count, marking differences."""
    lines = []
    for key in sorted(expected.keys() | found.keys()):
        mark = "  " if expected[key] == found[key] else "≠ "
        lines.append(f"{mark}{key[0]} {key[1]}: MedCover {expected[key]}, directory {found[key]}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", help="write a CSV of all users to fill in, then stop")
    parser.add_argument("--base-dn", help="directory base DN, e.g. dc=example,dc=org")
    parser.add_argument("--assignment", help="filled-in CSV: email and unit (a slug or external)")
    parser.add_argument("--unit", help="slug of the Místní skupina for people the CSV does not assign")
    parser.add_argument("--existing", help="dump of the directory before loading")
    parser.add_argument("--output", help="LDIF file to write")
    parser.add_argument("--check", help="dump of the directory after loading, to compare with MedCover")
    args = parser.parse_args()

    with create_app().app_context():
        if args.template:
            print(f"Wrote {write_template(args.template)} users to {args.template}")
            return
        if not (args.base_dn and args.existing and (args.output or args.check)):
            parser.error("--base-dn, --existing and --output or --check are required")
        people, units = people_and_units(read_ldif(args.existing))
        assignment = read_assignment(args.assignment) if args.assignment else {}
        records, rekeyed, expected = export(args.base_dn, args.unit, assignment, people, units)
        if args.check:
            lines = compare(expected, directory_counts(read_ldif(args.check), args.base_dn))
            print("\n".join(lines))
            sys.exit(1 if any(line.startswith("≠") for line in lines) else 0)

    # Member data: readable by the owner only.
    with open(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write("\n\n".join(records) + "\n")
    print(f"Wrote {len(records)} records to {args.output}")
    for email in rekeyed:
        print(f"Re-keyed existing person {email} to the MedCover UUID")


if __name__ == "__main__":
    main()
