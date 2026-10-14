"""The Nautobot-facing tools against a fake Nautobot REST API (tests/fake_nautobot.py)."""

import json
from unittest.mock import patch

import pytest
from fastmcp import Client

from nautobot_mcp.helpers.api_hints import (
    endpoint_app,
    normalize_api_path,
    paging_hint,
)
from nautobot_mcp.helpers.startup import ENDPOINT_INDEX, ComponentState, ServerState
from nautobot_mcp.helpers.tool_handlers import (
    handle_api_request_schema,
    handle_dynamic_api_request,
)
from nautobot_mcp.main import build_app
from nautobot_mcp.utils.config import Config
from tests.fake_nautobot import READONLY_TOKEN, FakeNautobot, serve
from tests.support import BowEmbedding


@pytest.fixture
def searcher(nautobot):
    from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

    with patch(
        "nautobot_mcp.helpers.endpoint_searcher_chroma.LocalEmbeddingFunction",
        BowEmbedding,
    ):
        instance = EndpointSearcherChroma()
        assert instance.initialize_collection() is True
        yield instance


def parse(text: str):
    return json.loads(text)


# --------------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    "given,expected",
    [
        ("/dcim/devices/", "/dcim/devices/"),
        ("/api/dcim/devices/", "/dcim/devices/"),
        ("dcim/devices/", "/dcim/devices/"),
        ("  /api/ipam/prefixes/ ", "/ipam/prefixes/"),
        ("/api", "/"),
        ("/apiary/x/", "/apiary/x/"),
    ],
)
def test_normalize_api_path(given, expected):
    assert normalize_api_path(given) == expected


def test_endpoint_app():
    assert endpoint_app("/dcim/devices/") == "dcim"
    assert endpoint_app("/api/ipam/prefixes/") == "ipam"
    assert endpoint_app("/") == ""


def test_paging_hint():
    data = {"count": 120, "next": "x", "results": [1, 2, 3]}
    assert "offset=13" in paging_hint(data, 10)
    assert paging_hint({"count": 3, "next": None, "results": [1]}, 0) is None
    assert paging_hint([1, 2], 0) is None


# --------------------------------------------------------------------------- the index
def test_index_resolves_writable_body_fields_from_definitions(searcher):
    meta = {(m["method"], m["path"]): m for m in searcher.list_endpoints()}
    fields = json.loads(meta[("POST", "/dcim/devices/")]["body_fields"])
    names = [f["name"] for f in fields]
    assert names[:5] == [
        "device_type",
        "location",
        "name",
        "role",
        "status",
    ]  # required first
    assert "id" not in names and "display" not in names  # readOnly fields excluded
    assert next(f for f in fields if f["name"] == "tags")["type"] == "array of string"
    ip_fields = json.loads(meta[("POST", "/ipam/ip-addresses/")]["body_fields"])
    assert next(f for f in ip_fields if f["name"] == "status")["enum"] == [
        "active",
        "reserved",
        "deprecated",
    ]
    assert meta[("GET", "/dcim/devices/")]["body_fields"] == "[]"


def test_list_endpoints_returns_every_indexed_operation(searcher):
    endpoints = searcher.list_endpoints()
    assert len(endpoints) == len({(e["method"], e["path"]) for e in endpoints})
    assert ("PATCH", "/dcim/devices/{id}/") in {
        (e["method"], e["path"]) for e in endpoints
    }


def test_old_format_index_is_rebuilt(searcher):
    """The stored hash includes an index-format version, so older indexes re-embed."""
    hash_file = searcher._hash_file
    hash_file.write_text("hash-from-an-older-index-format")
    before = searcher.collection.count()
    with patch.object(
        searcher.collection, "upsert", wraps=searcher.collection.upsert
    ) as up:
        assert searcher.initialize_collection() is True
    assert up.called and searcher.collection.count() == before


# --------------------------------------------------------------------------- schema search
@pytest.mark.asyncio
async def test_schema_search_results_are_compact_and_actionable(searcher):
    result = parse(await handle_api_request_schema("create a new device", 8, searcher))
    by_key = {(e["method"], e["path"]): e for e in result["matching_endpoints"]}
    post = by_key[("POST", "/dcim/devices/")]
    assert post["body_fields"][0].startswith("device_type (object, required)")
    assert any(f.startswith("tags (array of string)") for f in post["body_fields"])
    assert all(not e["path"].startswith("/api") for e in result["matching_endpoints"])
    assert "nautobot_dynamic_api_request" in result["how_to_call"]
    assert result["schema_url"].endswith("/api/swagger.json")


@pytest.mark.asyncio
async def test_schema_search_caps_the_filter_list(searcher):
    result = parse(await handle_api_request_schema("list devices", 8, searcher))
    listing = next(
        e
        for e in result["matching_endpoints"]
        if (e["method"], e["path"]) == ("GET", "/dcim/devices/")
    )
    params = listing["query_parameters"]
    assert params[:3] == ["limit", "offset", "q"]
    assert params[-1].endswith("more filters") and len(params) == 41
    detail_candidates = [e for e in result["matching_endpoints"] if "{id}" in e["path"]]
    if detail_candidates:
        assert detail_candidates[0]["path_parameters"] == ["id"]


@pytest.mark.asyncio
async def test_schema_search_without_hits_explains_how_to_browse():
    class Empty:
        base_url = "http://x/api/swagger.json"

        def search(self, query, n_results=5):
            return None

    result = parse(await handle_api_request_schema("zzz", 5, Empty()))
    assert result["matching_endpoints"] == []
    assert "nautobot://api/overview" in result["hint"]


# --------------------------------------------------------------------------- API requests
@pytest.mark.asyncio
async def test_get_list_adds_depth_and_explains_paging(nautobot):
    out = parse(await handle_dynamic_api_request("GET", "/dcim/devices/", {"limit": 5}))
    assert len(out["results"]) == 5 and out["count"] == 120
    assert "offset=5" in out["_mcp_hint"]
    assert nautobot.requests[-1][2]["depth"] == ["0"]  # server-side default applied


@pytest.mark.asyncio
async def test_explicit_depth_and_disabled_default(nautobot, monkeypatch):
    await handle_dynamic_api_request("GET", "/dcim/devices/", {"limit": 1, "depth": 0})
    assert nautobot.requests[-1][2]["depth"] == ["0"]
    monkeypatch.setattr(Config, "DEFAULT_DEPTH", -1)
    await handle_dynamic_api_request("GET", "/dcim/devices/", {"limit": 1})
    assert "depth" not in nautobot.requests[-1][2]


@pytest.mark.asyncio
async def test_leading_api_prefix_is_tolerated(nautobot):
    out = parse(await handle_dynamic_api_request("get", "/api/dcim/locations/"))
    assert out["results"][0]["name"] == "DFW1"
    assert nautobot.requests[-1][1] == "/api/dcim/locations/"


@pytest.mark.asyncio
async def test_filters_work_through_the_tool(nautobot):
    out = parse(
        await handle_dynamic_api_request(
            "GET", "/dcim/devices/", {"name__ic": "ROUTER", "limit": 100}
        )
    )
    assert out["count"] == 60 and all("router" in r["name"] for r in out["results"])


@pytest.mark.asyncio
async def test_404_is_returned_as_a_structured_error_with_a_hint(nautobot):
    out = parse(await handle_dynamic_api_request("GET", "/dcim/devices/not-a-uuid/"))
    assert out["error"] is True and out["status"] == 404
    assert "Not found" in json.dumps(out["detail"])
    assert "schema search" in out["hint"]


@pytest.mark.asyncio
async def test_validation_errors_carry_nautobots_field_messages(nautobot):
    out = parse(
        await handle_dynamic_api_request("POST", "/dcim/devices/", body={"name": "x"})
    )
    assert out["status"] == 400
    assert set(out["detail"]) == {"device_type", "role", "location", "status"}
    assert "body_fields" in out["hint"]


@pytest.mark.asyncio
async def test_create_patch_delete_round_trip(nautobot):
    body = {
        "name": "core1",
        "device_type": "t",
        "role": "r",
        "location": "l",
        "status": "Active",
    }
    created = parse(
        await handle_dynamic_api_request("POST", "/dcim/devices/", body=body)
    )
    assert created["name"] == "core1"
    path = f"/dcim/devices/{created['id']}/"
    patched = parse(
        await handle_dynamic_api_request("PATCH", path, body={"serial": "S1"})
    )
    assert patched["serial"] == "S1" and patched["name"] == "core1"
    gone = parse(await handle_dynamic_api_request("DELETE", path))
    assert gone == {"status": 204, "result": "success (no content returned)"}
    assert parse(await handle_dynamic_api_request("GET", path))["status"] == 404


@pytest.mark.asyncio
async def test_bad_token_and_read_only_token(nautobot, monkeypatch):
    monkeypatch.setattr(Config, "NAUTOBOT_TOKEN", "wrong")
    out = parse(await handle_dynamic_api_request("GET", "/dcim/devices/"))
    assert out["status"] == 401 and "NAUTOBOT_TOKEN" in out["hint"]

    monkeypatch.setattr(Config, "NAUTOBOT_TOKEN", READONLY_TOKEN)
    assert "results" in parse(
        await handle_dynamic_api_request("GET", "/dcim/locations/")
    )
    denied = parse(
        await handle_dynamic_api_request("POST", "/dcim/locations/", body={"name": "x"})
    )
    assert denied["status"] == 403 and "write" in denied["hint"]


@pytest.mark.asyncio
async def test_method_not_allowed_and_unknown_method(nautobot):
    assert (
        parse(await handle_dynamic_api_request("DELETE", "/dcim/devices/"))["status"]
        == 405
    )
    out = parse(await handle_dynamic_api_request("TRACE", "/dcim/devices/"))
    assert out["error"] and "Unsupported method" in out["message"]


@pytest.mark.asyncio
async def test_timeout_and_unreachable_host_give_actionable_hints(
    nautobot, monkeypatch
):
    monkeypatch.setattr(Config, "API_TIMEOUT", 1)
    out = parse(await handle_dynamic_api_request("GET", "/slow/"))
    assert "did not answer within 1s" in out["message"] and "API_TIMEOUT" in out["hint"]

    monkeypatch.setattr(Config, "NAUTOBOT_BASE_URL", "http://127.0.0.1:9/api")
    out = parse(await handle_dynamic_api_request("GET", "/dcim/devices/"))
    assert "Could not reach Nautobot" in out["message"] and "--check" in out["hint"]


@pytest.mark.asyncio
async def test_oversized_lists_are_trimmed_with_an_explanation(nautobot, monkeypatch):
    monkeypatch.setattr(Config, "MAX_RESPONSE_CHARS", 5000)
    text = await handle_dynamic_api_request("GET", "/dcim/devices/", {"limit": 100})
    out = parse(text)
    assert len(text) < 6000 and 0 < len(out["results"]) < 100
    assert "trimmed" in out["_mcp_hint"] and out["count"] == 120


# --------------------------------------------------------------------------- resources
def ready_state(searcher) -> ServerState:
    state = ServerState(enable_kb=False)
    state.endpoint_searcher = searcher
    state.status[ENDPOINT_INDEX].state = ComponentState.READY
    return state


@pytest.mark.asyncio
async def test_overview_resource_describes_the_connected_instance(searcher):
    async with Client(build_app(ready_state(searcher))) as client:
        text = (await client.read_resource("nautobot://api/overview"))[0].text
    out = parse(text)
    assert out["nautobot"]["available"] is True
    assert out["nautobot"]["nautobot_version"] == "3.0.2"
    assert set(out["nautobot"]["plugins"]) == {
        "nautobot_golden_config",
        "nautobot_ssot",
    }
    apps = out["api"]["apps"]
    assert {"dcim", "ipam", "extras"} <= set(apps)
    assert apps["dcim"]["resources"] == ["devices", "location-types", "locations"]
    assert out["api"]["total_operations"] == len(searcher.list_endpoints())


@pytest.mark.asyncio
async def test_overview_degrades_gracefully_when_status_is_unavailable(
    searcher, monkeypatch
):
    monkeypatch.setattr(Config, "NAUTOBOT_BASE_URL", "http://127.0.0.1:9/api")
    async with Client(build_app(ready_state(searcher))) as client:
        out = parse((await client.read_resource("nautobot://api/overview"))[0].text)
    assert out["nautobot"]["available"] is False and out["nautobot"]["error"]
    assert "dcim" in out["api"]["apps"]  # the API map still comes from the index


@pytest.mark.asyncio
async def test_overview_before_the_index_is_ready_says_so(nautobot):
    async with Client(build_app(ServerState(enable_kb=False))) as client:
        out = parse((await client.read_resource("nautobot://api/overview"))[0].text)
    assert "still being built" in out["api"]


@pytest.mark.asyncio
async def test_browse_endpoints_by_app_resource(searcher):
    async with Client(build_app(ready_state(searcher))) as client:
        dcim = parse(
            (await client.read_resource("nautobot://api/endpoints/dcim"))[0].text
        )
        missing = parse(
            (await client.read_resource("nautobot://api/endpoints/nope"))[0].text
        )
    paths = {e["path"]: e["methods"] for e in dcim["endpoints"]}
    assert paths["/dcim/devices/"] == ["GET", "POST"]
    assert paths["/dcim/devices/{id}/"] == ["DELETE", "GET", "PATCH", "PUT"]
    assert dcim["endpoint_count"] == len(paths)
    assert "dcim" in missing["available_apps"] and "error" in missing


@pytest.mark.asyncio
async def test_conventions_guide_is_served_and_matches_the_tools(nautobot):
    async with Client(build_app(ServerState(enable_kb=False))) as client:
        guide = (await client.read_resource("nautobot://guide/api-conventions"))[0].text
        tools = {t.name: t for t in await client.list_tools()}
    assert (
        "relative to `/api`" in guide and "PATCH" in guide and "confirmation" in guide
    )
    desc = tools["nautobot_dynamic_api_request"].description
    assert "relative to /api" in desc and "Confirm" in desc
    assert len(desc) < 400
    assert "never guess" in tools["nautobot_openapi_api_request_schema"].description


@pytest.mark.asyncio
async def test_instructions_point_at_the_hint_resources(nautobot):
    async with Client(build_app(ServerState(enable_kb=False))) as client:
        text = client.initialize_result.instructions
    for needle in (
        "nautobot://api/overview",
        "nautobot://guide/api-conventions",
        "confirm",
    ):
        assert needle in text


@pytest.mark.asyncio
async def test_new_prompts_render(nautobot):
    async with Client(build_app(ServerState(enable_kb=False))) as client:
        safe = await client.get_prompt(
            "nautobot_safe_change", {"change": "rename switch001"}
        )
        text = safe.messages[0].content.text
        assert "rename switch001" in text and "explicit confirmation" in text
        ip = await client.get_prompt("nautobot_ip_lookup", {"address": "10.1.2.3"})
        assert "10.1.2.3" in ip.messages[0].content.text
        inv = await client.get_prompt(
            "nautobot_inventory_report", {"scope": "devices in DFW1"}
        )
        assert "read-only" in inv.messages[0].content.text.lower()


# --------------------------------------------------------------------------- end to end
@pytest.mark.asyncio
async def test_tools_end_to_end_through_an_mcp_client(searcher):
    async with Client(build_app(ready_state(searcher))) as client:
        found = await client.call_tool(
            "nautobot_openapi_api_request_schema", {"query": "list locations"}
        )
        found = parse((found.content if hasattr(found, "content") else found)[0].text)
        assert any(e["path"] == "/dcim/locations/" for e in found["matching_endpoints"])

        res = await client.call_tool(
            "nautobot_dynamic_api_request",
            {"method": "GET", "path": "/api/dcim/devices/", "params": {"limit": 2}},
        )
        body = parse((res.content if hasattr(res, "content") else res)[0].text)
        assert len(body["results"]) == 2 and "offset=2" in body["_mcp_hint"]


# --------------------------------------------------------------------------- --check
def test_check_reports_version_schema_and_auth(nautobot, capsys, monkeypatch):
    from nautobot_mcp.utils import validate_config

    results = {name: (ok, msg) for name, ok, msg in validate_config.validate_config()}
    assert results["OpenAPI Schema"][0] and "operations" in results["OpenAPI Schema"][1]
    assert results["API Endpoint"][0]
    assert results["Authentication"][0]
    assert results["Nautobot version"][0] and "3.0.2" in results["Nautobot version"][1]
    assert "nautobot_golden_config" in results["Nautobot version"][1]

    monkeypatch.setattr(Config, "NAUTOBOT_TOKEN", "wrong")
    results = {name: (ok, msg) for name, ok, msg in validate_config.validate_config()}
    assert not results["Authentication"][0] and "401" in results["Authentication"][1]
    assert not results["Nautobot version"][0]


# --------------------------------------------------------------------------- MetaMCP compatibility
# MetaMCP (and similar proxies) only serve `resources/read` for URIs that appeared in
# `resources/list`; URI templates are listed but cannot be read there. So everything a model is
# pointed at must be a concrete, listed resource.
@pytest.mark.asyncio
async def test_every_documented_resource_is_listed_concretely_and_readable(searcher):
    async with Client(build_app(ready_state(searcher))) as client:
        listed = {str(r.uri) for r in await client.list_resources()}
        for uri in (
            "nautobot://api/overview",
            "nautobot://guide/api-conventions",
            "nautobot://server/status",
            "nautobot://server/startup-log",
            "nautobot://api/endpoints/dcim",
            "nautobot://api/endpoints/ipam",
            "nautobot://api/endpoints/extras",
        ):
            assert uri in listed, uri
        # a proxy that only honors listed URIs can read every one of them
        for uri in sorted(listed):
            assert (await client.read_resource(uri))[0].text, uri


@pytest.mark.asyncio
async def test_plugin_apps_get_resources_once_the_index_is_ready(monkeypatch):
    app = FakeNautobot(token="secret", extra_endpoints=3)
    server = serve(app)
    url = f"http://127.0.0.1:{server.server_port}"
    for name, value in {
        "NAUTOBOT_URL": url,
        "NAUTOBOT_BASE_URL": url + "/api",
        "GLOBAL_TOOL_PROMPT": url + "/api/swagger.json",
        "NAUTOBOT_TOKEN": "secret",
        "READY_WAIT": 0.0,
    }.items():
        monkeypatch.setattr(Config, name, value)
    try:
        from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

        with patch(
            "nautobot_mcp.helpers.endpoint_searcher_chroma.LocalEmbeddingFunction",
            BowEmbedding,
        ):
            searcher = EndpointSearcherChroma()
            assert searcher.initialize_collection() is True
        state = ready_state(searcher)
        sessions_told = []

        class Session:
            async def send_resource_list_changed(self):
                sessions_told.append(True)

        state._sessions[1] = Session()
        async with Client(build_app(state)) as client:
            before = {str(r.uri) for r in await client.list_resources()}
            assert "nautobot://api/endpoints/plugins" not in before
            await (
                state.on_index_ready()
            )  # what the startup worker does when the index is ready
            after = {str(r.uri) for r in await client.list_resources()}
            assert "nautobot://api/endpoints/plugins" in after
            plugins = parse(
                (await client.read_resource("nautobot://api/endpoints/plugins"))[0].text
            )
            assert plugins["endpoint_count"] == 3
            assert sessions_told == [
                True
            ]  # clients were told the resource list changed
            await state.on_index_ready()  # idempotent: nothing new, nobody told again
            assert sessions_told == [True]
    finally:
        server.shutdown()


def test_hint_text_tolerates_server_name_prefixes():
    from nautobot_mcp.helpers.api_hints import schema_search_response
    from nautobot_mcp.helpers.tool_definitions import get_api_request_schema_description

    hits = [
        {"metadata": {"path": "/dcim/devices/", "method": "GET", "parameters": "[]"}}
    ]
    how = schema_search_response(hits, "q", "u")["how_to_call"]
    assert "server-name prefix" in how and "nautobot_dynamic_api_request" in how
    assert "nautobot_dynamic_api_request" in get_api_request_schema_description()


@pytest.mark.asyncio
async def test_not_ready_tells_the_model_when_to_retry():
    state = ServerState(enable_kb=False)
    state._mark(ENDPOINT_INDEX, ComponentState.INITIALIZING, "indexing Swagger schema")
    body = parse(state.not_ready_response(ENDPOINT_INDEX))
    assert body["retry_in_seconds"] == 15
    state._mark(ENDPOINT_INDEX, ComponentState.FAILED, "boom")
    assert "retry_in_seconds" not in parse(state.not_ready_response(ENDPOINT_INDEX))
