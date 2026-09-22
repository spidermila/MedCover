"""Tests for the optional Azure Monitor telemetry hook in the app factory."""

import sys
import types

import app as app_package
from app import configure_telemetry

ENV_VAR = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def _fake_azure_module(calls: list) -> types.ModuleType:
    """Stand-in for azure.monitor.opentelemetry recording configure calls."""
    module = types.ModuleType("azure.monitor.opentelemetry")
    module.configure_azure_monitor = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    return module


class _FakeCredential:
    """Stand-in for azure.identity.ManagedIdentityCredential."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id


def _install_fake(monkeypatch, calls: list) -> None:
    identity = types.ModuleType("azure.identity")
    identity.ManagedIdentityCredential = _FakeCredential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", _fake_azure_module(calls))
    monkeypatch.setattr(app_package, "_telemetry_configured", False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)


def test_no_connection_string_is_a_noop(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.delenv(ENV_VAR, raising=False)

    assert configure_telemetry() is False
    assert calls == []


def test_connection_string_starts_the_exporter(monkeypatch):
    """Without a managed identity the exporter uses the connection string's key."""
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")

    assert configure_telemetry() is True
    assert calls == [{}]


def test_managed_identity_authenticates_the_exporter(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")

    assert configure_telemetry() is True
    assert len(calls) == 1
    credential = calls[0]["credential"]
    assert isinstance(credential, _FakeCredential)
    assert credential.client_id == "11111111-1111-1111-1111-111111111111"


def test_blank_client_id_keeps_key_auth(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AZURE_CLIENT_ID", " \n")

    assert configure_telemetry() is True
    assert calls == [{}]


def test_service_principal_keeps_key_auth(monkeypatch):
    """AZURE_CLIENT_ID with a client secret is a service principal, not a managed identity."""
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")

    assert configure_telemetry() is True
    assert calls == [{}]


def test_exporter_is_started_only_once(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")

    assert configure_telemetry() is True
    assert configure_telemetry() is False
    assert len(calls) == 1


def test_missing_package_disables_telemetry(monkeypatch, caplog):
    """A connection string without the telemetry deps installed must not break startup."""
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", None)
    monkeypatch.setattr(app_package, "_telemetry_configured", False)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")

    with caplog.at_level("WARNING"):
        assert configure_telemetry() is False
    assert "telemetry dependencies" in caplog.text


def test_missing_identity_package_disables_telemetry(monkeypatch, caplog):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")

    with caplog.at_level("WARNING"):
        assert configure_telemetry() is False
    assert calls == []
    assert "telemetry dependencies" in caplog.text
