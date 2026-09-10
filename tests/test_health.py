"""Tests for the /health endpoint."""

from unittest.mock import patch

from app.extensions import db


def test_health_returns_200(client):
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_json(client):
    response = client.get("/health")
    data = response.get_json()
    assert data is not None
    assert "status" in data


def test_health_accessible_without_login(client):
    """Health endpoint must return 200 without authentication (Docker healthcheck)."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_has_no_detail_key_when_ok(client):
    """A healthy response should not include an error detail field."""
    response = client.get("/health")
    data = response.get_json()
    assert "detail" not in data


def test_health_db_ok_returns_ok_status(client):
    response = client.get("/health")
    data = response.get_json()
    # In a test environment with a working DB, status should be 'ok'
    assert data["status"] == "ok"


def test_setup_guard_rolls_back_after_settings_db_error(app):
    """A swallowed setup lookup failure must not poison the request session."""
    setup_guard = next(fn for fn in app.before_request_funcs[None] if fn.__name__ == "_setup_guard")

    with app.test_request_context("/"):
        with (
            patch("app.models.settings.get_settings", side_effect=Exception("DB unavailable")),
            patch.object(db.session, "rollback") as rollback,
        ):
            assert setup_guard() is None

    rollback.assert_called_once_with()
