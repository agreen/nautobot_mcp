# Quick Start Guide

This guide assumes you have completed the [installation](installation.md).

## 1. Point it at Nautobot

```bash
export NAUTOBOT_URL=https://nautobot.example.com
export NAUTOBOT_TOKEN=your-api-token
nautobot-mcp --check
```

## 2. Run it

```bash
nautobot-mcp                    # stdio, for MCP clients that spawn the process
nautobot-mcp --mode http        # streamable HTTP at http://127.0.0.1:8000/mcp/
```

## 3. Connect a client

=== "uvx (stdio)"
    ```json
    {
      "servers": {
        "nautobot_mcp": {
          "type": "stdio",
          "command": "uvx",
          "args": ["--from", "git+https://github.com/agreen/nautobot_mcp", "nautobot-mcp"],
          "env": {
            "NAUTOBOT_URL": "https://nautobot.example.com",
            "NAUTOBOT_TOKEN": "your-api-token"
          }
        }
      }
    }
    ```

=== "From a checkout (stdio)"
    ```json
    {
      "servers": {
        "nautobot_mcp": {
          "type": "stdio",
          "command": "uv",
          "args": ["run", "--project", "/absolute/path/to/nautobot_mcp", "nautobot-mcp"],
          "env": {
            "NAUTOBOT_URL": "https://nautobot.example.com",
            "NAUTOBOT_TOKEN": "your-api-token"
          }
        }
      }
    }
    ```

=== "HTTP"
    ```json
    { "servers": { "nautobot_mcp": { "type": "http", "url": "http://localhost:8000/mcp/" } } }
    ```

## 4. Try it

Ask your assistant to *find the API endpoint for listing devices*, then *list the first 5 devices*.
The first questions after a fresh start may return a `not_ready` status while the indexes are
built (see [installation](installation.md#first-start)); retry shortly.

## Tools

| Tool | Purpose |
|---|---|
| `nautobot_openapi_api_request_schema` | Natural-language search over Nautobot's API endpoints |
| `nautobot_dynamic_api_request` | Execute GET/POST/PUT/PATCH/DELETE against the Nautobot API |
| `refresh_endpoint_index` | Re-index the Swagger schema |
| `nautobot_kb_*` | Knowledge-base search and repository management (when `NAUTOBOT_MCP_ENABLE_KB=true`) |
