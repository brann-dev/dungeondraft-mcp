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
> 3.4.2** — raw TCP from the modding sandbox works, no fallback needed. 29 tools
> across query / create / modify / terrain / levels / selection / capture /
> undo (see below). `paint_terrain` is the lone experimental one.

## What the AI can do

- **Inspect:** `get_status`, `list_levels`, `list_elements`, `get_element`,
  `list_asset_categories`, `list_assets` (with substring search).
- **Create:** `place_object`, `draw_wall`, `draw_path`, `add_light`,
  `add_portal`, `add_roof`, `add_text`.
- **Terrain:** `set_terrain_slot`, `fill_terrain`, `paint_terrain`*.
- **Edit:** `move_element`, `modify_object`, `duplicate_object`,
  `delete_element`, `select_elements`, `clear_selection`.
- **Levels:** `add_level`, `set_level`.
- **See:** `screenshot` (current window) and `export_map` (clean full-map
  render) return images, so the model can look at its own work and iterate.
- **Undo:** every create / move / modify / terrain edit registers a Dungeondraft
  undo record (Ctrl+Z works), and `undo` / `redo` drive that stack so the model
  can reverse its own changes.

Every element is referenced by an integer `id`; create and list calls return
ids you feed back into edit calls. Coordinates are woxel (pixel) space — call
`get_status` for `map_center`. Discover assets with `list_assets`. (* experimental.)

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

```bash
cd server
pip install -e .        # or: uv pip install -e .
```

Register it with your MCP client. For **Claude Code**:

```bash
claude mcp add dungeondraft -- dungeondraft-mcp
```

Or add to a client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "dungeondraft-mcp"
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

Good next targets: wall-mounted portals (`Wall.AddPortal`), pattern shapes
(floors), and grouping a batch of edits into a single undo step.

## Layout

```
mod/dungeondraft-mcp-bridge/   the Dungeondraft mod (copy into DD's mods folder)
  mcp_bridge.ddmod             manifest
  scripts/tools/mcp_bridge.gd  TCP server + command handlers
server/                        the Python MCP server
  dungeondraft_mcp/server.py   MCP tool definitions
  dungeondraft_mcp/bridge_client.py  TCP/JSON client
  test_bridge.py               standalone smoke test (run this first)
PROTOCOL.md                    wire protocol + known unknowns
```

## Credits

Built against the [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/).
Engine is Godot 3.4.2, so the GDScript uses Godot 3 networking class names.
