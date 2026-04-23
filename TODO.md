# Synthos TODO

> Shared todo list — editable in Obsidian, git-tracked, used by Claude for context.
> **Checkbox syntax**: `- [ ]` pending, `- [x]` done. Feel free to reorder, add, or delete.
> Last sync: 2026-04-22

---

## 🔥 Active this week

- [ ] **Portal template extraction** — patch `patch/2026-04-22-portal-template-extraction`, page-by-page. `/terms` done locally, awaiting pi5 click-test. Pattern: move inline HTML strings → `src/templates/<page>.html` + `render_template()`.
- [ ] **SD card swap on pi4b** — waiting on powered USB hub delivery. Attempt this weekend if hardware arrives. Trying physical store tomorrow.
- [ ] **Pre-trip SD mitigations** (before user is away 3 weeks):
	- [ ] Snapshot pi4b `.env` + systemd units to safe location
	- [ ] Take full SD image → R2 (`dd | gzip | rclone`)
	- [ ] Verify pi5 has independent R2 backup path (not dependent on pi4b strongbox)
	- [ ] Confirm `daily_master` email actually sends (visibility signal during absence)
- [ ] **Watch attribution patch behavior** — 482 flags today, 97% company populate. Review sample weekly.
- [ ] **Verify stop-loss behavior 15:00-16:00 ET window** — first day with `LATE_DAY_TIGHTEN_PCT=0.0`. Expecting no noise stops on breakeven positions.

## 📱 Retail Portal

### Template extraction (ongoing)

- [x] `/terms` — 2026-04-22 (pattern validator, 93 lines extracted)
- [ ] `/login` — next up, larger (~500 lines)
- [ ] `/logout` — trivial (redirect only, no HTML)
- [ ] `/subscribe` — customer onboarding
- [ ] `/check-email` — post-signup holding page
- [ ] `/activate/<token>` — email activation
- [ ] `/notifications` — smaller list page
- [ ] `/news` — medium
- [ ] `/screening`
- [ ] `/performance`
- [ ] `/settings`
- [ ] `/dashboard` (`/`) — largest, do last
- [ ] All remaining pages until inline HTML is gone from `retail_portal.py`

### Small portal issues to fix (add specifics as found)

- [ ] **`check_email.html` theming** — page renders but looks off: emoji needs changing (current 📬 feels out of place against the dark theme) and the page theme needs to match the newer SYNTHOS visual language rather than the generic dark-card look.
- [ ] **Password-toggle eye icons bug** — the `.pw-eye` visibility-toggle SVG icons render massive across multiple pages because the `.pw-wrap` / `.pw-eye` CSS only lives in one template (password-reset form), while the password-toggle JS is copy-pasted across 4+ pages including setup_account and signup. Fixed in `setup_account.html` on 2026-04-23 by inlining the CSS. Broader fix: extract to `static/css/pw-toggle.css` and reference from every template using password inputs.
- [ ] **`_SIGNUP_PAGE_HTML` will hit same eye-icon bug** — has the password-toggle JS but no `.pw-eye` CSS. Apply same CSS when extracting that template.
- [ ] **Portal shared-CSS consolidation** — multiple pages copy-paste the same `@font-face` rules (Inter, JetBrains Mono), the `.pw-eye` block, and likely other chrome. Extract to shared `static/css/*.css` files during later DRY pass (not part of initial extraction).

### Mobile wave experience (spec from 2026-04-21 evening)

- [ ] Cut patch branch `patch/YYYY-MM-DD-mobile-wave` when ready to build
- [ ] Client-side viewport detection (≤768px)
- [ ] Fullscreen wave view on mobile login → Exit Wave pill (bottom-center, auto-hide after 3-4s of no touch, longer hold on first load)
- [ ] Desktop: condensed wave card + triangle toggle + Enter Wave pill
- [ ] PWA manifest + icons + iOS standalone meta tags
- [ ] Smooth exit transition (crossfade or slide-up)
- [ ] Landscape: naive stretch first, revisit after testing

## 🖥️ Command Portal

- [ ] *(add specifics as you think of them)*

## 🧠 Trading Logic

### Attribution patch followups (from 2026-04-21 deploy)

- [ ] **2026-04-28 enforcement review** — examine 5 days of shadow logs, decide on flipping `TICKER_REMAP_ENFORCE=True` + `TICKER_REJECT_ENFORCE=True` in `retail_news_agent.py`
- [ ] **Fix duplicate flag writes** — article processed via both signal + display paths writes 2 identical flag rows (observed in 482 flags today; true unique count ~240)
- [ ] Grow `TICKER_ALIASES` dict based on observed `[TICKER_REJECT]` logs (currently ~30 mega-caps)

### Late-day tightening follow-up

- [ ] **Re-evaluate in 2-4 weeks** — once `exit_performance` has 20+ exits for the optimizer. Decide whether to keep `LATE_DAY_TIGHTEN_PCT=0.0` or move to Option 3 (conditional: only tighten if `gain_pct > 0.01`)
- [ ] Add `LATE_DAY_TIGHTEN_PCT=0.0` resolved-items entry to `synthos_build/data/system_architecture.json` (bundle with next arch update)

### AUTO/USER per-position management

- [ ] **Feature merge target 2026-05-03** — spec: `synthos-company/documentation/specs/AUTO_USER_TAGGING.md`
	- Branch: `patch/2026-05-03-auto-user-tagging` (active)
	- Adds `managed_by` tag to positions (bot vs user), sticky-USER ticker preferences, 4 API endpoints, portal UI toggles
	- **Pre-merge update needed**: spec's feature matrix row for "late-day stop tightening" is obsolete — we disabled `LATE_DAY_TIGHTEN_PCT=0.0` globally on 2026-04-21. Strike that row or mark "currently disabled globally" before merge.

### Other trading-logic items

- [ ] Separate premarket from overnight cycle (future refactor — noted as TODO in code)
- [ ] News-triggered sentiment re-run (Q1 from earlier design — speculative)
- [ ] **Congressional signal → combined_score** — design decision still open. Currently `congressional_flag` (recent_buy / recent_sell / none) is displayed in sector_screening but not mathematically weighted into `combined_score`. Should it add a modifier? Surfaced from deleted INFORMATION_FLOW_WORKING_DOC.md (2026-04-22).

### SYSTEM_MANIFEST.md rewrite-or-retire decision

- [ ] **Decide fate of `synthos-company/documentation/specs/SYSTEM_MANIFEST.md`** — stamped OUTDATED on 2026-04-23 (v5.0 describes cancelled process_node, unused Redis, retired agent filenames). Two paths:
	1. **Rewrite as v6.0** reflecting actual architecture (Pi 5 retail + Pi 4B company, 14 retail agents, SQLite+HTTP comms) — 2-3h focused work
	2. **Retire** and redistribute unique content into smaller focused docs:
		- ENV_SCHEMA → new `docs/env_schema.md` or merge into CLAUDE.md
		- UPGRADE_RULES → new `docs/upgrade_rules.md`
		- DEPENDENCY_GRAPH → could be auto-generated from imports
		- SYSTEM_PATHS section is mostly stable, could keep or fold into architecture.json
- 136 cross-references to this file exist across repos; whichever path we take, need to update or redirect those refs

### Operational stale cleanup (from 2026-04-23 arch.json drift audit)

These surfaced during the ground-truth audit — **not urgent**, can roll into the pi4b SSD-swap day or next pi5-touch moment:

- [ ] **pi4b: decide on `company_scoop.py`** — currently flagged `status: broken` in arch.json. Either revive as a systemd service (the cron replacement daemon never started) or formally retire. `scoop.log` empty since 2026-04-18.
- [ ] **pi4b: remove `synthos-login.service`** (disabled/inactive unit file, points at dead `login_server/app.py`). Companion to deleting the `login_server/` directory itself (already on deferred list).
- [ ] **pi4b: remove `/home/pi/synthos_build/`** — empty skeleton directory (only has empty `data/` subfolder from 2026-04-08). Leftover from an old hardware role.
- [ ] **pi4b: remove `/home/pi/synthos-process/`** — dead repo from cancelled process_node (Phase 4 merge into retail_node). 3+ weeks stale.
- [ ] **pi5: remove `/etc/systemd/system/synthos-portal.service.bak.20260418_081448`** — 4-day-old stale backup file.

## 🏗️ Infrastructure

- [ ] **pi4b USB SSD install** (hardware on hand, blocker: powered USB hub delivery)
- [ ] **Full disaster recovery drill** — restore from R2 + verify trading continuity
- [ ] **pi5-expansion prep**: `ASSIGNED_NODE` customer setting for multi-node routing
- [ ] **pi5-expansion decision**: enrichment master pattern vs PostgreSQL-first (Phase 8)

## 📦 Deferred / Next Phase

- [ ] Backup pipeline hardening — encrypt-on-source + 3-stream split + retail_backup schedule. Plan: `synthos-company/documentation/specs/BACKUP_ENCRYPT_AND_SPLIT_PLAN.md`
- [ ] Installer restore UI — post-install "restore from file or R2" page. Companion to backup hardening.
- [ ] Phase 6 gate conditions — trading mode, boot-time integrity, retail license gate (see `PROJECT_STATUS.md`)
- [ ] C8 news-agent gate-pipeline refactor — baseline harness is on `patch/2026-04-24`, actual refactor is future work

## ✅ Recently completed (last 7 days)

- [x] 2026-04-21: News attribution patch (Fix A enforced, Fix C shadow, company populate, flag table)
- [x] 2026-04-21: `LATE_DAY_TIGHTEN_PCT=0.0` disabled on pi5
- [x] 2026-04-21: Enrichment pipeline reconstruction (timer 09:10→09:15, overnight cycle completion, standalone price_poller timer, pre-market self-check)
- [x] 2026-04-21: Candidate-generator filter `combined_score` → `momentum_score` (threshold 0.45)
- [x] 2026-04-21: `_MARKET_STATE_UPDATED` key-name fix (validator degradation)
- [x] 2026-04-21: Second sector_screener pass at market close (2×/day)
- [x] 2026-04-21: Cleaned 9 stale `claude/*` branches + 4 orphan worktrees

---

## 🔗 Reference docs

- `PROJECT_STATUS.md` — phased roadmap, gate conditions
- `synthos_build/data/system_architecture.json` — live system map (v3.9)
- `docs/` — spec archive

## Workflow conventions

- **Patch branches**: `patch/YYYY-MM-DD-short-name` off main. Never commit to main directly.
- **Always**: `py_compile` check → dry-run if possible → click-test on pi5 before merging to main.
- **Deploy chain**: edit on Mac → commit → push to GitHub → `git pull` on Pi → restart service → verify.
- **Never**: push during market hours (09:30-16:00 ET) unless the change is trading-critical.
