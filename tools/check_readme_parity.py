#!/usr/bin/env python3
"""Check that each translated README has the same structure as README.md.

Translations drift when the English README changes and one language is
missed. Prose is translated, so only the parts that must stay identical are
compared: heading levels in order, the number of bullets, numbered items,
table rows and bold spans, the set of inline-code spans (tool and parameter
names stay in English), the set of external links, and every fenced block
byte for byte except the first (the architecture diagram) and the last (the
folder tree), whose labels are translated.

    tools/check_readme_parity.py            # every README.*.md
    tools/check_readme_parity.py README.ja.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "README.md"


def fenced(text: str) -> list[str]:
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, flags=re.M | re.S)


def without_fences(text: str) -> str:
    return re.sub(r"^```[^\n]*\n.*?^```", "", text, flags=re.M | re.S)


def shape(text: str) -> dict:
    body = without_fences(text)
    return {
        "headings": re.findall(r"^(#{1,6}) ", body, flags=re.M),
        "bullets": len(re.findall(r"^\s*- ", body, flags=re.M)),
        "numbered": len(re.findall(r"^\s*\d+\. ", body, flags=re.M)),
        "table_rows": len(re.findall(r"^\|", body, flags=re.M)),
        # A bold span may wrap onto the next line in the English README.
        "bold": len(re.findall(r"\*\*[^*]+?\*\*", body, flags=re.S)),
        "code": set(re.findall(r"`([^`\n]+)`", body)),
        "links": set(re.findall(r"\]\((https?://[^)\s]+)\)", body)),
    }


def compare(reference: Path, translation: Path) -> list[str]:
    ref, tr = reference.read_text(), translation.read_text()
    a, b = shape(ref), shape(tr)
    problems = []
    if a["headings"] != b["headings"]:
        problems.append(f"heading levels differ: {len(a['headings'])} vs {len(b['headings'])} "
                        f"({''.join(a['headings'])} vs {''.join(b['headings'])})")
    for key in ("bullets", "numbered", "table_rows", "bold"):
        if a[key] != b[key]:
            problems.append(f"{key}: {a[key]} in README.md, {b[key]} in {translation.name}")
    for key in ("code", "links"):
        missing, extra = sorted(a[key] - b[key]), sorted(b[key] - a[key])
        if missing:
            problems.append(f"{key} missing: {missing[:12]}{' ...' if len(missing) > 12 else ''}")
        if extra:
            problems.append(f"{key} extra: {extra[:12]}{' ...' if len(extra) > 12 else ''}")
    fa, fb = fenced(ref), fenced(tr)
    if len(fa) != len(fb):
        problems.append(f"fenced blocks: {len(fa)} vs {len(fb)}")
    else:
        for i, (x, y) in enumerate(zip(fa, fb)):
            if i in (0, len(fa) - 1):
                continue   # the diagram and the folder tree are translated
            if x != y:
                problems.append(f"fenced block {i + 1} differs")
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or sorted(REPO.glob("README.*.md"))
    bad = 0
    for path in targets:
        problems = compare(REFERENCE, path)
        status = "ok" if not problems else f"{len(problems)} problem(s)"
        print(f"{path.name}: {status}")
        for problem in problems:
            print(f"  - {problem}")
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
