"""Backward-compatible HTTP entrypoint.

The implementation lives in ``main.py`` (``python main.py --mode http`` is equivalent).
"""

import asyncio
import os

from main import ServerState, build_http_app, serve_http

state = ServerState()
mcp_app = build_http_app(state)


async def main():
    await serve_http(
        mcp_app,
        state,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8000")),
    )


if __name__ == "__main__":
    asyncio.run(main())
