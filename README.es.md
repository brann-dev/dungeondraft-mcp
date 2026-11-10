# dungeondraft-mcp

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Français](README.fr.md) · Español · [Português](README.pt-BR.md) · [Русский](README.ru.md)

> Este documento es una traducción del [README.md](README.md) en inglés, que es la referencia en caso de diferencias. Si encuentras un error de traducción, abre una issue o un PR.

Un servidor MCP que permite a un LLM (Claude Code, Claude Desktop, etc.) manejar una **instancia de Dungeondraft en ejecución** (colocar objetos, dibujar muros, inspeccionar el mapa) a través de la API de modding en GDScript de Dungeondraft.

Dungeondraft no tiene un servidor integrado al que conectarse, así que este proyecto consta de **dos mitades** que se comunican por un socket local:

```
Claude / cliente MCP
      │  MCP (stdio)
      ▼
server/   ── servidor MCP en Python  (este repositorio)
      │  JSON delimitado por saltos de línea sobre TCP  (127.0.0.1:8787)
      ▼
mod/      ── mod de Dungeondraft  (GDScript, se ejecuta dentro de DD)
      │  llamadas a la API de modding
      ▼
   el mapa abierto  (Objects, Walls, Levels, …)
```

El mod abre un servidor TCP dentro de Dungeondraft y lo consulta en cada fotograma desde el hook `update(delta)`; el lado de Python expone cada capacidad como una herramienta MCP y reenvía las llamadas en JSON. Consulta [PROTOCOL.md](PROTOCOL.md) (en inglés) para el formato de los mensajes.

> **Estado: funciona.** Comprobado de extremo a extremo con Dungeondraft sobre **Godot 3.4.2**: el TCP directo funciona desde el sandbox de modding, sin necesidad de una alternativa. 103 herramientas de consulta, creación, disposición, modificación, terreno, agua, materiales, niveles, capas, comprobaciones de distribución y de escena, packs de assets, archivos de mapa, importación y exportación VTT, selección, captura, cámara, historial y ciclo de vida del mapa, además de un acceso de último recurso a la API y la interfaz propias de Dungeondraft (ver más abajo).

## Qué puede hacer la IA

- **Inspeccionar:** `ping` (también indica si el usuario ha pausado las ediciones de la IA), `get_status`, `list_levels`, `list_elements`, `get_element`, `list_asset_categories`, `list_assets` (con búsqueda por subcadena), `get_terrain` (lee el terreno pintado como una cuadrícula de pesos por ranura).
- **Packs de assets:** `preview_assets` (una hoja de contactos con hasta 32 assets candidatos, para mirar antes de colocar; `true_scale` muestra sus tamaños relativos), `list_asset_packs` (qué packs están instalados y cuáles puede usar de verdad este mapa: Dungeondraft asigna los packs a cada mapa, y un mapa creado sin ellos solo ve, sin avisar, los ~1800 assets básicos), `search_pack_contents` (busca dentro de todos los packs instalados, incluidos los que el mapa no tiene, por palabras y etiquetas, sin leer nunca las imágenes) y `prepare_map_with_packs` (escribe una copia del mapa que incluye más packs y la abre). `inspect_map_file` resume un `.dungeondraft_map` sin abrirlo: packs que necesita, recuentos por nivel, texturas más usadas, puertas cuyo indicador de cerrada contradice su nombre; `compare_to` compara dos archivos.
- **Crear:** `place_object`, `draw_wall`, `draw_path`, `add_light`, `add_portal`, `add_roof`, `add_text`, `place_pattern` (suelos con patrón: tablones, baldosas, ladrillo, dibujados bajo los objetos), `build_room` (un muro cerrado y su suelo a juego sobre un mismo trazado, como el trazado combinado de la interfaz), `import_image` (incrusta un PNG/JPG/WEBP del disco como objeto: utilería propia, ayudas de juego o un mapa de batalla entero como fondo; los píxeles se guardan en el mapa), `scatter_objects` (una arboleda o un campo de escombros en una sola llamada, sin tocar muros ni puertas), `place_objects` (muchos objetos en los puntos elegidos en una sola llamada), `arrange_objects` (una fila de estanterías pegada a un muro, sillas alrededor de una mesa, objetos sobre un escritorio, ajustados a lo que cada asset dibuja realmente), `list_prefabs` / `place_prefab` (los grupos guardados de Dungeondraft), `generate_dungeon` (la distribución de mazmorra o de cueva del asistente Map Wizard en una sola llamada), `set_trace_image` (una imagen de referencia mostrada sobre el mapa para calcarla). `build_room` reutiliza un muro que ya está sobre el contorno, de modo que dos habitaciones contiguas comparten un solo muro y una puerta entre ellas se abre en un VTT.
- **Terreno:** `set_terrain_slot`, `fill_terrain` (todo el nivel), `fill_region` (un rectángulo o un polígono, por ejemplo el suelo de una habitación), `paint_terrain` (un pincel suave), `paint_path` (un trazo continuo y suave a lo largo de una polilínea: caminos y senderos). Ranuras 0 a 3 por defecto; `set_terrain_slot(..., expand=True)` activa las ranuras 4 a 7 del nivel.
- **Agua y materiales:** `draw_water` (lagos, ríos y fosos como rectángulo o polígono, con orilla y degradado de profundidad; `erase` recorta islas) y `draw_material` (lava, hielo, adoquines y otras texturas del pincel de materiales sobre un rectángulo o un polígono, en la capa elegida).
- **Cuevas:** `dig_cave` excava cavernas y túneles a lo largo de un trazado (el pincel de cuevas: abre suelo en la roca, con paredes rocosas y escombros automáticos; `dig=false` vuelve a rellenar; tintes opcionales de suelo y pared), `clear_caves` borra toda la capa de cuevas.
- **Editar:** `move_element`, `move_elements` (varios con un mismo desplazamiento), `modify_object` (escala, rotación, color, sombra, espejo, bloqueo de luz), `modify_wall` (cambia la forma o el aspecto de un muro; sus puertas y ventanas siguen montadas y acompañan a su segmento), `modify_light`, `modify_text`, `duplicate_object`, `delete_element`, `delete_elements`, `select_elements`, `clear_selection`. `set_map_style` fija el desgaste de los edificios y el estilo de línea de la cuadrícula para todo el mapa; `set_map_size` cambia el tamaño del mapa en casillas, igual que el propio Change Map Size de Dungeondraft, y `crop_map` conserva una ventana de casillas recortando desde cualquier lado, con una vista previa de lo que se eliminaría antes de hacerlo. Las herramientas de colocación aceptan `snap=True` para ajustarse a la cuadrícula del usuario, incluida la cuadrícula hexagonal o isométrica del Custom Snap Mod de Lievven cuando ese mod está cargado; `get_snap_settings` lee esa configuración.
- **Niveles:** `add_level`, `set_level`, `rename_level`, `clone_level` (copia una planta entera para empezar la siguiente), `delete_level`, `reorder_levels`, `compare_levels` (muestra otra planta, tenue, bajo la actual para alinear las cosas), `configure_level` (luz ambiental para escenas de atardecer y de noche, iluminación activada o no, visibilidad y fundido del terreno, orilla del agua).
- **Capas:** `list_layers`, `set_element_layer` (decide qué tapa a qué: una alfombra bajo una mesa, la copa de un árbol sobre un sendero), `add_layer`; `place_object` e `import_image` también aceptan `layer`.
- **Comprobaciones de distribución:** `get_asset_footprint` (cuánto ocupa de verdad un asset, antes de elegir una escala), `check_object_placement` (probar un sitio: ¿atraviesa un muro, bloquea una puerta, se solapa con algo?), `validate_layout` (revisar un nivel ya amueblado), `validate_floorplan` (antes de amueblar: ¿se puede llegar a todas las habitaciones, da cada puerta a algún sitio, hay alguna entrada?), `validate_scene` (suelo desnudo sin nada pintado encima, y luces sin una fuente visible) y `set_spatial_region` / `remove_spatial_region` / `list_spatial_regions` para habitaciones, zonas que deben quedar despejadas y suelo protegido. Las herramientas de objetos aceptan `check=True` para rechazar de entrada una mala colocación; las ediciones de terreno, cuevas, agua y materiales se niegan a tocar una región `protected`.
- **Ciclo de vida del mapa:** `get_current_map` (ruta del mapa abierto y si hay un guardado en curso), `save_map` (guarda en el archivo actual, como el botón Save de DD) y `open_map` (cambia a otro mapa guardado; responde antes de que cargue el nuevo mapa, así que consulta `get_current_map`; los cambios sin guardar se descartan sin avisar, así que llama antes a `save_map`).
- **Ver:** `screenshot` (ventana actual) y `export_map` (render limpio del mapa completo, `grid=False` para exportar sin cuadrícula) devuelven imágenes, de modo que el modelo puede mirar su propio trabajo e iterar. `set_grid` muestra u oculta la cuadrícula del editor. `export_uvtt` escribe un `.dd2vtt` de Universal VTT (imagen + muros, puertas, luces) para importarlo en Foundry o Roll20, e `import_dd2vtt` reconstruye uno sobre el mapa abierto: muros vueltos a unir a través de cada hueco de puerta, puertas y ventanas, luces y luz ambiental, con la imagen de la exportación debajo como referencia para calcar.
- **Cámara:** `get_camera`, `set_camera`, `focus_element` (centra un elemento), `fit_elements` (encuadra un grupo), `reset_camera` (recupera una vista perdida): orienta la vista antes de un `screenshot` para inspeccionar un punto concreto, como una puerta abierta en un muro.
- **Trabaja a tu lado:** `get_selection` lee lo que tienes seleccionado en Dungeondraft (ids, tipos y los límites de la selección) y `get_cursor` dónde descansa el puntero del ratón sobre el mapa, así que puedes seleccionar unas cuantas cosas, o dejar el puntero en algún sitio, y decir «haz estos más grandes» o «pon un cofre aquí».
- **Acceso nativo:** `native_targets`, `native_describe`, `native_get`, `native_set`, `native_call` llegan a los objetos propios de Dungeondraft, y `ui_tree` / `ui_action` leen y manejan sus paneles y diálogos, para todo lo que no tiene una herramienta dedicada (fundido suave del terreno, el tamaño de un pincel, un preajuste de bioma). Es el último recurso: se salta el deshacer, así que guarda antes.
- **Lotes:** `batch` ejecuta hasta 100 llamadas a herramientas en una sola petición, para series largas de colocaciones independientes, como un único paso de deshacer.
- **Deshacer:** cada edición reversible (creación, borrado, movimiento, modificación, capa, terreno, cueva, agua, material) es un paso en el historial de deshacer del propio Dungeondraft, en la misma línea de tiempo que las ediciones que haces a mano. Tus Ctrl+Z y Ctrl+Y recorren los pasos del asistente igual que los tuyos, y sus `undo` / `redo` mueven ese mismo historial; dejan intacto un paso que hayas hecho a mano salvo que lo pidas (`include_user`). Un lote, una dispersión, una habitación construida, un prefab o un borrado de varios elementos cuentan como un solo paso. `checkpoint` marca un punto en el historial y `rollback_checkpoint` deshace todo lo posterior en una sola llamada; `list_checkpoints` muestra los que siguen abiertos. El historial conserva tantos pasos como indique la preferencia **Max undos** de Dungeondraft (32 por defecto). En una versión de Dungeondraft a la que le falte lo que esto necesita, el puente recurre a su propia lista de deshacer, separada de Ctrl+Z, y `ping` indica cuál se está usando.

Cada elemento se identifica con un `id` entero; las llamadas de creación y de listado devuelven ids que luego pasas a las de edición. Las coordenadas están en woxels (píxeles del mundo): llama a `get_status` para obtener `map_center`. Los assets se buscan con `list_assets`.

## Instalación

**Requisitos:** [Dungeondraft](https://dungeondraft.net/) (probado en las versiones con Godot 3.4.2), Python **3.10+** y un cliente MCP (por ejemplo Claude Code o Claude Desktop). En Windows, `python` / `.venv\Scripts\` sustituyen a las rutas `python3` / `.venv/bin/` que aparecen más abajo.

### 1. Instalar el mod en Dungeondraft

1. En la pantalla de título de Dungeondraft, abre **Mods** y anota (o define) tu carpeta de mods.
2. Copia `mod/dungeondraft-mcp-bridge/` en esa carpeta.
3. Activa **MCP Bridge** en la lista de mods y abre o crea un mapa.

Al cargar deberías ver en el registro de Dungeondraft:

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` es la versión del protocolo del puente, que sube a medida que se añaden comandos (`ping` también la indica).

### 2. Instalar el servidor MCP

Instálalo en un venv propio (el Python del sistema de la mayoría de las distribuciones está gestionado externamente, y un venv mantiene el punto de entrada limpio y estable). Desde la raíz del repositorio:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

Esto instala el paquete `dungeondraft_mcp` y crea el punto de entrada `.venv/bin/dungeondraft-mcp`.

La primera vez que Dungeondraft arranca con el mod, este escribe un token aleatorio en la carpeta de usuario de Dungeondraft (junto a `config.ini`); el servidor lo lee de ahí, así que no hay nada que configurar. Solo un programa que pueda leer tus archivos puede manejar tu mapa. El panel **MCP Bridge**, en los ajustes de Dungeondraft, muestra qué está haciendo el puente y reúne los controles que son solo tuyos: **Pause AI edits** (pausa las ediciones de la IA; las herramientas de solo lectura siguen funcionando), **Undo the assistant's last step** (deshace el último paso del asistente) y el interruptor del token.

### 3. Comprobar que el puente funciona

Con DD abierto y un mapa cargado, ejecuta el validador comando a comando. Construye una pequeña escena desechable cerca del centro del mapa, imprime PASS/FAIL para cada comando y después borra o deshace lo que hizo:

```bash
.venv/bin/python server/test_bridge.py
```

Si no consigue conectar *y* nunca viste la línea `listening` en el registro de DD, el sandbox bloqueó `TCP_Server`: consulta la nota sobre la alternativa en PROTOCOL.md.

Las comprobaciones de distribución son geometría pura y tienen sus propias pruebas, que no necesitan ni Dungeondraft ni un mapa:

```bash
.venv/bin/python -m unittest discover -s server/tests
```

Para *ver* cómo se construye algo (una pequeña habitación amueblada que queda en el mapa, en lugar de una prueba que limpia lo que hace), ejecuta `.venv/bin/python server/demo_build.py`.

### 4. Conectar tu cliente MCP

Registra el punto de entrada con su **ruta absoluta**. Para **Claude Code**:

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

Reinicia Claude Code para que cargue el servidor (`/mcp` muestra su estado y sus herramientas). También puedes añadirlo a la configuración de un cliente (por ejemplo `claude_desktop_config.json` de Claude Desktop), usando la ruta absoluta:

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

Después pídele al modelo cosas como *«¿cuál es el estado de mi mapa de Dungeondraft?»* o *«lista algunos assets de objetos y pon una silla en el centro del mapa»*.

## Configuración

Estas variables de entorno configuran el **servidor de Python** (defínelas donde el cliente MCP lo lanza). Los valores por defecto coinciden con los del mod, así que solo las necesitas si cambias las constantes `HOST` / `PORT` del mod en `mcp_bridge.gd`: las dos mitades deben coincidir.

| Variable de entorno | Valor por defecto | Para qué sirve |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | host al que se conecta el servidor |
| `DD_BRIDGE_PORT` | `8787` | puerto al que se conecta el servidor (igual que el `PORT` del mod) |

## Ampliar

Añadir una capacidad es simétrico: un manejador en cada lado.

1. **Mod** (`mod/.../scripts/tools/mcp_bridge.gd`): añade un `case` al `match` de `_safe_dispatch()` y una función `_my_command(req)` que devuelva `_ok(...)` / `_err(...)`.
2. **Servidor** (`server/dungeondraft_mcp/server.py`): añade un `@mcp.tool()` que llame a `bridge.request("my_command", ...)`.

Una capacidad que solo es cálculo sobre lo que el mod ya informa vive únicamente en el servidor: las comprobaciones de distribución (`spatial.py`), `scatter_objects` y `batch` no tienen un manejador propio en el mod.

Buenos siguientes objetivos: borrar un nivel, agrupar un lote de ediciones en un único paso de deshacer y huellas que sigan el contorno de un sprite en lugar de su rectángulo envolvente.

Los pull requests son bienvenidos. Lee antes [CONTRIBUTING.md](CONTRIBUTING.md) (en inglés): explica qué debe incluir un cambio completo y cómo probarlo.

### Ciclo de desarrollo (léelo antes de iterar sobre el mod)

Hay algunas cosas que te darán problemas si no las conoces de antemano:

- **Editar el mod y editar el servidor se recargan por caminos distintos.** Los cambios de GDScript en `mcp_bridge.gd` surten efecto cuando **recargas el mod dentro de Dungeondraft** (o reinicias DD). Las definiciones `@mcp.tool()` nuevas o modificadas en `server.py` solo aparecen después de que **se reinicie el proceso del servidor MCP**; en Claude Code eso significa `/mcp` → reconectar `dungeondraft` (o reiniciar el cliente). La instalación es editable (`pip install -e`), así que nunca reinstalas; solo reinicias el proceso. Añadir una *herramienta nueva* necesita **las dos** recargas (el mod para el manejador, el servidor para el registro de la herramienta); cambiar el comportamiento de un manejador *existente* solo necesita recargar el mod.

- **Sondea Dungeondraft antes de escribir un manejador.** `native_describe` lista los métodos y las propiedades de un objeto vivo, `native_get` los lee y `native_call` prueba un método, todo sin recargar el mod, así que puedes averiguar primero qué hace realmente una API. Dentro de un manejador sigue sin haber depurador: cuando una llamada a DD se comporta de forma inesperada (por ejemplo, un setter que no se mantiene), **devuelve el estado intermedio en la respuesta**, es decir, guarda los valores de antes y de después en un campo de depuración, para que una sola recarga muestre dónde cambia un valor.

- **Un script que no compila hace que Dungeondraft se cierre** al cargar el mod, sin ningún mensaje. Comprueba antes, sin conexión, que `mcp_bridge.gd` compila; la receta está en PROTOCOL.md («Mod compile errors crash Dungeondraft»).

## Estructura

```
mod/dungeondraft-mcp-bridge/   el mod de Dungeondraft (cópialo en la carpeta de mods de DD)
  mcp_bridge.ddmod             manifiesto
  scripts/tools/mcp_bridge.gd  servidor TCP + manejadores de comandos
server/                        el servidor MCP en Python
  dungeondraft_mcp/server.py   definiciones de las herramientas MCP
  dungeondraft_mcp/spatial.py  comprobaciones de distribución (geometría pura sobre una instantánea del mapa)
  dungeondraft_mcp/layout.py   disposición del mobiliario y muros compartidos
  dungeondraft_mcp/floorplan.py  accesibilidad de las habitaciones a partir de muros y puertas
  dungeondraft_mcp/scene.py    comprobaciones de suelo desnudo y de fuentes de luz
  dungeondraft_mcp/mapfile.py  lector de .dungeondraft_map y edición del manifiesto de packs
  dungeondraft_mcp/packs.py    índice, etiquetas y búsqueda de packs de assets (sin conexión)
  dungeondraft_mcp/uvtt_import.py  analizador de exportaciones Universal VTT y plan de reconstrucción
  dungeondraft_mcp/bridge_client.py  cliente TCP/JSON
  tests/                       pruebas unitarias sin conexión (no necesitan Dungeondraft)
  test_bridge.py               prueba de humo independiente, comando a comando
  test_features.py             validador en vivo de las herramientas de la 0.3.0
  demo_build.py                construye una habitación de ejemplo visible y la deja en el mapa
tools/check_gdscript.py        comprobación de compilación del mod sin conexión (Godot 3.4.2 estándar)
.github/workflows/ci.yml       pruebas unitarias y comprobación de compilación en cada push
PROTOCOL.md                    protocolo de comunicación + notas de implementación (en inglés)
```

## Créditos

Construido sobre la [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/). El motor es Godot 3.4.2, así que el GDScript usa los nombres de clases de red de Godot 3.

Las ranuras de terreno ampliadas, la edición de muros, la importación de imágenes, el agua, los materiales, las herramientas de niveles y capas, las comprobaciones de distribución, el acceso nativo y a la interfaz, la edición de luces y texto, la dispersión y los lotes fueron creados primero por Li-Jiaquan en [dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge), un fork de este proyecto, y se trajeron de vuelta desde allí.

El deshacer de peticiones enteras y los borrados reversibles, los puntos de control, la herramienta de disposición y los muros compartidos, las comprobaciones de plano y de escena, el listado, la búsqueda y la preparación de packs de assets, las hojas de contactos de vista previa, la inspección de archivos de mapa, la importación de Universal VTT, los prefabs y el generador del asistente Map Wizard, el estilo de mapa, el parámetro Range propio de Dungeondraft para las luces y la puerta por defecto, el token del puente y el panel de pausa, el cambio de tamaño y el recorte del mapa, y la compatibilidad con Custom Snap Mod fueron creados primero en [battlemap-mcp](https://github.com/thekannen/battlemap-mcp) por [@thekannen](https://github.com/thekannen), una reconstrucción de este proyecto, y se volvieron a derivar aquí a partir del código propio de Dungeondraft.

## Licencia

[MIT](LICENSE).
