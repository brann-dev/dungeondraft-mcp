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
**Verify first:** call `ping` (expect `pong`, a protocol version and
`authenticated: true`), then `get_status` (expect `map_open: true` and a
`map_center`). If `ping` fails, the mod isn't loaded or DD isn't running — ask
the user to start it; don't retry blindly. `authenticated: false` means the
server and the mod don't share the bridge token (restart DD and reconnect);
`paused: true` means the user paused AI edits in DD's MCP Bridge panel: say
so and make no edits until they resume.

## Working with the map

- Coordinates are **woxel** (world-pixel) space; get `map_center` from
  `get_status`. There are 256 woxels per tile.
- Every element has an integer `id`; create calls return it, `list_elements` /
  `get_element` report it. Feed ids back into move/modify/delete.
- Discover assets with `list_assets` (it takes a `category` and a substring
  `search`); list categories with `list_asset_categories`. A map only sees
  the asset packs it includes: `list_asset_packs` says which, and a map made
  without packs silently sees just the core assets. `search_pack_contents`
  looks inside every installed pack; `prepare_map_with_packs` gives a copy of
  the map more packs.
- **Look at your work**: `screenshot` (current view) and the camera tools
  (`set_camera`, `focus_element`, `fit_elements`) return/aim the view so you can
  inspect what you built and iterate. Use them.
- The bridge keeps its **own** `undo` / `redo` stacks (independent of DD's
  Ctrl+Z). Deletes are undoable; `batch`, `scatter_objects`, `build_room`,
  `place_objects`, `move_elements` and `delete_elements` are each ONE undo
  step. `checkpoint(label)` before a multi-step change lets
  `rollback_checkpoint(label)` take all of it back. `delete_level` is not
  undoable.
- **Furnishing a room:** run `validate_floorplan` first (is every room
  reachable, does every door open onto something), read `get_asset_footprint`
  before choosing a scale (sprites vary wildly in size), place with
  `check=True` so an object that would cross a wall, block a door or overlap
  another is refused with the reason, use `arrange_objects` for a row along a
  wall, seats around a table or items on a surface, and run `validate_layout`
  and `validate_scene` (bare ground, lights with no source) when done. A refusal means nothing changed:
  move, turn or shrink the object rather than dropping the check. Overlaps that
  are intended (chairs under a table, canopies) take `allow_overlap=True`, or a
  different `layer` (rugs lower, tabletop items higher). Footprints are
  rectangles, so still look at a `screenshot`.
- Objects and paths go on layer 100 by default, like Dungeondraft's own tools;
  on one layer later objects cover earlier ones, and a higher layer always wins.
- `configure_level(ambient_light=...)` sets the mood (a dark blue for night
  makes the placed lights glow). `batch` runs many independent edits in one
  call, and `scatter_objects` fills an area with trees or rubble. Light
  `range` is Dungeondraft's own Range (what its Light tool shows); a door
  placed with `add_portal` is closed (blocks sight) unless the asset is a
  window, which is what VTT exports go by.
- Levels are listed **top floor first**; a level's `index` shifts when levels
  are added or cloned (new ones land at index 0). Its `id` holds while the map
  is open but is reassigned on load.
- `native_*` and `ui_*` reach Dungeondraft's own API and controls. They are a
  last resort for what has no dedicated tool: nothing they do is undoable, so
  `save_map` first, and check the effect, since a failed call just returns null.

## Editing the bridge itself

If you change code (not just drive the map), the two halves reload differently:

- **Mod** (`mod/.../mcp_bridge.gd`, GDScript): click **Reload Mods** in
  Dungeondraft's toolbar, or restart DD (`Dungeondraft.exe "<map path>"` reopens
  a map directly; the bridge is up about 12 s later). Changing an existing
  handler needs only this.
- **Reload Mods can also crash DD natively when the script's member `var`s
  changed** (Godot hot-reloads the script into live instances; the crash is in
  `ModManager.Update` → `ModScript.Update`). After adding or removing a
  class-level `var`, restart DD instead of reloading.
- **A GDScript compile (parse) error crashes Dungeondraft natively** when the
  mod loads, with no error message. Compile-check `.gd` changes before
  reloading (see PROTOCOL.md's implementation notes), and avoid static type
  assertions such as `self is Node`: the script has no `extends`, so that line
  doesn't compile.
- **Server** (`server/dungeondraft_mcp/server.py`): new/changed `@mcp.tool()`
  defs need an **MCP-server reconnect** (`/mcp` → reconnect). Adding a *new tool*
  needs **both** reloads. The install is editable — never reinstall.
- GDScript on Godot 3.4.2 has **no try/catch**: an unhandled runtime error in a
  handler crashes the mod and kills the bridge (full DD restart). Validate
  inputs before calling DD API methods.
- A **C# exception inside a DD method** is different: the call returns `null`,
  DD logs *Unhandled Exception* to stdout and carries on. So does a call with
  the wrong number of arguments (C# defaults are not filled in): it silently
  does nothing. Check results; see PROTOCOL.md "Calling DD's C# from the mod".
- `open_map` reuses the already-compiled mod script. Only Reload Mods or a DD
  restart loads an edited `.gd`.
- The layout checks live in `server/dungeondraft_mcp/spatial.py` and are unit
  tested offline: `.venv/bin/python -m unittest discover -s server/tests`.
