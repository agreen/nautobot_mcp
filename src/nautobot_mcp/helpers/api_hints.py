"""Hints that help an LLM use the Nautobot API correctly.

Everything here is about *shape*: turning raw OpenAPI search hits into compact, actionable
results, turning HTTP failures into explanations, and the static conventions guide served as
the ``nautobot://guide/api-conventions`` resource. The exact parameter names for the connected
instance always come from the schema search; the guide only covers general conventions.
"""

import json
from typing import Any, Dict, List, Optional

# Query parameters worth listing first because they apply to almost every list endpoint
_COMMON_QUERY = ("limit", "offset", "q", "depth", "brief", "id", "name")
MAX_QUERY_PARAMS_SHOWN = 40
MAX_BODY_FIELDS_SHOWN = 60
MAX_FIELD_DESCRIPTION = 100

API_CONVENTIONS_MD = """\
# Nautobot REST API conventions

General rules that hold across Nautobot's REST API. For the exact endpoints, filter names and
body fields of *this* instance, use the API schema search tool; read `nautobot://api/overview`
for its version and installed apps.

## Workflow
1. Search the schema for the operation you need. Do not guess paths or field names; names and
   available filters differ between Nautobot versions and installed plugins.
2. Call the endpoint with `nautobot_dynamic_api_request`. Paths are relative to `/api`
   (`/dcim/devices/`, not `/api/dcim/devices/`) and normally end with a trailing slash.
3. For anything that changes data (POST, PUT, PATCH, DELETE): state exactly what will change and
   get the user's confirmation first. Prefer a GET beforehand to confirm the target object.

## Reading data
- List endpoints return `{"count", "next", "previous", "results": [...]}`. Results are paginated
  (the default page size is typically 50). Use `limit` and `offset`, and follow `next` for more.
- Objects are identified by UUID. A single object lives at `/<app>/<resource>/<uuid>/`.
- Filter with query parameters named after the filter fields, for example `name=router1` or
  `status=active`. Repeat a parameter to match any of several values. Many fields accept lookup
  suffixes such as `name__ic` (contains, case-insensitive), `name__isw` (starts with),
  `name__n` (not), and `__gte` / `__lte` for numbers and dates.
- `q=<text>` runs a general search on endpoints that support it.
- Related objects (location, tenant, status, role, ...) are usually filtered by name or UUID. Check
  the endpoint's `query_parameters` in the schema search result for what is accepted.
- `depth` controls how much detail nested related objects carry in responses. Higher values give
  more detail and much larger responses. This server adds a default `depth` to GET requests (see
  the tool description); pass `depth` explicitly to override it.
- Keep responses small: filter, set a modest `limit`, and fetch further pages only if needed.

## Changing data
- POST creates (body = the object's fields; required fields are marked in `body_fields`).
- PATCH updates only the fields you send. Prefer PATCH over PUT, which replaces the whole object.
- DELETE removes an object at its detail URL. Deletion can be refused if other objects depend on it.
- Related objects in a body are normally given by UUID (some versions also accept a name or other
  natural key). Look the object up first instead of inventing identifiers.
- Custom fields live under `custom_fields`.
- Validation errors come back as HTTP 400 with a JSON body naming each offending field.

## Interpreting errors
- 400: invalid body or filter; the response names the fields. Fix them and retry.
- 401 / 403: missing or invalid token / insufficient permissions for this action.
- 404: wrong path or UUID. Re-run the schema search; check the trailing slash.
- 405: that method is not supported on this endpoint.
- 409: conflict, for example the object is referenced elsewhere.
"""


def normalize_api_path(path: str) -> str:
    """Return ``path`` relative to ``/api`` with a leading slash.

    Models sometimes pass ``/api/dcim/devices/`` (as seen in a browser or in docs); the request
    tool prepends ``/api`` itself, so that would otherwise become ``/api/api/...``.
    """
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if path == "/api" or path.startswith("/api/"):
        path = path[len("/api") :] or "/"
    return path


def endpoint_app(path: str) -> str:
    """The Nautobot app an endpoint belongs to (``/dcim/devices/`` -> ``dcim``)."""
    parts = [p for p in normalize_api_path(path).split("/") if p]
    return parts[0] if parts else ""


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _short(text: Any, limit: int = MAX_FIELD_DESCRIPTION) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_field(field: Dict[str, Any]) -> str:
    bits = [str(field.get("type") or "any")]
    if field.get("required"):
        bits.append("required")
    if field.get("enum"):
        bits.append("one of: " + ", ".join(str(v) for v in field["enum"][:12]))
    desc = _short(field.get("description"))
    return f"{field.get('name')} ({', '.join(bits)})" + (f": {desc}" if desc else "")


def shape_endpoint(hit: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a raw search hit into a compact, LLM-friendly endpoint description."""
    meta = hit.get("metadata") or {}
    path = normalize_api_path(meta.get("path", ""))
    method = (meta.get("method") or "").upper()
    params: List[Dict[str, Any]] = _loads(meta.get("parameters"), [])
    body_fields: List[Dict[str, Any]] = _loads(meta.get("body_fields"), [])

    out: Dict[str, Any] = {
        "method": method,
        "path": path,
        "description": _short(meta.get("description") or hit.get("document"), 220),
    }
    if meta.get("operation_id"):
        out["operation_id"] = meta["operation_id"]

    path_params = [p["name"] for p in params if p.get("in") == "path" and p.get("name")]
    if path_params:
        out["path_parameters"] = path_params  # e.g. ["id"]: the object's UUID

    query = [p["name"] for p in params if p.get("in") == "query" and p.get("name")]
    if query:
        first = [n for n in _COMMON_QUERY if n in query]
        rest = [n for n in query if n not in first]
        names = first + rest
        shown = names[:MAX_QUERY_PARAMS_SHOWN]
        out["query_parameters"] = shown + (
            [f"... {len(names) - len(shown)} more filters"]
            if len(names) > len(shown)
            else []
        )

    if body_fields:
        shown_fields = body_fields[:MAX_BODY_FIELDS_SHOWN]
        out["body_fields"] = [_describe_field(f) for f in shown_fields]
        if len(body_fields) > len(shown_fields):
            out["body_fields"].append(
                f"... {len(body_fields) - len(shown_fields)} more fields"
            )
    elif any(p.get("in") == "body" for p in params):
        out["body_fields"] = "see the object's fields via a GET on its detail URL"
    return out


def schema_search_response(
    hits: Optional[List[Dict[str, Any]]], query: str, schema_url: str
) -> Dict[str, Any]:
    """The full tool result for a schema search."""
    if not hits:
        return {
            "matching_endpoints": [],
            "hint": (
                "No endpoint matched. Try different words (resource names such as 'device', "
                "'ip address', 'location'), or browse an app's endpoints with the "
                "nautobot://api/endpoints/{app} resource (apps are listed in "
                "nautobot://api/overview)."
            ),
        }
    return {
        "matching_endpoints": [shape_endpoint(h) for h in hits],
        "how_to_call": (
            "Use the API request tool (nautobot_dynamic_api_request; your client may show it with "
            "a server-name prefix) with the method and path shown (relative to /api). "
            "Replace {id} with the object's UUID. For GET, query_parameters are the accepted "
            "filters (add __ic for case-insensitive contains, e.g. name__ic=sw). For writes, "
            "body_fields lists the fields; confirm changes with the user first."
        ),
        "schema_url": schema_url,
    }


_STATUS_HINTS = {
    400: "The request was rejected as invalid. The detail names the offending fields or filters; "
    "check them against the schema search result (body_fields / query_parameters) and retry.",
    401: "Authentication failed. The server's NAUTOBOT_TOKEN is missing, invalid or expired.",
    403: "The token is valid but not allowed to do this. It may lack permission for this "
    "object or action (writes need a token with write access).",
    404: "Not found. Check the path (relative to /api, with a trailing slash) and that any UUID "
    "exists; re-run the schema search if unsure.",
    405: "That HTTP method is not supported on this endpoint. Use the schema search to see which "
    "methods the path allows (list endpoints take GET/POST; detail endpoints GET/PUT/PATCH/DELETE).",
    409: "Conflict, often because other objects still reference this one.",
    429: "Rate limited by Nautobot or a proxy in front of it. Wait and retry.",
}


def error_hint(status: int) -> str:
    if status in _STATUS_HINTS:
        return _STATUS_HINTS[status]
    if 500 <= status < 600:
        return (
            "Nautobot reported a server-side error. Retrying once is reasonable; if it persists, "
            "report it, and consider whether the request (huge `depth`, very large `limit`) is "
            "too heavy."
        )
    return "Unexpected response from Nautobot."


def paging_hint(data: Dict[str, Any], offset: int) -> Optional[str]:
    """Explain how to fetch the rest of a paginated list response."""
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not data.get("next"):
        return None
    shown = len(results)
    return (
        f"Showing {shown} of {data.get('count', '?')} results starting at offset {offset}. "
        f"For more, repeat the request with offset={offset + shown} (and the same filters), or "
        "narrow the filters."
    )
