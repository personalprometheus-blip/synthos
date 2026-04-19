#!/usr/bin/env python3
"""
diff_baseline.py — compare a fresh capture against a stored baseline JSON.

Used by the C8 refactor parity check: after modifying retail_news_agent.py,
re-capture the baseline with capture_baseline.py and run this tool to verify
byte-identical classifier output.

The top-level `captured_at` field is the wall-clock time of the capture
run itself (not part of the classifier output) and is ignored by default.

Usage:
  python3 tests/news_baseline/diff_baseline.py cycle_01.json /tmp/fresh_cycle_01.json

Exit 0 if identical (ignoring captured_at), 1 if a semantic diff is found,
2 on file-load error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Keys to ignore at the TOP level of the baseline object only (they reflect
# capture-run metadata, not classifier output).
IGNORED_TOP_KEYS = {"captured_at"}


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"invalid JSON in {path}: {e}")


def _strip_top_level(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in IGNORED_TOP_KEYS}


def _diff(path: str, a: Any, b: Any, out: list[str]) -> None:
    if type(a) is not type(b):
        out.append(f"  {path}: type mismatch ({type(a).__name__} vs {type(b).__name__})")
        return
    if isinstance(a, dict):
        only_a = sorted(set(a) - set(b))
        only_b = sorted(set(b) - set(a))
        for k in only_a:
            out.append(f"  {path}.{k}: only in A")
        for k in only_b:
            out.append(f"  {path}.{k}: only in B")
        for k in sorted(set(a) & set(b)):
            _diff(f"{path}.{k}", a[k], b[k], out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"  {path}: length {len(a)} vs {len(b)}")
            return
        for i, (ea, eb) in enumerate(zip(a, b)):
            _diff(f"{path}[{i}]", ea, eb, out)
    else:
        if a != b:
            out.append(f"  {path}: {a!r} != {b!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("baseline", type=Path, help="stored baseline JSON")
    parser.add_argument("candidate", type=Path, help="freshly-captured JSON to compare against baseline")
    parser.add_argument("--include-captured-at", action="store_true",
                        help="also compare the captured_at metadata field")
    args = parser.parse_args()

    base = load(args.baseline)
    cand = load(args.candidate)

    if not args.include_captured_at:
        base = _strip_top_level(base)
        cand = _strip_top_level(cand)

    out: list[str] = []
    _diff("$", base, cand, out)

    if not out:
        print(f"IDENTICAL — {args.baseline.name} matches {args.candidate.name}")
        if not args.include_captured_at:
            print("(captured_at metadata ignored; use --include-captured-at to compare)")
        return 0

    print(f"DIFFERENCES found between {args.baseline.name} and {args.candidate.name}:")
    for line in out:
        print(line)
    print(f"\n{len(out)} difference(s) — refactor is NOT parity-clean")
    return 1


if __name__ == "__main__":
    sys.exit(main())
