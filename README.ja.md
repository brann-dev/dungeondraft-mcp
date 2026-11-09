# dungeondraft-mcp

[English](README.md) · [简体中文](README.zh-CN.md) · 日本語 · [Français](README.fr.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md)

> この文書は英語版 [README.md](README.md) の翻訳です。内容に違いがある場合は英語版を正とします。訳の誤りに気づいたら、issue や PR でお知らせください。

LLM（Claude Code、Claude Desktop など）が、Dungeondraft の GDScript MOD API を通じて**起動中の Dungeondraft** を操作できるようにする MCP サーバーです。オブジェクトの配置、壁の描画、マップの確認などができます。

Dungeondraft には外部から接続できるサーバー機能がないため、このプロジェクトは localhost のソケットでつながる **2 つの部分**で構成されています。

```
Claude / MCP クライアント
      │  MCP（stdio）
      ▼
server/   ── Python MCP サーバー（このリポジトリ）
      │  TCP 上の改行区切り JSON（127.0.0.1:8787）
      ▼
mod/      ── Dungeondraft MOD（GDScript、DD 内で動作）
      │  MOD API の呼び出し
      ▼
   開いているマップ（Objects、Walls、Levels…）
```

MOD は Dungeondraft の内部で TCP サーバーを開き、毎フレーム `update(delta)` フックからポーリングします。Python 側は各機能を MCP ツールとして公開し、呼び出しを JSON で転送します。通信形式は [PROTOCOL.md](PROTOCOL.md)（英語）を参照してください。

> **状態：動作中。** **Godot 3.4.2** ベースの Dungeondraft でエンドツーエンドの動作を確認済みです。MOD のサンドボックスから素の TCP が使えるため、代替手段は不要です。クエリ、作成、配置、変更、地形、水域、マテリアル、レベル、レイヤー、レイアウトとシーンのチェック、アセットパック、マップファイル、VTT のインポートとエクスポート、選択、キャプチャ、カメラ、履歴、マップの保存と読み込みにわたる 97 個のツールに加え、Dungeondraft 自身の API と UI に直接アクセスする最終手段も用意しています（下記参照）。

## AI にできること

- **確認：** `ping`、`get_status`、`list_levels`、`list_elements`、`get_element`、`list_asset_categories`、`list_assets`（部分文字列検索に対応）、`get_terrain`（塗られた地形をスロットごとの重みのグリッドとして読み取ります）。
- **アセットパック：** `list_asset_packs`（どのパックがインストールされていて、そのうちどれをこのマップが実際に使えるか。Dungeondraft はパックをマップごとに限定するため、パックなしで作ったマップには約 1800 個のコアアセットしか見えず、警告も出ません）、`search_pack_contents`（マップが使えないものも含め、インストール済みのすべてのパックの中身を単語とタグで検索します。画像データは読みません）、`prepare_map_with_packs`（より多くのパックを含めたマップのコピーを書き出して開きます）。`inspect_map_file` は `.dungeondraft_map` を開かずに要約します。必要なパック、レベルごとの要素数、よく使われているテクスチャ、閉状態のフラグが名前と矛盾しているドアなどです。`compare_to` は 2 つのファイルの差分を取ります。
- **作成：** `place_object`、`draw_wall`、`draw_path`、`add_light`、`add_portal`、`add_roof`、`add_text`、`place_pattern`（板張り・タイル・レンガなどのタイル状の床。オブジェクトの下に描画されます）、`build_room`（同じ 1 本のパス上に、閉じた壁とそれにぴったり合う床をまとめて作成します。UI で壁と床を一緒にトレースするのと同じです）、`import_image`（ディスク上の PNG/JPG/WEBP をオブジェクトとして埋め込みます。自作の小道具、ハンドアウト、バトルマップ全体の背景など。画像データはマップファイル内に保存されます）、`scatter_objects`（1 回の呼び出しで木立やがれきをばらまきます。壁や出入口は避けます）、`place_objects`（1 回の呼び出しで複数のオブジェクトをそれぞれ指定した場所に置きます）、`arrange_objects`（壁にぴったり沿わせた棚の列、テーブルを囲む椅子、机の上の小物などを、各アセットが実際に描く範囲に合わせて配置します）、`list_prefabs` / `place_prefab`（Dungeondraft に保存されたグループ）、`generate_dungeon`（マップウィザードのダンジョンや洞窟のレイアウトを 1 回の呼び出しで生成します）、`set_trace_image`（トレース用の参考画像をマップの上に表示します）。`build_room` は境界上にすでに立っている壁を再利用するので、隣り合う 2 つの部屋は 1 枚の壁を共有し、その間のドアは VTT でも開きます。
- **地形：** `set_terrain_slot`、`fill_terrain`（レベル全体）、`fill_region`（矩形または多角形。たとえば 1 部屋分の床）、`paint_terrain`（ソフトブラシ）、`paint_path`（折れ線に沿ったなめらかな連続ストローク。道や小道に）。既定ではスロット 0〜3 を使います。`set_terrain_slot(..., expand=True)` でそのレベルのスロット 4〜7 を有効にできます。
- **水域とマテリアル：** `draw_water`（湖・川・堀を矩形または多角形で描画します。岸辺と深さのグラデーション付きで、`erase` を使うと島を切り抜けます）と `draw_material`（溶岩・氷・石畳などのマテリアルブラシのテクスチャを、指定したレイヤーに矩形または多角形で塗ります）。
- **洞窟：** `dig_cave` はパスに沿って洞窟やトンネルを掘ります（洞窟ブラシに相当します。岩盤から床を掘り出し、岩壁とがれきを自動生成します。`dig=false` で埋め戻し。床と壁の色合いも指定できます）。`clear_caves` は洞窟レイヤー全体を消去します。
- **編集：** `move_element`、`move_elements`（複数の要素を同じオフセットで移動）、`modify_object`（拡大縮小、回転、色、影、反転、光の遮断）、`modify_wall`（壁の形や見た目を変更します。取り付けられたドアや窓はそのまま残り、壁の区間に合わせて移動します）、`modify_light`、`modify_text`、`duplicate_object`、`delete_element`、`delete_elements`、`select_elements`、`clear_selection`。`set_map_style` はマップ全体の建物の劣化具合とグリッドの線種を設定します。
- **レベル：** `add_level`、`set_level`、`rename_level`、`clone_level`（フロア全体をコピーして次の階の出発点にします）、`delete_level`、`reorder_levels`、`compare_levels`（別のフロアを現在のフロアの下に薄く表示して位置を合わせます）、`configure_level`（夕暮れや夜の場面のための環境光、ライティングのオン/オフ、地形の表示とブレンド、水域の岸辺）。
- **レイヤー：** `list_layers`、`set_element_layer`（何が何を覆うかを決めます。ラグはテーブルの下、樹冠は小道の上など）、`add_layer`。`place_object` と `import_image` でも `layer` を指定できます。
- **レイアウトチェック：** `get_asset_footprint`（アセットの実際の大きさ。スケールを決める前に確認します）、`check_object_placement`（場所を試します。壁をまたがないか、ドアをふさがないか、何かに重ならないか）、`validate_layout`（家具を置き終えたレベルを点検します）、`validate_floorplan`（家具を置く前に確認します。すべての部屋に到達できるか、すべてのドアがどこかに通じているか、入口はあるか）、`validate_scene`（何も塗られていないむき出しの地面と、目に見える光源のないライト）、そして部屋・空けておく区域・保護する地面を登録する `set_spatial_region` / `remove_spatial_region` / `list_spatial_regions`。オブジェクト系のツールに `check=True` を渡すと、不適切な配置をその場で拒否します。地形・洞窟・水域・マテリアルの編集は `protected`（保護）区域には触れません。
- **マップのライフサイクル：** `get_current_map`（開いているマップのパスと、保存中かどうか）、`save_map`（現在のファイルに保存します。DD の Save ボタンと同じです）、`open_map`（保存済みの別のマップに切り替えます。新しいマップの読み込みが終わる前に応答するので、`get_current_map` をポーリングしてください。未保存の変更は確認なしで破棄されるため、先に `save_map` を実行してください）。
- **見る：** `screenshot`（現在のウィンドウ）と `export_map`（マップ全体のきれいなレンダリング。`grid=False` でグリッドなし）は画像を返すので、モデルは自分の作業結果を見て調整を繰り返せます。`set_grid` はエディターのグリッドの表示/非表示を切り替えます。`export_uvtt` は Universal VTT の `.dd2vtt`（画像 + 壁・ドア・ライト）を書き出し、Foundry や Roll20 に取り込めます。`import_dd2vtt` はそのファイルを開いているマップ上に再構築します。各出入口でつなぎ直した壁、ドアと窓、ライトと環境光を復元し、書き出し時の画像をトレース用に下に敷きます。
- **カメラ：** `get_camera`、`set_camera`、`focus_element`（1 つの要素を中央に表示）、`fit_elements`（複数の要素を画面に収める）、`reset_camera`（見失ったビューを元に戻す）。`screenshot` の前にビューを合わせれば、壁に開けたドアのような特定の場所を確認できます。
- **ネイティブアクセス：** `native_targets`、`native_describe`、`native_get`、`native_set`、`native_call` は Dungeondraft 自身のオブジェクトにアクセスし、`ui_tree` / `ui_action` はそのパネルやダイアログを読み取って操作します。専用ツールがない機能（地形のスムーズブレンド、ブラシのサイズ、バイオームのプリセットなど）のためのものです。これは最終手段です。アンドゥを通らないので、先に保存してください。
- **バッチ：** `batch` は 1 回のリクエストで最大 100 件のツール呼び出しを実行します。互いに独立した配置を大量に行うときに便利で、全体が 1 回のアンドゥ単位になります。
- **アンドゥ：** ブリッジは作成・削除・移動・変更・レイヤー・地形・洞窟・水域・マテリアルの編集について独自のアンドゥ/リドゥスタックを持っているため、モデルは `undo` / `redo` で自分の変更を確実に取り消せます（Dungeondraft の Ctrl+Z とは独立しています）。バッチ、スキャッター、部屋の作成、プレハブ、複数要素の削除は、それぞれ 1 ステップとして扱われます。`checkpoint` は履歴に目印を付け、`rollback_checkpoint` はそれ以降の変更を 1 回の呼び出しですべて取り消します。`list_checkpoints` は有効な目印の一覧を表示します。履歴は直近 200 ステップを保持します。

すべての要素は整数の `id` で参照します。作成系と一覧系の呼び出しが id を返すので、それを編集系の呼び出しに渡します。座標は woxel（ワールドピクセル）空間です。`map_center` は `get_status` で取得できます。アセットは `list_assets` で探します。

## セットアップ

**前提条件：** [Dungeondraft](https://dungeondraft.net/)（Godot 3.4.2 ベースのビルドでテスト済み）、Python **3.10 以上**、MCP クライアント（Claude Code や Claude Desktop など）。Windows では、以下の `python3` / `.venv/bin/` を `python` / `.venv\Scripts\` に読み替えてください。

### 1. MOD を Dungeondraft にインストールする

1. Dungeondraft のタイトル画面で **Mods** を開き、MOD フォルダーを確認（または設定）します。
2. `mod/dungeondraft-mcp-bridge/` をそのフォルダーにコピーします。
3. MOD 一覧で **MCP Bridge** を有効にし、マップを開くか新規作成します。

読み込まれると、Dungeondraft のログに次の行が表示されます。

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` はブリッジのプロトコルバージョンで、コマンドが追加されるたびに上がります（`ping` でも確認できます）。

### 2. MCP サーバーをインストールする

専用の venv にインストールしてください（多くのディストリビューションではシステムの Python が外部管理されているため、venv を使うとエントリーポイントをきれいで安定した状態に保てます）。リポジトリのルートで実行します。

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

これで `dungeondraft_mcp` パッケージがインストールされ、エントリーポイント `.venv/bin/dungeondraft-mcp` が作成されます。

### 3. ブリッジの動作を確認する

DD を起動してマップを読み込んだ状態で、コマンドごとの検証スクリプトを実行します。マップ中央付近に小さな使い捨てのシーンを作り、各コマンドの PASS/FAIL を表示したあと、作ったものを削除するか元に戻します。

```bash
.venv/bin/python server/test_bridge.py
```

接続に失敗し、*しかも* DD のログに `listening` の行が一度も出ていない場合は、サンドボックスが `TCP_Server` をブロックしています。PROTOCOL.md の代替手段に関する注記を参照してください。

レイアウトチェックは純粋な幾何計算で、専用のテストがあります。Dungeondraft もマップも必要ありません。

```bash
.venv/bin/python -m unittest discover -s server/tests
```

実際に何かが作られるところを*見たい*場合（後片付けをするテストではなく、家具付きの小さな部屋をマップに残します）は、`.venv/bin/python server/demo_build.py` を実行してください。

### 4. MCP クライアントに接続する

エントリーポイントは**絶対パス**で登録します。**Claude Code** の場合：

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

サーバーを読み込むために Claude Code を再起動します（`/mcp` で状態とツールを確認できます）。クライアントの設定ファイル（たとえば Claude Desktop の `claude_desktop_config.json`）に、同じく絶対パスで追加することもできます。

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

あとは、*「Dungeondraft のマップの状態を教えて」*や*「オブジェクトのアセットをいくつか一覧にして、マップの中央に椅子を置いて」*のようにモデルに頼んでみてください。

## 設定

以下の環境変数は **Python サーバー**の設定です（MCP クライアントがサーバーを起動する場所で設定します）。既定値は MOD と一致しているので、`mcp_bridge.gd` の `HOST` / `PORT` 定数を変更した場合にだけ必要です。両方の値は一致していなければなりません。

| 環境変数 | 既定値 | 用途 |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | サーバーの接続先ホスト |
| `DD_BRIDGE_PORT` | `8787` | サーバーの接続先ポート（MOD の `PORT` に合わせます） |

## 拡張する

機能の追加は対称的で、両側にハンドラーを 1 つずつ書きます。

1. **MOD**（`mod/.../scripts/tools/mcp_bridge.gd`）：`_safe_dispatch()` の `match` に `case` を追加し、`_ok(...)` / `_err(...)` を返す `_my_command(req)` 関数を追加します。
2. **サーバー**（`server/dungeondraft_mcp/server.py`）：`bridge.request("my_command", ...)` を呼び出す `@mcp.tool()` を追加します。

MOD がすでに報告している情報を計算するだけの機能は、サーバー側だけにあります。レイアウトチェック（`spatial.py`）、`scatter_objects`、`batch` には、MOD 側の専用ハンドラーがありません。

次に取り組むとよい課題：レベルの削除、複数の編集を 1 回のアンドゥにまとめること、スプライトの外接矩形ではなく輪郭に沿ったフットプリント。

プルリクエストを歓迎します。まず [CONTRIBUTING.md](CONTRIBUTING.md)（英語）を読んでください。変更に含めるべきものと、テストの方法をまとめています。

### 開発ループ（MOD を触る前に読んでください）

あらかじめ知らないと痛い目を見ることがいくつかあります。

- **MOD の編集とサーバーの編集では、再読み込みの方法が違います。** `mcp_bridge.gd` の GDScript の変更は、**Dungeondraft 内で MOD を再読み込み**する（または DD を再起動する）と反映されます。`server.py` で追加・変更した `@mcp.tool()` の定義は、**MCP サーバーのプロセスを再起動**して初めて現れます。Claude Code では `/mcp` → `dungeondraft` を再接続（またはクライアントを再起動）です。インストールは編集可能モード（`pip install -e`）なので再インストールは不要で、プロセスを起動し直すだけです。*新しいツール*の追加には**両方**の再読み込みが必要です（ハンドラーのために MOD、ツール登録のためにサーバー）。*既存の*ハンドラーの動作を変えるだけなら、MOD の再読み込みだけで済みます。

- **ハンドラーを書く前に Dungeondraft を調べましょう。** `native_describe` は動作中のオブジェクトのメソッドとプロパティを一覧し、`native_get` はそれらを読み取り、`native_call` はメソッドを試しに呼び出します。どれも MOD の再読み込みなしで使えるので、API が実際にどう動くかを先に確かめられます。ハンドラーの内部には今もデバッガーがありません。DD の呼び出しが予想外の動きをしたとき（たとえばセッターが効かないとき）は、**途中の状態をレスポンスに入れて返してください**。呼び出し前後の値をデバッグ用フィールドに入れておけば、1 回の再読み込みで値がどこで変わるかが分かります。

- **コンパイルできないスクリプトは、MOD の読み込み時に Dungeondraft をクラッシュさせます。** メッセージは何も出ません。先に `mcp_bridge.gd` をオフラインでコンパイルチェックしてください。手順は PROTOCOL.md（「Mod compile errors crash Dungeondraft」）にあります。

## 構成

```
mod/dungeondraft-mcp-bridge/   Dungeondraft MOD（DD の MOD フォルダーにコピー）
  mcp_bridge.ddmod             マニフェスト
  scripts/tools/mcp_bridge.gd  TCP サーバー + コマンドハンドラー
server/                        Python MCP サーバー
  dungeondraft_mcp/server.py   MCP ツールの定義
  dungeondraft_mcp/spatial.py  レイアウトチェック（マップのスナップショットに対する純粋な幾何計算）
  dungeondraft_mcp/layout.py   家具の配置と共有壁
  dungeondraft_mcp/floorplan.py  壁とドアから部屋の到達可能性を求める
  dungeondraft_mcp/scene.py    むき出しの地面と光源のチェック
  dungeondraft_mcp/mapfile.py  .dungeondraft_map の読み取りとパックマニフェストの編集
  dungeondraft_mcp/packs.py    アセットパックのインデックス、タグ、検索（オフライン）
  dungeondraft_mcp/uvtt_import.py  Universal VTT エクスポートのパーサーと再構築プラン
  dungeondraft_mcp/bridge_client.py  TCP/JSON クライアント
  tests/                       オフラインの単体テスト（Dungeondraft 不要）
  test_bridge.py               コマンドごとの単独スモークテスト
  test_features.py             0.3.0 のツールのライブ検証スクリプト
  demo_build.py                見て分かるサンプルの部屋を作って残す
tools/check_gdscript.py        MOD のオフラインコンパイルチェック（素の Godot 3.4.2）
.github/workflows/ci.yml       プッシュごとに単体テストとコンパイルチェックを実行
PROTOCOL.md                    通信プロトコル + 実装メモ（英語）
```

## クレジット

[Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/) をもとに作られています。エンジンは Godot 3.4.2 なので、GDScript では Godot 3 のネットワーククラス名を使っています。

拡張地形スロット、壁の編集、画像のインポート、水域、マテリアル、レベルとレイヤーのツール、レイアウトチェック、ネイティブ/UI アクセス、ライトとテキストの編集、スキャッター、バッチは、Li-Jiaquan 氏がこのプロジェクトのフォーク [dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge) で最初に実装し、そこから移植したものです。

共有壁、リクエスト全体を 1 回で取り消すアンドゥと取り消せる削除、間取りとシーンのチェック、アセットパックの読み取り、マップファイルの検査、Universal VTT のインポート、配置ツール、ライトへの Dungeondraft 自身の Range の採用は、thekannen 氏が [battlemap-mcp](https://github.com/thekannen/battlemap-mcp)（このプロジェクトの 6 月のプロトタイプを作り直したもの）で最初に実装し、ここでは Dungeondraft 自身のコードに照らして改めて導き出したものです。

## ライセンス

[MIT](LICENSE)。
