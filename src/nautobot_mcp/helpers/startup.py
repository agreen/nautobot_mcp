"""Background initialization and self-reporting for the Nautobot MCP server.

The MCP ``initialize`` handshake must be answered immediately. Everything slow (loading
the embedding model, fetching and embedding the Swagger schema, cloning and indexing the
knowledge-base repositories) therefore runs here, in worker threads, *after* the
transport is already serving.

While that happens the server reports what it is doing through every channel MCP offers:

* ``notifications/message`` log notifications to each connected client session (and a
  replay of earlier events to sessions that connect late),
* progress notifications on tool calls that wait for an index (see ``ensure_ready``),
* a readable ring buffer of events (the ``nautobot://server/startup-log`` resource),
* a full status report (``nautobot_server_status`` tool, ``nautobot://server/status``
  resource, HTTP ``/health``), and
* plain log lines on stderr.
"""

import asyncio
import json
import logging
import platform
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from nautobot_mcp import __version__
from nautobot_mcp.utils.config import config
from nautobot_mcp.utils.locks import file_lock
from nautobot_mcp.utils.path import (
    claim_state_root,
    get_chroma_db_path,
    get_model_dir,
    get_repo_cache_dir,
)

logger = logging.getLogger("nautobot_mcp")

# Wall-clock time at which this module was first imported, i.e. as early in the process as
# we can observe from Python. All "seconds since start" figures are relative to this.
PROCESS_STARTED_AT = time.time()

ENDPOINT_INDEX = "endpoint_index"
KB_INDEX = "kb_index"

# MCP logging levels, least to most severe
LOG_LEVELS = [
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
]
_LEVEL_RANK = {name: rank for rank, name in enumerate(LOG_LEVELS)}
_PY_LOG_METHOD = {
    "notice": "info",
    "critical": "critical",
    "alert": "critical",
    "emergency": "critical",
}

# Size of chroma's ONNX model archive (79.3 MiB), used to estimate download progress
_MODEL_ARCHIVE_BYTES = int(79.3 * 1024 * 1024)
_MODEL_ARCHIVE_NAME = "onnx.tar.gz"
_MODEL_DIR_NAME = "all-MiniLM-L6-v2"


def _is_store_error(exc: BaseException) -> bool:
    """True for ChromaDB / on-disk index failures, which a rebuild can fix."""
    return type(exc).__module__.split(".")[0] == "chromadb" or any(
        word in str(exc).lower() for word in ("hnsw", "segment", "sqlite", "compactor")
    )


class ComponentState(str, Enum):
    PENDING = "pending"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class StartupEvent:
    at: float
    level: str
    message: str
    component: Optional[str] = None
    progress: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "seconds_since_start": round(self.at - PROCESS_STARTED_AT, 2),
            "level": self.level,
            "message": self.message,
        }
        if self.component:
            out["component"] = self.component
        if self.progress is not None:
            out["progress"] = round(self.progress, 3)
        return out

    def text(self) -> str:
        prefix = f"[{self.component}] " if self.component else ""
        return f"{prefix}{self.message}"


@dataclass
class ComponentStatus:
    state: ComponentState = ComponentState.PENDING
    detail: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # 0..1 completion of the current phase, when it can be measured
    progress: Optional[float] = None
    progress_detail: Optional[str] = None
    # (state, detail, wall-clock time) for every transition, used to time each phase
    timeline: List[Tuple[str, Optional[str], float]] = field(default_factory=list)
    _last_progress_bucket: int = -1

    def phases(self) -> List[Dict[str, Any]]:
        """How long each initializing phase took (e.g. model load vs. indexing)."""
        out = []
        for i, (state, detail, began) in enumerate(self.timeline):
            if state != ComponentState.INITIALIZING.value:
                continue
            ended = (
                self.timeline[i + 1][2] if i + 1 < len(self.timeline) else time.time()
            )
            out.append({"phase": detail, "seconds": round(ended - began, 2)})
        return out[-20:]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state.value}
        if self.detail:
            out["detail"] = self.detail
        if self.started_at:
            end = self.finished_at or time.time()
            out["elapsed_seconds"] = round(end - self.started_at, 1)
        if self.state is ComponentState.INITIALIZING and self.progress is not None:
            out["progress"] = round(self.progress, 3)
        if self.state is ComponentState.READY and self.started_at and self.finished_at:
            out["time_to_ready_seconds"] = round(self.finished_at - self.started_at, 2)
        phases = self.phases()
        if phases:
            out["phases"] = phases
        return out


class ServerState:
    """Owns the heavy components, tracks their initialization, and reports on it."""

    def __init__(self, enable_kb: bool = True) -> None:
        self.endpoint_searcher: Any = None
        self.nautobot_kb: Any = None
        self.status: Dict[str, ComponentStatus] = {
            ENDPOINT_INDEX: ComponentStatus(),
            KB_INDEX: ComponentStatus(),
        }
        if not enable_kb:
            self.status[KB_INDEX] = ComponentStatus(
                state=ComponentState.DISABLED,
                detail="disabled via NAUTOBOT_MCP_ENABLE_KB=false",
            )
        self._tasks: list[asyncio.Task] = []
        self.transport: Optional[str] = None
        self.serving_at: Optional[float] = None  # transport began accepting requests
        self.ready_at: Optional[float] = None  # every component ready (or disabled)
        # Both workers build an embedding model, and the first build downloads it into
        # the data dir; doing that from two threads at once races on the same files.
        # Serialize model loading only; the slow indexing phases still run in parallel.
        self._model_load_lock = threading.Lock()

        # Event stream
        self.events: Deque[StartupEvent] = deque(maxlen=200)
        self.log_level: str = "info"  # minimum level sent to clients (logging/setLevel)
        self._sessions: Dict[int, Any] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Schedule background initialization. Returns immediately. Idempotent."""
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        claimed = claim_state_root()
        if not claimed["shared"]:
            self.emit(
                "warning",
                "another nautobot-mcp instance is already using this data directory; "
                f"using a private index in {claimed['root']} (first start is slower)",
            )
        self.emit(
            "info",
            "server is up; building search indexes in the background"
            if self.status[KB_INDEX].state is not ComponentState.DISABLED
            else "server is up; building the API endpoint index in the background",
        )
        self._tasks = [
            asyncio.create_task(self._init_endpoint_index(), name="init-endpoints")
        ]
        if self.status[KB_INDEX].state is not ComponentState.DISABLED:
            self._tasks.append(
                asyncio.create_task(self._init_kb_index(), name="init-kb")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------ events
    def emit(
        self,
        level: str,
        message: str,
        component: Optional[str] = None,
        progress: Optional[float] = None,
    ) -> None:
        """Record an event, log it to stderr, and push it to connected clients.

        Safe to call from worker threads.
        """
        event = StartupEvent(time.time(), level, message, component, progress)
        getattr(logger, _PY_LOG_METHOD.get(level, level))(f"[startup] {event.text()}")
        if (
            self._loop is not None
            and threading.get_ident() != self._loop_thread
            and not self._loop.is_closed()
        ):
            self._loop.call_soon_threadsafe(self._record, event)
        else:
            self._record(event)

    def _record(self, event: StartupEvent) -> None:
        self.events.append(event)
        for session in list(self._sessions.values()):
            self._schedule_send(session, event)

    def _schedule_send(self, session: Any, event: StartupEvent) -> None:
        if _LEVEL_RANK.get(event.level, 1) < _LEVEL_RANK.get(self.log_level, 1):
            return
        try:
            asyncio.get_running_loop().create_task(self._send(session, event))
        except RuntimeError:  # no running loop (e.g. emitted before the server started)
            pass

    async def _send(self, session: Any, event: StartupEvent) -> None:
        try:
            await session.send_log_message(
                level=event.level,
                data=event.text(),
                logger=f"nautobot_mcp.{event.component}"
                if event.component
                else "nautobot_mcp",
            )
        except Exception:  # noqa: BLE001 - client went away; stop sending to it
            self._sessions.pop(id(session), None)

    def register_session(self, session: Any, replay: int = 30) -> None:
        """Start sending log notifications to ``session``; replay recent events once."""
        if id(session) in self._sessions:
            return
        self._sessions[id(session)] = session
        for event in list(self.events)[-replay:]:
            self._schedule_send(session, event)

    def set_log_level(self, level: str) -> None:
        if level in _LEVEL_RANK:
            self.log_level = level

    def set_progress(
        self, component: str, done: float, total: float, detail: str
    ) -> None:
        """Record measurable progress of the current phase (callable from any thread)."""
        status = self.status[component]
        fraction = 0.0 if not total else max(0.0, min(1.0, done / total))
        status.progress = fraction
        status.progress_detail = detail
        bucket = int(fraction * 5)  # announce at 0%, 20%, 40%, 60%, 80%, 100%
        if bucket != status._last_progress_bucket:
            status._last_progress_bucket = bucket
            self.emit(
                "info",
                f"{detail}: {int(fraction * 100)}% ({int(done)}/{int(total)})",
                component,
                progress=fraction,
            )

    # ------------------------------------------------------------------ status
    def is_ready(self, component: str) -> bool:
        return self.status[component].state is ComponentState.READY

    def snapshot(self) -> Dict[str, Any]:
        ready = all(
            s.state in (ComponentState.READY, ComponentState.DISABLED)
            for s in self.status.values()
        )
        return {
            "ready": ready,
            "components": {name: s.to_dict() for name, s in self.status.items()},
        }

    def mark_serving(self, transport: str) -> None:
        """Record that the transport is now accepting requests (the handshake can be answered)."""
        self.transport = transport
        self.serving_at = time.time()
        self.emit(
            "info",
            f"serving over {transport} "
            f"{self.serving_at - PROCESS_STARTED_AT:.2f}s after process start",
        )

    def boot_info(self) -> Dict[str, Any]:
        """Cheap timing summary (safe to call from frequent health checks)."""
        now = time.time()
        return {
            "version": __version__,
            "transport": self.transport,
            "started_at": datetime.fromtimestamp(
                PROCESS_STARTED_AT, timezone.utc
            ).isoformat(timespec="seconds"),
            "uptime_seconds": round(now - PROCESS_STARTED_AT, 1),
            "serving_after_seconds": (
                round(self.serving_at - PROCESS_STARTED_AT, 2)
                if self.serving_at
                else None
            ),
            "all_ready_after_seconds": (
                round(self.ready_at - PROCESS_STARTED_AT, 2) if self.ready_at else None
            ),
        }

    async def status_report(self) -> Dict[str, Any]:
        """Full self-report: identity, boot/warm-up timings, per-phase timings, index sizes."""
        report: Dict[str, Any] = {
            **self.snapshot(),
            "boot": self.boot_info(),
            "server": {
                "name": config.SERVER_NAME,
                "python": platform.python_version(),
                "nautobot_url": config.NAUTOBOT_URL,
                "token_configured": bool(config.NAUTOBOT_TOKEN),
                "knowledge_base_enabled": config.ENABLE_KB,
            },
            "recent_events": [e.to_dict() for e in list(self.events)[-10:]],
        }
        sizes: Dict[str, Any] = {}
        try:
            if self.endpoint_searcher is not None:
                sizes["endpoints_indexed"] = await asyncio.to_thread(
                    self.endpoint_searcher.collection.count
                )
            if self.nautobot_kb is not None and self.is_ready(KB_INDEX):
                stats = await asyncio.to_thread(self.nautobot_kb.get_repository_stats)
                sizes["kb_documents"] = sum(
                    s.get("document_count", 0) for s in stats.values()
                )
                sizes["kb_repositories"] = len(stats)
        except Exception as e:  # noqa: BLE001 - a status call must never fail
            sizes["error"] = str(e)
        if sizes:
            report["index"] = sizes
        return report

    def startup_log(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    # ------------------------------------------------------------------ gating
    async def ensure_ready(
        self, component: str, ctx: Any = None, timeout: Optional[float] = None
    ) -> Optional[str]:
        """Return ``None`` when ``component`` is usable, else a JSON status message.

        If the component is still being built, wait up to ``timeout`` seconds (default
        ``NAUTOBOT_MCP_READY_WAIT``) first, reporting MCP progress to ``ctx`` so the
        client can show what the server is doing. A failed component is not waited for.
        """
        if self.is_ready(component):
            return None
        timeout = config.READY_WAIT if timeout is None else timeout
        status = self.status[component]
        began = time.monotonic()
        last_report = 0.0
        while (
            status.state in (ComponentState.PENDING, ComponentState.INITIALIZING)
            and time.monotonic() - began < timeout
        ):
            if time.monotonic() - last_report >= 1.0:
                last_report = time.monotonic()
                await self._report(ctx, component, time.monotonic() - began)
            await asyncio.sleep(0.25)
        return self.not_ready_response(component)

    async def _report(self, ctx: Any, component: str, elapsed: float) -> None:
        if ctx is None:
            return
        status = self.status[component]
        what = status.progress_detail or status.detail or "starting"
        pct = f" ({int(status.progress * 100)}%)" if status.progress is not None else ""
        message = f"Waiting for {component}: {what}{pct}"
        try:
            # elapsed seconds are monotonic, which MCP requires of progress values
            await ctx.report_progress(progress=round(elapsed, 1), message=message)
        except TypeError:  # older fastmcp without ``message``
            try:
                await ctx.report_progress(progress=round(elapsed, 1))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - progress is best effort
            pass

    def not_ready_response(self, component: str) -> Optional[str]:
        """JSON for a tool call that arrives before ``component`` is usable, else None."""
        if self.is_ready(component):
            return None
        status = self.status[component]
        if status.state is ComponentState.DISABLED:
            return json.dumps(
                {
                    "status": "disabled",
                    "component": component,
                    "message": f"The {component} is {status.detail}.",
                },
                indent=2,
            )
        if status.state is ComponentState.FAILED:
            message = (
                f"The {component} failed to initialize: {status.detail}. "
                "It will be retried automatically; check server logs."
            )
        else:
            what = status.progress_detail or status.detail or "starting"
            pct = (
                f", {int(status.progress * 100)}%"
                if status.progress is not None
                else ""
            )
            message = (
                f"The {component} is still being built in the background "
                f"({what}{pct}). First start can take several minutes; retry shortly, "
                "or call nautobot_server_status for details."
            )
        return json.dumps(
            {
                "status": "not_ready",
                "component": component,
                "message": message,
                "server": self.snapshot(),
            },
            indent=2,
        )

    # ------------------------------------------------------------------ workers
    def _mark(
        self, component: str, state: ComponentState, detail: Optional[str] = None
    ) -> None:
        status = self.status[component]
        now = time.time()
        status.state = state
        status.detail = detail
        status.progress = None
        status.progress_detail = None
        status._last_progress_bucket = -1
        status.timeline.append((state.value, detail, now))
        if state is ComponentState.INITIALIZING and status.started_at is None:
            status.started_at = now
        if state in (ComponentState.READY, ComponentState.FAILED):
            status.finished_at = now

        text = state.value + (f": {detail}" if detail else "")
        if state is ComponentState.READY:
            took = f" in {now - status.started_at:.1f}s" if status.started_at else ""
            text = f"ready{took}" + (f" ({detail})" if detail else "")
        elif state is ComponentState.INITIALIZING and detail:
            text = detail
        self.emit(
            "error" if state is ComponentState.FAILED else "info", text, component
        )

        if self.ready_at is None and self.snapshot()["ready"]:
            self.ready_at = now
            self.emit(
                "notice",
                f"all components ready {now - PROCESS_STARTED_AT:.1f}s after process start ("
                + ", ".join(
                    f"{name}: "
                    + (
                        "disabled"
                        if s.state is ComponentState.DISABLED
                        else ", ".join(
                            f"{p['phase']} {p['seconds']}s" for p in s.phases()
                        )
                    )
                    for name, s in self.status.items()
                )
                + ")",
            )

    def _build_endpoint_searcher(self) -> Any:
        # Imported lazily: pulls in chromadb / onnxruntime.
        from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

        with self._model_load_lock, file_lock(Path(get_model_dir()) / ".lock"):
            return EndpointSearcherChroma()

    def _build_kb(self) -> Any:
        from nautobot_mcp.helpers.nb_kb_v2 import EnhancedNautobotKnowledge

        with self._model_load_lock, file_lock(Path(get_model_dir()) / ".lock"):
            return EnhancedNautobotKnowledge()

    async def _poll_model_download(self, component: str) -> None:
        """Estimate embedding-model download progress from the growing archive file."""
        from nautobot_mcp.utils.path import get_model_dir

        archive = Path(get_model_dir()) / _MODEL_DIR_NAME / _MODEL_ARCHIVE_NAME
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    size = archive.stat().st_size
                except OSError:
                    continue
                if size and self.status[component].detail == "loading embedding model":
                    self.set_progress(
                        component,
                        min(size, _MODEL_ARCHIVE_BYTES * 0.99),
                        _MODEL_ARCHIVE_BYTES,
                        "downloading embedding model",
                    )
        except asyncio.CancelledError:
            pass

    async def _load_model_phase(self, component: str, build) -> Any:
        """Run ``build`` in a thread while polling the model download for progress."""
        poller = asyncio.create_task(self._poll_model_download(component))
        try:
            return await asyncio.to_thread(build)
        finally:
            poller.cancel()

    async def _recover_endpoint_store(self) -> None:
        """Discard the (rebuildable) endpoint index after a ChromaDB failure."""
        searcher = self.endpoint_searcher
        if searcher is not None:
            try:
                await asyncio.to_thread(searcher.reset)
                return
            except Exception:  # noqa: BLE001 - fall through to a hard wipe
                self.endpoint_searcher = None
        await asyncio.to_thread(
            shutil.rmtree,
            Path(get_chroma_db_path()) / "nautobot_api",
            True,
        )

    async def _init_endpoint_index(self) -> None:
        """Build the endpoint index; never give up.

        Retries with backoff while Nautobot is unreachable (as a sidecar this server often
        starts alongside Nautobot, whose Swagger endpoint may not answer for minutes), and
        rebuilds the on-disk index from scratch if ChromaDB reports it as unusable.
        """
        component = ENDPOINT_INDEX
        delay = 5.0
        while True:
            try:
                if self.endpoint_searcher is None:
                    self._mark(
                        component,
                        ComponentState.INITIALIZING,
                        "loading embedding model",
                    )
                    self.endpoint_searcher = await self._load_model_phase(
                        component, self._build_endpoint_searcher
                    )
                self._mark(
                    component, ComponentState.INITIALIZING, "indexing Swagger schema"
                )

                def on_progress(done: int, total: int) -> None:
                    self.set_progress(component, done, total, "embedding endpoints")

                ok = await asyncio.to_thread(
                    self.endpoint_searcher.initialize_collection, False, on_progress
                )
                if ok:
                    self._mark(component, ComponentState.READY)
                    return
                error = getattr(self.endpoint_searcher, "last_error", None)
                if error is not None and _is_store_error(error):
                    raise error
                if await asyncio.to_thread(self.endpoint_searcher.collection.count) > 0:
                    self._mark(
                        component,
                        ComponentState.READY,
                        "Nautobot schema unreachable; serving previously indexed endpoints",
                    )
                    return
                problem = f"could not fetch {config.GLOBAL_TOOL_PROMPT}; retrying in {delay:.0f}s"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - surfaced through status, never fatal
                logger.exception("Endpoint index initialization failed")
                if _is_store_error(e):
                    problem = (
                        f"index store error ({type(e).__name__}: {e}); "
                        f"rebuilding it from scratch, retrying in {delay:.0f}s"
                    )
                    try:
                        await self._recover_endpoint_store()
                    except Exception:  # noqa: BLE001
                        logger.exception("Could not reset the endpoint index")
                else:
                    problem = f"{type(e).__name__}: {e}; retrying in {delay:.0f}s"
            self._mark(component, ComponentState.FAILED, problem)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _recover_kb_store(self) -> None:
        """Discard the (rebuildable) knowledge-base index and force re-indexing."""
        self.nautobot_kb = None
        await asyncio.to_thread(
            shutil.rmtree, Path(get_chroma_db_path()) / "nautobot_kb_v2", True
        )
        # Without this the git cache would claim the repos are already indexed.
        metadata = Path(get_repo_cache_dir()) / "repo_metadata.json"
        await asyncio.to_thread(metadata.unlink, True)

    async def _init_kb_index(self) -> None:
        component = KB_INDEX
        delay = 5.0
        for attempt in range(1, 6):
            try:
                if self.nautobot_kb is None:
                    self._mark(
                        component,
                        ComponentState.INITIALIZING,
                        "loading embedding model",
                    )
                    self.nautobot_kb = await self._load_model_phase(
                        component, self._build_kb
                    )
                self._mark(
                    component,
                    ComponentState.INITIALIZING,
                    "cloning/indexing repositories",
                )

                def on_progress(done: int, total: int, repo: str = "") -> None:
                    self.set_progress(
                        component,
                        done,
                        total,
                        f"repositories ({repo})" if repo else "repositories",
                    )

                await asyncio.to_thread(
                    self.nautobot_kb.initialize_all_repositories, False, on_progress
                )
                self._mark(component, ComponentState.READY)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("Knowledge base initialization failed")
                giving_up = attempt == 5
                self._mark(
                    component,
                    ComponentState.FAILED,
                    f"{type(e).__name__}: {e}"
                    + ("" if giving_up else f"; retrying in {delay:.0f}s"),
                )
                if giving_up:
                    return
                if _is_store_error(e):
                    try:
                        await self._recover_kb_store()
                    except Exception:  # noqa: BLE001
                        logger.exception("Could not reset the knowledge base index")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
