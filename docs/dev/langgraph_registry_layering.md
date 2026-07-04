# Layered LangGraph agent registry (platform base + client overlay)

## The problem

`langgraph.json` is the agent-graph registry: it maps an `agent_name` to the
`build_workflow` that implements it. At runtime it is read by four consumers:

- `app/websocket/request_handler.py` — resolves `agent_name` → workflow path, then
  dynamically imports and compiles it (this is how client agents like `leo` are compiled;
  they are **not** in `main.py`'s hardcoded startup list).
- `app/routers/api.py` `/available-agents` — the UI agent-list.
- `app/routers/ui.py` — validates custom agent modes against the registered graphs.
- `app/agents/leonardo/rails_agent/tools.py` — the AI-builder's `read_langgraph_json` /
  `edit_langgraph_json`, which let an agent register a new agent live (no rebuild).

The registry mixed **two ownership tiers** in one file:

- **Platform** graphs (`rails_agent`, `llamabot`, …) — owned upstream, must flow to every
  instance on update.
- **Client** graphs (`leo`, `user_api_agent`, per-client agents) — owned downstream, must
  never be overwritten.

Leonardo's platform sync (`bin/update`) pulls an **allowlist** of paths wholesale from
upstream via `git checkout upstream/main -- <path>`. `langgraph/langgraph.json` was on the
allowlist so new platform agents would arrive — but a wholesale checkout **clobbers the
client graphs** in the same file. One file, two owners, an all-or-nothing sync: the two
requirements were in direct conflict.

## The fix: split by ownership, merge at read time

Make the ownership boundary a **file** boundary — exactly the allowlist's own rule ("a path
qualifies only if clients never hand-edit it").

| File | Owner | Ships via | On allowlist? | Editable by agent? |
|---|---|---|---|---|
| `langgraph.json` (base) | platform | LlamaBot **image** | no (removed) | no (read-only) |
| `langgraph.local.json` (overlay) | client | host **mount** | never | **yes** |
| `langgraph.d/*.json` (drop-ins) | client | host mount | never | optional |

At read time the `graphs` maps are deep-merged:

```
base.graphs  <  langgraph.d/*.json (sorted)  <  langgraph.local.json
```

Later layers win on a key collision, so a client can register new agents — or deliberately
shadow a platform one — without touching the base. Only `graphs` is merged; `dependencies`
/ `env` and other top-level keys stay platform-owned (read from the base only).

### Why the platform base belongs in the image, not the git sync

Platform agent **code** only ever ships in the image (`app/agents/leonardo/…`). Syncing the
registry independently could register an agent whose code isn't in the running image (or
vice-versa). Baking the base into the image **versions the registry and the code together**
— strictly more correct, and it's what `DISTRIBUTION.md` already says to aim for ("trend the
allowlist toward zero by moving paths into the image").

## Implementation

### `app/lib/langgraph_registry.py`

The single merge helper every consumer funnels through:

- `resolve_base_path()` — locate the base: `$LANGGRAPH_CONFIG` → nearest `langgraph.json`
  walking up from cwd → walking up from the module.
- `local_overlay_path(base)` — the overlay write target: `$LANGGRAPH_LOCAL_CONFIG` else a
  sibling `langgraph.local.json`. Returned even when absent, so callers can create it.
- `overlay_paths_for(base)` — existing overlays in merge order (`langgraph.d/*.json` then
  `langgraph.local.json`).
- `load_registry(base=None)` / `load_graphs(base=None)` — merged full config / merged
  `graphs` map.

Fail-open everywhere: a missing or malformed overlay is logged and skipped so a broken
client file can never take down the registry.

### Consumers wired to the helper

- `api.py` `/available-agents` → `load_graphs()`
- `ui.py` custom-agent-mode validation → `load_graphs()`
- `request_handler.py` `get_workflow_from_langgraph_json` → `load_graphs()` then
  `_parse_graph_entry` (preserves the string / `{workflow, recursion_limit, …}` formats)
- `rails_agent/tools.py`:
  - `read_langgraph_json` → shows the **merged** list of all registered agents + the raw
    editable overlay content.
  - `edit_langgraph_json` → edits **`langgraph.local.json`**, creating it as `{"graphs":{}}`
    on first write. It never touches the base.
- `app/scripts/repair_thread.py` → `load_graphs()` so client agents are repairable.

> The AI-builder registering into the overlay is the payoff: agent-registered graphs now
> survive `bin/update` and container recreates automatically.

### Hard invariant

`langgraph.local.json` **must stay a host-mounted file**. The AI-builder writes to it live;
if it were baked or on an ephemeral path, agent-registered client graphs would vanish on the
next recreate — the same failure class as the SESSION_SECRET-in-ephemeral-`/app` incident.
Platform base → image; client overlay → mount.

## Leonardo changes

- `langgraph/langgraph.local.json` — new client overlay (`leo`, `user_api_agent`).
- `docker-compose.yml` / `docker-compose-dev.yml` — **dropped** the
  `./langgraph/langgraph.json:/app/app/langgraph.json` mount; **added**
  `./langgraph/langgraph.local.json:/app/app/langgraph.local.json`. The
  `./langgraph/agents:/app/app/user_agents` code mount is unchanged.
- `bin/update` — removed `langgraph/langgraph.json` from the `ALLOWLIST` (the tension it
  documented is gone). `test/bin_update_sync.sh` updated to assert the base is no longer
  synced and the overlay is preserved.
- `docs/DISTRIBUTION.md` — the `langgraph/**` open decision marked resolved.

> `Leonardo/langgraph/langgraph.json` is now **vestigial** at runtime (no longer mounted or
> synced). It is left in place for `langgraph dev` CLI use; it is not read by the running
> app. Do not add client agents there — use `langgraph.local.json`.

## LlamaBot base reconciliation

The image base (`app/langgraph.json`) and Leonardo's old merged file had drifted. Added
`rails_user_feedback_agent` (its code was already in the image) so the baked base is a
superset of the platform agents Leonardo clients use. Post-change the dev-box merged
registry is a superset of the previous set — no agent is lost.

## Tests

- `app/tests/test_langgraph_registry.py` — merge contract: overlay adds/shadows, missing &
  malformed overlays fail open, `langgraph.d` ordering, top-level keys preserved.
- `Leonardo/test/bin_update_sync.sh` — base no longer synced; overlay preserved.

## Adding an agent — quick reference

- **Platform agent** (ships to all instances): add code under `app/agents/leonardo/…`,
  register it in `app/langgraph.json`, cut a LlamaBot image. Arrives via the image channel.
- **Client agent** (one instance): drop code under `langgraph/agents/<name>/` and register
  `"<name>": "./user_agents/<name>/nodes.py:build_workflow"` in that instance's
  `langgraph.local.json` (the AI-builder's `edit_langgraph_json` does this for you).

## Env overrides

- `LANGGRAPH_CONFIG` — explicit path to the base file.
- `LANGGRAPH_LOCAL_CONFIG` — explicit path to the client overlay.
