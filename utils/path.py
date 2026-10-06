# nautobot_mcp/utils/path.py

import os
from pathlib import Path


def get_chroma_db_path() -> str:
    """Ensure and return the directory that holds the ChromaDB databases.

    Set ``NAUTOBOT_MCP_DATA_DIR`` to relocate it (e.g. to a Docker volume). Otherwise it
    defaults to ``<project root>/backend/nautobot_mcp/db``, regardless of what the
    checkout directory is called (inside a container it is simply ``/app``).
    """
    override = os.getenv("NAUTOBOT_MCP_DATA_DIR")
    if override:
        db_path = Path(override)
    else:
        db_path = (
            Path(__file__).resolve().parent.parent / "backend" / "nautobot_mcp" / "db"
        )
    db_path.mkdir(parents=True, exist_ok=True)
    return str(db_path)
