"""Condition template CRUD and shared HTML invariants."""

import re
from pathlib import Path

import pytest

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventTemplate, EventTemplateQualificationRequirement, StaffingMode
from app.models.qualification import Qualification
from tests.conftest import _make_master_event, _make_rp_qual


def _form(qid, **extra):
    return {
        "name": "Template",
        "minimum_participants": "2",
        "maximum_participants": "4",
        "requirement_qualification": str(qid),
        "requirement_count": "1",
        **extra,
    }


def _make_template(app, name="Template", paid=False, **kwargs):
    qid = _make_rp_qual(app)
    with app.app_context():
        template = EventTemplate(
            name=name,
            paid=paid,
            minimum_participants=2,
            maximum_participants=4,
            qualification_requirements=[EventTemplateQualificationRequirement(qualification_id=qid, minimum_count=1)],
        )
        db.session.add(template)
        db.session.commit()
        return template.id


@pytest.mark.parametrize("page", ["/templates/", "/templates/create"])
def test_template_access(admin_client, page):
    assert admin_client.get(page).status_code == 200


@pytest.mark.parametrize("page", ["/templates/", "/templates/create"])
def test_template_access_requires_permission(member_client, page):
    assert member_client.get(page).status_code == 403


def test_template_crud_and_audit(app, admin_client):
    qid = _make_rp_qual(app)
    assert admin_client.post("/templates/create", data=_form(qid, description="Original", paid="1")).status_code == 302
    with app.app_context():
        template = db.session.scalar(db.select(EventTemplate))
        tid, version = template.id, template.version
        assert (template.minimum_participants, template.maximum_participants) == (2, 4)
        assert template.qualification_requirements[0].qualification_id == qid
        assert not template.spot_templates
        assert template.paid
    html = admin_client.get(f"/templates/{tid}/edit").data.decode()
    assert 'name="requirement_qualification"' in html
    assert 'id="spotRows"' not in html
    assert (
        admin_client.post(
            f"/templates/{tid}/edit",
            data=_form(
                qid,
                name="Edited",
                description="",
                version=str(version),
                maximum_participants="5",
                requirement_count="2",
            ),
        ).status_code
        == 302
    )
    with app.app_context():
        template = db.session.get(EventTemplate, tid)
        assert template.name == "Edited"
        assert template.description is None
        assert template.maximum_participants == 5
        assert template.qualification_requirements[0].minimum_count == 2
        version = template.version
    html = admin_client.get(f"/templates/{tid}/edit").data.decode()
    assert ">None</textarea>" not in html
    assert admin_client.post(f"/templates/{tid}/edit", data=_form(qid, version=str(version - 1))).status_code == 200
    admin_client.post(f"/templates/{tid}/delete")
    with app.app_context():
        assert db.session.get(EventTemplate, tid) is None
        assert not db.session.scalars(db.select(EventTemplateQualificationRequirement)).all()
        assert {
            a.action_type
            for a in db.session.scalars(db.select(AuditLogEntry).where(AuditLogEntry.entity_type == "EventTemplate"))
        } == {"create", "edit", "delete"}


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"minimum_participants": "0"},
        {"maximum_participants": "1"},
        {"requirement_count": "0"},
        {"requirement_qualification": ""},
        {"requirement_qualification": "duplicate"},
    ],
)
def test_invalid_template_does_not_write(app, admin_client, changes):
    qid = _make_rp_qual(app)
    duplicate = changes.get("requirement_qualification") == "duplicate"
    if duplicate:
        changes = {"requirement_qualification": [str(qid), str(qid)], "requirement_count": ["1", "1"]}
    response = admin_client.post("/templates/create", data=_form(qid, **changes))
    assert response.status_code == 200
    if duplicate:
        assert "Kvalifikace smí být v plánu pouze jednou." in response.data.decode()
    with app.app_context():
        assert db.session.scalar(db.select(EventTemplate)) is None


def test_template_requires_rp_and_unique_name(app, admin_client):
    tid = _make_template(app)
    with app.app_context():
        qid = db.session.get(EventTemplate, tid).qualification_requirements[0].qualification_id
        other = Qualification(name="Non RP")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    assert admin_client.post("/templates/create", data=_form(qid)).status_code == 200
    assert admin_client.post(f"/templates/{tid}/edit", data=_form(other_id)).status_code == 200
    with app.app_context():
        assert db.session.get(EventTemplate, tid).qualification_requirements[0].qualification_id == qid


def test_template_edit_delete_permission(app, member_client):
    tid = _make_template(app)
    assert member_client.get(f"/templates/{tid}/edit").status_code == 403
    assert member_client.post(f"/templates/{tid}/delete").status_code == 403
    assert member_client.get(f"/events/create-from-template/{tid}").status_code == 403


def test_event_from_template_copies_complete_condition_plan(app, admin_client):
    tid = _make_template(app, paid=True)
    me = _make_master_event(app)
    html = admin_client.get(f"/events/create-from-template/{tid}").data.decode()
    assert 'name="minimum_participants"' in html and 'value="4"' in html
    assert 'id="spotRows"' not in html
    with app.app_context():
        qid = db.session.get(EventTemplate, tid).qualification_requirements[0].qualification_id
    data = _form(
        qid,
        template_id=str(tid),
        master_event_id=str(me),
        paid="1",
        start_datetime="2030-06-01T10:00",
        end_datetime="2030-06-01T18:00",
    )
    assert admin_client.post("/events/create", data=data).status_code == 302
    with app.app_context():
        event = db.session.scalar(db.select(Event))
        assert event.staffing_mode == StaffingMode.CONDITIONS and not event.spots
        assert (event.minimum_participants, event.maximum_participants) == (2, 4)
        assert event.qualification_requirements[0].qualification_id == qid
    assert admin_client.get("/events/create-from-template/99999999").status_code == 404


class TestTemplateLint:
    """Static checks on Jinja2 HTML templates to catch common mistakes."""

    def _strip_quoted(self, s: str) -> str:
        """Remove quoted attribute values so > inside values don't fool the parser."""

        s = re.sub(r'"[^"]*"', '""', s)
        s = re.sub(r"'[^']*'", "''", s)
        return s

    def test_all_form_tags_are_closed(self):
        """Every <form …> opening tag must end with > before the next element.

        A missing > causes the browser to treat child elements (e.g. hidden
        csrf_token inputs) as malformed attributes, silently dropping them.
        This was the root cause of the digest-block delete CSRF 400 (PR #179).
        """

        template_dir = Path(__file__).parent.parent / "app" / "templates"
        issues = []

        for tmpl in sorted(template_dir.rglob("*.html")):
            lines = tmpl.read_text().splitlines()
            i = 0
            while i < len(lines):
                if re.search(r"<form\b", lines[i], re.IGNORECASE):
                    block = lines[i]
                    start_line = i + 1
                    j = i + 1
                    # Collect continuation lines until > found (cap at 20 lines)
                    while ">" not in self._strip_quoted(block) and j < min(i + 20, len(lines)):
                        block += "\n" + lines[j]
                        j += 1
                    # After stripping quotes, verify > appears before any new < tag.
                    # A > that belongs to a child element (e.g. <input>) is NOT the
                    # closing > of the <form> opening tag.
                    stripped = self._strip_quoted(block)
                    # Find the position after the "<form" keyword
                    form_pos = stripped.lower().find("<form")
                    after_form = stripped[form_pos + 5 :] if form_pos != -1 else stripped
                    first_gt = after_form.find(">")
                    first_lt = after_form.find("<")
                    broken = first_gt == -1 or (first_lt != -1 and first_lt < first_gt)
                    if broken:
                        rel = tmpl.relative_to(template_dir)
                        issues.append(f"{rel}:{start_line}")
                    i = j
                else:
                    i += 1

        assert not issues, "Found <form> tags missing their closing >:\n" + "\n".join(f"  {loc}" for loc in issues)

    def test_all_post_forms_have_csrf_token(self):
        """Every POST form in a template must contain csrf_token.

        Checks the raw template source — catches missing tokens before
        they reach production. Complements the dynamic CSRF validation
        done by Flask-WTF at request time.
        """

        template_dir = Path(__file__).parent.parent / "app" / "templates"
        issues = []

        for tmpl in sorted(template_dir.rglob("*.html")):
            content = tmpl.read_text()
            for part in re.split(r"<form\b", content, flags=re.IGNORECASE)[1:]:
                close = part.find("</form")
                block = part[:close] if close != -1 else part[:2000]
                if re.search(r'method\s*=\s*["\']?post', block, re.IGNORECASE):
                    if "csrf_token" not in block:
                        rel = tmpl.relative_to(template_dir)
                        issues.append(str(rel))

        assert not issues, "POST forms missing csrf_token:\n" + "\n".join(f"  {p}" for p in issues)


# ── RP spot constraint for templates ─────────────────────────────────────────
