import pytest

from app.extensions import db
from app.models.equipment import EquipmentType, EventTemplateEquipmentPlan
from app.models.event import Event, EventSpotTemplate, EventTemplate
from app.models.qualification import Qualification
from tests.test_condition_forms import _form as event_form
from tests.test_templates import _make_template


def legacy_template(app):
    with app.app_context():
        qualification = Qualification(name="Historical qualification", can_be_rp=True)
        template = EventTemplate(
            name="Legacy reference",
            description="Original notes",
            paid=True,
            spot_templates=[
                EventSpotTemplate(
                    description="Optional driver", is_optional=True, required_qualifications=[qualification]
                ),
                EventSpotTemplate(description="Required medic", is_optional=False),
            ],
            equipment_plans=[
                EventTemplateEquipmentPlan(equipment_type=EquipmentType(name="Original bag"), quantity_required=2)
            ],
        )
        db.session.add(template)
        db.session.commit()
        return template.id, qualification.id


def test_legacy_template_reference_preserves_contents_and_manual_delete(app, admin_client):
    template_id, qualification_id = legacy_template(app)
    page = admin_client.get(f"/templates/{template_id}")
    assert page.status_code == 200
    for label in [
        "Original notes",
        "Optional driver",
        "Required medic",
        "Historical qualification",
        "Original bag: 2",
        "Volitelná",
        "Povinná",
    ]:
        assert label in page.data.decode()
    assert f"/events/create-from-template/{template_id}".encode() not in page.data
    assert f"/templates/{template_id}/edit".encode() not in page.data
    # A qualification removed later still remains readable in the old reference.
    admin_client.post(f"/qualifications/{qualification_id}/delete")
    page = admin_client.get(f"/templates/{template_id}")
    assert "Historical qualification (smazaná kvalifikace)" in page.data.decode()
    assert admin_client.post(f"/templates/{template_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(EventTemplate, template_id) is None
        assert not db.session.scalars(db.select(EventSpotTemplate)).all()


@pytest.mark.parametrize("method", ["get", "post"])
def test_legacy_template_cannot_be_edited_or_used(app, admin_client, method):
    template_id, _ = legacy_template(app)
    call = getattr(admin_client, method)
    assert (
        call(
            f"/templates/{template_id}/edit", data={"minimum_participants": "1", "maximum_participants": "2"}
        ).status_code
        == 403
    )
    assert call(f"/events/create-from-template/{template_id}").status_code in (403, 405)
    data = event_form(app, template_id=str(template_id))
    assert admin_client.post("/events/create", data=data).status_code == 403
    assert admin_client.get(f"/events/create?template_id={template_id}").status_code == 403
    with app.app_context():
        template = db.session.get(EventTemplate, template_id)
        assert template.is_legacy and template.minimum_participants is None
        assert not db.session.scalars(db.select(Event)).all()


def test_template_selectors_exclude_legacy_but_keep_condition_plans(app, admin_client):
    legacy_id, _ = legacy_template(app)
    condition_id = _make_template(app, name="New condition template")
    html = admin_client.get("/events/").data.decode()
    assert f"/events/create-from-template/{legacy_id}" not in html
    assert f"/events/create-from-template/{condition_id}" in html
    assert admin_client.get(f"/events/create-from-template/{condition_id}").status_code == 200
    assert admin_client.get(f"/templates/{condition_id}").status_code == 200
    html = admin_client.get("/templates/").data.decode()
    assert "Legacy reference" in html and "New condition template" in html
    assert f"/events/create-from-template/{legacy_id}" not in html


def test_legacy_reference_requires_template_permissions(app, member_client):
    legacy_id, _ = legacy_template(app)
    assert member_client.get(f"/templates/{legacy_id}").status_code == 403
    assert member_client.post(f"/templates/{legacy_id}/delete").status_code == 403
