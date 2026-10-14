"""Configuration for the Nautobot MCP server.

Everything is configured through environment variables (a ``.env`` file in the working
directory is also honored). See ``.env.example`` for the full list.
"""

import os

from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("true", "1", "yes", "on")


def _nautobot_url() -> str:
    url = os.getenv("NAUTOBOT_URL", "http://localhost:8080").strip().rstrip("/")
    # Be forgiving: people often paste the API root.
    return url[: -len("/api")] if url.endswith("/api") else url


class Config:
    """Application settings, read from the environment at import time."""

    # Nautobot connection
    NAUTOBOT_URL: str = _nautobot_url()
    NAUTOBOT_TOKEN: str = os.getenv("NAUTOBOT_TOKEN", "")
    NAUTOBOT_BASE_URL: str = NAUTOBOT_URL + "/api"
    # OpenAPI schema location (name kept for backwards compatibility)
    GLOBAL_TOOL_PROMPT: str = NAUTOBOT_URL + "/api/swagger.json"
    SSL_VERIFY: bool = _env_bool("SSL_VERIFY", True)

    # How long an index-backed tool call waits for a still-warming index (reporting MCP
    # progress) before answering "not_ready". 0 = never wait.
    READY_WAIT: float = float(os.getenv("NAUTOBOT_MCP_READY_WAIT", "20"))

    # `depth` added to GET requests by the generic request tool (how deeply related objects are
    # expanded). Nautobot's own default is 0; a negative value omits the parameter entirely.
    # Higher values give richer but much larger responses.
    DEFAULT_DEPTH: int = int(os.getenv("NAUTOBOT_MCP_DEFAULT_DEPTH", "0"))
    # Longest tool response returned for an API call; list results are trimmed to fit.
    MAX_RESPONSE_CHARS: int = int(os.getenv("NAUTOBOT_MCP_MAX_RESPONSE_CHARS", "60000"))

    # Request timeouts (seconds)
    API_TIMEOUT: int = int(os.getenv("API_TIMEOUT", "10"))
    # Fetching the full Swagger schema can be slow on a large Nautobot instance
    SCHEMA_TIMEOUT: int = int(os.getenv("SCHEMA_TIMEOUT", "120"))

    # Knowledge base (semantic search over Nautobot GitHub repos). It clones and indexes
    # several large repositories, so turn it off if you only need the API tools.
    ENABLE_KB: bool = _env_bool("NAUTOBOT_MCP_ENABLE_KB", True)
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # Server identity
    API_PREFIX: str = os.getenv("API_PREFIX", "nautobot_openapi")
    SERVER_NAME: str = os.getenv("SERVER_NAME", "nautobot_mcp_server")
    SERVER_VERSION: str = os.getenv("SERVER_VERSION", "0.3.0")

    # Search
    DEFAULT_SEARCH_RESULTS: int = int(os.getenv("DEFAULT_SEARCH_RESULTS", "5"))
    KNOWLEDGE_BASE_MODE: str = os.getenv("KNOWLEDGE_BASE_MODE", "offline")

    # Logging / telemetry
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    POSTHOG_API_KEY: str = os.getenv("POSTHOG_API_KEY", "disable")

    @classmethod
    def validate(cls) -> None:
        """Run connectivity checks (network). Not called at import; see ``--check``."""
        from nautobot_mcp.utils import validate_config

        validate_config.validate_config()

    @classmethod
    def get_headers(cls) -> dict:
        """Get standardized headers for Nautobot API requests."""
        headers = {"Content-Type": "application/json"}
        if cls.NAUTOBOT_TOKEN:
            headers["Authorization"] = f"Token {cls.NAUTOBOT_TOKEN}"
        return headers

    @classmethod
    def get_full_url(cls, path: str) -> str:
        """Construct full URL for API endpoints."""
        return f"{cls.NAUTOBOT_BASE_URL.rstrip('/')}{path}"


# Create a global config instance
config = Config()
