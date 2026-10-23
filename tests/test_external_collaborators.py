"""External collaborator access, onboarding, and opportunities."""

import pytest

from app.extensions import db
from app.models.invite import RegistrationInvite
from app.models.role import Role
from app.models.user import UserAccount


def test_external_identity_and_permissions(app, external_client):
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "external@test.com"))
        assert user.is_external
        assert user.has_permission("event.assign_own")
        assert user.has_any_permission("report.view", "equipment.view")
        assert not user.has_any_permission("event.view_draft", "work_report.generate_any", "admin.view")


@pytest.mark.parametrize("external", [False, True])
def test_invitation_grants_selected_role(app, admin_client, client, external):
    response = admin_client.post(
        "/users/invites/create", data={"email": "invite@test.com", "is_external": "on" if external else ""}
    )
    assert response.status_code == 302
    with app.app_context():
        invite = db.session.scalar(db.select(RegistrationInvite).where(RegistrationInvite.email == "invite@test.com"))
        assert invite.is_external is external
        token = invite.token
    client.get("/auth/logout")
    response = client.post(
        f"/auth/register/{token}",
        data={
            "full_name": "Friendly Collaborator",
            "password": "testpass123",
            "password2": "testpass123",
            "role": Role.ADMIN,
            "is_external": "",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "invite@test.com"))
        assert [role.name for role in user.roles] == [Role.EXTERNAL if external else Role.MEMBER]


@pytest.mark.parametrize("url", ["/users/", "/master-events/", "/admin/"])
def test_external_cannot_access_internal_sections(external_client, url):
    assert external_client.get(url).status_code == 403


def test_external_can_use_own_profile(external_client):
    assert external_client.get("/users/profile").status_code == 200


def test_external_cannot_create_invites(external_client):
    assert (
        external_client.post("/users/invites/create", data={"email": "other@test.com", "is_external": "on"}).status_code
        == 403
    )
