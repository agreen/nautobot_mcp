#!/usr/bin/env python3
"""A small fake Nautobot REST API for tests and manual testing of the MCP server.

It imitates the parts of Nautobot's API the MCP server relies on, closely enough to exercise
every code path without a real instance:

* ``GET /api/swagger.json``  Swagger 2.0 document (``basePath: /api``, relative paths,
  ``definitions`` for request bodies, ``readOnly`` fields, many filter parameters)
* ``GET /api/status/``       version / plugin information
* locations, devices and IP addresses with paging, filters (``name``, ``name__ic``, ``q``,
  ``location``), ``depth``, detail GET, POST (400 on invalid bodies), PATCH, PUT and DELETE
* auth: ``401`` for a wrong token, ``403`` for writes made with the read-only token
* ``GET /api/slow/``         sleeps (for timeout tests)

Run it standalone::

    python tests/fake_nautobot.py --port 8080 --token secret
    nautobot-mcp --check   # with NAUTOBOT_URL=http://127.0.0.1:8080 NAUTOBOT_TOKEN=secret

Options let you make the schema huge (``--extra-endpoints 2000``) or slow (``--schema-delay 5``).
"""

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

NAUTOBOT_VERSION = "2.3.4"
READONLY_TOKEN = "readonly-token"

_COMMON_FILTERS = [
    "name",
    "name__ic",
    "name__isw",
    "name__n",
    "q",
    "id",
    "status",
    "tenant",
]


def _query_param(
    name: str, type_: str = "string", description: str = ""
) -> Dict[str, Any]:
    return {
        "name": name,
        "in": "query",
        "required": False,
        "type": type_,
        "description": description,
    }


def _list_params(extra: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    params = [
        _query_param("limit", "integer", "Number of results to return per page."),
        _query_param(
            "offset", "integer", "The initial index from which to return the results."
        ),
        _query_param("depth", "integer", "Nested relationship depth."),
    ]
    for name in _COMMON_FILTERS + (extra or []):
        params.append(_query_param(name))
    # Real Nautobot list endpoints have a very large number of filters
    params.extend(_query_param(f"cf_custom_{i}") for i in range(30))
    return params


def build_schema(extra_endpoints: int = 0) -> Dict[str, Any]:
    """The Swagger document served at /api/swagger.json."""

    def ref(name: str) -> Dict[str, str]:
        return {"$ref": f"#/definitions/{name}"}

    def body(name: str) -> Dict[str, Any]:
        return {"name": "data", "in": "body", "required": True, "schema": ref(name)}

    id_param = {
        "name": "id",
        "in": "path",
        "required": True,
        "type": "string",
        "format": "uuid",
    }
    paths: Dict[str, Any] = {}
    for app, resource, definition, label, filters in [
        (
            "dcim",
            "locations",
            "WritableLocation",
            "location",
            ["location_type", "parent"],
        ),
        (
            "dcim",
            "devices",
            "WritableDevice",
            "device",
            ["location", "role", "serial", "device_type"],
        ),
        (
            "ipam",
            "ip-addresses",
            "WritableIPAddress",
            "IP address",
            ["address", "parent", "namespace"],
        ),
    ]:
        base = f"/{app}/{resource}/"
        paths[base] = {
            "get": {
                "operationId": f"{app}_{resource}_list",
                "description": f"List {label}s. Supports filtering and pagination.",
                "parameters": _list_params(filters),
            },
            "post": {
                "operationId": f"{app}_{resource}_create",
                "description": f"Create a new {label}.",
                "parameters": [body(definition)],
            },
        }
        paths[base + "{id}/"] = {
            "get": {
                "operationId": f"{app}_{resource}_read",
                "description": f"Retrieve a {label} by its UUID.",
                "parameters": [id_param],
            },
            "put": {
                "operationId": f"{app}_{resource}_update",
                "description": f"Replace a {label} (all fields).",
                "parameters": [body(definition), id_param],
            },
            "patch": {
                "operationId": f"{app}_{resource}_partial_update",
                "description": f"Update some fields of a {label}.",
                "parameters": [body(definition), id_param],
            },
            "delete": {
                "operationId": f"{app}_{resource}_delete",
                "description": f"Delete a {label}.",
                "parameters": [id_param],
            },
        }
    paths["/extras/statuses/"] = {
        "get": {
            "operationId": "extras_statuses_list",
            "description": "List statuses (objects such as devices and IPs refer to these).",
            "parameters": _list_params(),
        }
    }
    paths["/status/"] = {
        "get": {
            "operationId": "status_list",
            "description": "Nautobot instance status.",
            "parameters": [],
        }
    }
    paths["/slow/"] = {
        "get": {
            "operationId": "slow_list",
            "description": "A deliberately slow endpoint.",
            "parameters": [],
        }
    }
    for i in range(extra_endpoints):
        paths[f"/plugins/widgets{i}/"] = {
            "get": {
                "operationId": f"plugins_widgets{i}_list",
                "description": f"List widget type {i} objects provided by a plugin.",
                "parameters": _list_params(),
            }
        }

    def prop(type_: str, **kw: Any) -> Dict[str, Any]:
        return {"type": type_, **kw}

    definitions = {
        "WritableLocation": {
            "required": ["name", "location_type", "status"],
            "properties": {
                "id": prop("string", format="uuid", readOnly=True),
                "url": prop("string", readOnly=True),
                "name": prop("string", description="Name of the location"),
                "location_type": prop("string", description="Location type (UUID)"),
                "status": prop("string", description="Status (UUID or name)"),
                "parent": prop("string", description="Parent location (UUID)"),
                "description": prop("string"),
                "custom_fields": prop("object"),
            },
        },
        "WritableDevice": {
            "required": ["name", "device_type", "role", "location", "status"],
            "properties": {
                "id": prop("string", format="uuid", readOnly=True),
                "display": prop("string", readOnly=True),
                "name": prop("string", description="Device name"),
                "device_type": prop("string", description="Device type (UUID)"),
                "role": prop("string", description="Role (UUID)"),
                "location": prop("string", description="Location (UUID)"),
                "status": prop("string", description="Status (UUID or name)"),
                "serial": prop("string"),
                "tags": prop("array", items={"type": "string"}),
                "custom_fields": prop("object"),
            },
        },
        "WritableIPAddress": {
            "required": ["address", "status"],
            "properties": {
                "id": prop("string", format="uuid", readOnly=True),
                "address": prop(
                    "string", description="IP address with mask, e.g. 10.0.0.1/24"
                ),
                "status": prop("string", enum=["active", "reserved", "deprecated"]),
                "description": prop("string"),
            },
        },
    }
    return {
        "swagger": "2.0",
        "info": {"title": "Nautobot API", "version": NAUTOBOT_VERSION},
        "basePath": "/api",
        "paths": paths,
        "definitions": definitions,
    }


class FakeNautobot:
    """In-memory data plus request handling; wrapped by an HTTP server in ``serve``."""

    RESOURCES = {
        "/dcim/locations/": ("WritableLocation", ["name", "location_type", "status"]),
        "/dcim/devices/": (
            "WritableDevice",
            ["name", "device_type", "role", "location", "status"],
        ),
        "/ipam/ip-addresses/": ("WritableIPAddress", ["address", "status"]),
    }

    def __init__(
        self,
        token: str = "secret",
        schema_delay: float = 0.0,
        extra_endpoints: int = 0,
        slow_seconds: float = 3.0,
    ) -> None:
        self.token = token
        self.schema_delay = schema_delay
        self.slow_seconds = slow_seconds
        self.schema = json.dumps(build_schema(extra_endpoints)).encode()
        self.requests: List[
            Tuple[str, str, Dict[str, List[str]]]
        ] = []  # (method, path, query)
        self.store: Dict[str, Dict[str, Dict[str, Any]]] = {
            p: {} for p in self.RESOURCES
        }
        self._lock = threading.Lock()
        loc = self._create(
            "/dcim/locations/",
            {"name": "DFW1", "location_type": "Site", "status": "Active"},
        )
        for i in range(1, 121):
            self._create(
                "/dcim/devices/",
                {
                    "name": f"switch{i:03d}" if i % 2 else f"router{i:03d}",
                    "device_type": "ex4300",
                    "role": "access",
                    "location": loc["id"],
                    "status": "Active",
                },
            )
        self._create(
            "/ipam/ip-addresses/", {"address": "10.1.2.3/24", "status": "active"}
        )

    # ------------------------------------------------------------------ data helpers
    def _create(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        obj = {"id": str(uuid.uuid4()), **data}
        obj["url"] = f"/api{path}{obj['id']}/"
        self.store[path][obj["id"]] = obj
        return obj

    def _present(self, path: str, obj: Dict[str, Any], depth: int) -> Dict[str, Any]:
        out = dict(obj)
        if path == "/dcim/devices/" and depth >= 1:
            loc = self.store["/dcim/locations/"].get(obj.get("location"))
            if loc:
                out["location"] = {
                    "id": loc["id"],
                    "name": loc["name"],
                    "url": loc["url"],
                }
        if depth >= 2:
            out["_depth_note"] = (
                "x" * 300
            )  # make depth visibly increase the payload size
        return out

    @staticmethod
    def _filters(
        items: List[Dict[str, Any]], query: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        for key, values in query.items():
            if key in ("limit", "offset", "depth"):
                continue
            if key == "q":
                items = [
                    i
                    for i in items
                    if any(values[0].lower() in str(v).lower() for v in i.values())
                ]
            elif key.endswith("__ic"):
                field = key[:-4]
                items = [
                    i
                    for i in items
                    if values[0].lower() in str(i.get(field, "")).lower()
                ]
            elif key.endswith("__isw"):
                field = key[:-5]
                items = [
                    i
                    for i in items
                    if str(i.get(field, "")).lower().startswith(values[0].lower())
                ]
            else:
                items = [i for i in items if str(i.get(key)) in values]
        return items

    # ------------------------------------------------------------------ request handling
    def handle(
        self, method: str, raw_path: str, headers: Dict[str, str], body: Optional[bytes]
    ) -> Tuple[int, Any, Dict[str, str]]:
        parsed = urlparse(raw_path)
        query = parse_qs(parsed.query)
        path = parsed.path
        with self._lock:
            self.requests.append((method, path, query))

        if path == "/api/swagger.json":
            time.sleep(self.schema_delay)
            return 200, self.schema, {}
        if path in ("/api", "/api/"):  # the API root is public in Nautobot
            return (
                200,
                {"dcim": "/api/dcim/", "ipam": "/api/ipam/", "extras": "/api/extras/"},
                {},
            )
        if not path.startswith("/api/"):
            return 404, {"detail": "Not found."}, {}

        auth = headers.get("Authorization", "")
        if auth not in (f"Token {self.token}", f"Token {READONLY_TOKEN}"):
            return 401, {"detail": "Invalid token."}, {}
        readonly = auth == f"Token {READONLY_TOKEN}"
        rel = path[len("/api") :]

        if rel == "/status/":
            return (
                200,
                {
                    "nautobot-version": NAUTOBOT_VERSION,
                    "django-version": "4.2.16",
                    "python-version": "3.11.9",
                    "plugins": {
                        "nautobot_golden_config": "2.1.0",
                        "nautobot_ssot": "3.2.0",
                    },
                    "installed-apps": {"nautobot": "2.3.4", "django": "4.2.16"},
                },
                {},
            )
        if rel == "/slow/":
            time.sleep(self.slow_seconds)
            return 200, {"slow": True}, {}
        if rel == "/extras/statuses/":
            return (
                200,
                {
                    "count": 2,
                    "next": None,
                    "previous": None,
                    "results": [{"name": "Active"}, {"name": "Planned"}],
                },
                {},
            )

        # resource routes
        for base in self.RESOURCES:
            if rel == base:
                return self._collection(method, base, query, body, readonly)
            if (
                rel.startswith(base)
                and rel.endswith("/")
                and rel.count("/") == base.count("/") + 1
            ):
                return self._detail(
                    method, base, rel[len(base) : -1], query, body, readonly
                )
        return 404, {"detail": "Not found."}, {}

    def _depth(self, query: Dict[str, List[str]]) -> int:
        return int(query.get("depth", ["0"])[0])

    def _collection(self, method, base, query, body, readonly):
        if method == "GET":
            items = self._filters(list(self.store[base].values()), query)
            limit = int(query.get("limit", ["50"])[0]) or 50
            offset = int(query.get("offset", ["0"])[0])
            page = items[offset : offset + limit]
            depth = self._depth(query)
            more = offset + limit < len(items)
            return (
                200,
                {
                    "count": len(items),
                    "next": f"http://fake/api{base}?limit={limit}&offset={offset + limit}"
                    if more
                    else None,
                    "previous": None,
                    "results": [self._present(base, i, depth) for i in page],
                },
                {},
            )
        if method == "POST":
            if readonly:
                return (
                    403,
                    {"detail": "You do not have permission to perform this action."},
                    {},
                )
            data, error = self._parse(body)
            if error:
                return 400, error, {}
            _, required = self.RESOURCES[base]
            missing = {
                f: ["This field is required."] for f in required if f not in data
            }
            if missing:
                return 400, missing, {}
            return 201, self._create(base, data), {}
        return 405, {"detail": f'Method "{method}" not allowed.'}, {}

    def _detail(self, method, base, obj_id, query, body, readonly):
        obj = self.store[base].get(obj_id)
        if obj is None:
            return 404, {"detail": "Not found."}, {}
        if method == "GET":
            return 200, self._present(base, obj, self._depth(query)), {}
        if readonly:
            return (
                403,
                {"detail": "You do not have permission to perform this action."},
                {},
            )
        if method in ("PATCH", "PUT"):
            data, error = self._parse(body)
            if error:
                return 400, error, {}
            obj.update(data)
            return 200, obj, {}
        if method == "DELETE":
            del self.store[base][obj_id]
            return 204, None, {}
        return 405, {"detail": f'Method "{method}" not allowed.'}, {}

    @staticmethod
    def _parse(
        body: Optional[bytes],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}, {"detail": "JSON parse error"}
        if not isinstance(data, dict):
            return {}, {"non_field_errors": ["Invalid data. Expected a dictionary."]}
        return data, None


def serve(
    app: FakeNautobot, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """Start the fake in a background thread; ``server.server_port`` is the bound port."""

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else None
            status, data, extra = app.handle(
                method, self.path, dict(self.headers), payload
            )
            raw = (
                data
                if isinstance(data, bytes)
                else (b"" if data is None else json.dumps(data).encode())
            )
            self.send_response(status)
            if raw:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = lambda self: self._respond("GET")  # noqa: E731
        do_POST = lambda self: self._respond("POST")  # noqa: E731
        do_PUT = lambda self: self._respond("PUT")  # noqa: E731
        do_PATCH = lambda self: self._respond("PATCH")  # noqa: E731
        do_DELETE = lambda self: self._respond("DELETE")  # noqa: E731

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default="secret")
    parser.add_argument(
        "--schema-delay", type=float, default=0.0, help="seconds to delay swagger.json"
    )
    parser.add_argument(
        "--extra-endpoints", type=int, default=0, help="add N filler endpoints"
    )
    args = parser.parse_args()
    app = FakeNautobot(args.token, args.schema_delay, args.extra_endpoints)
    server = serve(app, args.host, args.port)
    print(
        f"Fake Nautobot {NAUTOBOT_VERSION} on http://{args.host}:{server.server_port}  "
        f"token={args.token!r}  read-only token={READONLY_TOKEN!r}",
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
