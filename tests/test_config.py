"""Configuration is plain environment variables (no environment-name indirection)."""

import importlib.util
import sys

import pytest


def _reload_config(monkeypatch, **env):
    """Evaluate a *separate copy* of the config module under the given environment.

    (Reloading the real module would replace the shared ``config`` object that the rest of
    the package already holds references to.)
    """
    for key in (
        "NAUTOBOT_URL",
        "NAUTOBOT_TOKEN",
        "SSL_VERIFY",
        "NAUTOBOT_MCP_ENABLE_KB",
        "NAUTOBOT_MCP_READY_WAIT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import nautobot_mcp.utils.config  # noqa: F401  (ensures the real module is loaded)

    real = sys.modules[
        "nautobot_mcp.utils.config"
    ]  # `nautobot_mcp.utils.config` attr is the instance
    spec = importlib.util.spec_from_file_location("_config_under_test", real.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.config


@pytest.mark.unit
def test_defaults(monkeypatch):
    cfg = _reload_config(monkeypatch)
    assert cfg.NAUTOBOT_URL == "http://localhost:8080"
    assert cfg.NAUTOBOT_BASE_URL == "http://localhost:8080/api"
    assert cfg.GLOBAL_TOOL_PROMPT == "http://localhost:8080/api/swagger.json"
    assert cfg.NAUTOBOT_TOKEN == ""
    assert cfg.SSL_VERIFY is True
    assert cfg.ENABLE_KB is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "given",
    ["https://nb.example.com", "https://nb.example.com/", "https://nb.example.com/api"],
)
def test_url_is_normalized(monkeypatch, given):
    cfg = _reload_config(monkeypatch, NAUTOBOT_URL=given)
    assert cfg.NAUTOBOT_URL == "https://nb.example.com"
    assert (
        cfg.get_full_url("/dcim/devices/") == "https://nb.example.com/api/dcim/devices/"
    )


@pytest.mark.unit
def test_token_header(monkeypatch):
    cfg = _reload_config(monkeypatch, NAUTOBOT_TOKEN="abc123")
    assert cfg.get_headers()["Authorization"] == "Token abc123"
    cfg = _reload_config(monkeypatch)
    assert "Authorization" not in cfg.get_headers()


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected", [("false", False), ("0", False), ("TRUE", True)]
)
def test_boolean_flags(monkeypatch, value, expected):
    cfg = _reload_config(monkeypatch, SSL_VERIFY=value, NAUTOBOT_MCP_ENABLE_KB=value)
    assert cfg.SSL_VERIFY is expected
    assert cfg.ENABLE_KB is expected


@pytest.mark.unit
def test_ready_wait_setting(monkeypatch):
    assert _reload_config(monkeypatch).READY_WAIT == 20.0
    assert _reload_config(monkeypatch, NAUTOBOT_MCP_READY_WAIT="0").READY_WAIT == 0.0
