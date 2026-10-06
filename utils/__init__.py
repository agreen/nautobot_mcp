"""Utility package.

Exports are resolved lazily (PEP 562) so that importing a light submodule such as
``utils.config`` does not pull in heavy dependencies (torch / sentence-transformers).
Those imports used to run before the MCP ``initialize`` handshake could be answered.
"""

import importlib
from typing import Any

# ``config`` is imported eagerly (it is cheap, and a submodule named ``config`` would
# otherwise shadow a lazily resolved ``config`` object). Everything else is lazy.
from .config import config

_EXPORTS = {
    "SentenceTransformerEmbeddingFunction": (
        "utils.embedding",
        "SentenceTransformerEmbeddingFunction",
    ),
    "get_chroma_db_path": ("utils.path", "get_chroma_db_path"),
    "GitRepoManager": ("utils.git_manager", "GitRepoManager"),
    "RepositoryConfig": ("utils.repo_config", "RepositoryConfig"),
    "RepositoryConfigManager": ("utils.repo_config", "RepositoryConfigManager"),
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
