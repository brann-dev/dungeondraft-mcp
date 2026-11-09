# dungeondraft-mcp

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · Français · [Español](README.es.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md)

> Ce document est une traduction du [README.md](README.md) anglais, qui fait référence en cas de différence. Si vous repérez une erreur de traduction, n'hésitez pas à ouvrir une issue ou une PR.

Un serveur MCP qui permet à un LLM (Claude Code, Claude Desktop, etc.) de piloter une **instance de Dungeondraft en cours d'exécution** (placer des objets, tracer des murs, inspecter la carte) via l'API de modding GDScript de Dungeondraft.

Dungeondraft n'a pas de serveur intégré auquel se connecter ; ce projet se compose donc de **deux moitiés** qui communiquent par un socket local :

```
Claude / client MCP
      │  MCP (stdio)
      ▼
server/   ── serveur MCP en Python  (ce dépôt)
      │  JSON délimité par des sauts de ligne, sur TCP  (127.0.0.1:8787)
      ▼
mod/      ── mod Dungeondraft  (GDScript, s'exécute dans DD)
      │  appels à l'API de modding
      ▼
   la carte ouverte  (Objects, Walls, Levels, …)
```

Le mod ouvre un serveur TCP à l'intérieur de Dungeondraft et l'interroge à chaque image depuis le hook `update(delta)` ; côté Python, chaque capacité est exposée comme un outil MCP et les appels sont transmis en JSON. Voir [PROTOCOL.md](PROTOCOL.md) (en anglais) pour le format des échanges.

> **État : fonctionnel.** Validé de bout en bout avec Dungeondraft sur **Godot 3.4.2** : le TCP brut fonctionne depuis le bac à sable de modding, aucune solution de repli n'est nécessaire. 75 outils couvrant consultation, création, modification, terrain, eau, matériaux, niveaux, calques, contrôles d'agencement, sélection, capture, caméra, annulation et cycle de vie de la carte, plus un accès de dernier recours à l'API et à l'interface de Dungeondraft lui-même (voir ci-dessous).

## Ce que l'IA peut faire

- **Inspecter :** `ping`, `get_status`, `list_levels`, `list_elements`, `get_element`, `list_asset_categories`, `list_assets` (avec recherche par sous-chaîne).
- **Créer :** `place_object`, `draw_wall`, `draw_path`, `add_light`, `add_portal`, `add_roof`, `add_text`, `place_pattern` (sols à motif : planches, carrelage, briques, dessinés sous les objets), `build_room` (une boucle de murs et le sol correspondant sur un même tracé, comme le tracé combiné de l'interface), `import_image` (intègre un PNG/JPG/WEBP du disque comme objet : accessoires personnalisés, aides de jeu ou battlemap entière en fond ; les pixels sont stockés dans la carte), `scatter_objects` (un bosquet ou un champ de gravats en un seul appel, à l'écart des murs et des portes), `set_trace_image` (une image de référence affichée par-dessus la carte pour la décalquer).
- **Terrain :** `set_terrain_slot`, `fill_terrain` (tout le niveau), `fill_region` (un rectangle ou un polygone, par exemple le sol d'une pièce), `paint_terrain` (un pinceau doux), `paint_path` (un trait continu et lisse le long d'une polyligne : routes et sentiers). Emplacements 0 à 3 par défaut ; `set_terrain_slot(..., expand=True)` active les emplacements 4 à 7 du niveau.
- **Eau et matériaux :** `draw_water` (lacs, rivières et douves en rectangle ou en polygone, avec rivage et dégradé de profondeur ; `erase` découpe des îles) et `draw_material` (lave, glace, pavés et autres textures du pinceau de matériaux, sur un rectangle ou un polygone et sur le calque choisi).
- **Grottes :** `dig_cave` creuse des cavernes et des tunnels le long d'un tracé (le pinceau de grotte : il dégage un sol dans la roche, avec parois rocheuses et débris automatiques ; `dig=false` rebouche ; teintes optionnelles du sol et des parois), `clear_caves` efface tout le calque des grottes.
- **Modifier :** `move_element`, `modify_object` (échelle, rotation, couleur, ombre, miroir, blocage de la lumière), `modify_wall` (redessine ou restyle un mur ; ses portes et fenêtres restent en place et suivent leur segment), `modify_light`, `modify_text`, `duplicate_object`, `delete_element`, `select_elements`, `clear_selection`.
- **Niveaux :** `add_level`, `set_level`, `rename_level`, `clone_level` (copie un étage entier pour démarrer le suivant), `reorder_levels`, `compare_levels` (affiche un autre étage en transparence sous l'étage courant pour aligner les éléments), `configure_level` (lumière ambiante pour les scènes de crépuscule et de nuit, éclairage activé ou non, visibilité et fondu du terrain, rivage de l'eau).
- **Calques :** `list_layers`, `set_element_layer` (décide de ce qui recouvre quoi : un tapis sous une table, une frondaison au-dessus d'un sentier), `add_layer` ; `place_object` et `import_image` acceptent aussi un `layer`.
- **Contrôles d'agencement :** `get_asset_footprint` (la taille réelle d'un asset, avant de choisir une échelle), `check_object_placement` (tester un emplacement : traverse-t-il un mur, bloque-t-il une porte, chevauche-t-il quelque chose ?), `validate_layout` (auditer un niveau meublé), ainsi que `set_spatial_region` / `remove_spatial_region` / `list_spatial_regions` pour les pièces, les zones à garder dégagées et les sols protégés. Les outils d'objets acceptent `check=True` pour refuser d'emblée un mauvais placement ; les modifications de terrain, de grotte, d'eau et de matériau refusent de toucher une région `protected`.
- **Cycle de vie de la carte :** `get_current_map` (chemin de la carte ouverte et sauvegarde en cours ou non), `save_map` (enregistre dans le fichier courant, comme le bouton Save de DD) et `open_map` (bascule vers une autre carte enregistrée ; il répond avant la fin du chargement de la nouvelle carte, interrogez donc `get_current_map` ; les modifications non enregistrées sont abandonnées sans avertissement, appelez donc `save_map` d'abord).
- **Voir :** `screenshot` (fenêtre courante) et `export_map` (rendu propre de la carte entière, `grid=False` pour exporter sans la grille) renvoient des images, ce qui permet au modèle de regarder son propre travail et d'itérer. `set_grid` affiche ou masque la grille de l'éditeur. `export_uvtt` écrit un fichier Universal VTT `.dd2vtt` (image + murs, portes, lumières) à importer dans Foundry ou Roll20.
- **Caméra :** `get_camera`, `set_camera`, `focus_element` (centre sur un élément), `fit_elements` (cadre un groupe), `reset_camera` (retrouve une vue perdue) : orientez la vue avant un `screenshot` pour inspecter un endroit précis, comme une porte percée dans un mur.
- **Accès natif :** `native_targets`, `native_describe`, `native_get`, `native_set`, `native_call` atteignent les objets de Dungeondraft lui-même, et `ui_tree` / `ui_action` lisent et actionnent ses panneaux et boîtes de dialogue, pour tout ce qui n'a pas d'outil dédié (fondu doux du terrain, taille d'un pinceau, préréglage de biome). C'est un dernier recours : il contourne l'annulation, enregistrez donc d'abord.
- **Lot :** `batch` exécute jusqu'à 100 appels d'outils en une seule requête, pour de longues séries de placements indépendants.
- **Annulation :** le pont tient ses propres piles d'annulation et de rétablissement pour les créations, déplacements, modifications, calques, terrain, grottes, eau et matériaux ; `undo` / `redo` permettent ainsi au modèle de revenir de façon fiable sur ses propres changements (indépendamment du Ctrl+Z de Dungeondraft ; `delete_element` n'est pas réversible).

Chaque élément est désigné par un `id` entier ; les appels de création et de liste renvoient des ids que l'on repasse aux appels de modification. Les coordonnées sont en woxels (pixels du monde) : appelez `get_status` pour obtenir `map_center`. Les assets se trouvent avec `list_assets`.

## Installation

**Prérequis :** [Dungeondraft](https://dungeondraft.net/) (testé sur les versions Godot 3.4.2), Python **3.10+** et un client MCP (par exemple Claude Code ou Claude Desktop). Sous Windows, `python` / `.venv\Scripts\` remplacent les chemins `python3` / `.venv/bin/` indiqués ci-dessous.

### 1. Installer le mod dans Dungeondraft

1. Sur l'écran titre de Dungeondraft, ouvrez **Mods** et notez (ou définissez) votre dossier de mods.
2. Copiez `mod/dungeondraft-mcp-bridge/` dans ce dossier.
3. Activez **MCP Bridge** dans la liste des mods, puis ouvrez ou créez une carte.

Au chargement, vous devriez voir dans le journal de Dungeondraft :

```
[mcp-bridge] listening on 127.0.0.1:8787 (protocol vN)
```

`N` est la version de protocole du pont, qui augmente à mesure que des commandes sont ajoutées (`ping` l'indique aussi).

### 2. Installer le serveur MCP

Installez-le dans un venv dédié (le Python système de la plupart des distributions est géré de l'extérieur ; un venv garde le point d'entrée propre et stable). Depuis la racine du dépôt :

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./server
```

Cela installe le paquet `dungeondraft_mcp` et crée le point d'entrée `.venv/bin/dungeondraft-mcp`.

### 3. Vérifier que le pont fonctionne

Avec DD ouvert et une carte chargée, lancez le validateur commande par commande. Il construit une petite scène jetable près du centre de la carte, affiche PASS/FAIL pour chaque commande, puis supprime ou annule ce qu'il a fait :

```bash
.venv/bin/python server/test_bridge.py
```

S'il n'arrive pas à se connecter *et* que vous n'avez jamais vu la ligne `listening` dans le journal de DD, le bac à sable a bloqué `TCP_Server` : voir la note sur la solution de repli dans PROTOCOL.md.

Les contrôles d'agencement sont de la géométrie pure et ont leurs propres tests, qui ne nécessitent ni Dungeondraft ni carte :

```bash
.venv/bin/python -m unittest discover -s server/tests
```

Pour *voir* quelque chose se construire (une petite pièce meublée laissée sur la carte, plutôt qu'un test qui nettoie derrière lui), lancez `.venv/bin/python server/demo_build.py`.

### 4. Brancher votre client MCP

Enregistrez le point d'entrée par son **chemin absolu**. Pour **Claude Code** :

```bash
claude mcp add dungeondraft -s user -- "$PWD/.venv/bin/dungeondraft-mcp"
```

Redémarrez Claude Code pour que le serveur se charge (`/mcp` affiche son état et ses outils). Vous pouvez aussi l'ajouter à la configuration d'un client (par exemple `claude_desktop_config.json` pour Claude Desktop), toujours avec le chemin absolu :

```json
{
  "mcpServers": {
    "dungeondraft": {
      "command": "/abs/path/to/dungeondraft-mcp/.venv/bin/dungeondraft-mcp"
    }
  }
}
```

Demandez ensuite au modèle, par exemple : *« Quel est l'état de ma carte Dungeondraft ? »* ou *« Liste quelques assets d'objets et pose une chaise au milieu de la carte. »*

## Configuration

Ces variables d'environnement configurent le **serveur Python** (définissez-les là où le client MCP le lance). Les valeurs par défaut correspondent à celles du mod ; vous n'en avez besoin que si vous modifiez les constantes `HOST` / `PORT` du mod dans `mcp_bridge.gd` : les deux moitiés doivent concorder.

| Variable d'environnement | Valeur par défaut | Rôle |
| --- | --- | --- |
| `DD_BRIDGE_HOST` | `127.0.0.1` | hôte auquel le serveur se connecte |
| `DD_BRIDGE_PORT` | `8787` | port auquel le serveur se connecte (identique au `PORT` du mod) |

## Étendre

Ajouter une capacité est symétrique : un gestionnaire de chaque côté.

1. **Mod** (`mod/.../scripts/tools/mcp_bridge.gd`) : ajoutez un `case` au `match` de `_safe_dispatch()` et une fonction `_my_command(req)` qui renvoie `_ok(...)` / `_err(...)`.
2. **Serveur** (`server/dungeondraft_mcp/server.py`) : ajoutez un `@mcp.tool()` qui appelle `bridge.request("my_command", ...)`.

Une capacité qui n'est qu'un calcul sur ce que le mod rapporte déjà vit uniquement dans le serveur : les contrôles d'agencement (`spatial.py`), `scatter_objects` et `batch` n'ont pas de gestionnaire propre dans le mod.

Prochaines cibles intéressantes : supprimer un niveau, regrouper un lot de modifications en une seule étape d'annulation, et des emprises qui suivent le contour d'un sprite plutôt que son rectangle englobant.

Les pull requests sont les bienvenues. Lisez d'abord [CONTRIBUTING.md](CONTRIBUTING.md) (en anglais) : il indique ce qu'une modification complète doit contenir et comment la tester.

### Boucle de développement (à lire avant d'itérer sur le mod)

Quelques points vous joueront des tours si vous ne les connaissez pas d'avance :

- **Modifier le mod et modifier le serveur ne se rechargent pas de la même façon.** Les changements GDScript dans `mcp_bridge.gd` prennent effet quand vous **rechargez le mod dans Dungeondraft** (ou redémarrez DD). Les définitions `@mcp.tool()` nouvelles ou modifiées dans `server.py` n'apparaissent qu'après le **redémarrage du processus du serveur MCP** ; dans Claude Code, cela signifie `/mcp` → reconnecter `dungeondraft` (ou redémarrer le client). L'installation est en mode éditable (`pip install -e`) : vous ne réinstallez jamais, vous relancez simplement le processus. Ajouter un *nouvel outil* demande **les deux** rechargements (le mod pour le gestionnaire, le serveur pour l'enregistrement de l'outil) ; changer le comportement d'un gestionnaire *existant* ne demande que le rechargement du mod.

- **Sondez Dungeondraft avant d'écrire un gestionnaire.** `native_describe` liste les méthodes et les propriétés d'un objet vivant, `native_get` les lit et `native_call` essaie une méthode, le tout sans recharger le mod : vous pouvez donc d'abord découvrir ce qu'une API fait réellement. À l'intérieur d'un gestionnaire, il n'y a toujours pas de débogueur : quand un appel à DD se comporte de façon inattendue (par exemple un setter qui ne tient pas), **renvoyez l'état intermédiaire dans la réponse**, c'est-à-dire stockez les valeurs avant et après dans un champ de débogage, pour qu'un seul rechargement montre où une valeur change.

- **Un script qui ne compile pas fait planter Dungeondraft** au chargement du mod, sans aucun message. Vérifiez d'abord hors ligne que `mcp_bridge.gd` compile ; la marche à suivre est dans PROTOCOL.md (« Mod compile errors crash Dungeondraft »).

## Organisation

```
mod/dungeondraft-mcp-bridge/   le mod Dungeondraft (à copier dans le dossier de mods de DD)
  mcp_bridge.ddmod             manifeste
  scripts/tools/mcp_bridge.gd  serveur TCP + gestionnaires de commandes
server/                        le serveur MCP en Python
  dungeondraft_mcp/server.py   définitions des outils MCP
  dungeondraft_mcp/spatial.py  contrôles d'agencement (géométrie pure sur un instantané de la carte)
  dungeondraft_mcp/bridge_client.py  client TCP/JSON
  tests/                       tests unitaires hors ligne (sans Dungeondraft)
  test_bridge.py               test de fumée autonome, commande par commande
  demo_build.py                construit une pièce d'exemple visible et la laisse en place
PROTOCOL.md                    protocole d'échange + notes d'implémentation (en anglais)
```

## Crédits

Construit à partir de la [Dungeondraft Modding API](https://megasploot.github.io/DungeondraftModdingAPI/). Le moteur est Godot 3.4.2 ; le GDScript utilise donc les noms de classes réseau de Godot 3.

Les emplacements de terrain étendus, l'édition des murs, l'import d'images, l'eau, les matériaux, les outils de niveaux et de calques, les contrôles d'agencement, l'accès natif et à l'interface, l'édition des lumières et du texte, la dispersion et les lots ont d'abord été réalisés par Li-Jiaquan dans [dungeondraft-mcp-bridge](https://github.com/Li-Jiaquan/dungeondraft-mcp-bridge), un fork de ce projet, puis repris ici.

## Licence

[MIT](LICENSE).
