# dungeondraft-mcp

English · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Français](README.fr.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md)

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
> 3.4.2** — raw TCP from the modding sandbox works, no fallback needed. 103 tools
> across query / create / arrange / modify / terrain / water / materials /
> levels / layers / layout and scene checks / asset packs / map files / VTT
> import and export / selection / capture / camera / history / map lifecycle,
> plus an escape hatch to Dungeondraft's own API and UI (see below).

## What the AI can do

- **Inspect:** `ping` (also says whether the user has paused AI edits), `get_status`, `list_levels`, `list_elements`,
  `get_element`, `list_asset_categories`, `list_assets` (with substring search),
  `get_terrain` (read the painted terrain back as a grid of slot weights).
- **Asset packs:** `preview_assets` (one contact sheet of up to 32 candidate
  assets, so you can look before you place; `true_scale` shows their relative
  sizes), `list_asset_packs` (which packs are installed, and which
  this map can actually use: Dungeondraft scopes packs per map, and a map
  made without them silently sees only the ~1800 core assets),
  `search_pack_contents` (search inside every installed pack, including ones
  the map lacks, by words and tags, never reading the art) and
  `prepare_map_with_packs` (write a copy of the map that includes more packs
  and open it). `inspect_map_file` summarises a `.dungeondraft_map` without
  opening it: packs it needs, counts per level, most used textures, doors
  whose closed flag contradicts their name; `compare_to` diffs two files.
- **Create:** `place_object`, `draw_wall`, `draw_path`, `add_light`,
  `add_portal`, `add_roof`, `add_text`, `place_pattern` (tiled floor shapes —
  wood planks, tile, brick — rendered below objects), `build_room` (a wall loop
  + matching floor on one shared path, like the UI's combined trace),
  `import_image` (embed a PNG/JPG/WEBP from disk as an object: custom props,
  handouts or a whole battle-map backdrop; the pixels are stored in the map),
  `scatter_objects` (a stand of trees or a field of rubble in one call, kept
  off walls and out of doorways), `place_objects` (many objects at chosen
  spots in one call), `arrange_objects` (a row of shelves flush against a
  wall, chairs ringing a table, items on a desk, fitted to what each asset
  actually draws), `list_prefabs` / `place_prefab` (Dungeondraft's saved
  groups), `generate_dungeon` (the Map Wizard's dungeon or cave layout in one
  call), `set_trace_image` (a reference picture shown over the map to trace
  from). `build_room` reuses a wall that already stands on the boundary, so
  two rooms side by side share one wall and a door between them opens in a
  VTT.
- **Terrain:** `set_terrain_slot`, `fill_terrain` (whole level), `fill_region`
  (a rect/polygon, e.g. one room's floor), `paint_terrain` (a soft brush),
  `paint_path` (a smooth continuous stroke along a polyline — roads/trails).
  Slots 0-3 by default; `set_terrain_slot(..., expand=True)` turns on the
  level's slots 4-7.
- **Water & materials:** `draw_water` (lakes, rivers and moats as a rect or
  polygon, with shoreline and depth gradient; `erase` cuts islands) and
  `draw_material` (lava, ice, cobblestone and other Material Brush textures
  over a rect or polygon, on a chosen layer).
- **Caves:** `dig_cave` carves caverns/tunnels along a path (the Cave Brush —
  dig open floor out of rock, with auto rocky walls + debris; `dig=false` fills
  back; optional ground/wall tints), `clear_caves` wipes the whole cave layer.
- **Edit:** `move_element`, `move_elements` (several by one offset),
  `modify_object` (scale, rotation, color, shadow, mirror, light blocking),
  `modify_wall` (reshape or restyle a wall; its doors and windows stay mounted
  and follow their segment), `modify_light`, `modify_text`,
  `duplicate_object`, `delete_element`, `delete_elements`, `select_elements`,
  `clear_selection`. `set_map_style` sets building wear and the grid's line
  style map-wide; `set_map_size` resizes the map in tiles, as Dungeondraft's
  own Change Map Size does, and `crop_map` keeps a window of tiles from any
  side, previewing what would be removed first. The placement tools take `snap=True` to land on
  the user's grid, including Lievven's Custom Snap Mod's hex or isometric
  grid when that mod is loaded; `get_snap_settings` reads it.
- **Levels:** `add_level`, `set_level`, `rename_level`, `clone_level` (copy a
  whole floor to start the next one from), `delete_level`, `reorder_levels`, `compare_levels`
  (show another floor faintly beneath the current one to line things up),
  `configure_level` (ambient light for dusk and night scenes, lighting on/off,
  terrain visibility and blending, the water's shoreline).
- **Layers:** `list_layers`, `set_element_layer` (decide what covers what: a
  rug under a table, a canopy over a path), `add_layer`; `place_object` and
  `import_image` take a `layer` too.
- **Layout checks:** `get_asset_footprint` (how big an asset really is, before
  picking a scale), `check_object_placement` (try a spot: does it cross a
  wall, block a door, overlap something?), `validate_layout` (audit a furnished
  level), `validate_floorplan` (before furnishing: is every room reachable,
  does every door open onto something, is there a way in), `validate_scene`
  (bare ground with nothing painted on it, and lights with no visible source),
  and `set_spatial_region` / `remove_spatial_region` /
  `list_spatial_regions` for rooms, keep-clear zones and protected ground. The
  object tools take `check=True` to refuse a bad placement outright; terrain,
  cave, water and material edits refuse to touch a `protected` region.
- **Map lifecycle:** `get_current_map` (open map's path + whether a save is
  running), `save_map` (save to the current file, like DD's Save button), and
  `open_map` (switch to another saved map; replies before the new map loads, so
  poll `get_current_map`; unsaved changes are discarded silently, so `save_map`
  first).
- **See:** `screenshot` (current window) and `export_map` (clean full-map
  render, `grid=False` to export without the grid) return images, so the model
  can look at its own work and iterate. `set_grid` shows/hides the editor grid.
  `export_uvtt` writes a Universal VTT `.dd2vtt` (image + walls, doors, lights)
  for import into Foundry or Roll20, and `import_dd2vtt` rebuilds one on the
  open map: walls rejoined through each doorway, doors and windows, lights and
  ambient light, with the export's image underneath as a trace.
- **Camera:** `get_camera`, `set_camera`, `focus_element` (center on one
  element), `fit_elements` (frame a group), `reset_camera` (recover a lost
  view) — point the view before a `screenshot` to inspect specific spots like a
  door cut into a wall.
- **Work alongside you:** `get_selection` reads what you have selected in
  Dungeondraft (ids, kinds and the bounds of the selection) and `get_cursor`
  where your mouse pointer rests on the map, so you can select a few things or
  park the pointer somewhere and say "make these bigger" or "put a chest here".
- **Native access:** `native_targets`, `native_describe`, `native_get`,
  `native_set`, `native_call` reach Dungeondraft's own objects, and `ui_tree` /
  `ui_action` read and operate its panels and dialogs, for anything without a
  dedicated tool (smooth terrain blending, a brush's size, a biome preset).
  A last resort: it bypasses undo, so save first.
- **Batch:** `batch` runs up to 100 tool calls in one request, for long runs
  of independent placements, as one undo step.
- **Undo:** every reversible edit (create / delete / move / modify / layer /
  terrain / cave / water / material) is a step in Dungeondraft's own undo
  history, on one timeline with the edits you make by hand. Your Ctrl+Z and
  Ctrl+Y walk through the assistant's steps as well as your own, and its
  `undo` / `redo` move the same history; they leave a step you made by hand
  alone unless you ask for it (`include_user`). A batch, a scatter, a built
  room, a prefab or a multi-element delete is one step. `checkpoint` marks a
  point in history and `rollback_checkpoint` undoes everything since in one
  call; `list_checkpoints` shows what is open. History keeps as many steps as
  Dungeondraft's **Max undos** preference (32 by default). On a Dungeondraft
  build that lacks what this needs, the bridge falls back to an undo list of
  its own, separate from Ctrl+Z, and `ping` says which is in use.

Every element is referenced by an integer `id`; create and list calls return
ids you feed back into edit calls. Coordinates are woxel (pixel) space — call
`get_status` for `map_center`. Discover assets with `list_assets`.

## Setup

**Prerequisites:** [Dungeondraft](https://dungeondraft.net/) (tested on the
Godot 3.4.2 builds), Python **3.10+**, and an MCP client (e.g. Claude Code or
Claude Desktop). On Windows, `python`/`.venv\Scripts\` replace the `python3`/
`.venv/bin/` paths shown below.

### 1. Install the mod into Dungeondraft

1. In Dungeondraft's title screen, open **Mods** and note (or set) your mods
   folder.
2. Copy `mod/dungeondraft-mcp-bridge/` into that folder.
3. Enable **MCP Bridge** in the mod list, then open or create a map.

On load you should see in the Dungeondraft log:

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` is the bridge's protocol version, which goes up as commands are added
(`ping` reports it too).

### 2. Install the MCP server

Install into a dedicated venv (most distros' system Python is externally
managed, so a venv keeps the entrypoint clean and stable). Run from the repo
root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

This installs the `dungeondraft_mcp` package and creates the
`.venv/bin/dungeondraft-mcp` entrypoint.

The first time Dungeondraft starts with the mod, the mod writes a random token
to Dungeondraft's user folder (next to `config.ini`); the server reads it from
there, so nothing needs configuring. Only a program that can read your files
can drive your map. The **MCP Bridge** panel under Dungeondraft's Settings
shows what the bridge is doing and holds the controls that are yours alone:
**Pause AI edits** (read-only tools keep working), **Undo the assistant's
last step**, and the token switch.

### 3. Verify the bridge works

With DD open and a map loaded, run the per-command validator. It builds a small
throwaway scene near the map center, prints PASS/FAIL for each command, then
deletes or undoes what it made:

```bash
.venv/bin/python server/test_bridge.py
```

If it fails to connect *and* you never saw the `listening` line in DD's log, the
sandbox blocked `TCP_Server` — see the fallback note in PROTOCOL.md.

The layout checks are plain geometry and have their own tests, which need
neither Dungeondraft nor a map:

```bash
.venv/bin/python -m unittest discover -s server/tests
```

To *see* something get built (a small furnished room left on the map, rather
than a self-cleaning test), run `.venv/bin/python server/demo_build.py`.

### 4. Wire up your MCP client

Register the entrypoint by its **absolute path**. For **Claude Code**:

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

These env vars configure the **Python server** (set them where the MCP client
launches it). The defaults match the mod, so you only need them if you change
the mod's `HOST` / `PORT` consts in `mcp_bridge.gd` — both halves must agree.

| Env var | Default | Purpose |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | host the server connects to |
| `DD_BRIDGE_PORT` | `8787` | port the server connects to (match the mod's `PORT`) |

## Extending

Adding a capability is symmetric — one handler on each side:

1. **Mod** (`mod/.../scripts/tools/mcp_bridge.gd`): add a `case` to the `match`
   in `_safe_dispatch()` and a `_my_command(req)` function returning
   `_ok(...)` / `_err(...)`.
2. **Server** (`server/dungeondraft_mcp/server.py`): add an `@mcp.tool()` that
   calls `bridge.request("my_command", ...)`.

A capability that is only computation on what the mod already reports lives in
the server alone: the layout checks (`spatial.py`), `scatter_objects` and
`batch` have no handler of their own in the mod.

Good next targets: deleting a level, grouping a batch of edits into a single
undo step, and footprints that follow a sprite's outline instead of its
bounding box.

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first: it
lists what a complete change includes and how to test it.

### Dev loop (read this before iterating on the mod)

A few things will bite you if you don't know them up front:

- **Editing the mod and the server are different reload paths.** GDScript
  changes in `mcp_bridge.gd` take effect when you **reload the mod inside
  Dungeondraft** (or restart DD). New or changed `@mcp.tool()` definitions in
  `server.py` only appear after the **MCP server process restarts** — in Claude
  Code that means `/mcp` → reconnect `dungeondraft` (or restart the client).
  The install is editable (`pip install -e`), so you never reinstall; you just
  cycle the process. Adding a *new tool* needs **both** reloads (mod for the
  handler, server for the tool registration); changing an *existing* handler's
  behavior needs only the mod reload.

- **Probe Dungeondraft before writing a handler.** `native_describe` lists a
  live object's methods and properties, `native_get` reads them and
  `native_call` tries a method, all without a mod reload, so you can find out
  what an API really does first. Inside a handler there is still no debugger:
  when a DD call behaves unexpectedly (e.g. a setter that doesn't stick),
  **return intermediate state in the response** — stash before/after values in
  a debug field — so one reload shows where a value changes.

- **A script that doesn't compile crashes Dungeondraft** when the mod loads,
  with no message. Compile-check `mcp_bridge.gd` offline first; PROTOCOL.md
  ("Mod compile errors crash Dungeondraft") has the recipe.

## Layout

```
mod/dungeondraft-mcp-bridge/   the Dungeondraft mod (copy into DD's mods folder)
  mcp_bridge.ddmod             manifest
  scripts/tools/mcp_bridge.gd  TCP server + command handlers
server/                        the Python MCP server
  dungeondraft_mcp/server.py   MCP tool definitions
  dungeondraft_mcp/spatial.py  layout checks (pure geometry on a map snapshot)
  dungeondraft_mcp/layout.py   furniture arrangement and shared walls
  dungeondraft_mcp/floorplan.py  room reachability from walls and doors
  dungeondraft_mcp/scene.py    bare ground and light-source checks
  dungeondraft_mcp/mapfile.py  .dungeondraft_map reader and pack manifest editing
  dungeondraft_mcp/packs.py    asset pack index, tags and search (offline)
  dungeondraft_mcp/uvtt_import.py  Universal VTT export parser and rebuild plan
  dungeondraft_mcp/bridge_client.py  TCP/JSON client
  tests/                       offline unit tests (no Dungeondraft needed)
  test_bridge.py               standalone per-command smoke test
  test_features.py             live validator for the 0.3.0 tools
  demo_build.py                builds a visible sample room and leaves it
tools/check_gdscript.py        offline compile check of the mod (stock Godot 3.4.2)
.github/workflows/ci.yml       unit tests and the compile check on every push
PROTOCOL.md                    wire protocol + implementation notes
```

## Credits

Built against the [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/).
Engine is Godot 3.4.2, so the GDScript uses Godot 3 networking class names.

Expanded terrain slots, wall editing, image import, water, materials, the
level and layer tools, layout checks, native/UI access, light and text editing,
scatter and batch were first built by Li-Jiaquan in
[dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge),
a fork of this project, and were ported back from there.

Whole-request undo and undoable deletes, checkpoints, the arrangement tool and
shared walls, the floorplan and scene checks, asset pack listing, search and
preparation, the preview sheets, map-file inspection, the Universal VTT import,
prefabs and the Map Wizard generator, map style, Dungeondraft's own Range for
lights and the door default, the bridge token and the pause panel, map resize
and crop, and Custom Snap Mod support were first built in
[battlemap-mcp](https://github.com/thekannen/battlemap-mcp) by
[@thekannen](https://github.com/thekannen), a rebuild of this project, and were
re-derived here against Dungeondraft's own code.

## License

[MIT](LICENSE).
