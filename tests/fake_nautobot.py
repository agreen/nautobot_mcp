#!/usr/bin/env python3
"""A small fake Nautobot REST API for tests and manual testing of the MCP server.

It imitates the parts of Nautobot's API the MCP server relies on, closely enough to exercise
every code path without a real instance. Details follow Nautobot's own source (drf-spectacular
with ``SCHEMA_PATH_PREFIX_TRIM``, ``COMPONENT_SPLIT_REQUEST`` and ``COMPONENT_SPLIT_PATCH``):

* ``GET /api/swagger.json``  OpenAPI 3 document: paths relative to ``/api``, request bodies in
  ``#/components/schemas/<Name>Request`` / ``Patched<Name>Request``, many filter parameters.
  ``--openapi 2`` serves the older Swagger 2.0 shape instead.
* ``GET /api/status/``       ``nautobot-version`` and ``nautobot-apps`` (``--legacy-plugins-key``
  adds the old ``plugins`` key)
* locations, devices, IP addresses and prefixes with paging, name-or-UUID filters, ``__ic`` style
  lookups, ``q`` search, ``depth`` (related objects are minimal references at depth 0 and carry
  ``display``/``name`` from depth 1), detail GET, POST (400 on invalid bodies), PATCH, PUT, DELETE
* reference lists: statuses, roles, location types, tenants
* auth: ``401`` for a wrong token, ``403`` for writes made with the read-only token
* ``GET /api/slow/``         sleeps (for timeout tests)

Run it standalone::

    python tests/fake_nautobot.py --port 8080 --token secret
    NAUTOBOT_URL=http://127.0.0.1:8080 NAUTOBOT_TOKEN=secret nautobot-mcp --check
"""

import argparse
import ipaddress
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

NAUTOBOT_VERSION = "3.0.2"
READONLY_TOKEN = "readonly-token"
APPS = {"nautobot_golden_config": "3.0.1", "nautobot_ssot": "4.1.0"}

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


# --------------------------------------------------------------------------- schema
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


def _prop(type_: str, **kw: Any) -> Dict[str, Any]:
    return {"type": type_, **kw}


# name -> (required fields, properties); every definition is emitted for both formats
_OBJECTS: Dict[str, Tuple[List[str], Dict[str, Any]]] = {
    "Location": (
        ["name", "location_type", "status"],
        {
            "id": _prop("string", format="uuid", readOnly=True),
            "url": _prop("string", readOnly=True),
            "name": _prop("string", description="Name of the location"),
            "location_type": "REF",
            "status": "REF",
            "parent": "REF",
            "description": _prop("string"),
            "custom_fields": _prop("object"),
        },
    ),
    "Device": (
        ["name", "device_type", "role", "location", "status"],
        {
            "id": _prop("string", format="uuid", readOnly=True),
            "display": _prop("string", readOnly=True),
            "name": _prop("string", description="Device name"),
            "device_type": "REF",
            "role": "REF",
            "location": "REF",
            "status": "REF",
            "platform": "REF",
            "tenant": "REF",
            "serial": _prop("string"),
            "tags": _prop("array", items={"type": "string"}),
            "custom_fields": _prop("object"),
        },
    ),
    "IPAddress": (
        ["address", "status"],
        {
            "id": _prop("string", format="uuid", readOnly=True),
            "address": _prop(
                "string", description="IP address with mask, e.g. 10.0.0.1/24"
            ),
            "status": "REF",
            "description": _prop("string"),
        },
    ),
    "Prefix": (
        ["prefix", "status"],
        {
            "id": _prop("string", format="uuid", readOnly=True),
            "prefix": _prop("string", description="Network in CIDR notation"),
            "status": "REF",
            "location": "REF",
            "tenant": "REF",
            "description": _prop("string"),
        },
    ),
}

_ROUTES: List[Tuple[str, str, str, str, List[str]]] = [
    # app, resource, object name, human label, extra filters
    ("dcim", "locations", "Location", "location", ["location_type", "parent"]),
    (
        "dcim",
        "devices",
        "Device",
        "device",
        ["location", "role", "serial", "device_type", "platform"],
    ),
    ("ipam", "ip-addresses", "IPAddress", "IP address", ["address", "parent", "host"]),
    (
        "ipam",
        "prefixes",
        "Prefix",
        "prefix",
        ["prefix", "prefix_exact", "within", "contains", "location"],
    ),
]


def _components(openapi: int) -> Dict[str, Any]:
    """Request-body schemas, keyed by component name."""
    schemas: Dict[str, Any] = {
        "ObjectRef": {
            "type": "object",
            "description": "Reference to another object by id or natural key",
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "name": {"type": "string"},
            },
        },
        "StatusEnum": {"type": "string", "enum": ["active", "reserved", "deprecated"]},
    }
    for name, (required, props) in _OBJECTS.items():
        built: Dict[str, Any] = {}
        for prop_name, spec in props.items():
            if spec == "REF":
                if prop_name == "status" and name == "IPAddress":
                    built[prop_name] = {
                        "allOf": [{"$ref": "#/components/schemas/StatusEnum"}]
                    }
                else:
                    built[prop_name] = {
                        "allOf": [{"$ref": "#/components/schemas/ObjectRef"}]
                    }
            else:
                built[prop_name] = spec
        schemas[f"{name}Request"] = {
            "type": "object",
            "properties": built,
            "required": required,
        }
        schemas[f"Patched{name}Request"] = {"type": "object", "properties": built}
    return schemas


def build_schema(extra_endpoints: int = 0, openapi: int = 3) -> Dict[str, Any]:
    """The schema document served at /api/swagger.json (OpenAPI 3 by default, like Nautobot)."""
    v3 = openapi == 3

    def param(p: Dict[str, Any]) -> Dict[str, Any]:
        if not v3:
            return p
        out = {k: v for k, v in p.items() if k != "type"}
        out["schema"] = {"type": p["type"]}
        return out

    id_param = {"name": "id", "in": "path", "required": True, "type": "string"}
    components = _components(openapi)

    def body_ops(request_name: str) -> Dict[str, Any]:
        if v3:
            media = {"schema": {"$ref": f"#/components/schemas/{request_name}"}}
            return {
                "requestBody": {
                    "content": {
                        "application/json": media,
                        "multipart/form-data": media,
                    },
                    "required": True,
                }
            }
        # Swagger 2.0: the definition is flattened to a plain object definition
        return {
            "_swagger2_body": {
                "name": "data",
                "in": "body",
                "required": True,
                "schema": {"$ref": f"#/definitions/{request_name}"},
            }
        }

    def operation(
        op_id: str,
        description: str,
        params: List[Dict[str, Any]],
        body: Optional[str] = None,
    ):
        op: Dict[str, Any] = {
            "operationId": op_id,
            "description": description,
            "parameters": [param(p) for p in params],
        }
        if body:
            extra = body_ops(body)
            if "_swagger2_body" in extra:
                op["parameters"] = [extra["_swagger2_body"], *op["parameters"]]
            else:
                op.update(extra)
        return op

    paths: Dict[str, Any] = {}
    for app, resource, obj, label, filters in _ROUTES:
        base = f"/{app}/{resource}/"
        paths[base] = {
            "get": operation(
                f"{app}_{resource}_list",
                f"List {label}s. Supports filtering and pagination.",
                _list_params(filters),
            ),
            "post": operation(
                f"{app}_{resource}_create",
                f"Create a new {label}.",
                [],
                f"{obj}Request",
            ),
        }
        paths[base + "{id}/"] = {
            "get": operation(
                f"{app}_{resource}_retrieve",
                f"Retrieve a {label} by its UUID.",
                [id_param],
            ),
            "put": operation(
                f"{app}_{resource}_update",
                f"Replace a {label} (all fields).",
                [id_param],
                f"{obj}Request",
            ),
            "patch": operation(
                f"{app}_{resource}_partial_update",
                f"Update some fields of a {label}.",
                [id_param],
                f"Patched{obj}Request",
            ),
            "delete": operation(
                f"{app}_{resource}_destroy", f"Delete a {label}.", [id_param]
            ),
        }
    for app, resource, label in [
        ("extras", "statuses", "status"),
        ("extras", "roles", "role"),
        ("dcim", "location-types", "location type"),
        ("tenancy", "tenants", "tenant"),
    ]:
        paths[f"/{app}/{resource}/"] = {
            "get": operation(
                f"{app}_{resource}_list", f"List {label}s.", _list_params()
            )
        }
    paths["/status/"] = {
        "get": operation("status_retrieve", "Nautobot instance status.", [])
    }
    paths["/slow/"] = {
        "get": operation("slow_list", "A deliberately slow endpoint.", [])
    }
    for i in range(extra_endpoints):
        paths[f"/plugins/widgets{i}/"] = {
            "get": operation(
                f"plugins_widgets{i}_list",
                f"List widget type {i} objects provided by a plugin.",
                _list_params(),
            )
        }

    if v3:
        return {
            "openapi": "3.0.3",
            "info": {"title": "API Documentation", "version": NAUTOBOT_VERSION},
            "servers": [{"url": "/api"}],
            "paths": paths,
            "components": {"schemas": components},
        }
    return {
        "swagger": "2.0",
        "info": {"title": "Nautobot API", "version": NAUTOBOT_VERSION},
        "basePath": "/api",
        "paths": paths,
        "definitions": components,
    }


# --------------------------------------------------------------------------- data + behaviour
_REF_KINDS = {
    "status": "extras.status",
    "role": "extras.role",
    "location_type": "dcim.locationtype",
    "location": "dcim.location",
    "parent": "dcim.location",
    "device_type": "dcim.devicetype",
    "platform": "dcim.platform",
    "tenant": "tenancy.tenant",
}


_FIELD_REFKIND = {
    "status": "statuses",
    "role": "roles",
    "location_type": "location_types",
    "tenant": "tenants",
}


class FakeNautobot:
    """In-memory data plus request handling; wrapped by an HTTP server in ``serve``."""

    # path -> (object name, required fields)
    RESOURCES = {
        "/dcim/locations/": ("Location", _OBJECTS["Location"][0]),
        "/dcim/devices/": ("Device", _OBJECTS["Device"][0]),
        "/ipam/ip-addresses/": ("IPAddress", _OBJECTS["IPAddress"][0]),
        "/ipam/prefixes/": ("Prefix", _OBJECTS["Prefix"][0]),
    }
    REFERENCE = {
        "/extras/statuses/": "statuses",
        "/extras/roles/": "roles",
        "/dcim/location-types/": "location_types",
        "/tenancy/tenants/": "tenants",
    }

    def __init__(
        self,
        token: str = "secret",
        schema_delay: float = 0.0,
        extra_endpoints: int = 0,
        slow_seconds: float = 3.0,
        openapi: int = 3,
        legacy_plugins_key: bool = False,
    ) -> None:
        self.token = token
        self.schema_delay = schema_delay
        self.slow_seconds = slow_seconds
        self.legacy_plugins_key = legacy_plugins_key
        self.schema = json.dumps(build_schema(extra_endpoints, openapi)).encode()
        self.requests: List[
            Tuple[str, str, Dict[str, List[str]]]
        ] = []  # (method, path, query)
        self.store: Dict[str, Dict[str, Dict[str, Any]]] = {
            p: {} for p in self.RESOURCES
        }
        self.reference: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

        def ref_list(kind: str, names: List[str]) -> Dict[str, Dict[str, Any]]:
            return {
                (i := str(uuid.uuid4())): {
                    "id": i,
                    "name": n,
                    "object_type": _REF_KINDS.get(kind, kind),
                }
                for n in names
            }

        self.reference = {
            "statuses": ref_list("status", ["Active", "Planned", "Decommissioning"]),
            "roles": ref_list("role", ["access", "core", "distribution"]),
            "location_types": ref_list("location_type", ["Region", "Site"]),
            "tenants": ref_list("tenant", ["Acme", "Globex"]),
        }
        self._by_name = {
            kind: {obj["name"]: obj for obj in objs.values()}
            for kind, objs in self.reference.items()
        }
        site = self._by_name["location_types"]["Site"]
        active = self._by_name["statuses"]["Active"]
        loc = self._create(
            "/dcim/locations/",
            {"name": "DFW1", "location_type": site["id"], "status": active["id"]},
        )
        for i in range(1, 121):
            self._create(
                "/dcim/devices/",
                {
                    "name": f"switch{i:03d}" if i % 2 else f"router{i:03d}",
                    "device_type": "ex4300",
                    "role": self._by_name["roles"]["access" if i <= 100 else "core"][
                        "id"
                    ],
                    "location": loc["id"],
                    "status": active["id"],
                    "serial": f"SN{i:05d}",
                },
            )
        prefix = self._create(
            "/ipam/prefixes/",
            {"prefix": "10.1.2.0/24", "status": active["id"], "location": loc["id"]},
        )
        self._create(
            "/ipam/prefixes/", {"prefix": "10.1.0.0/16", "status": active["id"]}
        )
        for host in (3, 4, 5):
            self._create(
                "/ipam/ip-addresses/",
                {
                    "address": f"10.1.2.{host}/24",
                    "status": active["id"],
                    "parent": prefix["id"],
                },
            )

    # ------------------------------------------------------------------ data helpers
    def _create(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        obj = {"id": str(uuid.uuid4()), **data}
        obj["url"] = f"/api{path}{obj['id']}/"
        self.store[path][obj["id"]] = obj
        return obj

    def _resolve_ref(self, field: str, value: Any, depth: int) -> Any:
        """Render a stored reference (UUID, name or None) the way Nautobot would at ``depth``."""
        if value is None or isinstance(value, dict):
            return value
        kind = _FIELD_REFKIND.get(field)
        if kind:
            obj = self.reference[kind].get(value) or self._by_name[kind].get(value)
            if obj:
                return self._render(obj, obj["object_type"], depth)
        if field in ("location", "parent"):
            target = self.store["/dcim/locations/"].get(value)
            if target:
                return self._render(target, "dcim.location", depth)
        # an unknown reference (e.g. a device type given by name): minimal reference, named from depth 1
        return self._render(
            {"id": value, "name": str(value), "url": f"/api/{field}/{value}/"},
            _REF_KINDS.get(field, field),
            depth,
        )

    @staticmethod
    def _render(obj: Dict[str, Any], object_type: str, depth: int) -> Dict[str, Any]:
        base = {
            "id": obj["id"],
            "object_type": object_type,
            "url": obj.get("url", f"/api/{object_type}/{obj['id']}/"),
        }
        if depth >= 1:
            base.update(
                {
                    "display": obj.get("name", obj["id"]),
                    "name": obj.get("name", obj["id"]),
                }
            )
        return base

    def _present(self, path: str, obj: Dict[str, Any], depth: int) -> Dict[str, Any]:
        out = dict(obj)
        for field in (
            "status",
            "role",
            "location_type",
            "location",
            "parent",
            "tenant",
            "platform",
            "device_type",
        ):
            if field in out:
                out[field] = self._resolve_ref(field, out[field], depth)
        if path == "/dcim/devices/":
            out["display"] = obj["name"]
            out["primary_ip4"] = None
        if depth >= 2:
            out["_depth_note"] = (
                "x" * 300
            )  # make depth visibly increase the payload size
        return out

    def _matches_ref(self, obj: Dict[str, Any], field: str, wanted: List[str]) -> bool:
        raw = obj.get(field)
        if raw is None:
            return False
        names = {str(raw)}
        for kind in ("statuses", "roles", "location_types", "tenants"):
            hit = self.reference[kind].get(raw)
            if hit:
                names.add(hit["name"])
        loc = self.store["/dcim/locations/"].get(raw)
        if loc:
            names.add(loc["name"])
        return any(w in names for w in wanted)

    def _filters(
        self, base: str, items: List[Dict[str, Any]], query: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        for key, values in query.items():
            if key in ("limit", "offset", "depth"):
                continue
            if key == "q":
                needle = values[0].lower()
                items = [
                    i
                    for i in items
                    if any(needle in str(v).lower() for v in i.values())
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
            elif key.endswith("__n"):
                field = key[:-3]
                items = [i for i in items if str(i.get(field)) not in values]
            elif (
                key
                in ("prefix", "within", "within_include", "contains", "prefix_exact")
                and base == "/ipam/prefixes/"
            ):
                items = [
                    i for i in items if self._prefix_match(key, i["prefix"], values)
                ]
            elif key == "parent" and base == "/ipam/ip-addresses/":
                items = [i for i in items if i.get("parent") in values]
            elif key == "address" and base == "/ipam/ip-addresses/":
                items = [
                    i
                    for i in items
                    if i["address"] in values or i["address"].split("/")[0] in values
                ]
            elif key == "host" and base == "/ipam/ip-addresses/":
                items = [i for i in items if i["address"].split("/")[0] in values]
            elif key in _REF_KINDS:
                items = [i for i in items if self._matches_ref(i, key, values)]
            else:
                items = [i for i in items if str(i.get(key)) in values]
        return items

    @staticmethod
    def _prefix_match(kind: str, candidate: str, values: List[str]) -> bool:
        net = ipaddress.ip_network(candidate, strict=False)
        for v in values:
            other = ipaddress.ip_network(v, strict=False)
            if kind == "prefix_exact" and net == other:
                return True
            if kind == "within" and net != other and net.subnet_of(other):
                return True
            if kind in ("prefix", "within_include") and net.subnet_of(other):
                return True
            if kind == "contains" and other.subnet_of(net):
                return True
        return False

    # ------------------------------------------------------------------ request handling
    def handle(
        self, method: str, raw_path: str, headers: Dict[str, str], body: Optional[bytes]
    ) -> Tuple[int, Any, Dict[str, str]]:
        parsed = urlparse(raw_path)
        query = parse_qs(parsed.query)
        path = parsed.path
        with self._lock:
            self.requests.append((method, path, query))

        auth = headers.get("Authorization", "")
        if path == "/api/swagger.json":
            # Nautobot requires authentication for its schema (SERVE_PERMISSIONS = IsAuthenticated)
            if auth not in (f"Token {self.token}", f"Token {READONLY_TOKEN}"):
                return (
                    403,
                    {"detail": "Authentication credentials were not provided."},
                    {},
                )
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

        if auth not in (f"Token {self.token}", f"Token {READONLY_TOKEN}"):
            return 401, {"detail": "Invalid token."}, {}
        readonly = auth == f"Token {READONLY_TOKEN}"
        rel = path[len("/api") :]

        if rel == "/status/":
            payload: Dict[str, Any] = {
                "nautobot-version": NAUTOBOT_VERSION,
                "django-version": "4.2.16",
                "python-version": "3.12.4",
                "installed-apps": {"nautobot": NAUTOBOT_VERSION, "django": "4.2.16"},
                "nautobot-apps": APPS,
                "celery-workers-running": 1,
            }
            if self.legacy_plugins_key:
                payload["plugins"] = APPS
            return 200, payload, {}
        if rel == "/slow/":
            time.sleep(self.slow_seconds)
            return 200, {"slow": True}, {}
        if rel in self.REFERENCE:
            items = list(self.reference[self.REFERENCE[rel]].values())
            items = self._filters(rel, items, query)
            depth = self._depth(query)
            limit = int(query.get("limit", ["50"])[0]) or 50
            offset = int(query.get("offset", ["0"])[0])
            page = items[offset : offset + limit]
            return (
                200,
                {
                    "count": len(items),
                    "next": None,
                    "previous": None,
                    "results": [
                        self._render(i, i["object_type"], max(depth, 1)) for i in page
                    ],
                },
                {},
            )

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
            items = self._filters(base, list(self.store[base].values()), query)
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
    parser.add_argument(
        "--openapi", type=int, choices=(2, 3), default=3, help="schema format to serve"
    )
    parser.add_argument(
        "--legacy-plugins-key",
        action="store_true",
        help="also return the old `plugins` key",
    )
    args = parser.parse_args()
    app = FakeNautobot(
        args.token,
        args.schema_delay,
        args.extra_endpoints,
        openapi=args.openapi,
        legacy_plugins_key=args.legacy_plugins_key,
    )
    server = serve(app, args.host, args.port)
    print(
        f"Fake Nautobot {NAUTOBOT_VERSION} (OpenAPI {args.openapi}) on http://{args.host}:{server.server_port}  "
        f"token={args.token!r}  read-only token={READONLY_TOKEN!r}",
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
