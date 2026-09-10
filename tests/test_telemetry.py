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


def _install_fake(monkeypatch, calls: list) -> None:
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", _fake_azure_module(calls))
    monkeypatch.setattr(app_package, "_telemetry_configured", False)


def test_no_connection_string_is_a_noop(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.delenv(ENV_VAR, raising=False)

    assert configure_telemetry() is False
    assert calls == []


def test_connection_string_starts_the_exporter(monkeypatch):
    calls: list = []
    _install_fake(monkeypatch, calls)
    monkeypatch.setenv(ENV_VAR, "InstrumentationKey=00000000-0000-0000-0000-000000000000")

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
    assert "azure-monitor-opentelemetry is not installed" in caplog.text
