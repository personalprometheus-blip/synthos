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

## ITEM-8 — R2 vault path fix (company node)

**Why deferred.** Lives on pi4b (company node), not pi5. Requires shell
access to the company host and coordination with whatever the R2
backup chain is currently doing. Out of scope for weekend
build-while-away work.

**Entry conditions.** Pi4b accessible, R2 credentials confirmed in
vault env, time window to test backup/restore round-trip safely.

**Scope.** Investigate `/home/pi/synthos-company/company_vault.py` on
pi4b; diagnose the path issue mentioned in the Apr 17 morning session;
fix and verify one successful backup lands in the R2 bucket.

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
