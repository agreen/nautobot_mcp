# High-Level Design

Status: **DRAFT v0.1 — for review.** Nothing in the "v1" scope is built until the open decisions
in section 12 are settled. Companion: [SDS.md](SDS.md) (per-tool and per-component specs).

## 1. Purpose

An MCP server that lets an LLM answer questions about, and (optionally) change, the data in a
Nautobot source of truth — reliably, with small answers, without surprising the operator.

### Goals
- G1. Answer common network-inventory questions in **one tool call** with a trimmed, summarised result.
- G2. **Never block the MCP handshake** and always report what it is doing (startup, health, progress).
- G3. **Safe by default**: read-only unless explicitly enabled; no network exposure without auth.
- G4. **Adapt to the connected Nautobot**: version, installed apps, and schema are discovered at runtime.
- G5. Work well behind **MetaMCP** (uvx install, stdio and streamable-HTTP) and as a Docker sidecar.
- G6. Everything testable offline against a fake Nautobot.

### Non-goals (v1)
- Running inside the Nautobot process (that is a different product; see rsp2k/nautobot-app-mcp).
- Per-end-user identity / OAuth (a single service token is used; see D6).
- Replacing Nautobot's UI, jobs, or GraphQL; we call them.
- Wrapping every endpoint 1:1 (the generic request tool covers the long tail).

## 2. Context

```
 LLM client ──► MetaMCP / MCP client ──► nautobot-mcp ──► Nautobot REST (+GraphQL)
                 (stdio or HTTP)          │  ├─ local state dir (profile cache, optional indexes)
                                          │  └─ optional: git repos (knowledge base)
```

Deployment modes: (a) `uvx` stdio child of MetaMCP; (b) Docker sidecar next to Nautobot,
streamable-HTTP on an internal network; (c) developer laptop, stdio.

## 3. Requirements

| ID | Requirement |
|----|-------------|
| F1 | Intent-level read tools for dcim and ipam (SDS §4). |
| F2 | Computed tools (utilisation, inventory summary, data-quality audit) (SDS §5). |
| F3 | Generic fallback: schema search + request tool covering every endpoint, including plugins. |
| F4 | GraphQL query + schema introspection (read-only). |
| F5 | Reference-data and guide **resources**; workflow **prompts** (SDS §7–8). |
| F6 | Toolsets: enable/disable groups and individual tools by config. |
| F7 | Plugin modules appear only when the connected Nautobot has the app installed. |
| F8 | Write tools exist only when `READ_ONLY=false`, are annotated destructive, and require confirmation text in their description. |
| N1 | `initialize` answers in < 1 s regardless of Nautobot or index state. |
| N2 | Every tool result ≤ `MAX_RESPONSE_CHARS` (default 20k), declared via `meta.truncated`. |
| N3 | Every tool failure is structured, actionable, and never an unhandled exception. |
| N4 | No secret, hostname or object data in logs or diagnostics unless opted in. |
| N5 | Works with Nautobot 2.x and 3.x (OpenAPI 3). Behaviour differences are isolated in one compat layer (D5). |

## 4. Landscape and what we take

| Source | Take | Leave |
|--------|------|-------|
| reshif/nautobot-mcp | `{summary,data,meta}` contract; budget enforcement; resolver with candidate lists; tool annotations; reference resources; prompts | single-commit maturity; no health/startup model |
| bsmeding/nautobot-mcp-server | client-level write gate; tenant scoping at one choke point; plugin-module registry | raw-passthrough wrappers; mcp 2.x pin |
| kvncampos upstream (our origin) | semantic schema/KB search idea | blocking startup |
| rsp2k app | per-user `ObjectPermission` model (future) | in-process, IdP requirement |
| ramixpe | GraphQL-backed composites (`describe_device`, topology); `schema_hints` | — |

## 5. Architecture

```
┌──────────────────────────── transport (stdio | streamable-http) ────────────────────────────┐
│ FastMCP app  ·  instructions  ·  middleware (session registry, auth, logging)               │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│ Registry: toolsets/modules → tools, resources, prompts      (filters: TOOLSETS, DISABLED_TOOLS,
│                                                              READ_ONLY, detected plugins)   │
├───────────────┬───────────────┬───────────────┬──────────────┬──────────────────────────────┤
│ core module   │ dcim module   │ ipam module   │ plugin mods  │ kb module (optional)         │
├───────────────┴───────────────┴───────────────┴──────────────┴──────────────────────────────┤
│ Services: Gateway (HTTP seam) · Resolver (name→object) · Response (contract, budget)       │
│           ReferenceCache (TTL) · Compat (OAS3/2.x/3.x differences) · WriteGate · Scope      │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│ ServerState: background workers, health, event log, progress   (already implemented)       │
│ InstanceProfile: version, apps, schema summary — cached on disk, reconciled in background  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Rules:
1. **Tools never touch httpx.** They call the Gateway (one seam: timeout, retry, concurrency, error normalisation).
2. **Tools never return raw API objects.** They project fields and go through `Response.build`.
3. **Modules are declarative**: each declares name, requirements (core, or a Nautobot app), tools, resources, prompts.
4. **Startup is lazy and layered**: server is ready at once; `InstanceProfile` loads from disk cache, then reconciles with the live instance in the background and emits `list_changed` if modules change.

## 6. Tool model

Tiers (every tool belongs to exactly one):

| Tier | Purpose | Count target | Default |
|------|---------|--------------|---------|
| T1 sharp | one question → one call, names in, trimmed out | ~15 | on |
| T2 computed | aggregates the API doesn't provide | ~5 | on |
| T3 power | GraphQL, schema search, generic request | 4 | on |
| T4 plugin | per-app tools (Golden Config, SSoT, …) | per app | on if app detected |
| T5 write | create/update/delete | few | **off** |

Naming: `nautobot_<noun>` / `nautobot_<verb>_<noun>`; no server-name prefix (MetaMCP adds one).
Budget: ≤ ~30 tools visible by default; plugin tools only appear when relevant.

## 7. Response contract

Every tool returns `{summary, data, meta, error}`:
`summary` one line the model can quote; `data` projected fields; `meta` = `{scope,count,truncated,partial,warnings,note,elapsed_ms}`;
`error` = `{kind,message,operation,choices}`. Kinds: `ambiguous_target`, `target_not_found`, `invalid_input`, `not_ready`, `timeout`, `api_error`, `read_only`, `unexpected_error`.
Hard failures also set MCP `isError`. Details in SDS §2.

## 8. Security model

- Read-only **by default** (`NAUTOBOT_MCP_READ_ONLY=true`). Enforced in the Gateway, not only by hiding tools; the generic request tool refuses non-GET (except known read-style POSTs: GraphQL, available-ips).
- Nautobot token: read-only token recommended; never logged; redacted in diagnostics.
- HTTP transport has **no inbound auth in v1**: bind `127.0.0.1` by default; docker compose publishes no port. Optional static bearer (`NAUTOBOT_MCP_AUTH_TOKEN`) is in scope for v1 if D6 = yes.
- Optional tenant scope (`TENANT_SCOPE`) applied centrally in the Gateway (list filters, reads, writes).
- Responses are untrusted data: fields copied from Nautobot are returned as data and never interpreted as instructions.

## 9. Observability and startup (implemented, retained)

`ServerState` + `/healthz`-style status resource, startup log resource, structured `not_ready`, progress/log notifications where the client relays them, `--check`, `--diagnose`. New: module/profile status in the status report.

## 10. Search and knowledge strategy

- **Endpoint discovery**: needed for the long tail and plugins. Options: embeddings (current; heavy), BM25 over operation summaries (tiny, instant, no model), or none (browse per-app resources). Default recommendation: **BM25 in core, embeddings optional extra** (D2).
- **Knowledge base** (docs/repos): optional extra; index-time chunking by heading/function with path+line ranges (D3).

## 11. Configuration surface (v1)

`NAUTOBOT_URL`, `NAUTOBOT_TOKEN`, `NAUTOBOT_VERIFY_SSL`/`CA_BUNDLE`, `NAUTOBOT_MCP_TRANSPORT|HOST|PORT`,
`NAUTOBOT_MCP_READ_ONLY`, `…_TOOLSETS`, `…_DISABLED_TOOLS`, `…_PLUGINS` (override detection), `…_TENANT_SCOPE`,
`…_MAX_RESPONSE_CHARS`, `…_TOOL_TIMEOUT`, `…_MAX_CONCURRENCY`, `…_DATA_DIR`, `…_ENABLE_KB`, `…_AUTH_TOKEN`.
Single flat env-var set; no environment-profile mapping.

## 12. Decisions

| ID | Decision | Recommendation | Status |
|----|----------|----------------|--------|
| D1 | Fork vs new project; package name (`nautobot-mcp` is taken on PyPI by bsmeding) | New repo, history preserved, MIT credit to Kevin Campos; new name | **OPEN** |
| D2 | Keep embeddings for endpoint search? | BM25 default; embeddings optional extra | **OPEN** |
| D3 | KB: keep, chunk at index time? | keep as optional extra; chunk | **OPEN** |
| D4 | GraphQL in v1 | yes, read-only | proposed |
| D5 | Supported Nautobot versions | 2.3+ and 3.x; verify with `--diagnose` on real instance | **needs data** |
| D6 | Inbound auth for HTTP | static bearer token optional in v1; OAuth later | **OPEN** |
| D7 | Write tools in v1 | none beyond generic request when `READ_ONLY=false` | proposed |
| D8 | MCP SDK | stay on FastMCP 2.x, pinned `<3` | proposed |
| D9 | Language of tool args | human names in, UUIDs accepted everywhere | proposed |

## 13. Milestones

M0 Spec sign-off (this doc + SDS) → M1 framework (registry, response contract, gateway, resolver, READ_ONLY) →
M2 core + dcim + ipam T1 tools → M3 computed tools + resources + prompts → M4 plugin detection + first plugin module →
M5 BM25 search, KB chunking → M6 packaging, rename, docs.
Each milestone ends green on the offline suite; M2+ also on the real-instance `--diagnose` fixtures.
