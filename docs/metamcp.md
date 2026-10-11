# Using with MetaMCP

MetaMCP aggregates MCP servers behind one endpoint ("namespace"). The notes below come from reading its
source (`apps/backend/src/lib/metamcp/metamcp-proxy.ts`, a June 2026 snapshot; your deployed version may
differ slightly) and from replaying its list-then-read flow against this server.

## How MetaMCP treats each MCP feature

| Feature | What MetaMCP does | What this server does about it |
|---|---|---|
| **Tools** | Proxied; names become `<server>__<tool>` | Hint text says the tool names "may carry a server prefix" |
| **Prompts** | `prompts/list` and `prompts/get` are proxied; names become `<server>__<prompt>` | Six prompts; none depends on a fixed name |
| **Resources** | `resources/list` and `resources/read` are proxied. **A URI can only be read if it appeared in `resources/list`** | Every resource a model is pointed at is a concrete listed resource, including one per API app (`nautobot://api/endpoints/dcim`, ...). Plugin apps are added once the index is ready, with a `list_changed` notification |
| **Resource templates** | Listed, but not readable (they never enter the URI map) | `nautobot://api/endpoints/{app}` is still offered for clients that support templates, but nothing relies on it |
| **Server `instructions`** | **Not forwarded.** The proxy's instructions are the *namespace description* | Paste the instructions into the namespace description (see below) |
| **Log notifications** (`notifications/message`) | The proxy declares no `logging` capability and I found no code that relays notifications | Startup events appear in the server's stderr / log view in MetaMCP. The channels that do reach the model are tool results and the status tool/resource |
| **Progress notifications** | The client's progress token is passed to the backend, and a progress notification resets the request timeout, but I found no code that relays progress downstream | Calls that wait for a warming index stay under MetaMCP's 60 s default request timeout (`NAUTOBOT_MCP_READY_WAIT` defaults to 20 s) |

To confirm on your deployment: open the server in MetaMCP and use **Load resources** and **Load prompts**.
You should see the 13 concrete resources (more once the index has built) and the prompts as
`<server>__query_nautobot` etc. Resource *templates* may show up in a separate list but cannot be opened.

## Give the model its instructions

Because MetaMCP does not pass the server's `instructions` to clients, put them where it does: the
**namespace description**. Print the exact text for your configuration:

```bash
uvx --from git+https://github.com/agreen/nautobot_mcp@uvx-packaging nautobot-mcp --print-instructions
```

(`NAUTOBOT_MCP_ENABLE_KB` affects the text; set it the same way as in the server definition.)

## What reaches the model through MetaMCP

These work regardless of how MetaMCP handles notifications and instructions:

- **Tool descriptions** (search first, never guess, confirm writes)
- **Tool results**: search results say how to call each endpoint; errors carry Nautobot's detail plus a
  fix-it hint; `not_ready` results state what is being built, the percentage, and `retry_in_seconds`
- **`nautobot_server_status`** and the resources, which an assistant can call or read on demand
- **Prompts**, when a user picks one

## Timeouts

MetaMCP's defaults are 60 s for requests. The first launch via `uvx` installs ~130 packages (about 30 s),
so pre-warm it once (`uvx ... nautobot-mcp --help`) before adding it to a namespace.
