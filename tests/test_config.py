"""Configuration is plain environment variables (no environment-name indirection)."""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_config_module(monkeypatch):
    """Reload the config module again after each test, once env changes are undone."""
    yield
    monkeypatch.undo()
    import sys

    importlib.reload(sys.modules["nautobot_mcp.utils.config"])


def _reload_config(monkeypatch, **env):
    for key in (
        "NAUTOBOT_URL",
        "NAUTOBOT_TOKEN",
        "SSL_VERIFY",
        "NAUTOBOT_MCP_ENABLE_KB",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import sys

    module = sys.modules["nautobot_mcp.utils.config"]
    module = importlib.reload(module)
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
