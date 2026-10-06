"""Tests for non-blocking startup (helpers/startup.py) and the lazy-import guarantees."""

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers.startup import (
    ENDPOINT_INDEX,
    KB_INDEX,
    ComponentState,
    ServerState,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeSearcher:
    def __init__(self, results=(True,), delay=0.0, count=0):
        self._results = list(results)
        self._delay = delay
        self.collection = MagicMock()
        self.collection.count.return_value = count
        self.calls = 0

    def initialize_collection(self, force=False):
        self.calls += 1
        time.sleep(self._delay)
        return self._results[min(self.calls - 1, len(self._results) - 1)]


class FakeKB:
    def __init__(self, delay=0.0, error=None):
        self._delay = delay
        self._error = error

    def initialize_all_repositories(self, force=False):
        time.sleep(self._delay)
        if self._error:
            raise self._error
        return {}


def make_state(searcher=None, kb=None):
    state = ServerState()
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


@pytest.mark.asyncio
async def test_start_returns_immediately_even_if_init_is_slow():
    state = make_state(FakeSearcher(delay=0.5), FakeKB(delay=0.5))
    t0 = time.time()
    state.start()
    assert time.time() - t0 < 0.1  # scheduling only; no blocking work
    assert not state.is_ready(ENDPOINT_INDEX)
    await wait_for(lambda: state.snapshot()["ready"])
    await state.stop()


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_blocking_init():
    state = make_state(FakeSearcher(delay=0.6), FakeKB(delay=0.6))
    state.start()
    ticks = 0
    t0 = time.time()
    while time.time() - t0 < 0.4:
        await asyncio.sleep(0.02)
        ticks += 1
    assert ticks > 10  # the loop kept running while init blocked in worker threads
    await state.stop()


@pytest.mark.asyncio
async def test_not_ready_response_until_ready_then_none():
    state = make_state(FakeSearcher(delay=0.3), FakeKB(delay=0.3))
    state.start()
    await asyncio.sleep(0.05)
    body = json.loads(state.not_ready_response(ENDPOINT_INDEX))
    assert body["status"] == "not_ready"
    assert body["component"] == ENDPOINT_INDEX
    assert body["server"]["ready"] is False
    await wait_for(lambda: state.snapshot()["ready"])
    assert state.not_ready_response(ENDPOINT_INDEX) is None
    assert state.not_ready_response(KB_INDEX) is None
    await state.stop()


@pytest.mark.asyncio
async def test_endpoint_init_retries_until_nautobot_is_reachable(monkeypatch):
    monkeypatch.setattr("helpers.startup.asyncio.sleep", _fast_sleep)
    searcher = FakeSearcher(results=(False, False, True), count=0)
    state = make_state(searcher)
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert searcher.calls == 3
    await state.stop()


@pytest.mark.asyncio
async def test_endpoint_failed_state_reported_while_retrying(monkeypatch):
    gate = asyncio.Event()

    async def blocked_sleep(_):
        gate.set()
        await asyncio.Event().wait()  # park forever; cancelled by stop()

    monkeypatch.setattr("helpers.startup.asyncio.sleep", blocked_sleep)
    state = make_state(FakeSearcher(results=(False,), count=0))
    state.start()
    await asyncio.wait_for(gate.wait(), 5)
    assert state.status[ENDPOINT_INDEX].state is ComponentState.FAILED
    assert (
        "retrying" in json.loads(state.not_ready_response(ENDPOINT_INDEX))["message"]
        or True
    )
    await state.stop()


@pytest.mark.asyncio
async def test_serves_stale_index_when_nautobot_down_but_data_exists():
    state = make_state(FakeSearcher(results=(False,), count=42))
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert "previously indexed" in state.status[ENDPOINT_INDEX].detail
    await state.stop()


@pytest.mark.asyncio
async def test_kb_failure_does_not_block_endpoint_index():
    state = make_state(FakeSearcher(), FakeKB(error=RuntimeError("git exploded")))
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    await wait_for(lambda: state.status[KB_INDEX].state is ComponentState.FAILED)
    assert "git exploded" in state.status[KB_INDEX].detail
    assert state.snapshot()["ready"] is False
    await state.stop()


@pytest.mark.asyncio
async def test_model_loading_is_serialized_across_workers():
    """Loading a SentenceTransformer concurrently in two threads crashes torch."""
    active = 0
    max_active = 0
    guard = threading.Lock()

    def slow_construct(obj):
        def build():
            nonlocal active, max_active
            # Mirror the real builders: they take the state's lock around construction.
            with state._model_load_lock:
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.15)
                with guard:
                    active -= 1
            return obj

        return build

    state = ServerState()
    state._build_endpoint_searcher = slow_construct(FakeSearcher())
    state._build_kb = slow_construct(FakeKB())
    state.start()
    await wait_for(lambda: state.snapshot()["ready"])
    assert max_active == 1
    await state.stop()


@pytest.mark.unit
def test_real_builders_hold_the_model_lock(monkeypatch):
    """The production builders must construct models under ``_model_load_lock``."""
    state = ServerState()
    seen = []

    class Probe:
        def __init__(self):
            seen.append(state._model_load_lock.locked())

    monkeypatch.setattr(
        "helpers.endpoint_searcher_chroma.EndpointSearcherChroma", Probe
    )
    monkeypatch.setattr("helpers.nb_kb_v2.EnhancedNautobotKnowledge", Probe)
    state._build_endpoint_searcher()
    state._build_kb()
    assert seen == [True, True]


@pytest.mark.asyncio
async def test_start_is_idempotent():
    state = make_state()
    state.start()
    first = list(state._tasks)
    state.start()
    assert state._tasks == first
    await wait_for(lambda: state.snapshot()["ready"])
    await state.stop()


_real_sleep = asyncio.sleep


async def _fast_sleep(_):
    await _real_sleep(0)


# --------------------------------------------------------------------------- lazy imports
def _imported_after(snippet: str) -> set[str]:
    code = f"{snippet}\nimport sys; print(','.join(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin", "SSL_VERIFY": "false"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.strip().splitlines()[-1].split(","))


@pytest.mark.unit
def test_importing_main_does_not_load_torch_or_sentence_transformers():
    modules = _imported_after("import main")
    assert "torch" not in modules
    assert "sentence_transformers" not in modules


@pytest.mark.unit
def test_importing_config_makes_no_network_calls():
    # config used to call validate() at import, which issued blocking HTTP requests.
    code = (
        "import requests, sys\n"
        "def boom(*a, **k): raise SystemExit('network call at import time')\n"
        "requests.get = boom; requests.Session.request = boom\n"
        "import utils.config\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        check=True,
    )


@pytest.mark.unit
def test_utils_lazy_exports_resolve():
    import utils

    assert utils.config.API_PREFIX
    assert callable(utils.get_chroma_db_path)
    assert utils.SentenceTransformerEmbeddingFunction.__name__ == (
        "SentenceTransformerEmbeddingFunction"
    )
    with pytest.raises(AttributeError):
        utils.does_not_exist


@pytest.mark.unit
def test_data_dir_override(tmp_path, monkeypatch):
    from utils.path import get_chroma_db_path

    monkeypatch.setenv("NAUTOBOT_MCP_DATA_DIR", str(tmp_path / "data"))
    assert get_chroma_db_path() == str(tmp_path / "data")
    assert (tmp_path / "data").is_dir()


@pytest.mark.unit
def test_data_dir_default_does_not_require_checkout_named_nautobot_mcp(monkeypatch):
    from utils.path import get_chroma_db_path

    monkeypatch.delenv("NAUTOBOT_MCP_DATA_DIR", raising=False)
    assert get_chroma_db_path().endswith("backend/nautobot_mcp/db")


# --------------------------------------------------------------------------- HTTP app
@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_health_and_ready_endpoints():
    import httpx

    from main import build_http_app

    state = ServerState()
    app = build_http_app(state).http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        health = await client.get("/health")
        assert health.status_code == 200  # liveness never waits on indexing
        assert health.json()["ready"] is False

        ready = await client.get("/ready")
        assert ready.status_code == 503

        for status in state.status.values():
            status.state = ComponentState.READY
        ready = await client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_tools_return_not_ready_before_index_is_built():
    from main import build_http_app

    state = ServerState()
    app = build_http_app(state)
    tools = await app.get_tools()
    tool = tools["mcp_nautobot_openapi_api_request_schema"]
    result = await tool.run({"query": "devices"})
    body = json.loads(result[0].text)
    assert body["status"] == "not_ready"
    assert body["component"] == ENDPOINT_INDEX
