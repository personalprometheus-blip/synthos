#!/usr/bin/env python3
"""Cyclomatic complexity + maintainability readout via radon.

Two reports:
  - **cc** (cyclomatic complexity) — rank functions by how many
    branches they have. Anything rated C or worse (>= 11) usually
    warrants a second look.
  - **mi** (maintainability index) — 0–100 per file, higher = easier
    to maintain. A/B/C letter grades. Files rated C have high risk.

Read-only. Doesn't modify any source file.

Run:
    cd ~/synthos/synthos_build && python3 tools/complexity.py
    python3 tools/complexity.py --min A              # show everything
    python3 tools/complexity.py --min D              # only worst offenders
    python3 tools/complexity.py --mi                 # only maintainability
    python3 tools/complexity.py --cc                 # only cyclomatic
    python3 tools/complexity.py src/retail_portal.py

Default scan targets:  src/ agents/ tools/
Default min grade:     C  (complexity >= 11 / maintainability < 20)
"""
import sys
import argparse
import subprocess
from pathlib import Path

_HERE         = Path(__file__).resolve().parent
_PROJECT_DIR  = _HERE.parent

_DEFAULT_TARGETS = ['src', 'agents', 'tools']


def _have_radon() -> bool:
    try:
        subprocess.run(
            [sys.executable, '-m', 'radon', '--version'],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


def _run_cc(targets: list[str], min_grade: str, show_all: bool) -> int:
    print(f"\n── Cyclomatic complexity (functions rated {min_grade} or worse) "
          f"──")
    cmd = [
        sys.executable, '-m', 'radon', 'cc',
        *targets,
        '--min', min_grade,
        '--show-complexity',
        '--average',
    ]
    if show_all:
        cmd.append('--total-average')
    return subprocess.run(cmd, cwd=str(_PROJECT_DIR)).returncode


def _run_mi(targets: list[str], min_grade: str) -> int:
    print(f"\n── Maintainability index (files rated {min_grade} or worse) ──")
    cmd = [
        sys.executable, '-m', 'radon', 'mi',
        *targets,
        '--min', min_grade,
        '--show',
    ]
    return subprocess.run(cmd, cwd=str(_PROJECT_DIR)).returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        'targets', nargs='*',
        help=f"paths to scan (default: {' '.join(_DEFAULT_TARGETS)})",
    )
    p.add_argument(
        '--min', default='C',
        choices=['A', 'B', 'C', 'D', 'E', 'F'],
        help="minimum grade to report (default: C — only moderate+)",
    )
    p.add_argument(
        '--cc', action='store_true',
        help='run only cyclomatic-complexity report',
    )
    p.add_argument(
        '--mi', action='store_true',
        help='run only maintainability-index report',
    )
    p.add_argument(
        '--average', action='store_true',
        help='include project-wide average in cc output',
    )
    args = p.parse_args()

    targets = args.targets or _DEFAULT_TARGETS
    resolved = [str(_PROJECT_DIR / t) for t in targets]

    if not _have_radon():
        print(
            "radon is not installed for this Python "
            f"({sys.executable}).\n"
            "Install with:  pip3 install --break-system-packages radon",
            file=sys.stderr,
        )
        return 127

    # Default: run both. Flags narrow it down.
    run_cc = args.cc or not (args.cc or args.mi)
    run_mi = args.mi or not (args.cc or args.mi)

    rc = 0
    try:
        if run_cc:
            r = _run_cc(resolved, args.min, args.average)
            rc = r if r else rc
        if run_mi:
            r = _run_mi(resolved, args.min)
            rc = r if r else rc
    except KeyboardInterrupt:
        return 130
    return rc


if __name__ == '__main__':
    sys.exit(main())
