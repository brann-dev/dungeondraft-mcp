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

### Terrain

| cmd | params |
| --- | --- |
| `set_terrain_slot` | `asset`, `slot=0` |
| `fill_terrain` | `slot=0`, `asset?` |
| `paint_terrain` *(experimental)* | `slot=0`, `x?`, `y?`, `radius=64`, `rate=1` |

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

### History

The bridge keeps its **own** undo/redo stacks (it does not use Dungeondraft's
`History.CreateCustomRecord`, which is unreliable on 3.4.2 — see notes). Every
reversible edit (creates, `move_element`, `modify_object`, `fill_terrain`,
`paint_terrain`) pushes an op; a fresh edit clears the redo stack.

| cmd | params | result |
| --- | --- | --- |
| `undo` | — | `{ undone, kind?, undo_depth? }` / `{ undone: false, reason }` |
| `redo` | — | `{ redone, kind?, redo_depth? }` / `{ redone: false, reason }` |

`delete_element` is **not** reversible (it frees the node). Levels, selection
and capture commands are not recorded. This stack is independent of the user's
Ctrl+Z in Dungeondraft.

`color` accepts `"#rrggbb"` / `"rrggbb"` or `[r,g,b]` / `[r,g,b,a]` floats 0..1.
`type` for walls: 0=auto,1=manual,2=cave; for roofs: 0=gable,1=hip,2=dormer.

## Known unknowns (verify before relying on)

- **TCP allowed at all?** CONFIRMED working on Dungeondraft / Godot 3.4.2 — the
  modding sandbox permits `TCP_Server`. (If a future DD/Godot version ever locks
  this down, the fallback is a file-watch transport: the mod polls a request file
  via `File` in `update()` and writes a response file.)
- **Element ids** — every element is referenced by `node_id`, force-assigned via
  `Global.World.AssignNodeID(node)` on create/list so it resolves immediately
  with `GetNodeByID` / `DeleteNodeByID`.
- **`export_map`** — `Exporter.Start(0, ppi, path)` writes asynchronously; if
  DD chunks very large maps into multiple files the single-path read may need
  adjusting. Verify on big maps.
- **`undo` / `redo`** — bridge-managed stacks, not DD's history. DD 3.4.2's
  `History.CreateCustomRecord` does not natively record programmatic creates,
  invokes custom `undo()` inconsistently, and never round-trips `redo()`, so it
  was abandoned. Create ops detach/re-attach the node (remove_child / add_child,
  keeping a reference and a stable id); transform ops restore a property
  snapshot; terrain ops restore a cloned splat image.
- **`paint_terrain`** — brush footprint and `Paint(...)` offset/position
  semantics are inferred; `fill_terrain` / `set_terrain_slot` are well-defined.
- **`add_portal`** — defaults to **wall-mounted** (`Wall.AddPortal`): snaps to
  the nearest wall within `snap_max` woxels, faces along that wall segment, and
  the wall remakes its lines so the portal cuts a gap (matching manual door
  placement). `radius` is the door half-width (≈128 = a 1-tile door, ≈256 = a
  2-tile door). Pass `mount:"free"` for a freestanding portal
  (`Level.CreateFreestandingPortal`); when `mount:"wall"` and no wall is near it
  falls back to freestanding unless `fallback_free:false`. `flip` reverses the
  facing. The response `kind` is `"wall_portal"` when mounted, `"portal"` when
  freestanding.
