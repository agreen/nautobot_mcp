"""Background initialization for the Nautobot MCP server.

The MCP ``initialize`` handshake must be answered immediately. Everything slow (loading
the embedding model, fetching and embedding the Swagger schema, cloning and indexing the
knowledge-base repositories) therefore runs here, in worker threads, *after* the
transport is already serving. Tools that depend on an index report a structured
"not ready" status until it is available.
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from nautobot_mcp.utils.config import config

logger = logging.getLogger("nautobot_mcp")

ENDPOINT_INDEX = "endpoint_index"
KB_INDEX = "kb_index"


class ComponentState(str, Enum):
    PENDING = "pending"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class ComponentStatus:
    state: ComponentState = ComponentState.PENDING
    detail: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state.value}
        if self.detail:
            out["detail"] = self.detail
        if self.started_at:
            end = self.finished_at or time.time()
            out["elapsed_seconds"] = round(end - self.started_at, 1)
        return out


class ServerState:
    """Owns the heavy components and tracks their initialization progress."""

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
        # Both workers build an embedding model, and the first build downloads it into
        # the data dir; doing that from two threads at once races on the same files.
        # Serialize model loading only; the slow indexing phases still run in parallel.
        self._model_load_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Schedule background initialization. Returns immediately. Idempotent."""
        if self._tasks:
            return
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
            message = (
                f"The {component} is still being built in the background "
                "(first start can take several minutes). Retry shortly."
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
        status.state = state
        status.detail = detail
        if state is ComponentState.INITIALIZING and status.started_at is None:
            status.started_at = time.time()
        if state in (ComponentState.READY, ComponentState.FAILED):
            status.finished_at = time.time()
        logger.info(
            f"[startup] {component}: {state.value}" + (f" ({detail})" if detail else "")
        )

    def _build_endpoint_searcher(self) -> Any:
        # Imported lazily: pulls in torch / sentence-transformers / chromadb.
        from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma

        with self._model_load_lock:
            return EndpointSearcherChroma()

    def _build_kb(self) -> Any:
        from nautobot_mcp.helpers.nb_kb_v2 import EnhancedNautobotKnowledge

        with self._model_load_lock:
            return EnhancedNautobotKnowledge()

    async def _init_endpoint_index(self) -> None:
        """Build the endpoint index, retrying while Nautobot is unreachable.

        As a sidecar this server often starts alongside Nautobot itself, whose Swagger
        endpoint may not answer for a few minutes.
        """
        component = ENDPOINT_INDEX
        delay = 5.0
        try:
            self._mark(
                component, ComponentState.INITIALIZING, "loading embedding model"
            )
            self.endpoint_searcher = await asyncio.to_thread(
                self._build_endpoint_searcher
            )
            while True:
                self._mark(
                    component, ComponentState.INITIALIZING, "indexing Swagger schema"
                )
                ok = await asyncio.to_thread(
                    self.endpoint_searcher.initialize_collection
                )
                if ok:
                    self._mark(component, ComponentState.READY)
                    return
                if await asyncio.to_thread(self.endpoint_searcher.collection.count) > 0:
                    self._mark(
                        component,
                        ComponentState.READY,
                        "Nautobot schema unreachable; serving previously indexed endpoints",
                    )
                    return
                self._mark(
                    component,
                    ComponentState.FAILED,
                    f"could not fetch {config.GLOBAL_TOOL_PROMPT}; retrying in {delay:.0f}s",
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - surfaced through status, never crash the server
            logger.exception("Endpoint index initialization crashed")
            self._mark(component, ComponentState.FAILED, str(e))

    async def _init_kb_index(self) -> None:
        component = KB_INDEX
        try:
            self._mark(
                component, ComponentState.INITIALIZING, "loading embedding model"
            )
            self.nautobot_kb = await asyncio.to_thread(self._build_kb)
            self._mark(
                component, ComponentState.INITIALIZING, "cloning/indexing repositories"
            )
            await asyncio.to_thread(self.nautobot_kb.initialize_all_repositories)
            self._mark(component, ComponentState.READY)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Knowledge base initialization crashed")
            self._mark(component, ComponentState.FAILED, str(e))
