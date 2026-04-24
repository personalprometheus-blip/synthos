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

## ~~PIPELINE-AUDIT — Gap 4 (urgent_flags vs news_flags rationale doc)~~ ✅ DONE 2026-04-24

Resolved: schema comments added to both `news_flags` and `urgent_flags`
table definitions in `src/retail_database.py` explaining the
keep-separate rationale; matching note added to
`docs/trade_lifecycle.md` §7 covering both tables and their distinct
roles in the trader (Gate 4/5 scoring vs Gate 10 PULSE_EXIT).

---

## PIPELINE-AUDIT — Gap 5 (sentiment agent 27-gate scope decision)

**Why deferred.** Requires a design decision, not a mechanical fix, and
touches the sentiment agent's entire output surface. Premature to touch
before travel; safer to let 3 weeks of live operation inform the call.

**Context.** `retail_market_sentiment_agent.py` has 27 gates. Of those,
only two outputs flow downstream: the composite `cascade_detected`
boolean (→ `urgent_flags` → Gate 10 PULSE_EXIT) and the composite market
score (→ `scan_log` MARKET row → `market_state_agent` with weight 0.40).
Gates 5 (breadth), 7 (volatility), 8 (options), 9 (safe-haven), 10
(credit), 11 (sector rotation), 13 (news), 15 (divergence), 21
(divergence warnings), 23 (risk discounts), 24 (persistence), 25
(evaluation), 26 (output) each emit rich per-dimension state labels
that get logged to `scan_log` but are never read by any downstream
consumer.

**Decision needed.** Option A — strip the agent to the composite +
cascade path, delete the other gates (smaller surface, lower
maintenance). Option B — wire the rich per-dimension outputs into
something that consumes them (bigger, but reveals more of sentiment's
original design intent). Both require measurement: over 3 weeks of
live runs, do the unused gate outputs cluster with known regime shifts
such that wiring them up would clearly improve signal quality?

**Entry conditions.**
1. ≥3 weeks of live sentiment scan_log data post-2026-04-24 (i.e.
   after travel).
2. One explicit decision logged in `docs/` or commit message, not a
   drive-by refactor.
3. No parallel Gate 5 composite rework in-flight.

**Scope.** Option A: ~-400 lines in `retail_market_sentiment_agent.py`
+ scan_log schema tidy. Option B: ~+200 lines to add a new consumer
(likely extending `market_state_agent` to blend per-dimension inputs).
Either way: full unit-test coverage on the kept gates.

**Risk.** Option A mid-travel would delete gates currently computing
inputs we forgot we use — verify zero downstream reads first with
`grep -r` across the whole repo before removing any. Option B can
quietly inflate Gate 5 composite weight budget past 1.0 if not paired
with a weight recalibration.

**Related.** `docs/pipeline_audit_2026-04-24.md` Gap 5.

---

## OPERATIONAL-HARDENING — pi5 direct backup + DEGRADED detector + token rotation

**Why deferred.** Three operational resilience items that don't each
warrant a separate backlog entry but collectively close gaps flagged
in the 2026-04-24 security/resilience audit. Grouped because they
share tooling + ops-doc surface.

### 1. Pi5 → R2 direct backup fallback
Current backup chain is `pi5 retail_backup.py → pi4b /receive_backup
→ strongbox → R2`. If pi4b dies during travel, pi5 keeps trading but
produces no offsite backup until pi4b recovers. Older R2 backups
remain intact.

**Fix:** give pi5 direct R2 upload credentials so it can fall back to
direct-upload if pi4b is unreachable for ≥24h. ~200 lines + one new
env var set (R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET, R2_BUCKET).
Reuse existing tar.gz pipeline.

### ~~2. DEGRADED detector in fault_detection_agent~~ ✅ DONE 2026-04-24

Implemented as `gate8_trade_activity_baseline` in
`retail_fault_detection_agent.py` (~140 lines including comments and
tunables). Compares today's `TRADE_DECISION` event count to the 30-day
weekday median (excluding weekends + missing days). Fires WARNING
`ACTIVITY_DEGRADED` if today < `DEGRADED_THRESHOLD_PCT` (default 30%)
of baseline AND baseline ≥ `DEGRADED_MIN_BASELINE` (default 10/day).
Skips on weekends, before 14:00 ET, during warm-up (<14 non-zero days
of history), and in low-traffic regime. Detail field names the likely
causes (over-restrictive gates, empty signal pool, validator stuck
CAUTION, regime locked BEAR) so on-call doesn't have to think.

### 3. Monitor token rotation procedure
`MONITOR_TOKEN` is a single shared secret pasted into pi4b, pi5,
and pi2w `.env` files. Rotating requires updating all three
atomically or alerts break mid-rotation. Today there's no tooling
and no documented procedure.

**Fix:** write `tools/rotate_monitor_token.py` that generates a
new token, pushes to each node via SSH, verifies delivery, then
swaps the live token. ~100 lines. Also document the manual
procedure in `docs/operations/TOKEN_ROTATION.md` so a shell-only
operator can do it without the tool.

**Entry conditions (all three tasks):**
1. Operator back from travel, 2-3 hours free.
2. R2 credentials available with correct bucket scope for pi5 (not
   just pi4b).
3. Fault detection agent not in active tuning — add gate 8 cleanly,
   don't interleave with other changes.

**Scope.** ~380 lines across 3 new files + 1 new agent gate.

**Risk.** Low. Each item is isolated and deployable independently.
Token rotation tool has the highest potential for "oops I locked
everyone out" — start with a dry-run mode that prints the plan
without executing.

---

## CUSTOMER-RISK-DIFFERENTIATION — make the per-customer schema actually do something

**Why deferred.** Today's code has per-customer schema
(`customer_settings` table, per-customer `signals.db`, per-customer
bias scans) that IMPLIES differentiation — different customers
having different risk profiles. Operationally, every customer gets
identical ATR multipliers, identical chase caps (2%/1.5%),
identical vol buckets, identical stop behavior. The per-customer
plumbing exists; the per-customer DATA doesn't differ.

If any customer-facing material mentions "personalized risk
settings," "tier-based management," "Conservative / Moderate /
Aggressive presets," the code doesn't back that up.

**Two paths:**

### Path A — Soften the claim (no code change)
Review marketing/portal copy, update to not imply differentiation
that doesn't exist. Honest + zero engineering work.

### Path B — Make it real (significant engineering)
Add risk-preset field to customer_settings:
`risk_preset ∈ {'Conservative', 'Moderate', 'Aggressive'}`.
Parameterize stop multipliers, chase caps, sizing factors per
preset. Trader consults preset when loading TradingControls for
each customer. Portal settings slider to let customer choose.
~400 lines across trader + portal + new migration.

**Recommendation:** Path A first. Talk to customers, see if
differentiation is actually wanted. Then decide whether Path B is
worth it. Don't build infrastructure for demand you haven't
confirmed.

**Entry conditions.**
1. Marketing copy review done (takes ~1 hour).
2. Decision made: Path A (soften) or Path B (build). Don't
   start Path B without committing to the portal UI work too —
   a preset setting nobody can change is worse than no preset.

---

## UNIT-TESTS — pytest coverage for core gate functions

**Why deferred.** All verification today is integration / dry-run.
Refactors are one "it crashed in dry-run" away from silent subtle
bugs. Unit tests around the core decision gates would catch the
class of bugs that full-run tests miss (boundary conditions,
exact-threshold behavior, regime-state edge cases).

**Highest-ROI candidates** (ordered by impact × testability):
1. `calculate_trail_stop()` — pure function of ATR + sector
2. `gate6_entry()` chase caps — Gate 6 is the current risk hotspot
3. `gate1_system()` kill conditions — drawdown, daily loss, api fail
4. `apply_member_weight()` in news agent — simple transform
5. `_compute_windows()` style anchor math (if window_calc ever
   comes back) — deterministic given inputs

**Scope.** `synthos_build/tests/` directory + pytest config.
~200-300 lines initial coverage on gates 1, 6, plus helpers.
Mock AlpacaClient, mock DB. CI hook optional.

**Entry conditions.**
1. Operator available to define expected behavior for boundary
   cases (e.g. "if ATR is NaN, should stop default to 2% or
   error?"). Subjective calls shouldn't be made in test code.
2. No active refactor in trader — add tests AGAINST stable code,
   not a moving target.

**Related context.**
- Every refactor in the 2026-04-23/24 session was verified by
  dry-run only. Unit tests would have caught several earlier.

---

## CUSTOMER-DATA-DELETION — GDPR/CCPA deletion workflow

**Why deferred.** Today an account can be deactivated (admin flips
`is_active=0`), but positions, outcomes, notifications,
signal_decisions, and member_weights tied to that customer stay
forever. For US customers this is probably fine. If EU customers
ever sign up, GDPR "right to be forgotten" requires a concrete
deletion workflow — currently we have none.

**Decision required before building:**
- Retention policy: do we delete everything, or just PII
  (email/phone/name) while keeping anonymized outcome rows?
- Pseudo-deletion vs hard-deletion: mark rows as deleted vs
  physically purge them?
- Cascade: if we delete a customer, does that affect member_weights
  they contributed to?

**Scope (hard-deletion variant).**
- Admin endpoint `/api/admin/delete-customer` with confirmation
  step (like "enter email address to confirm").
- Cascade helper: DELETE from positions WHERE customer_id=?;
  DELETE FROM notifications; etc. ~8 tables to touch.
- Audit trail: write a "CUSTOMER_DELETED" system_log event BEFORE
  deletion for legal trail.
- ~150 lines + retention policy doc.

**Entry conditions.**
1. At least one concrete request OR first EU customer signup.
2. Retention policy finalized (legal review probably warranted
   for EU customers).
3. Backup immutability confirmed — "deleted" means deleted from
   live DB; historical backups in R2 may still contain the data.

Not urgent until it is.

---

## ENCRYPTION-KEY-ROTATION — tool BUILT 2026-04-24 (not yet run)

Tool delivered at `synthos_build/tools/rotate_encryption_key.py` with
3 modes: `--dry-run` (rotates a /tmp copy, leaves live DB untouched),
`--commit` (backs up live DB then rotates), `--verify-key` (round-trip
sanity check). Self-test at `tools/rotate_encryption_key_test.py`
exercises rotation logic end-to-end on a synthetic 3-row fixture and
asserts all 5 invariants (round-trip identity, OLD key fails after
rotation, email_hash recomputed with NEW key, etc.). Test passes.

Ready for operator use when a rotation is needed. Run dry-run first;
follow the workflow in the tool's docstring. NOT yet exercised against
production auth.db.

### Original entry preserved below for context:

## ENCRYPTION-KEY-ROTATION — tooling to rotate the auth.db Fernet key without lockout

**Why deferred.** Today the Fernet `ENCRYPTION_KEY` in `.env`
encrypts every sensitive customer field: email, display name, phone,
Alpaca API key, Alpaca secret. There is no mechanism to rotate this
key. If it ever leaks (accidental `.env` commit, backup intercept,
pi5 compromise), every customer's Alpaca credentials become
decryptable by whoever has the key — and rotating requires
re-encrypting every row atomically with a new key, which nothing in
the current tree does.

This is a real security hole but NOT an immediate fire. The fix is
tooling, not behavior change.

**Entry conditions (all must be true):**

1. Operator back from travel with attention bandwidth to test
   rotation on a full customer DB (takes careful dry-run + real-run
   separation).
2. Pi5 `.env` verified with 0600 perms + not in any repo.
3. R2 / offsite backup of auth.db confirmed recoverable (so if
   rotation corrupts mid-flight, we can roll back).

**Scope.**
- New tool: `synthos_build/tools/rotate_encryption_key.py` — reads
  OLD_KEY + NEW_KEY from env or CLI, decrypts every field with OLD,
  re-encrypts with NEW, writes to a staging table, commits in one
  transaction, then swaps the .env key.
- Migration strategy: two-key window. Both keys present in .env for
  a transition period; decrypt tries NEW first, falls back to OLD,
  writes always use NEW. After verification, drop OLD key.
- ~200 lines of new tool code + maybe 20 lines of auth.py changes
  for dual-key decrypt support.

**Risk.**
- Catastrophic: a failed mid-flight rotation leaves some rows
  encrypted with OLD and others with NEW. All subsequent logins
  break.
- Mitigation: always run against a COPY of auth.db first. Only
  promote to the live file after verifying every row round-trips
  successfully with the new key.
- Transaction atomicity: SQLite supports BEGIN/COMMIT but the
  tool must NOT let the process be SIGKILLed mid-migration. Use
  a file-based progress marker.

**Related context.**
- Key storage today: `synthos_build/user/.env` on pi5, key name
  `ENCRYPTION_KEY`.
- Fernet library: `cryptography.fernet.Fernet`.
- Fields encrypted: see `retail_database.py` / `auth.py` columns
  with `_enc` suffix.
- Broader security review 2026-04-24 (backlog item below references
  same audit).

---

## EDGAR-SIGNAL-EXPANSION — widen SEC EDGAR ingestion beyond congressional filings

**Why deferred.** Congress (STOCK Act) filings already flow through
`retail_news_agent.py`'s SEC path. EDGAR carries several higher-signal
filing types that aren't touched yet. Adding them is real work with
ingestion + parsing + dedup considerations. Better done post-travel
with time to observe the new signal volume.

**Two phases, in order.**

### Phase 1 — filings NOT yet wired in (net-new signal sources)

Priority by signal-to-effort:

1. **Form 4 (corporate insider transactions)** — ★★★★★
   Officers/directors buy/sell their own company's stock. Filed
   within 2 business days (vs congress at 30-45 days). Best real-time
   insider signal publicly available. Fits existing schema: person +
   ticker + tx_type + date + amount — same shape as congress.
   Route into `news_agent` as a new source path, source_tier=1.

2. **13D (activist ≥5% crossing)** — ★★★★
   When someone crosses 5% ownership with *intent to influence*
   (vs passive 13G). Big catalyst. Low volume (~50-100/year). Would
   need new entity classifier ("is this filer a known activist?").

3. **Form 144 (intent to sell restricted shares)** — ★★
   Early warning of insider sales before Form 4 confirms them.
   Nice-to-have supplement.

### Phase 2 — enhance partial coverage in existing agents

4. **8-K filtered by item code** — partly covered today by
   Alpaca news ingestion. Direct EDGAR pull catches edge items
   news sources underweight:
   - Item 2.02 — earnings results (pre-wire sometimes)
   - Item 5.02 — officer/director changes
   - Item 1.03 — bankruptcy / receivership
   - Item 8.01 — "other events" catch-all (pre-announcement signal)
   Wire into `retail_news_agent.py` as an additional ingestion
   source alongside Alpaca news, with dedup via headline Jaccard
   similarity (Gate 8 already does this for news).

5. **13G** — companion to 13D, lower signal. Include when doing 13D.

**Explicitly skipping:** 13F (45-day lag, priced-in by the time
public), 10-K/10-Q (already covered by earnings calendar + news),
DEF 14A (low trading signal).

**Why seed existing agents, not build new ones.**
News agent already has: 22-gate classification spine, tier-weighted
scoring, member-weight feedback, corroboration tracking, Gate 4 event
risk, news_flags table for trader consumption. Building a parallel
"EDGAR agent" would duplicate all of that. Each EDGAR source simply
becomes a new `source` value and `source_tier` assignment inside
news_agent's existing ingestion loop.

**Entry conditions (all must be true):**

1. Operator back from travel with attention to observe new signal
   volume for 1-2 weeks.
2. News_agent Gate 20 evaluation loop decision made — if it gets
   implemented, EDGAR signals feed into that loop's accuracy stats
   automatically. If it stays deferred, that's fine too.
3. EDGAR rate-limit posture confirmed — EDGAR allows 10 req/sec
   with proper User-Agent. Verify `fetch_with_retry` in news_agent
   handles this cleanly.

**Scope.**

Phase 1:
- `retail_news_agent.py` — new ingestion function `fetch_edgar_form4()`,
  new source-tier mapping. Likely ~200 lines.
- Possibly `fetch_edgar_13d()` if activist tracking is wanted. ~150
  more lines + entity-classifier helper.
- Schema: `signals` table already has `source`, `source_tier`,
  `transaction_type` — no migration needed.

Phase 2:
- `retail_news_agent.py` — new source `edgar_8k`, add item-code
  filter parser. ~100 lines.

Total: ~300-450 lines across one file, maybe 2.

**Risk.**
- Signal flood. Form 4 alone generates thousands of filings/month
  across all US equities. Need tier quotas + ticker-dedup already
  in place (they are — Gate 4 TICKER_DEDUP + tier cap at 60/25/10/5).
- Classification quality drop. A Form 4 from a shell-company CEO
  isn't the same signal as JPM's CEO buying $10M of JPM. Need
  threshold filter on transaction size + filer title.
- 8-K volume overlap with Alpaca news. Dedup via Jaccard may let
  duplicates leak on formatting differences; test before full enable.

**Related context.**
- Current congressional SEC path: `retail_news_agent.py` SEC EDGAR
  ingestion section (fetches STOCK Act filings).
- News agent gate spine: see 2026-04-24 news audit in session history.
- EDGAR full-text-search API: https://efts.sec.gov/LATEST/search-index
- EDGAR RSS per filing-type: https://www.sec.gov/cgi-bin/browse-edgar

---

## DYNAMIC-UNIVERSE-EXPANSION — replace 110 hand-curated sector tickers with full Alpaca tradable universe

**Why deferred.** Current `retail_sector_screener.py` operates on 11
sectors × 10 hand-curated holdings = 110 tickers. That ceiling
misses 98% of Alpaca's ~5,000 tradable US stocks and forces a
quarterly "review SPDR ETF fact sheets" manual task that nobody
actually does on schedule. New additions to ETFs (ANET → XLK in
2024, PLTR in 2025) are invisible to the screener until the next
manual refresh. Also: per-ticker momentum scoring is duplicative —
the trader's Gate 6 already computes MA20/ROC/momentum from fresh
daily bars for every signal regardless of what the screener said.

**Desired end state.**

1. Screener output narrows to **sector-level regime only** (~11 API
   calls/day, one per ETF). Per-ticker momentum scoring deleted.
2. Ticker universe expands to **all Alpaca-tradable symbols** with
   proper sector metadata. Use `/v2/assets` asset metadata
   (already cached by `retail_tradable_cache.py`) which carries
   sector classification for every tradable stock.
3. Top-N-per-sector candidate generation runs against the full
   universe each day using real momentum scoring — no hand-curated
   constant required.
4. Quarterly manual refresh requirement **deleted**. Holdings come
   from live asset metadata, not a Python dict.

**Entry conditions (all must be true):**

1. **Window calculator deletion completed and stable** for ≥1 week.
   This entry touches the same signal-intake pipeline; don't
   compound two architectural changes in the same window.
2. **Tradable cache sector-tagging verified.** Need to confirm
   `retail_tradable_cache.py` actually writes sector metadata to
   `tradable_cache` — if the sector column is NULL for most rows,
   this entry blocks on enriching that cache.
3. **Screener consumption review** — downstream consumers of
   `screener_score` (Gate 5 weighted input, baseline stamp)
   identified so we know what a "sector-level only" output breaks.

**Scope.**

- `retail_sector_screener.py` — massive simplification. Delete
  SECTOR_CONFIG dict (~170 lines). Replace per-holding loop with
  a tradable-cache query + top-N momentum ranker. New shape ~300
  lines total instead of 814.
- `retail_database.py` — `stamp_signals_screener` /
  `stamp_signals_screener_baseline` / `flag_congressional_screening`
  may need parameter updates.
- Maybe a new `tradable_cache`-joined query helper.
- `trade_lifecycle.md` — update §3 (validation chain) to reflect
  sector-regime-only screener.

Total: ~500-line net reduction + 1-2 new query methods. ~1 day
of focused work.

**Risk.**

- Gate 5 composite scoring uses `screener_score` as a weighted
  input. If the new screener only stamps sector-level scores
  (via the baseline stamp path), every signal's screener component
  becomes a sector-baseline rather than a per-ticker momentum
  score. Need to confirm this doesn't degrade scoring quality.
  Mitigation: ship in two phases — first widen the universe while
  keeping per-ticker momentum, then later deprecate per-ticker
  scoring once we've proven sector-baseline is sufficient.
- Tradable cache sector tagging may be incomplete. Dry-run a
  full cache enumeration first and verify sector coverage ≥90%.
- ETF-specific tickers (sector ETFs themselves) should be excluded
  from the candidate set — they'd dominate top-N by sector weight.

**Related context.**

- 2026-04-24 sector-screener audit: flagged the 110-ticker manual
  ceiling. Noted that FMP `/etf-holder` is already in the .env but
  was never wired — current tier may not include that endpoint
  anyway, and this approach sidesteps the dependency.
- Trader's Gate 6 at `retail_trade_logic_agent.py` L1870 — does
  its own MA20/ROC from fresh bars, duplicating screener work.
- `retail_tradable_cache.py` — asset metadata source.

---

## TIER-CALIBRATION-EXPERIMENT — 5-day paper fleet behavior study

**Why deferred.** Originally scheduled to start Monday 2026-04-20; pushed
back when weekend hardware migration + overnight-queue infrastructure
build landed higher priority. The experiment needs a stable baseline
it can actually measure, not a moving target.

**State at defer-time:**
- All 7 real-trading customers tagged with TIER / EXPERIMENT_ID /
  EXPERIMENT_FREEZE=true (`customer_settings` via
  `tools/apply_tier_ladder.py` → `2026-04-17_tier_ladder`)
- Portal settings slide-out locked (`SETTINGS_UI_LOCKED=true` default)
- `tier_readout.py` deployed as part of the `tools/` package; invoked
  via `cd ~/synthos/synthos_build && python3 tools/tier_readout.py`
- Signal-pool tier-weighted cap (60/25/10/5) live, VALIDATED expiry 12h
- Fleet is already running the T1-T5 configs, so "doing nothing" during
  the defer still yields data — just data that the overnight-queue
  landing will partially invalidate

**Entry conditions (all must be true):**

1. **Hardware migration complete** — ~~pi5 running from SSD/NVMe mount~~ ✅
   (completed 2026-04-18; root is `/dev/nvme0n1p2`, all 13 DBs pass
   `PRAGMA integrity_check`, services active post-cutover). **Still
   pending:** one intentional reboot on NVMe to confirm "boot stable
   across ≥1 reboot" — trivial, ~30s.
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
  `tools/apply_tier_ladder.py`
- Original proposal chat session 2026-04-17
- Migration playbook: `docs/pi5_nvme_migration.md`; executed 2026-04-18

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

## C10 — Centralize price polling on pi4b (deferred)

**Why deferred.** Proposed 2026-04-19: move `retail_price_poller.py` from
pi5 to a new agent on pi4b that polls Alpaca every 60s for the union of
tickers in `open positions ∪ validated signals` across all customers,
writes to a shared DB, and has pi5 trader read from there. Rejected
in favor of evolving the existing pi5-local poller in place as part
of the AUTO/USER tagging build (same centralization benefits, no SPOF,
no network latency on every trader read).

Revisit conditions (any one triggers reconsideration):
1. We run more than one retail Pi, so centralized polling avoids
   duplicate fetches across nodes.
2. Measurements show pi5 is measurably CPU-bound specifically on
   Alpaca HTTP calls (not on gate compute), AND the bulk-prefetch
   evolution hasn't relieved it.
3. Price history needs to survive pi5 rebuilds (today: rebuilds are
   rare and price history is not load-bearing).

**Scope if we ever build it.**
- New agent on pi4b (`company_price_agent.py` or similar)
- New DB / table for prices, with freshness timestamps
- Fallback path: if pi4b unreachable, pi5 falls back to direct Alpaca
- Staleness policy: what trader does if last update > threshold
- Monitoring: pi4b price-agent heartbeat + alert on silence

**Related context.**
- Session 2026-04-19: idea raised while scoping the AUTO/USER tagging
  build. Decided the bulk-prefetch evolution on pi5 captures the real
  benefits (centralized cache, single admin API key, no duplicate
  fetches) without the SPOF + network-latency costs.

---

## C9 — News agent module split (PHASE 0 LANDED 2026-04-24)

**Phase 0 (this commit) — extracted pure data only.** Created
`agents/news/__init__.py` + `agents/news/keywords.py` (163 lines)
holding all 14 keyword frozensets, term tuples, sector maps, and the
Alpaca-source tier dict. `retail_news_agent.py` shrunk by 105 lines
(now 3,458) and re-imports every name verbatim — every internal
reference works unchanged. No external callers depend on these
constants (verified via repo-wide grep). Smoke-test confirms all 14
names import with intact contents (53 +ve / 59 -ve / 12 sectors / etc).

**Phase 0 deliberately stopped at data.** No fetcher/classifier/gate
logic was moved — that still needs C8 (gate-pipeline registry) to
land first per the original entry conditions below.

**Remaining phases (still gated on C8 + golden-file fixtures):**

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

## OVERNIGHT-QUEUE — deferred pieces (Phase 3.2, 3.3, 4.1, 4.3)

**Why deferred.** The overnight-queue plan was built out through phases
1, 2, 3.1 (backend), and 5 during the weekend build-while-away session.
Three pieces were deliberately held back because they need either
visual judgment or running-host access that shouldn't happen without
the user present.

**Deferred pieces:**

1. **Phase 3.2 — Pending dashboard card (HTML)**
   Backend endpoint `/api/pending` is live. Returns
   `{active, cancelled, operating_mode}`. Frontend needs to:
   - Add a card to the dashboard showing `active` entries
   - Managed/supervised customers: approve/reject buttons wired to
     existing `/api/approval` endpoint
   - Automatic customers: read-only preview, low-emphasis styling
   - Empty state copy: "No pending decisions — check back after the
     next overnight cycle"
   - Styling should match existing dashboard cards
   Needs visual review on layout + emptystate wording.

2. **Phase 3.3 — Cancelled-protective overlay in trade history**
   Data lives at `/api/pending` under `cancelled`. Frontend needs to:
   - Render CANCELLED_PROTECTIVE rows in place of where the trade
     would have been in trade history
   - Use `warn_red()` icon from `synthos_build/src/icons.py`
   - Label: "CANCELLED (protective)"
   - Show the `cancelled_reason` field
   - Muted / struck-through styling on trade details
   Needs visual review on exact presentation.

3. **Phase 4.1 — Cron consolidation (pi5-side)**
   Replace the following crontab entries on pi5:
   ```
   # Remove
   10 9 * * 1-5  retail_market_daemon.py             (weekdays only, 9:10 ET start)
   5 0-8,16-23 * * 1-5  retail_scheduler.py --session overnight

   # Add
   5 * * * *  retail_market_daemon.py                (hourly, 24/7)
   ```
   The daemon's `main()` already dispatches to intraday vs overnight
   paths based on wall-clock time, so the single hourly entry is
   correct. Crontab edit requires shell access and isn't safe to do
   while the user is out.

4. **Phase 4.3 — Deploy + verify**
   - `git pull` on pi5
   - Run DB migration (idempotent ALTER for queue_origin,
     reevaluated_at, cancelled_reason — safe to run while daemon is
     stopped)
   - Kill and restart daemon to pick up new main() dispatch
   - Restart portal to pick up new /api/pending endpoint + security
     gating changes
   - Smoke test: check that weekend cron fire triggers overnight
     cycle; check that market-open pre-eval runs; check that
     submit_order off-hours writes to pending_approvals

**Entry conditions:**
1. User available to debug any portal/daemon regression
2. Pi5 accessible (post-hardware migration is fine)
3. Tier-calibration experiment not actively running OR at a clean
   pause point (so behavior changes don't confound measurements)

**Risk.** Low — all the schema additions are idempotent ALTERs; new
code paths (overnight queue, pre-open re-eval) only activate outside
market hours so they're off-by-default during regular trading. Portal
security changes could break a frontend endpoint that was silently
relying on the auth gap; mitigation is to fix each specific 401 as
it surfaces.

**Related context.**
- Plan: `docs/overnight_queue_plan.md`
- Audit doc: `docs/trade_lifecycle.md`
- Commits: 2ba8f04 (phase 1), e5a6149 (phase 2), 8e0d343 (phase 3
  backend), 8711c62 (audit doc), ff2a06b (system update removal),
  6f530d1 (security sweep)

---

## PI4B-SSD-MIGRATION — company node SD → SSD (blocked on hardware)

**Why deferred.** Pi4b (the company/monitor node) is the next candidate
for the same SD-to-SSD migration we just finished on pi5. Blocked on
a powered USB hub — the external SSD pulls more current than pi4b's
USB ports can supply unassisted, so it needs its own power source.

**State at defer-time:**
- Pi4b is still booting from SD, running the monitor stack + synthos-
  company services.
- The playbook `docs/pi5_nvme_migration.md` is directly reusable with
  two edits: (a) the target device will be `sdaN` (USB SSD) instead of
  `nvme0n1p2`, so fstab/cmdline.txt substitutions differ, and (b) the
  EEPROM BOOT_ORDER value for USB-first on pi4b is `0xf14` not
  `0xf461` (different bootloader generation).
- Pi5 migration validated the general shape of the procedure: manual
  rsync beats rpi-clone for this workload.

**Entry conditions (all must be true):**

1. **Powered USB hub on hand** — sufficient amperage to keep the SSD
   happy under sustained write load (≥2 A recommended).
2. **Pi4b accessible** — same "user available to debug" posture we had
   for pi5.
3. **Monitor / company stack quiet** — pick a window where the
   company node isn't actively ingesting or reporting. Early morning
   weekend is ideal.

**Scope.** Same 10-step structure as pi5. Expect ~30-40 minutes
end-to-end if the hardware cooperates.

**Risk.** Lower than pi5 — this is the second run of a playbook we
already know works. Same cold-rollback posture: keep the SD in hand,
reinsert if SSD boot fails.

**Related context.**
- Reusable playbook: `docs/pi5_nvme_migration.md`
- Pi5 migration completed 2026-04-18 (commit `9881b1f`); apply
  lessons learned (manual rsync vs rpi-clone, EEPROM tool caching).

---

## ~~ITEM-8 — R2 vault path fix (company node)~~ ✅ RESOLVED 2026-04-24 (not actually a bug)

**Diagnosis.** Investigation on pi4b found `company_vault.py` reporting
"Backup run complete: 0/0 succeeded" daily — looked alarming, was
actually correct behavior. The vault iterates `customers WHERE
status='ACTIVE'` and the customers table is empty (no paying customers
yet, system pre-launch). Zero customers → zero work → 0/0.

**The actual operator backup chain is healthy:** verified
`company_strongbox.py` ran 2026-04-24 02:00 successfully —
- `synthos_backup_company-pi_2026-04-24.tar.gz.enc` (852 KB) → R2
- `synthos_backup_synthos-pi-retail_2026-04-24.tar.gz.enc` (37 MB) → R2
both with sha256 stamps + 30-day retention enforced.

**Patch applied** (`agents/company_vault.py`): docstring updated to
explain vault is fleet-management not operator-data; early-exit when
customers list is empty so the log line reads "No ACTIVE customers in
fleet — nothing to back up" instead of the misleading "0/0 succeeded".
No alert fires now when there's no work; future paying-customer
failures still alert as before.

---

## ITEM-9 — Interrogation upgrade path (feature additions)

**Why deferred.** The listener's stability problems were closed during
the Apr 17 session (heartbeat, watchdog restart, tightened promoter,
stuck-signal diagnostic). The remaining work is feature additions —
multi-peer corroboration, stronger per-signal validation, signed ACKs,
metrics — none of which have clear requirements yet and all of which
need design input from the user.

**Current state (stable):**
- Listener posts heartbeat every 60s to owner DB
- Fault detector includes it in EXPECTED_AGENTS
- Watchdog auto-restarts on crash
- Promoter blocks UNVALIDATED (only VALIDATED/CORROBORATED/SKIPPED
  promote)
- Stuck-signal diagnostic names the listener as the bottleneck when
  it fails

**Candidate features for future:**
- Multi-peer CORROBORATED status (2+ nodes must ACK)
- Signed ACKs (HMAC over payload) to prevent rogue-peer abuse
- Per-signal data validation beyond ticker format (e.g. verify the
  price summary matches the listener's own Alpaca snapshot)
- Persistent dedup window across restarts (currently in-memory only)
- ACK rate / rejection metrics surfaced to the dashboard

**Entry conditions.** User-driven — these are feature decisions, not
bug fixes. Pick them up when the architecture around cross-node
corroboration matters to a real use case.

---

## ITEM-10 — Remaining UI cleanups

**Why deferred.** Three of the four item-10 entries need visual
judgment on exact scope. "Remove System Update" landed during the
weekend build (commit ff2a06b).

**Remaining:**
1. **Trade mode switch** — what change? Today there's an AUTO/MANAGED
   indicator next to the bell icon. Needs user clarification on the
   desired behavior.
2. **Remove news tabs** — which news tabs specifically? The
   notifications dropdown has tabs for All / System / Daily / Account.
   Unclear which should be removed.
3. **Support button** — add? remove? restyle? reposition?

**Entry conditions.** User available for a brief "which one do you
mean" scoping pass. Each is ~5-15 min of work once scope is clear.

---

## EARLY-ACCESS-TOS — revisit & flip the flag

**Why deferred.** Built dormant behind `EARLY_ACCESS_TOS_ENABLED = False`
in `src/retail_portal.py`. The UI, routes, state model, supersession
wiring, and fixture-bypass logic are all in place and syntax-checked on
both Mac and pi4b. Left inert pending a second-pass review of the TOS
copy and the modal/overlay UX.

**State at defer-time:**
- TOS copy:  `docs/tos_early_access.md` (placeholders for
  EFFECTIVE_DATE, CONTACT_EMAIL, GOVERNING_STATE, VENUE_COUNTY)
- Design:    `docs/early_access_tos_design.md`
- Code:      `src/retail_portal.py` — feature block marked with loud
  banner comments; grep for `EARLY_ACCESS_TOS_ENABLED`.
- Routes:    `GET /api/ea/status`, `POST /api/ea/accept-tos`,
  `POST /api/ea/hide-setup` (all short-circuit when flag off)
- State keys (all in per-customer `customer_settings`):
  `ACCOUNT_TYPE`, `EA_TOS_ACCEPTED_VERSION`, `EA_TOS_ACCEPTED_AT`,
  `EA_SETUP_GUIDE_HIDDEN`
- Fixtures are identified by `ACCOUNT_TYPE='fixture'`; real
  beta-testers / early-adopters get the full flow.

**Open design questions to revisit:**

1. **Scroll-to-enable on Accept** — button disabled until the modal
   body is scrolled to the bottom. Too strict? Too loose? Remove
   entirely?
2. **Setup overlay is a bottom-right card, not a centred modal.** Want
   more presence for the overlay? Swap to centred.
3. **Fixture tagging script.** Before flipping the flag, `test_01` and
   `test_02` need `ACCOUNT_TYPE='fixture'` written to their
   customer_settings. Add to `tools/apply_tier_ladder.py` or make a
   new `tools/tag_fixtures.py`?
4. **TOS copy itself** — two items flagged in an earlier pass for a
   deeper look:
   - §5 brokerage-conflict wording ("if conflict, broker's terms
     control *with respect to brokerage relationship*") — deliberately
     deferred for deeper review at deploy time.
   - Section 9 "Basic Ground Rules" softened once; confirm it's now
     at the right tone for early-adopter audience.
5. **Placeholders** — EFFECTIVE_DATE, CONTACT_EMAIL, GOVERNING_STATE,
   VENUE_COUNTY must be filled in before flipping the flag. Rendered
   verbatim into the modal body; no code change needed to fill them.

**Entry conditions (all must be true):**

1. **TOS copy reviewed end-to-end** — including all items above.
2. **Placeholders filled** in `docs/tos_early_access.md`.
3. **Fixture tagger decided + run** — `test_01`, `test_02` carry
   `ACCOUNT_TYPE='fixture'` in their customer_settings.
4. **UX pass** — visual check of both modal and overlay on the
   dashboard to confirm positioning, contrast, keyboard dismissal,
   and mobile layout are acceptable.

**Scope when entered.**

- Flip `EARLY_ACCESS_TOS_ENABLED = True` in `src/retail_portal.py`.
- Restart the portal service on pi5.
- Smoke-test the flip-the-flag checklist in
  `docs/early_access_tos_design.md`.

**Risk.**

Low while dormant (feature flag is off, zero runtime cost beyond a
few hundred bytes of inert DOM). On flip, risk is confined to the
login flow — worst case, revert the flag.

**Related context.**

- Commit landing the dormant build: `[TBD — this commit]`
- Original conversation: ToS drafting + modal/overlay UX iteration
  over several turns, including the test-vs-beta-tester distinction.

---

## ~~PORTAL-CSP-CHARTJS — self-host Chart.js or allow-list CDN~~ ✅ DONE 2026-04-24

Resolved via Option 1 (self-host). `chart.umd.min.js` v4.4.0 (200KB)
copied into `synthos_build/static/js/`; `<script src=>` in
`src/templates/portal.html` swapped from cdnjs URL to `/static/js/chart.umd.min.js`.
No other Chart.js references in the codebase. CSP `'self'` now satisfies
the script load.

---

## PORTAL-CSP-CHARTJS — self-host Chart.js or allow-list CDN _(historical detail below)_

**Why deferred.** The Cloudflare tunnel serving `portal.synth-cloud.com`
sets a Content-Security-Policy header of `script-src 'self' 'unsafe-inline'`,
which blocks the portal's external Chart.js CDN script:

```
Loading the script 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js'
violates the following Content Security Policy directive:
"script-src 'self' 'unsafe-inline'"
```

Chart.js never loads → any chart on the portal (market chart,
performance chart, sparkline visualisations) renders as empty / errors
silently. The same CSP also blocks `cloudflareinsights.com` beacon but
that's harmless.

Not an emergency — the functional data is intact, just the charts are
blank. Caught during the 2026-04-18 post-migration debugging session.

**Options (pick one when we get to it):**

1. **Self-host Chart.js** — copy `chart.umd.min.js` into the portal's
   static asset path, replace the `<script src=".../cdnjs..."` tag.
   Most portable; no external-network dependency at page load.
2. **Allow-list the CDN in CSP** — add `https://cdnjs.cloudflare.com`
   to `script-src`. One-line Cloudflare dashboard edit, or wherever
   the CSP is set (need to trace which layer adds it).
3. **Remove the CSP entirely** — if it was set unintentionally. Least
   good: gives up the XSS protection benefit.

**Entry conditions.** None technical — this can be done any time. Do
it during a quiet portal window (non-trading hours) since it'll need
a portal restart + visual verification the charts come back.

**Scope.** Option 1 = ~30 minutes (download file, move into the portal
asset tree, update one script tag, test). Options 2/3 = 5 minutes of
config + restart.

**Risk.** Very low. If Chart.js fails to load the new way, charts stay
blank — same as today. Non-chart UI unaffected.

**Related context.**
- Discovered 2026-04-18 while debugging a separate init-chain JS error
  (Jinja substitution inside a `// comment` — see commit `bba337a`)
- Fix commit for that separate bug: `bba337a`
- Related prevention idea: add a `portal_lint.py` check that flags
  `{{...}}` appearing inside `//`-style JS comments, to avoid the
  Jinja-in-comment trap reoccurring.

---

## MONDAY-SPOT-CHECK — Patrick's stuck-signal queue drain

**Why deferred.** Saturday afternoon validator spot-check surfaced an
informational WARNING on Patrick's account (30eff008):

```
FAULT: [WARNING] GATE2_FRESHNESS/STUCK_SIGNALS:
  11 signal(s) stuck at QUEUED (>120m) —
  bottleneck: sentiment (most-missing stamp: sentiment_evaluated_at)
```

Likely story: backlog accumulated from Friday market close → Saturday
morning when retail_scheduler was still weekday-only. The weekend
hourly scheduler cron (`5 * * * 0,6`) was added Saturday AM; Sunday
runs should drain the queue naturally. Validator still reports
verdict=GO — the WARNING does NOT cascade to CAUTION (proof the
2026-04-17 bias-threshold fix is holding).

**What to do Monday before 9:30 ET market open:**

```bash
# from workstation
ssh -J pi4b pi516gb@10.0.0.11 \
  "cd ~/synthos/synthos_build && python3 tools/validator_investigate.py | \
   grep -A2 'Patrick McGuire'"
```

**Three outcomes possible:**

1. **0 stuck signals** — queue drained via Sat/Sun hourly scheduler
   fires. Nothing to do; weekend-scheduler fix worked as intended.
   Mark item closed.
2. **<11 stuck signals** (decreasing) — sentiment IS draining but
   slowly; keep watching. Not urgent if market_daemon starts
   successfully at 9:10 ET Monday (that will also process the
   queue).
3. **≥11 stuck signals** (flat or growing) — real sentiment
   bottleneck that the parallelism fix (SENTIMENT_FETCH_WORKERS=5)
   didn't fully address. Investigate: is sentiment_evaluated_at
   stamping working? Is the agent getting rate-limited by Alpaca?
   Is Patrick's signal volume exceeding what 5 workers can chew
   through in an hour?

**Related context.**
- Spot-check conducted 2026-04-18 PM during "stabilize, don't
  extend" session.
- No code change proposed today — this is pure observation, and
  the hourly scheduler change already in place is expected to
  resolve it.

---

## NEWS-WAVE-TRACKING — use source count + regional spread as signal confidence modifier

**Why deferred.** Today's signal scoring is single-event based: one Benzinga
article produces one signal with one confidence tier. But the same underlying
event often surfaces across 4-10+ sources within a few hours ("wave"), and
the degree of adoption is information: a single-source story on a ticker is
noise-level; the same story on 6 sources globally is real sentiment shift.
We don't capture this signal.

Originally specified in the (now-deleted) AGENT_ENHANCEMENT_PLAN.md as
`duplicate_counter` + `duplicate_regions` fields on signals. Rescued into
this backlog on 2026-04-22 during docs cleanup because the idea is still
valuable even though the parent doc was superseded.

**Entry conditions (all must be true):**

1. **Attribution patch shadow period complete** — `TICKER_REMAP_ENFORCE` /
   `TICKER_REJECT_ENFORCE` enforcement decision made on 2026-04-28 and
   stable for ≥5 trading days afterward. Adding another scoring layer
   on top of moving attribution logic is asking for confused debugging.
2. **Duplicate flag-write bug fixed** — articles currently process twice
   (signal + display paths), inflating counts. Need a clean `seen_articles`
   table or equivalent before we can reason about counters.
3. **Benzinga headline-only limitation understood** — we only get
   headlines, not bodies, so "same story" detection relies on Jaccard
   against headline + source-name. Might be noisy; needs a measurement
   pass before we build scoring logic.

**Scope.** Add `duplicate_counter` (INTEGER) + `duplicate_regions` (TEXT
JSON array) columns to signals. Populate via a post-classification pass
that scans last-4h signals for the same ticker with >=50% headline Jaccard
similarity. Modify signal confidence scoring to add a small bump (+0.05
to +0.15) when counter >=4 or regions.length >=3. No behavior change if
counter is 1.

**Risk.** Low. Additive scoring nudge with a clamped effect size. Easy
to disable via feature flag. Worst case: trader sees slightly higher
confidence on widely-reported stories, which is generally correct.

**Related context.**
- Original spec (deleted): `synthos-company/documentation/specs/AGENT_ENHANCEMENT_PLAN.md`
- Attribution patch: `patch/2026-04-21-news-attribution`
- Duplicate flag-write bug: see `synthos/TODO.md` active-this-week section

---

## OVERVALUATION-ALERT — warn (don't block) when entry price exceeds historical P/E band

**Why deferred.** The trader's 13-gate risk chain checks ATR, liquidity,
correlation, and portfolio concentration. It does NOT check whether the
entry price is meaningfully above the ticker's own historical valuation
range or the sector's mean P/E. Strong-company-bad-timing is a known
pattern where a signal is "correct" on sentiment but wrong on entry
price.

Originally specified in the (now-deleted) AGENT_ENHANCEMENT_PLAN.md.
Worth rescuing because it addresses a real gap in gate 6/8 risk logic,
and it's meant to be an alert (display-layer), not a block — low risk
to the dispatch pipeline.

**Entry conditions (all must be true):**

1. **SEC EDGAR financial-disclosure pipeline built or vendored** — we
   need trailing earnings + revenue numbers per ticker to compute P/E
   and growth-adjusted fair value. Shallow `earnings_cache` isn't enough.
2. **Sector P/E benchmark source identified** — needs a per-sector-
   per-month mean P/E feed. Could be Alpaca bars-derived or a third-
   party feed; TBD.
3. **No open validator CAUTION verdicts across fleet** — don't stack
   new data-quality dependencies on an already-noisy pipeline.

**Scope.** New helper `compute_pe_overvaluation(ticker, entry_price)` →
returns `{status: 'normal' | 'elevated' | 'extreme', pe_ratio,
sector_mean, band_z}`. Gate 8 consumes the result: if `status='extreme'`,
add `overvaluation_flag=True` to the trade record (not a block). Portal
UI surfaces the flag as a small warning badge on the position card.

**Risk.** Low. Pure advisory — no gating behavior changes. Worst case:
false positives annoy the user with warnings on legitimate growth stocks
(TSLA, NVDA always look expensive by traditional P/E).

**Related context.**
- Original spec (deleted): `synthos-company/documentation/specs/AGENT_ENHANCEMENT_PLAN.md`
- 13-gate chain: `synthos_build/agents/retail_trade_logic_agent.py`, gate4-gate11

---

## Historical / completed (struck through)

<!-- Move completed items here with commit SHAs when done, keep for
     institutional memory. -->

### ~~PI5-NVME-MIGRATION — pi5 retail stack SD → NVMe~~ ✅ 2026-04-18

**What:** Moved the entire retail stack from the 128 GB SD card to the
attached 256 GB Patriot M.2 P300 NVMe. Cold-rollback SD is preserved,
EEPROM `BOOT_ORDER` is `0xf461` (NVMe-first), services auto-started
on NVMe boot, all 13 DBs report `ok` post-migration. The SD card is
now in the user's physical possession as a bootable recovery image.

**Commits (in order of the migration session):**
- `3d87152` — pre-migration housekeeping: land `rotate_logs.py`,
  gitignore `.bak`/runtime-state patterns
- `122becc` — annotate `.wave_override` in gitignore
- `a396a2d` — migration playbook in `docs/pi5_nvme_migration.md`

**Notes:**
- `rpi-clone` has a bug with NVMe partition naming (uses `nvme0n12`
  instead of `nvme0n1p2`). Fell back to manual rsync clone; faster
  and more controllable. If ever reused: format the target partitions
  directly, skip rpi-clone's `mkfs` step.
- EEPROM flash appeared to not apply while still running on SD
  (`vcgencmd bootloader_config` kept showing `0xf416` post-apply).
  Post-NVMe-boot confirmed the flash DID persist — those tools were
  just reading a boot-time cache. Non-issue.

---

## NEWS-AGENT-GATE-20 — implement real evaluation loop using `outcomes` table

**Status (2026-04-24).** Honesty patch only — the gate's docstring no
longer claims to be a feedback loop, and the misleading
`accuracy_note="accuracy_tracking_pending"` placeholder was renamed to
`"scaffolding"`. Behavior is unchanged. **Real implementation still
deferred** per the entry conditions below.

**Why deferred.** Gate 20 (`gate20_evaluation`) used to claim it was the
news pipeline's feedback loop — "comparing predicted vs. realized market
response" per its docstring — but the implementation only checked
whether the ticker was still in the active signals table and wrote the
string `"accuracy_tracking_pending"`. The 2026-04-24 news agent audit
flagged the discrepancy; the docstring + placeholder are now honest
about being scaffolding.

This is the closest thing to a "learning" feedback loop the news
pipeline could have, and the data to power it already exists — every
closed trade writes an `outcomes` row with P&L, hold time, and a
backref to the originating signal (see trade_lifecycle.md §7).

**Why NOT just fix it.** Implementing this properly is ~1 day of work.
Not appropriate to ship 3 days before the operator's 3-week travel
window where a faulty evaluation loop could drift the scoring in
ways no one is watching. Safer to design the full feedback loop when
there's time to observe it for 1-2 weeks post-deploy.

**Entry conditions** (ALL must be met):
1. Operator back from extended travel, with 2+ weeks of focused
   attention available to watch classification accuracy daily.
2. `outcomes` table has ≥ 50 closed trades with `signal_id` backrefs
   (enough samples per event_class to compute meaningful accuracy).
3. Decision on how Gate 20 should FEED BACK into upstream scoring:
   read-only (informational only, no effect on future classifications)
   OR adaptive (tune composite weights based on historical accuracy).
   Read-only is safer; adaptive is more valuable. Must pick one before
   implementation, not during.

**Scope.**
- `synthos_build/agents/retail_news_agent.py` — `gate20_evaluation`
  body replaced (~80 lines). Also touches the `NewsDecisionLog.commit`
  path if we add a dedicated `news_accuracy` persistence row.
- `synthos_build/src/retail_database.py` — add helper method
  `get_news_accuracy_by_event_class(event_class, days)` that joins
  `signals` to `outcomes` by `signal_id` and computes win rate /
  avg P&L per classification bucket. ~40 lines.
- Optionally: new `news_accuracy` summary table if we want a rolling
  view the portal can show. Add idempotent migration. ~60 lines.
- `trade_lifecycle.md` — update §1 / §3 to describe the real loop.

Total: ~180 lines + 1 schema migration. Manageable; the reason for
deferral is not size, it's the need for post-deploy observation time.

**Risk.**
- If adaptive (tuning weights from accuracy): a few bad classifications
  could skew future scoring for days until more data dilutes them.
  Mitigation: start as read-only, add weight-tuning in a second patch
  only after read-only accuracy reports look sane.
- Schema migration could race with market_daemon / price_poller on
  DB lock — use idempotent ALTER TABLE with try/except, same pattern
  as `_migrate_pending_signups`.
- The `outcomes → signals` join assumes `signal_id` is always
  populated on position rows. Pre-2026-04-08 rows may be NULL; skip
  those in the accuracy query.

**Related context.**
- News-agent audit finding 2026-04-24: Gate 20 is hardcoded
  `accuracy_tracking_pending` — it produces the gate log entry but
  does no evaluation.
- `outcomes` table contract: see `trade_lifecycle.md` §7 "Outcome
  tracking."
- Historical source: `gate20_evaluation` at L2679 of
  `retail_news_agent.py`.
