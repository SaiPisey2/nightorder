"""Startup configuration checks.

The gate-token secret has a built-in development default so `make infra` works
with no setup. That default is in the published source, so a deployment that
enforces auth while still using it can have approval links forged.
"""
import pytest

from nightorder.config import (
    DEV_GATE_TOKEN_SECRET,
    InsecureConfiguration,
    check_runtime_config,
)

REAL_SECRET = "s7Qx0_not-the-built-in-value_Kz2"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("NIGHTORDER_AUTH", "NIGHTORDER_GATE_TOKEN_SECRET", "NIGHTORDER_ADMIN_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_auth_on_with_the_built_in_secret_is_refused(monkeypatch):
    monkeypatch.setenv("NIGHTORDER_AUTH", "on")
    monkeypatch.setenv("NIGHTORDER_GATE_TOKEN_SECRET", DEV_GATE_TOKEN_SECRET)
    with pytest.raises(InsecureConfiguration) as e:
        check_runtime_config()
    assert "NIGHTORDER_GATE_TOKEN_SECRET" in str(e.value)


def test_auth_on_with_the_secret_left_unset_is_refused(monkeypatch):
    """Unset resolves to the built-in default, so it must refuse identically."""
    monkeypatch.setenv("NIGHTORDER_AUTH", "on")
    with pytest.raises(InsecureConfiguration):
        check_runtime_config()


def test_auth_on_with_a_real_secret_but_no_admin_key_warns(monkeypatch):
    monkeypatch.setenv("NIGHTORDER_AUTH", "on")
    monkeypatch.setenv("NIGHTORDER_GATE_TOKEN_SECRET", REAL_SECRET)
    warnings = check_runtime_config()
    assert any("NIGHTORDER_ADMIN_KEY" in w for w in warnings)


def test_auth_on_fully_configured_is_silent(monkeypatch):
    monkeypatch.setenv("NIGHTORDER_AUTH", "on")
    monkeypatch.setenv("NIGHTORDER_GATE_TOKEN_SECRET", REAL_SECRET)
    monkeypatch.setenv("NIGHTORDER_ADMIN_KEY", "admin")
    assert check_runtime_config() == []


def test_open_mode_says_so_and_does_not_refuse():
    warnings = check_runtime_config()
    assert any("NIGHTORDER_AUTH=off" in w for w in warnings)
    assert any("development gate-token secret" in w for w in warnings)


def test_open_mode_with_a_real_secret_warns_only_about_being_open(monkeypatch):
    monkeypatch.setenv("NIGHTORDER_GATE_TOKEN_SECRET", REAL_SECRET)
    warnings = check_runtime_config()
    assert any("NIGHTORDER_AUTH=off" in w for w in warnings)
    assert not any("development gate-token secret" in w for w in warnings)


@pytest.mark.parametrize("value", ["ON", "On", "on"])
def test_auth_flag_is_case_insensitive(monkeypatch, value):
    monkeypatch.setenv("NIGHTORDER_AUTH", value)
    with pytest.raises(InsecureConfiguration):
        check_runtime_config()
