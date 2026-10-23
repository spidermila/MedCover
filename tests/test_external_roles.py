"""External is an exclusive role across individual and bulk administration."""

import pytest

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.role import ROLE_PERMISSIONS, Role, exclusive_roles
from app.models.user import UserAccount
from app.routes.users import _apply_role_update
from tests.conftest import _make_user


def role_ids(*names):
    return list(db.session.scalars(db.select(Role.id).where(Role.name.in_(names))))


@pytest.mark.parametrize(
    ("names", "kept", "dropped"),
    [
        ([], [], []),
        ([Role.MEMBER], [Role.MEMBER], []),
        ([Role.EXTERNAL], [Role.EXTERNAL], []),
        ([Role.MEMBER, Role.COORDINATOR], [Role.MEMBER, Role.COORDINATOR], []),
        ([Role.COORDINATOR, Role.EXTERNAL, Role.ADMIN], [Role.EXTERNAL], [Role.ADMIN, Role.COORDINATOR]),
    ],
)
def test_exclusive_roles_keeps_external_alone(names, kept, dropped):
    roles, left_out = exclusive_roles([Role(name=name) for name in names])
    assert [role.name for role in roles] == kept
    assert left_out == dropped


def user_audits():
    return db.session.scalar(
        db.select(db.func.count()).select_from(AuditLogEntry).where(AuditLogEntry.entity_type == "UserAccount")
    )


@pytest.mark.parametrize("other_role", [name for name in ROLE_PERMISSIONS if name != Role.EXTERNAL])
def test_create_rejects_external_with_any_other_role(app, admin_client, other_role):
    with app.app_context():
        selected = role_ids(Role.EXTERNAL, other_role)
        before = user_audits()
    response = admin_client.post(
        "/users/create", data={"name": "Exclusive Target", "email": "target@test.com", "role_ids": selected}
    )
    assert response.status_code == 200
    assert "Role External nesmí být kombinována" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(db.select(UserAccount).where(UserAccount.email == "target@test.com")) is None
        assert user_audits() == before


def test_invalid_edit_rolls_back_info_roles_and_audit(app, admin_client):
    with app.app_context():
        user = _make_user("target@test.com", "Exclusive Target", Role.MEMBER)
        user_id, version = user.id, user.version
        selected = role_ids(Role.EXTERNAL, Role.MEMBER)
        before = user_audits()
    response = admin_client.post(
        f"/users/{user_id}/save",
        data={
            "name": "Changed Target",
            "email": "changed@test.com",
            "phone": "123456789",
            "new_password": "changedpass123",
            "role_ids": selected,
            "version": version,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Role External nesmí být kombinována" in response.get_data(as_text=True)
    with app.app_context():
        user = db.session.get(UserAccount, user_id)
        assert (user.name, user.email, user.phone, user.version) == (
            "Exclusive Target",
            "target@test.com",
            None,
            version,
        )
        assert [role.name for role in user.roles] == [Role.MEMBER]
        assert user.check_password("testpass123")
        assert user_audits() == before


def test_role_helper_validates_before_mutation_or_audit(app):
    with app.app_context():
        user = _make_user("target@test.com", "Exclusive Target", Role.MEMBER)
        before = user_audits()
        with pytest.raises(ValueError, match="External"):
            _apply_role_update(user, role_ids(Role.EXTERNAL, Role.VIEWER))
        assert [role.name for role in user.roles] == [Role.MEMBER]
        assert user_audits() == before


@pytest.mark.parametrize(
    "before_roles,after_roles",
    [
        ([Role.MEMBER], [Role.EXTERNAL]),
        ([Role.EXTERNAL], [Role.MEMBER, Role.VIEWER]),
        ([Role.MEMBER], [Role.COORDINATOR, Role.DEBRIEFING_MANAGER]),
    ],
)
def test_explicit_edit_can_switch_role_sets(app, admin_client, before_roles, after_roles):
    with app.app_context():
        user = _make_user("target@test.com", "Exclusive Target", before_roles[0])
        user_id, version = user.id, user.version
        selected = role_ids(*after_roles)
    assert (
        admin_client.post(
            f"/users/{user_id}/save",
            data={"name": "Exclusive Target", "email": "target@test.com", "role_ids": selected, "version": version},
        ).status_code
        == 302
    )
    with app.app_context():
        user = db.session.get(UserAccount, user_id)
        assert {role.name for role in user.roles} == set(after_roles)
        assert user.is_external is (after_roles == [Role.EXTERNAL])
        if Role.DEBRIEFING_MANAGER in after_roles:
            assert user.has_any_permission("event.edit")
            assert user.has_permission("debriefing.manage")


@pytest.mark.parametrize("added,existing", [(Role.EXTERNAL, Role.MEMBER), (Role.VIEWER, Role.EXTERNAL)])
def test_bulk_add_rejects_entire_mixed_batch(app, admin_client, added, existing):
    with app.app_context():
        valid = _make_user("valid@test.com", "Roleless", Role.MEMBER)
        valid.roles = []
        db.session.commit()
        invalid = _make_user("invalid@test.com", "Exclusive Target", existing)
        valid_id, invalid_id = valid.id, invalid.id
        selected_role = role_ids(added)[0]
        before = user_audits()
    response = admin_client.post(
        "/users/batch",
        data={"user_ids": [str(valid_id), str(invalid_id)], "action": "add_role", "role_id": selected_role},
        follow_redirects=True,
    )
    assert "Nebyl upraven žádný uživatel" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(UserAccount, valid_id).roles == []
        assert [role.name for role in db.session.get(UserAccount, invalid_id).roles] == [existing]
        assert user_audits() == before


def test_bulk_can_add_and_remove_external_on_roleless_account(app, admin_client):
    with app.app_context():
        user = _make_user("target@test.com", "Exclusive Target", Role.MEMBER)
        user.roles = []
        db.session.commit()
        user_id = user.id
        external_id = role_ids(Role.EXTERNAL)[0]
        member_id = role_ids(Role.MEMBER)[0]
    data = {"user_ids": [str(user_id)], "role_id": external_id}
    for action in ["add_role", "add_role"]:
        assert admin_client.post("/users/batch", data={**data, "action": action}).status_code == 302
        with app.app_context():
            assert [role.name for role in db.session.get(UserAccount, user_id).roles] == [Role.EXTERNAL]
    admin_client.post("/users/batch", data={**data, "action": "remove_role"})
    admin_client.post("/users/batch", data={**data, "action": "add_role", "role_id": member_id})
    with app.app_context():
        assert [role.name for role in db.session.get(UserAccount, user_id).roles] == [Role.MEMBER]


@pytest.mark.parametrize("url", ["/users/create", "/users/"])
def test_role_forms_explain_external_exclusivity(admin_client, url):
    response = admin_client.get(url)
    assert response.status_code == 200
    assert "External" in response.get_data(as_text=True)
    assert "kombinovat" in response.get_data(as_text=True)


def test_manual_create_accepts_external_alone(app, admin_client):
    with app.app_context():
        selected = role_ids(Role.EXTERNAL)
    response = admin_client.post(
        "/users/create", data={"name": "Exclusive Target", "email": "target@test.com", "role_ids": selected}
    )
    assert response.status_code == 302
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "target@test.com"))
        assert user.is_external
        assert [role.name for role in user.roles] == [Role.EXTERNAL]
