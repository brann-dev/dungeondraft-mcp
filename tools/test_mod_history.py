#!/usr/bin/env python3
"""Test the bridge mod's shared-history code offline, against a stand-in for Dungeondraft's History.

The mod records its steps in Dungeondraft's own undo history and drives
History.Undo() / Redo() itself, which is unforgiving: a call out of bounds or
inside Dungeondraft's three-frame lock breaks undo for the user. This runs the
mod's real code (recording, groups, undo/redo, checkpoints) in a stock Godot
3.4.2 headless binary with a History that keeps Dungeondraft's rules and
counts every such call; tools/mod_history_test.gd holds the cases.

The mod script is loaded the way tools/check_gdscript.py compiles it: with
Dungeondraft's preamble, and with inferred declarations relaxed for the stock
binary's stricter type pass.

Usage:
    tools/test_mod_history.py [--godot PATH]

The binary is the same one check_gdscript.py needs; pass its path with
--godot or the GODOT_HEADLESS environment variable.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from check_gdscript import DEFAULT_SCRIPT, relaxed_copy

HARNESS = Path(__file__).resolve().parent / "mod_history_test.gd"


def run(godot: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "project.godot").write_text("config_version=4\n")
        (project / "mod_copy.gd").write_text(relaxed_copy(DEFAULT_SCRIPT.read_text()))
        (project / "harness.gd").write_text(HARNESS.read_text())
        done = subprocess.run(
            [str(godot), "--path", str(project), "-s", "harness.gd"],
            capture_output=True, text=True, timeout=300,
        )
    output = done.stdout + done.stderr
    summary = re.search(r"HISTORY_TESTS passed=(\d+) failed=(\d+)", output)
    # A script error inside a test aborts that test without failing a check,
    # so any error line fails the run too.
    errors = [line for line in output.splitlines() if "SCRIPT ERROR" in line or line.startswith("ERROR")]
    if summary is None or errors or summary.group(2) != "0":
        print(output)
        print("shared-history tests FAILED" if summary else "shared-history tests did not run to completion",
              file=sys.stderr)
        return 1
    print(f"shared-history tests: {summary.group(1)} checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--godot", default=os.environ.get("GODOT_HEADLESS"),
                        help="path to a Godot 3.4.x headless binary (or set GODOT_HEADLESS)")
    args = parser.parse_args()
    if not args.godot:
        print("no Godot binary: pass --godot or set GODOT_HEADLESS", file=sys.stderr)
        return 2
    godot = Path(args.godot).resolve()
    if not godot.is_file():
        print(f"no Godot binary at {godot}", file=sys.stderr)
        return 2
    return run(godot)


if __name__ == "__main__":
    sys.exit(main())
