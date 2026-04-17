# Synthos Retail — Deferred Work Backlog

Items that are scoped and ready to build but are intentionally deferred
until specific **system conditions** are met. Not calendar-tagged — each
entry has entry conditions that can be checked at any time.

When a condition set is met, move the entry to `docs/active_builds.md`
(or execute directly) and strike it through here with the completion
commit SHA.

---

## Entry format

Each backlog item:

- **Title** — one-line description
- **Why deferred** — what's unsafe about building this now
- **Entry conditions** — list of concrete, checkable conditions that ALL
  must be met before implementation starts
- **Scope** — files touched, estimated lines changed
- **Risk** — what could break, how to mitigate
- **Related context** — commit refs, previous discussion, related items

---

## TIER-CALIBRATION-EXPERIMENT — 5-day paper fleet behavior study

**Why deferred.** Originally scheduled to start Monday 2026-04-20; pushed
back when weekend hardware migration + overnight-queue infrastructure
build landed higher priority. The experiment needs a stable baseline
it can actually measure, not a moving target.

**State at defer-time:**
- All 7 real-trading customers tagged with TIER / EXPERIMENT_ID /
  EXPERIMENT_FREEZE=true (`customer_settings` via
  `/tmp/apply_tier_ladder.py` → `2026-04-17_tier_ladder`)
- Portal settings slide-out locked (`SETTINGS_UI_LOCKED=true` default)
- `tier_readout.py` deployed to pi5 at
  `/home/pi516gb/synthos/synthos_build/tier_readout.py`
- Signal-pool tier-weighted cap (60/25/10/5) live, VALIDATED expiry 12h
- Fleet is already running the T1-T5 configs, so "doing nothing" during
  the defer still yields data — just data that the overnight-queue
  landing will partially invalidate

**Entry conditions (all must be true):**

1. **Hardware migration complete** — pi5 running from SSD/NVMe mount,
   boot stable across ≥1 reboot, all systemd services active, DB
   integrity check passes (`PRAGMA integrity_check` on every
   per-customer + master DB returns `ok`).
2. **Overnight-queue infrastructure shipped and stable ≥48h** —
   `docs/overnight_queue_plan.md` executed end-to-end, no new crash-class
   bugs in watchdog/auditor logs during the 48h window, one full
   weekend-through-Monday-open cycle exercised without manual intervention.
3. **No open validator CAUTION verdicts** — every customer reads GO at
   the time of restart; if any are CAUTION, the reason must be named
   and acceptable, not "we don't know why."
4. **`tier_readout.py` smoke-tested** on the new hardware (synthetic or
   real data) — proves the readout tool still runs against the migrated
   DBs so we can measure the experiment on day one.

**Scope.** Un-defer: restart the daemon, reset `EXPERIMENT_START`
timestamps per customer, re-run `tier_readout.py` to establish day-0
baseline, then let the week run.

**Risk.** Low — this is a passive observation experiment. Biggest risk
is restarting without resetting experiment metadata, which would fold
pre/post-deferral data together and corrupt the week's readings.

**Related context.**
- Tier ladder commit: `c97dc6c` (pool cap) + fleet-apply via
  `/tmp/apply_tier_ladder.py`
- Original proposal chat session 2026-04-17

---

## C8 — News agent gate-pipeline refactor

**Why deferred.** The main `run()` loop of `retail_news_agent.py` hard-codes
all 22 classification gates in sequence with per-gate argument shapes.
Refactoring to a gate-list dispatch pattern is pure maintainability gain
(no behavior change intended) but any regression is hard to catch without
a known-good baseline to diff against.

**Entry conditions (all must be true):**

1. **≥2 weeks of clean pipeline runs** — `check_pipeline_health` in
   `retail_watchdog.py` has fired zero PIPELINE_STALL alerts during
   market hours, continuously, for at least 10 consecutive trading days.
2. **Zero unexplained validator CAUTION verdicts** — every CAUTION verdict
   in that window must map to a named cause (e.g. a customer hit their
   daily-loss limit, an agent heartbeat genuinely missed) and not to
   upstream noise.
3. **Golden-file baseline captured** — at least 5 full enrichment cycles
   (news → promoter) have been exported as fixtures under
   `tests/news_baseline/<cycle_id>.json`. The refactored agent must
   produce byte-identical signal_decisions rows for the same input
   headlines to pass.
4. **Tier-calibration data collected** — at least one weekly
   `tier_readout.py` run shows the parameter space actually varies
   behavior per tier. This rules out "the refactor changed gate outputs
   but we can't tell because behavior was undifferentiated anyway."

**Scope.**
- `retail_news_agent.py` lines ~2700–2800 (main `run()` loop)
- New module-level registry of gate callables with consistent signatures
- ~60 lines net decrease, 1 careful refactor session

**Risk.** Classification gate outputs are the input to the trader's
Gate 5 scoring. A regression could silently change which signals get
validated. Mitigation: the golden-file fixture check in Entry
Condition #3 is mandatory.

**Related context.**
- Discussed in chat session 2026-04-17 after validator CAUTION cleanup
- Parent proposal: news-agent Tier C cleanup list

---

## C9 — News agent module split

**Why deferred.** `retail_news_agent.py` is 3,181 lines. Single-file
constraint makes it hard to test fetchers, classifiers, or keyword
dictionaries in isolation, and makes code review of any change to the
file expensive. Splitting into modules is a structural refactor — zero
behavior change if done correctly, but the "done correctly" bar is high.

Target structure:
- `agents/news/fetchers.py` — Alpaca news, SEC insider, Finviz volume
- `agents/news/classifiers.py` — topic, entity, event, sentiment, etc.
- `agents/news/keywords.py` — `_POSITIVE`, `_NEGATIVE`, `_MACRO_TERMS`,
  `_EARNINGS_TERMS`, etc. as pure data
- `agents/retail_news_agent.py` — stays as entry point, orchestrates
  fetch → classify → stamp → broadcast

**Entry conditions (all must be true):**

1. **C8 is complete and has been in production ≥1 week** — the gate
   registry from C8 makes the classifier split straightforward. Doing C9
   without C8 means double the structural churn.
2. **All conditions from C8** — same baseline requirements carry forward.
3. **Import-path integration test exists** — `tests/news/test_imports.py`
   must verify every public name in the pre-split agent is still
   reachable at its old import path (via re-exports in the top-level
   `retail_news_agent.py`). Catches subtle breakage in scripts that
   import from the news agent directly.

**Scope.**
- Split 3,181 lines across 3–4 files with re-exports for backward compat
- ~0 net line change (same code, reorganized)
- 2–3 careful review sessions

**Risk.** Import breakage in callers that import from
`retail_news_agent` by name. Need to grep the whole repo for
`from retail_news_agent import` before starting and ensure every
imported name is re-exported from the new top-level module.

**Related context.**
- Parent proposal: news-agent Tier C cleanup list
- Depends on: C8 (above)

---

## Historical / completed (struck through)

<!-- Move completed items here with commit SHAs when done, keep for
     institutional memory. -->

_(none yet)_
