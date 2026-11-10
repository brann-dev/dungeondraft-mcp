# Contributing

Thanks for wanting to improve dungeondraft-mcp. Forks and pull requests are
welcome, and so are bug reports and translation fixes.

This page lists what a change needs before it can be merged. For how the
project works, read [README.md](README.md) first, then
[PROTOCOL.md](PROTOCOL.md) for the commands and for what has been learned about
Dungeondraft's API.

## Before you start

- **A small fix, or a new command in the existing style:** open a pull request.
- **A larger change** (a new group of tools, a change to a default, a new
  dependency): open an issue first and describe the idea, so the design is
  agreed before you write it.

## Set up

Follow [Setup](README.md#setup) in the README: install the server in a venv
with `pip install -e`, copy the mod into Dungeondraft's mods folder and enable
it.

Two things to know while you work:

- Use a scratch map. The test script and your own experiments draw on whatever
  map is open.
- Dungeondraft loads the copy of the mod in its mods folder, not the one in
  this repo. Copy `mod/dungeondraft-mcp-bridge/` there again after every
  change, then restart Dungeondraft or click Reload Mods.

## What a complete change includes

A new or changed command touches both halves and the docs:

1. **Mod** (`mod/dungeondraft-mcp-bridge/scripts/tools/mcp_bridge.gd`): the
   handler, and its entry in `_safe_dispatch()`.
2. **Server** (`server/dungeondraft_mcp/server.py`): the `@mcp.tool()`. Its
   docstring is what the AI model reads, so say what the tool is for, what each
   argument means and what comes back.
3. **Protocol version:** raise `PROTOCOL_VERSION` in the mod when a command is
   added, or when its parameters or results change.
4. **PROTOCOL.md:** a row in the command table, and an implementation note for
   anything about Dungeondraft you had to find out.
5. **README.md:** the tool list and the tool count. The README has six
   translations (`README.*.md`); update them too, or say in the pull request
   that they still need it.
6. **Undo:** an edit the user could want to reverse should hand back an undo
   op (see `_ok_op` and `_apply_op` in the mod). The op becomes a step in
   Dungeondraft's own history, so the user's Ctrl+Z runs it too, at any
   moment: it must not depend on which level is showing or which tool is
   active. PROTOCOL.md ("Shared history") explains how the steps are kept.
7. **Tests:** a check in `server/test_bridge.py` if the command can clean up
   after itself, and unit tests in `server/tests/` for logic that runs without
   Dungeondraft. A change to the history code also runs
   `tools/test_mod_history.py` (offline, with the Godot binary of the compile
   check) and `server/test_history.py` (live).

## Rules for the mod

The mod runs inside Dungeondraft, on Godot 3.4.2, where a mistake can take the
whole editor down:

- **Compile-check the script before loading it.** A script that does not
  compile crashes Dungeondraft when the mod loads, with no message. PROTOCOL.md
  ("Mod compile errors crash Dungeondraft") has the recipe.
- **Validate input before calling Dungeondraft.** GDScript here has no
  try/catch. A Dungeondraft method that throws returns `null` and only writes
  to Dungeondraft's log, and a call with the wrong number of arguments silently
  does nothing. Check arguments first and check results afterwards.
- **Do what Dungeondraft's own tools do.** Where you can, call the methods the
  editor's tools call and use their defaults, so the result saves, reloads and
  stays selectable. A bare API call often skips bookkeeping the editor does;
  PROTOCOL.md has several examples.
- **Add a class-level `var` only if you need one.** Reload Mods crashes
  Dungeondraft when a script's member variables have changed, so such a change
  needs a full restart. Say so in the pull request.
- **Read only Godot values from Dungeondraft's objects.** A property that
  holds a plain C# object (`Global.Preferences` is one) crashes Dungeondraft
  when GDScript asks for it. And leave `Editor.History` to the history code:
  its `Undo()` and `Redo()` check nothing themselves.

## Rules for the whole project

- **The bridge stays local.** It listens on `127.0.0.1` only. A change that
  opens it to the network, downloads or runs code, or adds binaries will be
  declined.
- **No new runtime dependency without discussion.** The server depends only on
  `mcp`.
- **One topic per pull request**, and no unrelated reformatting.
- **Match the code around you:** its naming, its comments (they explain why,
  usually a Dungeondraft quirk), tabs in GDScript, four spaces in Python.

## Test it

1. Run the offline tests: `.venv/bin/python -m unittest discover -s server/tests`
2. Compile-check the mod.
3. With Dungeondraft open on a scratch map, run
   `.venv/bin/python server/test_bridge.py`. Every line should say PASS.
4. Try your command by hand: normal use, bad input (it should return an error,
   not break the bridge), `undo` and `redo`, and a save and reopen if it
   changes what is stored in the map.

In the pull request, say what you ran and on what: your Dungeondraft version,
your operating system, and which of the steps above. A screenshot helps for
anything visual.

**AI-assisted work is welcome**; much of this project was written that way. The
same rule applies to it: you have run the change in Dungeondraft yourself.
Generated code that nobody has run will be closed.

## Reporting a bug

Open an issue with:

- your Dungeondraft version and operating system;
- the bridge's protocol version (the `ping` tool reports it);
- the tool you called, its arguments, and the reply;
- anything in Dungeondraft's log from that moment, especially lines with
  `Unhandled Exception`.

## Translations

The translated READMEs have not been reviewed by native speakers. Corrections
are very welcome: fix the wording in the file for your language and open a pull
request. Tool and parameter names stay in English.

## License

The project is under the [MIT license](LICENSE). By contributing, you agree
that your contribution is released under it. Contributors are credited in the
commit history and, for larger pieces, in the README.
