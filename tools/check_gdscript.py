#!/usr/bin/env python3
"""Compile-check the bridge mod offline, the way Dungeondraft compiles it.

A GDScript parse error crashes Dungeondraft natively when the mod loads, with
no message. Dungeondraft compiles a mod script by prepending
`var Global = {}` and `var Script=null` and calling GDScript.reload(), so this
does the same with a stock Godot 3.4.2 headless binary and reports the error
with the line number mapped back to the file.

The stock binary runs a static type pass that Dungeondraft's release build
skips: `var x := some_untyped_call()` fails there as "the variable type can't
be inferred" but is fine in Dungeondraft. Inferred declarations are relaxed to
plain assignments in the checked copy, which keeps every real parse error.

Usage:
    tools/check_gdscript.py [--godot PATH] [script.gd]

The binary is the `Godot_v3.4.2-stable_linux_headless.64` build from the
godotengine/godot 3.4.2-stable release (any 3.4.x headless build works); pass
its path with --godot or the GODOT_HEADLESS environment variable.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = REPO / "mod" / "dungeondraft-mcp-bridge" / "scripts" / "tools" / "mcp_bridge.gd"
PREAMBLE = "var Global = {}\nvar Script=null\n\n"
PREAMBLE_LINES = PREAMBLE.count("\n")

CHECK_SCRIPT = """extends SceneTree
func _init():
\tvar f = File.new()
\tf.open("res://mod_copy.gd", File.READ)
\tvar src = f.get_as_text()
\tf.close()
\tvar gd = GDScript.new()
\tgd.source_code = src
\tvar err = gd.reload()
\tprint("RELOAD_RESULT=", err)
\tquit()
"""


def relaxed_copy(source: str) -> str:
    """The script as Dungeondraft sees it, minus the inferred declarations."""
    relaxed = re.sub(r"^(\s*var\s+\w+)\s*:=", r"\1 =", source, flags=re.M)
    return PREAMBLE + relaxed


def check(script: Path, godot: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "project.godot").write_text("config_version=4\n")
        (project / "check.gd").write_text(CHECK_SCRIPT)
        (project / "mod_copy.gd").write_text(relaxed_copy(script.read_text()))
        run = subprocess.run(
            [str(godot), "--path", str(project), "-s", "check.gd"],
            capture_output=True, text=True, timeout=300,
        )
    output = run.stdout + run.stderr
    match = re.search(r"RELOAD_RESULT=(\d+)", output)
    if match is None:
        print(output)
        print("check did not run to completion", file=sys.stderr)
        return 2
    if match.group(1) == "0":
        print(f"{script.name}: compiles")
        return 0
    for line in output.splitlines():
        line = re.sub(r"built-in:(\d+)",
                      lambda m: f"{script.name}:{int(m.group(1)) - PREAMBLE_LINES}", line)
        if "ERROR" in line or "at:" in line:
            print(line.strip())
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("script", nargs="?", default=str(DEFAULT_SCRIPT), type=Path)
    parser.add_argument("--godot", default=os.environ.get("GODOT_HEADLESS"),
                        help="path to a Godot 3.4.x headless binary (or set GODOT_HEADLESS)")
    args = parser.parse_args()
    if not args.godot:
        print("no Godot binary: pass --godot or set GODOT_HEADLESS", file=sys.stderr)
        return 2
    return check(Path(args.script), Path(args.godot))


if __name__ == "__main__":
    sys.exit(main())
