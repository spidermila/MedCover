"""Development quick login exposes the dedicated External account only when enabled."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from app.models.role import Role
from app.models.user import UserAccount
from app.routes import dev, register_blueprints


@pytest.mark.parametrize("enabled", [False, True])
def test_external_dev_account_button_and_login(monkeypatch, enabled):
    account = next(account for account in dev.DEV_ACCOUNTS if account["role"] == "external")
    user = UserAccount(id=uuid4(), email=account["email"], name=account["name"], is_active=True)
    user.roles = [Role(name=Role.EXTERNAL)]
    app = Flask(__name__)
    app.config.update(SECRET_KEY="dev-login-test", DEV_LOGIN_ENABLED=enabled, TESTING=True)
    manager = LoginManager(app)
    manager.user_loader(lambda user_id: user if user_id == str(user.id) else None)
    app.jinja_loader = ChoiceLoader(
        [
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(Path(__file__).resolve().parent.parent / "app/templates"),
        ]
    )
    app.jinja_env.globals["csrf_token"] = lambda: "test-token"
    register_blueprints(app)
    lookup = Mock(return_value=user)
    monkeypatch.setattr(dev, "db", SimpleNamespace(select=sa.select, session=SimpleNamespace(scalar=lookup)))
    client = app.test_client()

    login_page = client.get("/auth/login")
    assert login_page.status_code == 200
    assert (b"/dev/login-as/external" in login_page.data) is enabled
    response = client.post("/dev/login-as/external")
    assert response.status_code == (302 if enabled else 404)
    if enabled:
        assert list(lookup.call_args.args[0].compile().params.values()) == [account["email"]]
        with client.session_transaction() as session:
            assert session["_user_id"] == str(user.id)
        assert user.is_external
        assert not user.has_permission("user.view")
    else:
        lookup.assert_not_called()
