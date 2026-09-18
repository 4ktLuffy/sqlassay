#!/usr/bin/env python
"""Every number in the documents is diffed against an artifact, or declared.

``make readme-check`` regenerates a set of figures from ``reports/`` and diffs
them. That leaves the rest -- an arXiv id, a count quoted in passing, a
percentage a reader would assume was measured -- true but unchecked, and a
reader cannot tell the two kinds apart.

This audits the gap. A number passes if either:

  * ``readme_check`` regenerated it from an artifact, or
  * it appears in ``NUMBERS.md`` on a row that names the command reproducing
    it, or a source it was read from.

A manifest row is a declaration, not a loophole: it has to name the individual
figure and where it came from. The point is that a reader should be able to
tell, for any number in the README, whether a machine checks it or a person
claimed it.

Exit 0 if every number is accounted for, 1 otherwise.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "LIMITATIONS.md")
MANIFEST = ROOT / "NUMBERS.md"

#: Numbers that carry no claim: version strings, dates, list markers, and the
#: small integers that appear as table scaffolding or prose ("one of three").
_IGNORED = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "1.0", "2.0", "0.0",
    "2022", "2023", "2024", "2025", "2026", "2027",
    "06", "11", "27", "16",  # date fragments in 2024-06-27 / 2025-11-06
}

_NUMBER = re.compile(r"(?<![\w.$-])\d[\d,]*(?:\.\d+)?%?(?![\w-])")

#: A manifest row counts when it names a source: a command in backticks, or a
#: markdown link to a paper or file. A row with neither is an undeclared claim,
#: which is what this file exists to catch.
_MANIFEST_ROW = re.compile(r"^\|.*(?:`[^`]+`|\[[^\]]+\]\([^)]+\)).*\|", re.MULTILINE)


def canon(number: str) -> str:
    """Normalise a figure so `1,465` and `1465` are the same claim.

    Digit grouping and a trailing comma are typography, not meaning. Treating
    them as distinct made the audit demand a declaration for a number that was
    already machine-checked three lines above.
    """
    return number.rstrip(",").replace(",", "")


def machine_checked() -> set[str]:
    """The figures ``readme_check`` regenerates, as they appear in the docs."""
    spec = importlib.util.spec_from_file_location("readme_check", ROOT / "tools" / "readme_check.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot import tools/readme_check.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: @dataclass resolves its own module through
    # sys.modules, and fails with an opaque AttributeError without this.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    out: set[str] = set()
    for check in module.build_checks():
        out.update(canon(n) for n in _NUMBER.findall(check.expected))
        # A check's pattern pins where the figure sits; the literal digits in
        # it are equally covered.
        out.update(canon(n) for n in _NUMBER.findall(check.pattern))
    return out


def declared() -> set[str]:
    """Figures declared in NUMBERS.md on a row carrying a command or source."""
    if not MANIFEST.exists():
        return set()
    text = MANIFEST.read_text(encoding="utf-8")
    out: set[str] = set()
    for row in _MANIFEST_ROW.findall(text):
        out.update(canon(n) for n in _NUMBER.findall(row))
    return out


def main() -> int:
    covered = machine_checked() | declared() | {canon(n) for n in _IGNORED}
    missing: dict[str, list[str]] = {}

    for name in DOCS:
        path = ROOT / name
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Fenced commands and link targets are not claims.
            if line.lstrip().startswith(("```", "sqlassay ", "python ", "make ", "$", "gh ")):
                continue
            for raw in _NUMBER.findall(line):
                if canon(raw) in covered:
                    continue
                missing.setdefault(canon(raw), []).append(f"{name}:{lineno}")

    if missing:
        print(f"{len(missing)} number(s) in the documents are neither checked nor declared:\n")
        for number, where in sorted(missing.items()):
            print(f"  {number:>12}   {', '.join(where[:3])}")
        print(
            "\nAdd a row to NUMBERS.md naming the command or source for each, "
            "or make it a figure readme_check regenerates."
        )
        return 1

    print(f"every number in {' and '.join(DOCS)} is machine-checked or declared in NUMBERS.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
