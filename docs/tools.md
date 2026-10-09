# MCP Tools Reference

The Nautobot MCP Server provides a comprehensive set of tools for interacting with Nautobot APIs and searching documentation. This page documents all available tools, their parameters, and usage examples.

## Tool Categories

- [API Discovery & Execution](#api-tools)
- [Knowledge Base Search](#knowledge-base-tools)
- [Repository Management](#repository-management-tools)
- [System Tools](#system-tools)

---

## API Tools

### `nautobot_openapi_api_request_schema`

Find Nautobot API endpoints by meaning, using the embedding index of the instance's OpenAPI schema.

**Purpose:** Discover the right endpoint, its filters and (for writes) its body fields. The assistant
should always call this before `nautobot_dynamic_api_request` instead of guessing paths.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | What you want to do, in plain language (e.g. "create a new device") |
| `n_results` | integer | No | Number of matching endpoints to return (default: 5) |

#### Response

Each match is compact and ready to use. Paths are relative to `/api`; request bodies are resolved from
the schema's definitions (required fields first, read-only fields omitted); GET filters are listed
(capped, with a count of the rest):

```json
{
  "matching_endpoints": [
    {
      "method": "POST",
      "path": "/dcim/devices/",
      "description": "Create a new device.",
      "operation_id": "dcim_devices_create",
      "body_fields": [
        "device_type (string, required): Device type (UUID)",
        "location (string, required): Location (UUID)",
        "name (string, required): Device name",
        "role (string, required): Role (UUID)",
        "status (string, required): Status (UUID or name)",
        "serial (string)",
        "tags (array of string)"
      ]
    }
  ],
  "how_to_call": "Use nautobot_dynamic_api_request with the method and path shown (relative to /api). ...",
  "schema_url": "https://nautobot.example.com/api/swagger.json"
}
```

GET endpoints carry `query_parameters` instead (`limit`, `offset`, `q`, ... then the rest, with
"... N more filters" when capped), and detail endpoints carry `path_parameters` (`["id"]`, the UUID).
When nothing matches, the response suggests browsing with the `nautobot://api/endpoints/{app}` resource.

---

### `nautobot_dynamic_api_request`

Execute a request against any Nautobot REST API endpoint.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `method` | string | Yes | GET, POST, PUT, PATCH or DELETE |
| `path` | string | Yes | Path relative to `/api`, with a trailing slash (e.g. `/dcim/devices/`). A leading `/api` is tolerated |
| `params` | object | No | Query parameters (filters, `limit`, `offset`, `depth`) |
| `body` | object | No | JSON body for POST/PUT/PATCH |

#### Behavior

- **`depth`:** GET requests get a default `depth` (`NAUTOBOT_MCP_DEFAULT_DEPTH`, default 2) so related
  objects are expanded. Pass `depth` in `params` to override; a negative setting omits it entirely.
- **Paging:** list results that have more pages carry an `_mcp_hint` saying which `offset` to use next.
- **Size cap:** responses are limited to `NAUTOBOT_MCP_MAX_RESPONSE_CHARS` characters. Over-long lists are
  trimmed to fit and say so in `_mcp_hint`.
- **Errors are results, not exceptions.** A non-2xx response, a timeout, a TLS problem or an unreachable
  host comes back as JSON with Nautobot's own error detail and a hint on how to fix the request:

```json
{
  "error": true,
  "status": 400,
  "message": "POST /dcim/devices/ failed: HTTP 400 Bad Request",
  "hint": "The request was rejected as invalid. The detail names the offending fields ...",
  "detail": { "device_type": ["This field is required."], "role": ["This field is required."] }
}
```

- A successful DELETE returns `{"status": 204, "result": "success (no content returned)"}`.

The tool description tells the assistant to confirm with the user before any POST/PUT/PATCH/DELETE; the
`nautobot_safe_change` prompt turns that into a preview-confirm-apply-verify workflow.

---

### `nautobot_server_status`

Report the server's own health: version, uptime, how long startup and index warm-up took (per phase),
whether each index is ready, index sizes and the most recent startup events. Takes no parameters and
works before the indexes are ready. See [Observability](observability.md).

### `refresh_endpoint_index`

Manually refresh the OpenAPI endpoint index from Nautobot.

**Purpose:** Update the endpoint search index after Nautobot updates or plugin installations.

#### Parameters

None

#### Example Usage

**Command:**
```
Refresh the endpoint index
```

**Response:**
```json
{
  "status": "success",
  "message": "Endpoint index refreshed",
  "endpoints_indexed": 250,
  "time_taken": "2.3s"
}
```

#### When to Use

- ⬆️ After Nautobot upgrades
- 🔌 After installing new plugins
- 🔄 Periodically for consistency

---

## Knowledge Base Tools

### `nautobot_kb_semantic_search`

Search through indexed Nautobot documentation and code using semantic similarity.

**Purpose:** Find relevant documentation, code examples, and best practices from indexed repositories.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query (natural language) |
| `n_results` | integer | No | Number of results to return (default: 5) |
| `optimize_for_llm` | boolean | No | Optimize content for LLM consumption (default: false) |

#### Example Usage

**Query:**
```json
{
  "query": "How do I create custom fields in Nautobot?",
  "n_results": 3,
  "optimize_for_llm": true
}
```

**Response:**
```json
{
  "results": [
    {
      "repository": "nautobot/nautobot",
      "file_path": "docs/user-guides/custom-fields.md",
      "content": "Custom fields allow you to add additional attributes...",
      "similarity_score": 0.92,
      "metadata": {
        "file_type": "markdown",
        "last_updated": "2024-10-10"
      }
    },
    {
      "repository": "nautobot/nautobot",
      "file_path": "examples/custom_fields_example.py",
      "content": "from nautobot.extras.models import CustomField...",
      "similarity_score": 0.88,
      "metadata": {
        "file_type": "python",
        "last_updated": "2024-09-15"
      }
    }
  ],
  "total_searched": 5000,
  "query_time": "0.15s"
}
```

#### Search Strategies

**Basic Search:**
```
"device configuration"
```

**Specific Topics:**
```
"GraphQL API authentication"
```

**Code Examples:**
```
"pynautobot example for creating devices"
```

**Best Practices:**
```
"best practices for organizing locations"
```

#### Use Cases

- 📚 **Documentation:** "How does the plugin system work?"
- 💻 **Code Examples:** "Show me PyNautobot examples"
- 🎓 **Learning:** "What are device roles?"
- 🔍 **Troubleshooting:** "How to debug Nautobot jobs"

---

## Repository Management Tools

### `nautobot_kb_list_repos`

List all repositories configured in the knowledge base.

#### Parameters

None

#### Example Usage

**Query:**
```
List all repositories
```

**Response:**
```json
{
  "repositories": [
    {
      "name": "nautobot/nautobot",
      "description": "Core Nautobot application",
      "priority": 1,
      "enabled": true,
      "branch": "main",
      "file_patterns": [".py", ".md", ".txt", ".rst"]
    },
    {
      "name": "nautobot/nautobot-plugin-golden-config",
      "description": "Golden Config Plugin",
      "priority": 2,
      "enabled": true,
      "branch": "main",
      "file_patterns": [".py", ".md", ".txt", ".rst"]
    }
  ],
  "total_count": 2
}
```

---

### `nautobot_kb_add_repo`

Add a new repository to the knowledge base.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repo` | string | Yes | Repository name in format 'owner/name' (e.g., 'nautobot/nautobot') |
| `description` | string | No | Repository description |

#### Example Usage

**Add Repository:**
```json
{
  "repo": "myorg/nautobot-custom-plugin",
  "description": "Custom internal plugin"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Added repository: myorg/nautobot-custom-plugin"
}
```

---

### `nautobot_kb_remove_repo`

Remove a repository from the knowledge base.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repo` | string | Yes | Repository name to remove |

#### Example Usage

**Remove Repository:**
```json
{
  "repo": "myorg/old-plugin"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Repository removed",
  "documents_deleted": 45
}
```

---

### `nautobot_kb_update_repos`

Update (pull latest changes) for configured repositories.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `force` | boolean | No | Force re-index all content (default: false) |

#### Example Usage

**Update All Repositories:**
```json
{
  "force": false
}
```

**Response:**
```json
{
  "status": "success",
  "updated": 5,
  "new_documents": 12,
  "updated_documents": 8,
  "time_taken": "45s"
}
```

---

### `nautobot_kb_init_repos`

Initialize/re-initialize all repositories in the knowledge base.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `force` | boolean | No | Force re-initialization (default: false) |

#### Example Usage

```json
{
  "force": true
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Repositories initialized",
  "total_repos": 5,
  "total_documents": 1250,
  "time_taken": "3m 45s"
}
```

---

### `nautobot_kb_repo_status`

Get status information about knowledge base repositories.

#### Parameters

None

#### Example Usage

**Get Status:**
```
Check repository status
```

**Response:**
```json
{
  "repositories": [
    {
      "name": "nautobot/nautobot",
      "status": "active",
      "documents": 450,
      "last_indexed": "2024-10-14 20:30:00",
      "indexing": false
    }
  ],
  "total_documents": 1250,
  "indexing_active": false
}
```

---

## System Tools

### Error Handling

All tools return consistent error responses:

```json
{
  "status": "error",
  "error": "Error description",
  "details": "Additional error context"
}
```

### Common Errors

| Error Code | Cause | Solution |
|------------|-------|----------|
| 401 | Invalid API token | Check `NAUTOBOT_TOKEN` in `.env` |
| 403 | Insufficient permissions | Grant token appropriate permissions |
| 404 | Resource not found | Verify endpoint path or resource ID |
| 429 | Rate limit exceeded | Wait or add GitHub token |
| 500 | Server error | Check Nautobot server logs |

---

## Using Tools with Copilot

### Natural Language Queries

Copilot automatically selects appropriate tools based on your query:

**Examples:**

```
"Find device management endpoints"
→ Uses: nautobot_openapi_api_request_schema

"Get all active devices"
→ Uses: nautobot_dynamic_api_request

"How do I create a job?"
→ Uses: nautobot_kb_semantic_search

"Add nautobot-plugin-ssot to the knowledge base"
→ Uses: nautobot_kb_add_repo
```

### Explicit Tool Usage

You can request specific tools:

```
Use nautobot_kb_semantic_search to find information about GraphQL
```

### Chaining Tools

Copilot can chain multiple tools:

```
First find the endpoint for creating devices, 
then show me an example from the knowledge base,
finally create a test device named "test-switch-01"
```

This will use:
1. `nautobot_openapi_api_request_schema`
2. `nautobot_kb_semantic_search`
3. `nautobot_dynamic_api_request`

---

## Best Practices

### API Tools

✅ **Do:**

- Use specific queries for endpoint discovery
- Include necessary parameters in API requests
- Validate data before POST/PATCH operations

❌ **Don't:**

- Make destructive operations without confirmation
- Hardcode resource IDs (query first)
- Skip error handling

### Knowledge Base Tools

✅ **Do:**

- Use natural language in queries
- Enable LLM optimization for longer docs
- Search before implementing features

❌ **Don't:**

- Use overly generic queries
- Ignore source repository information
- Expect real-time Nautobot data

### Repository Management

✅ **Do:**

- Add repos relevant to your work
- Update repos periodically
- Remove unused repos to save space

❌ **Don't:**

- Add very large repos without consideration
- Force re-index frequently
- Add repos without descriptions

---

## Performance Tips

### Search Optimization

- **Specific queries** return better results than generic ones
- **Enable LLM optimization** for better summarization
- **Limit results** to what you need (default: 5)

### Caching

Tools leverage caching:

- **Endpoint index** cached until refresh
- **Embeddings** cached in ChromaDB
- **Repository content** cached locally

### Resource Usage

Monitor resource usage:

```bash
# Check backend size
du -sh backend/

# Check repository cache
du -sh ~/.cache/nautobot_mcp/
```

---

## Next Steps

- [Examples](examples.md) - Real-world usage examples
- [Architecture](architecture.md) - How tools work internally
- [Troubleshooting](troubleshooting.md) - Solve common issues
