"""Icon library — wire-style inline SVG strings for the portal UI.

Uniform style for every glyph:
    viewBox      = "0 0 24 24"
    fill         = "none"
    stroke       = "currentColor"   (parent CSS controls color)
    stroke-width = 1.5
    stroke-linecap / linejoin = "round"

Rationale for keeping this a plain Python module (not a template include
or separate .svg files):
    - The portal renders via render_template_string on a single HTML
      string, so asset imports from disk need an extra path/cache step
      we don't want to add mid-pipeline.
    - A Python dict lookup is ~200ns and gives us typo-safety (KeyError
      beats silently rendering nothing when a template typos an icon
      name).
    - Templates can reach this via the `icons` dict passed into
      render_template_string — see usage block at bottom.

Adding a new icon:
    1. Draw the glyph at 24×24. Wire style only — no fills, no gradients.
    2. Add to _RAW below as "name": "<path.../>".
    3. That's it — `icon('name')` works immediately.
"""

# Raw path content for each glyph. _wrap() adds the <svg> outer element
# uniformly so individual entries stay readable.
_RAW = {
    # ── NAV / HEADER ──────────────────────────────────────────────────
    "bell": (
        '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>'
        '<path d="M13.73 21a2 2 0 0 1-3.46 0"/>'
    ),
    "user": (
        '<circle cx="12" cy="8" r="4"/>'
        '<path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>'
    ),
    "settings": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
    ),

    # ── STATUS / FEEDBACK ─────────────────────────────────────────────
    # Triangular warning glyph with an internal bang. The glyph is
    # color-agnostic — render_warning_red / render_warning_amber wrap
    # it with the right CSS class so the stroke inherits the chosen
    # accent color.
    "warn_triangle": (
        '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
        '<line x1="12" y1="9" x2="12" y2="13"/>'
        '<line x1="12" y1="17" x2="12.01" y2="17"/>'
    ),

    # ── TRADE / LIFECYCLE ─────────────────────────────────────────────
    "check":  '<polyline points="20 6 9 17 4 12"/>',
    "x_mark": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "clock": (
        '<circle cx="12" cy="12" r="10"/>'
        '<polyline points="12 6 12 12 16 14"/>'
    ),

    # ── MISC ──────────────────────────────────────────────────────────
    "refresh": (
        '<polyline points="23 4 23 10 17 10"/>'
        '<polyline points="1 20 1 14 7 14"/>'
        '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'
    ),
    "info": (
        '<circle cx="12" cy="12" r="10"/>'
        '<line x1="12" y1="16" x2="12" y2="12"/>'
        '<line x1="12" y1="8" x2="12.01" y2="8"/>'
    ),
}


def _wrap(path_content: str, extra_class: str = "", size: int = 24) -> str:
    """Produce a complete <svg> element with the uniform wire-icon style.
    Size default matches the viewBox so templates get pixel-perfect
    output without needing to size explicitly."""
    cls_attr = f' class="icon {extra_class}"' if extra_class else ' class="icon"'
    return (
        f'<svg{cls_attr} width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f'{path_content}'
        '</svg>'
    )


def icon(name: str, cls: str = "", size: int = 24) -> str:
    """Return the inline SVG string for a named icon.

    Unknown names produce an empty string (templates stay functional;
    missing icons are visibly missing rather than 500-ing the page).
    Style classes can be chained via `cls`:

        icon('warn_triangle', cls='icon-warn-red')
    """
    raw = _RAW.get(name)
    if raw is None:
        return ""
    return _wrap(raw, extra_class=cls, size=size)


# ── CONVENIENCE WRAPPERS ──────────────────────────────────────────────
# The two warning variants the plan calls out (Phase 1.2). They apply
# specific CSS classes that retail_portal.py's stylesheet paints
# red / amber; falling back to currentColor if the class isn't styled.

def warn_red(size: int = 16) -> str:
    """⚠️ Red — cancelled-protective, critical alerts.

    Pair with the CSS:
        .icon-warn-red { color: var(--pink, #ff4b6e); }
    """
    return icon("warn_triangle", cls="icon-warn-red", size=size)


def warn_amber(size: int = 16) -> str:
    """⚠️ Amber — warnings / attention-needed but non-blocking.

    Pair with the CSS:
        .icon-warn-amber { color: var(--amber, #f59e0b); }
    """
    return icon("warn_triangle", cls="icon-warn-amber", size=size)


# ── USAGE NOTES ───────────────────────────────────────────────────────
# In retail_portal.py's render_template_string call, add:
#
#     from icons import icon, warn_red, warn_amber
#     ...
#     render_template_string(PORTAL_HTML, ..., icon=icon,
#                            warn_red=warn_red, warn_amber=warn_amber)
#
# Templates then reference with the `safe` filter so HTML isn't escaped:
#
#     {{ icon('bell') | safe }}
#     {{ warn_red() | safe }} Cancelled (protective)
#     {{ icon('check', cls='trade-fill-check') | safe }}
