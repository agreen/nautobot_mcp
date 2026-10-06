"""Unified entrypoint for Nautobot MCP Server.

Supports stdio and HTTP (streamable-http) transports, selected via CLI args or env vars.

Startup is deliberately non-blocking: the transport starts serving (and answers the MCP
``initialize`` handshake) immediately, while the embedding model, Swagger index and
knowledge-base index are built in the background (see ``helpers/startup.py``). Tools that
need an index return a structured ``not_ready`` status until it is available.

Usage:
    # stdio mode (default)
    python main.py --mode stdio

    # HTTP mode
    python main.py --mode http --host 0.0.0.0 --port 8000

    # Using environment variables
    MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000 python main.py
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

import urllib3

from helpers.startup import ENDPOINT_INDEX, KB_INDEX, ServerState
from helpers.tool_handlers import (
    handle_add_repo,
    handle_api_request_schema,
    handle_dynamic_api_request,
    handle_init_repos,
    handle_kb_semantic_search,
    handle_list_repos,
    handle_refresh_endpoint_index,
    handle_remove_repo,
    handle_repo_status,
    handle_update_repos,
)
from utils.config import config

# Disable SSL warnings if SSL verification is disabled
if not config.SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging with safe fallback for invalid LOG_LEVEL.
# (logging goes to stderr, which keeps stdout clean for the stdio protocol)
_log_level_name = str(config.LOG_LEVEL).upper()
_log_level = getattr(logging, _log_level_name, None)
if not isinstance(_log_level, int):
    logging.basicConfig(level=logging.INFO)
    logging.warning(
        "Invalid LOG_LEVEL '%s' in configuration; falling back to INFO.",
        config.LOG_LEVEL,
    )
else:
    logging.basicConfig(level=_log_level)
logger = logging.getLogger("nautobot_mcp")

# Set external service configurations
os.environ["POSTHOG_API_KEY"] = config.POSTHOG_API_KEY


async def run_stdio_mode():
    """Run the MCP server in stdio transport mode."""
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions

    from helpers.tool_definitions import (
        get_add_repo_description,
        get_add_repo_input_schema,
        get_api_request_schema_description,
        get_api_request_schema_input_schema,
        get_dynamic_api_request_description,
        get_dynamic_api_request_input_schema,
        get_init_repos_description,
        get_init_repos_input_schema,
        get_kb_semantic_search_description,
        get_kb_semantic_search_input_schema,
        get_list_repos_description,
        get_list_repos_input_schema,
        get_refresh_endpoint_index_description,
        get_refresh_endpoint_index_input_schema,
        get_remove_repo_description,
        get_remove_repo_input_schema,
        get_repo_status_description,
        get_repo_status_input_schema,
        get_update_repos_description,
        get_update_repos_input_schema,
    )
    from helpers.tool_handlers import (
        handle_add_repo,
        handle_api_request_schema,
        handle_dynamic_api_request,
        handle_init_repos,
        handle_kb_semantic_search,
        handle_list_repos,
        handle_refresh_endpoint_index,
        handle_remove_repo,
        handle_repo_status,
        handle_update_repos,
    )

    logger.info("Starting MCP Server in stdio mode...")

    # Heavy components are built in the background; see ServerState.
    state = ServerState()

    server = Server(config.API_PREFIX)

    global_tool_prompt = config.GLOBAL_TOOL_PROMPT
    if global_tool_prompt and not global_tool_prompt.endswith(" "):
        global_tool_prompt += " "

    stdio_index_requirements = {
        f"{config.API_PREFIX}_api_request_schema": ENDPOINT_INDEX,
        "refresh_endpoint_index": ENDPOINT_INDEX,
        "nautobot_kb_semantic_search": KB_INDEX,
        "nautobot_kb_update_repos": KB_INDEX,
        "nautobot_kb_init_repos": KB_INDEX,
        "nautobot_kb_repo_status": KB_INDEX,
    }

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            # ---------------------------------------
            # ---------- NB API TOOLS ---------------
            # ---------------------------------------
            types.Tool(
                name=f"{config.API_PREFIX}_api_request_schema",
                description=get_api_request_schema_description(global_tool_prompt),
                inputSchema=get_api_request_schema_input_schema(),
            ),
            types.Tool(
                name="nautobot_dynamic_api_request",
                description=get_dynamic_api_request_description(),
                inputSchema=get_dynamic_api_request_input_schema(),
            ),
            types.Tool(
                name="refresh_endpoint_index",
                description=get_refresh_endpoint_index_description(),
                inputSchema=get_refresh_endpoint_index_input_schema(),
            ),
            # -----------------------------------
            # ---------- KB TOOLS ---------------
            # -----------------------------------
            types.Tool(
                name="nautobot_kb_semantic_search",
                description=get_kb_semantic_search_description(),
                inputSchema=get_kb_semantic_search_input_schema(),
            ),
            # -----------------------------------
            # ------- REPO MANAGEMENT TOOLS ----
            # -----------------------------------
            types.Tool(
                name="nautobot_kb_list_repos",
                description=get_list_repos_description(),
                inputSchema=get_list_repos_input_schema(),
            ),
            types.Tool(
                name="nautobot_kb_add_repo",
                description=get_add_repo_description(),
                inputSchema=get_add_repo_input_schema(),
            ),
            types.Tool(
                name="nautobot_kb_remove_repo",
                description=get_remove_repo_description(),
                inputSchema=get_remove_repo_input_schema(),
            ),
            types.Tool(
                name="nautobot_kb_update_repos",
                description=get_update_repos_description(),
                inputSchema=get_update_repos_input_schema(),
            ),
            types.Tool(
                name="nautobot_kb_init_repos",
                description=get_init_repos_description(),
                inputSchema=get_init_repos_input_schema(),
            ),
            types.Tool(
                name="nautobot_kb_repo_status",
                description=get_repo_status_description(),
                inputSchema=get_repo_status_input_schema(),
            ),
        ]

    @server.call_tool()
    async def handle_invoke_tool(
        name: str, inputs: Dict[str, Any]
    ) -> list[types.TextContent]:
        try:
            index_needed = stdio_index_requirements.get(name)
            if index_needed:
                not_ready = state.not_ready_response(index_needed)
                if not_ready:
                    return [types.TextContent(type="text", text=not_ready)]

            # --------------------------------------------
            # ---------- Call NB API TOOLS ---------------
            # --------------------------------------------
            if name == f"{config.API_PREFIX}_api_request_schema":
                query = inputs["query"]
                n_results = inputs.get("n_results", config.DEFAULT_SEARCH_RESULTS)
                response_text = await handle_api_request_schema(
                    query, n_results, state.endpoint_searcher
                )
                return [types.TextContent(type="text", text=response_text)]

            elif name == "refresh_endpoint_index":
                response_text = await handle_refresh_endpoint_index(
                    state.endpoint_searcher
                )
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_dynamic_api_request":
                method = inputs["method"]
                path = inputs["path"]
                params = inputs.get("params")
                body = inputs.get("body")
                response_text = await handle_dynamic_api_request(
                    method, path, params, body
                )
                return [types.TextContent(type="text", text=response_text)]

            # ----------------------------------------
            # ---------- CALL KB TOOLS ---------------
            # ----------------------------------------
            elif name == "nautobot_kb_semantic_search":
                query = inputs["query"]
                n_results = inputs.get("n_results", 5)
                response_text = await handle_kb_semantic_search(
                    query, n_results, state.nautobot_kb
                )
                return [types.TextContent(type="text", text=response_text)]

            # ----------------------------------------
            # -------- REPO MANAGEMENT TOOLS --------
            # ----------------------------------------
            elif name == "nautobot_kb_list_repos":
                response_text = await handle_list_repos()
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_kb_add_repo":
                repo = inputs["repo"]
                description = inputs.get("description")
                response_text = await handle_add_repo(repo, description)
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_kb_remove_repo":
                repo = inputs["repo"]
                response_text = await handle_remove_repo(repo)
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_kb_update_repos":
                repo = inputs.get("repo")
                force = inputs.get("force", False)
                response_text = await handle_update_repos(
                    state.nautobot_kb, repo, force
                )
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_kb_init_repos":
                force = inputs.get("force", False)
                response_text = await handle_init_repos(state.nautobot_kb, force)
                return [types.TextContent(type="text", text=response_text)]

            elif name == "nautobot_kb_repo_status":
                response_text = await handle_repo_status(state.nautobot_kb)
                return [types.TextContent(type="text", text=response_text)]

            else:
                raise ValueError(f"Unknown tool: {name}")

        except Exception as e:
            logger.exception(f"Error invoking tool {name}")
            error_text = json.dumps({"error": str(e)}, indent=2)
            return [types.TextContent(type="text", text=error_text)]

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running with stdio transport")
        # Kick off index building only now; the handshake below is served immediately.
        state.start()
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=config.SERVER_NAME,
                server_version=config.SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def build_http_app(state: ServerState):
    """Create the FastMCP app. Tools read components from ``state`` at call time."""
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp_app = FastMCP(config.SERVER_NAME)

    # Liveness: always 200 once the process is serving (use for container healthchecks).
    @mcp_app.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "healthy", "service": config.SERVER_NAME, **state.snapshot()}
        )

    # Readiness: 503 until the indexes are built.
    @mcp_app.custom_route("/ready", methods=["GET"])
    async def ready_check(request: Request) -> JSONResponse:
        snapshot = state.snapshot()
        return JSONResponse(snapshot, status_code=200 if snapshot["ready"] else 503)

    # API Tools
    @mcp_app.tool()
    async def mcp_nautobot_openapi_api_request_schema(
        query: str, n_results: int = 5
    ) -> str:
        """Search for Nautobot API endpoints using natural language. Returns endpoint paths, methods, parameters, and response formats. Use this before making API requests to find the correct endpoint."""
        return state.not_ready_response(
            ENDPOINT_INDEX
        ) or await handle_api_request_schema(query, n_results, state.endpoint_searcher)

    @mcp_app.tool()
    async def mcp_nautobot_dynamic_api_request(
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> str:
        """Execute HTTP requests to Nautobot API for CRUD operations. Use GET to retrieve data, POST to create, PUT/PATCH to update, DELETE to remove. Query the API schema tool first to find correct endpoints and parameters. GET/DELETE use 'params', POST/PUT/PATCH use 'body'."""
        return await handle_dynamic_api_request(method, path, params, body)

    @mcp_app.tool()
    async def mcp_refresh_endpoint_index() -> str:
        """Manually refresh the OpenAPI endpoint index from the latest Nautobot schema."""
        return state.not_ready_response(
            ENDPOINT_INDEX
        ) or await handle_refresh_endpoint_index(state.endpoint_searcher)

    # Knowledge Base Tools
    @mcp_app.tool()
    async def mcp_nautobot_kb_semantic_search(query: str, n_results: int = 5) -> str:
        """Search Nautobot GitHub repositories for code examples, best practices, and documentation. Use for: Jobs/Apps/Plugin examples, implementation patterns, API usage, feature guidance. Returns code snippets and docs with source attribution. Results truncated to 300 chars."""
        return state.not_ready_response(KB_INDEX) or await handle_kb_semantic_search(
            query, n_results, state.nautobot_kb
        )

    # Repository Management Tools
    @mcp_app.tool()
    async def mcp_nautobot_kb_list_repos() -> str:
        """List all repositories configured in the Nautobot knowledge base with their metadata."""
        return await handle_list_repos()

    @mcp_app.tool()
    async def mcp_nautobot_kb_add_repo(
        repo: str, description: Optional[str] = None
    ) -> str:
        """Add a new GitHub repository to the Nautobot knowledge base for indexing and search."""
        return await handle_add_repo(repo, description)

    @mcp_app.tool()
    async def mcp_nautobot_kb_remove_repo(repo: str) -> str:
        """Remove a repository from the Nautobot knowledge base configuration."""
        return await handle_remove_repo(repo)

    @mcp_app.tool()
    async def mcp_nautobot_kb_update_repos(
        repo: Optional[str] = None, force: bool = False
    ) -> str:
        """Update repository indexes in the knowledge base. Specify a repo to update one, or omit to update all. Use force=true to reindex even if unchanged."""
        return state.not_ready_response(KB_INDEX) or await handle_update_repos(
            state.nautobot_kb, repo, force
        )

    @mcp_app.tool()
    async def mcp_nautobot_kb_init_repos(force: bool = False) -> str:
        """Initialize all repositories in the knowledge base. Use force=true to reindex all repos."""
        return state.not_ready_response(KB_INDEX) or await handle_init_repos(
            state.nautobot_kb, force
        )

    @mcp_app.tool()
    async def mcp_nautobot_kb_repo_status() -> str:
        """Show repository status including document counts, indexing status, and configuration."""
        return state.not_ready_response(KB_INDEX) or await handle_repo_status(
            state.nautobot_kb
        )

    return mcp_app


async def serve_http(
    mcp_app, state: ServerState, host: str = "127.0.0.1", port: int = 8000
) -> None:
    """Serve ``mcp_app`` over streamable-http, building indexes in the background."""
    state.start()  # returns immediately; the server below starts accepting requests now
    try:
        await mcp_app.run_async(transport="streamable-http", host=host, port=port)
    finally:
        await state.stop()


async def run_http_mode(port: int = 8000, host: str = "127.0.0.1"):
    """Run the MCP server in HTTP transport mode using FastMCP.

    Args:
        port: Port number to listen on (default: 8000)
        host: Interface to bind (default: 127.0.0.1; use 0.0.0.0 inside a container)
    """
    logger.info(f"Starting MCP Server in HTTP mode on {host}:{port}...")
    state = ServerState()
    await serve_http(build_http_app(state), state, host=host, port=port)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="Nautobot MCP Server - Unified entrypoint for stdio and HTTP modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run in stdio mode (default)
  python main.py
  python main.py --mode stdio

  # Run in HTTP mode on default port (8000)
  python main.py --mode http

  # Bind all interfaces (containers)
  python main.py --mode http --host 0.0.0.0

  # Run in HTTP mode on custom port
  python main.py --mode http --port 9000

  # Using environment variables
  MCP_TRANSPORT=http MCP_PORT=8000 python main.py
        """,
    )

    # Normalize transport mode from environment (case-insensitive)
    transport_default = os.getenv("MCP_TRANSPORT", "stdio")
    if transport_default is not None:
        transport_default = transport_default.lower()

    parser.add_argument(
        "--mode",
        "-m",
        choices=["stdio", "http"],
        default=transport_default,
        help="Transport mode: stdio (default) or http",
    )

    # Validate port from environment before using it
    env_port = os.getenv("MCP_PORT", "8000")
    try:
        default_port = int(env_port)
    except ValueError:
        parser.error(
            f"environment variable MCP_PORT={env_port!r} is not a valid integer"
        )

    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Interface to bind in HTTP mode (default: 127.0.0.1; env MCP_HOST)",
    )

    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=default_port,
        help="Port number for HTTP mode (default: 8000)",
    )

    return parser.parse_args()


async def main():
    """Main entrypoint that dispatches to the appropriate transport mode."""
    args = parse_arguments()

    logger.info(f"Nautobot MCP Server v{config.SERVER_VERSION}")
    logger.info(f"Transport mode: {args.mode}")

    try:
        if args.mode == "stdio":
            await run_stdio_mode()
        elif args.mode == "http":
            await run_http_mode(port=args.port, host=args.host)
        else:
            logger.error(f"Invalid mode: {args.mode}")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception:
        logger.exception("Fatal error in main entrypoint")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
