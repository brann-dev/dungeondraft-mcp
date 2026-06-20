# dungeondraft-mcp

An MCP server that drives a **running Dungeondraft instance** over a localhost
socket. Two halves: a Python MCP server (`server/`) and a GDScript mod
(`mod/`) that runs inside Dungeondraft. See [README.md](README.md) for the
architecture and [PROTOCOL.md](PROTOCOL.md) for every command + implementation
notes.

## Before you call any tool

The tools talk to a live Dungeondraft, not this repo. They only work when:

1. **Dungeondraft is running** with the **MCP Bridge mod enabled**, and
2. **a map is open**.

If both aren't true, calls fail (connection refused, or `"no map open"`).
**Verify first:** call `ping` (expect `pong` + a protocol version), then
`get_status` (expect `map_open: true` and a `map_center`). If `ping` fails, the
mod isn't loaded or DD isn't running — ask the user to start it; don't retry
blindly.

## Working with the map

- Coordinates are **woxel** (world-pixel) space; get `map_center` from
  `get_status`. There are 256 woxels per tile.
- Every element has an integer `id`; create calls return it, `list_elements` /
  `get_element` report it. Feed ids back into move/modify/delete.
- Discover assets with `list_assets` (it takes a `category` and a substring
  `search`); list categories with `list_asset_categories`.
- **Look at your work**: `screenshot` (current view) and the camera tools
  (`set_camera`, `focus_element`, `fit_elements`) return/aim the view so you can
  inspect what you built and iterate. Use them.
- The bridge keeps its **own** `undo` / `redo` stacks (independent of DD's
  Ctrl+Z). `delete_element` is **not** undoable.

## Editing the bridge itself

If you change code (not just drive the map), the two halves reload differently:

- **Mod** (`mod/.../mcp_bridge.gd`, GDScript): reload the mod inside Dungeondraft
  (keeps the current map). Changing an existing handler needs only this.
- **Server** (`server/dungeondraft_mcp/server.py`): new/changed `@mcp.tool()`
  defs need an **MCP-server reconnect** (`/mcp` → reconnect). Adding a *new tool*
  needs **both** reloads. The install is editable — never reinstall.
- GDScript on Godot 3.4.2 has **no try/catch**: an unhandled runtime error in a
  handler crashes the mod and kills the bridge (full DD restart). Validate
  inputs before calling DD API methods.
