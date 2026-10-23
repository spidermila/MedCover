"""The MemberBase export: assignment CSV, placement and the staging count check."""

import importlib.util
import runpy
import sys
from pathlib import Path

import pytest

import app as app_package
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


def test_read_ldif_decodes_base64_and_skips_comments_and_entries_without_dn(tmp_path):
    dump = tmp_path / "dump.ldif"
    dump.write_text(
        "# extended LDIF\n"
        "dn: uid=a,ou=north,ou=units,dc=example,dc=org\n"
        "cn:: xaBhZsOhxZk=\n"
        "no separator here\n"
        "# comment: inside an entry\n"
        "mail: A@example.org\n"
        "\n"
        "search result\n"
        "result: 0 Success\n",
        encoding="utf-8",
    )
    entries = _script.read_ldif(str(dump))
    assert entries == [
        {"dn": ["uid=a,ou=north,ou=units,dc=example,dc=org"], "cn": ["Šafář"], "mail": ["A@example.org"]}
    ]


@pytest.mark.parametrize(
    ("dn", "place"),
    [
        (f"uid=x,ou=external,{BASE}", "external"),
        (f"uid=x,ou=north,ou=units,{BASE}", "north"),
        (f"cn=admin,ou=roles,ou=medcover,ou=apps,{BASE}", None),
    ],
)
def test_place_of(dn, place):
    assert _script.place_of(dn) == place


def test_export_skips_what_does_not_need_loading(app):
    with app.app_context():
        live = Qualification(name="Platná")
        gone = Qualification(name="Smazaná", is_deleted=True)
        db.session.add_all([live, gone])
        former = _make_user("former@example.org", "Bývalý Jan", Role.MEMBER)
        current = _make_user("current@example.org", "Současný Petr", Role.MEMBER)
        former.is_archived = True
        former.qualifications = [live]
        current.qualifications = [live, gone]
        db.session.commit()
        existing = {"current@example.org": (f"uid={current.id},ou=north,ou=units,{BASE}", str(current.id))}

        records, rekeyed, expected = _script.export(BASE, "north", {}, existing, {"north"})

        ldif = "\n\n".join(records)
        assert rekeyed == []  # the directory already has the MedCover UUID
        assert "crcMemberStatus: former" in ldif
        assert (
            f"cn=member,ou=roles,ou=medcover,ou=apps,{BASE}\nchangetype: modify\nadd: member\nmember: uid={current.id}"
            in ldif
        )
        assert f"member: uid={former.id}" not in ldif.split("cn=member,ou=roles")[1]  # archived: no roles
        assert expected[("qualification", "Platná")] == 2
        assert ("qualification", "Smazaná") not in expected


def test_new_people_carry_status_and_surname_first_name_parts(app):
    with app.app_context():
        active = _make_user("active@example.org", "Novak Jan Josef", Role.MEMBER)
        inactive = _make_user("inactive@example.org", "Svobodova", Role.MEMBER)
        active.is_active = True
        inactive.is_active = False
        db.session.commit()

        records, _, _ = _script.export(BASE, "north", {}, {}, {"north"})

        by_dn = {r.splitlines()[0]: r for r in records}
        first = by_dn[f"dn: uid={active.id},ou=north,ou=units,{BASE}"]
        second = by_dn[f"dn: uid={inactive.id},ou=north,ou=units,{BASE}"]
        assert "crcMemberStatus: new" in first
        assert "sn: Novak" in first and "givenName: Jan Josef" in first
        assert "crcMemberStatus: inactive" in second
        assert "sn: Svobodova" in second and "givenName:" not in second


def test_directory_counts_ignore_other_entries(app):
    with app.app_context():
        counts = _script.directory_counts(
            [{"dn": [f"ou=units,{BASE}"], "objectclass": ["organizationalUnit"]}, {"dn": [BASE]}], BASE
        )
        assert counts == {}


def _run_main(monkeypatch, app, *args):
    monkeypatch.setattr(_script, "create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["export_memberbase.py", *args])
    _script.main()


def test_main_writes_template_then_ldif_and_reports_rekeyed(app, monkeypatch, tmp_path, capsys):
    with app.app_context():
        _make_user("boss@example.org", "Šéf Karel", Role.ADMIN)
    template = tmp_path / "assignment.csv"
    _run_main(monkeypatch, app, "--template", str(template))
    assert "Wrote 1 users to" in capsys.readouterr().out
    assert "boss@example.org" in template.read_text(encoding="utf-8-sig")

    existing = tmp_path / "existing.ldif"
    existing.write_text(EXISTING, encoding="utf-8")
    output = tmp_path / "medcover.ldif"
    _run_main(monkeypatch, app, "--base-dn", BASE, "--existing", str(existing), "--output", str(output))
    out = capsys.readouterr().out
    assert f"Wrote 3 records to {output}" in out
    assert "Re-keyed existing person boss@example.org to the MedCover UUID" in out
    assert "changetype: modrdn" in output.read_text(encoding="utf-8")
    assert output.stat().st_mode & 0o777 == 0o600


def test_main_requires_arguments(app, monkeypatch):
    with pytest.raises(SystemExit) as refused:
        _run_main(monkeypatch, app, "--base-dn", BASE)
    assert refused.value.code == 2


def test_main_check_exit_code_tells_whether_the_directory_matches(app, monkeypatch, tmp_path):
    with app.app_context():
        boss = _make_user("boss@example.org", "Šéf Karel", Role.ADMIN)
        boss_id = str(boss.id)
    existing = tmp_path / "existing.ldif"
    existing.write_text(EXISTING, encoding="utf-8")
    loaded = tmp_path / "loaded.ldif"

    loaded.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as differs:
        _run_main(monkeypatch, app, "--base-dn", BASE, "--existing", str(existing), "--check", str(loaded))
    assert differs.value.code == 1

    loaded.write_text(
        f"""dn: uid={boss_id},ou=north,ou=units,{BASE}
objectClass: crcMember

dn: cn=admin,ou=roles,ou=medcover,ou=apps,{BASE}
member: uid={boss_id},ou=north,ou=units,{BASE}
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as matches:
        _run_main(monkeypatch, app, "--base-dn", BASE, "--existing", str(existing), "--check", str(loaded))
    assert matches.value.code == 0


def test_script_runs_as_a_program(app, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(app_package, "create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["export_memberbase.py", "--template", str(tmp_path / "t.csv")])
    runpy.run_path(str(Path(_script.__file__)), run_name="__main__")
    assert "users to" in capsys.readouterr().out
