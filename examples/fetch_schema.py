"""Download Nautobot's OpenAPI schema to a local file.

Usage (reads NAUTOBOT_URL / NAUTOBOT_TOKEN / SSL_VERIFY from the environment or .env):

    NAUTOBOT_URL=https://nautobot.example.com NAUTOBOT_TOKEN=... python examples/fetch_schema.py
"""

import json

import requests

from nautobot_mcp.utils.config import config


class SchemaFetcher:
    """Fetches and saves the OpenAPI schema from a specified URL."""

    def __init__(self, token: str = ""):
        self.token = token

    def fetch_and_save(self, url, output_file):
        headers = {"Authorization": f"Token {self.token}"} if self.token else {}
        response = requests.get(
            url,
            headers=headers,
            verify=config.SSL_VERIFY,
            timeout=config.SCHEMA_TIMEOUT,
        )
        response.raise_for_status()
        with open(output_file, "w") as f:
            json.dump(response.json(), f, indent=2)
            print(f"Schema saved to {output_file}")


if __name__ == "__main__":
    SchemaFetcher(token=config.NAUTOBOT_TOKEN).fetch_and_save(
        config.GLOBAL_TOOL_PROMPT, "examples/openapi_schema.json"
    )
