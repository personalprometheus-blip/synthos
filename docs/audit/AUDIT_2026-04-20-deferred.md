# Post-Phase-B audit — deferred items

Documenting what the Phase B audit found but was too large to fix in
the Round 6/7/8 pass. Each entry names the finding, why deferral is
the right call, and what fixing it properly would involve.

---

## D1. Trader `run()` complexity refactor

**Finding (Phase A, radon):** `agents/retail_trade_logic_agent.py::run`
scored **184** on radon's cyclomatic complexity — off the standard
A-F scale. The function is ~850 lines spanning: halt check → boot
bootstrap → Alpaca client init → reconciliation → Gate 0/1/2/3/13
system gates → Gate 10 exits → managed-mode execution → Gate 4-11
entry chain → monthly tax sweep → daily report.

**Why defer:** This is the money path. Every edit risks introducing
subtle behavior drift. A proper refactor needs:
1. Extract each Gate-N block into its own named helper with explicit
   inputs/outputs, ideally in a separate module per gate group
2. Compose the extracted helpers inside a shorter `run()` shell
3. Comprehensive regression test — at minimum snapshot the
   `decision_log` JSON for a fixed set of signals before and after

That's ~days of focused work plus a test harness that doesn't exist.
Not a bug today, just maintenance debt.

**Trigger to revisit:** next non-trivial trader change; or if we find
ourselves afraid to touch the file.

---

## D2. Portal complexity hotspots

**Finding:** `src/retail_portal.py::api_admin_market_activity` F (67),
`_enrich_positions` F (45), `get_system_status` E (38),
`stripe_webhook` E (33), etc.

**Why defer:** The portal's a UI surface — complexity shows up as
deep nesting in endpoint handlers that format diverse response
shapes, not as hidden logic bugs. Low bug-risk, high rewrite cost.

**Trigger to revisit:** when we add a new admin view and find
ourselves copying from `api_admin_market_activity` or
`get_system_status`.

---

## D3. Decimal-based money math replacement

**Finding:** `close_position()` uses `round(exit_price * shares, 2)`
style float math throughout. Over months of trades, accumulated
rounding error could drift portfolio cash by cents. Correct fix is
`decimal.Decimal` for cash/proceeds/P&L.

**Why defer:** Touches every call site that passes money around —
helpers, SQL writes, JSON responses, dashboard rendering. Error
propagation is currently bounded in practice (paper accounts, sub-$1
deltas), so the immediate risk is low. Real fix is a sustained
refactor across DB schema (REAL → TEXT for decimal), helpers, and
every caller.

**Trigger to revisit:** when we go live with real capital.

---

## D4. `datetime.utcnow()` blanket replacement

**Finding:** 27 sites across the codebase still use
`datetime.utcnow()` (deprecated in Python 3.12+). Audit Round 3 only
fixed the single noisy site in window_calculator.

**Why defer:** Many sites use `.utcnow().isoformat()` which produces
a naive ISO string (`2026-04-20T12:00:00`). Switching to
`datetime.now(timezone.utc).isoformat()` produces an aware string
(`2026-04-20T12:00:00+00:00`) — different serialization. Any
downstream comparison (string equality, SQLite comparisons, JSON
API consumers) could break silently. Proper fix is per-site: choose
`.replace(tzinfo=None)` for backward-compat or update consumers.

**Trigger to revisit:** when we upgrade to Python 3.12 (the
deprecation becomes a runtime warning floor).

---

## D5. Portal template var / unused-ID audit

**Finding:** 32 portal_lint warnings across `retail_portal.py`.
Most are `unused-id` (HTML IDs without getElementById references)
or `template-var-maybe-undeclared` (which are mostly JS-inside-Jinja
false positives, escaped `{{ }}`).

**Why defer:** Each warning needs hand-verification — is the ID
referenced by a CSS selector? A querySelector call that the grep
missed? A future JS handler? Mechanical deletion risks breaking the
UI. The `customer_count` one was a real bug (fixed in Round 4);
the rest are mostly noise but confirming that takes time.

**Trigger to revisit:** next portal UI sprint, when someone's
actively touching these templates anyway.

---

## D6. Duplicate helpers across agents

**Finding:** `get_active_customers`, `now_et`, `is_market_hours`,
`kill_switch_active`, `fetch_with_retry` are duplicated across 2-3
files. Round 3 partially fixed `get_active_customers` via delegation;
the rest remain.

**Why defer:** Extracting to a `retail_shared.py` module is easy
mechanically but every agent's subprocess import pattern (path
manipulation then `from retail_database import ...`) would need to
be checked for circularity. Also — these duplicates are currently
identical and changing one file doesn't silently break others
(different daemons, per-customer isolation).

**Trigger to revisit:** when a caller's definition needs to diverge
from the canonical (e.g. a new retry pattern for one agent only).

---

## Not in this doc because already fixed

- 5 critical F821s (Round 1)
- Macro window anchor pinning (Round 4)
- Trade-daemon runaway bugs — candidate dedup + Gate 4 ticker dedup (Round 1)
- `.known_good/` cruft cleanup (Round 4)
- BIL concentration alert + tradable-asset filter (Round 5)
- Composite index on `signals(ticker, tx_date, status)` (Round 6)
- `api_calls` date-scan rewrite (Round 6)
- Same-day guard on earnings refresh (Round 6)
- Three `if True:` trader placeholders deleted (Round 6)
- Bare-except pattern downgrades (Round 6)
- `entry_signal_score` stop storing as formatted string + cast migration (Round 7)
- `close_position()` atomic transaction (Round 7)
- Sentiment `fetch_with_retry` tuple timeout + status logging (Round 7)
- Gate 11 equity source alignment (Round 7)
- `daily_master` ET-day binning (Round 7)
- Validator staleness policy — missing timestamp = STALE (Round 8)
