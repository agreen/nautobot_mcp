"""OpenAPI helpers and the redacted --diagnose report."""

import json
import re

from nautobot_mcp import diagnose
from nautobot_mcp.helpers import openapi

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SPEC3 = {
    "openapi": "3.0.3",
    "components": {
        "schemas": {
            "Status": {"type": "string", "enum": ["active", "planned"]},
            "DeviceRequest": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "id": {"type": "string", "format": "uuid", "readOnly": True},
                    "name": {"type": "string", "description": "Device name"},
                    "status": {"allOf": [{"$ref": "#/components/schemas/Status"}]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
    "paths": {
        "/dcim/devices/": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/DeviceRequest"}
                        }
                    }
                }
            },
            "get": {"parameters": [{"in": "query", "name": "q"}]},
        }
    },
}


def test_schema_format_detects_versions():
    assert openapi.schema_format(SPEC3)["format"] == "openapi3"
    assert openapi.schema_format({"swagger": "2.0"})["format"] == "swagger2"
    assert openapi.schema_format({})["format"] == "unknown"


def test_body_fields_resolve_refs_allof_and_skip_read_only():
    op = SPEC3["paths"]["/dcim/devices/"]["post"]
    fields = {f["name"]: f for f in openapi.body_fields(op, SPEC3)}
    assert "id" not in fields
    assert fields["name"]["required"] and fields["name"]["type"] == "string"
    assert fields["status"]["enum"] == ["active", "planned"]
    assert fields["tags"]["type"] == "array of string"
    assert list(fields)[0] == "name"  # required first


def test_swagger2_body_parameter_still_supported():
    spec = {
        "swagger": "2.0",
        "definitions": {"D": {"properties": {"name": {"type": "string"}}}},
    }
    op = {"parameters": [{"in": "body", "schema": {"$ref": "#/definitions/D"}}]}
    assert [f["name"] for f in openapi.body_fields(op, spec)] == ["name"]


def test_cyclic_refs_terminate():
    spec = {"components": {"schemas": {"A": {"$ref": "#/components/schemas/A"}}}}
    assert openapi.deref(spec, {"$ref": "#/components/schemas/A"}) == {}


def test_query_parameters_and_operations():
    ops = {(p, m) for p, m, _ in openapi.operations(SPEC3)}
    assert ops == {("/dcim/devices/", "POST"), ("/dcim/devices/", "GET")}
    assert openapi.query_parameters(SPEC3["paths"]["/dcim/devices/"]["get"]) == ["q"]


def test_value_type_never_returns_values():
    shape = diagnose.value_type({"name": "secret-router", "n": 3, "t": [1, 2]})
    assert "secret-router" not in json.dumps(shape)


def test_diagnose_report_is_redacted(nautobot):
    report = json.dumps(diagnose.run_diagnose(include_names=False))
    assert "secret" not in report  # the fake's token
    assert "127.0.0.1" not in report
    assert not UUID.search(report)
    assert "redaction" in report
