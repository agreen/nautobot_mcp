"""Shared test helpers: a bag-of-words embedding and a started fake Nautobot."""

import re
import zlib

from chromadb import EmbeddingFunction

from nautobot_mcp.utils.config import Config
from tests.fake_nautobot import FakeNautobot, serve


class BowEmbedding(EmbeddingFunction):
    """Deterministic bag-of-words embedding: similar wording lands close together."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, input):
        out = []
        for text in input:
            vec = [0.0] * 256
            for word in re.findall(r"[a-z]+", text.lower()):
                vec[zlib.crc32(word.encode()) % 256] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out

    @staticmethod
    def name():
        return "bow"


def start_fake(monkeypatch, **kwargs):
    """Start a fake Nautobot and point the (class-level) config at it. Returns (app, server)."""
    app = FakeNautobot(token="secret", slow_seconds=3, **kwargs)
    server = serve(app)
    url = f"http://127.0.0.1:{server.server_port}"
    # get_headers/get_full_url are classmethods, so patch the class, not the instance
    for name, value in {
        "NAUTOBOT_URL": url,
        "NAUTOBOT_BASE_URL": url + "/api",
        "GLOBAL_TOOL_PROMPT": url + "/api/swagger.json",
        "NAUTOBOT_TOKEN": "secret",
        "SSL_VERIFY": True,
        "API_TIMEOUT": 10,
        "SCHEMA_TIMEOUT": 10,
        "DEFAULT_DEPTH": 0,
        "MAX_RESPONSE_CHARS": 60000,
        "READY_WAIT": 0.0,
    }.items():
        monkeypatch.setattr(Config, name, value)
    return app, server
