"""Startup reporting: log notifications, progress, resources, prompts, instructions."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastmcp import Client

from nautobot_mcp.helpers.startup import (
    ENDPOINT_INDEX,
    KB_INDEX,
    ComponentState,
    ServerState,
)
from nautobot_mcp.main import build_app


class FakeSearcher:
    base_url = "http://nautobot.test/api/swagger.json"

    def __init__(self, delay=0.0, count=7):
        self._delay = delay
        self.collection = MagicMock()
        self.collection.count.return_value = count
        self.last_error = None

    def initialize_collection(self, force=False, progress_cb=None):
        steps = 4
        for i in range(steps + 1):
            if progress_cb:
                progress_cb(i, steps)
            time.sleep(self._delay / steps)
        return True

    def search(self, query, n_results=5):
        return [
            {
                "document": "GET /dcim/devices/ - List devices",
                "metadata": {
                    "path": "/dcim/devices/",
                    "method": "GET",
                    "description": "List devices",
                    "parameters": json.dumps(
                        [
                            {"name": "limit", "in": "query"},
                            {"name": "name", "in": "query"},
                        ]
                    ),
                },
            }
        ]


class FakeKB:
    def initialize_all_repositories(self, force=False, progress_cb=None):
        if progress_cb:
            progress_cb(0, 1, "nautobot/nautobot")
            progress_cb(1, 1, "")
        return {}

    def get_repository_stats(self):
        return {"nautobot/nautobot": {"document_count": 5}}


class FakeSession:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    async def send_log_message(self, level, data, logger=None):
        if self._fail:
            raise RuntimeError("client went away")
        self.sent.append((level, data, logger))


class FakeCtx:
    def __init__(self, supports_message=True):
        self.calls = []
        self._supports_message = supports_message

    async def report_progress(self, progress, total=None, message=None):
        if message is not None and not self._supports_message:
            raise TypeError("unexpected keyword 'message'")
        self.calls.append((progress, total, message))


def make_state(searcher=None, kb=None, enable_kb=True):
    state = ServerState(enable_kb=enable_kb)
    state._build_endpoint_searcher = lambda: searcher or FakeSearcher()
    state._build_kb = lambda: kb or FakeKB()
    return state


async def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# --------------------------------------------------------------------------- log events
@pytest.mark.asyncio
async def test_late_session_gets_recent_events_replayed():
    state = make_state()
    state.emit("info", "first")
    state.emit("warning", "second", component=ENDPOINT_INDEX)
    session = FakeSession()
    state.register_session(session)
    await asyncio.sleep(0.05)
    assert [(lvl, data) for lvl, data, _ in session.sent] == [
        ("info", "first"),
        ("warning", "[endpoint_index] second"),
    ]
    assert session.sent[1][2] == "nautobot_mcp.endpoint_index"
    assert session.sent[0][2] == "nautobot_mcp"


@pytest.mark.asyncio
async def test_session_registered_once_and_receives_live_phase_events():
    state = make_state()
    session = FakeSession()
    state.register_session(session)
    state.register_session(session)  # idempotent
    state.start()
    await wait_for(lambda: state.snapshot()["ready"])
    await asyncio.sleep(0.05)
    texts = [d for _, d, _ in session.sent]
    assert any("loading embedding model" in t for t in texts)
    assert any("indexing Swagger schema" in t for t in texts)
    assert any(t.startswith("[endpoint_index] ready in") for t in texts)
    assert any("all components ready" in t for t in texts)
    assert len(texts) == len(set(id(e) for e in state.events))  # no duplicate delivery
    await state.stop()


@pytest.mark.asyncio
async def test_log_level_filters_what_clients_receive():
    state = make_state()
    session = FakeSession()
    state.register_session(session)
    state.set_log_level("warning")
    state.emit("info", "chatty")
    state.emit("error", "important")
    await asyncio.sleep(0.05)
    assert [d for _, d, _ in session.sent] == ["important"]
    state.set_log_level("not-a-level")  # ignored
    assert state.log_level == "warning"


@pytest.mark.asyncio
async def test_events_emitted_from_worker_threads_are_delivered():
    state = make_state()
    session = FakeSession()
    state.register_session(session)
    state.start()
    await asyncio.to_thread(state.emit, "info", "from a worker thread")
    await asyncio.sleep(0.1)
    assert any(d == "from a worker thread" for _, d, _ in session.sent)
    await state.stop()


@pytest.mark.asyncio
async def test_dead_session_is_dropped_not_fatal():
    state = make_state()
    dead = FakeSession(fail=True)
    state.register_session(dead)
    state.emit("info", "hello")
    await asyncio.sleep(0.05)
    assert id(dead) not in state._sessions
    state.emit("info", "still fine")  # must not raise


@pytest.mark.asyncio
async def test_progress_is_announced_in_steps_and_visible_in_status():
    state = make_state()
    state._mark(ENDPOINT_INDEX, ComponentState.INITIALIZING, "indexing")
    for done in range(0, 11):
        state.set_progress(ENDPOINT_INDEX, done, 10, "embedding endpoints")
    announced = [e for e in state.events if e.progress is not None]
    assert [round(e.progress, 1) for e in announced] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    state.set_progress(ENDPOINT_INDEX, 3, 10, "embedding endpoints")
    assert state.snapshot()["components"][ENDPOINT_INDEX]["progress"] == 0.3


@pytest.mark.asyncio
async def test_status_report_lists_recent_events_and_startup_log_has_everything():
    state = make_state()
    state.mark_serving("stdio")
    state.start()
    await wait_for(lambda: state.snapshot()["ready"])
    report = await state.status_report()
    assert report["recent_events"] and "message" in report["recent_events"][-1]
    log = state.startup_log()
    assert any("serving over stdio" in e["message"] for e in log)
    assert log == sorted(log, key=lambda e: e["seconds_since_start"])
    await state.stop()


# --------------------------------------------------------------------------- waiting
@pytest.mark.asyncio
async def test_ensure_ready_waits_and_reports_monotonic_progress():
    state = make_state(FakeSearcher(delay=1.6))
    state.start()
    ctx = FakeCtx()
    result = await state.ensure_ready(ENDPOINT_INDEX, ctx, timeout=10)
    assert result is None
    assert len(ctx.calls) >= 2
    progress_values = [c[0] for c in ctx.calls]
    assert progress_values == sorted(progress_values)
    assert len(set(progress_values)) == len(progress_values)  # strictly increasing
    assert "Waiting for endpoint_index" in ctx.calls[0][2]
    await state.stop()


@pytest.mark.asyncio
async def test_ensure_ready_times_out_with_not_ready_json():
    state = make_state(FakeSearcher(delay=3))
    state.start()
    began = time.monotonic()
    result = await state.ensure_ready(ENDPOINT_INDEX, FakeCtx(), timeout=0.6)
    assert 0.5 <= time.monotonic() - began < 2
    body = json.loads(result)
    assert body["status"] == "not_ready" and body["component"] == ENDPOINT_INDEX
    assert "nautobot_server_status" in body["message"]
    await state.stop()


@pytest.mark.asyncio
async def test_ensure_ready_does_not_wait_for_failed_or_disabled_components():
    state = make_state(enable_kb=False)
    began = time.monotonic()
    body = json.loads(await state.ensure_ready(KB_INDEX, None, timeout=5))
    assert body["status"] == "disabled"
    state._mark(ENDPOINT_INDEX, ComponentState.FAILED, "boom")
    body = json.loads(await state.ensure_ready(ENDPOINT_INDEX, None, timeout=5))
    assert "failed to initialize" in body["message"]
    assert time.monotonic() - began < 1


@pytest.mark.asyncio
async def test_ensure_ready_tolerates_ctx_without_message_support():
    state = make_state(FakeSearcher(delay=1.2))
    state.start()
    ctx = FakeCtx(supports_message=False)
    assert await state.ensure_ready(ENDPOINT_INDEX, ctx, timeout=10) is None
    assert ctx.calls and ctx.calls[0][2] is None
    await state.stop()


@pytest.mark.asyncio
async def test_download_progress_is_estimated_from_archive_size(monkeypatch):
    from nautobot_mcp.helpers import startup
    from nautobot_mcp.utils.path import get_model_dir

    archive = Path(get_model_dir()) / "all-MiniLM-L6-v2" / "onnx.tar.gz"
    archive.parent.mkdir(parents=True)
    with open(archive, "wb") as f:
        f.truncate(startup._MODEL_ARCHIVE_BYTES // 2)  # sparse: half "downloaded"
    state = make_state()
    state._mark(ENDPOINT_INDEX, ComponentState.INITIALIZING, "loading embedding model")
    poller = asyncio.create_task(state._poll_model_download(ENDPOINT_INDEX))
    await asyncio.sleep(1.3)
    poller.cancel()
    await asyncio.gather(poller, return_exceptions=True)
    status = state.status[ENDPOINT_INDEX]
    assert status.progress == pytest.approx(0.5, abs=0.02)
    assert status.progress_detail == "downloading embedding model"


# --------------------------------------------------------------------------- MCP surface
@pytest.mark.asyncio
async def test_advertises_logging_resources_prompts_and_instructions():
    state = make_state()
    async with Client(build_app(state)) as client:
        init = client.initialize_result
        assert init.capabilities.logging is not None
        assert init.capabilities.resources is not None
        assert init.capabilities.prompts is not None
        assert "nautobot_server_status" in init.instructions
        assert "nautobot://server/status" in init.instructions
        resources = {str(r.uri) for r in await client.list_resources()}
        assert {
            "nautobot://server/status",
            "nautobot://server/startup-log",
            "nautobot://kb/repositories",
        } <= resources
        prompts = {p.name for p in await client.list_prompts()}
        assert prompts == {
            "query_nautobot",
            "diagnose_nautobot_mcp",
            "nautobot_job_example",
            "nautobot_inventory_report",
            "nautobot_ip_lookup",
            "nautobot_safe_change",
        }


@pytest.mark.asyncio
async def test_kb_resources_prompts_and_tools_absent_when_kb_disabled():
    state = make_state(enable_kb=False)
    async with Client(build_app(state)) as client:
        assert (
            "nautobot_kb_semantic_search" not in client.initialize_result.instructions
        )
        assert "nautobot://kb/repositories" not in {
            str(r.uri) for r in await client.list_resources()
        }
        assert "nautobot_job_example" not in {
            p.name for p in await client.list_prompts()
        }
        assert not [t.name for t in await client.list_tools() if "kb" in t.name]


@pytest.mark.asyncio
async def test_status_and_startup_log_resources_are_readable_json():
    state = make_state()
    state.mark_serving("http")
    state.start()
    await wait_for(lambda: state.snapshot()["ready"])
    async with Client(build_app(state)) as client:
        status = json.loads(
            (await client.read_resource("nautobot://server/status"))[0].text
        )
        assert status["ready"] is True and status["boot"]["transport"] == "http"
        log = json.loads(
            (await client.read_resource("nautobot://server/startup-log"))[0].text
        )
        assert any("ready" in e["message"] for e in log)
        repos = (await client.read_resource("nautobot://kb/repositories"))[0].text
        assert json.loads(repos)  # valid JSON even with no repositories configured
    await state.stop()


@pytest.mark.asyncio
async def test_prompts_render_with_the_real_tool_names():
    state = make_state()
    async with Client(build_app(state)) as client:
        msg = await client.get_prompt("query_nautobot", {"task": "list active devices"})
        text = msg.messages[0].content.text
        assert "list active devices" in text
        assert "nautobot_openapi_api_request_schema" in text
        assert "nautobot_dynamic_api_request" in text
        diag = await client.get_prompt("diagnose_nautobot_mcp")
        assert "nautobot_server_status" in diag.messages[0].content.text


@pytest.mark.asyncio
async def test_client_receives_startup_logs_and_tool_progress_end_to_end(monkeypatch):
    logs, progress = [], []

    async def on_log(message):
        logs.append(message.data if hasattr(message, "data") else str(message))

    async def on_progress(value, total, message):
        progress.append((value, message))

    from nautobot_mcp.utils.config import config

    monkeypatch.setattr(config, "READY_WAIT", 15.0)
    state = make_state(FakeSearcher(delay=2.0))
    state.mark_serving("stdio")
    state.start()
    async with Client(
        build_app(state), log_handler=on_log, progress_handler=on_progress
    ) as client:
        await (
            client.list_tools()
        )  # first request registers the session (replays history)
        result = await client.call_tool(
            "nautobot_openapi_api_request_schema",
            {"query": "devices"},
            progress_handler=on_progress,
        )
        body = json.loads(
            (result.content if hasattr(result, "content") else result)[0].text
        )
        assert body["matching_endpoints"][0]["path"] == "/dcim/devices/"
        await asyncio.sleep(0.2)
    assert any("serving over stdio" in str(m) for m in logs), logs
    assert any("indexing Swagger schema" in str(m) for m in logs), logs
    assert progress, "tool call that waited for the index reported no progress"
    assert "Waiting for endpoint_index" in progress[0][1]
    await state.stop()


@pytest.mark.asyncio
async def test_set_logging_level_from_client_reaches_state():
    state = make_state()
    async with Client(build_app(state)) as client:
        await client.session.set_logging_level("warning")
    assert state.log_level == "warning"
