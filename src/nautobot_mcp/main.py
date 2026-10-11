"""Unified entrypoint for Nautobot MCP Server.

Supports stdio and HTTP (streamable-http) transports, selected via CLI args or env vars.
One FastMCP application serves both, so tools, resources and prompts are identical.

Startup is deliberately non-blocking: the transport starts serving (and answers the MCP
``initialize`` handshake) immediately, while the embedding model, Swagger index and
knowledge-base index are built in the background (see ``helpers/startup.py``). Progress is
reported to clients as MCP log notifications, and index-backed tools wait briefly (reporting
MCP progress) before answering ``not_ready``.

Usage:
    # stdio mode (default)
    nautobot-mcp --mode stdio

    # HTTP mode
    nautobot-mcp --mode http --host 0.0.0.0 --port 8000

    # Using environment variables
    MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000 nautobot-mcp
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Annotated, Any, Optional

import urllib3
from pydantic import Field

from nautobot_mcp.helpers.api_hints import API_CONVENTIONS_MD, endpoint_app
from nautobot_mcp.helpers.nautobot_overview import (
    InstanceInfoCache,
    build_app_endpoints,
    build_overview,
)
from nautobot_mcp.helpers.startup import (
    ENDPOINT_INDEX,
    KB_INDEX,
    ComponentState,
    ServerState,
)
from nautobot_mcp.helpers.tool_definitions import (
    get_add_repo_description,
    get_api_request_schema_description,
    get_dynamic_api_request_description,
    get_init_repos_description,
    get_kb_semantic_search_description,
    get_list_repos_description,
    get_refresh_endpoint_index_description,
    get_remove_repo_description,
    get_repo_status_description,
    get_server_status_description,
    get_update_repos_description,
)
from nautobot_mcp.helpers.tool_handlers import (
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
from nautobot_mcp.utils.config import config

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


# Nautobot's built-in REST API apps. Published as concrete resources up front; apps added by
# plugins are discovered from the schema once the index is ready.
CORE_API_APPS = (
    "circuits",
    "cloud",
    "dcim",
    "extras",
    "ipam",
    "tenancy",
    "users",
    "virtualization",
    "wireless",
)


def _instructions(kb_enabled: bool) -> str:
    """Server instructions, returned to every client in the ``initialize`` result."""
    text = (
        "Nautobot MCP server. To work with Nautobot data: (1) search the API schema with "
        f"`{config.API_PREFIX}_api_request_schema` to find the endpoint, its filters and body "
        "fields; never guess paths or field names; (2) call it with "
        "`nautobot_dynamic_api_request` (paths are relative to /api); (3) confirm with the "
        "user before any write or delete. Read nautobot://api/overview for this instance's "
        "Nautobot version, plugins and API apps, and nautobot://guide/api-conventions for "
        "filtering, paging and write conventions."
    )
    if kb_enabled:
        text += (
            " `nautobot_kb_semantic_search` searches Nautobot's GitHub repositories for "
            "code examples and documentation."
        )
    text += (
        " Search indexes are built in the background after startup (the first run downloads "
        "an embedding model and can take a few minutes). Index-backed tools wait briefly, then "
        'return {"status": "not_ready"} with the progress so far; retry after the suggested '
        "delay. Call `nautobot_server_status` (or read the nautobot://server/status resource) "
        "to see what the server is doing. Tool names may carry a server-name prefix in your "
        "client."
    )
    return text


def _tool_progress(ctx: Any, loop: asyncio.AbstractEventLoop, label: str):
    """Build a thread-safe ``callback(done, total, name="")`` that reports MCP progress."""

    async def send(done: float, total: float, message: str) -> None:
        try:
            await ctx.report_progress(progress=done, total=total, message=message)
        except Exception:  # noqa: BLE001 - progress is best effort
            pass

    def callback(done: float, total: float, name: str = "") -> None:
        message = f"{label}: {name + ' ' if name else ''}{int(done)}/{int(total)}"
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(send(done, total, message))
        )

    return callback


def build_app(state: ServerState):
    """Create the FastMCP app. Tools read components from ``state`` at call time."""
    from fastmcp import Context, FastMCP
    from fastmcp.server.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    kb_enabled = state.status[KB_INDEX].state is not ComponentState.DISABLED

    class SessionRegistrar(Middleware):
        """Remember each client session so startup events can be pushed to it."""

        async def on_message(self, context, call_next):
            fastmcp_ctx = getattr(context, "fastmcp_context", None)
            if fastmcp_ctx is not None:
                try:
                    state.register_session(fastmcp_ctx.session)
                except Exception:  # noqa: BLE001 - never break a request over logging
                    pass
            return await call_next(context)

    app = FastMCP(
        config.SERVER_NAME,
        instructions=_instructions(kb_enabled),
        middleware=[SessionRegistrar()],
    )

    # Honor `logging/setLevel` from clients (also advertises the `logging` capability).
    try:

        @app._mcp_server.set_logging_level()
        async def _set_logging_level(level: str) -> None:
            state.set_log_level(level)

    except Exception:  # noqa: BLE001 - internal API; degrade to the default level
        logger.debug("Could not register logging/setLevel handler", exc_info=True)

    # ------------------------------------------------------------------ HTTP-only routes
    # Liveness: always 200 once the process is serving (use for container healthchecks).
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "service": config.SERVER_NAME,
                **state.snapshot(),
                "boot": state.boot_info(),
            }
        )

    # Readiness: 503 until the indexes are built.
    @app.custom_route("/ready", methods=["GET"])
    async def ready_check(request: Request) -> JSONResponse:
        snapshot = state.snapshot()
        return JSONResponse(snapshot, status_code=200 if snapshot["ready"] else 503)

    # ------------------------------------------------------------------ API tools
    @app.tool(
        name=f"{config.API_PREFIX}_api_request_schema",
        description=get_api_request_schema_description(),
    )
    async def api_request_schema(
        query: Annotated[
            str, Field(description="Natural language description of the API operation")
        ],
        n_results: Annotated[
            int, Field(description="Number of matching endpoints to return")
        ] = config.DEFAULT_SEARCH_RESULTS,
        ctx: Context | None = None,
    ) -> str:
        return await state.ensure_ready(ENDPOINT_INDEX, ctx) or (
            await handle_api_request_schema(query, n_results, state.endpoint_searcher)
        )

    @app.tool(
        name="nautobot_dynamic_api_request",
        description=get_dynamic_api_request_description(config.DEFAULT_DEPTH),
    )
    async def dynamic_api_request(
        method: Annotated[
            str, Field(description="HTTP method: GET, POST, PUT, PATCH, DELETE")
        ],
        path: Annotated[str, Field(description="API path, e.g. /dcim/devices/")],
        params: Annotated[
            Optional[dict], Field(description="Query parameters (GET/DELETE)")
        ] = None,
        body: Annotated[
            Optional[dict], Field(description="JSON body (POST/PUT/PATCH)")
        ] = None,
    ) -> str:
        return await handle_dynamic_api_request(method, path, params, body)

    @app.tool(
        name="refresh_endpoint_index",
        description=get_refresh_endpoint_index_description(),
    )
    async def refresh_endpoint_index(ctx: Context | None = None) -> str:
        if not_ready := await state.ensure_ready(ENDPOINT_INDEX, ctx):
            return not_ready
        progress = (
            _tool_progress(ctx, asyncio.get_running_loop(), "Re-embedding endpoints")
            if ctx is not None
            else None
        )
        return await handle_refresh_endpoint_index(state.endpoint_searcher, progress)

    @app.tool(
        name="nautobot_server_status", description=get_server_status_description()
    )
    async def server_status() -> str:
        return json.dumps(await state.status_report(), indent=2)

    # ------------------------------------------------------------------ KB tools
    if kb_enabled:

        @app.tool(
            name="nautobot_kb_semantic_search",
            description=get_kb_semantic_search_description(),
        )
        async def kb_semantic_search(
            query: Annotated[str, Field(description="What to search for")],
            n_results: Annotated[int, Field(description="Number of results")] = 5,
            ctx: Context | None = None,
        ) -> str:
            return await state.ensure_ready(KB_INDEX, ctx) or (
                await handle_kb_semantic_search(query, n_results, state.nautobot_kb)
            )

        @app.tool(
            name="nautobot_kb_list_repos", description=get_list_repos_description()
        )
        async def kb_list_repos() -> str:
            return await handle_list_repos()

        @app.tool(name="nautobot_kb_add_repo", description=get_add_repo_description())
        async def kb_add_repo(
            repo: Annotated[str, Field(description="Repository as owner/name")],
            description: Annotated[
                Optional[str], Field(description="Optional description")
            ] = None,
        ) -> str:
            return await handle_add_repo(repo, description)

        @app.tool(
            name="nautobot_kb_remove_repo", description=get_remove_repo_description()
        )
        async def kb_remove_repo(
            repo: Annotated[str, Field(description="Repository as owner/name")],
        ) -> str:
            return await handle_remove_repo(repo)

        @app.tool(
            name="nautobot_kb_update_repos", description=get_update_repos_description()
        )
        async def kb_update_repos(
            repo: Annotated[
                Optional[str],
                Field(description="Repository to update; omit to update all"),
            ] = None,
            force: Annotated[
                bool, Field(description="Re-index even if unchanged")
            ] = False,
            ctx: Context | None = None,
        ) -> str:
            if not_ready := await state.ensure_ready(KB_INDEX, ctx):
                return not_ready
            progress = (
                _tool_progress(ctx, asyncio.get_running_loop(), "Updating repositories")
                if ctx is not None and repo is None
                else None
            )
            return await handle_update_repos(state.nautobot_kb, repo, force, progress)

        @app.tool(
            name="nautobot_kb_init_repos", description=get_init_repos_description()
        )
        async def kb_init_repos(
            force: Annotated[
                bool, Field(description="Re-index all repositories")
            ] = False,
            ctx: Context | None = None,
        ) -> str:
            if not_ready := await state.ensure_ready(KB_INDEX, ctx):
                return not_ready
            progress = (
                _tool_progress(
                    ctx, asyncio.get_running_loop(), "Initializing repositories"
                )
                if ctx is not None
                else None
            )
            return await handle_init_repos(state.nautobot_kb, force, progress)

        @app.tool(
            name="nautobot_kb_repo_status", description=get_repo_status_description()
        )
        async def kb_repo_status(ctx: Context | None = None) -> str:
            return await state.ensure_ready(KB_INDEX, ctx) or (
                await handle_repo_status(state.nautobot_kb)
            )

    # ------------------------------------------------------------------ resources
    # Read-only context a client (or user) can attach without invoking a tool.
    info_cache = InstanceInfoCache()

    async def indexed_endpoints() -> list | None:
        """All indexed endpoints, or None while the endpoint index is still being built."""
        if await state.ensure_ready(ENDPOINT_INDEX, None):
            return None
        return await asyncio.to_thread(state.endpoint_searcher.list_endpoints)

    @app.resource(
        "nautobot://guide/api-conventions",
        name="api_conventions",
        description="How to read, filter, page and safely change data through Nautobot's REST "
        "API, and how to interpret its errors. Read before building non-trivial queries.",
        mime_type="text/markdown",
    )
    def api_conventions_resource() -> str:
        return API_CONVENTIONS_MD

    @app.resource(
        "nautobot://api/overview",
        name="api_overview",
        description="The connected Nautobot instance: version, python/django, installed "
        "plugins, and every API app with its resources. Read this to know what this "
        "specific Nautobot supports before searching.",
        mime_type="application/json",
    )
    async def api_overview_resource() -> str:
        info = await asyncio.to_thread(info_cache.get)
        endpoints = await indexed_endpoints()
        if endpoints is None:
            return json.dumps(
                {
                    "nautobot": {"url": config.NAUTOBOT_URL, **info},
                    "api": "The API index is still being built; read this resource again shortly.",
                },
                indent=2,
            )
        return build_overview(endpoints, info)

    async def endpoints_json(app_name: str) -> str:
        endpoints = await indexed_endpoints()
        if endpoints is None:
            return json.dumps(
                {"error": "The API index is still being built; retry shortly."}
            )
        return build_app_endpoints(endpoints, app_name)

    # Some hosts (MetaMCP among them) only serve `resources/read` for URIs that appeared in
    # `resources/list`, so a URI *template* alone is unreadable there. Publish one concrete
    # resource per API app as well, and keep the template for clients that support it.
    @app.resource(
        "nautobot://api/endpoints/{app}",
        name="api_endpoints_for_app",
        description="Every endpoint of one Nautobot API app (e.g. dcim, ipam, circuits, "
        "extras, tenancy) with the HTTP methods each supports.",
        mime_type="application/json",
    )
    async def api_endpoints_resource(app: str) -> str:
        return await endpoints_json(app)

    published_apps: set[str] = set()

    def publish_app_resource(app_name: str) -> None:
        if app_name in published_apps:
            return
        published_apps.add(app_name)

        async def read() -> str:
            return await endpoints_json(app_name)

        app.resource(
            f"nautobot://api/endpoints/{app_name}",
            name=f"api_endpoints_{app_name}",
            description=f"Every endpoint of the Nautobot '{app_name}' API app with the HTTP "
            "methods each supports. Use it to browse when the schema search does not surface "
            "what you need.",
            mime_type="application/json",
        )(read)

    for core_app in CORE_API_APPS:
        publish_app_resource(core_app)

    async def publish_discovered_apps() -> None:
        """Once the index exists, add resources for apps the core list did not know (plugins)."""
        endpoints = await asyncio.to_thread(state.endpoint_searcher.list_endpoints)
        before = len(published_apps)
        for meta in endpoints:
            name = endpoint_app(meta.get("path", ""))
            if name:
                publish_app_resource(name)
        if len(published_apps) != before:
            await state.notify_sessions(
                lambda session: session.send_resource_list_changed()
            )

    state.on_index_ready = publish_discovered_apps

    @app.resource(
        "nautobot://server/status",
        name="server_status",
        description="Live health of this MCP server: boot and warm-up timings, per-phase "
        "durations, index readiness and sizes.",
        mime_type="application/json",
    )
    async def server_status_resource() -> str:
        return json.dumps(await state.status_report(), indent=2)

    @app.resource(
        "nautobot://server/startup-log",
        name="startup_log",
        description="Chronological log of what the server did while starting up and "
        "building its indexes (model download, indexing progress, failures).",
        mime_type="application/json",
    )
    async def startup_log_resource() -> str:
        return json.dumps(state.startup_log(), indent=2)

    if kb_enabled:

        @app.resource(
            "nautobot://kb/repositories",
            name="kb_repositories",
            description="GitHub repositories configured for the Nautobot knowledge base.",
            mime_type="application/json",
        )
        async def kb_repositories_resource() -> str:
            return await handle_list_repos()

    # ------------------------------------------------------------------ prompts
    # User-invoked workflow templates (shown as slash commands in many clients).
    @app.prompt(
        name="query_nautobot",
        description="Answer a question or make a change in Nautobot using the API tools.",
    )
    def query_nautobot(
        task: Annotated[str, Field(description="What you want to know or change")],
    ) -> str:
        return (
            f"Task: {task}\n\n"
            f"1. Use the API schema search tool (`{config.API_PREFIX}_api_request_schema`; your "
            "client may show it with a server-name prefix) to find the endpoint(s) and "
            "parameters for this task. Do not guess paths.\n"
            "2. Use `nautobot_dynamic_api_request` to call them. Prefer GET first to inspect "
            "current state, and use filters and `limit` to keep responses small.\n"
            "3. For any POST/PUT/PATCH/DELETE, state exactly what you are about to change and "
            "confirm with me before sending it.\n"
            "4. Summarize the result and note anything surprising."
        )

    @app.prompt(
        name="diagnose_nautobot_mcp",
        description="Check why Nautobot MCP tools are slow, not ready, or failing.",
    )
    def diagnose_nautobot_mcp() -> str:
        return (
            "Call `nautobot_server_status` and explain the result in plain language: is each "
            "index ready, how long did startup and warm-up take, what failed or is still in "
            "progress, and what (if anything) should I do about it? Also check that the "
            "Nautobot URL looks right and that a token is configured."
        )

    @app.prompt(
        name="nautobot_inventory_report",
        description="Summarize part of the Nautobot inventory (devices, locations, IPs, ...).",
    )
    def nautobot_inventory_report(
        scope: Annotated[
            str, Field(description="What to inventory, e.g. 'devices in location DFW1'")
        ],
    ) -> str:
        return (
            f"Produce a concise inventory report for: {scope}\n\n"
            "Use the API schema search to find the right list endpoints, then query them with "
            "narrow filters and a modest `limit` (page with `offset` only if needed). Report "
            "counts first, then a short table of the key fields (name, status, role/type, "
            "location). Use `count` from the list response instead of paging just to count. "
            "Make read-only calls only. Mention anything you could not retrieve."
        )

    @app.prompt(
        name="nautobot_ip_lookup",
        description="Find what an IP address or prefix is assigned to in Nautobot.",
    )
    def nautobot_ip_lookup(
        address: Annotated[
            str, Field(description="An IP address or prefix, e.g. 10.1.2.3")
        ],
    ) -> str:
        return (
            f"Find everything Nautobot knows about {address}: the IP address / prefix record, "
            "its status, role, tenant and namespace/VRF where applicable, and the device and "
            "interface it is assigned to, if any. Use the API schema search to find the IPAM "
            "endpoints and filters (do not guess them). Read-only. If several records match, "
            "list them all and say how they differ."
        )

    @app.prompt(
        name="nautobot_safe_change",
        description="Make a change in Nautobot with a preview, explicit confirmation and verification.",
    )
    def nautobot_safe_change(
        change: Annotated[
            str, Field(description="The change to make, in plain language")
        ],
    ) -> str:
        return (
            f"I want to make this change in Nautobot: {change}\n\n"
            "1. Find the endpoint(s) with the API schema search and read-only GET the current "
            "object(s) to confirm exactly which records are affected.\n"
            "2. Show me a preview: each request (method, path, body) and the before/after of "
            "every field that changes. Do NOT send any write yet.\n"
            "3. Wait for my explicit confirmation. Prefer PATCH over PUT.\n"
            "4. After confirmation, apply the change, then GET the object(s) again and confirm "
            "the result matches. If anything fails, stop and report the error detail."
        )

    if kb_enabled:

        @app.prompt(
            name="nautobot_job_example",
            description="Find examples and write a Nautobot Job for a goal.",
        )
        def nautobot_job_example(
            goal: Annotated[str, Field(description="What the Job should do")],
        ) -> str:
            return (
                f"I want a Nautobot Job that does the following: {goal}\n\n"
                "Use `nautobot_kb_semantic_search` to find existing Job examples and the "
                "relevant Nautobot APIs, then write the Job. Cite which repository/file each "
                "pattern came from, and list any assumptions you made."
            )

    return app


async def serve_http(
    app, state: ServerState, host: str = "127.0.0.1", port: int = 8000
) -> None:
    """Serve ``app`` over streamable-http, building indexes in the background."""
    state.mark_serving("http")
    state.start()  # returns immediately; the server below starts accepting requests now
    try:
        await app.run_async(
            transport="streamable-http", host=host, port=port, show_banner=False
        )
    finally:
        await state.stop()


async def run_http_mode(port: int = 8000, host: str = "127.0.0.1"):
    """Run the MCP server in HTTP transport mode.

    Args:
        port: Port number to listen on (default: 8000)
        host: Interface to bind (default: 127.0.0.1; use 0.0.0.0 inside a container)
    """
    logger.info(f"Starting MCP Server in HTTP mode on {host}:{port}...")
    state = ServerState(enable_kb=config.ENABLE_KB)
    await serve_http(build_app(state), state, host=host, port=port)


async def run_stdio_mode():
    """Run the MCP server in stdio transport mode."""
    logger.info("Starting MCP Server in stdio mode...")
    state = ServerState(enable_kb=config.ENABLE_KB)
    app = build_app(state)
    state.mark_serving("stdio")
    state.start()  # background index building; the handshake is served immediately
    try:
        await app.run_async(transport="stdio", show_banner=False)
    finally:
        await state.stop()


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
  nautobot-mcp
  nautobot-mcp --mode stdio

  # Run in HTTP mode on default port (8000)
  nautobot-mcp --mode http

  # Bind all interfaces (containers)
  nautobot-mcp --mode http --host 0.0.0.0

  # Run in HTTP mode on custom port
  nautobot-mcp --mode http --port 9000

  # Using environment variables
  MCP_TRANSPORT=http MCP_PORT=8000 nautobot-mcp
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
        "--print-instructions",
        action="store_true",
        help="Print the server instructions (for pasting into hosts that do not forward "
        "them, e.g. a MetaMCP namespace description) and exit",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Check the Nautobot connection settings (URL, schema, token) and exit",
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

    if args.print_instructions:
        print(_instructions(config.ENABLE_KB))
        return

    if args.check:
        from nautobot_mcp.utils import validate_config

        sys.exit(validate_config.main())

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


def cli() -> None:
    """Console-script entry point (``nautobot-mcp`` / ``uvx nautobot-mcp``)."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
