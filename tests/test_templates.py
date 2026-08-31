"""Tests for the Event Templates CRUD feature."""

import re
from pathlib import Path

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpotTemplate, EventTemplate
from app.models.qualification import Qualification
from tests.conftest import _make_master_event, _make_rp_qual


def _make_template(
    app,
    name: str = "Test Template",
    paid: bool = False,
    spot_count: int = 0,
    rp_qual_id: int | None = None,
) -> int:
    """Create an EventTemplate in the DB. If rp_qual_id is given, the first spot will require
    that qualification (with can_be_rp=True), satisfying the RP constraint."""
    with app.app_context():
        tmpl = EventTemplate(
            name=name,
            description="Test description",
            paid=paid,
            reminder_schedule="24,48",
        )
        db.session.add(tmpl)
        db.session.flush()
        for i in range(spot_count):
            st = EventSpotTemplate(template_id=tmpl.id, description=f"Pozice {i + 1}")
            if i == 0 and rp_qual_id is not None:
                qual = db.session.get(Qualification, rp_qual_id)
                if qual:
                    st.required_qualifications = [qual]
            db.session.add(st)
        db.session.commit()
        return tmpl.id


def _event_form_data(master_event_id: int, name: str = "Template Test Event", rp_qual_id: int | None = None) -> dict:
    data: dict = {
        "name": name,
        "master_event_id": str(master_event_id),
        "start_datetime": "2030-07-01T10:00",
        "end_datetime": "2030-07-01T18:00",
        "spot_count": "0",
    }
    if rp_qual_id is not None:
        data["spot_total"] = "1"
        data["spot_desc_0"] = "Zdravotník"
        data["spot_cred_0"] = str(rp_qual_id)
    return data


# ── List page ─────────────────────────────────────────────────────────────────


class TestTemplateListPermissions:
    def test_list_requires_login(self, client):
        response = client.get("/templates/", follow_redirects=False)
        assert response.status_code == 302

    def test_list_accessible_for_admin(self, admin_client):
        response = admin_client.get("/templates/")
        assert response.status_code == 200

    def test_list_accessible_for_coordinator(self, coordinator_client):
        response = coordinator_client.get("/templates/")
        assert response.status_code == 200

    def test_list_forbidden_for_member(self, member_client):
        # Member no longer has event_template.view — templates are only for coordinators+
        response = member_client.get("/templates/")
        assert response.status_code == 403


# ── Create ────────────────────────────────────────────────────────────────────


class TestTemplateCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/templates/create")
        assert response.status_code == 200

    def test_create_page_forbidden_for_member(self, member_client):
        response = member_client.get("/templates/create")
        assert response.status_code == 403

    def test_admin_can_create_template(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        response = admin_client.post(
            "/templates/create",
            data={
                "name": "Závod",
                "reminder_schedule": "24",
                "paid": "1",
                "spot_desc_0": "Zdravotník",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == "Závod"))
            assert tmpl is not None
            assert tmpl.paid is True
            assert tmpl.version == 1

    def test_create_template_missing_name_returns_error(self, app, admin_client):
        response = admin_client.post(
            "/templates/create",
            data={"name": "", "reminder_schedule": "24"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            count = db.session.scalar(db.select(db.func.count()).select_from(EventTemplate))
            assert count == 0

    def test_template_appears_in_list_after_creation(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        admin_client.post(
            "/templates/create",
            data={
                "name": "Maraton",
                "reminder_schedule": "24",
                "spot_desc_0": "Zdravotník",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        response = admin_client.get("/templates/")
        assert b"Maraton" in response.data

    def test_create_template_with_spots(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        response = admin_client.post(
            "/templates/create",
            data={
                "name": "Se pozicemi",
                "reminder_schedule": "24",
                "spot_desc_0": "Záchranář",
                "spot_cred_0": str(rp_qual_id),
                "spot_desc_1": "Řidič",
                "spot_total": "2",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == "Se pozicemi"))
            assert tmpl is not None
            assert len(tmpl.spot_templates) == 2

    def test_create_produces_audit_entry(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        admin_client.post(
            "/templates/create",
            data={
                "name": "Audit Test",
                "reminder_schedule": "24",
                "spot_desc_0": "Zdravotník",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "EventTemplate",
                    AuditLogEntry.action_type == "create",
                )
            )
            assert entry is not None


# ── Edit ──────────────────────────────────────────────────────────────────────


class TestTemplateEdit:
    def test_edit_page_loads_for_admin(self, app, admin_client):
        tmpl_id = _make_template(app)
        response = admin_client.get(f"/templates/{tmpl_id}/edit")
        assert response.status_code == 200

    def test_edit_page_forbidden_for_member(self, app, member_client):
        tmpl_id = _make_template(app)
        response = member_client.get(f"/templates/{tmpl_id}/edit")
        assert response.status_code == 403

    def test_admin_can_edit_template(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Original")
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            ver = tmpl.version

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Updated",
                "reminder_schedule": "48",
                "version": str(ver),
                "spot_desc_0": "Zdravotník",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            assert tmpl.name == "Updated"
            assert tmpl.version == ver + 1

    def test_edit_with_stale_version_returns_error(self, app, admin_client):
        tmpl_id = _make_template(app, name="Stale Version Test")
        # Submit with version 0 (wrong)
        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={"name": "Updated Stale", "reminder_schedule": "24", "version": "0"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím změněn" in response.data.decode("utf-8")
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            assert tmpl.name == "Stale Version Test"

    def test_edit_rebuilds_spots(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Rebuild Spots", spot_count=2)
        with app.app_context():
            ver = db.session.get(EventTemplate, tmpl_id).version

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Rebuild Spots",
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_desc_0": "Nová pozice",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            assert len(tmpl.spot_templates) == 1
            assert tmpl.spot_templates[0].description == "Nová pozice"

    def test_create_saves_spot_qualifications(self, app, admin_client):
        """Regression: spot_cred_N name must use the spot index, not the
        inner qualification loop index, so qualifications are stored correctly."""
        with app.app_context():
            q1 = Qualification(name="Zelenáč", can_be_rp=True)
            q2 = Qualification(name="Záchranář")
            db.session.add_all([q1, q2])
            db.session.commit()
            q1_id, q2_id = q1.id, q2.id

        response = admin_client.post(
            "/templates/create",
            data={
                "name": "Qual Save Test",
                "reminder_schedule": "24",
                "spot_desc_0": "Pozice A",
                "spot_cred_0": str(q1_id),
                "spot_desc_1": "Pozice B",
                "spot_cred_1": str(q2_id),
                "spot_total": "2",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == "Qual Save Test"))
            assert tmpl is not None
            assert len(tmpl.spot_templates) == 2
            by_desc = {st.description: st for st in tmpl.spot_templates}
            assert len(by_desc["Pozice A"].required_qualifications) == 1
            assert by_desc["Pozice A"].required_qualifications[0].id == q1_id
            assert len(by_desc["Pozice B"].required_qualifications) == 1
            assert by_desc["Pozice B"].required_qualifications[0].id == q2_id

    def test_edit_saves_spot_qualifications(self, app, admin_client):
        """Regression: editing a template preserves qualifications per spot."""
        with app.app_context():
            q = Qualification(name="EditQualTest", can_be_rp=True)
            db.session.add(q)
            db.session.commit()
            q_id = q.id

        tmpl_id = _make_template(app, name="Edit Qual Save", spot_count=1)
        with app.app_context():
            ver = db.session.get(EventTemplate, tmpl_id).version

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Edit Qual Save",
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_desc_0": "Pozice 1",
                "spot_cred_0": str(q_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            assert len(tmpl.spot_templates) == 1
            st = tmpl.spot_templates[0]
            assert len(st.required_qualifications) == 1
            assert st.required_qualifications[0].id == q_id

    def test_edit_page_renders_empty_description_when_null(self, app, admin_client):
        """Regression for #472: a template with no description must not render the literal
        string 'None' inside the <textarea>, otherwise submitting the form persists 'None'."""
        with app.app_context():
            tmpl = EventTemplate(name="No Description", description=None, reminder_schedule="24")
            db.session.add(tmpl)
            db.session.commit()
            tmpl_id = tmpl.id

        response = admin_client.get(f"/templates/{tmpl_id}/edit")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        match = re.search(
            r'<textarea[^>]*name="description"[^>]*>(.*?)</textarea>',
            body,
            re.DOTALL,
        )
        assert match is not None, "description textarea not found"
        assert match.group(1) == "", f"expected empty textarea, got {match.group(1)!r}"

    def test_edit_does_not_persist_literal_none_from_empty_description(self, app, admin_client):
        """Regression for #472: submitting the edit form for a template that had no
        description must leave description NULL, not the string 'None'."""
        rp_qual_id = _make_rp_qual(app)
        with app.app_context():
            tmpl = EventTemplate(name="Roundtrip None", description=None, reminder_schedule="24")
            db.session.add(tmpl)
            db.session.flush()
            st = EventSpotTemplate(template_id=tmpl.id, description="Zdravotník")
            qual = db.session.get(Qualification, rp_qual_id)
            st.required_qualifications = [qual]
            db.session.add(st)
            db.session.commit()
            tmpl_id = tmpl.id
            ver = tmpl.version

        # Fetch the edit page, then POST back the value the page rendered.
        page = admin_client.get(f"/templates/{tmpl_id}/edit")
        body = page.data.decode("utf-8")
        match = re.search(
            r'<textarea[^>]*name="description"[^>]*>(.*?)</textarea>',
            body,
            re.DOTALL,
        )
        assert match is not None
        rendered_description = match.group(1)

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Roundtrip None",
                "description": rendered_description,
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_desc_0": "Zdravotník",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            assert tmpl.description is None


# ── Delete ────────────────────────────────────────────────────────────────────


class TestTemplateDelete:
    def test_admin_can_delete_template(self, app, admin_client):
        tmpl_id = _make_template(app, name="To Delete")
        response = admin_client.post(
            f"/templates/{tmpl_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EventTemplate, tmpl_id) is None

    def test_delete_forbidden_for_member(self, app, member_client):
        tmpl_id = _make_template(app, name="Protected Template")
        response = member_client.post(f"/templates/{tmpl_id}/delete")
        assert response.status_code == 403
        with app.app_context():
            assert db.session.get(EventTemplate, tmpl_id) is not None

    def test_delete_produces_audit_entry(self, app, admin_client):
        tmpl_id = _make_template(app, name="Audit Delete")
        admin_client.post(f"/templates/{tmpl_id}/delete", follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "EventTemplate",
                    AuditLogEntry.action_type == "delete",
                )
            )
            assert entry is not None


# ── Create event from template ────────────────────────────────────────────────


class TestCreateEventFromTemplate:
    def test_create_from_template_page_loads(self, app, admin_client):
        tmpl_id = _make_template(app, name="Load Template")
        response = admin_client.get(f"/events/create-from-template/{tmpl_id}")
        assert response.status_code == 200
        assert "Load Template" in response.data.decode("utf-8")

    def test_create_from_template_forbidden_for_member(self, app, member_client):
        tmpl_id = _make_template(app)
        response = member_client.get(f"/events/create-from-template/{tmpl_id}")
        assert response.status_code == 403

    def test_create_from_template_prefills_paid(self, app, admin_client):
        tmpl_id = _make_template(app, name="Paid Template", paid=True)
        response = admin_client.get(f"/events/create-from-template/{tmpl_id}")
        # paid checkbox should be checked
        assert b"checked" in response.data

    def test_create_from_nonexistent_template_returns_404(self, app, admin_client):
        response = admin_client.get("/events/create-from-template/99999")
        assert response.status_code == 404

    def test_create_event_from_template_creates_spots(self, app, admin_client):
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Spot Template", spot_count=3, rp_qual_id=rp_qual_id)
        me_id = _make_master_event(app)
        data = _event_form_data(me_id, name="From Template Event")
        data["template_id"] = str(tmpl_id)
        # Simulate spots pre-filled from template (as form.html does on GET)
        data.update(
            {
                "spot_total": "3",
                "spot_desc_0": "Pozice 1",
                "spot_cred_0": str(rp_qual_id),
                "spot_desc_1": "Pozice 2",
                "spot_desc_2": "Pozice 3",
            }
        )
        response = admin_client.post(
            "/events/create",
            data=data,
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "From Template Event"))
            assert event is not None
            assert len(event.spots) == 3

    def test_create_event_from_template_spot_descriptions_match(self, app, admin_client):
        """Spots created from template carry descriptions from spot templates."""
        with app.app_context():
            rp_qual = Qualification(name="RP Qual Desc", can_be_rp=True)
            db.session.add(rp_qual)
            db.session.flush()
            tmpl = EventTemplate(name="Desc Template", reminder_schedule="24")
            db.session.add(tmpl)
            db.session.flush()
            st1 = EventSpotTemplate(template_id=tmpl.id, description="Záchranář")
            st1.required_qualifications = [rp_qual]
            db.session.add(st1)
            db.session.add(EventSpotTemplate(template_id=tmpl.id, description="Řidič"))
            db.session.commit()
            tmpl_id = tmpl.id
            rp_qual_id = rp_qual.id

        me_id = _make_master_event(app)
        data = _event_form_data(me_id, name="Desc Template Event")
        data["template_id"] = str(tmpl_id)
        # Simulate spots pre-filled from template (as form.html does on GET)
        data.update(
            {
                "spot_total": "2",
                "spot_desc_0": "Záchranář",
                "spot_cred_0": str(rp_qual_id),
                "spot_desc_1": "Řidič",
            }
        )
        admin_client.post("/events/create", data=data, follow_redirects=True)

        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Desc Template Event"))
            assert event is not None
            descriptions = {s.description for s in event.spots}
            assert "Záchranář" in descriptions
            assert "Řidič" in descriptions

    def test_create_event_without_template_uses_spot_count(self, app, admin_client):
        me_id = _make_master_event(app)
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, name="Spot Count Event")
        data["spot_total"] = "2"
        data["spot_desc_0"] = "Záchranář"
        data["spot_cred_0"] = str(rp_qual_id)
        data["spot_desc_1"] = "Zdravotník"
        response = admin_client.post("/events/create", data=data, follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            event = db.session.scalar(db.select(Event).where(Event.name == "Spot Count Event"))
            assert event is not None
            assert len(event.spots) == 2


# ── Template lint ─────────────────────────────────────────────────────────────


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


class TestTemplateSpotRpConstraint:
    """Verify that create/edit template routes enforce the RP-capable spot constraint."""

    def test_create_template_no_spots_rejected(self, app, admin_client):
        """POST to create with no spots must be rejected."""
        response = admin_client.post(
            "/templates/create",
            data={"name": "No Spots Template", "reminder_schedule": "24"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Šablona musí mít alespoň jednu pozici".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(EventTemplate)) == 0

    def test_create_template_all_optional_spots_rejected(self, app, admin_client):
        """POST to create where all spots are optional must be rejected."""
        rp_qual_id = _make_rp_qual(app)
        response = admin_client.post(
            "/templates/create",
            data={
                "name": "All Optional Template",
                "reminder_schedule": "24",
                "spot_desc_0": "Volitelná pozice",
                "spot_cred_0": str(rp_qual_id),
                "spot_optional_0": "1",
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Šablona musí mít alespoň jednu povinnou pozici".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(EventTemplate)) == 0

    def test_create_template_mandatory_spot_no_rp_qual_rejected(self, app, admin_client):
        """POST to create with a mandatory spot without RP qual must be rejected."""
        with app.app_context():
            non_rp_qual = Qualification(name="Non RP Template Qual", can_be_rp=False)
            db.session.add(non_rp_qual)
            db.session.commit()
            qual_id = non_rp_qual.id

        response = admin_client.post(
            "/templates/create",
            data={
                "name": "No RP Qual Template",
                "reminder_schedule": "24",
                "spot_desc_0": "Povinná pozice",
                "spot_cred_0": str(qual_id),
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci".encode() in response.data
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count()).select_from(EventTemplate)) == 0

    def test_create_template_with_rp_qual_mandatory_spot_succeeds(self, app, admin_client):
        """POST to create with a mandatory spot with RP-capable qual must succeed."""
        rp_qual_id = _make_rp_qual(app)
        response = admin_client.post(
            "/templates/create",
            data={
                "name": "Valid RP Template",
                "reminder_schedule": "24",
                "spot_desc_0": "RP Pozice",
                "spot_cred_0": str(rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            tmpl = db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == "Valid RP Template"))
            assert tmpl is not None

    def test_edit_template_removing_last_rp_capable_spot_rejected(self, app, admin_client):
        """Editing a template to remove the last RP-capable mandatory spot must be rejected."""
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Has RP Spot Template", spot_count=1, rp_qual_id=rp_qual_id)
        with app.app_context():
            ver = db.session.get(EventTemplate, tmpl_id).version

        # Edit to replace the RP spot with a non-RP spot
        with app.app_context():
            non_rp_qual = Qualification(name="Non RP Edit Qual", can_be_rp=False)
            db.session.add(non_rp_qual)
            db.session.commit()
            non_rp_qual_id = non_rp_qual.id

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Has RP Spot Template",
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_desc_0": "Povinná bez RP",
                "spot_cred_0": str(non_rp_qual_id),
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci".encode() in response.data
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            # Template should remain unchanged (spots not rebuilt due to validation failure)
            assert len(tmpl.spot_templates) == 1

    def test_edit_template_all_optional_spots_rejected(self, app, admin_client):
        """Editing a template so that all spots become optional must be rejected."""
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Edit All Optional Template", spot_count=1, rp_qual_id=rp_qual_id)
        with app.app_context():
            ver = db.session.get(EventTemplate, tmpl_id).version

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Edit All Optional Template",
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_desc_0": "Volitelná pozice",
                "spot_cred_0": str(rp_qual_id),
                "spot_optional_0": "1",
                "spot_total": "1",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Šablona musí mít alespoň jednu povinnou pozici".encode() in response.data
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            # Template should remain unchanged
            assert len(tmpl.spot_templates) == 1

    def test_edit_template_no_spots_rejected(self, app, admin_client):
        """Editing a template to have zero spots must be rejected."""
        rp_qual_id = _make_rp_qual(app)
        tmpl_id = _make_template(app, name="Edit No Spots Template", spot_count=1, rp_qual_id=rp_qual_id)
        with app.app_context():
            ver = db.session.get(EventTemplate, tmpl_id).version

        response = admin_client.post(
            f"/templates/{tmpl_id}/edit",
            data={
                "name": "Edit No Spots Template",
                "reminder_schedule": "24",
                "version": str(ver),
                "spot_total": "0",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Šablona musí mít alespoň jednu pozici".encode() in response.data
        with app.app_context():
            tmpl = db.session.get(EventTemplate, tmpl_id)
            # Template should remain unchanged
            assert len(tmpl.spot_templates) == 1
