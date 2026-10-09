# Testing

## Automated tests

```bash
uv sync
uv run python -m pytest tests/ --deselect tests/test_http_server.py
```

The suite needs no network and no Nautobot: it runs against `tests/fake_nautobot.py`, a small fake
Nautobot REST API (Swagger 2.0 with `definitions`, `/api/status/`, paging, filters, validation errors,
401/403, a slow endpoint). `tests/test_http_server.py` is a manual script that needs a running server.

## Try the server without a Nautobot

```bash
# terminal 1: a fake Nautobot with a realistic schema (add --extra-endpoints 2000 for a large one)
python tests/fake_nautobot.py --port 8080 --token secret

# terminal 2: point the MCP server at it
NAUTOBOT_URL=http://127.0.0.1:8080 NAUTOBOT_TOKEN=secret nautobot-mcp --check
NAUTOBOT_URL=http://127.0.0.1:8080 NAUTOBOT_TOKEN=secret SSL_VERIFY=false nautobot-mcp --mode http
```

The fake also accepts the token `readonly-token`, which can read but gets 403 on writes.

## Test plan for your real Nautobot

Use the same command your MCP host will run. Replace the URL/token. Work through these in order; each
step says what you should see.

| # | Step | Expected |
|---|---|---|
| 1 | `nautobot-mcp --check` | All lines PASS: schema with operation count, API endpoint, **Nautobot version and plugins**, authentication. A line with *no `definitions`* means POST/PATCH body fields will not be listed. |
| 2 | Start the server in your host (MetaMCP etc.) and list its tools | 4 tools with `NAUTOBOT_MCP_ENABLE_KB=false`, 11 with it enabled |
| 3 | Call `nautobot_server_status` right away | `ready: false` at first; `boot.serving_after_seconds` is a small number; `endpoint_index` moves through *loading embedding model* → *indexing Swagger schema* |
| 4 | Watch the host's notifications / log view | Startup events (model download %, *embedding endpoints: N%*, *ready in …s*) |
| 5 | Read `nautobot://api/overview` | Your real Nautobot version, your plugins, and your API apps with resources. **Check this against what you know is installed.** |
| 6 | Read `nautobot://api/endpoints/dcim` and `.../ipam` | Complete endpoint lists with methods |
| 7 | Read `nautobot://guide/api-conventions` | The conventions text. **Check the statements against your version** (see below) |
| 8 | Search: *"list devices in a location"*, *"create an ip address"*, *"find prefixes"* | The right paths, relative to `/api`, with sensible filters; POST results list the required body fields |
| 9 | Read-only requests: list locations with `limit` 5; filter devices with `name__ic`; follow the paging hint | Results, a paging `_mcp_hint`, and filters that really filter |
| 10 | Ask for something that does not exist (a made-up UUID) | A structured 404 with a hint, not a crash |
| 11 | Run the `nautobot_safe_change` prompt on a **throwaway object** (create a test location, rename it, delete it) | The assistant previews, waits for you to confirm, applies, then verifies |
| 12 | Restart the server | `endpoint_index` ready within seconds (the index and model are cached in the data dir) |
| 13 | Start two copies at once (some hosts do) | Both work; one logs that it uses a private index |

### Things to verify against your Nautobot version

These are written from general knowledge of Nautobot's REST API and should be confirmed on your instance:

- the default page size and maximum `limit`
- `depth` support, and its effect on response size (if your version rejects it, set
  `NAUTOBOT_MCP_DEFAULT_DEPTH=-1`)
- the `name__ic` / `__isw` / `__n` lookup suffixes and `q=` search on the endpoints you use
- whether related objects in request bodies accept names as well as UUIDs
- the version-specific vocabulary (for example Locations in 2.x versus Sites/Regions in 1.x); the
  overview resource reports your version so the assistant can account for it

If something is off, send me: the output of `nautobot-mcp --check`, the host's server log (stderr), and
the result of `nautobot_server_status`.

## Inspecting the server by hand

The MCP Inspector can list and call tools, read resources and run prompts interactively:

```bash
npx @modelcontextprotocol/inspector \
  uvx --from git+https://github.com/agreen/nautobot_mcp@uvx-packaging nautobot-mcp
```

Set `NAUTOBOT_URL` / `NAUTOBOT_TOKEN` in the Inspector's environment-variables panel.
