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

### Create (each returns `{ id, ... }`)

| cmd | params |
| --- | --- |
| `place_object` | `asset`, `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0`, `color?` |
| `draw_wall` | `points[[x,y]...]`, `asset?`, `loop=false`, `shadow=true`, `type=0`, `joint=1`, `color?` |
| `draw_path` | `points[[x,y]...]`, `asset`, `layer=0`, `sorting=0`, `smoothness?`, `width?` |
| `add_light` | `x?`, `y?`, `color?`, `energy=1`, `range=1`, `shadows=true`, `asset?` |
| `add_portal` | `asset`, `x?`, `y?`, `closed=false`, `radius=64`, `rotation=0` |
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
- **`paint_terrain`** — brush footprint and `Paint(...)` offset/position
  semantics are inferred; `fill_terrain` / `set_terrain_slot` are well-defined.
- **`add_portal`** — only freestanding portals (`Level.CreateFreestandingPortal`);
  wall-mounted portals (`Wall.AddPortal`) aren't exposed yet.
