# Configuration Reference

The server is configured entirely through environment variables. A `.env` file in the working
directory is also read (copy `.env.example` as a starting point). When launched by an MCP
client or MetaMCP, put the variables in the server definition's `env` block.

There is no "environment" switch: point `NAUTOBOT_URL` / `NAUTOBOT_TOKEN` at whichever Nautobot
instance you want.

## Nautobot connection

| Variable | Default | Description |
|---|---|---|
| `NAUTOBOT_URL` | `http://localhost:8080` | Base URL of Nautobot. A trailing `/api` is tolerated. |
| `NAUTOBOT_TOKEN` | *(empty)* | API token. Needs write permission if you want create/update/delete. |
| `SSL_VERIFY` | `true` | Set `false` for self-signed certificates. |
| `API_TIMEOUT` | `10` | Seconds per Nautobot API call. |
| `SCHEMA_TIMEOUT` | `120` | Seconds to fetch the Swagger schema (large instances are slow). |

Run `nautobot-mcp --check` to verify the URL, schema and token.

## Features

| Variable | Default | Description |
|---|---|---|
| `NAUTOBOT_MCP_ENABLE_KB` | `true` | Knowledge-base tools (semantic search over Nautobot GitHub repos). Clones and indexes several large repositories; set `false` for API-only use. The `nautobot_kb_*` tools are then not offered at all. |
| `GITHUB_TOKEN` | *(empty)* | Raises GitHub rate limits for the knowledge base. |
| `NAUTOBOT_MCP_READY_WAIT` | `20` | Seconds an index-backed tool waits for a still-warming index (reporting MCP progress) before returning `not_ready`. `0` = never wait. |
| `DEFAULT_SEARCH_RESULTS` | `5` | Default number of results for API schema search. |
| `API_PREFIX` | `nautobot_openapi` | Prefix of the schema-search tool name (`<prefix>_api_request_schema`). |

## Storage

All writable state lives under one directory and is never written inside the installed package:

```
<data dir>/chroma/                   ChromaDB indexes
<data dir>/repos/                    cloned knowledge-base repositories
<data dir>/models/                   embedding model (downloaded on first use, ~80 MB)
<data dir>/user_repositories.json    repositories added through the MCP tools
```

`NAUTOBOT_MCP_DATA_DIR` sets the location; the default is `~/.local/share/nautobot-mcp`
(`$XDG_DATA_HOME/nautobot-mcp`). Use a persistent volume in containers so restarts reuse the index.

## Transport

| Variable | Default | CLI flag | Description |
|---|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `--mode` | `stdio` or `http` (streamable HTTP at `/mcp/`). |
| `MCP_HOST` | `127.0.0.1` | `--host` | Bind address for HTTP mode (`0.0.0.0` in containers). |
| `MCP_PORT` | `8000` | `--port` | Port for HTTP mode. |

HTTP mode also serves `GET /health` (liveness) and `GET /ready` (503 until indexes are built).
The HTTP endpoint has no authentication; keep it on loopback or a private network.

## Logging

`LOG_LEVEL` (default `INFO`). Logs go to stderr so they never corrupt the stdio protocol.

## Embedding model

Semantic search uses `all-MiniLM-L6-v2` through ChromaDB's bundled ONNX runtime (no PyTorch).
The model is fixed; there is no model setting. See [installation](installation.md#embedding-model).
