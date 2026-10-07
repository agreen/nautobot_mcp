# Startup, Health & Observability

The server answers the MCP `initialize` handshake immediately and builds its search indexes in the
background (first run: download the ~80 MB embedding model, then embed the Nautobot API schema; with
the knowledge base enabled it also clones and indexes GitHub repositories). This page describes how
the server tells you what it is doing.

## What a client sees

| Channel | What it carries | Notes |
|---|---|---|
| **`initialize` result → `instructions`** | How to use the tools, that indexes warm up in the background, and where to look for status | Static text; returned to every client |
| **Log notifications** (`notifications/message`) | Startup phases and progress: *serving*, *loading embedding model*, *downloading embedding model: 56%*, *embedding endpoints: 45% (1000/2200)*, *ready in 64.3s*, retries and failures | Sent to every connected session. A client that connects late gets the recent history replayed on its first request. Honors `logging/setLevel`. |
| **Progress notifications** | Tool calls that arrive while an index is still building *wait* (up to `NAUTOBOT_MCP_READY_WAIT`, default 20 s) and report `Waiting for endpoint_index: downloading embedding model (98%)` | Only sent if the client supplied a progress token with the request. Long tools (`refresh_endpoint_index`, `nautobot_kb_init_repos`, `nautobot_kb_update_repos`) also report real progress while they run. |
| **`nautobot_server_status` tool** | Version, uptime, seconds until serving / until all indexes ready, per-phase durations, index sizes, last events | Works on both transports and before the indexes are ready |
| **Resources** | `nautobot://server/status` (same as the tool) and `nautobot://server/startup-log` (every event since start) | Read-only, attachable as context |
| **stderr** | The same events as plain log lines | Shown in the *logs* view of hosts like MetaMCP |
| **HTTP `/health`, `/ready`** | Liveness (always 200 once serving, includes boot timings) and readiness (503 until indexed) | HTTP transport only |

MCP's built-in `ping` request also works at any time and is answered by the protocol layer, independent
of the indexes.

If the index is still not ready after the wait, the tool returns:

```json
{ "status": "not_ready", "component": "endpoint_index",
  "message": "The endpoint_index is still being built in the background (embedding endpoints, 45%). ..." }
```

`nautobot_dynamic_api_request` never waits: it only needs Nautobot.

## Timings

`nautobot_server_status` (or `nautobot://server/status`) reports, for example:

```json
{
  "ready": true,
  "boot": { "version": "0.3.0", "transport": "stdio", "uptime_seconds": 69.2,
            "serving_after_seconds": 4.48, "all_ready_after_seconds": 69.0 },
  "components": {
    "endpoint_index": { "state": "ready", "time_to_ready_seconds": 64.32,
      "phases": [ { "phase": "loading embedding model", "seconds": 7.55 },
                  { "phase": "indexing Swagger schema", "seconds": 56.76 } ] },
    "kb_index": { "state": "disabled" }
  },
  "index": { "endpoints_indexed": 2200 }
}
```

`serving_after_seconds` is measured from process start to the transport accepting requests, so it is
the number to compare against a client's connect timeout. A restart that finds an unchanged Nautobot
schema skips re-embedding and is ready in seconds.

## Resources and prompts

**Resources** (read-only context): `nautobot://server/status`, `nautobot://server/startup-log`, and,
when the knowledge base is enabled, `nautobot://kb/repositories`.

**Prompts** (user-invoked workflow templates, shown as slash commands in many clients):

| Prompt | Purpose |
|---|---|
| `query_nautobot(task)` | Look something up or change something using the schema-search → API-request workflow, confirming writes first |
| `diagnose_nautobot_mcp()` | Have the assistant read the server status and explain what is slow, not ready, or failing |
| `nautobot_job_example(goal)` | Find examples with the knowledge base and write a Nautobot Job (knowledge base enabled only) |

**Not implemented, on purpose:** *roots* (the server never touches the client's files) and *sampling*
(the server never needs to ask the client's model to generate text).

## Self-healing

The search indexes are caches that can always be rebuilt, so the server never gives up on them:

- If Nautobot is unreachable at startup (common when running as a sidecar), the endpoint index retries
  with backoff (5 s doubling to 60 s) and serves a previously built index in the meantime if one exists.
- If ChromaDB reports a damaged index (for example *"Failed to apply logs to the hnsw segment writer"*),
  the server deletes that index, rebuilds it from scratch and retries, instead of staying failed.
- If several server processes share one data directory (some MCP hosts start one process per session),
  the first owns the shared indexes and cloned repositories; the others automatically use a private
  directory under `<data dir>/instances/<pid>` and say so in their log. Private directories are removed on
  exit, and leftovers of dead processes are cleaned up. The embedding model is shared between all of them.
