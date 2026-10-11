"""The MemberBase export: assignment CSV, placement and the staging count check."""

import importlib.util
from pathlib import Path

import pytest

from app.extensions import db
from app.models.qualification import Qualification
from app.models.role import Role
from tests.conftest import _make_user

BASE = "dc=example,dc=org"


def _import_script():
    spec = importlib.util.spec_from_file_location(
        "export_memberbase", Path(__file__).parent.parent / "scripts" / "export_memberbase.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_script = _import_script()

EXISTING = f"""dn: ou=north,ou=units,{BASE}
objectClass: organizationalUnit

dn: uid=old-id,ou=north,ou=units,{BASE}
objectClass: crcMember
crcMemberId: old-id
mail: Boss@example.org
"""


def test_assignment_places_people_and_check_compares_counts(app, tmp_path):
    with app.app_context():
        qual = Qualification(name="Testovací kvalifikace")
        db.session.add(qual)
        novak = _make_user("novak@example.org", "Novák Jan", Role.MEMBER)
        _make_user("guest@example.org", "Host Petr", Role.VIEWER)
        boss = _make_user("boss@example.org", "Šéf Karel", Role.ADMIN)
        novak.qualifications = [qual]
        db.session.commit()
        novak_id, boss_id = str(novak.id), str(boss.id)

        template = tmp_path / "assignment.csv"
        _script.write_template(str(template))
        lines = template.read_text(encoding="utf-8-sig").splitlines()
        assert lines[0] == "email;name;archived;unit"
        filled = [lines[0]] + [
            line + {"novak": "north", "guest": "external"}.get(line.split("@")[0], "") for line in lines[1:]
        ]
        template.write_text("\n".join(filled), encoding="utf-8-sig")

        existing = tmp_path / "existing.ldif"
        existing.write_text(EXISTING, encoding="utf-8")
        people, units = _script.people_and_units(_script.read_ldif(str(existing)))
        assert units == {"north"}
        records, rekeyed, expected = _script.export(BASE, None, _script.read_assignment(str(template)), people, units)

        ldif = "\n\n".join(records)
        assert f"dn: uid={novak_id},ou=north,ou=units,{BASE}" in ldif
        assert "ou=external," + BASE in ldif and "crcMemberKind: external" in ldif
        assert (
            f"dn: cn=members,ou=north,ou=units,{BASE}\nchangetype: modify\nadd: member\nmember: uid={novak_id}" in ldif
        )
        assert rekeyed == ["boss@example.org"]  # already in the directory: stays where it is
        assert expected == {
            ("Místní skupina", "north"): 2,
            ("Místní skupina", "external"): 1,
            ("qualification", "Testovací kvalifikace"): 1,
            ("role", "admin"): 1,
            ("role", "member"): 1,
            ("role", "viewer"): 1,
        }

        loaded = tmp_path / "loaded.ldif"
        qid = _script.qualification_id(qual)
        loaded.write_text(
            f"""dn: uid={novak_id},ou=north,ou=units,{BASE}
objectClass: crcMember

dn: crcHoldingId=h,uid={novak_id},ou=north,ou=units,{BASE}
objectClass: crcHolding
crcQualificationRef: {qid}

dn: uid={boss_id},ou=north,ou=units,{BASE}
objectClass: crcMember

dn: cn=admin,ou=roles,ou=medcover,ou=apps,{BASE}
member: uid={boss_id},ou=north,ou=units,{BASE}
""",
            encoding="utf-8",
        )
        report = _script.compare(expected, _script.directory_counts(_script.read_ldif(str(loaded)), BASE))
        assert "  Místní skupina north: MedCover 2, directory 2" in report
        assert "≠ Místní skupina external: MedCover 1, directory 0" in report
        assert "≠ role member: MedCover 1, directory 0" in report
        assert "  qualification Testovací kvalifikace: MedCover 1, directory 1" in report


def test_bad_assignment_is_refused(app, tmp_path):
    with app.app_context():
        _make_user("novak@example.org", "Novák Jan", Role.MEMBER)
        _make_user("nobody-assigned@example.org", "Nikdo Jan", Role.MEMBER)
        assignment = {"novak@example.org": "south", "typo@example.org": "north"}
        with pytest.raises(SystemExit) as refused:
            _script.export(BASE, None, assignment, {}, {"north"})
        message = str(refused.value)
        assert "typo@example.org: not a MedCover user" in message
        assert "novak@example.org: unknown Místní skupina 'south'" in message
        assert "nobody-assigned@example.org: no Místní skupina" in message
