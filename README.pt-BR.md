# dungeondraft-mcp

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Français](README.fr.md) · [Español](README.es.md) · Português · [Русский](README.ru.md)

> Este documento é uma tradução do [README.md](README.md) em inglês, que vale como referência em caso de diferença. Se encontrar um erro de tradução, abra uma issue ou um PR.

Um servidor MCP que permite a um LLM (Claude Code, Claude Desktop etc.) controlar uma **instância do Dungeondraft em execução** (posicionar objetos, desenhar paredes, inspecionar o mapa) por meio da API de modding em GDScript do Dungeondraft.

O Dungeondraft não tem um servidor embutido ao qual se conectar, então este projeto é formado por **duas metades** que se comunicam por um socket local:

```
Claude / cliente MCP
      │  MCP (stdio)
      ▼
server/   ── servidor MCP em Python  (este repositório)
      │  JSON delimitado por quebras de linha sobre TCP  (127.0.0.1:8787)
      ▼
mod/      ── mod do Dungeondraft  (GDScript, roda dentro do DD)
      │  chamadas à API de modding
      ▼
   o mapa aberto  (Objects, Walls, Levels, …)
```

O mod abre um servidor TCP dentro do Dungeondraft e o consulta a cada quadro pelo hook `update(delta)`; o lado Python expõe cada recurso como uma ferramenta MCP e encaminha as chamadas em JSON. Veja [PROTOCOL.md](PROTOCOL.md) (em inglês) para o formato das mensagens.

> **Status: funcionando.** Confirmado de ponta a ponta com o Dungeondraft em **Godot 3.4.2**: o TCP puro funciona a partir do sandbox de modding, sem necessidade de alternativa. São 101 ferramentas de consulta, criação, arranjo, modificação, terreno, água, materiais, níveis, camadas, verificações de layout e de cena, pacotes de assets, arquivos de mapa, importação e exportação VTT, seleção, captura, câmera, histórico e ciclo de vida do mapa, além de um acesso de último recurso à API e à interface do próprio Dungeondraft (veja abaixo).

## O que a IA pode fazer

- **Inspecionar:** `ping` (também informa se o usuário pausou as edições da IA), `get_status`, `list_levels`, `list_elements`, `get_element`, `list_asset_categories`, `list_assets` (com busca por substring), `get_terrain` (lê de volta o terreno pintado como uma grade de pesos por slot).
- **Pacotes de assets:** `preview_assets` (uma única folha de contato com até 32 assets candidatos, para você olhar antes de posicionar; `true_scale` mostra os tamanhos relativos deles), `list_asset_packs` (quais pacotes estão instalados e quais este mapa pode de fato usar: o Dungeondraft vincula os pacotes a cada mapa, e um mapa criado sem eles enxerga em silêncio apenas os ~1800 assets básicos), `search_pack_contents` (busca dentro de todos os pacotes instalados, inclusive os que faltam ao mapa, por palavras e tags, sem nunca ler a arte) e `prepare_map_with_packs` (grava uma cópia do mapa que inclui mais pacotes e a abre). `inspect_map_file` resume um `.dungeondraft_map` sem abri-lo: pacotes de que ele precisa, contagens por nível, texturas mais usadas, portas cujo sinalizador de fechada contradiz o nome; `compare_to` compara dois arquivos.
- **Criar:** `place_object`, `draw_wall`, `draw_path`, `add_light`, `add_portal`, `add_roof`, `add_text`, `place_pattern` (pisos com padrão, como tábuas, ladrilhos e tijolos, desenhados abaixo dos objetos), `build_room` (um laço de paredes e o piso correspondente sobre um mesmo traçado, como o traçado combinado da interface), `import_image` (incorpora um PNG/JPG/WEBP do disco como objeto: adereços próprios, handouts ou um battlemap inteiro como fundo; os pixels ficam guardados no mapa), `scatter_objects` (um bosque ou um campo de entulho em uma única chamada, longe de paredes e portas), `place_objects` (muitos objetos em pontos escolhidos, em uma única chamada), `arrange_objects` (uma fileira de estantes encostada na parede, cadeiras ao redor de uma mesa, itens sobre uma escrivaninha, ajustados ao que cada asset realmente desenha), `list_prefabs` / `place_prefab` (os grupos salvos do Dungeondraft), `generate_dungeon` (a planta de masmorra ou de caverna do Map Wizard em uma única chamada), `set_trace_image` (uma imagem de referência exibida sobre o mapa para decalcar). `build_room` reaproveita uma parede que já esteja sobre o contorno, de modo que duas salas lado a lado compartilham uma única parede e uma porta entre elas abre em um VTT.
- **Terreno:** `set_terrain_slot`, `fill_terrain` (o nível inteiro), `fill_region` (um retângulo ou um polígono, por exemplo o piso de uma sala), `paint_terrain` (um pincel suave), `paint_path` (um traço contínuo e suave ao longo de uma polilinha: estradas e trilhas). Slots 0 a 3 por padrão; `set_terrain_slot(..., expand=True)` ativa os slots 4 a 7 do nível.
- **Água e materiais:** `draw_water` (lagos, rios e fossos como retângulo ou polígono, com margem e gradiente de profundidade; `erase` recorta ilhas) e `draw_material` (lava, gelo, paralelepípedo e outras texturas do pincel de materiais sobre um retângulo ou um polígono, na camada escolhida).
- **Cavernas:** `dig_cave` escava cavernas e túneis ao longo de um traçado (o pincel de cavernas: abre chão na rocha, com paredes rochosas e entulho automáticos; `dig=false` preenche de volta; tons opcionais para o chão e as paredes), `clear_caves` apaga toda a camada de cavernas.
- **Editar:** `move_element`, `move_elements` (vários pelo mesmo deslocamento), `modify_object` (escala, rotação, cor, sombra, espelhamento, bloqueio de luz), `modify_wall` (muda a forma ou a aparência de uma parede; as portas e janelas continuam montadas e acompanham o seu segmento), `modify_light`, `modify_text`, `duplicate_object`, `delete_element`, `delete_elements`, `select_elements`, `clear_selection`. `set_map_style` define o desgaste das construções e o estilo das linhas da grade para o mapa inteiro; `set_map_size` redimensiona o mapa em tiles, como o próprio Change Map Size do Dungeondraft, e `crop_map` mantém uma janela de tiles a partir de qualquer lado, mostrando antes uma prévia do que seria removido. As ferramentas de posicionamento aceitam `snap=True` para cair na grade do usuário, inclusive na grade hexagonal ou isométrica do Custom Snap Mod de Lievven quando esse mod está carregado; `get_snap_settings` a lê.
- **Níveis:** `add_level`, `set_level`, `rename_level`, `clone_level` (copia um andar inteiro para começar o próximo), `delete_level`, `reorder_levels`, `compare_levels` (mostra outro andar, esmaecido, sob o atual para alinhar as coisas), `configure_level` (luz ambiente para cenas de entardecer e de noite, iluminação ligada ou desligada, visibilidade e mistura do terreno, margem da água).
- **Camadas:** `list_layers`, `set_element_layer` (decide o que cobre o quê: um tapete sob uma mesa, a copa de uma árvore sobre uma trilha), `add_layer`; `place_object` e `import_image` também aceitam `layer`.
- **Verificações de layout:** `get_asset_footprint` (o tamanho real de um asset, antes de escolher a escala), `check_object_placement` (testar um lugar: atravessa uma parede, bloqueia uma porta, sobrepõe algo?), `validate_layout` (auditar um nível já mobiliado), `validate_floorplan` (antes de mobiliar: toda sala é alcançável, toda porta dá para algum lugar, existe uma entrada?), `validate_scene` (chão nu sem nada pintado e luzes sem fonte visível) e `set_spatial_region` / `remove_spatial_region` / `list_spatial_regions` para salas, zonas que devem ficar livres e chão protegido. As ferramentas de objetos aceitam `check=True` para recusar de imediato um posicionamento ruim; as edições de terreno, caverna, água e material se recusam a tocar uma região `protected`.
- **Ciclo de vida do mapa:** `get_current_map` (caminho do mapa aberto e se há um salvamento em andamento), `save_map` (salva no arquivo atual, como o botão Save do DD) e `open_map` (troca para outro mapa salvo; responde antes de o novo mapa carregar, então consulte `get_current_map`; alterações não salvas são descartadas sem aviso, então chame `save_map` antes).
- **Ver:** `screenshot` (janela atual) e `export_map` (renderização limpa do mapa inteiro, `grid=False` para exportar sem a grade) devolvem imagens, de modo que o modelo pode olhar o próprio trabalho e iterar. `set_grid` mostra ou oculta a grade do editor. `export_uvtt` grava um `.dd2vtt` do Universal VTT (imagem + paredes, portas, luzes) para importar no Foundry ou no Roll20, e `import_dd2vtt` reconstrói um deles no mapa aberto: paredes reunidas através de cada vão de porta, portas e janelas, luzes e luz ambiente, com a imagem da exportação por baixo como decalque.
- **Câmera:** `get_camera`, `set_camera`, `focus_element` (centraliza um elemento), `fit_elements` (enquadra um grupo), `reset_camera` (recupera uma vista perdida): ajuste a vista antes de um `screenshot` para inspecionar um ponto específico, como uma porta aberta em uma parede.
- **Acesso nativo:** `native_targets`, `native_describe`, `native_get`, `native_set`, `native_call` alcançam os objetos do próprio Dungeondraft, e `ui_tree` / `ui_action` leem e operam seus painéis e diálogos, para tudo o que não tem uma ferramenta dedicada (mistura suave do terreno, o tamanho de um pincel, uma predefinição de bioma). É o último recurso: ele contorna o desfazer, então salve antes.
- **Lote:** `batch` executa até 100 chamadas de ferramentas em uma única requisição, para longas séries de posicionamentos independentes, como um único passo de desfazer.
- **Desfazer:** a ponte mantém suas próprias pilhas de desfazer e refazer para criações, exclusões, movimentos, modificações, camadas, terreno, cavernas, água e materiais, de modo que `undo` / `redo` permitem ao modelo reverter com segurança as próprias alterações (independente do Ctrl+Z do Dungeondraft). Um lote, uma dispersão, uma sala construída, um prefab ou uma exclusão de vários elementos contam como um único passo. `checkpoint` marca um ponto no histórico e `rollback_checkpoint` desfaz tudo desde então em uma única chamada; `list_checkpoints` mostra os que estão abertos. O histórico guarda os últimos 200 passos.

Cada elemento é referenciado por um `id` inteiro; as chamadas de criação e de listagem devolvem ids que você passa para as de edição. As coordenadas estão em woxels (pixels do mundo): chame `get_status` para obter `map_center`. Descubra os assets com `list_assets`.

## Instalação

**Pré-requisitos:** [Dungeondraft](https://dungeondraft.net/) (testado nas versões com Godot 3.4.2), Python **3.10+** e um cliente MCP (por exemplo Claude Code ou Claude Desktop). No Windows, `python` / `.venv\Scripts\` substituem os caminhos `python3` / `.venv/bin/` mostrados abaixo.

### 1. Instalar o mod no Dungeondraft

1. Na tela de título do Dungeondraft, abra **Mods** e anote (ou defina) a sua pasta de mods.
2. Copie `mod/dungeondraft-mcp-bridge/` para essa pasta.
3. Ative **MCP Bridge** na lista de mods e abra ou crie um mapa.

Ao carregar, você deve ver no log do Dungeondraft:

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` é a versão do protocolo da ponte, que aumenta conforme comandos são adicionados (`ping` também a informa).

### 2. Instalar o servidor MCP

Instale em um venv dedicado (o Python do sistema da maioria das distribuições é gerenciado externamente, e um venv mantém o ponto de entrada limpo e estável). Execute na raiz do repositório:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

Isso instala o pacote `dungeondraft_mcp` e cria o ponto de entrada `.venv/bin/dungeondraft-mcp`.

Na primeira vez que o Dungeondraft inicia com o mod, o mod grava um token aleatório na pasta de usuário do Dungeondraft (ao lado de `config.ini`); o servidor o lê de lá, então nada precisa ser configurado. Só um programa capaz de ler os seus arquivos pode controlar o seu mapa. O painel **MCP Bridge**, nas Configurações (Settings) do Dungeondraft, mostra o que a ponte está fazendo e reúne os controles que são só seus: **Pause AI edits** (pausa as edições da IA; as ferramentas somente de leitura continuam funcionando), **Undo the assistant's last step** (desfaz o último passo do assistente) e o interruptor do token.

### 3. Verificar se a ponte funciona

Com o DD aberto e um mapa carregado, execute o validador comando a comando. Ele monta uma pequena cena descartável perto do centro do mapa, imprime PASS/FAIL para cada comando e depois apaga ou desfaz o que fez:

```bash
.venv/bin/python server/test_bridge.py
```

Se ele não conseguir conectar *e* você nunca tiver visto a linha `listening` no log do DD, o sandbox bloqueou `TCP_Server`: veja a nota sobre a alternativa em PROTOCOL.md.

As verificações de layout são geometria pura e têm testes próprios, que não precisam nem do Dungeondraft nem de um mapa:

```bash
.venv/bin/python -m unittest discover -s server/tests
```

Para *ver* algo sendo construído (uma pequena sala mobiliada deixada no mapa, em vez de um teste que limpa o que fez), execute `.venv/bin/python server/demo_build.py`.

### 4. Conectar o seu cliente MCP

Registre o ponto de entrada pelo **caminho absoluto**. Para o **Claude Code**:

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

Reinicie o Claude Code para o servidor carregar (`/mcp` mostra o status e as ferramentas). Ou adicione à configuração de um cliente (por exemplo o `claude_desktop_config.json` do Claude Desktop), usando o caminho absoluto:

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

Depois peça ao modelo coisas como *“qual é o estado do meu mapa do Dungeondraft?”* ou *“liste alguns assets de objetos e coloque uma cadeira no meio do mapa”*.

## Configuração

Estas variáveis de ambiente configuram o **servidor Python** (defina-as onde o cliente MCP o inicia). Os valores padrão coincidem com os do mod, então você só precisa delas se alterar as constantes `HOST` / `PORT` do mod em `mcp_bridge.gd`: as duas metades precisam concordar.

| Variável de ambiente | Padrão | Finalidade |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | host ao qual o servidor se conecta |
| `DD_BRIDGE_PORT` | `8787` | porta à qual o servidor se conecta (igual ao `PORT` do mod) |

## Estendendo

Adicionar um recurso é simétrico: um handler de cada lado.

1. **Mod** (`mod/.../scripts/tools/mcp_bridge.gd`): adicione um `case` ao `match` em `_safe_dispatch()` e uma função `_my_command(req)` que retorne `_ok(...)` / `_err(...)`.
2. **Servidor** (`server/dungeondraft_mcp/server.py`): adicione um `@mcp.tool()` que chame `bridge.request("my_command", ...)`.

Um recurso que é apenas cálculo sobre o que o mod já informa vive somente no servidor: as verificações de layout (`spatial.py`), `scatter_objects` e `batch` não têm handler próprio no mod.

Bons próximos alvos: excluir um nível, agrupar um lote de edições em um único passo de desfazer e áreas ocupadas que sigam o contorno de um sprite em vez do seu retângulo envolvente.

Pull requests são bem-vindos. Leia antes o [CONTRIBUTING.md](CONTRIBUTING.md) (em inglês): ele lista o que uma mudança completa deve incluir e como testá-la.

### Ciclo de desenvolvimento (leia antes de iterar no mod)

Algumas coisas vão te atrapalhar se você não souber delas de antemão:

- **Editar o mod e editar o servidor têm caminhos de recarga diferentes.** As mudanças de GDScript em `mcp_bridge.gd` entram em vigor quando você **recarrega o mod dentro do Dungeondraft** (ou reinicia o DD). Definições `@mcp.tool()` novas ou alteradas em `server.py` só aparecem depois que **o processo do servidor MCP reinicia**; no Claude Code isso significa `/mcp` → reconectar `dungeondraft` (ou reiniciar o cliente). A instalação é editável (`pip install -e`), então você nunca reinstala; apenas reinicia o processo. Adicionar uma *ferramenta nova* exige **as duas** recargas (o mod para o handler, o servidor para o registro da ferramenta); mudar o comportamento de um handler *existente* exige apenas recarregar o mod.

- **Sonde o Dungeondraft antes de escrever um handler.** `native_describe` lista os métodos e as propriedades de um objeto vivo, `native_get` os lê e `native_call` experimenta um método, tudo sem recarregar o mod, então você pode descobrir antes o que uma API realmente faz. Dentro de um handler continua não havendo depurador: quando uma chamada ao DD se comporta de forma inesperada (por exemplo, um setter que não tem efeito), **devolva o estado intermediário na resposta**, isto é, guarde os valores de antes e de depois em um campo de depuração, para que uma única recarga mostre onde um valor muda.

- **Um script que não compila derruba o Dungeondraft** ao carregar o mod, sem nenhuma mensagem. Verifique antes, offline, se `mcp_bridge.gd` compila; a receita está em PROTOCOL.md (“Mod compile errors crash Dungeondraft”).

## Estrutura

```
mod/dungeondraft-mcp-bridge/   o mod do Dungeondraft (copie para a pasta de mods do DD)
  mcp_bridge.ddmod             manifesto
  scripts/tools/mcp_bridge.gd  servidor TCP + handlers de comandos
server/                        o servidor MCP em Python
  dungeondraft_mcp/server.py   definições das ferramentas MCP
  dungeondraft_mcp/spatial.py  verificações de layout (geometria pura sobre um snapshot do mapa)
  dungeondraft_mcp/layout.py   arranjo de mobília e paredes compartilhadas
  dungeondraft_mcp/floorplan.py  alcançabilidade das salas a partir de paredes e portas
  dungeondraft_mcp/scene.py    verificações de chão nu e de fontes de luz
  dungeondraft_mcp/mapfile.py  leitor de .dungeondraft_map e edição do manifesto de pacotes
  dungeondraft_mcp/packs.py    índice, tags e busca de pacotes de assets (offline)
  dungeondraft_mcp/uvtt_import.py  parser da exportação Universal VTT e plano de reconstrução
  dungeondraft_mcp/bridge_client.py  cliente TCP/JSON
  tests/                       testes unitários offline (não precisam do Dungeondraft)
  test_bridge.py               teste de fumaça independente, comando a comando
  test_features.py             validador ao vivo das ferramentas da 0.3.0
  demo_build.py                constrói uma sala de exemplo visível e a deixa no mapa
tools/check_gdscript.py        verificação de compilação offline do mod (Godot 3.4.2 padrão)
.github/workflows/ci.yml       testes unitários e verificação de compilação a cada push
PROTOCOL.md                    protocolo de comunicação + notas de implementação (em inglês)
```

## Créditos

Construído sobre a [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/). O motor é o Godot 3.4.2, então o GDScript usa os nomes de classes de rede do Godot 3.

Os slots de terreno expandidos, a edição de paredes, a importação de imagens, a água, os materiais, as ferramentas de níveis e camadas, as verificações de layout, o acesso nativo e à interface, a edição de luzes e textos, a dispersão e os lotes foram feitos primeiro por Li-Jiaquan em [dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge), um fork deste projeto, e trazidos de volta de lá.

As paredes compartilhadas, o desfazer de requisições inteiras e as exclusões reversíveis, as verificações de planta e de cena, a leitura de pacotes de assets, a inspeção de arquivos de mapa, a importação Universal VTT, a ferramenta de arranjo e o Range do próprio Dungeondraft para as luzes foram feitos primeiro por thekannen em [battlemap-mcp](https://github.com/thekannen/battlemap-mcp), uma reconstrução do protótipo de junho deste projeto, e foram derivados de novo aqui a partir do próprio código do Dungeondraft.

## Licença

[MIT](LICENSE).
