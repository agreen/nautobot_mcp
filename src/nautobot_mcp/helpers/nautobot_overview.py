"""Facts about the connected Nautobot instance, served as MCP resources.

An LLM's idea of Nautobot comes from training data that mixes several major versions. These
helpers describe the *actual* instance: its version and plugins (from ``/api/status/``) and the
apps/resources its API really exposes (from the indexed OpenAPI schema).
"""

import json
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests

from nautobot_mcp.helpers.api_hints import endpoint_app, normalize_api_path
from nautobot_mcp.utils.config import config

logger = logging.getLogger("nautobot_mcp")

INFO_TTL_SECONDS = 300
MAX_RESOURCES_PER_APP = 60


def fetch_instance_info() -> Dict[str, Any]:
    """Read ``/api/status/`` (blocking). Never raises; failures are reported in the result."""
    try:
        response = requests.get(
            config.get_full_url("/status/"),
            headers=config.get_headers(),
            timeout=config.API_TIMEOUT,
            verify=config.SSL_VERIFY,
        )
        response.raise_for_status()
        raw = response.json()
    except Exception as e:  # noqa: BLE001 - descriptive failure is the result
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    # Nautobot 3 reports apps under `nautobot-apps`; `plugins` is the deprecated name (and is
    # slated for removal), so read both and let the new key win.
    plugins: Dict[str, Any] = {}
    for key in ("plugins", "nautobot-apps"):
        value = raw.get(key) or {}
        plugins.update(
            value if isinstance(value, dict) else {name: None for name in value}
        )
    return {
        "available": True,
        "nautobot_version": raw.get("nautobot-version"),
        "python_version": raw.get("python-version"),
        "django_version": raw.get("django-version"),
        "plugins": plugins,
        "installed_app_count": len(raw.get("installed-apps") or {}),
    }


class InstanceInfoCache:
    """Caches ``fetch_instance_info`` briefly so repeated resource reads stay cheap."""

    def __init__(self, ttl: float = INFO_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._value: Optional[Dict[str, Any]] = None
        self._at = 0.0

    def get(self) -> Dict[str, Any]:
        now = time.monotonic()
        if (
            self._value is None
            or now - self._at > self._ttl
            or not self._value.get("available")
        ):
            # Failures are retried on the next read rather than cached for minutes.
            self._value = fetch_instance_info()
            self._at = now
        return self._value


def _group(endpoints: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    apps: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"operations": 0, "paths": set()}
    )
    for meta in endpoints:
        path = normalize_api_path(meta.get("path", ""))
        app = endpoint_app(path)
        if not app:
            continue
        apps[app]["operations"] += 1
        apps[app]["paths"].add(path)
    return apps


def _resource_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[1] if len(parts) > 1 else ""


def build_overview(endpoints: List[Dict[str, Any]], info: Dict[str, Any]) -> str:
    """JSON overview: instance facts plus a map of API apps to their resources."""
    apps = _group(endpoints)
    api_apps: Dict[str, Any] = {}
    for app in sorted(apps):
        resources = sorted({_resource_name(p) for p in apps[app]["paths"]} - {""})
        entry: Dict[str, Any] = {
            "operations": apps[app]["operations"],
            "resources": resources[:MAX_RESOURCES_PER_APP],
        }
        if len(resources) > MAX_RESOURCES_PER_APP:
            entry["more_resources"] = len(resources) - MAX_RESOURCES_PER_APP
        api_apps[app] = entry
    return json.dumps(
        {
            "nautobot": {"url": config.NAUTOBOT_URL, **info},
            "api": {
                "total_operations": len(endpoints),
                "apps": api_apps,
            },
            "how_to_use": [
                "Search endpoints by meaning with the API schema search tool.",
                "Browse every endpoint of an app with the nautobot://api/endpoints/{app} resource.",
                "Read nautobot://guide/api-conventions for filtering, paging and write conventions.",
            ],
        },
        indent=2,
    )


def build_app_endpoints(endpoints: List[Dict[str, Any]], app: str) -> str:
    """JSON list of one app's endpoints with the methods each path supports."""
    app = app.strip().strip("/").lower()
    selected = [m for m in endpoints if endpoint_app(m.get("path", "")) == app]
    if not selected:
        return json.dumps(
            {
                "error": f"No endpoints found for app '{app}'.",
                "available_apps": sorted(_group(endpoints)),
            },
            indent=2,
        )
    by_path: Dict[str, Dict[str, Any]] = {}
    for meta in selected:
        path = normalize_api_path(meta.get("path", ""))
        entry = by_path.setdefault(path, {"path": path, "methods": []})
        entry["methods"].append((meta.get("method") or "").upper())
        if "description" not in entry and meta.get("description"):
            entry["description"] = " ".join(str(meta["description"]).split())[:160]
    paths = [by_path[p] for p in sorted(by_path)]
    for entry in paths:
        entry["methods"] = sorted(set(entry["methods"]))
    return json.dumps(
        {"app": app, "endpoint_count": len(paths), "endpoints": paths}, indent=2
    )
