"""Locations of the server's writable state.

Everything lives under one data directory so that it can be mounted/persisted easily:

    <data dir>/chroma/                 ChromaDB indexes
    <data dir>/repos/                  cloned knowledge-base repositories
    <data dir>/models/                 embedding model (downloaded on first use)
    <data dir>/user_repositories.json  repositories added through the MCP tools

``NAUTOBOT_MCP_DATA_DIR`` overrides the default (``$XDG_DATA_HOME/nautobot-mcp``,
i.e. ``~/.local/share/nautobot-mcp``). The installed package directory is never written to.

ChromaDB's persistent stores and git clones must not be shared by several processes. When
more than one server instance runs against the same data directory (some MCP hosts start
one process per session), the first instance owns ``chroma/`` and ``repos/`` and later
ones are moved to private directories under ``<data dir>/instances/<pid>`` by
``claim_state_root()``; the (read-only) embedding model stays shared.
"""

import atexit
import logging
import os
import shutil
from pathlib import Path
from typing import IO, Optional

from nautobot_mcp.utils.locks import try_lock

logger = logging.getLogger("nautobot_mcp")

_state_root: Optional[Path] = None  # where chroma/ and repos/ live for this process
_lock_handle: Optional[IO] = None  # keeps the owner lock for the process lifetime


def get_data_dir() -> Path:
    override = os.getenv("NAUTOBOT_MCP_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "nautobot-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _purge_stale_instances(instances: Path) -> None:
    """Remove private state left behind by instances that are no longer running."""
    if not instances.is_dir():
        return
    for child in instances.iterdir():
        if child.name.isdigit() and not _pid_alive(int(child.name)):
            shutil.rmtree(child, ignore_errors=True)


def claim_state_root() -> dict:
    """Decide (once per process) where this instance keeps ChromaDB and cloned repos.

    Returns ``{"shared": bool, "root": str}``. Safe to call repeatedly.
    """
    global _state_root, _lock_handle
    data_dir = get_data_dir()
    if _state_root is None:
        _purge_stale_instances(data_dir / "instances")
        handle = try_lock(data_dir / "instance.lock")
        if handle is not None:
            _lock_handle = handle
            _state_root = data_dir
        else:
            private = data_dir / "instances" / str(os.getpid())
            private.mkdir(parents=True, exist_ok=True)
            _state_root = private
            atexit.register(shutil.rmtree, private, True)
            logger.warning(
                "Another nautobot-mcp instance is using %s; this instance keeps a private "
                "index in %s (first start is slower, and it is removed on exit).",
                data_dir,
                private,
            )
    return {"shared": _state_root == data_dir, "root": str(_state_root)}


def _root() -> Path:
    return _state_root if _state_root is not None else get_data_dir()


def _subdir(base: Path, name: str) -> str:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_chroma_db_path() -> str:
    """Directory holding the ChromaDB databases."""
    return _subdir(_root(), "chroma")


def get_repo_cache_dir() -> str:
    """Directory holding cloned knowledge-base repositories."""
    return _subdir(_root(), "repos")


def get_model_dir() -> str:
    """Directory holding the embedding model (always shared between instances)."""
    return _subdir(get_data_dir(), "models")
