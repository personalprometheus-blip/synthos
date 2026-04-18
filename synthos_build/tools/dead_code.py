#!/usr/bin/env python3
"""Dead-code scan across src/, agents/, tools/.

Wraps `vulture`: finds unused imports, functions, classes, variables,
and attributes. Vulture uses confidence levels (1–100) to rank
findings — higher = more likely to be truly dead.

Read-only. Doesn't modify any source file. Safe to run any time.

Run:
    cd ~/synthos/synthos_build && python3 tools/dead_code.py
    python3 tools/dead_code.py --min-confidence 80
    python3 tools/dead_code.py src/                 # narrow target
    python3 tools/dead_code.py --paths              # show scan paths and exit

Default scan targets:  src/ agents/ tools/
Default confidence:    70   (balanced; 100 = only certain dead code)
"""
import sys
import argparse
import subprocess
from pathlib import Path

_HERE         = Path(__file__).resolve().parent
_PROJECT_DIR  = _HERE.parent

_DEFAULT_TARGETS = ['src', 'agents', 'tools']

# Patterns vulture can't usefully lint even when they look unused —
# Flask routes, event handlers, subclass hooks, dunder methods, etc.
# Keep this conservative; prefer trimming false positives inline with
# `# noqa` or `_ = foo` over growing this list.
_EXCLUDE_NAMES = [
    # Flask decorators mark routes as "used" by the framework, but
    # vulture can't see that.
    'before_request', 'after_request', 'teardown_*',
    # Pytest discovers by name
    'test_*', 'Test*',
]


def _have_vulture() -> bool:
    try:
        subprocess.run(
            [sys.executable, '-m', 'vulture', '--version'],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        'targets', nargs='*',
        help=f"paths to scan (default: {' '.join(_DEFAULT_TARGETS)})",
    )
    p.add_argument(
        '--min-confidence', type=int, default=70,
        help='minimum confidence 0-100 (default: 70)',
    )
    p.add_argument(
        '--paths', action='store_true',
        help='print resolved scan paths and exit',
    )
    args = p.parse_args()

    targets = args.targets or _DEFAULT_TARGETS
    resolved = [str(_PROJECT_DIR / t) for t in targets]

    if args.paths:
        for r in resolved:
            print(r)
        return 0

    if not _have_vulture():
        print(
            "vulture is not installed for this Python "
            f"({sys.executable}).\n"
            "Install with:  pip3 install --break-system-packages vulture",
            file=sys.stderr,
        )
        return 127

    ignore = ','.join(_EXCLUDE_NAMES)
    cmd = [
        sys.executable, '-m', 'vulture',
        *resolved,
        '--min-confidence', str(args.min_confidence),
        '--ignore-names', ignore,
        '--sort-by-size',
    ]

    # Stream output straight to the terminal. Vulture's default output
    # is `path:line: message` which most editors can click on.
    try:
        rc = subprocess.run(cmd, cwd=str(_PROJECT_DIR)).returncode
    except KeyboardInterrupt:
        return 130

    # Vulture returns 3 when it finds dead code. That's not a tool
    # error, it's a normal result. Treat as 0 so CI chaining with
    # ruff/radon works cleanly.
    return 0 if rc in (0, 3) else rc


if __name__ == '__main__':
    sys.exit(main())
