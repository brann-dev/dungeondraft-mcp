# dungeondraft-mcp

[English](README.md) · 简体中文 · [日本語](README.ja.md) · [Français](README.fr.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md)

> 本文译自英文版 [README.md](README.md)，内容以英文版为准。如发现翻译问题，欢迎提 issue 或 PR。

一个 MCP 服务器，让大语言模型（Claude Code、Claude Desktop 等）通过 Dungeondraft 的 GDScript 模组 API 操作**正在运行的 Dungeondraft**：放置物件、绘制墙体、查看地图。

Dungeondraft 本身没有可供外部连接的服务端，因此本项目由**两部分**组成，二者通过本机套接字通信：

```
Claude / MCP 客户端
      │  MCP（stdio）
      ▼
server/   ── Python MCP 服务器（本仓库）
      │  TCP 上按行分隔的 JSON（127.0.0.1:8787）
      ▼
mod/      ── Dungeondraft 模组（GDScript，运行在 DD 内部）
      │  调用模组 API
      ▼
   当前打开的地图（Objects、Walls、Levels……）
```

模组在 Dungeondraft 内部开启一个 TCP 服务，并在每帧的 `update(delta)` 回调中轮询；Python 端把每项能力暴露为一个 MCP 工具，并以 JSON 转发调用。通信格式见 [PROTOCOL.md](PROTOCOL.md)（英文）。

> **状态：可用。** 已在基于 **Godot 3.4.2** 的 Dungeondraft 上完成端到端验证：模组沙箱内可以直接使用原生 TCP，无需备用方案。共 100 个工具，涵盖查询、创建、排布、修改、地形、水域、材质、楼层、图层、布局与场景检查、素材包、地图文件、VTT 导入与导出、选择、截图与导出、镜头、历史记录、地图的打开与保存，另有一条直达 Dungeondraft 自身 API 和界面的兜底通道（见下文）。

## AI 能做什么

- **查看：** `ping`（还会告知用户是否已暂停 AI 编辑）、`get_status`、`list_levels`、`list_elements`、`get_element`、`list_asset_categories`、`list_assets`（支持子串搜索）、`get_terrain`（把已绘制的地形读回为各槽权重的网格）。
- **素材包：** `preview_assets`（把最多 32 个候选素材合成一张预览总图，放置之前可以先看一看；`true_scale` 会按相对大小显示它们）、`list_asset_packs`（列出已安装的素材包，以及当前地图实际能用的那些：Dungeondraft 按地图限定素材包，创建时未带上它们的地图只能看到约 1800 个核心素材，而且不会有任何提示）、`search_pack_contents`（按词语和标签搜索每一个已安装素材包的内容，包括当前地图未启用的，全程不读取美术资源）和 `prepare_map_with_packs`（写出一份包含更多素材包的地图副本并打开它）。`inspect_map_file` 无需打开 `.dungeondraft_map` 文件即可给出摘要：它需要哪些素材包、各楼层的元素数量、最常用的纹理、关闭标志与名字相矛盾的门；`compare_to` 对比两个文件的差异。
- **创建：** `place_object`、`draw_wall`、`draw_path`、`add_light`、`add_portal`、`add_roof`、`add_text`、`place_pattern`（平铺的图案地板，如木板、瓷砖、砖块，绘制在物件下方）、`build_room`（沿同一条路径同时生成闭合墙体和与之吻合的地板，就像界面中把墙和地板一起描出来那样）、`import_image`（把本地的 PNG/JPG/WEBP 嵌入为物件：自制道具、给玩家的资料图，或整张战斗地图底图；像素数据保存在地图文件内）、`scatter_objects`（一次调用撒出一片树林或一地碎石，自动避开墙体和门口）、`place_objects`（一次调用把多个物件放到各自指定的位置）、`arrange_objects`（紧贴墙面的一排架子、围着桌子的椅子、书桌上的物品，按每个素材实际绘制的范围贴合摆放）、`list_prefabs` / `place_prefab`（Dungeondraft 保存的组合）、`generate_dungeon`（一次调用生成地图向导的地牢或洞穴布局）、`set_trace_image`（在地图上方叠加一张参考图，用于描摹）。`build_room` 会复用已经立在边界上的墙，因此并排的两个房间共用一面墙，它们之间的门在 VTT 中也能正常打开。
- **地形：** `set_terrain_slot`、`fill_terrain`（整层填充）、`fill_region`（填充矩形或多边形，例如某个房间的地面）、`paint_terrain`（柔边笔刷）、`paint_path`（沿折线连续平滑地涂抹，适合道路和小径）。默认使用 0–3 号槽；`set_terrain_slot(..., expand=True)` 可开启该楼层的 4–7 号槽。
- **水域与材质：** `draw_water`（以矩形或多边形绘制湖泊、河流、护城河，自带岸线和深浅渐变；`erase` 可挖出岛屿）和 `draw_material`（在指定图层上以矩形或多边形铺设熔岩、冰面、卵石路等材质笔刷纹理）。
- **洞穴：** `dig_cave` 沿路径开凿洞穴和隧道（即洞穴笔刷：从岩石中挖出可通行的地面，自动生成岩壁和碎石；`dig=false` 为回填；可选地面和岩壁染色），`clear_caves` 清空整个洞穴层。
- **编辑：** `move_element`、`move_elements`（把多个元素按同一偏移量移动）、`modify_object`（缩放、旋转、颜色、阴影、镜像、是否遮挡光线）、`modify_wall`（改变墙体的形状或外观；墙上的门窗保持安装状态，并随所在墙段移动）、`modify_light`、`modify_text`、`duplicate_object`、`delete_element`、`delete_elements`、`select_elements`、`clear_selection`。`set_map_style` 设置整张地图的建筑磨损程度和网格线样式；`set_map_size` 以格为单位调整地图大小，效果与 Dungeondraft 自带的 Change Map Size 相同。放置类工具可传 `snap=True` 对齐到用户的网格，若加载了 Lievven 的 Custom Snap Mod，也包括它的六边形或等轴网格；`get_snap_settings` 可读取当前的吸附设置。
- **楼层：** `add_level`、`set_level`、`rename_level`、`clone_level`（复制整个楼层，作为下一层的起点）、`delete_level`、`reorder_levels`、`compare_levels`（把另一楼层半透明地显示在当前楼层下方，便于对齐）、`configure_level`（用于黄昏和夜景的环境光、灯光开关、地形的显示与混合方式、水域岸线）。
- **图层：** `list_layers`、`set_element_layer`（决定谁盖住谁：地毯在桌子下面，树冠在小路上方）、`add_layer`；`place_object` 和 `import_image` 也接受 `layer` 参数。
- **布局检查：** `get_asset_footprint`（素材的实际大小，选缩放比例前先看）、`check_object_placement`（试放一个位置：会不会穿墙、堵门、压到别的物件？）、`validate_layout`（检查已经布置好的楼层）、`validate_floorplan`（布置家具之前：每个房间都能到达吗，每扇门都通向某处吗，有进去的入口吗）、`validate_scene`（什么都没涂的裸露地面，以及没有可见光源的灯光），以及用于登记房间、净空区和保护区的 `set_spatial_region` / `remove_spatial_region` / `list_spatial_regions`。物件类工具可以传 `check=True`，直接拒绝不合理的放置；地形、洞穴、水域和材质的编辑不会触碰 `protected`（保护）区域。
- **地图的打开与保存：** `get_current_map`（当前地图的路径，以及是否正在保存）、`save_map`（保存到当前文件，等同于 DD 的保存按钮）、`open_map`（切换到另一张已保存的地图；它会在新地图加载完成之前返回，请轮询 `get_current_map`；未保存的修改会被直接丢弃且没有提示，所以请先 `save_map`）。
- **观察：** `screenshot`（当前窗口）和 `export_map`（干净的整图渲染，`grid=False` 时不带网格）会返回图像，模型可以看到自己的成果并反复调整。`set_grid` 显示或隐藏编辑器网格。`export_uvtt` 导出 Universal VTT 的 `.dd2vtt` 文件（图像 + 墙体、门、灯光），可导入 Foundry 或 Roll20；`import_dd2vtt` 则把这样的文件重建到当前打开的地图上：穿过每个门洞重新接合的墙体、门和窗、灯光与环境光，并把导出文件中的图像垫在下方作为描摹底图。
- **镜头：** `get_camera`、`set_camera`、`focus_element`（对准某个元素）、`fit_elements`（框住一组元素）、`reset_camera`（找回丢失的视图）。截图之前先调整视图，就能查看具体位置，例如墙上开出的门洞。
- **原生访问：** `native_targets`、`native_describe`、`native_get`、`native_set`、`native_call` 可以直接访问 Dungeondraft 自身的对象，`ui_tree` / `ui_action` 可以读取并操作它的面板和对话框，用于没有专用工具的功能（地形平滑混合、笔刷大小、生物群系预设等）。这是最后的手段：它绕过撤销机制，使用前请先保存。
- **批量：** `batch` 可在一次请求中执行最多 100 个工具调用，适合大量相互独立的放置操作，整体算作一步撤销。
- **撤销：** 桥接维护自己的撤销/重做栈，覆盖创建、删除、移动、修改、图层、地形、洞穴、水域和材质的编辑，因此模型可以用 `undo` / `redo` 可靠地回退自己的改动（与 Dungeondraft 的 Ctrl+Z 相互独立）。一次批量调用、一次散布、一间生成的房间、一个预制组合或一次多元素删除都算作一步。`checkpoint` 在历史中做一个标记，`rollback_checkpoint` 一次调用就撤销此后的全部改动；`list_checkpoints` 列出尚未回滚的标记。历史记录保留最近 200 步。

每个元素都用一个整数 `id` 标识；创建和列表类调用会返回 id，供后续的编辑调用使用。坐标采用 woxel（世界像素）空间，调用 `get_status` 可得到 `map_center`。素材路径用 `list_assets` 查询。

## 安装

**前置条件：** [Dungeondraft](https://dungeondraft.net/)（已在基于 Godot 3.4.2 的版本上测试）、Python **3.10+**，以及一个 MCP 客户端（如 Claude Code 或 Claude Desktop）。在 Windows 上，请把下文的 `python3` / `.venv/bin/` 换成 `python` / `.venv\Scripts\`。

### 1. 把模组安装到 Dungeondraft

1. 在 Dungeondraft 的标题界面打开 **Mods**，记下（或设置）你的模组文件夹。
2. 把 `mod/dungeondraft-mcp-bridge/` 复制到该文件夹。
3. 在模组列表中启用 **MCP Bridge**，然后打开或新建一张地图。

加载后，你应该能在 Dungeondraft 的日志中看到：

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` 是桥接的协议版本号，会随着命令的增加而递增（`ping` 也会返回它）。

### 2. 安装 MCP 服务器

请安装到独立的虚拟环境中（多数发行版的系统 Python 由系统管理，使用 venv 可以让入口命令保持干净、稳定）。在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

这会安装 `dungeondraft_mcp` 包，并生成入口命令 `.venv/bin/dungeondraft-mcp`。

Dungeondraft 第一次带着该模组启动时，模组会在 Dungeondraft 的用户文件夹（`config.ini` 旁边）写入一个随机令牌；服务器从那里读取它，因此无需任何配置。只有能读取你文件的程序才能操作你的地图。Dungeondraft 设置（Settings）中的 **MCP Bridge** 面板显示桥接正在做什么，并提供只有你才能使用的控制项：**Pause AI edits**（暂停 AI 编辑；只读工具照常可用）、**Undo the assistant's last step**（撤销助手的上一步），以及令牌开关。

### 3. 验证桥接是否正常

打开 DD 并载入一张地图后，运行逐命令验证脚本。它会在地图中心附近搭建一个小型临时场景，为每个命令打印 PASS/FAIL，然后删除或撤销它所做的一切：

```bash
.venv/bin/python server/test_bridge.py
```

如果连接失败，*并且*你从未在 DD 日志中看到 `listening` 那一行，说明沙箱拦截了 `TCP_Server`，请参阅 PROTOCOL.md 中关于备用方案的说明。

布局检查是纯几何计算，另有独立的测试，既不需要 Dungeondraft，也不需要地图：

```bash
.venv/bin/python -m unittest discover -s server/tests
```

想*亲眼看到*东西被搭出来（在地图上留下一间带家具的小房间，而不是会自行清理的测试），可以运行 `.venv/bin/python server/demo_build.py`。

### 4. 接入你的 MCP 客户端

请用**绝对路径**注册入口命令。**Claude Code** 的做法：

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

重启 Claude Code 以加载服务器（`/mcp` 可查看其状态和工具）。也可以写入客户端的配置文件（例如 Claude Desktop 的 `claude_desktop_config.json`），同样使用绝对路径：

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

然后就可以向模型提出这样的请求：*“我的 Dungeondraft 地图现在是什么状态？”* 或 *“列出一些物件素材，并在地图中央放一把椅子。”*

## 配置

这些环境变量用于配置 **Python 服务器**（在 MCP 客户端启动它的地方设置）。默认值与模组一致，只有当你修改了 `mcp_bridge.gd` 中的 `HOST` / `PORT` 常量时才需要设置，两端必须保持一致。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | 服务器连接的主机 |
| `DD_BRIDGE_PORT` | `8787` | 服务器连接的端口（需与模组的 `PORT` 一致） |

## 扩展

新增一项能力的方式是对称的：两端各写一个处理函数。

1. **模组**（`mod/.../scripts/tools/mcp_bridge.gd`）：在 `_safe_dispatch()` 的 `match` 中加一个 `case` 分支，并添加一个返回 `_ok(...)` / `_err(...)` 的 `_my_command(req)` 函数。
2. **服务器**（`server/dungeondraft_mcp/server.py`）：添加一个 `@mcp.tool()`，在其中调用 `bridge.request("my_command", ...)`。

如果一项能力只是对模组已有数据的计算，那它只存在于服务器端：布局检查（`spatial.py`）、`scatter_objects` 和 `batch` 在模组里没有对应的处理函数。

值得下一步实现的方向：删除楼层、把一批编辑合并为一步撤销，以及让占地范围贴合精灵图的轮廓，而不是它的外接矩形。

欢迎提交 PR。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)（英文）：其中列出了一个完整的改动应包含哪些内容，以及如何测试。

### 开发循环（改模组之前请先读这一节）

有几件事如果事先不知道，会让你吃亏：

- **修改模组和修改服务器是两条不同的重载路径。** `mcp_bridge.gd` 中的 GDScript 改动，要在 **Dungeondraft 内重新加载模组**（或重启 DD）后才生效。`server.py` 中新增或修改的 `@mcp.tool()` 定义，只有在 **MCP 服务器进程重启**后才会出现；在 Claude Code 中就是 `/mcp` → 重新连接 `dungeondraft`（或重启客户端）。服务器是可编辑安装（`pip install -e`），因此永远不需要重新安装，只需重启进程。添加一个*新工具*需要**两边都**重载（模组重载处理函数，服务器重载工具注册）；修改*已有*处理函数的行为只需重载模组。

- **写处理函数之前，先探一探 Dungeondraft。** `native_describe` 列出运行中对象的方法和属性，`native_get` 读取它们，`native_call` 试调用某个方法，这些都不需要重载模组，可以先弄清某个 API 的真实行为。处理函数内部仍然没有调试器：当某个 DD 调用表现异常时（例如某个 setter 设置后不生效），可以**把中间状态放进响应里返回**，也就是把调用前后的值存进一个调试字段，这样重载一次就能看出值是在哪里变掉的。

- **脚本编译不通过会让 Dungeondraft 在加载模组时直接崩溃**，而且没有任何提示。请先离线对 `mcp_bridge.gd` 做编译检查，方法见 PROTOCOL.md（“Mod compile errors crash Dungeondraft” 一节）。

## 目录结构

```
mod/dungeondraft-mcp-bridge/   Dungeondraft 模组（复制到 DD 的模组文件夹）
  mcp_bridge.ddmod             清单文件
  scripts/tools/mcp_bridge.gd  TCP 服务 + 命令处理函数
server/                        Python MCP 服务器
  dungeondraft_mcp/server.py   MCP 工具定义
  dungeondraft_mcp/spatial.py  布局检查（基于地图快照的纯几何计算）
  dungeondraft_mcp/layout.py   家具排布与共用墙体
  dungeondraft_mcp/floorplan.py  根据墙体和门计算房间的可达性
  dungeondraft_mcp/scene.py    裸露地面与光源检查
  dungeondraft_mcp/mapfile.py  .dungeondraft_map 读取器与素材包清单编辑
  dungeondraft_mcp/packs.py    素材包索引、标签与搜索（离线）
  dungeondraft_mcp/uvtt_import.py  Universal VTT 导出文件的解析与重建计划
  dungeondraft_mcp/bridge_client.py  TCP/JSON 客户端
  tests/                       离线单元测试（不需要 Dungeondraft）
  test_bridge.py               独立的逐命令冒烟测试
  test_features.py             0.3.0 新工具的在线验证脚本
  demo_build.py                搭建一间看得见的示例房间，并保留在地图上
tools/check_gdscript.py        模组的离线编译检查（原版 Godot 3.4.2）
.github/workflows/ci.yml       每次推送时运行单元测试和编译检查
PROTOCOL.md                    通信协议 + 实现笔记（英文）
```

## 致谢

基于 [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/) 开发。引擎为 Godot 3.4.2，因此 GDScript 使用的是 Godot 3 的网络类名。

扩展地形槽、墙体编辑、图片导入、水域、材质、楼层与图层工具、布局检查、原生与界面访问、灯光与文字编辑、散布和批量调用，最初由 Li-Jiaquan 在本项目的分支 [dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge) 中实现，之后移植回本仓库。

共用墙体、整次请求算作一步的撤销与可撤销的删除、平面图与场景检查、素材包读取、地图文件检视、Universal VTT 导入、排布工具，以及灯光使用 Dungeondraft 自身的 Range，最初由 thekannen 在 [battlemap-mcp](https://github.com/thekannen/battlemap-mcp) 中实现（该项目是对本项目六月原型的重写），之后在这里对照 Dungeondraft 自身的代码重新推导实现。

## 许可

[MIT](LICENSE)。
