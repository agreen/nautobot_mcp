"""A broken or contended index must never leave the server permanently failed."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chromadb.errors import InternalError

from nautobot_mcp.helpers import startup
from nautobot_mcp.helpers.startup import (
    ENDPOINT_INDEX,
    KB_INDEX,
    ComponentState,
    ServerState,
    _is_store_error,
)
from nautobot_mcp.utils import path as path_utils
from nautobot_mcp.utils.locks import file_lock, try_lock

HNSW_ERROR = InternalError(
    "Error executing plan: Error sending backfill request to compactor: "
    "Failed to apply logs to the hnsw segment writer"
)
_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    async def quick(delay):
        await _real_sleep(0.01 if delay >= 1 else delay)

    monkeypatch.setattr(startup.asyncio, "sleep", quick)


async def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await _real_sleep(0.02)
    raise AssertionError("condition not met in time")


class Searcher:
    """Scriptable stand-in: ``script`` lists what each initialize_collection call does."""

    def __init__(self, script, count_error=None, count=0):
        self.script = list(script)
        self.calls = 0
        self.resets = 0
        self.last_error = None
        self.collection = MagicMock()
        if count_error:
            self.collection.count.side_effect = count_error
        else:
            self.collection.count.return_value = count

    def initialize_collection(self, force=False, progress_cb=None):
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            self.last_error = outcome
            return False
        self.last_error = None
        return outcome

    def reset(self):
        self.resets += 1
        self.collection = MagicMock()
        self.collection.count.return_value = 0


def make_state(searcher=None, kb=None, enable_kb=False):
    state = ServerState(enable_kb=enable_kb)
    state._build_endpoint_searcher = lambda: searcher
    state._build_kb = lambda: kb
    return state


# --------------------------------------------------------------------------- classification
def test_store_errors_are_recognized():
    assert _is_store_error(HNSW_ERROR)
    assert _is_store_error(RuntimeError("table collections already exists in sqlite"))
    assert not _is_store_error(ValueError("bad url"))


# --------------------------------------------------------------------------- endpoint index
@pytest.mark.asyncio
async def test_store_error_during_indexing_resets_index_and_recovers():
    searcher = Searcher([HNSW_ERROR, True])
    state = make_state(searcher)
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert searcher.resets == 1 and searcher.calls == 2
    assert any("rebuilding it from scratch" in e.message for e in state.events)
    await state.stop()


@pytest.mark.asyncio
async def test_count_raising_store_error_no_longer_crashes_the_worker():
    """The exact failure seen in the wild: initialize returned False, then count() raised."""

    class Reporter(Searcher):
        def reset(self):
            super().reset()

    searcher = Reporter([False, True], count_error=HNSW_ERROR)
    state = make_state(searcher)
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert searcher.resets == 1
    assert not any(
        "initialization crashed" in e.message for e in state.events
    )  # old behaviour: permanent FAILED
    await state.stop()


@pytest.mark.asyncio
async def test_store_error_when_opening_wipes_the_directory_then_retries():
    stale = Path(path_utils.get_chroma_db_path()) / "nautobot_api" / "db" / "junk.bin"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"corrupt")

    attempts = []

    def build():
        attempts.append(stale.exists())
        if len(attempts) == 1:
            raise HNSW_ERROR
        return Searcher([True])

    state = ServerState(enable_kb=False)
    state._build_endpoint_searcher = build
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert attempts == [
        True,
        False,
    ]  # junk present on 1st attempt, wiped before the 2nd
    await state.stop()


@pytest.mark.asyncio
async def test_unexpected_errors_are_retried_not_fatal():
    searcher = Searcher([RuntimeError("something odd"), True])
    searcher.initialize_collection = MagicMock(
        side_effect=[RuntimeError("something odd"), True]
    )
    state = make_state(searcher)
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert searcher.resets == 0  # not a store error: no destructive reset
    await state.stop()


@pytest.mark.asyncio
async def test_unreachable_nautobot_retries_then_serves_stale_index_if_any():
    searcher = Searcher([False], count=42)
    state = make_state(searcher)
    state.start()
    await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
    assert "previously indexed" in state.status[ENDPOINT_INDEX].detail
    assert searcher.resets == 0
    await state.stop()


# --------------------------------------------------------------------------- knowledge base
@pytest.mark.asyncio
async def test_kb_constructor_failure_is_retried_and_store_is_reset(tmp_path):
    kb_dir = Path(path_utils.get_chroma_db_path()) / "nautobot_kb_v2"
    kb_dir.mkdir(parents=True)
    metadata = Path(path_utils.get_repo_cache_dir()) / "repo_metadata.json"
    metadata.write_text("{}")

    good = MagicMock()
    good.initialize_all_repositories = MagicMock(return_value={})
    builds = []

    def build():
        builds.append(1)
        if len(builds) < 3:
            raise HNSW_ERROR
        return good

    state = ServerState(enable_kb=True)
    state._build_endpoint_searcher = lambda: Searcher([True])
    state._build_kb = build
    state.start()
    await wait_for(lambda: state.is_ready(KB_INDEX))
    assert len(builds) == 3
    assert not kb_dir.exists() and not metadata.exists()  # forced a full re-index
    await state.stop()


@pytest.mark.asyncio
async def test_kb_gives_up_after_repeated_failures_but_reports_why():
    state = ServerState(enable_kb=True)
    state._build_endpoint_searcher = lambda: Searcher([True])

    def build():
        raise RuntimeError("git is not installed")

    state._build_kb = build
    state.start()
    await wait_for(lambda: state.status[KB_INDEX].state is ComponentState.FAILED)
    await wait_for(lambda: state._tasks[1].done())
    assert "git is not installed" in state.status[KB_INDEX].detail
    body = json.loads(state.not_ready_response(KB_INDEX))
    assert "failed to initialize" in body["message"]
    await state.stop()


# --------------------------------------------------------------------------- real chroma store
def test_endpoint_searcher_reset_really_clears_a_real_chroma_store(tmp_path):
    from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

    class TinyEmbedding:
        def __init__(self, *a, **k):
            pass

        def __call__(self, input):
            return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in input]

        @staticmethod
        def name():
            return "tiny"

    schema = {
        "paths": {f"/api/x{i}/": {"get": {"operationId": f"x{i}"}} for i in range(5)}
    }
    response = MagicMock()
    response.json.return_value = schema
    response.content = b"schema-v1"
    with (
        patch(
            "nautobot_mcp.helpers.endpoint_searcher_chroma.LocalEmbeddingFunction",
            TinyEmbedding,
        ),
        patch(
            "nautobot_mcp.helpers.endpoint_searcher_chroma.requests.get",
            return_value=response,
        ),
    ):
        searcher = EndpointSearcherChroma()
        assert searcher.initialize_collection() is True
        assert searcher.collection.count() == 5
        searcher.reset()
        assert searcher.collection.count() == 0
        assert searcher.initialize_collection() is True  # hash file went with the wipe
        assert searcher.collection.count() == 5


# --------------------------------------------------------------------------- instances
def test_second_instance_gets_a_private_state_root_and_shared_models(tmp_path):
    data = path_utils.get_data_dir()
    holder = try_lock(data / "instance.lock")  # "another process" owns the shared dir
    assert holder is not None
    try:
        claimed = path_utils.claim_state_root()
        assert claimed["shared"] is False
        private = data / "instances" / str(os.getpid())
        assert Path(claimed["root"]) == private
        assert path_utils.get_chroma_db_path() == str(private / "chroma")
        assert path_utils.get_repo_cache_dir() == str(private / "repos")
        assert path_utils.get_model_dir() == str(data / "models")  # model stays shared
        assert path_utils.claim_state_root() == claimed  # idempotent
    finally:
        holder.close()


def test_first_instance_owns_the_shared_directory():
    data = path_utils.get_data_dir()
    claimed = path_utils.claim_state_root()
    assert claimed == {"shared": True, "root": str(data)}
    assert path_utils.get_chroma_db_path() == str(data / "chroma")
    assert try_lock(data / "instance.lock") is None  # we hold it


def test_stale_private_dirs_from_dead_processes_are_purged():
    data = path_utils.get_data_dir()
    dead, alive = data / "instances" / "999999", data / "instances" / str(os.getpid())
    for d in (dead, alive):
        d.mkdir(parents=True)
    path_utils.claim_state_root()
    assert not dead.exists()
    assert alive.exists()  # a live pid's directory is left alone


@pytest.mark.asyncio
async def test_start_warns_when_sharing_a_data_dir_with_another_instance():
    holder = try_lock(path_utils.get_data_dir() / "instance.lock")
    try:
        state = make_state(Searcher([True]))
        state.start()
        await wait_for(lambda: state.is_ready(ENDPOINT_INDEX))
        warnings = [e for e in state.events if e.level == "warning"]
        assert warnings and "private index" in warnings[0].message
        await state.stop()
    finally:
        holder.close()


def test_file_lock_serializes_and_try_lock_reports_contention(tmp_path):
    lock = tmp_path / "x.lock"
    with file_lock(lock):
        assert try_lock(lock) is None
    again = try_lock(lock)
    assert again is not None
    again.close()
