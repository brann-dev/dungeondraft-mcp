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

> **Status: prototype.** `ping` / `get_status` / `list_assets` / `place_object`
> are built on documented API calls. `draw_wall` is experimental. One assumption
> is unverified until you run it: that DD's modding sandbox permits TCP at all
> (see step 2).

## Setup

### 1. Install the mod into Dungeondraft

1. In Dungeondraft's title screen, open **Mods** and note (or set) your mods
   folder.
2. Copy `mod/dungeondraft-mcp-bridge/` into that folder.
3. Enable **MCP Bridge** in the mod list, then open or create a map.

On load you should see in the Dungeondraft log:

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol v1)
```

### 2. Verify the socket actually works (the one risky assumption)

With DD open and a map loaded:

```bash
cd server
python test_bridge.py
```

If `ping` and `get_status` come back, networking is allowed and you're done with
the hard part. If it fails *and* you never saw the `listening` line in DD's log,
the sandbox blocked `TCP_Server` — see the fallback note in PROTOCOL.md.

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

Good next targets: `paint_terrain` (`Terrain.Paint`), `draw_path`
(`Pathways.CreatePath`), `select`/`delete` via `node_id`
(`Global.World.GetNodeByID`), and a `screenshot`/export trigger.

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
Engine is Godot 3.5.x, so the GDScript uses Godot 3 networking class names.
