# dungeondraft-mcp

An MCP server that lets an LLM (Claude Code, Claude Desktop, etc.) drive a
**running Dungeondraft instance** — place objects, draw walls, inspect the map —
through Dungeondraft's GDScript modding API.

Dungeondraft has no built-in server to talk to, so this project is **two halves**
that meet over a localhost socket:

```
Claude / MCP client
      │  MCP (stdio)
      ▼
server/   ── Python MCP server  (this repo)
      │  newline-delimited JSON over TCP  (127.0.0.1:8787)
      ▼
mod/      ── Dungeondraft mod  (GDScript, runs inside DD)
      │  modding API calls
      ▼
   the open map  (Objects, Walls, Levels, …)
```

The mod opens a TCP server inside Dungeondraft and polls it every frame from the
`update(delta)` hook; the Python side exposes each capability as an MCP tool and
forwards calls as JSON. See [PROTOCOL.md](PROTOCOL.md) for the wire format.

> **Status: working.** Confirmed end-to-end against Dungeondraft on **Godot
> 3.4.2** — raw TCP from the modding sandbox works, no fallback needed. 34 tools
> across query / create / modify / terrain / levels / selection / capture /
> camera / undo (see below).

## What the AI can do

- **Inspect:** `get_status`, `list_levels`, `list_elements`, `get_element`,
  `list_asset_categories`, `list_assets` (with substring search).
- **Create:** `place_object`, `draw_wall`, `draw_path`, `add_light`,
  `add_portal`, `add_roof`, `add_text`.
- **Terrain:** `set_terrain_slot`, `fill_terrain` (whole level), `fill_region`
  (a rect/polygon, e.g. one room's floor), `paint_terrain` (a soft brush).
- **Edit:** `move_element`, `modify_object`, `duplicate_object`,
  `delete_element`, `select_elements`, `clear_selection`.
- **Levels:** `add_level`, `set_level`.
- **See:** `screenshot` (current window) and `export_map` (clean full-map
  render) return images, so the model can look at its own work and iterate.
- **Camera:** `get_camera`, `set_camera`, `focus_element` (center on one
  element), `fit_elements` (frame a group) — point the view before a
  `screenshot` to inspect specific spots like a door cut into a wall.
- **Undo:** the bridge keeps its own undo/redo stacks for create / move / modify
  / terrain edits, so `undo` / `redo` let the model reliably reverse its own
  changes (independent of Dungeondraft's Ctrl+Z; `delete_element` is not
  reversible).

Every element is referenced by an integer `id`; create and list calls return
ids you feed back into edit calls. Coordinates are woxel (pixel) space — call
`get_status` for `map_center`. Discover assets with `list_assets`.

## Setup

### 1. Install the mod into Dungeondraft

1. In Dungeondraft's title screen, open **Mods** and note (or set) your mods
   folder.
2. Copy `mod/dungeondraft-mcp-bridge/` into that folder.
3. Enable **MCP Bridge** in the mod list, then open or create a map.

On load you should see in the Dungeondraft log:

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol v3)
```

### 2. Verify the commands work

With DD open and a map loaded, run the per-command validator. It builds a small
throwaway scene near the map center, prints PASS/FAIL for each command, then
deletes what it made:

```bash
cd server
python test_bridge.py
```

If it fails to connect *and* you never saw the `listening` line in DD's log, the
sandbox blocked `TCP_Server` — see the fallback note in PROTOCOL.md. (Confirmed
working on Godot 3.4.2, so this should just pass.)

### 3. Install and wire up the MCP server

Install into a dedicated venv (most distros' system Python is externally
managed, so a venv keeps the entrypoint clean and stable):

```bash
python -m venv .venv
.venv/bin/pip install -e ./server
```

This creates the `.venv/bin/dungeondraft-mcp` entrypoint. Register it with your
MCP client using its **absolute path**. For **Claude Code**:

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

Restart Claude Code so the server loads (`/mcp` shows its status and tools).
Or add to a client config (e.g. Claude Desktop `claude_desktop_config.json`),
using the absolute path:

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

Then ask the model things like *"what's the status of my Dungeondraft map?"* or
*"list some object assets and drop a chair in the middle of the map."*

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | bridge host |
| `DD_BRIDGE_PORT` | `8787` | bridge port (must match the mod's `PORT` const) |

## Extending

Adding a capability is symmetric — one handler on each side:

1. **Mod** (`mod/.../scripts/tools/mcp_bridge.gd`): add a `case` in `_dispatch()`
   and a `_my_command(req)` function returning `_ok(...)` / `_err(...)`.
2. **Server** (`server/dungeondraft_mcp/server.py`): add an `@mcp.tool()` that
   calls `bridge.request("my_command", ...)`.

Good next targets: pattern shapes (floors), region-scoped terrain fill, and
grouping a batch of edits into a single undo step.

### Dev loop (read this before iterating on the mod)

Two things will bite you if you don't know them up front:

- **Editing the mod and the server are different reload paths.** GDScript
  changes in `mcp_bridge.gd` take effect when you **reload the mod inside
  Dungeondraft** (or restart DD). New or changed `@mcp.tool()` definitions in
  `server.py` only appear after the **MCP server process restarts** — in Claude
  Code that means `/mcp` → reconnect `dungeondraft` (or restart the client).
  The install is editable (`pip install -e`), so you never reinstall; you just
  cycle the process. Adding a *new tool* needs **both** reloads (mod for the
  handler, server for the tool registration); changing an *existing* handler's
  behavior needs only the mod reload.

- **There's no live `eval`/introspect command**, so probing a running node's
  properties needs a mod reload. When a DD API behaves unexpectedly (e.g. a
  setter that doesn't stick), a quick way to diagnose it is to **return
  intermediate state in the response** — stash before/after values in a debug
  field — so one reload shows where a value changes.

## Layout

```
mod/dungeondraft-mcp-bridge/   the Dungeondraft mod (copy into DD's mods folder)
  mcp_bridge.ddmod             manifest
  scripts/tools/mcp_bridge.gd  TCP server + command handlers
server/                        the Python MCP server
  dungeondraft_mcp/server.py   MCP tool definitions
  dungeondraft_mcp/bridge_client.py  TCP/JSON client
  test_bridge.py               standalone smoke test (run this first)
PROTOCOL.md                    wire protocol + implementation notes
```

## Credits

Built against the [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/).
Engine is Godot 3.4.2, so the GDScript uses Godot 3 networking class names.
