"""Nautobot MCP server."""

from importlib import metadata

try:
    __version__ = metadata.version("nautobot-mcp")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+local"
