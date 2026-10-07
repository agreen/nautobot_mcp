# Nautobot MCP Server

[![CI](https://github.com/kvncampos/nautobot_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/kvncampos/nautobot_mcp/actions/workflows/ci.yml)
[![Documentation](https://github.com/kvncampos/nautobot_mcp/actions/workflows/docs.yml/badge.svg)](https://github.com/kvncampos/nautobot_mcp/actions/workflows/docs.yml)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/kvncampos/nautobot_mcp)](https://github.com/kvncampos/nautobot_mcp/releases)
[![GitHub](https://img.shields.io/github/license/kvncampos/nautobot_mcp)](https://github.com/kvncampos/nautobot_mcp/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A Model Context Protocol (MCP) server for interacting with Nautobot APIs using semantic search and dynamic API requests. This server provides intelligent access to Nautobot instances and a comprehensive knowledge base of Nautobot-related repositories.

## 📖 Documentation

**[View Full Documentation →](https://kvncampos.github.io/nautobot_mcp/)**

- [Installation Guide](https://kvncampos.github.io/nautobot_mcp/installation/)
- [Quick Start](https://kvncampos.github.io/nautobot_mcp/quickstart/)
- [Architecture](https://kvncampos.github.io/nautobot_mcp/architecture/)
- [Tools Reference](https://kvncampos.github.io/nautobot_mcp/tools/)
- [Examples](https://kvncampos.github.io/nautobot_mcp/examples/)

## 🚀 Features

### Core Capabilities
- **Dynamic API Access**: Perform CRUD operations on any Nautobot API endpoint
- **Semantic Endpoint Discovery**: Find relevant API endpoints using natural language queries
- **Knowledge Base Search**: Access indexed content from official Nautobot repositories
- **Multi-Environment Support**: Connect to different Nautobot instances (dev, staging, prod)
- **Smart Caching**: Efficient ChromaDB-powered vector storage with Git-based updates

### API Tools
- `nautobot_dynamic_api_request`: Execute any HTTP method against Nautobot APIs
- `nautobot_openapi_api_request_schema`: Discover API endpoints through semantic search
- `nautobot_kb_semantic_search`: Search through indexed Nautobot documentation and code
- Repository management tools for maintaining the knowledge base

## 📋 Prerequisites

- Python 3.11 or 3.12 (`uvx` / `uv` will fetch it for you)
- Access to a Nautobot instance and an API token
- Git (only for the optional knowledge base, which clones GitHub repositories)
- Internet access on first run to download the ~80 MB embedding model (see [Embedding model](#embedding-model))

## Design Overview

![MCP Overview Diagram](docs/mcp_overview.png)

## 🛠️ Installation

### Option 1: `uvx` (MetaMCP, Claude Desktop, any stdio MCP client)

```bash
uvx --from git+https://github.com/agreen/nautobot_mcp nautobot-mcp
```

Configure it with environment variables. For example in an MCP client / MetaMCP server definition:

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

Notes:

- **First launch installs ~130 packages (≈30 s on a fast connection).** Pre-warm it once
  (`uvx --from git+https://github.com/agreen/nautobot_mcp nautobot-mcp --help`) if your client has a short connect
  timeout. After that the MCP handshake takes under a second.
- **The server answers the handshake immediately** and builds its search indexes in the background. Index-backed
  tools return `{"status": "not_ready", ...}` until they are built (first run: model download + indexing, a minute or
  two; later runs: seconds). `nautobot_dynamic_api_request` never waits.
- `nautobot-mcp --check` verifies the URL, schema and token and exits.
- Startup progress is reported to clients as MCP log notifications and tool-call progress, and the server
  describes itself via the `nautobot_server_status` tool and `nautobot://server/status` resource. See
  [Observability](docs/observability.md).
- State (indexes, model, cloned repos) is kept in `~/.local/share/nautobot-mcp`; set `NAUTOBOT_MCP_DATA_DIR` to move it.

### Option 2: Docker / sidecar next to Nautobot

See [DOCKER.md](DOCKER.md) and `docker-compose.sidecar.yml`. The image runs the HTTP transport on port 8000
(`/mcp/`), bakes in the embedding model, and exposes `/health` and `/ready`.

### Option 3: From source

```bash
git clone https://github.com/agreen/nautobot_mcp && cd nautobot_mcp
uv sync                 # creates .venv with dev dependencies
cp .env.example .env    # set NAUTOBOT_URL / NAUTOBOT_TOKEN
uv run nautobot-mcp --check
uv run nautobot-mcp                 # stdio
uv run nautobot-mcp --mode http     # HTTP on 127.0.0.1:8000
```

## ⚙️ Configuration

Everything is a plain environment variable (a `.env` file in the working directory is also read). There is no
"environment" switch: point `NAUTOBOT_URL` / `NAUTOBOT_TOKEN` at whichever Nautobot you want.

| Variable | Default | Description |
|---|---|---|
| `NAUTOBOT_URL` | `http://localhost:8080` | Nautobot base URL (a trailing `/api` is tolerated) |
| `NAUTOBOT_TOKEN` | *(empty)* | API token |
| `SSL_VERIFY` | `true` | Set `false` for self-signed certificates |
| `NAUTOBOT_MCP_ENABLE_KB` | `true` | Enable the knowledge-base tools (clones and indexes Nautobot GitHub repos). Set `false` for API-only use |
| `GITHUB_TOKEN` | *(empty)* | Higher GitHub rate limits for the knowledge base |
| `NAUTOBOT_MCP_DATA_DIR` | `~/.local/share/nautobot-mcp` | Indexes, cloned repos, embedding model, user-added repos |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` | `stdio` / `127.0.0.1` / `8000` | Transport settings (CLI flags `--mode/--host/--port` win) |
| `NAUTOBOT_MCP_READY_WAIT` | `20` | Seconds an index-backed tool call waits (reporting progress) for a still-warming index before answering `not_ready`; `0` disables waiting |
| `API_TIMEOUT` | `10` | Seconds per Nautobot API call |
| `SCHEMA_TIMEOUT` | `120` | Seconds to fetch the Swagger schema |
| `DEFAULT_SEARCH_RESULTS` | `5` | Default result count for schema search |
| `API_PREFIX` | `nautobot_openapi` | Prefix of the schema-search tool name |
| `LOG_LEVEL` | `INFO` | Logging level (logs go to stderr) |

### Embedding model

Semantic search uses `all-MiniLM-L6-v2` through ChromaDB's bundled ONNX runtime (no PyTorch). The ~80 MB model is
downloaded from ChromaDB's model bucket on first use into `<data dir>/models`; the Docker image ships it pre-baked.
For air-gapped hosts, copy a populated `models/` directory into the data dir.

### Repository Configuration (knowledge base)

The default repositories are listed in `src/nautobot_mcp/config/repositories.json` (read-only package data).
Repositories added with the `nautobot_kb_add_repo` tool are stored in `<data dir>/user_repositories.json`.

## 🚀 Usage

```bash
nautobot-mcp                      # stdio (default)
nautobot-mcp --mode http          # streamable HTTP at http://127.0.0.1:8000/mcp/
nautobot-mcp --mode http --host 0.0.0.0 --port 9000
python -m nautobot_mcp --check    # verify configuration
```

HTTP mode also serves `GET /health` (liveness) and `GET /ready` (503 until indexes are built). The HTTP endpoint has
**no authentication** and can write to Nautobot with your token; bind it to loopback or keep it on a private network.

### VS Code / Claude Desktop (stdio)

```json
{
  "servers": {
    "nautobot": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/agreen/nautobot_mcp", "nautobot-mcp"],
      "env": { "NAUTOBOT_URL": "https://nautobot.example.com", "NAUTOBOT_TOKEN": "..." }
    }
  }
}
```

### Example API Requests

#### Search for API Endpoints
```python
# Find endpoints related to devices
query = "get device information"
# Returns relevant endpoints like /dcim/devices/
```

#### Execute API Requests
```python
# Get all locations
method = "GET"
path = "/dcim/locations/"
params = {"limit": 100}
```

#### Search Knowledge Base
```python
# Find documentation about custom fields
query = "how to create custom fields in Nautobot"
# Returns relevant documentation and code examples
```

## 📁 Project Structure

```
nautobot_mcp/
├── pyproject.toml            # package metadata, `nautobot-mcp` console script
├── src/nautobot_mcp/
│   ├── main.py               # entrypoint: stdio + HTTP transports, tool registration
│   ├── config/repositories.json   # default knowledge-base repositories
│   ├── helpers/
│   │   ├── startup.py        # background index building (non-blocking handshake)
│   │   ├── tool_handlers.py  # tool implementations
│   │   ├── tool_definitions.py
│   │   ├── endpoint_searcher_chroma.py   # Swagger endpoint index
│   │   ├── nb_kb_v2.py       # knowledge base (per-repository vector stores)
│   │   └── content_processor.py
│   └── utils/                # config, paths, embedding, git + repo config
├── tests/
├── Dockerfile, docker-compose.yml, docker-compose.sidecar.yml
└── docs/
```

## 🔧 Development

### Running Tests

```bash
# Run all tests
pytest

# Run specific test categories
pytest -m "unit"
pytest -m "integration"
pytest -m "not slow"

# Run with coverage
pytest --cov=helpers --cov=utils
```

### Code Quality

The project uses several tools for code quality:

```bash
# Format code
ruff format .

# Lint code
ruff check .

# Pre-commit hooks (install once)
pre-commit install
```

### Adding New Repositories

To add repositories to the knowledge base:

1. **Add to configuration**:
   ```python
   from nautobot_mcp.helpers.manage_repos import RepositoryManager
   
   manager = RepositoryManager()
   manager.add_repository("owner/repo", category="custom", description="My custom repo")
   ```

2. **Initialize the repository**:
   ```python
   manager.initialize_repositories(force=True)
   ```

## 📖 Examples

### Basic Knowledge Base Search

```python
from nautobot_mcp.helpers.nb_kb_v2 import EnhancedNautobotKnowledge

kb = EnhancedNautobotKnowledge()
results = kb.search("custom field validation", n_results=5)

for result in results:
    print(f"Repository: {result['metadata']['repository']}")
    print(f"File: {result['metadata']['file_path']}")
    print(f"Content: {result['document'][:200]}...")
```

### API Endpoint Discovery

```python
from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

searcher = EndpointSearcherChroma()
endpoints = searcher.search("create new device", n_results=3)

for endpoint in endpoints:
    print(f"Method: {endpoint['method']}")
    print(f"Path: {endpoint['path']}")
    print(f"Description: {endpoint['description']}")
```

### Dynamic API Requests

```python
import requests
from nautobot_mcp.utils.config import config

# Example: Get device count
response = requests.get(
    f"{config.NAUTOBOT_BASE_URL}/api/dcim/devices/",
    headers={"Authorization": f"Token {config.NAUTOBOT_TOKEN}"},
    params={"limit": 1},
    verify=config.SSL_VERIFY
)

total_count = response.json()["count"]
print(f"Total devices: {total_count}")
```


## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Guidelines

- Follow PEP 8 style guidelines
- Add tests for new functionality
- Update documentation for API changes
- Use type hints where appropriate
- Run the full test suite before submitting

## 🧪 Testing & Validation

### Available Examples

The `examples/` directory contains several demonstration scripts:

- **`example_kb_search.py`** - Basic knowledge base search functionality
- **`example_job.py`** - Example Nautobot job integration
- **`demo_hybrid_processing.py`** - Demonstrates hybrid content processing
- **`example_detailed_search_analysis.py`** - Advanced search analysis
- **`fetch_schema.py`** - OpenAPI schema fetching utility
- **`pynautobot_kb_example/`** - PyNautobot integration examples

### Running Tests

The project includes comprehensive tests in the `tests/` directory:

```bash
# Run all tests
uv run python -m pytest tests/

# Run specific test files
uv run python tests/test_nb_kb_v2.py
uv run python tests/test_endpoint_searcher_chroma.py
uv run python tests/test_manage_repos.py
```

### Validation

Test your configuration and server setup:

```bash
# Test configuration
uv run nautobot-mcp --check

# Test server initialization
uv run python -c "import nautobot_mcp.main; print('Server imports OK')"
```

## 🛠️ MCP Tools Available

### API Tools
- **`nautobot_openapi_api_request_schema`**: Search for API endpoints by intent
- **`nautobot_dynamic_api_request`**: Execute API requests with any HTTP method
- **`refresh_endpoint_index`**: Manually refresh the endpoint search index
- **`nautobot_server_status`**: The server's own health: boot and warm-up timings, per-phase durations, index readiness and sizes

### Knowledge Base Tools
- **`nautobot_kb_semantic_search`**: Semantic search over Nautobot knowledge base repositories
- **`nautobot_kb_list_repos`**: List repositories configured in Nautobot knowledge base
- **`nautobot_kb_add_repo`**: Add a new repository to the Nautobot knowledge base
- **`nautobot_kb_remove_repo`**: Remove an existing repository from the Nautobot knowledge base
- **`nautobot_kb_update_repos`**: Update repositories in the Nautobot knowledge base
- **`nautobot_kb_init_repos`**: Initialize repositories in the Nautobot knowledge base
- **`nautobot_kb_repo_status`**: Show nautobot knowledge base repository status including document counts and indexing status

## 🐛 Troubleshooting

### Common Issues

1. **SSL Certificate Errors**:
   ```bash
   # Set SSL_VERIFY=False in .env for development
   SSL_VERIFY=False
   ```

2. **ChromaDB Permission Issues**:
   ```bash
   # Ensure proper permissions on the backend directory
   chmod -R 755 backend/
   ```

3. **GitHub API Rate Limits**:
   ```bash
   # Ensure you have a valid GitHub token
   GITHUB_TOKEN=your_github_token
   ```

4. **Repository Initialization Fails**:
   ```python
   # Force reinitialize repositories
   kb = EnhancedNautobotKnowledge()
   kb.initialize_all_repositories(force=True)
   ```

### Docker-Specific Issues

1. **Container Won't Start**:
   ```bash
   # Check logs for errors
   docker compose logs
   
   # Verify environment variables
   docker compose config
   
   # Rebuild the image
   docker compose build --no-cache
   ```

2. **Volume Permission Issues**:
   ```bash
   # Check volume permissions
   docker compose exec nautobot-mcp ls -la /app/backend/
   
   # If needed, recreate volumes
   docker compose down -v
   docker compose up -d
   ```

3. **Port Already in Use (HTTP mode)**:
   ```bash
   # Check what's using the port
   lsof -i :8000
   
   # Use a different port
   MCP_PORT=9000 docker compose up -d
   ```

4. **Out of Memory Errors**:
   ```bash
   # Increase memory limits in docker compose.yml
   # Under deploy.resources.limits.memory
   
   # Check current usage
   docker stats nautobot-mcp-server
   ```

5. **ChromaDB Data Not Persisting**:
   ```bash
   # Verify volumes are created
   docker volume ls | grep nautobot-mcp
   
   # Inspect volume
   docker volume inspect nautobot-mcp-data
   ```

6. **Building Behind Corporate Proxy**:
   ```bash
   # Add proxy settings to docker compose.yml build args
   docker compose build \
     --build-arg HTTP_PROXY=http://proxy.example.com:8080 \
     --build-arg HTTPS_PROXY=http://proxy.example.com:8080
   ```

### Debug Mode

Enable debug logging by setting:
```bash
LOG_LEVEL=DEBUG
```

## 📞 Support

- 📖 **[Documentation](https://kvncampos.github.io/nautobot_mcp/)** - Comprehensive guides and references
- 🐛 **[Issue Tracker](https://github.com/kvncampos/nautobot_mcp/issues)** - Bug reports and feature requests
- 💬 **[Discussions](https://github.com/kvncampos/nautobot_mcp/discussions)** - Questions and community support
- 📂 **Examples** - Check the `examples/` directory for usage patterns
- 🧪 **Tests** - Review the test suite for implementation details

## 🙏 Acknowledgments

- [Nautobot](https://github.com/nautobot/nautobot) - Network automation platform
- [Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk) - MCP Python SDK
- [ChromaDB](https://github.com/chroma-core/chroma) - Vector database
- [ChromaDB](https://www.trychroma.com/) - Vector store and bundled ONNX embedding runtime
- [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) - Embedding model

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.