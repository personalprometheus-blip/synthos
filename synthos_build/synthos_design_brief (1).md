# Synthos — UI Design Brief
**Version 1.1 · March 2026**

All visual components across the Synthos ecosystem (Customer Portal, Command Console, Audit Page, Shortcuts, Installer) must follow this spec. When building new pages, panels, or widgets, refer here first.

---

## Philosophy

**Dark glass trading terminal.** Every surface should feel like a professional financial tool — precise, dense, and confident. No gradients for decoration. No rounded corners on everything. No bright backgrounds. Inspiration: Bloomberg Terminal meets modern SaaS dark mode.

Three rules:
1. **Color communicates state** — teal = healthy/active, pink = danger/halt, amber = warning/pending, purple = AI/intelligence
2. **Glow = alive** — pulsing dots and edge glows indicate live data. Static = stale or inactive
3. **Mono for data, sans for prose** — JetBrains Mono for IDs, timestamps, values, code. Inter for labels, descriptions, navigation

---

## Color Tokens

```css
:root {
  /* Backgrounds */
  --bg:       #0a0c14;    /* page background — near black with blue tint */
  --surface:  #111520;    /* card/panel background */
  --surface2: #161b28;    /* nested element background */

  /* Borders */
  --border:   rgba(255,255,255,0.07);   /* default card border */
  --border2:  rgba(255,255,255,0.12);   /* hover / active border */

  /* Text */
  --text:     rgba(255,255,255,0.88);   /* primary text */
  --muted:    rgba(255,255,255,0.35);   /* secondary / labels */
  --dim:      rgba(255,255,255,0.18);   /* tertiary / timestamps */

  /* Brand colors */
  --teal:     #00f5d4;                  /* healthy, active, profit, confirmed */
  --teal2:    rgba(0,245,212,0.12);     /* teal tint background */

  --pink:     #ff4b6e;                  /* danger, kill switch, loss, error */
  --pink2:    rgba(255,75,110,0.12);    /* pink tint background */

  --purple:   #7b61ff;                  /* AI/Claude, intelligence, improvement */
  --purple2:  rgba(123,97,255,0.12);    /* purple tint background */

  --amber:    #ffb347;                  /* warning, pending, supervised mode */
  --amber2:   rgba(255,179,71,0.12);    /* amber tint background */

  /* Typography */
  --mono:     'JetBrains Mono', monospace;
  --sans:     'Inter', sans-serif;
}
```

**Font imports:**
```html
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
```

---

## Color Semantics

| Color | Use |
|-------|-----|
| Teal `#00f5d4` | Online, healthy, profit, BUY signal, confirmed, active state, API connected |
| Pink `#ff4b6e` | Kill switch engaged, loss, SELL signal, error, offline, danger zone |
| Amber `#ffb347` | Supervised mode, pending approval, warning, stale data, watch signal |
| Purple `#7b61ff` | AI activity, Claude reasoning, intelligence tab, improvement agent, autonomous mode |
| White 88% | Primary readable text |
| White 35% | Labels, section titles, secondary info |
| White 18% | Timestamps, IDs, tertiary info |

---

## Typography

```css
/* Section labels — always uppercase, always muted */
.section-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
}

/* Stat values — large, colored, glowing */
.stat-val {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.5px;
}

/* Body text */
body {
  font-size: 14px;
  line-height: 1.5;
  font-family: var(--sans);
}

/* Monospace — IDs, timestamps, values, code */
.mono { font-family: var(--mono); }

/* Badges / pills */
.badge {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
```

**Never use:** font-weight 800+, all-caps for body copy, serif fonts, font sizes below 9px.

---

## Core Components

### Glass Card
The standard container for all content panels.

```css
.glass {
  border-radius: 16px;
  border: 1px solid var(--border);
  background: var(--surface);
  position: relative;
  overflow: hidden;
}

/* Top edge highlight — subtle shimmer */
.glass::before {
  content: '';
  position: absolute;
  top: 0; left: 20%; right: 20%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent);
}
```

**Glow variants** — add class for color-accented cards:

```css
.glass.teal-glow {
  border-color: rgba(0,245,212,0.15);
  background: linear-gradient(160deg, rgba(0,245,212,0.05) 0%, var(--surface) 40%);
}
.glass.teal-glow::before {
  background: linear-gradient(90deg, transparent, rgba(0,245,212,0.3), transparent);
  box-shadow: 0 0 8px rgba(0,245,212,0.2);
}

/* Same pattern for: .pink-glow, .purple-glow, .amber-glow */
```

---

### Stat Card
Used in dashboard stats grids.

```css
.stat-card {
  padding: 14px 16px;
  border-radius: 14px;
  border: 1px solid var(--border);
  background: var(--surface);
  position: relative;
  overflow: hidden;
}

/* Colored top bar */
.stat-card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  border-radius: 14px 14px 0 0;
}
.stat-card.teal::after  { background: linear-gradient(90deg, transparent, var(--teal), transparent); }
.stat-card.pink::after  { background: linear-gradient(90deg, transparent, var(--pink), transparent); }
.stat-card.amber::after { background: linear-gradient(90deg, transparent, var(--amber), transparent); }
.stat-card.purple::after { background: linear-gradient(90deg, transparent, var(--purple), transparent); }

/* Colored value with glow */
.stat-card.teal .stat-val   { color: var(--teal);   text-shadow: 0 0 20px rgba(0,245,212,0.3); }
.stat-card.pink .stat-val   { color: var(--pink);   text-shadow: 0 0 20px rgba(255,75,110,0.3); }
.stat-card.amber .stat-val  { color: var(--amber);  text-shadow: 0 0 20px rgba(255,179,71,0.3); }
.stat-card.purple .stat-val { color: var(--purple); text-shadow: 0 0 20px rgba(123,97,255,0.3); }
```

---

### Status Pill
Compact inline status indicator used in headers and footers.

```css
.status-pill {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 99px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  border: 1px solid;
}

.sp-ok   { background: rgba(0,245,212,0.08);  border-color: rgba(0,245,212,0.25);  color: var(--teal); }
.sp-warn { background: rgba(255,179,71,0.08); border-color: rgba(255,179,71,0.25); color: var(--amber); }
.sp-err  { background: rgba(255,75,110,0.08); border-color: rgba(255,75,110,0.25); color: var(--pink); }
.sp-dim  { background: rgba(255,255,255,0.04); border-color: var(--border);        color: var(--muted); }
```

---

### Status Dot
```css
.status-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
.dot-on   { background: var(--teal);  box-shadow: 0 0 6px var(--teal); }
.dot-warn { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
.dot-off  { background: var(--pink);  box-shadow: 0 0 6px var(--pink); }
.dot-dim  { background: rgba(255,255,255,0.2); }

/* Live pulse animation */
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(0.8)} }
.dot-pulse { animation: pulse 2s infinite; }
```

---

### Badge / Tag
```css
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 99px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.05em;
  border: 1px solid;
}

/* Variants */
.badge-supervised { background: rgba(0,245,212,0.08);  border-color: rgba(0,245,212,0.2);  color: var(--teal); }
.badge-autonomous { background: rgba(255,179,71,0.08); border-color: rgba(255,179,71,0.2); color: var(--amber); }
.badge-paper      { background: rgba(255,255,255,0.04); border-color: var(--border);        color: var(--muted); }
.badge-kill       { background: rgba(255,75,110,0.12); border-color: rgba(255,75,110,0.3); color: var(--pink); }
.badge-pending    { background: rgba(123,97,255,0.1);  border-color: rgba(123,97,255,0.25); color: #a78bfa; }
```

---

### Button
```css
.btn {
  padding: 8px 18px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
  font-family: var(--sans);
  transition: all 0.18s;
  border: 1px solid;
}

.btn-teal   { background: rgba(0,245,212,0.1);  border-color: rgba(0,245,212,0.3);  color: var(--teal); }
.btn-pink   { background: rgba(255,75,110,0.1); border-color: rgba(255,75,110,0.3); color: var(--pink); }
.btn-amber  { background: rgba(255,179,71,0.1); border-color: rgba(255,179,71,0.3); color: var(--amber); }
.btn-purple { background: rgba(123,97,255,0.1); border-color: rgba(123,97,255,0.3); color: var(--purple); }
.btn-ghost  { background: transparent;          border-color: var(--border);         color: var(--muted); }

.btn:hover { filter: brightness(1.15); box-shadow: 0 0 16px currentColor; opacity: 0.8; }
```

---

### Input / Form Field
```css
input, select, textarea {
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 8px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  padding: 8px 12px;
  outline: none;
  transition: border-color 0.15s;
}
input:focus {
  border-color: rgba(0,245,212,0.4);
  box-shadow: 0 0 0 2px rgba(0,245,212,0.08);
}
```

---

### Header (Sticky)
```css
.header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(10,12,20,0.85);
  backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.wordmark {
  font-family: var(--mono);
  font-size: 1rem;
  font-weight: 600;
  letter-spacing: 0.15em;
  color: var(--teal);
  text-shadow: 0 0 20px rgba(0,245,212,0.4);
}
```

---

### Section Divider
```css
.section-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-title::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}
```

---

### Toast Notification
```css
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(60px);
  padding: 10px 20px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  background: var(--surface);
  border: 1px solid var(--border2);
  color: var(--text);
  z-index: 1000;
  transition: transform 0.25s;
  box-shadow: 0 8px 32px rgba(0,0,0,0.5);
}
.toast.show  { transform: translateX(-50%) translateY(0); }
.toast.ok    { border-color: rgba(0,245,212,0.4); color: var(--teal); }
.toast.error { border-color: rgba(255,75,110,0.4); color: var(--pink); }
```

---

### Modal / Overlay
```css
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(8px);
  z-index: 500;
  display: flex;
  align-items: center;
  justify-content: center;
}

.modal {
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 24px;
  width: min(860px, 95vw);
  max-height: 88vh;
  overflow: hidden;
  box-shadow: 0 24px 80px rgba(0,0,0,0.6);
}
```

---

### Scrollbar
```css
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 99px; }
```

---

## Layout Grid

```css
/* Standard page wrapper */
.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 24px;
}

/* Two column — main content + sidebar */
.two-col {
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 16px;
}
@media (max-width: 900px) {
  .two-col { grid-template-columns: 1fr; }
}

/* Auto-fit card grid */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}

/* Stats row */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}
```

---

## Icons & Identity

**Wordmark:** `SYNTHOS` in JetBrains Mono, weight 600, letter-spacing 0.15em, color `--teal`, text-shadow glow.

**Page icons** (desktop shortcuts):
- Trading Portal — dark card, teal bar chart + sparkline
- File Transfer — dark card, purple file icons with arrow, purple glow
- Command Console — dark card, amber terminal screen with Pi chips, amber glow

**Agent color assignment:**
- Agent 1 — The Trader → Teal
- Agent 2 — The Daily → Purple
- Agent 3 — The Pulse → Amber
- Agent 4 — Audit / Self-Improvement → Pink/Purple gradient

---

## Do / Don't

| Do | Don't |
|----|-------|
| Use CSS variables for all colors | Hardcode hex values |
| Glow effects via `text-shadow` / `box-shadow` | Drop shadows for depth |
| `border-radius: 16px` for cards, `10px` for buttons, `99px` for pills | Mix radius sizes randomly |
| Uppercase + letter-spacing for labels | Uppercase for body text |
| JetBrains Mono for all data values | Mono for UI labels |
| Subtle 1px borders at 7-12% white opacity | Thick visible borders |
| `backdrop-filter: blur()` for overlays | Solid opaque backgrounds on overlays |
| Color-coded state (teal/pink/amber) | Generic grey for status |
| `transition: all 0.15-0.18s` on interactive elements | No transitions |
| Animations only for live data (pulse, blink) | Decorative animations |

---

## Chart Style (Chart.js)

```javascript
// Standard sparkline config
{
  borderColor: '#00f5d4',
  borderWidth: 2,
  fill: true,
  backgroundColor: gradient, // teal 0.2 → transparent
  tension: 0.4,
  pointRadius: 0,
  pointHitRadius: 8,
}

// Tooltip
tooltip: {
  backgroundColor: 'rgba(13,17,32,0.95)',
  borderColor: 'rgba(0,245,212,0.3)',
  borderWidth: 1,
  titleColor: 'rgba(255,255,255,0.5)',
  bodyColor: '#00f5d4',
  bodyFont: { weight: 'bold' },
}

// Axes
scales: {
  x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: 'rgba(255,255,255,0.3)' } },
  y: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: 'rgba(255,255,255,0.3)' }, position: 'right' }
}
```

---

*This document is the single source of truth for Synthos UI. All new components must be consistent with this spec. When in doubt — darker, more subtle, less decoration.*
