# Bridge wire protocol

A deliberately tiny protocol so both halves stay trivial to extend.

- **Transport:** TCP on `127.0.0.1:8787` (override the mod's `PORT` const and the
  server's `DD_BRIDGE_PORT` env var together).
- **Framing:** one JSON object per line, terminated by `\n`, UTF-8.
- **Model:** request/response. The MCP server opens a connection, writes one
  request line, reads one response line. The mod also tolerates multiple
  requests on a persistent connection.

## Token and pause

Every request except `ping` must carry `"token": "<64 hex chars>"`. The mod
writes a random token to Dungeondraft's user folder on first start
(`user://mcp_bridge_token`, the folder that holds `config.ini`), and the
Python server reads that file for every request (`DD_BRIDGE_TOKEN_FILE` and
`DD_BRIDGE_TOKEN` override it). On POSIX the mod runs `chmod 600` on the file
as it writes it, and the server re-applies 0600 whenever it reads a looser
mode; Godot's `File` cannot set permissions itself. A request without the right token gets `{ ok: false, error: "bad
token: ..." }` or `"this bridge requires a token ..."`. The user can turn the
requirement off ("Require token") or write a new token ("New token") in the
**MCP Bridge** panel under Settings; the choice persists in
`user://mcp_bridge_settings.json`. It is a bearer token over localhost: a
program that cannot read the user's files cannot drive the map (another user,
a sandboxed app, a web page talking to localhost). It is not encryption.

The same panel has **Pause AI edits**. While paused, only read-only commands
run (`READ_ONLY_CMDS` in the mod: ping, status, listings, the terrain and
spatial read-backs, screenshot and export, camera, selection, `native_get` /
`native_describe` / `native_targets`, `ui_tree`); everything else is refused
with `error: "paused: ..."`. `ping` and `get_status` report `paused`; `ping`
also reports `auth` ("token" / "off") and `authenticated`. The pause persists
across restarts until the user lifts it. **Undo the assistant's last step**
in the panel pops the bridge's own undo stack. The panel and its controls are
unreachable through `native_*` and `ui_action`.

## Request

Any request may carry `"snap": true`: the mod then snaps its `x`/`y`, every
entry of `points` and a `rect` (origin and far corner; the size is kept when
snapping would collapse it) to the user's grid before the handler runs
(`_snap_request`): the Custom Snap Mod's `get_snapped_position` when that mod
is loaded, else Dungeondraft's grid at the step its own tool for that kind
uses: half a cell for objects, paths, portals, lights and texts, a whole cell
for walls, floors, patterns and roofs unless the half-grid-snap preference is
on. The server snaps `place_object`, `move_element`, `import_image` and
`build_room` itself first (`get_snap_settings` with `for`), so a placement
check judges the snapped spot.

```json
{ "cmd": "place_object", "id": 7, "asset": "objects/chair.png", "x": 100, "y": 200 }
```

- `cmd` (string, required) — the command name.
- `id` (any, optional) — echoed back verbatim in the response for correlation.
- everything else — command-specific parameters.

## Response

Success:

```json
{ "ok": true, "result": { "placed": true, "position": [100, 200], "node_id": 42 }, "id": 7 }
```

Error:

```json
{ "ok": false, "error": "no map open", "id": 7 }
```

## Commands

Coordinates are **woxel** (world pixel) space; map center is in
`get_status -> map_center`. Every element is referenced by integer `id`
(== Dungeondraft `node_id`); create commands return it, query commands list it.

### Read / query

| cmd | params | result |
| --- | --- | --- |
| `ping` | — (no token needed) → `{ pong: true, protocol, engine, auth: "token"|"off", authenticated, paused }` |
| `get_status` | — | `{ map_open, level_id, level_index, level_count, map_size_woxels, map_center, terrain_slots (4 or 8), terrain_visible, counts{}, active_tool }`, `paused` |
| `list_asset_categories` | — | `{ categories[] }` |
| `list_assets` | `category="Objects"`, `search=""`, `limit=100` | `{ category, total, matched, returned, assets[] }` |
| `list_elements` | `kind="objects"`, `limit=200` | `{ kind, count, elements[] }` |
| `get_element` | `id` | element descriptor |
| `list_levels` | — | `{ current_index, current_id, levels[{ index, id, label }] }`, top floor first |
| `list_asset_packs` | — → `{ installed: [{id, name, version, author, keywords?, allow_3rd_party_mapping_software_to_read?}], included_in_this_map: [{id, name, version, author}], installed_but_not_in_this_map, uses_default_assets }`. Installed = the folders under `res://packs` (every mounted pack); included = `Header.AssetManifest`, the only packs whose assets this map can use. |
| `terrain_samples` | `samples=32` (2–64 per side), `rect?[x,y,w,h]` → `{ level_id, map_size, rect, samples_per_side, slots: [texture path or null per slot], smooth_blending, samples: [{ x, y, weights: [slot0..slot3 (+4..7)], covered }] }`. Weights are read straight from the splat image(s); `covered` is true under a pattern floor (`PatternShapes.GetShapes()` polygons) or a dug cave cell (the `CaveMesh` BitMap, cell = floor(woxel/64)+1). Floor-tool shapes are Clipper paths with no accessor and do not count. |
| `list_layers` | — | `{ level_id, layers[{ layer, label, locked }] }`, bottom to top |

`kind` ∈ `objects, walls, lights, paths, portals, roofs, texts`. An element
descriptor is `{ id, kind, position[x,y], rotation(deg), scale, asset?, layer? }`
(`layer` for objects, paths and pattern floors, the kinds that have one). An
object adds `mirror` and `block_light` (and `scale` is positive even when it is
mirrored), a light `color`, `energy`, `range`, `shadows`.
A level's `index` is its place in the list and changes when levels are added,
cloned or reordered; its `id` stays put while the map is open (DD hands ids out
afresh on load). `get_status.level_id` is the id (it used to report the index
under that name; `level_index` is the index).
`get_element` on a wall (and `modify_wall`) adds its geometry: `points[[x,y]...]`,
`loop`, `type`, `joint`, `portal_count`. `list_elements` leaves these out.

`portals` includes **wall-mounted** portals (doors/windows), which live inside
each wall's `Portals` array rather than `level.Portals` — `list_elements` walks
the walls to surface them. A wall portal describes as `{ id, kind:"wall_portal",
wall_id, position[x,y], normal[x,y], facing(deg), radius, closed, asset? }`
where `position` is the door's gap center and `normal` is the outward unit
vector (perpendicular to the wall, pointing out of the room). To route a road to
a door, draw to `position`; offset along `normal` by a tile or so to stop just
outside the threshold. `get_element` on a door id returns the same descriptor.
Text elements describe as `{ id, kind:"text", position[x,y], text, size?,
color?, font? }` (a DD Text extends LineEdit, so it has no `scale`/`rotation`/
`asset` and is positioned by `rect_position`). Font name+size are applied
together via `SetFont` because `SetFontSize` alone doesn't stick on a fresh
node; default color is black.

### Create (each returns `{ id, ... }`)

| cmd | params |
| --- | --- |
| `place_object` | `asset`, `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0`, `color?`, `layer=100` → `{ id, position, color?, warning? }` |
| `draw_wall` | `points[[x,y]...]`, `asset?`, `loop=false`, `shadow=true`, `type=0`, `joint=1`, `color?` |
| `draw_path` | `points[[x,y]...]`, `asset`, `layer=100`, `sorting=0`, `smoothness?`, `width?` |
| `add_light` | `x?`, `y?`, `color?`, `energy=1`, `range=2.0`, `shadows=true`, `asset?` |

`range` on `add_light`, `modify_light` and in descriptors is Dungeondraft's own Range, the number the Light tool shows and the saved map and Universal VTT export carry: `texture_scale * texture_width / 512` (twice the texture scale for the built-in 1024 px light textures). The mod converts (`_light_range` / `_light_scale`).
| `add_portal` | `asset`, `x?`, `y?`, `closed=false`, `radius=64`, `mount="wall"`, `snap_max=256`, `flip=false`, `fallback_free=true`, `rotation=0` |
| `add_roof` | `points[[x,y]...]` (the RIDGE line), `asset`, `width=256`, `type=0`, `sorting=0` |
| `add_text` | `text`, `x?`, `y?`, `size?`, `color?`, `font?` |
| `place_pattern` | `asset`, `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `category="Patterns"`, `color?`, `rotation?` |
| `build_room` | `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `wall_asset?`, `floor="pattern"\|"terrain"\|"none"`, `floor_asset?`, `floor_category="Simple Tiles"`, `floor_color?`, `floor_slot=1`, `wall_type=0`, `wall_joint=1` → `{ wall_id, floor_id?, points }` |
| `import_image` | `path` (absolute; `.png` / `.jpg` / `.jpeg` / `.webp`), `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0`, `shadow=true`, `layer=100` → `{ id, position, image_size[w,h], asset }`. The new element is an ordinary object whose `asset` is `embedded://<path>` |

### Terrain

`fill_region` rasterizes a woxel-space rectangle or polygon into the terrain's
splat weight image, so it fills only inside the shape (e.g. one room's floor)
rather than the whole level like `fill_terrain`. Like `paint_terrain` and
`paint_path` it edits the weights directly (`Terrain.Paint` does nothing from a
mod; see the notes below). It is undo-recorded like the other terrain ops.

| cmd | params |
| --- | --- |
| `set_terrain_slot` | `asset`, `slot=0`, `expand=false` → `{ slot, terrain_slots }` |
| `fill_terrain` | `slot=0`, `asset?` |
| `fill_region` | `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `slot=0`, `asset?`, `rate=1` |
| `paint_terrain` | `slot=0`, `x?`, `y?`, `radius=64`, `rate=1`, `asset?` |
| `paint_path` | `points=[[x,y]...]`, `slot=0`, `radius=96`, `rate=1`, `asset?` |
| `dig_cave` | `points=[[x,y]...]` **or** `x?`,`y?`, `radius=256`, `dig=true`, `ground_color?`, `wall_color?`, `texture?` |
| `clear_caves` | — (wipes the whole cave layer back to rock) |

`slot` is 0-3, or 0-7 on a level with expanded terrain slots
(`get_status.terrain_slots` is 8). A slot of 4-7 on a 4-slot level is refused;
`set_terrain_slot` with `expand: true` and a slot of 4-7 turns the level's
expanded slots on first (they stay on). Painting or filling a slot of 4-7 that
has no texture yet needs `asset`.

### Water / materials

Both take a shape in woxels: `rect=[x,y,w,h]` **or** `points=[[x,y]...]` (a
polygon, >= 3 points, with some area).

| cmd | params |
| --- | --- |
| `draw_water` | shape, `erase=false`, `deep_color?`, `shallow_color?`, `blend_distance?` (tiles, 0 < d <= 4) → `{ erase, point_count, deep_color?, shallow_color?, blend_distance? }` |
| `draw_material` | `asset` (a `Materials` asset), shape, `layer=-400`, `smooth=true`, `erase=false` → `{ erase, layer, asset, cells, cell_size }` |

### Modify / delete / levels / selection

| cmd | params |
| --- | --- |
| `move_element` | `id`, `x`, `y` |
| `modify_object` | `id`, `scale?`, `rotation?`, `color?`, `shadow?`, `mirror?`, `block_light?` → descriptor (+ `warning?`) |
| `modify_light` | `id`, `color?`, `energy?`, `range?`, `shadows?`, `rotation?`, `asset?` → light descriptor |
| `modify_text` | `id`, `text?`, `size?`, `color?`, `font?` → text descriptor |
| `modify_wall` | `id`, `points?[[x,y]...]`, `loop?`, `type?`, `joint?`, `asset?`, `color?`, `shadow?` → wall descriptor with geometry |
| `set_element_layer` | `id`, `layer` → `{ id, layer, previous }` (objects, paths and pattern floors) |
| `duplicate_object` | `id`, `dx=64`, `dy=0` → `{ id }` |
| `delete_element` | `id` → `{ deleted, id, undoable: true }` (reversible, see History) |
| `delete_elements` | `ids[]` (≤100, all checked first) → `{ deleted, ids, undoable: true }`; one undo step |
| `add_level` | `label="Level"`, `terrain=false` → `{ id, label, index: 0, current_index, terrain_visible }` |
| `set_level` | `index` → `{ current_index, current_id }` |
| `rename_level` | `index`, `label` → level list |
| `clone_level` | `index`, `label?` → `{ id, label, index: 0, current_index }` |
| `reorder_levels` | `ids[]` (every level id once, top floor first) → level list |
| `compare_levels` | `index=-1` (-1 = off), `reference_opacity=0.35`, `current_opacity=1` → `{ comparing, ... }` |
| `delete_level` | `index` → level list + `{ deleted_id, deleted_label }`; refuses the last level; **not** undoable |
| `set_map_size` | `width`, `height` (tiles, 8–128) → `{ width_tiles, height_tiles, previous_tiles, changed, map_size_woxels, steps, history_cleared }`. Drives the Change Map Size window (`Master.ChangeMapSize`): right and bottom edges only, every level's splat read back, the bridge's history cleared. **Not** undoable. |
| `add_layer` | `layer` (z, -4096..4096), `label?` → layer list |
| `configure_level` | `ambient_light?`, `lighting?`, `terrain_visible?`, `smooth_terrain?`, `water_shoreline?` → `{ level_id, ambient_light, lighting, terrain_visible, smooth_terrain, water_shoreline }` (no params = just read) |
| `set_trace_image` | `path` (empty = remove), `x?`, `y?`, `scale=1`, `opacity=0.5` → `{ trace_image, path?, position?, scale?, opacity?, image_size? }` |
| `get_map_style` | — → `{ building_wear, building_wear_texture, building_wear_options: [{index, name, texture}], grid_style, grid_texture, grid_style_options }` (the Map Settings tool's wear list and `MapSettings.GRID_TEXTURES`) |
| `get_snap_settings` | `points?[[x,y]...]` (≤200), `for?` (a command name: snap as that tool would) → `{ vanilla_snapping, half_grid_snap, cell_size, mod_loaded, mod?: { enabled, geometry, interval, offset, radius_to_corner, preset? }, snapped?, note }` (the Custom Snap Mod's `snappy_mod` tool instance when loaded; else half a cell, or a whole cell for `draw_wall` / `modify_wall` / `build_room` / `place_pattern` / `add_roof` unless `Preferences.HalfGridSnap`) |
| `preview_assets` | `assets[]` (≤32), `category="Objects"`, `columns=4` (1–8), `cell_px=192` (64–512), `true_scale=false` → `{ path, columns, rows, cell_px, count, sizes[], missing[], largest_px, true_scale }`. Renders a contact sheet with Godot's Image (each texture's `get_data()`, resized and blended into a cell) to `user://mcp_bridge_preview.png`; `path` is that file's absolute path. |
| `set_map_style` | `building_wear?` (option name or index), `grid_style?` (dashes / dotted / narrow_line / thick_line) → as `get_map_style`; applied through `MapSettings.SetBuildingWear` / `SetGridStyle`, undoable (`map_style` op snapshots `World.BuildingWear` and the grid texture) |
| `list_prefabs` | `set?` → `{ sets, set, prefabs }` (the Prefab tool's Set dropdown and List; the tool is enabled for the read and disabled again unless it was active) |
| `place_prefab` | `name`, `set?`, `x?`, `y?` → `{ prefab, ids, count, centre, unmoved?, note? }`. Selects the list item (`PrefabTool.OnSelectPrefab` deserialises the prefab into the level), shifts the movable pieces (props, lights, free portals, texts) so their centre lands on x,y, then `Confirm()`. One `group` undo op of the created nodes. |
| `generator_options` | — → `{ design, designs, dials: { boundary, complexity, density: {value, min, max, step} } }` (the Generator tool's `Design` and Range controls) |
| `generate_dungeon` | `design?` (Dungeon / Cave), `boundary?`, `complexity?`, `density?` → `{ design, dials, counts_before, counts_after, level_id }`; runs `Generator.Generate()` on the current level. **Not** undoable. |
| `select_elements` | `ids[]` |
| `clear_selection` | — |

### Map lifecycle

| cmd | params | result | errors |
| --- | --- | --- | --- |
| `get_current_map` | — | `{ map_open, path, dirty, saving }` | — |
| `save_map` | — | `{ saved: true, path, saving }`. `saved` means a save was **requested**, not that it finished | `"no map open"`, `"map has never been saved"`, `"save already in progress"` (checked in that order) |
| `open_map` | `path` | `{ opening: path }` | `"missing 'path'"`, `"file not found: <path>"`, `"save in progress"` |

- `get_current_map`: `path` is the map file's full path exactly as DD holds it
  (a Windows path with backslashes), or `""` if the map has never been saved.
  `dirty` is always `null`, because DD exposes no unsaved-changes flag (see notes).
  `saving` is `true` while a save is still being written.
- `save_map` saves to the map's current file, just as clicking DD's Save button
  does. It never opens a Save As dialog: a map that has never been saved is
  refused instead. Save a new map once in DD by hand first. **`saved: true` does
  not confirm a write.** It means the Save button's handler was called. The save
  can still be running, and a save that fails inside DD still replies
  `saved: true`. The save is asynchronous, and `IsSaving` is only set about a
  frame after the handler runs. So the reply's `saving` (read straight after the
  call) can be `false` while the save is pending. In testing, the reply said
  `saving: false` and an immediate `get_current_map` said `saving: true`.
  Don't treat `saving: false` in the reply as "done". To confirm the save, poll
  `get_current_map` until `saving` is `false` **and** the file's modification
  time has moved past its value before the save.
- `open_map` **replies before the switch happens.** The mod validates `path`,
  refuses with `"save in progress"` while `Master.IsSaving` is true (switching
  would cut the save off and lose the work), sends `{ opening }`, then calls
  `ForceOpenMap` on a later frame. If a save starts in between, the deferred
  open waits for it to finish. If the save is still running after 30 s, the
  open is abandoned and logged, and the current map stays open, so a caller
  polling for the new map times out instead of losing data. While the new
  map loads, the bridge is briefly unreachable (connections fail), and then a
  fresh bridge instance on the same port takes over. Poll `get_current_map`
  (retrying connection errors) until `path` names the new map; this took about
  1.8 s in testing. **Unsaved changes to the current map are discarded silently,
  without a prompt.** Call `save_map` first to keep them. The bridge's undo/redo
  stacks reset with the new map.

### Layout facts

The layout checks (does this table cross a wall, block a door, overlap the
bed?) run in the **server** (`dungeondraft_mcp/spatial.py`); the mod only
reports geometry and keeps the regions.

| cmd | params | result |
| --- | --- | --- |
| `spatial_snapshot` | `asset?` + `category="Objects"`, or `path?` (an image file) | `{ level_id, map_size[w,h], objects[], unreadable[ids], walls[], portals[], regions[], footprint? }` |
| `asset_footprint` | `asset` + `category="Objects"`, or `path` | `{ asset, size[w,h], visible[x,y,w,h], local[4 x [x,y]] }` |
| `list_spatial_regions` | — | `{ level_id, regions[{ name, kind, points }] }` |
| `set_spatial_region` | `name`, `kind="clearance"` (`room` / `clearance` / `protected`), `rect` **or** `points` | region list |
| `remove_spatial_region` | `name` | region list |

A snapshot object is `{ id, asset, layer, position, scale[sx,sy], rotation(deg),
local[4], polygon[4] }`: `polygon` is its footprint now, in woxels, and `local`
the same four corners in the object's own space (world = position +
rotate(scale * local)), so the server can work out the footprint after a move,
turn or resize. A wall is `{ id, points (woxels), loop, half_width }`, a portal
`{ id, wall_id, asset, position, direction (unit, along the wall), radius,
window }`. `footprint` / `asset_footprint` describe a new object at scale 1:
`visible` is the box around its opaque pixels in image pixels, `local` that
box's corners around the sprite's center.

### Native access

An escape hatch to Dungeondraft's own objects and UI, for what no command
covers. `target` is a root (`World`, `Editor`, `Level` = the current level,
`Camera`, `Exporter`, `Header`), `Tool:<name>`, `Window:<name>`, `Element:<id>`,
`Level:<id>` or `Object:<instance id>`, optionally followed by `.Member` steps
(`Level.Terrain`, `Tool:TerrainBrush.Controls.SmoothBlending`). Values are
plain JSON where possible; other Godot types are `{ "$type", "value" }`
(`Vector2`, `Rect2`, `Color`, ...) and objects `{ "$target": "Object:<id>",
"class" }`, which can be passed back as a target or an argument. None of
this is undoable.

| cmd | params | result |
| --- | --- | --- |
| `native_targets` | — | `{ targets[] }`: the roots, every tool, every window |
| `native_describe` | `target="Level"`, `search?` | `{ target, class, method_count, methods[{ name, args[{ name, type }], returns? }], property_count, properties[{ name, type, settable }] }` (lists capped at 200) |
| `native_get` | `target`, `properties[]` | `{ target, values{} }`; an unknown name reads as `null` |
| `native_set` | `target`, `property`, `value` | `{ target, property, value }` with the value held afterwards |
| `native_call` | `target`, `method`, `args=[]` | `{ target, method, value }` |
| `ui_tree` | `target="Editor"`, `search?`, `limit=200` | `{ target, matched, returned, controls[] }` |
| `ui_action` | `target` (a control), `action` (`press` / `set` / `show` / `hide`), `value?` | the control's new state |

`native_call` / `native_set` convert plain JSON to the declared type (`[x, y]`
→ Vector2, `[[x,y]...]` → Vector2Array, `[x,y,w,h]` → Rect2, `"#rrggbb"` →
Color, whole numbers → int). Other arguments: `{ "$target": "<target>" }` for
an object, `{ "$type": "Texture", "category", "asset" }` for an asset texture,
`{ "$type": "Image", "path" }` for an image file. A `ui_tree` control is
`{ target, class, visible, control?, text?, tooltip?, disabled?, pressed?,
value/min/max/step?, selected?, items?, selected_items?, color? }`.
`ui_action` `set` takes `true`/`false` for a toggle, a number for a slider or
spin box, a string for a text field, an item index or its exact text for an
option or item list, a color for a color picker.

### Capture

The caller passes the absolute `path` the mod should write the PNG to (the MCP
server uses a temp path and reads it back as an image).

| cmd | params | result |
| --- | --- | --- |
| `screenshot` | `path` | `{ path, width, height }` — window grab, synchronous |
| `export_map` | `path`, `ppi=40`, `grid?`, `format="png"` (`png` / `jpg` / `webp` / `uvtt`), `quality?` (1-100, jpg/webp) | `{ path, ppi, format, async, grid, grid_was }` — full-map render on a background thread; poll `path` for the file. The mod restores the grid, camera and level staging itself when DD finishes (`export_finish` is the fallback). `uvtt` writes a Universal VTT `.dd2vtt` (base64 image + walls / portals / lights) for Foundry or Roll20 import |
| `set_grid` | `visible?` | `{ grid }` — show/hide the editor grid (omit `visible` to read it) |
| `export_finish` | — | `{ restored, exporting }` — un-stage the export level and restore camera + grid. The mod does this itself when DD reports the export done; send it after the file is complete (or on timeout) as the fallback for a run that died inside DD |

### Camera

The editor camera is a `Camera2D` at `Global.Camera`. Its world center is the
inherited `global_position`; `zoom` is a Camera2D factor where **larger = zoomed
out** (0.5 magnifies 2×, 2.0 shows twice as much). `set_camera`/`focus_element`/
`fit_elements` pan by writing `global_position` and keep DD's bottom-bar zoom
dropdown in sync via `SetZoomOptionByRaw`. Use these before `screenshot` to
frame what you want to inspect.

| cmd | params | result |
| --- | --- | --- |
| `get_camera` | — | `{ position[x,y], zoom, viewport_size[w,h] }` |
| `set_camera` | `x?`, `y?`, `zoom?` | camera state |
| `focus_element` | `id`, `zoom?` | `{ id, focused[x,y], camera{} }` — centers on the element (any kind) |
| `fit_elements` | `ids[]`, `pad=0.15` | `{ fit, missing[], center[x,y], bounds[[minx,miny],[maxx,maxy]], camera{} }` — frames the union of the ids' world bounds |
| `reset_camera` | — | camera state — default anchor, map center, zoom 1.0; rescues a lost view |

`focus_element`/`fit_elements` use each element's **world-space bounding rect**:
`GlobalRect` for walls/paths, the Control rect for texts, a prop's `Rect`, or a
zero-size rect at the node position as a fallback. `focus_element` centers on
the rect's midpoint; `fit_elements` frames the union of all rects.

### History

The bridge keeps its **own** undo/redo stacks (it does not use Dungeondraft's
`History.CreateCustomRecord`, which is unreliable on 3.4.2 — see notes). Every
reversible edit (creates including `import_image`, `move_element`,
`modify_object`, `modify_wall`, `modify_light`, `modify_text`,
`set_element_layer`, `fill_terrain`, `fill_region`, `paint_terrain`,
`paint_path`, `dig_cave`, `clear_caves`, `draw_water`, `draw_material`,
`place_pattern`, `delete_element`, `delete_elements`) pushes an op; a fresh
edit clears the redo stack. Terrain ops snapshot the splat images
before/after; `dig_cave` and `draw_material` snapshot their `BitMap` the same
way, `draw_water` the water mesh's saved state, `modify_wall` the wall's points,
texture, tint and flags. A deleted object is serialised (`Prop.Save`) and
rebuilt by `LoadObject` on undo with its id; any other deleted node is detached
from the tree and held by the op. History keeps the last 200 ops; past that
the oldest is dropped (and freed, if it held a detached node).

| cmd | params | result |
| --- | --- | --- |
| `undo` | — | `{ undone, kind?, steps?, label?, undo_depth? }` / `{ undone: false, reason }` |
| `redo` | — | `{ redone, kind?, steps?, label?, redo_depth? }` / `{ redone: false, reason }` |
| `begin_undo_group` | `label?`, `undo_group` (token) | `{ open_groups, label }` |
| `end_undo_group` | `undo_group` (token) | `{ steps, open_groups, undo_depth }` |
| `checkpoint` | `label` (≤64 chars; reusing a label moves it) | `{ label, undo_depth, history_limit }` |
| `list_checkpoints` | — | `{ checkpoints: [{ label, steps_since, reachable, age_ms }] }` |
| `rollback_checkpoint` | `label` | `{ label, rolled_back, kinds[], undo_depth, redo_depth }`; refuses if history no longer reaches the mark |

**Undo groups.** Between `begin_undo_group` and `end_undo_group`, every
recorded op joins the group instead of the stack; the group is pushed as one
op of kind `group` (undone newest entry first, redone in order), or as the
lone entry when only one edit was recorded, or not at all when none was. The
server opens a group around `batch`, `scatter_objects`, `place_objects` and
`move_elements`, and the mod around `build_room`'s wall and floor, so each is
one undo step. Every request made inside a group carries the group's token as
`undo_group`; a request without the open group's token closes the group first
(its owner stopped talking), so a server that dies mid-batch cannot leave later
edits swallowed into a stale group. `undo`, `redo` and the checkpoint commands
close any open group before they run.

**Checkpoints** remember the serial number of the newest op at the time; a
rollback undoes while the top op is newer than that. Ops pushed get increasing
serials (a redo gets a fresh one), and `_lost_serial` records the newest op
dropped from history: a checkpoint older than it is reported `reachable:
false` and refused.

Not recorded: `delete_level`, `set_terrain_slot`, levels, selection, capture
and native/UI commands. This stack is independent of the user's Ctrl+Z in
Dungeondraft.

`color` accepts `"#rrggbb"` / `"#rrggbbaa"` (the `#` is optional) or `[r,g,b]`
/ `[r,g,b,a]` floats 0..1. Colors in results use the same form, with the alpha
pair only when the color is not opaque.
`type` for walls: 0=auto,1=manual,2=cave; for roofs: 0=gable,1=hip,2=dormer.

## Implementation notes

- **Transport** — the mod opens a `TCP_Server` inside Dungeondraft; the modding
  sandbox on Godot 3.4.2 permits it. If a future DD/Godot version locks this
  down, the fallback is a file-watch transport: the mod polls a request file via
  `File` in `update()` and writes a response file.
- **Listener lifecycle** — DD calls `start()` on a new script instance every
  time a map is opened/created (and on mod reload), with no teardown hook, so the
  previous instance's `TCP_Server` keeps the port bound and a fresh `listen()`
  fails ("port in use"). The listener is therefore process-global: kept in
  `Engine` meta (`mcp_bridge_server`) and **adopted** by each new instance, which
  also claims `mcp_bridge_owner`; older instances see they no longer own it and
  stop polling in `update()`. Undo/redo stacks are per instance, so they reset
  with each map.
- **Registering created nodes like DD does** — DD's own tools/loaders set a
  `"preview" = false` meta on every object, path and light, and add each object
  to `Objects.AddToSearchTable` (the `QuickSearchBuckets` index). The bare
  `CreateObject` / `CreatePath` / `CreateLight` calls skip both, so the bridge
  does them itself. Without the meta, `Pathways.Save()` / `Lights.Save()` throw
  on a hard `(bool)GetMeta("preview")` cast — the save aborts with no file
  written and DD stays "busy" (`Master.IsSaving` stuck) until restart — and
  `Objects.Save()` silently skips the object. Without the search-table entry,
  `SelectTool` (which hit-tests props only through that index) can never select
  the object. (`Prop._ExitTree` removes itself from the index, so deletes need
  no extra step.)
- **Undo/redo of a created object** — props are **not** detached/re-attached
  like other kinds: `Prop._EnterTree` builds a fresh, empty `Sprite` on every
  tree entry, so a re-added prop loses its texture (renders via the stale old
  sprite, but is unselectable and saves without a texture). Like DD's
  `ObjectRecord`, undo snapshots it with `Prop.Save()` and deletes it; redo
  rebuilds it with `Objects.LoadObject(data)` (same id, meta and index entry).
- **Object custom color** — only textures tagged **`Colorable`** (the red-mask
  assets) take a color, and `Prop.SetCustomColor` only sets the shader's
  `tint_r` if the Sprite **already has the `CustomColors` ShaderMaterial** —
  `ObjectTool.PromoteCustomColor` / `Prop.Load` install it first; a bare
  `CreateObject()` prop has none, so the call silently did nothing and the raw red
  mask rendered. The bridge installs a duplicate of
  `res://materials/CustomColors.material`, then calls `SetCustomColor`, then
  clears the texture flags (as `Prop.Load` does). `Master.Tags` is static C# and
  unreachable from GDScript, so the Colorable set is rebuilt from the same tag
  files (`res://data/default.dungeondraft_tags`, each
  `res://packs/<id>/data/default.dungeondraft_tags`, the user override file),
  plus DD's "path contains `colorable`" rule. `place_object` on a Colorable asset
  with no `color` uses ObjectTool's default palette color **`#6b3834`**
  (`Cache.DefaultCustomColors[0]`) and reports the resolved `color`; a `color` on
  a non-Colorable asset is ignored with a `warning` (as DD's tools do).
  `duplicate_object` copies the source's color. Object descriptors include
  `colorable` and, when set, `color`. The color is saved as `custom_color` and
  survives undo/redo and save/reload.
- **Element ids** — every element is referenced by `node_id`, force-assigned via
  `Global.World.AssignNodeID(node)` on create/list so it resolves immediately
  with `GetNodeByID` / `DeleteNodeByID`.
- **Export grid** — `Exporter` has **no grid option**: it renders what the editor
  shows, and the Export window's Grid checkbox just calls `Editor.ToggleGrid`.
  `export_map` with `grid` toggles it before `Start`; the render spans later
  frames, so the mod restores it from `update()` once `Master.IsExporting`
  drops (see "Export level staging"), and `export_finish` is the fallback.
- **`export_map`** — `Exporter.Start(mode, ppi, path)` writes asynchronously. If
  DD chunks a very large map into multiple files, the single-path read would need
  extending.
- **Export formats** — `Exporter.Start` modes (modding API reference): 0 PNG,
  1 JPEG, 2 WEBP, 3 Universal VTT, 4 Mod. `Exporter.Quality` (1-100) applies to
  JPEG/WEBP. `Start` uses `path` verbatim, so the caller supplies the `.dd2vtt`
  extension. The Universal VTT JSON holds `resolution` (`map_size` in cells,
  `pixels_per_grid` = ppi), `line_of_sight` (wall polylines),
  `objects_line_of_sight`, `portals`, `lights` and a base64 `image`; the Python
  `export_uvtt` tool parses it and returns the counts.
- **Export level staging** — `ExportWindow` calls
  `World.SetSourceLevel(CurrentLevelId)` and `SetOverlayLevel(selected - 1,
  opacity)` when it opens and `World.RestoreLevelsPostExport()` when it closes.
  The Exporter's VTT helpers (`ExportLOS` / `ExportPortals` / `ExportLights` /
  `SaveEnvironment`) read `World.SourceLevel`, so a bare `Exporter.Start` with
  it null throws inside `Run`, a C# `async Task` that `Start` doesn't await: the
  exception is swallowed, the intermediate `<path>.png` is left behind with no
  `.dd2vtt`, and Run's own camera/viewport restore never runs (view left at the
  export anchor and zoom, `Master.IsExporting` stuck true). The bridge stages
  the level before `Start`, polls `Master.IsExporting` (static, but readable via
  the Master node like `IsSaving`) in `update()` and restores levels, grid and a
  camera snapshot when it drops. `export_finish` is the explicit fallback and
  also clears a stuck `IsExporting`; `reset_camera` rescues a view left off-map.
- **`undo` / `redo`** — bridge-managed stacks, not DD's history. DD 3.4.2's
  `History.CreateCustomRecord` does not record programmatic creates, invokes
  custom `undo()` inconsistently, and never round-trips `redo()`, so the bridge
  uses its own. Create ops detach/re-attach the node (remove_child / add_child,
  keeping a reference and a stable id); transform ops restore a property
  snapshot; terrain ops restore the cloned splat images. Handlers that know
  their own before/after state (`modify_wall`, `draw_water`, `draw_material`)
  return the op next to their result (`_ok_op`); `_record_and_dispatch` strips
  it from the response and pushes it.
- **`paint_terrain` / `fill_region` / `paint_path`** — all edit the splat weight
  image directly (`CloneSplatImage` → raise the target slot's channel per pixel →
  `RestoreSplat`) rather than `Terrain.Paint(...)`, which does not modify the
  splat from a mod context. `paint_terrain` uses a soft radial falloff (a single
  circular brush); `fill_region` is a hard rect/polygon. `paint_path` rasterizes
  a continuous stroke along a polyline by measuring each pixel's distance to the
  nearest segment and applying the same smoothstep falloff once — a uniform-width
  ribbon for roads/trails, with no gaps or double-painted overlaps (the reason
  stamping many `paint_terrain` dabs reads as dotty). All three are undoable.
- **terrain splat** — `splatImage` is RGBA = weights of slots 0–3; `splatImage2`
  = slots 4–7. `set_terrain_slot` / `SetTexture(tex, slot)` swap a slot's texture
  **globally** (everywhere that slot is weighted), so to texture one region you
  assign the slot once, then raise that slot's weight only inside the region
  (what `fill_region` / `paint_terrain` do).
- **Expanded terrain slots (4-7)** — `splatImage2` exists only once
  `Terrain.ExpandSlots(true)` has run (the Terrain Brush's "Expand Slots"
  toggle; saved as `expand_slots`). On a 4-slot level `SetTexture(tex, 4..7)` is
  silently ignored, `Fill(4..7)` fills slot `n - 4` instead, and
  `CloneSplatImage2()` dereferences a null image, so the bridge refuses those
  slots up front. **`RestoreSplat2(splat, splat2)` takes both images**, as DD's
  `PaintTerrain2` record passes them; `RestoreSplat(splat)` is the 4-slot call.
  With expanded slots the bridge clones both images, moves the painted slot's
  weight toward 1 while scaling the other seven down, and restores both; undo
  snapshots both too. `set_terrain_slot(expand: true)` goes through
  `TerrainBrush.ExpandSlots` when the brush's panel has been built (its slot
  list grows to 8 and DD gives the new slots its default textures), otherwise
  straight through `Terrain.ExpandSlots`, and the panel syncs when next opened.
  The bridge never turns expanded slots off.
- **`place_pattern`** — draws a tiled-floor shape via `PatternShapes.DrawRect` /
  `DrawPolygon`, then applies the texture with `PatternShape.SetOptions(tex,
  color, rotation)` (the draw call alone leaves the shape untextured). New shapes
  land in the tool's active layer, by default "Layer 100" (z 100, above the
  Objects node at z 0), so a floor would cover furniture. The bridge moves the
  shape with **`PatternShape.SetLayer(z)`** (default `z` = -100, DD's own
  "below objects" layer): `PatternShapes` keeps one `Node2D` per layer
  (`GetOrMakeLayer` creates it with `ZIndex = layer`), the layer is what
  `Save()`/`Load()` persist, and it stays relative to the level. An earlier
  absolute-`z_index` hack (`z_as_relative = false`) looked right in the editor
  but reloaded floors came back on layer 100 and the export staging (level at
  z -2000) drew them over everything. Still do **not** call
  `PatternShapeTool.SetLayer(index)` — that takes a *menu index* into the
  tool's LayerMenu and hard-crashes on an invalid one.
- **default tints** — `draw_wall` / `place_pattern` apply the texture's own
  default color when no `color` is given (walls via `WallTool.GetWallColor(tex)`,
  patterns by reading back `PatternShapeTool.Color` after setting the texture),
  rather than `Color(1,1,1)` (white) — which renders bleached/washed-out. Pass
  an explicit `color` to override. **`place_pattern` color caveat:** unlike walls,
  patterns have **no per-texture default** lookup — `PatternShapeTool.Color` is
  *persistent tool state that leaks across calls* (it holds whatever the previous
  `place_pattern` set). Reading it back as a "default" made every no-color call
  inherit the prior call's tint, and if it was ever left transparent (alpha ≈ 0)
  every subsequent floor rendered **invisible**. So `place_pattern` never trusts
  `tool.Color`: an explicit `color` is used verbatim; with no color it applies a
  deterministic opaque neutral tint (`DEFAULT_PATTERN_TINT`) and reports
  `used_default_tint: true` + the resolved `color`. Each call is independent.
  Pass an explicit `color` for an exact per-asset look.
- **`build_room`** — convenience composite: draws a looped wall **and** a floor
  along the **same** boundary path (so the floor meets the wall with no gap, like
  the UI's combined trace), by calling `draw_wall` + `place_pattern`/`fill_region`
  internally. `floor` = `"pattern"` / `"terrain"` / `"none"`.
- **`add_roof`** — `points` is the roof's **RIDGE line** (the peak), **not** a
  footprint to trace. `Roof.Set(points, width, type)` builds a complete,
  self-closing roof that slopes down `width` woxels perpendicular to each side of
  the ridge, with hips/gables off the ridge ends. The covered footprint = the
  ridge's bounding box expanded by `width` on every side. So **2 points already
  make a full, clean roof** (a short ridge → a near-pyramid hip). To roof a
  building W×H: ridge along the **long** axis, centered, `width` = half the
  **short** dimension; make the ridge ~`width` shorter than the long wall at each
  end so the end hips land on the short walls. (An earlier version wrongly treated
  `points` as a footprint and closed the loop — that duplicated a vertex and
  malformed the seam corner; removed. The single-`Set` ridge model has no seam.)
  `type`: 0=gable, 1=hip, 2=dormer. Roofs have **no color/tint** (the tileset
  renders at its natural color — there is no `SetColor`, unlike walls/patterns)
  and sit at z 800 (above everything). It is a create (returns an `id`), so
  `undo` reverses it.
- **`dig_cave`** — caves are a separate layer: a `CaveMesh` (a `MeshInstance2D`,
  z -300) whose open/rock state is a Godot **`BitMap`** (one bit per cell;
  `true` = open cave floor). The bridge enables the `CaveBrush` tool (the UI
  path, which wires the mesh to the level), fetches the BitMap via
  `get_Bitmap()`, sets bits along the requested woxel path (rasterized as a
  constant-width ribbon, like `paint_path`), then `SetBitmap` +
  `FinalizeMeshAndBorders` + `UpdateMesh` rebuilds the floor, the rocky wall
  border and debris — the same result as hand-painting with the Cave Brush.
  Cell mapping: `cell = floor(woxel / CellSize) + MapEdgeBuffer` (CellSize is 64
  woxels = ¼ tile). `dig=false` fills cave back to rock. `ground_color` /
  `wall_color` tint the **whole** cave system (DD applies one color globally, as
  the UI does), `texture` sets the cave floor from the `Caves` asset bank. Note:
  `SetCircle` and the `IsEmpty` flag on the mesh proved unreliable from a mod
  context (an internal grid the BitMap doesn't reflect) — editing the BitMap
  directly is the path that renders. Undo-recorded (snapshots the BitMap before
  and after, like the terrain ops). `clear_caves` wipes the whole cave layer back
  to rock in one call (native `CaveMesh.Clear()`, or zero the BitMap as a
  fallback, then rebuild) — also undoable.
- **`modify_wall`** — `Wall.Set(...)` is the only call that replaces a wall's
  shape wholesale, and its `Clear()` frees every mounted portal;
  `Wall.ModifyPoint` (the UI's vertex drag) deletes the portals on the two
  segments next to the vertex. So a wall with doors/windows is reshaped in
  place: the bridge assigns `Points`, then for each portal keeps its fraction
  along its segment (`WallPointIndex`), moves it there, turns `rotation` and
  `Direction` by the segment's change of angle, and re-keys it with
  `Wall.MovePortal(portal, index)` (what `SelectTool` does when a door is
  dragged along its wall), then `RemakeLines()`. `Direction` is the unflipped
  wall tangent (`Begin`/`End` = position ∓ `Direction * Radius`, which
  `RemakeLines` uses to cut the gap); a flipped portal only differs by π in
  `rotation`, so turning both by the same delta keeps either kind right. That
  path needs the same point count / loop / type / joint, and each portal must
  still fit its segment; otherwise the command is refused before anything
  changes. A wall without portals goes through `Set` + `RemakeLines` (`Set`
  alone leaves the old shadow lines behind), so it can change point count, loop,
  type and joint. A new `asset` without `color` takes the texture's default
  tint, as `draw_wall` does.
- **`import_image`** — calls `ObjectTool.EmbedObject(path)`, the handler behind
  dropping an image file on DD's window. It loads the file, registers it in
  `World.EmbeddedTextures` **keyed by the path** (saved under the map's
  `embedded` section as a base64 PNG, whatever the source format), and creates
  a recorded prop whose texture is `embedded://<path>`. So the same path
  imported twice shares one stored copy, a changed file at an already-embedded
  path is not re-read, and the map file contains the source path. `EmbedObject`
  dereferences the loaded texture unchecked, so the bridge decodes the file
  itself first and refuses what Godot can't read. The prop is created at the
  cursor on the Object tool's active layer with the tool's shadow setting and is
  indexed for selection under that layer; the bridge gives it the requested
  `layer`, transform and `shadow`, and re-indexes it
  (`RemoveFromSearchTable` / `AddToSearchTable`). Undo/redo use the prop path
  (`Prop.Save` / `Objects.LoadObject`), which resolves `embedded://` textures.
- **`draw_water`** — `WaterMesh.DrawPolygon(points, invert)` is the Water
  Brush's shape mode: it unions the polygon into the level's water (or
  subtracts it), rebuilds the mesh and shoreline, and records DD's own history
  entry. It is an `async void` but only waits while a user stroke is in flight,
  so from the bridge it completes before returning. Colors are per body of
  water: on rebuild, a polygon whose outline is unchanged keeps its colors and
  any new or reshaped one takes the mesh's current `DeepColor` / `ShallowColor`
  / `BlendDistance` (the Water Brush's pickers). The bridge sets those for the
  one call and restores them, so a draw that merges into an existing lake
  recolors the whole merged body. `BlendDistance` is in tiles; the mesh builder
  divides by it and has gradient rings up to 4. Undo stores `WaterMesh.Save()`
  before/after and restores with `Load()`. A level that never had water saves
  no `tree`, and `Load` ignores a state without one, so that "before" is stored
  as the "after" tree with its `children` emptied.
- **`draw_material`** — materials are `MaterialMesh` nodes, one per (layer,
  texture), from `Level.GetOrMakeMaterialMesh(layer, texture, smooth)`; the
  Material Brush uses the same lookup, so bridge and UI edit the same mesh. Like
  the cave layer they are marching-squares meshes over a `BitMap`, but the bits
  are grid **corners** `CellSize` apart (128 woxels) at woxel
  `((ix - MapEdgeBuffer) * CellSize, ...)`, and a set corner fills out to the
  midpoint toward each unset neighbour. The bridge sets every corner inside or
  on the shape, so the patch covers the shape and can run up to half a cell (64
  woxels) past it, and a shape containing no corner is refused. `SetBitmap` +
  `FinalizeMeshAndBorders` rebuild fill and border. `layer` must be one of the
  level's own layers (`Level.SaveLayers()`: by default -400 "Below Ground", -100
  "Below Water", 100-400 "User Layer 1-4", 700 "Above Walls", 900 "Above
  Roofs"); -400 is the Material Brush's default and draws over the terrain,
  under objects. `smooth` applies when the mesh is created and whenever passed.
- **`add_portal`** — defaults to **wall-mounted** (`Wall.AddPortal`): snaps to
  the nearest wall within `snap_max` woxels, faces along that wall segment, and
  the wall remakes its lines so the portal cuts a gap (matching manual door
  placement). `radius` is the door half-width (≈128 = a 1-tile door, ≈256 = a
  2-tile door). Pass `mount:"free"` for a freestanding portal
  (`Level.CreateFreestandingPortal`); when `mount:"wall"` and no wall is near it
  falls back to freestanding unless `fallback_free:false`. `flip` reverses the
  facing. The response `kind` is `"wall_portal"` when mounted, `"portal"` when
  freestanding.
- **Map lifecycle (`get_current_map` / `save_map` / `open_map`)** — findings
  from probing DD's API on 3.4.2:
  - `Global.Editor` (a `CanvasLayer`, `res://scenes/Editor.tscn`) has
    `CurrentMapFile`, a String holding the full Windows path, or empty if the
    map was never saved. Its parent is the **Master** node (`/root/Master`,
    `res://scenes/Master.tscn`), which has `IsSaving` (bool). The bridge reads it
    as `Global.Editor.get_parent().get("IsSaving")`. `Global.get("Master")` and
    `Editor.get("Master")` do **not** reach it.
  - Neither Editor nor World has any dirty / unsaved / modified property, and
    the window title shows no unsaved marker either. So `dirty` is reported as
    `null`.
  - `Editor.ForceOpenMap(file)` switches maps **without any prompt** and
    silently discards unsaved changes, even with freshly placed objects. The
    other open-related methods on Editor are `OpenMap()` (no arguments; the UI
    file picker), `OnOpenedOrSaved(path)` and `UpdateOpenRecent`.
  - **`Editor.SaveMap` takes two arguments: `SaveMap(bool saveAs, action)`.** A
    one-argument call `SaveMap(false)` returns without writing anything, even
    on a modified map. Most likely the C# binding rejects the wrong argument
    count, and GDScript never sees an error. `ExecuteAfterSave(action)` and `Master.Save(path, onFinish)`
    (plus `Master.SaveTags()`) also exist. The bridge calls
    `Editor._on_SaveButton_pressed()`, the Save button's own handler. It writes
    the file with no dialog, popup or toast, as long as `CurrentMapFile` is set,
    which `save_map` checks first.
  - `open_map` defers `ForceOpenMap` with `call_deferred` plus
    `yield(Global.World.get_tree(), "idle_frame")`, so the reply is sent before
    the map is torn down. The bridge script has no `extends` (it is a
    `Reference`), so it has no `get_tree()` of its own; `Global.World` (a Node)
    provides one. Saving is asynchronous (`Master.Save` takes an `onFinish`
    callback), so `open_map` checks `IsSaving` both when the request arrives and
    on every frame before `ForceOpenMap` (giving up after 30 s, measured with
    `OS.get_ticks_msec()`). A save that throws leaves `IsSaving` stuck (see the
    note on registering created nodes), so in that case `open_map` refuses or
    abandons the switch instead of discarding the map.
- **Calling DD's C# from the mod** — three things hold for every call, found
  with `SaveMap`, `SetLevel` and a deliberately failing call:
  - **C# default arguments are not filled in.** A call must pass every
    declared parameter; with fewer, no method matches, nothing runs and GDScript
    sees no error. `World.SetLevel(int level, bool loading = false)` called with
    one argument was a silent no-op, so `set_level` never switched levels until
    it passed both.
  - **A C# exception does not reach GDScript.** The call returns `null`, the
    handler carries on and replies, and DD prints *Unhandled Exception* with the
    stack to its log (stdout). DD and the bridge keep running (checked on
    1.2.0.1 Linux), but the rest of that frame's `Editor._Process` is skipped
    and whatever the method was doing is left half done. So validate before
    calling: a `null` from a call that should return something is the only
    sign from inside.
  - **Private members are reachable** (methods, fields, property setters), and
    `get_method_list()` lists them like public ones. Generic containers cross
    only when Godot can marshal them: `List<T>` and `Dictionary<K,V>` do,
    `SortedDictionary` and `HashSet` read as `null`.
  - One more trap of the same kind: `Script.GetAssetTexture` throws (and so
    returns `null`) for an asset path it does not know. The bridge's "could not
    load ... asset" errors rely on that `null`; each one also leaves an
    exception in DD's log.
- **Colors** — Godot 3 parses an eight-digit color string as **AARRGGBB**
  (`Color("ff000080")` is opaque half-blue) and `Color.to_html(true)` writes the
  same order. The bridge's `_color` moves a trailing alpha pair to the front
  before parsing and `_hex` writes `#rrggbbaa`, so alpha is last on the wire in
  both directions.
- **Levels** — `World.levels` is ordered top floor first, and
  `World.CurrentLevelId` is an **index** into it despite the name; a level's
  identity is `Level.ID`, assigned when the level is created or the map loaded
  (so stable for a session, not across a reload). `World.CreateLevel(label)` and
  `CloneLevel(level, label)` insert at index 0 and keep the current level
  current, so the current index goes up by one. The bridge then does what
  `LevelSettings` does: `Editor.UpdateLevelOptions()` (the level dropdown) and,
  for a new level, hides its terrain (`Terrain.Visible = false`). A visible
  terrain is an opaque sheet, which would hide the level `compare_levels` shows
  beneath; `add_level` takes `terrain: true` for a level that wants ground. A
  clone is built from `Level.Save()` and fills in over the following frames,
  with fresh node ids. `reorder_levels` passes a GDScript array to
  `SetNewLevelOrder(List<Level>)`. `compare_levels` is
  `SetCompareLevels(reference, referenceOpacity, currentOpacity, true)` /
  `DisableCompareLevels()`; the setting is stored per current level and DD
  re-applies it in `SetLevel`. The bridge ticks `Editor.CompareToggle` with
  `set_pressed_no_signal` so the checkbox agrees without firing its handler.
- **Layers** — a layer is a z value. `Level.Layers` maps z to a label: seven
  locked ones DD's own nodes draw on (-500 Terrain, -300 Caves, -200 Floor,
  0 Water, 500 Portals, 600 Walls, 800 Roofs) and the user layers (-400, -100,
  100-400, 700, 900 by default). The table itself is a `SortedDictionary` and
  can't be read; `Level.SaveLayers()` returns the user layers and
  `LoadLayers({ z: label })` adds to them, which is how `add_layer` works (this
  DD version has no layer editor of its own; the table is saved with the map
  and the layer menus list it). An object's or path's layer is its `z_index`,
  a pattern floor's is its parent layer node (`SetLayer`). `set_element_layer`
  follows `SelectTool`: for an object, `Objects.RemoveFromSearchTable`, set
  `z_index`, `AddToSearchTable`, because the selection index is keyed by z.
  Within one z, the level's node order decides: Water, Patterns, Materials,
  Paths, Objects. The bridge creates objects and paths on **100**, the layer
  DD's Object, Path and Scatter tools start on, so what it places stacks with
  what is placed by hand (it used 0, the locked Water layer, until protocol
  27, and 0 is no longer accepted). Pattern floors stay on -100 and materials
  on -400. A light's `range` is its `texture_scale`; DD's Range slider is
  `texture_scale * texture width / 512`, twice that for the built-in 1024-px
  light textures.
- **Element and level settings** — a mirrored object is one with a negative x
  scale (`Prop.Mirror`), so `modify_object`'s `scale` keeps the sign and the
  transform snapshot already covers it; `SetBlockLight` builds light occluders
  from the sprite's silhouette. `modify_light` writes the `Light2D` directly
  (`color`, `energy`, `texture_scale`, `shadow_enabled`, `rotation`, `texture`),
  which is all `Lights.SaveLight` stores. `modify_text` goes through
  `Text.SetFont(name, size)`, which rebuilds the font and refits the label, and
  `SetFontColor`. The ambient light is the color of the level's
  `LightPassRender` rect (saved per level as `ambient_light`); the bridge also
  hands it to the Environment tool and its picker. The Lighting checkbox calls
  `Level.ToggleLighting` on every level, and so does the bridge:
  `Editor.ToggleLighting(true)` itself dereferences `World.SourceLevel`, which
  is null outside an export. The trace image is `World.AddTraceImage(path,
  scale, opacity)`, a sprite DD hides while exporting; the bridge decodes the
  file first because a file that fails to load leaves a texture-less sprite.
  `scatter_objects` and `batch` are not bridge commands: the server composes
  them from `place_object` and from its own tools.
- **Native access** — built on Godot's reflection of DD's C# objects.
  `get_method_list()` gives every C# method with argument names and Variant
  types (object arguments carry no class, and nothing marks a method private),
  including the `get_X` / `set_X` accessors of C# properties, from which
  `native_describe` derives the property list; plain C# fields are not listed
  anywhere but `get()` reads them. DD's methods are PascalCase and Godot's
  snake_case, and `native_call` only accepts the former, which keeps
  `free`, `call`, `set_script` and the rest of the engine out of reach.
  Argument counts are matched exactly (see above) and each argument is fitted
  to its declared type. Objects are handed out as `Object:<instance id>` and
  looked up again with `instance_from_id`, so nothing is stored in the mod; an
  object nothing else holds (a freshly cloned `Image`) is gone by the next
  call. `get_class()` returns the Godot base class, so the class name comes
  from the C# script's path when it has one. Tools (`Editor.Tools`) are plain
  objects, not nodes: each keeps its named controls in a `Controls` dictionary
  and its panel is `Editor.Toolset.GetToolPanel(name)`, which is what `ui_tree`
  walks for a `Tool:<name>` target. Control captions are translation keys until
  drawn, so `ui_tree` reports `tr(text)`. `ui_action` sets a control and emits
  the signal DD's handler is connected to (`toggled` comes from setting
  `pressed`; `item_selected`, `color_changed`, `text_changed` + `text_entered`
  are emitted by hand); a button's `pressed` is emitted deferred, so a handler
  that opens a dialog or swaps the map runs after the reply. Many tool
  handlers only act while their tool is the active one.
- **Layout checks** — a footprint is the rectangle around a sprite's opaque
  pixels (`Texture.get_data().get_used_rect()`, read once per texture and kept
  as meta on the shared texture object), mapped through `Sprite.get_rect()` and
  the prop's transform. DD's own object art is cropped tight, so trimming
  mostly matters for imported images. A wall's thickness is its texture's
  height (it is a `Line2D` of that width). A door is told from a window only by
  "window" in the asset's file name. Regions are stored as meta on the level
  node: DD's map format has no place for them, so they last for the session.
  Everything else is in the server: `spatial.py` clips each footprint against
  walls, door zones, other footprints and regions, and counts contact (within
  half a woxel, or an overlap under 16 square woxels) as touching, not as a
  conflict, so furniture can sit flush against a wall. Objects only collide
  with objects on the same layer. The placement check is opt-in per call
  (`check` / `region` on the server's object tools) because rectangles are
  conservative: a tree's box reaches well past its round canopy, and a chair
  tucked under a table overlaps it. `protected` regions are enforced without a
  flag, by the server's terrain, cave, water and material tools, with a margin
  for how far each bleeds past its shape (one terrain cell, the cave border,
  the material's quarter-tile overshoot). A client talking to the mod directly
  gets the facts but none of the checks.
- **Mod compile errors crash Dungeondraft** — DD compiles mod scripts when a
  map opens, and a GDScript **parse error** makes it crash natively, with no
  console output (only a Windows `.dmp`), instead of reporting the error. A
  broken script crashes DD the same way when it is loaded through the toolbar's
  **Reload Mods** button, or by launching `Dungeondraft.exe "<map path>"`
  (which opens that map directly with mods active; the bridge answered about
  12 s after launch, which makes it a quick restart-and-test loop).
  Example: `self is Node` in this script (which has no `extends`, so it is a
  Reference) is a parse error in 3.4.2: *A value of type "self" will never be
  an instance of "Node"*. Compile-check `.gd` changes offline before
  restarting DD, with the stock Godot 3.4.2 binary (the headless build is
  enough). DD compiles a mod script by prepending
  `var Global = {}\nvar Script=null\n\n` to the file and calling
  `GDScript.reload()`; do the same from a `-s` script (`GDScript.new()`, set
  `source_code`, `reload()` returns the error and prints the line, offset by 3).
  The stock binary is stricter than DD's release build, which skips the static
  type pass: `var x := <call on Global>` is reported as *the variable type
  can't be inferred* there but compiles in DD, so turn `:=` into `=` on such
  lines in the copy being checked. Avoid static type assertions on `self` and
  on values from untyped globals. A map switch (`open_map`) reuses the compiled
  script; only Reload Mods or a restart picks up an edited file.
