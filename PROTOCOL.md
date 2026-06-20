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

| cmd | params | result |
| --- | --- | --- |
| `ping` | — | `{ pong, protocol, engine }` (`engine` = `Engine.get_version_info()`) |
| `get_status` | — | `{ map_open, level_id, level_count, map_size_woxels, object_count, active_tool }` |
| `list_assets` | `category="Objects"`, `limit=100` | `{ category, total, returned, assets[] }` |
| `place_object` | `asset`, `x?`, `y?`, `scale=1`, `rotation=0`, `sorting=0` | `{ placed, position, node_id }` |
| `draw_wall` *(experimental)* | `points[[x,y]...]`, `asset?`, `loop=false`, `shadow=true`, `type=0`, `joint=1` | `{ drawn, point_count }` |

Coordinates are **woxel** (world pixel) space; map center is
`map_size_woxels * 0.5`.

## Known unknowns (verify before relying on)

- **TCP allowed at all?** CONFIRMED working on Dungeondraft / Godot 3.4.2 — the
  modding sandbox permits `TCP_Server`. (If a future DD/Godot version ever locks
  this down, the fallback is a file-watch transport: the mod polls a request file
  via `File` in `update()` and writes a response file.)
- **`draw_wall`** — wall texture category (`"Walls"`) and the coordinate space
  for `AddWall` are inferred from examples, not documented. Check visually.
- **Terrain / paths** — not implemented yet; `Terrain.Paint` needs a brush
  `Image` and a `terrainID` whose valid range is undocumented.
