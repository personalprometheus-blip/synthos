#!/usr/bin/env python3
"""Fast lint scan across src/, agents/, tools/.

Wraps `ruff check`: finds bugs, unused imports, undefined names,
style drift, and dozens of other quality issues. Rust-backed, runs
in a couple hundred milliseconds on the whole codebase.

Read-only by default. Doesn't modify any source file. Does NOT run
`ruff format --write` — pass --fix explicitly if you want safe
auto-fixes (ruff is conservative about what counts as safe).

Run:
    cd ~/synthos/synthos_build && python3 tools/lint.py
    python3 tools/lint.py --fix                # apply safe auto-fixes
    python3 tools/lint.py --fix --unsafe-fixes # include risky fixes
    python3 tools/lint.py src/retail_portal.py # narrow target

Default scan targets:  src/ agents/ tools/
Default rule set:      ruff's defaults (E + F) — pycodestyle errors
                       and pyflakes. Expand via `--select` if wanted.
"""
import sys
import argparse
import subprocess
from pathlib import Path

_HERE         = Path(__file__).resolve().parent
_PROJECT_DIR  = _HERE.parent

_DEFAULT_TARGETS = ['src', 'agents', 'tools']

# Files/dirs ruff should skip. Ruff already ignores .venv, __pycache__,
# build/, dist/ etc. by default; we only need to add project-specific
# opt-outs here.
_EXCLUDE_GLOBS = [
    'data/',
    'docs/',
    # Generated/cached outputs from the diagnostic tools — not source.
    '**/*_rendered.*',
]


def _have_ruff() -> bool:
    try:
        subprocess.run(
            [sys.executable, '-m', 'ruff', '--version'],
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
        '--select',
        help="override default rule set (e.g. 'E,F,B,UP')",
    )
    p.add_argument(
        '--fix', action='store_true',
        help='apply safe auto-fixes (writes files)',
    )
    p.add_argument(
        '--unsafe-fixes', action='store_true',
        help='with --fix, also apply fixes ruff considers risky',
    )
    p.add_argument(
        '--format',
        choices=['concise', 'full', 'json', 'github'],
        default='concise',
        help='output format (default: concise — one line per finding)',
    )
    args = p.parse_args()

    if args.unsafe_fixes and not args.fix:
        print("--unsafe-fixes only makes sense with --fix", file=sys.stderr)
        return 2

    targets = args.targets or _DEFAULT_TARGETS
    resolved = [str(_PROJECT_DIR / t) for t in targets]

    if not _have_ruff():
        print(
            "ruff is not installed for this Python "
            f"({sys.executable}).\n"
            "Install with:  pip3 install --break-system-packages ruff",
            file=sys.stderr,
        )
        return 127

    cmd = [
        sys.executable, '-m', 'ruff', 'check',
        *resolved,
        '--output-format', args.format,
    ]
    for g in _EXCLUDE_GLOBS:
        cmd += ['--extend-exclude', g]
    if args.select:
        cmd += ['--select', args.select]
    if args.fix:
        cmd.append('--fix')
    if args.unsafe_fixes:
        cmd.append('--unsafe-fixes')

    try:
        return subprocess.run(cmd, cwd=str(_PROJECT_DIR)).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
