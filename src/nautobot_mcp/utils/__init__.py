"""Utility package.

Exports are resolved lazily (PEP 562) so importing a light submodule such as
``nautobot_mcp.utils.config`` does not pull in heavy dependencies (chromadb, onnxruntime).
Those imports used to run before the MCP ``initialize`` handshake could be answered.
"""

import importlib
from typing import Any

# ``config`` is imported eagerly (it is cheap, and a submodule named ``config`` would
# otherwise shadow a lazily resolved ``config`` object). Everything else is lazy.
from .config import config

_EXPORTS = {
    "LocalEmbeddingFunction": (
        "nautobot_mcp.utils.embedding",
        "LocalEmbeddingFunction",
    ),
    "get_chroma_db_path": ("nautobot_mcp.utils.path", "get_chroma_db_path"),
    "GitRepoManager": ("nautobot_mcp.utils.git_manager", "GitRepoManager"),
    "RepositoryConfig": ("nautobot_mcp.utils.repo_config", "RepositoryConfig"),
    "RepositoryConfigManager": (
        "nautobot_mcp.utils.repo_config",
        "RepositoryConfigManager",
    ),
}

__all__ = ["config", *_EXPORTS]


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value
