# Synthos TODO

> Shared todo list — editable in Obsidian, git-tracked, used by Claude for context.
> **Checkbox syntax**: `- [ ]` pending, `- [x]` done. Feel free to reorder, add, or delete.
> Last sync: 2026-04-25 (post-verification sweep)

---

## 🔥 Active this week

- [ ] **Pre-trip SD mitigations** (before user is away 3 weeks):
	- [ ] Snapshot pi4b `.env` + systemd units to safe location
	- [ ] Take full SD image → R2 (`dd | gzip | rclone`)
	- [ ] Verify pi5 has independent R2 backup path (not dependent on pi4b strongbox)
	- [ ] Confirm `daily_master` email actually sends (visibility signal during absence) — `retail_daily_master.py` confirmed present at `synthos_build/src/`; logs dir exists on pi5; only the email-deliverability needs a single-shot test
- [ ] **SD card swap on pi4b** — waiting on powered USB hub delivery. Attempt this weekend if hardware arrives.
- [ ] **Watch attribution patch behavior** — TICKER_REMAP + TICKER_REJECT enforcement now ON (2026-04-25). Sample real-world enforced rejects weekly to make sure no false positives are blocking legitimate signals.
- [ ] **Verify stop-loss behavior 15:00-16:00 ET window** — `LATE_DAY_TIGHTEN_PCT=0.0` deployed 2026-04-21 (4+ trading days now). Pull `exit_performance` to confirm the 15:00-16:00 bucket no longer shows the breakeven-noise pattern.

## 🆕 Active follow-ups from 2026-04-25 dashboard sprint

- [ ] **Monday 09:25 ET — verify Phase 7 work fires correctly** on first live trader run:
	- [ ] history WIN/LOSS reclassification on first trail-stop win
	- [ ] `entry_pattern` populates on next-opened position (Phase 5 lazy ALTER triggers)
	- [ ] `entry_thesis` populates on next-opened position (Phase 7L)
	- [ ] Planning drawer Live Snapshot loads ADR + sector ETF on real position
	- [ ] News tracker chip appears on ticker-attached articles after dashboard polls run
- [ ] **Phase 7e deferred bucket** (~3-5h total when scheduled):
	- [ ] Continuous trade-arc chart on History drawer (replaces 2-dot arc; needs Alpaca daily-bar fetch on drawer-open)
	- [ ] User memos surviving close into `closed_positions` (schema add `user_memo TEXT`)
	- [ ] Volatility-anchored "Suggested Levels" on Planning drawer (ATR-based bands, replaces rejected generic-percentage version) — Option B, approved
	- [ ] Feedback button ("bot got this wrong") with three flag types — discussed, queue-style backlog table
- [ ] **Pattern-calibration line on Approval drawer** — needs ~30d post-Phase-5 trade data before stats are meaningful (calendar item, eligible ~2026-05-25)

### Documented but not-fixed (residue from Phase 7L recap, intentional)

- `drawer-ticker` element ID is misnamed (holds company name) — comment in code, rename has search/replace risk
- `get_watching_signals()` legacy function still defined — no production callers after 7k/7L; tests still call it for backwards-compat smoke. Removable after the test is updated.

## 📱 Retail Portal

### Template extraction — VERIFIED ESSENTIALLY DONE 2026-04-25

✅ All public/customer-facing pages extracted: `/terms`, `/login`, `/logout`, `/subscribe`, `/check-email`, `/activate/<token>` (→ setup_account / verify_success / verify_error), `/notifications`, `/news`, `/screening`, `/performance`, `/settings`, `/dashboard` (`/`). 18 templates in `src/templates/`. Zero top-level `*_HTML` constants in retail_portal.py.

- [ ] **`/logs` admin page** — only remaining inline HTML in retail_portal.py (~100 lines in `logs_page()` at line 5441). Admin-only, low priority. Extract when convenient.

### Small portal issues to fix (verified 2026-04-25)

- [ ] **`check_email.html` theming** — verified `📬` emoji still present (line: `<div class="icon">📬</div>`). Page theme also still generic dark-card vs newer SYNTHOS visual language. Not yet addressed.
- [ ] **Portal shared-CSS consolidation** — verified `src/static/css/` directory does NOT exist. `@font-face` rules and `.pw-eye` block still copy-pasted. Defer until next DRY pass.
- [ ] **Password-toggle eye icons bug** — verified `.pw-eye` / `.pw-wrap` CSS not present in any template currently. The original templates that had the bug may have been replaced during extraction. Re-test on `signup.html` and `setup_account.html` post-extraction; could already be moot.

### Mobile wave experience (spec from 2026-04-21 evening)

- [ ] No patch branch yet. All sub-items still pending:
	- [ ] Cut patch branch `patch/YYYY-MM-DD-mobile-wave` when ready to build
	- [ ] Client-side viewport detection (≤768px)
	- [ ] Fullscreen wave view on mobile login → Exit Wave pill (bottom-center, auto-hide after 3-4s of no touch)
	- [ ] Desktop: condensed wave card + triangle toggle + Enter Wave pill
	- [ ] PWA manifest + icons + iOS standalone meta tags
	- [ ] Smooth exit transition (crossfade or slide-up)
	- [ ] Landscape: naive stretch first, revisit after testing

## 🖥️ Command Portal

✅ ROI-deferred 2026-04-25. `synthos_monitor.py` is 9,194 lines with ~1,952 lines of inline HTML (21%, 7 constants). Only used by admin (Patrick), no observed bugs. Not extracting unless specific edit-pain emerges.

- [ ] *(reactivate when admin-portal editing actually hurts)*

## 🧠 Trading Logic

### Attribution patch followups (from 2026-04-21 deploy)

- [x] **2026-04-28 enforcement review** — VERIFIED done early on 2026-04-25: `TICKER_REMAP_ENFORCE=True`, `TICKER_REJECT_ENFORCE=True` flipped after 4.5-day shadow review.
- [ ] **Fix duplicate flag writes** — verified still pending; no dedup pattern detected in `retail_news_agent.py`. Article processed via signal + display paths still writes duplicate flag rows.
- [ ] Grow `TICKER_ALIASES` dict based on observed `[TICKER_REJECT]` logs (currently ~30 mega-caps).

### Late-day tightening follow-up

- [ ] **Re-evaluate in 2-4 weeks** — once `exit_performance` has 20+ exits for the optimizer. Decide whether to keep `LATE_DAY_TIGHTEN_PCT=0.0` or move to Option 3 (conditional: only tighten if `gain_pct > 0.01`). Calendar: ~2026-05-05.
- [x] **`LATE_DAY_TIGHTEN_PCT=0.0` resolved-items entry to `system_architecture.json`** — VERIFIED present in arch.json under "Late-day stop tightening disabled" entry.

### AUTO/USER per-position management

- [x] **Feature merged & shipped to main** — VERIFIED 2026-04-25. Multiple commits on main reference the feature. `managed_by` column populated, `sticky=user` mechanism live, lock chip + ticker-preferences UI in production. Branch `patch/2026-05-03-auto-user-tagging` could be cleaned up.

### Other trading-logic items (design decisions / future)

- [ ] Separate premarket from overnight cycle (future refactor — noted as TODO in code)
- [ ] News-triggered sentiment re-run (Q1 from earlier design — speculative)
- [ ] **Congressional signal → combined_score** — design decision still open. `congressional_flag` displayed in sector_screening but not weighted into `combined_score`. Should it modify the score?

### SYSTEM_MANIFEST.md rewrite-or-retire decision

- [ ] **Decide fate of `synthos-company/documentation/specs/SYSTEM_MANIFEST.md`** — stamped OUTDATED on 2026-04-23. Two paths:
	1. **Rewrite as v6.0** reflecting actual architecture — 2-3h focused work
	2. **Retire** and redistribute unique content (ENV_SCHEMA, UPGRADE_RULES, DEPENDENCY_GRAPH, SYSTEM_PATHS) into smaller focused docs
- 136 cross-references exist; either path needs a redirect/update sweep.

### Operational stale cleanup (verified 2026-04-25)

| Item | Verified state | Action |
|---|---|---|
| pi4b: `company_scoop.py` decision | Service unit no longer exists on pi4b (`Unit synthos-scoop.service could not be found`). `.py` file may still exist; need to decide retire-and-delete vs revive-as-systemd | retire decision |
| pi4b: `synthos-login.service` | **Still present** (`/etc/systemd/system/synthos-login.service`, disabled+inactive). Pending removal. | remove |
| pi4b: `/home/pi/synthos_build/` | **Still present** (empty skeleton from 2026-04-08) | remove |
| pi4b: `/home/pi/synthos-process/` | **Still present** (10 entries from cancelled process_node) | remove |
| pi5: `synthos-portal.service.bak.20260418_081448` | **Still present** in `/etc/systemd/system/` | remove |
| pi4b: `synthos-company/login_server/` directory | **Still present** | move to documentation/archive/ |

### Stale branch cleanup (newly surfaced 2026-04-25)

- [ ] **4 stale `claude/*` branches** + **multiple stale `patch/2026-04-19-*` branches** still hanging around. Last cleanup was 2026-04-21 (cleaned 9 + 4 worktrees); a fresh sweep would clear another batch.

## 🏗️ Infrastructure

- [ ] **pi4b USB SSD install** (hardware on hand, blocker: powered USB hub delivery)
- [ ] **Full disaster recovery drill** — restore from R2 + verify trading continuity
- [ ] **pi5-expansion prep**: `ASSIGNED_NODE` customer setting for multi-node routing — verified NOT implemented (no references in codebase)
- [ ] **pi5-expansion decision**: enrichment master pattern vs PostgreSQL-first (Phase 8)

## 📦 Deferred / Next Phase

- [ ] Backup pipeline hardening — encrypt-on-source + 3-stream split + retail_backup schedule. Plan: `synthos-company/documentation/specs/BACKUP_ENCRYPT_AND_SPLIT_PLAN.md`
- [ ] Installer restore UI — post-install "restore from file or R2" page. Companion to backup hardening.
- [ ] Phase 6 gate conditions — trading mode, boot-time integrity, retail license gate (see `PROJECT_STATUS.md`)
- [ ] C8 news-agent gate-pipeline refactor — baseline harness is on `patch/2026-04-24`, actual refactor is future work

## ✅ Recently completed (last 7 days)

### 2026-04-25 — Customer Dashboard UX Overhaul (Phases 5–7L, ~20 commits)

- [x] **Phase 5** (`2b38efb`) — `entry_pattern` column on positions, row badge + trail-stop% + days held + sector
- [x] **Phase 6** (`8347f06`) — kill Regime Strip, planning card upgrade with buy zone/stop/target/thesis
- [x] **Phase 7a** (`2f43b0a`) — lock chip, Bot Active dot, history WIN/LOSS reclass, Signal Trust widget
- [x] **Phase 7b** (`f94b2c7`) — circular lock icon, drawer header = company name + sparkline
- [x] **Phase 7c** (`3f4b64d`) — new History drawer (replaces openLogicModal hack)
- [x] **Phase 7d** (`84d7a1d`) — new Approval drawer (replaces inline expansion)
- [x] **Phase 7f** (`6a981ad`) — cleanup: openLogicModal removed, Cost Basis dedup, ESC-to-close on all drawers
- [x] **Phase 7g** (`2889b32`) — Planning drawer (watchlist deep-dive); `/api/ticker-news` endpoint
- [x] **Phase 7h** (`d7280b8`) — fresh-only ticker-news; new `/api/ticker-context` (live price + ADR + sector ETF)
- [x] **Phase 7i+j** (`e7998e9`, `bbc6feb`) — News page redesign (Tracked + Hide-low-quality + ticker chips); Intel → Bot Watchlist
- [x] **Phase 7i fixup** (`4f214c0`) — News filters re-render on tracker-update; expanded clickbait regex (10% flag rate on real data)
- [x] **Phase 7k** (`b5963eb`) — Watchlist wiring fix: `/api/watchlist` reads signals table not news_feed
- [x] **Phase 7L** (`a7a7176`, `fe5b3a6`, `3181fe0`, `3b7ab14`, `9cd27cf`) — punch-list cleanup: openSigModal removed, /api/planning fallback fixed, sizing reads window cache, sync_to_github wrong-dir fix (MEDIUM-C audit), news dedup tightened, positions.entry_thesis column, computeSignalTrust unified
- [x] Status sync (`f2c625f` synthos / `a47238a` synthos-company) — all 6 status files refreshed for the dashboard overhaul

### 2026-04-24/25 — Pre-launch security audit + attribution enforcement

- [x] CRITICAL-1 closed: /api/admin-override admin gate
- [x] CRITICAL-2 closed: /api/keys admin gate
- [x] HIGH-1 closed: /reset-password handler added (forgot-password was broken)
- [x] HIGH-3 closed: /api/audit promoted to @admin_required
- [x] Persistent login rate limiter (login_attempts table) — 10 fails/15min per account, 20/5min per IP
- [x] Two-step verify-new-email flow (pending_email_changes table)
- [x] Password min-length standardized 8→12 (OWASP 2023)
- [x] auth.py state/zip_code migration bug (silently broken since 2026-04-13)
- [x] Phase 4.5 file-upload audit: 21/21 customer-side probes pass
- [x] **Attribution patch enforcement: TICKER_REMAP + TICKER_REJECT flipped True after 4.5-day shadow** — VERIFIED in news agent

### 2026-04-25 — Items previously listed as pending but verified DONE

- [x] **Retail portal template extraction** — verified essentially complete (12 of 12 listed pages now in `src/templates/`; only `/logs` admin page remains)
- [x] **AUTO/USER per-position management feature** — verified merged to main (managed_by + sticky=user + lock chip all in production)
- [x] **`LATE_DAY_TIGHTEN_PCT=0.0` to architecture.json** — verified present in arch.json
- [x] **2026-04-28 enforcement review** — verified completed early (2026-04-25)
- [x] **company_scoop.py service unit** — verified removed from pi4b (`Unit synthos-scoop.service could not be found`); `.py` retire-vs-revive decision still open

### 2026-04-21 (week of)

- [x] News attribution patch (Fix A enforced, Fix C shadow, company populate, flag table)
- [x] `LATE_DAY_TIGHTEN_PCT=0.0` disabled on pi5
- [x] Enrichment pipeline reconstruction (timer 09:10→09:15, overnight cycle completion, standalone price_poller timer, pre-market self-check)
- [x] Candidate-generator filter `combined_score` → `momentum_score` (threshold 0.45)
- [x] `_MARKET_STATE_UPDATED` key-name fix (validator degradation)
- [x] Second sector_screener pass at market close (2×/day)
- [x] Cleaned 9 stale `claude/*` branches + 4 orphan worktrees

---

## 🔗 Reference docs

- `PROJECT_STATUS.md` — phased roadmap, gate conditions
- `synthos_build/data/system_architecture.json` — live system map (v3.14)
- `synthos_build/data/project_status.json` — live JSON dashboard (v2.3)
- `docs/` — spec archive

## Workflow conventions

- **Patch branches**: `patch/YYYY-MM-DD-short-name` off main. Never commit to main directly.
- **Always**: `py_compile` check → dry-run if possible → click-test on pi5 before merging to main.
- **Deploy chain**: edit on Mac → commit → push to GitHub → `git pull` on Pi → restart service → verify.
- **Never**: push during market hours (09:30-16:00 ET) unless the change is trading-critical.
