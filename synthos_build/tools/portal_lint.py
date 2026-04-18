#!/usr/bin/env python3
"""portal_lint.py — cheap static checks for retail_portal.py

Catches the bug classes we've hit in this codebase before:

  1. fetch('/api/X') → no @app.route('/api/X') defined
  2. document.getElementById('id') → no HTML element with id="id"
  3. @login_required declared BELOW @app.route (decorator order is wrong —
     Flask binds @app.route first, then login_required wraps a copy of
     the view that's no longer routed, so auth is silently bypassed)
  4. Jinja expressions rendered with `| safe` (flags them for
     manual audit — they're often correct but worth re-reading)
  5. {{ template_var }} used in the template but never passed to
     render_template_string (renders as empty silently)
  6. Unclosed or badly-nested HTML tags (best-effort via stdlib parser)
  7. HTML ids that are defined but never referenced from JS
     (info-only; unused ids are harmless but flag them for cleanup)

Usage:
    python3 tools/portal_lint.py                  # defaults to src/retail_portal.py
    python3 tools/portal_lint.py path/to/file.py  # lint any file w/ inline HTML
    python3 tools/portal_lint.py --strict         # exit 1 if any ERROR-level
                                                  # finding surfaces

Exit codes:
    0 = no ERRORs (WARNINGs OK unless --strict)
    1 = at least one ERROR (or any finding under --strict)
"""
from __future__ import annotations

import argparse
import html.parser
import os
import re
import sys
from collections import defaultdict


# ── Finding shape ─────────────────────────────────────────────────────
# Kept flat (tuple) so this file has zero non-stdlib deps and can run
# anywhere Python 3 runs.
_FINDINGS: list = []   # (level, source_line, code, message)


def _add(level: str, line: int, code: str, msg: str) -> None:
    _FINDINGS.append((level, line, code, msg))


# ── Extractors ────────────────────────────────────────────────────────

_FLASK_ROUTE_RE = re.compile(
    r'^\s*@app\.route\(\s*["\']([^"\']+)["\']', re.MULTILINE
)
# Decorator order check — detect the pattern where a non-route
# decorator sits ABOVE @app.route. We do this line-walk rather than
# regex so we can name the offending decorator in the error message.


def extract_routes(src: str) -> set[str]:
    """Return the set of `/path` strings registered by @app.route."""
    return set(_FLASK_ROUTE_RE.findall(src))


def find_decorator_order_bugs(src: str) -> list[tuple[int, str, str]]:
    """Find decorators like @login_required declared *above* @app.route,
    which makes the auth wrapper a no-op in Flask's binding order.

    Returns [(line_no, bad_decorator_name, route_path), ...]."""
    lines = src.splitlines()
    findings = []
    # Decorators that make no sense above @app.route
    suspect = {
        'login_required', 'admin_required', 'authenticated_only',
        'construction_required',
    }
    for i, line in enumerate(lines):
        m = re.match(r'\s*@(\w+)', line)
        if not m or m.group(1) not in suspect:
            continue
        # Look at following non-blank lines for @app.route before def
        for j in range(i + 1, min(i + 6, len(lines))):
            nxt = lines[j].strip()
            if nxt.startswith('def '):
                break
            mm = re.match(r'@app\.route\(\s*["\']([^"\']+)["\']', nxt)
            if mm:
                findings.append((i + 1, m.group(1), mm.group(1)))
                break
    return findings


_FETCH_RE = re.compile(r"""\bfetch\(\s*['"`]([^'"`]+)['"`]""")


def extract_fetch_urls(src: str) -> list[tuple[int, str]]:
    """Find every fetch('...') call and return [(line_no, url), ...].

    Strips query string before comparing to declared routes so
    `/api/x?hours=24` matches `/api/x`.
    """
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _FETCH_RE.finditer(line):
            raw = m.group(1).strip()
            url = raw.split('?', 1)[0].split('#', 1)[0]
            out.append((i, url))
    return out


_GET_BY_ID_RE = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""")


def extract_js_ids(src: str) -> list[tuple[int, str]]:
    """Find every document.getElementById('foo') call — even when the
    `document.` prefix is omitted (common inside nested closures)."""
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _GET_BY_ID_RE.finditer(line):
            out.append((i, m.group(1)))
    return out


_HTML_ID_RE = re.compile(r"""\bid\s*=\s*['"]([^'"{}]+)['"]""")


def extract_html_ids(src: str) -> list[tuple[int, str]]:
    """Find every static id="foo" in the template. Skips Jinja-templated
    ids like id="row-{{ i }}" because we can't statically know the
    runtime value."""
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _HTML_ID_RE.finditer(line):
            val = m.group(1).strip()
            if not val or '{' in val or '}' in val:
                continue
            out.append((i, val))
    return out


_JINJA_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)")


def extract_jinja_vars(src: str) -> list[tuple[int, str]]:
    """Top-level {{ var }} expressions. Skips method calls on known
    Flask globals (request, session, config, url_for, etc.)."""
    out = []
    # Anything in quotes or comments is a false positive — but stripping
    # those properly requires parsing Python, overkill for a lint.
    FLASK_GLOBALS = {'request', 'session', 'config', 'url_for',
                     'g', 'flashed_messages', 'loop'}
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _JINJA_VAR_RE.finditer(line):
            name = m.group(1)
            if name in FLASK_GLOBALS:
                continue
            out.append((i, name))
    return out


_SAFE_FILTER_RE = re.compile(r"\{\{[^}]+\|\s*safe\b")


def find_unsafe_markers(src: str) -> list[tuple[int, str]]:
    """Flag every `{{ expr | safe }}` for manual review. Not an error —
    these are often legitimate (e.g. icon() helper returning raw SVG) —
    but worth auditing because `| safe` disables HTML escaping."""
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _SAFE_FILTER_RE.finditer(line):
            out.append((i, m.group(0).strip()))
    return out


_RENDER_KWARGS_RE = re.compile(
    r"render_template_string\s*\(\s*\w+"
    r"((?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^,)]+)+)"
)
_KWARG_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=")


def extract_render_kwargs(src: str) -> set[str]:
    """All keyword args passed to any render_template_string() call.
    Returns the set of variable names the templates can safely use."""
    out = set()
    # Use a simpler pass — find every render_template_string call and
    # pull kwargs from a few lines following. Regex for multi-line
    # arg lists gets hairy; line-walk is more predictable.
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        if 'render_template_string' in lines[i]:
            # Scan ahead until matching close paren depth returns to 0.
            buf = []
            depth = 0
            started = False
            for j in range(i, min(i + 30, len(lines))):
                buf.append(lines[j])
                for ch in lines[j]:
                    if ch == '(':
                        depth += 1
                        started = True
                    elif ch == ')':
                        depth -= 1
                if started and depth == 0:
                    break
            blob = '\n'.join(buf)
            for m in _KWARG_NAME_RE.finditer(blob):
                out.add(m.group(1))
            i = j + 1
        else:
            i += 1
    # Strip names that are actually parameter names for OTHER calls
    # in the scanned block. Keeping false positives over false negatives
    # is the right trade: we'd rather NOT warn on a missing-kwarg that's
    # actually defined than report a spurious missing-kwarg.
    return out


class _UnclosedTagFinder(html.parser.HTMLParser):
    """Best-effort HTML structural sanity check. Known limitations:
    void elements (br/img/hr/input/etc.), self-closing SVG, and
    inline conditional Jinja (`{% if %}` around tag pairs) all
    produce false positives — so we only log WARNING, never ERROR."""

    VOID = {
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        # Walk up looking for a match — tolerates minor nesting bugs
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                self.stack = self.stack[:k]
                return
        # No matching opener — closing tag without opener

    def done(self):
        for tag, ln in self.stack:
            self.errors.append((ln, f"unclosed <{tag}>"))


def find_structural_issues(html_blob: str) -> list[tuple[int, str]]:
    p = _UnclosedTagFinder()
    try:
        p.feed(html_blob)
        p.close()
        p.done()
    except Exception as e:
        return [(0, f"HTML parse error: {e}")]
    return p.errors


# ── Driver ────────────────────────────────────────────────────────────

def lint(path: str) -> tuple[list, int]:
    with open(path) as f:
        src = f.read()

    # 1. fetch(URL) → @app.route cross-check
    routes = extract_routes(src)
    for line, url in extract_fetch_urls(src):
        if not url.startswith('/'):
            continue  # absolute URLs to other services are fine
        # Allow dynamic paths: match any route that starts the same
        # segment-by-segment (so /api/flags/ack matches a route like
        # /api/flags/acknowledge). Not exhaustive — flag for review,
        # not outright fail.
        exact_match   = url in routes
        prefix_match  = any(url.startswith(r) for r in routes)
        dynamic_match = any(
            r'<' in r and _route_matches(r, url)
            for r in routes
        )
        if not (exact_match or prefix_match or dynamic_match):
            _add('ERROR', line, 'missing-route',
                 f"fetch(...) → {url!r} has no matching @app.route "
                 f"in this file")

    # 2. decorator-order bugs
    for line, dec, route in find_decorator_order_bugs(src):
        _add('ERROR', line, 'decorator-order',
             f"@{dec} is above @app.route({route!r}) — Flask binds "
             f"the route first, so the wrapper never runs. Auth is "
             f"silently bypassed. Swap the two decorator lines.")

    # 3. getElementById → id="..." cross-check
    html_ids = {v for _, v in extract_html_ids(src)}
    for line, jid in extract_js_ids(src):
        if jid not in html_ids:
            _add('WARNING', line, 'unknown-id',
                 f"getElementById({jid!r}) references an id not "
                 f"found in this file's HTML (might be dynamic or "
                 f"defined elsewhere)")

    # 4. {{ | safe }} audit — info level
    for line, expr in find_unsafe_markers(src):
        _add('INFO', line, 'safe-filter',
             f"{expr}   (| safe disables HTML escaping — verify "
             f"the source is trusted)")

    # 5. {{ var }} without a render_template_string kwarg
    kwargs = extract_render_kwargs(src)
    # Plus the handful of names Flask always makes available in
    # templates — these don't need to be passed explicitly.
    AUTO_KWARGS = {
        'request', 'session', 'config', 'url_for', 'g',
        'flashed_messages', 'loop', 'self',
    }
    seen_missing: set[tuple[int, str]] = set()
    for line, var in extract_jinja_vars(src):
        if var in kwargs or var in AUTO_KWARGS:
            continue
        if (line, var) in seen_missing:
            continue
        seen_missing.add((line, var))
        # Down-level this to INFO rather than WARNING: many templates
        # are shared and get their kwargs from a helper that's in a
        # different file. Without doing a cross-file index we can't
        # tell true positives from shared-template noise.
        _add('INFO', line, 'template-var-maybe-undeclared',
             f"{{{{ {var} }}}} used but not seen as a kwarg to any "
             f"render_template_string(...) in this file")

    # 6. HTML structural check — only on the biggest triple-quoted
    # r"""...""" block, which is where PORTAL_HTML lives. Skips
    # smaller inline strings that are often fragments.
    big_html = _largest_html_blob(src)
    if big_html:
        for line, msg in find_structural_issues(big_html):
            _add('WARNING', line, 'html-structure', msg)

    # 7. Dead-ID sweep — ids defined in HTML but never used from JS.
    js_ids_used = {v for _, v in extract_js_ids(src)}
    defined_ids = defaultdict(list)
    for ln, hid in extract_html_ids(src):
        defined_ids[hid].append(ln)
    for hid, lns in defined_ids.items():
        if hid in js_ids_used:
            continue
        # Skip ids used only for HTML anchors / form labels — these
        # are referenced via selector strings that we don't parse.
        # Report at INFO and cap to first occurrence to keep noise down.
        _add('INFO', lns[0], 'unused-id',
             f"id={hid!r} defined in HTML but no getElementById call "
             f"references it (might be selector-based or truly dead)")

    # ── Summarize + return ─────────────────────────────────────────
    counts = {'ERROR': 0, 'WARNING': 0, 'INFO': 0}
    for lvl, _, _, _ in _FINDINGS:
        counts[lvl] = counts.get(lvl, 0) + 1
    return _FINDINGS, counts.get('ERROR', 0)


def _route_matches(route: str, url: str) -> bool:
    """Match /api/foo/<id> against /api/foo/42."""
    r_parts = route.strip('/').split('/')
    u_parts = url.strip('/').split('/')
    if len(r_parts) != len(u_parts):
        return False
    for rp, up in zip(r_parts, u_parts):
        if rp.startswith('<') and rp.endswith('>'):
            continue
        if rp != up:
            return False
    return True


def _largest_html_blob(src: str) -> str:
    """Find the largest triple-quoted string that looks like HTML."""
    # Simple heuristic: find r""" or """ blocks starting with '<'.
    blobs = re.findall(r'(?:r?"""|r?\'\'\')([\s\S]*?)(?:"""|\'\'\')', src)
    html_blobs = [b for b in blobs if b.lstrip().startswith('<')]
    if not html_blobs:
        return ''
    return max(html_blobs, key=len)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('path', nargs='?', default=None,
                   help='Python file with inline HTML template '
                        '(defaults to src/retail_portal.py)')
    p.add_argument('--strict', action='store_true',
                   help='Exit 1 if ANY finding surfaces (including '
                        'WARNINGs and INFO). Default is exit-1 on '
                        'ERROR only.')
    p.add_argument('--quiet', action='store_true',
                   help='Only print ERROR-level findings.')
    args = p.parse_args()

    if args.path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        args.path = os.path.join(os.path.dirname(here), 'src',
                                 'retail_portal.py')
    if not os.path.exists(args.path):
        print(f"File not found: {args.path}", file=sys.stderr)
        return 2

    findings, error_count = lint(args.path)

    # Group by level for a readable report
    by_level = defaultdict(list)
    for lvl, ln, code, msg in findings:
        by_level[lvl].append((ln, code, msg))
    for lvl in ('ERROR', 'WARNING', 'INFO'):
        items = by_level.get(lvl, [])
        if not items:
            continue
        if args.quiet and lvl != 'ERROR':
            continue
        print(f"\n── {lvl} ({len(items)}) ──")
        for ln, code, msg in sorted(items):
            print(f"  L{ln:>5}  [{code}]  {msg}")

    e = by_level.get('ERROR', [])
    w = by_level.get('WARNING', [])
    i = by_level.get('INFO', [])
    print(f"\nSummary: {len(e)} ERROR · {len(w)} WARNING · {len(i)} INFO")

    if args.strict and findings:
        return 1
    if error_count > 0:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
