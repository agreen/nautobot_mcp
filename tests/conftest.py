"""Shared fixtures: every test gets its own data directory and a fresh instance claim."""

import pytest


def _reset_path_state():
    from nautobot_mcp.utils import path

    if path._lock_handle is not None:
        path._lock_handle.close()
    path._lock_handle = None
    path._state_root = None


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Never touch the real ~/.local/share/nautobot-mcp, and never leak the claimed root."""
    monkeypatch.setenv("NAUTOBOT_MCP_DATA_DIR", str(tmp_path / "data"))
    _reset_path_state()
    yield
    _reset_path_state()
