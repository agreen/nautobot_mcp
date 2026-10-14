"""Reading Nautobot's OpenAPI document.

Nautobot generates its schema with drf-spectacular (OpenAPI 3.0): paths are relative to ``/api``,
request bodies live under ``components/schemas`` as ``<Name>Request`` / ``Patched<Name>Request``,
and fields are often wrapped in ``allOf`` / ``$ref`` indirections. Older Nautobot releases (and the
test fake's ``--openapi 2`` mode) serve Swagger 2.0 instead, with ``in: body`` parameters and
``definitions``. Everything here accepts both.
"""

from typing import Any, Dict, List, Optional, Tuple

MAX_REF_DEPTH = 8
MAX_BODY_FIELDS = 80
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def schema_format(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Which format/version the document is, and how many reusable schemas it defines."""
    if "openapi" in spec:
        version = str(spec["openapi"])
        schemas = (spec.get("components") or {}).get("schemas") or {}
        return {"format": "openapi3", "version": version, "schema_count": len(schemas)}
    if "swagger" in spec:
        return {
            "format": "swagger2",
            "version": str(spec["swagger"]),
            "schema_count": len(spec.get("definitions") or {}),
        }
    return {"format": "unknown", "version": None, "schema_count": 0}


def operations(spec: Dict[str, Any]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Every ``(path, METHOD, operation)`` in the document."""
    out = []
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                out.append((path, method.upper(), op))
    return out


def resolve_ref(spec: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """Follow a local ``#/a/b/c`` reference; an unresolvable reference yields ``{}``."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part.replace("~1", "/").replace("~0", "~"))
    return node if isinstance(node, dict) else {}


def deref(
    spec: Dict[str, Any], schema: Optional[Dict[str, Any]], _depth: int = 0
) -> Dict[str, Any]:
    """Follow ``$ref`` and merge ``allOf`` so the result describes the schema directly."""
    if not isinstance(schema, dict) or _depth > MAX_REF_DEPTH:
        return {}
    if "$ref" in schema:
        return deref(spec, resolve_ref(spec, schema["$ref"]), _depth + 1)
    if "allOf" in schema:
        merged: Dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
        properties: Dict[str, Any] = dict(merged.get("properties") or {})
        required = list(merged.get("required") or [])
        for part in schema["allOf"]:
            resolved = deref(spec, part, _depth + 1)
            properties.update(resolved.get("properties") or {})
            required.extend(resolved.get("required") or [])
            for key in ("type", "enum", "format", "description"):
                if key in resolved and key not in merged:
                    merged[key] = resolved[key]
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = sorted(set(required))
        return merged
    return schema


def describe_type(spec: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> str:
    """A short human type for a property: ``string``, ``uuid``, ``array of string``, ``object``..."""
    resolved = deref(spec, schema)
    for key in ("oneOf", "anyOf"):
        if key in resolved:
            kinds = []
            for option in resolved[key]:
                kind = describe_type(spec, option)
                if kind not in kinds and kind != "null":
                    kinds.append(kind)
            return " | ".join(kinds) or "any"
    kind = resolved.get("type")
    if kind == "array":
        return f"array of {describe_type(spec, resolved.get('items'))}"
    if kind == "string" and resolved.get("format") == "uuid":
        return "uuid"
    if kind:
        return str(kind)
    if resolved.get("properties"):
        return "object"
    return "any"


def request_body_schema(
    operation: Dict[str, Any], spec: Dict[str, Any]
) -> Dict[str, Any]:
    """The (dereferenced) JSON request-body schema of an operation, or ``{}`` if it has none."""
    body = operation.get("requestBody")  # OpenAPI 3
    if isinstance(body, dict):
        content = body.get("content") or {}
        media = content.get("application/json") or next(iter(content.values()), {})
        return deref(spec, (media or {}).get("schema"))
    for param in operation.get("parameters") or []:  # Swagger 2
        if isinstance(param, dict) and param.get("in") == "body":
            return deref(spec, param.get("schema"))
    return {}


def body_fields(
    operation: Dict[str, Any], spec: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Writable fields of an operation's request body: required first, read-only omitted."""
    schema = request_body_schema(operation, spec)
    required = set(schema.get("required") or [])
    fields = []
    for name, prop in (schema.get("properties") or {}).items():
        resolved = deref(spec, prop)
        if resolved.get("readOnly") or (prop or {}).get("readOnly"):
            continue
        description = " ".join(
            str(resolved.get("description") or resolved.get("title") or "").split()
        )
        fields.append(
            {
                "name": name,
                "type": describe_type(spec, prop),
                "required": name in required,
                "enum": resolved.get("enum"),
                "description": description[:100],
            }
        )
    fields.sort(key=lambda f: (not f["required"], f["name"]))
    return fields[:MAX_BODY_FIELDS]


def query_parameters(operation: Dict[str, Any]) -> List[str]:
    return [
        p["name"]
        for p in operation.get("parameters") or []
        if isinstance(p, dict) and p.get("in") == "query" and p.get("name")
    ]
