# Installation Guide

## Prerequisites

- Python 3.11 or 3.12 (`uv`/`uvx` will download it if missing)
- A Nautobot instance and an API token
- Git, only if you use the knowledge base (it clones GitHub repositories)
- Internet access on first run for the ~80 MB embedding model

## Option 1: uvx (recommended for MetaMCP and stdio MCP clients)

```bash
uvx --from git+https://github.com/agreen/nautobot_mcp nautobot-mcp
```

Server definition for an MCP client or MetaMCP:

```json
{
  "command": "uvx",
  "args": ["--from", "git+https://github.com/agreen/nautobot_mcp", "nautobot-mcp"],
  "env": {
    "NAUTOBOT_URL": "https://nautobot.example.com",
    "NAUTOBOT_TOKEN": "your-api-token",
    "NAUTOBOT_MCP_ENABLE_KB": "false"
  }
}
```

The first launch resolves and installs ~130 packages (about 30 s on a fast connection). If your
client has a short connect timeout, run the command once beforehand to warm the cache; later
launches answer the MCP handshake in under a second.

Verify your settings:

```bash
uvx --from git+https://github.com/agreen/nautobot_mcp nautobot-mcp --check
```

## Option 2: Docker

See [DOCKER.md](https://github.com/agreen/nautobot_mcp/blob/main/DOCKER.md) for the standalone and
sidecar (`docker-compose.sidecar.yml`) setups.

## Option 3: From source

```bash
git clone https://github.com/agreen/nautobot_mcp
cd nautobot_mcp
uv sync
cp .env.example .env     # set NAUTOBOT_URL / NAUTOBOT_TOKEN
uv run nautobot-mcp --check
uv run nautobot-mcp
uv run python -m pytest tests/
```

## Embedding model

The server uses `all-MiniLM-L6-v2` via ChromaDB's ONNX runtime. It is downloaded on first use from
ChromaDB's model bucket into `<data dir>/models` (Docker images ship it pre-baked). On an air-gapped
host, copy a populated `models/` directory into the data directory beforehand.

## First start

The server answers the MCP handshake immediately and builds its indexes in the background. Until
then the index-backed tools return `{"status": "not_ready"}`; `nautobot_dynamic_api_request` works
right away. First run: model download plus indexing (a minute or two); later runs: seconds.
