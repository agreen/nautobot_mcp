# Software Design Specification

Status: **DRAFT v0.1.** Implements [HLD.md](HLD.md). Items marked **[VERIFY]** depend on facts about
the real Nautobot that `--diagnose` must confirm before implementation.

## 1. Conventions

- Tool names `nautobot_*`, snake_case. Descriptions: 1–3 sentences, say *when to use it* and *which
  tool to use instead* for neighbouring needs. No parameter docs in prose; use per-parameter
  descriptions in the schema.
- Inputs take **human names**; a UUID is accepted anywhere a name is. Optional filters default to
  `None`. Free-form `dict` parameters are only allowed on T3 tools.
- Every tool carries annotations: `readOnlyHint` (true for T1–T3 reads), `destructiveHint` (T5),
  `idempotentHint`, `openWorldHint=false`, and a short `title`.
- Pagination: `limit` (default 25, max 100) and `offset`. Results beyond `limit` set
  `meta.truncated=true` and the summary says how to page.
- Default `depth=0` on API calls unless the tool needs a nested label, then `depth=1` (**[VERIFY]**
  that `display` is present at depth 0).
- Trailing slash on every API path; paths relative to `/api`.

## 2. Response contract

```
ToolResult {
  summary: str                  # one sentence, quotable; first thing the model should say
  data:    object               # projected fields only
  meta:    { scope?, count?, truncated, partial, warnings[], note?, elapsed_ms }
  error?:  { kind, message, operation?, choices?[] }
}
```

- `partial=true` whenever a sub-request failed; `warnings` names each one.
- `enforce_budget`: if `len(json(data)) > MAX_RESPONSE_CHARS`, halve the largest list repeatedly;
  set `truncated` and a `note` telling the model how to narrow.
- Error kinds and handling:

| kind | when | isError | model guidance in `summary` |
|------|------|---------|-----------------------------|
| `ambiguous_target` | name matched >1 | no | list `choices`, ask the user |
| `target_not_found` | name matched 0 | no | suggest `nautobot_find` |
| `invalid_input` | HTTP 400/422 or bad arg | no | per-field messages in `choices` |
| `not_ready` | background component not ready | no | `retry_in_seconds` |
| `read_only` | write refused | no | how to enable (operator action) |
| `timeout` / `api_error` / `unexpected_error` | | yes | status + detail |

## 3. Core services

### 3.1 Gateway (`core/gateway.py`)
`get(path, params)`, `list(path, params, cap)`, `count(path, params)`, `post_read(path, body)`,
and `send(method, …)` used only by the guarded generic request tool.
- One `httpx.AsyncClient`; `Semaphore(MAX_CONCURRENCY=8)`; per-request timeout; retry 502/503/504
  and transport errors twice with 0.25 s/0.5 s back-off; **never retry non-GET**.
- Write gate: non-GET refused with `read_only` when `READ_ONLY`, except an allow-list of read-style
  POSTs (`/graphql/`, `…/available-ips/`, `…/available-prefixes/` **only GET-equivalent use**).
- No tenant scoping: the Nautobot token's object permissions are the access boundary (HLD §8).
- Normalises errors to `ApiError(status, detail, fields)`. Follows `next` URLs; rewrites host if
  the instance returns an internal hostname in `next` **[VERIFY]**.

### 3.2 Resolver (`core/resolver.py`)
`one(kind, text)`: exact match → `__ic` fallback → raise `Ambiguous(choices[≤25])`/`NotFound`.
Kinds: `device`, `location`, `prefix`, `ip`, `vlan`, `rack`, `interface(device)`. UUID input short-circuits
to a GET. IP/prefix kinds are only queried when the text looks like an address (the API 500s on junk **[VERIFY]**).
Duplicate names across locations (devices are unique per location/tenant in Nautobot 2+) are the
normal ambiguity case; `choices` include `location` to disambiguate.

### 3.3 ReferenceCache
TTL (default 120 s) read-through cache for small lists (locations, roles, statuses, manufacturers,
platforms, location types). Used by resources and by input validation hints. Invalidated by `refresh`.

### 3.4 InstanceProfile
`{version, python, apps[], schema_hash, endpoint_count, api_versions, features{}}`; stored in
`DATA_DIR/profile.json`; loaded at start, refreshed in background; on change emit
`tools/list_changed` + `resources/list_changed`. `features` records measured behaviours
(supports `sort`, supports `q` on devices, `depth` max, CSV export) from `--diagnose`/runtime probes.

### 3.5 Compat
Single module hiding 2.x/3.x and OpenAPI 2/3 differences: status key (`plugins` vs `nautobot-apps`),
field renames (**[VERIFY]** list, e.g. `site`→`location`, `role`, `software_version`), lookups.

## 4. T1 sharp tools (module → tool)

Legend: **In** inputs; **Calls** backing requests; **Out** `data` fields.

### core
| Tool | In | Calls | Out / notes |
|------|----|-------|-------------|
| `nautobot_status` | – | `/status/` (+ server state) | version, apps, server readiness; also what `nautobot://server/status` shows |
| `nautobot_find` | `query`, `types[]?` | per-type list with `name__ic`/`q`, ≤10 each | matches grouped by type with `name`, `id`, key attribute; only queries IP/prefix types when query is address-like |
| `nautobot_changelog` | `object?`, `user?`, `since?`, `limit` | `/extras/object-changes/` | who/what/when, action, object **[VERIFY]** |

### dcim
| Tool | In | Calls | Out |
|------|----|-------|-----|
| `nautobot_list_devices` | `location?`, `role?`, `status?`, `manufacturer?`, `model?`, `platform?`, `software_version?`, `tenant?`, `q?`, `limit`, `offset` | `/dcim/devices/` depth 1 | `[{id,name,role,device_type,status,location,primary_ip4}]` + total count |
| `nautobot_device` | `device` | resolver + `/dcim/devices/{id}/` + interface count | identity, role/type/platform/status, location, rack/position, serial, asset tag, tenant, primary IPs, software version, interface count |
| `nautobot_device_interfaces` | `device`, `connected_only?`, `limit` | `/dcim/interfaces/?device_id=` | name,type,enabled,mode,mtu,mac,status,untagged vlan,lag,cabled,connected_to |
| `nautobot_interface` | `device`, `name` | interface + `/ipam/ip-addresses/?interfaces=` (**[VERIFY]** filter name; 3.x uses a through-table endpoint) | full interface + IPs + peer |
| `nautobot_device_config_context` | `device` | `/dcim/devices/{id}/?include=config_context` | merged context, top-level keys in summary |
| `nautobot_cabling` | `device` | interfaces with `cable` + `/dcim/cables/` | list of local-interface → peer device/interface |
| `nautobot_location` | `location` | location + child locations + device/rack/prefix counts | hierarchy path, counts by status |
| `nautobot_list_locations` | `location_type?`, `parent?`, `q?` | `/dcim/locations/` | name,type,parent,status,device_count |
| `nautobot_rack` | `rack` | rack + `/elevation/` | units used/free, devices by U |

### ipam
| Tool | In | Calls | Out |
|------|----|-------|-----|
| `nautobot_ip_lookup` | `address` | `/ipam/ip-addresses/?address=` + parent prefix + assignment | status, role, DNS name, parent prefix, assigned device/interface or "unassigned" |
| `nautobot_prefix` | `prefix` | prefix + child prefixes + `…/available-ips/` (limit) | status, role, location, vlan, utilisation%, first free IPs |
| `nautobot_list_prefixes` | `location?`, `role?`, `status?`, `vrf?`/`namespace?`, `within?`, `q?` | `/ipam/prefixes/` | prefix,status,role,location,vlan,utilisation |
| `nautobot_vlan` | `vlan` (vid or name), `location?` | `/ipam/vlans/` + prefixes mapped | vid,name,status,group,prefixes |
| `nautobot_list_vlans` | `location?`, `group?`, `status?`, `q?` | `/ipam/vlans/` | vid,name,status,group |

Total T1 ≈ 16. Anything beyond: use T3.

## 5. T2 computed tools

| Tool | Purpose | Algorithm | Cost control |
|------|---------|-----------|--------------|
| `nautobot_device_inventory_summary` | counts of devices by role/status/platform/model, scoped by location | group via repeated `count` calls (limit=1) per facet value, or one paged scan capped at 5,000 rows; prefer facet counts | declare `partial` if cap hit |
| `nautobot_prefix_utilization` | utilisation for a prefix or all prefixes in a location, ranked | `…/available-ips/` size vs prefix size; or `utilization` field if present **[VERIFY]** | cap 50 prefixes |
| `nautobot_data_quality_audit` | devices missing primary IP/platform/rack/software version; IPs unassigned; prefixes without location | `count` with `has_primary_ip=false`-style filters **[VERIFY filters]**, then ≤20 samples per gap | counts exact, samples capped |
| `nautobot_site_report` | one-page location summary | device summary + prefix list + vlan count + rack count + gaps | sub-fetches via `Collector` |
| `nautobot_ip_allocate_suggest` | suggest next free IP/prefix (no write) | `available-ips` / `available-prefixes` | read-only by design |

## 6. T3 power tools

| Tool | Spec |
|------|------|
| `nautobot_graphql` | `query:str`, `variables?`; read-only (reject `mutation`); result via budget; returns GraphQL `errors` verbatim in `error.choices` |
| `nautobot_graphql_schema` | `type?`; root query fields, or fields of one type; cached |
| `nautobot_api_search` | `query`, `limit`; BM25 (or embedding) over OpenAPI operations; returns compact method/path/filters/body fields; same shape as today's schema search |
| `nautobot_request` | `method`,`path`,`params?`,`body?`; GET always; non-GET only if `READ_ONLY=false`; normalises path/trailing slash; depth default 0; structured errors |

## 7. Resources

| URI | Content | Source / refresh |
|-----|---------|------------------|
| `nautobot://guide/api-conventions` | static conventions guide | static |
| `nautobot://guide/tools` | which tool for which question (generated from registry) | generated at registry build |
| `nautobot://server/status`, `…/startup-log` | readiness, components, events | live |
| `nautobot://instance/overview` | version, apps, endpoints per app | InstanceProfile |
| `nautobot://api/endpoints/{app}` (concrete per app) | endpoint list | InstanceProfile |
| `nautobot://ref/locations`, `…/roles`, `…/statuses`, `…/manufacturers`, `…/platforms`, `…/location-types` | `[{id,name,…}]` capped at 1,000 | ReferenceCache |
| `nautobot://kb/repositories` | KB repos (if enabled) | KB state |
Concrete URIs only (MetaMCP cannot read templates).

## 8. Prompts

`find`, `device_report(device)`, `ip_lookup(address)`, `site_inventory(location)`, `data_quality(location?)`,
`prefix_report(prefix)`, `vlan_report(vlan)`, `connectivity(device)`, `change_history(object)`,
`safe_change(description)` (write workflow with confirmation), `diagnose_nautobot_mcp`.
Each prompt names the exact tools to call, in order, and what to put in the answer. Arguments with
autocomplete from ReferenceCache where supported.

## 9. Module framework

```python
class Module(Protocol):
    name: str                       # "dcim", "golden_config"
    requires_app: str | None        # key in InstanceProfile.apps, or None for core
    def tools(self) -> list[ToolSpec]
    def resources(self) -> list[ResourceSpec]
    def prompts(self) -> list[PromptSpec]
```
- Built-in modules live in-package; third-party via entry-point group `nautobot_mcp.modules`.
- Enabled set = (`TOOLSETS` selection or all) ∩ (core ∪ detected apps ∪ `PLUGINS` override) minus `DISABLED_TOOLS`; T5 only if not `READ_ONLY`.
- Registration is idempotent and diffed: on profile change, add/remove tools and send `list_changed`.
- First plugin module target: Golden Config (compliance, config text) — **[VERIFY]** endpoints on the real instance.

## 10. Startup sequence

1. Build app (no I/O), serve `initialize` immediately.
2. Background: load profile cache → register modules → emit `list_changed`.
3. Background: probe Nautobot (`/status/`, schema) → update profile → reconcile modules.
4. Background (if enabled): BM25 index (seconds) → embeddings/KB (minutes, optional).
5. Tools needing the profile wait ≤ `READY_WAIT` with progress, else `not_ready`. T1 tools do **not** wait for indexes.

## 11. Test strategy

- Offline `FakeNautobot` (OpenAPI 3) extended per module; fixtures derived from `--diagnose` shapes.
- Per-tool: happy path, ambiguous, not found, invalid filter, partial failure, truncation, read-only refusal.
- Contract tests: every registered tool has annotations, a `title`, a description ≤ 400 chars, and returns a valid `ToolResult`.
- Registry tests: toolset/disable/read-only/plugin-detection matrices; `list_changed` on profile change.
- MetaMCP compatibility test: list tools/resources/prompts through an in-memory client; concrete resources readable.
- Real-instance smoke (manual, read-only): `--check` and a scripted run of each T1 tool, redacted output saved as fixtures.

## 12. Unverified assumptions (to confirm with `--diagnose` / real instance)

1. `display` present on nested refs at depth 0; field names on devices/interfaces/ip-addresses in the installed version.
2. Filter names: `q`, `has_primary_ip`-style, `interfaces` on ip-addresses, `software_version`, `within`.
3. Availability of utilisation fields on prefixes; speed of `available-ips`.
4. `next` URLs' host rewriting behind a proxy.
5. Object-changes endpoint shape; Golden Config endpoints.
6. Maximum safe `limit` and timeouts under real data volume.
