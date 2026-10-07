# Changelog

All notable changes to the Nautobot MCP Server will be documented in this file.

## [0.3.0]

### Added
- Installable package (`src/nautobot_mcp`) with a `nautobot-mcp` console script; runs with `uvx`
  (`uvx --from git+https://github.com/agreen/nautobot_mcp nautobot-mcp`) and `python -m nautobot_mcp`
- `--check` flag to verify the Nautobot URL, schema and token
- `NAUTOBOT_MCP_ENABLE_KB=false` to disable the knowledge base (no GitHub cloning, tools not offered)
- Non-blocking startup: the MCP handshake is answered immediately; indexes build in the background and
  index-backed tools return a `not_ready` status until ready. HTTP mode adds `/health` and `/ready`
- Docker: HTTP by default, healthcheck, `docker-compose.sidecar.yml` for running next to Nautobot

### Changed
- **Configuration is now plain `NAUTOBOT_URL` / `NAUTOBOT_TOKEN`.** The `NAUTOBOT_ENV` local/nonprod/prod
  mapping (and `NAUTOBOT_*_BASE_URL` / `NAUTOBOT_*_TOKEN` variables) was removed
- Embeddings run on ChromaDB's ONNX build of `all-MiniLM-L6-v2` instead of PyTorch/sentence-transformers
  (install drops from ~4.3 GB to ~0.4 GB). The ~80 MB model downloads on first use; existing vector indexes
  should be rebuilt. `EMBEDDING_MODEL` is no longer configurable
- All state lives under `NAUTOBOT_MCP_DATA_DIR` (default `~/.local/share/nautobot-mcp`); the install
  directory is never written to. User-added repositories moved to `<data dir>/user_repositories.json`
- Dependency upper bounds (`mcp<2`, `fastmcp<3`, `chromadb<2`) because `uvx` ignores `uv.lock`

### Observability and resilience (0.3.0)
- Startup progress is reported to clients as MCP log notifications (replayed to late joiners, honoring
  `logging/setLevel`), and index-backed tool calls now wait briefly while reporting MCP progress
  (`NAUTOBOT_MCP_READY_WAIT`, default 20 s) instead of failing immediately
- New `nautobot_server_status` tool and `nautobot://server/status` / `nautobot://server/startup-log`
  resources with boot and per-phase warm-up timings; HTTP `/health` includes boot timings
- Server `instructions` in the `initialize` result; prompts `query_nautobot`, `diagnose_nautobot_mcp`,
  `nautobot_job_example`; resource `nautobot://kb/repositories`
- One FastMCP application now serves stdio and HTTP, so **tool names are the same on both**; the HTTP
  transport lost its `mcp_` prefix (`nautobot_openapi_api_request_schema`, `nautobot_kb_*`, ...). Requires
  `fastmcp>=2.12`
- The search indexes self-heal: ChromaDB failures (e.g. "hnsw segment writer") trigger an automatic
  index rebuild and retry rather than a permanent failure; the endpoint index retries with backoff while
  Nautobot is unreachable
- Several server processes sharing one data directory no longer corrupt each other: the first owns the
  shared indexes, the rest use private directories (cleaned up on exit)
- Python 3.13 supported
- Indexing reports progress every 100 endpoints

### Removed
- `server.py` / `server_http.py` (use `nautobot-mcp [--mode http]`), the unused `nb_kb_v1.py` and
  `cache_embedding.py`, the committed `backend/models` directory

## [Unreleased]

### Added
- MKDocs documentation with GitHub Pages support
- Comprehensive user guides and examples
- Architecture documentation with diagrams
- Common tool handlers module (`helpers/tool_handlers.py`) for shared implementation logic
- Common tool definitions module (`helpers/tool_definitions.py`) for consistent tool schemas
- Comprehensive unit tests for tool definitions (`tests/test_tool_definitions.py`)

### Changed
- **[Breaking]** Improved tool descriptions following LLM ingestion best practices
  - Descriptions are now more concise (under 300 characters)
  - Added clear, actionable language for better LLM comprehension
  - Documented parameter usage patterns (GET/DELETE use `params`, POST/PUT/PATCH use `body`)
- **[Breaking]** Removed unused parameters:
  - Removed `repo_type` parameter from `nautobot_kb_list_repos` (was accepted but not used)
  - Removed `category` parameter from `nautobot_kb_add_repo` (was accepted but not used)
- Refactored both `server.py` and `server_http.py` to eliminate code duplication
  - Reduced `server.py` from 631 to 249 lines (60% reduction)
  - Reduced `server_http.py` from 405 to 151 lines (62% reduction)
  - Extracted ~400 lines of duplicate code to shared modules
- Improved maintainability by establishing single source of truth for tool implementations

### Fixed
- Inconsistent tool descriptions between stdio and HTTP implementations
- Missing parameter constraints and validations in tool schemas
- Unclear documentation about which HTTP methods use which parameters

## [0.2.0] - 2024-10-14

### Added
- Enhanced knowledge base with optimized search
- Repository management tools
- Support for multiple Nautobot environments
- ChromaDB-based vector storage

### Changed
- Improved embedding model configuration
- Better error handling and logging

## [0.1.0] - 2024-10-01

### Added
- Initial MCP server implementation
- OpenAPI endpoint discovery
- Basic knowledge base search
- VS Code Copilot integration

---

## Contributing

See the [Contributing Guide](contributing.md) for information on how to contribute to this project.
