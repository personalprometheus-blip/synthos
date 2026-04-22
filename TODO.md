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

- [ ] *(add as you find them)*

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

### Other trading-logic items

- [ ] Separate premarket from overnight cycle (future refactor — noted as TODO in code)
- [ ] News-triggered sentiment re-run (Q1 from earlier design — speculative)

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
