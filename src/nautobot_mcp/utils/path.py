"""Locations of the server's writable state.

Everything lives under one data directory so that it can be mounted/persisted easily:

    <data dir>/chroma/                 ChromaDB indexes
    <data dir>/repos/                  cloned knowledge-base repositories
    <data dir>/models/                 embedding model (downloaded on first use)
    <data dir>/user_repositories.json  repositories added through the MCP tools

``NAUTOBOT_MCP_DATA_DIR`` overrides the default (``$XDG_DATA_HOME/nautobot-mcp``,
i.e. ``~/.local/share/nautobot-mcp``). The installed package directory is never written to.
"""

import os
from pathlib import Path


def get_data_dir() -> Path:
    override = os.getenv("NAUTOBOT_MCP_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "nautobot-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _subdir(name: str) -> str:
    path = get_data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_chroma_db_path() -> str:
    """Directory holding the ChromaDB databases."""
    return _subdir("chroma")


def get_repo_cache_dir() -> str:
    """Directory holding cloned knowledge-base repositories."""
    return _subdir("repos")


def get_model_dir() -> str:
    """Directory holding the embedding model."""
    return _subdir("models")
