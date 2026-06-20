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
Text elements describe as `{ id, kind:"text", position[x,y], text, size?,
color?, font? }` (a DD Text extends LineEdit, so it has no `scale`/`rotation`/
`asset` and is positioned by `rect_position`). Font name+size are applied
together via `SetFont` because `SetFontSize` alone doesn't stick on a fresh
node; default color is black.

### Create (each returns `{ id, ... }`)

| cmd | params |
| --- | --- |
| `place_object` | `asset`, `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0`, `color?` |
| `draw_wall` | `points[[x,y]...]`, `asset?`, `loop=false`, `shadow=true`, `type=0`, `joint=1`, `color?` |
| `draw_path` | `points[[x,y]...]`, `asset`, `layer=0`, `sorting=0`, `smoothness?`, `width?` |
| `add_light` | `x?`, `y?`, `color?`, `energy=1`, `range=1`, `shadows=true`, `asset?` |
| `add_portal` | `asset`, `x?`, `y?`, `closed=false`, `radius=64`, `mount="wall"`, `snap_max=256`, `flip=false`, `fallback_free=true`, `rotation=0` |
| `add_roof` | `points[[x,y]...]`, `asset`, `width=256`, `type=0`, `sorting=0` |
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

### Modify / delete / levels / selection

| cmd | params |
| --- | --- |
| `move_element` | `id`, `x`, `y` |
| `modify_object` | `id`, `scale?`, `rotation?`, `color?`, `shadow?` |
| `duplicate_object` | `id`, `dx=64`, `dy=0` → `{ id }` |
| `delete_element` | `id` → `{ deleted }` |
| `add_level` | `label="Level"` → `{ id, label }` |
| `set_level` | `index` |
| `select_elements` | `ids[]` |
| `clear_selection` | — |

### Capture

The caller passes the absolute `path` the mod should write the PNG to (the MCP
server uses a temp path and reads it back as an image).

| cmd | params | result |
| --- | --- | --- |
| `screenshot` | `path` | `{ path, width, height }` — window grab, synchronous |
| `export_map` | `path`, `ppi=40` | `{ path, ppi, async }` — full-map render on a background thread; poll `path` for the file |

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
`fill_region`, `paint_terrain`) pushes an op; a fresh edit clears the redo
stack.

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
- **Element ids** — every element is referenced by `node_id`, force-assigned via
  `Global.World.AssignNodeID(node)` on create/list so it resolves immediately
  with `GetNodeByID` / `DeleteNodeByID`.
- **`export_map`** — `Exporter.Start(0, ppi, path)` writes asynchronously. If DD
  chunks a very large map into multiple files, the single-path read would need
  extending.
- **`undo` / `redo`** — bridge-managed stacks, not DD's history. DD 3.4.2's
  `History.CreateCustomRecord` does not record programmatic creates, invokes
  custom `undo()` inconsistently, and never round-trips `redo()`, so the bridge
  uses its own. Create ops detach/re-attach the node (remove_child / add_child,
  keeping a reference and a stable id); transform ops restore a property
  snapshot; terrain ops restore a cloned splat image.
- **`paint_terrain` / `fill_region`** — both edit the splat weight image
  directly (`CloneSplatImage` → raise the target slot's channel per pixel →
  `RestoreSplat`) rather than `Terrain.Paint(...)`, which does not modify the
  splat from a mod context. `paint_terrain` uses a soft radial falloff (a
  brush); `fill_region` is a hard rect/polygon. Both are undoable.
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
  an explicit `color` to override.
- **`build_room`** — convenience composite: draws a looped wall **and** a floor
  along the **same** boundary path (so the floor meets the wall with no gap, like
  the UI's combined trace), by calling `draw_wall` + `place_pattern`/`fill_region`
  internally. `floor` = `"pattern"` / `"terrain"` / `"none"`.
- **`add_portal`** — defaults to **wall-mounted** (`Wall.AddPortal`): snaps to
  the nearest wall within `snap_max` woxels, faces along that wall segment, and
  the wall remakes its lines so the portal cuts a gap (matching manual door
  placement). `radius` is the door half-width (≈128 = a 1-tile door, ≈256 = a
  2-tile door). Pass `mount:"free"` for a freestanding portal
  (`Level.CreateFreestandingPortal`); when `mount:"wall"` and no wall is near it
  falls back to freestanding unless `fallback_free:false`. `flip` reverses the
  facing. The response `kind` is `"wall_portal"` when mounted, `"portal"` when
  freestanding.
