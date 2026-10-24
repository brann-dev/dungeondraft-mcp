# Bridge wire protocol

A deliberately tiny protocol so both halves stay trivial to extend.

- **Transport:** TCP on `127.0.0.1:8787` (override the mod's `PORT` const and the
  server's `DD_BRIDGE_PORT` env var together).
- **Framing:** one JSON object per line, terminated by `\n`, UTF-8.
- **Model:** request/response. The MCP server opens a connection, writes one
  request line, reads one response line. The mod also tolerates multiple
  requests on a persistent connection.

## Request

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
| `ping` | — | `{ pong, protocol, engine }` |
| `get_status` | — | `{ map_open, level_id, level_count, map_size_woxels, map_center, counts{}, active_tool }` |
| `list_asset_categories` | — | `{ categories[] }` |
| `list_assets` | `category="Objects"`, `search=""`, `limit=100` | `{ category, total, matched, returned, assets[] }` |
| `list_elements` | `kind="objects"`, `limit=200` | `{ kind, count, elements[] }` |
| `get_element` | `id` | element descriptor |
| `list_levels` | — | `{ current_index, levels[] }` |

`kind` ∈ `objects, walls, lights, paths, portals, roofs, texts`. An element
descriptor is `{ id, kind, position[x,y], rotation(deg), scale, asset? }`.

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
| `place_object` | `asset`, `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0`, `color?` → `{ id, position, color?, warning? }` |
| `draw_wall` | `points[[x,y]...]`, `asset?`, `loop=false`, `shadow=true`, `type=0`, `joint=1`, `color?` |
| `draw_path` | `points[[x,y]...]`, `asset`, `layer=0`, `sorting=0`, `smoothness?`, `width?` |
| `add_light` | `x?`, `y?`, `color?`, `energy=1`, `range=1`, `shadows=true`, `asset?` |
| `add_portal` | `asset`, `x?`, `y?`, `closed=false`, `radius=64`, `mount="wall"`, `snap_max=256`, `flip=false`, `fallback_free=true`, `rotation=0` |
| `add_roof` | `points[[x,y]...]` (the RIDGE line), `asset`, `width=256`, `type=0`, `sorting=0` |
| `add_text` | `text`, `x?`, `y?`, `size?`, `color?`, `font?` |
| `place_pattern` | `asset`, `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `category="Patterns"`, `color?`, `rotation?` |
| `build_room` | `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `wall_asset?`, `floor="pattern"\|"terrain"\|"none"`, `floor_asset?`, `floor_category="Simple Tiles"`, `floor_color?`, `floor_slot=1`, `wall_type=0`, `wall_joint=1` → `{ wall_id, floor_id?, points }` |

### Terrain

`fill_region` rasterizes a woxel-space rectangle or polygon into a texture-space
alpha brush and stamps it once via `Terrain.Paint`, so it fills only inside the
shape (e.g. one room's floor) rather than the whole level like `fill_terrain`.
It is undo-recorded like the other terrain ops.

| cmd | params |
| --- | --- |
| `set_terrain_slot` | `asset`, `slot=0` |
| `fill_terrain` | `slot=0`, `asset?` |
| `fill_region` | `rect=[x,y,w,h]` **or** `points=[[x,y]...]`, `slot=0`, `asset?`, `rate=1` |
| `paint_terrain` | `slot=0`, `x?`, `y?`, `radius=64`, `rate=1`, `asset?` |
| `paint_path` | `points=[[x,y]...]`, `slot=0`, `radius=96`, `rate=1`, `asset?` |
| `dig_cave` | `points=[[x,y]...]` **or** `x?`,`y?`, `radius=256`, `dig=true`, `ground_color?`, `wall_color?`, `texture?` |
| `clear_caves` | — (wipes the whole cave layer back to rock) |

### Modify / delete / levels / selection

| cmd | params |
| --- | --- |
| `move_element` | `id`, `x`, `y` |
| `modify_object` | `id`, `scale?`, `rotation?`, `color?`, `shadow?` → descriptor (+ `warning?`) |
| `duplicate_object` | `id`, `dx=64`, `dy=0` → `{ id }` |
| `delete_element` | `id` → `{ deleted }` |
| `add_level` | `label="Level"` → `{ id, label }` |
| `set_level` | `index` |
| `select_elements` | `ids[]` |
| `clear_selection` | — |

### Map lifecycle

| cmd | params | result | errors |
| --- | --- | --- | --- |
| `get_current_map` | — | `{ map_open, path, dirty, saving }` | — |
| `save_map` | — | `{ saved: true, path }` | `"no map open"`, `"map has never been saved"`, `"save already in progress"` (checked in that order) |
| `open_map` | `path` | `{ opening: path }` | `"missing 'path'"`, `"file not found: <path>"` |

- `get_current_map`: `path` is the map file's full path exactly as DD holds it
  (a Windows path with backslashes), or `""` if the map has never been saved.
  `dirty` is always `null`, because DD exposes no unsaved-changes flag (see notes).
  `saving` is `true` while a save is still being written.
- `save_map` saves to the map's current file, just as clicking DD's Save button
  does. It never opens a Save As dialog: a map that has never been saved is
  refused instead. Save a new map once in DD by hand first.
- `open_map` **replies before the switch happens.** The mod validates `path`,
  sends `{ opening }`, then calls `ForceOpenMap` one frame later. While the new
  map loads, the bridge is briefly unreachable (connections fail), and then a
  fresh bridge instance on the same port takes over. Poll `get_current_map`
  (retrying connection errors) until `path` names the new map; this took about
  1.8 s in testing. **Unsaved changes to the current map are discarded silently,
  without a prompt.** Call `save_map` first to keep them. The bridge's undo/redo
  stacks reset with the new map.

### Capture

The caller passes the absolute `path` the mod should write the PNG to (the MCP
server uses a temp path and reads it back as an image).

| cmd | params | result |
| --- | --- | --- |
| `screenshot` | `path` | `{ path, width, height }` — window grab, synchronous |
| `export_map` | `path`, `ppi=40`, `grid?` | `{ path, ppi, async, grid, grid_was }` — full-map render on a background thread; poll `path` for the file, then `set_grid(grid_was)` to restore the editor grid |
| `set_grid` | `visible?` | `{ grid }` — show/hide the editor grid (omit `visible` to read it) |

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

`focus_element`/`fit_elements` use each element's **world-space bounding rect**:
`GlobalRect` for walls/paths, the Control rect for texts, a prop's `Rect`, or a
zero-size rect at the node position as a fallback. `focus_element` centers on
the rect's midpoint; `fit_elements` frames the union of all rects.

### History

The bridge keeps its **own** undo/redo stacks (it does not use Dungeondraft's
`History.CreateCustomRecord`, which is unreliable on 3.4.2 — see notes). Every
reversible edit (creates, `move_element`, `modify_object`, `fill_terrain`,
`fill_region`, `paint_terrain`, `paint_path`, `dig_cave`) pushes an op; a fresh
edit clears the redo stack. Terrain ops snapshot the splat image before/after;
`dig_cave` snapshots the cave `BitMap` the same way.

| cmd | params | result |
| --- | --- | --- |
| `undo` | — | `{ undone, kind?, undo_depth? }` / `{ undone: false, reason }` |
| `redo` | — | `{ redone, kind?, redo_depth? }` / `{ redone: false, reason }` |

`delete_element` is **not** reversible (it frees the node). Levels, selection
and capture commands are not recorded. This stack is independent of the user's
Ctrl+Z in Dungeondraft.

`color` accepts `"#rrggbb"` / `"rrggbb"` or `[r,g,b]` / `[r,g,b,a]` floats 0..1.
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
  frames, so the mod can't restore it itself — the Python tool calls
  `set_grid(grid_was)` after the file is written.
- **`export_map`** — `Exporter.Start(0, ppi, path)` writes asynchronously. If DD
  chunks a very large map into multiple files, the single-path read would need
  extending.
- **`undo` / `redo`** — bridge-managed stacks, not DD's history. DD 3.4.2's
  `History.CreateCustomRecord` does not record programmatic creates, invokes
  custom `undo()` inconsistently, and never round-trips `redo()`, so the bridge
  uses its own. Create ops detach/re-attach the node (remove_child / add_child,
  keeping a reference and a stable id); transform ops restore a property
  snapshot; terrain ops restore a cloned splat image.
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
- **`place_pattern`** — draws a tiled-floor shape via `PatternShapes.DrawRect` /
  `DrawPolygon`, then applies the texture with `PatternShape.SetOptions(tex,
  color, rotation)` (the draw call alone leaves the shape untextured). New shapes
  land in the tool's default "Layer 100" (z 100, above the Objects node at z 0),
  so to render the floor **below** objects we set `z_as_relative = false` and an
  absolute `z_index` (default `z` = -100, between FloorShapes at -200 and Objects
  at 0). Do **not** use `PatternShapeTool.SetLayer()` on an uncreated index — it
  hard-crashes the mod (GDScript has no try/catch; valid range undocumented).
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
    provides one.
- **Mod compile errors crash Dungeondraft** — DD compiles mod scripts when a
  map opens, and a GDScript **parse error** makes it crash natively, with no
  console output (only a Windows `.dmp`), instead of reporting the error. DD has
  no in-app mod reload either, so every `.gd` change costs a full restart.
  Example: `self is Node` in this script (which has no `extends`, so it is a
  Reference) is a parse error in 3.4.2: *A value of type "self" will never be
  an instance of "Node"*. Compile-check `.gd` changes offline before
  restarting DD. The stock Godot 3.4.2 binary can load the script in a scratch
  project with a stub `Global` autoload (an untyped stub needs `var x := <call
  on Global>` turned into `=`). Avoid static type assertions on `self` and on
  values from untyped globals.
