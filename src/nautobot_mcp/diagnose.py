"""``nautobot-mcp --diagnose``: describe the *shape* of a Nautobot instance, safely.

The curated tools and hints depend on details that differ between Nautobot versions and
installs: the schema format, which filters exist and what they accept, how related objects are
rendered at each ``depth``, and which endpoints are present. This command measures them against
the configured instance and prints a JSON report.

The report is designed to be safe to share. It contains field names, value *types*, HTTP status
codes and timings. It never contains object values, names, UUIDs, hostnames or the token. Pass
``--include-names`` to additionally list the names of the small reference vocabularies
(statuses, roles, location types, tenants), which are useful but may be considered internal.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from nautobot_mcp import __version__
from nautobot_mcp.helpers.api_hints import endpoint_app, normalize_api_path
from nautobot_mcp.helpers.openapi import (
    body_fields,
    operations,
    query_parameters,
    schema_format,
)
from nautobot_mcp.utils.config import config

# Endpoints whose shape matters to the curated tools
INSPECTED = (
    "/dcim/devices/",
    "/dcim/locations/",
    "/dcim/location-types/",
    "/ipam/ip-addresses/",
    "/ipam/prefixes/",
    "/extras/statuses/",
    "/extras/roles/",
    "/tenancy/tenants/",
)
REFERENCE = {
    "statuses": "/extras/statuses/",
    "roles": "/extras/roles/",
    "location_types": "/dcim/location-types/",
    "tenants": "/tenancy/tenants/",
}


def value_type(value: Any, depth: int = 0) -> Any:
    """The type of a value, never the value. Dicts are expanded one level to show their keys."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = value_type(value[0], depth + 1) if value else "empty"
        return {"array": inner} if depth < 2 else "array"
    if isinstance(value, dict):
        if depth >= 2:
            return "object"
        return {key: value_type(item, depth + 1) for key, item in value.items()}
    return type(value).__name__


class Prober:
    """Issues requests against the configured Nautobot and records what happened."""

    def __init__(self, timeout: Optional[float] = None) -> None:
        self.timeout = timeout or max(config.API_TIMEOUT, 15)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        began = time.monotonic()
        try:
            response = requests.get(
                config.get_full_url(path),
                headers=config.get_headers(),
                params=params,
                timeout=self.timeout,
                verify=config.SSL_VERIFY,
            )
        except Exception as e:  # noqa: BLE001 - recorded, never raised
            return {
                "status": None,
                "error": type(e).__name__,
                "ms": _ms(began),
                "data": None,
            }
        try:
            data = response.json()
        except ValueError:
            data = None
        return {
            "status": response.status_code,
            "ms": _ms(began),
            "data": data,
            "bytes": len(response.content),
        }


def _ms(began: float) -> int:
    return round((time.monotonic() - began) * 1000)


def _results(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return [r for r in data["results"] if isinstance(r, dict)]
    return []


def _name_of(obj: Any) -> Optional[str]:
    """A natural-key-ish name for a related object, tolerant of every depth."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("name", "display", "label", "model", "value"):
            if isinstance(obj.get(key), str):
                return obj[key]
    return None


def diagnose_status(prober: Prober) -> Dict[str, Any]:
    result = prober.get("/status/")
    data = result["data"] if isinstance(result["data"], dict) else {}
    apps = data.get("nautobot-apps") or data.get("plugins") or {}
    return {
        "http": result["status"],
        "ms": result["ms"],
        "keys": sorted(data),
        "nautobot_version": data.get("nautobot-version"),
        "python_version": data.get("python-version"),
        "django_version": data.get("django-version"),
        "has_nautobot_apps_key": "nautobot-apps" in data,
        "has_plugins_key": "plugins" in data,
        "apps": apps if isinstance(apps, dict) else sorted(apps),
        **({"error": result["error"]} if result.get("error") else {}),
    }


def diagnose_schema(prober: Prober) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    began = time.monotonic()
    try:
        response = requests.get(
            config.GLOBAL_TOOL_PROMPT,
            headers=config.get_headers(),
            timeout=max(config.SCHEMA_TIMEOUT, 30),
            verify=config.SSL_VERIFY,
        )
        spec = response.json()
    except Exception as e:  # noqa: BLE001
        return {"http": None, "error": type(e).__name__}, {}
    ops = operations(spec)
    apps: Dict[str, int] = {}
    for path, _, _ in ops:
        apps[endpoint_app(path)] = apps.get(endpoint_app(path), 0) + 1
    report = {
        "http": response.status_code,
        "seconds": round(time.monotonic() - began, 2),
        "megabytes": round(len(response.content) / 1_000_000, 2),
        **schema_format(spec),
        "servers": spec.get("servers"),
        "base_path": spec.get("basePath"),
        "path_count": len(spec.get("paths") or {}),
        "operation_count": len(ops),
        "operations_by_app": dict(sorted(apps.items())),
        "path_style_examples": list((spec.get("paths") or {}))[:3],
    }
    return report, spec


def diagnose_endpoint_schema(spec: Dict[str, Any], path: str) -> Dict[str, Any]:
    """What the schema says about one endpoint: filters, and writable body fields."""
    wanted = normalize_api_path(path)
    found: Dict[str, Any] = {}
    for schema_path, method, op in operations(spec):
        if normalize_api_path(schema_path) != wanted:
            continue
        entry: Dict[str, Any] = {"operation_id": op.get("operationId")}
        if method == "GET":
            names = query_parameters(op)
            entry["query_parameters"] = names
            entry["query_parameter_count"] = len(names)
        fields = body_fields(op, spec)
        if fields:
            entry["body_fields"] = [
                {
                    "name": f["name"],
                    "type": f["type"],
                    "required": f["required"],
                    "enum": bool(f["enum"]),
                }
                for f in fields
            ]
        found[method] = entry
    return found or {"absent": True}


def diagnose_samples(prober: Prober, path: str) -> Dict[str, Any]:
    """Response shapes at depth 0, 1 and 2 (types only), plus paging metadata."""
    out: Dict[str, Any] = {}
    for depth in (0, 1, 2):
        result = prober.get(path, {"limit": 1, "depth": depth})
        data = result["data"]
        entry: Dict[str, Any] = {
            "http": result["status"],
            "ms": result["ms"],
            "bytes": result.get("bytes"),
        }
        if isinstance(data, dict):
            entry["page_keys"] = sorted(k for k in data if k != "results")
            entry["count_type"] = value_type(data.get("count"))
            rows = _results(data)
            if rows:
                entry["object_fields"] = {k: value_type(v) for k, v in rows[0].items()}
        out[f"depth_{depth}"] = entry
    return out


def _first(prober: Prober, path: str, **params: Any) -> Dict[str, Any]:
    rows = _results(prober.get(path, {"limit": 1, "depth": 1, **params})["data"])
    return rows[0] if rows else {}


def run_probes(prober: Prober, present: Callable[[str], bool]) -> List[Dict[str, Any]]:
    """Try the filters the curated tools rely on; record only the request form and the outcome."""
    probes: List[Dict[str, Any]] = []

    def probe(label: str, path: str, params: Dict[str, Any]) -> None:
        result = prober.get(path, {"limit": 1, **params})
        data = result["data"]
        probes.append(
            {
                "probe": label,
                "request": f"GET {path}?limit=1&"
                + "&".join(f"{k}=<value>" for k in params),
                "http": result["status"],
                "ms": result["ms"],
                "returned_results": bool(_results(data)),
                "count_type": value_type(data.get("count"))
                if isinstance(data, dict)
                else None,
            }
        )

    if present("/dcim/devices/"):
        device = _first(prober, "/dcim/devices/")
        probe("device search by q", "/dcim/devices/", {"q": "a"})
        probe("device name contains", "/dcim/devices/", {"name__ic": "a"})
        probe("device name starts with", "/dcim/devices/", {"name__isw": "a"})
        for field in (
            "location",
            "role",
            "status",
            "platform",
            "tenant",
            "device_type",
        ):
            name = _name_of(device.get(field))
            if name:
                probe(
                    f"device filter {field} by name (natural key)",
                    "/dcim/devices/",
                    {field: name},
                )
            ident = (
                device.get(field, {}).get("id")
                if isinstance(device.get(field), dict)
                else None
            )
            if ident:
                probe(
                    f"device filter {field} by UUID", "/dcim/devices/", {field: ident}
                )
        probe("device has_primary_ip", "/dcim/devices/", {"has_primary_ip": "true"})
    if present("/ipam/prefixes/"):
        prefix = _first(prober, "/ipam/prefixes/")
        cidr = prefix.get("prefix") if isinstance(prefix.get("prefix"), str) else None
        if cidr:
            for name in (
                "prefix",
                "prefix_exact",
                "within",
                "within_include",
                "contains",
            ):
                probe(f"prefix filter {name}", "/ipam/prefixes/", {name: cidr})
            if present("/ipam/ip-addresses/"):
                probe(
                    "ip filter parent = prefix UUID",
                    "/ipam/ip-addresses/",
                    {"parent": prefix.get("id")},
                )
                probe(
                    "ip filter parent = prefix CIDR",
                    "/ipam/ip-addresses/",
                    {"parent": cidr},
                )
    if present("/ipam/ip-addresses/"):
        ip = _first(prober, "/ipam/ip-addresses/")
        address = ip.get("address") if isinstance(ip.get("address"), str) else None
        if address:
            probe(
                "ip filter address (with mask)",
                "/ipam/ip-addresses/",
                {"address": address},
            )
            probe(
                "ip filter address (host only)",
                "/ipam/ip-addresses/",
                {"address": address.split("/")[0]},
            )
            probe(
                "ip filter host", "/ipam/ip-addresses/", {"host": address.split("/")[0]}
            )
    return probes


def reference_vocabularies(prober: Prober, include_names: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, path in REFERENCE.items():
        result = prober.get(path, {"limit": 200, "depth": 1})
        rows = _results(result["data"])
        entry: Dict[str, Any] = {"http": result["status"], "count": len(rows)}
        if rows:
            entry["fields"] = {k: value_type(v) for k, v in rows[0].items()}
        if include_names:
            entry["names"] = sorted(n for n in (_name_of(r) for r in rows) if n)
        out[key] = entry
    return out


def run_diagnose(include_names: bool = False) -> Dict[str, Any]:
    prober = Prober()
    status = diagnose_status(prober)
    schema, spec = diagnose_schema(prober)

    def present(path: str) -> bool:
        wanted = normalize_api_path(path)
        return any(normalize_api_path(p) == wanted for p in (spec.get("paths") or {}))

    endpoints: Dict[str, Any] = {}
    for path in INSPECTED:
        endpoints[path] = {
            "in_schema": diagnose_endpoint_schema(spec, path),
            "responses": diagnose_samples(prober, path) if present(path) else None,
        }
    report = {
        "tool": f"nautobot-mcp {__version__} --diagnose",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "redaction": "values, names, UUIDs, hostnames and the token are never included"
        + (
            "; reference vocabulary NAMES are included (--include-names)"
            if include_names
            else ""
        ),
        "settings": {
            "ssl_verify": config.SSL_VERIFY,
            "token_configured": bool(config.NAUTOBOT_TOKEN),
            "default_depth": config.DEFAULT_DEPTH,
        },
        "status": status,
        "schema": schema,
        "endpoints": endpoints,
        "reference_vocabularies": reference_vocabularies(prober, include_names),
        "probes": run_probes(prober, present),
    }
    return report


def main(include_names: bool = False, output: Optional[str] = None) -> int:
    report = run_diagnose(include_names)
    text = json.dumps(report, indent=2, sort_keys=False)
    if output:
        with open(output, "w") as f:
            f.write(text + "\n")
        print(
            f"Wrote {output} ({len(text)} bytes). It contains no object values, UUIDs or token."
        )
    else:
        print(text)
    reachable = (
        report["status"].get("http") == 200 and report["schema"].get("http") == 200
    )
    return 0 if reachable else 1
