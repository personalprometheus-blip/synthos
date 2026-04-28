# EDGAR Ingestion — Operator Guide

**Status:** code landed 2026-04-27/28 on `patch/2026-04-27-news-edgar-expansion`.
Stages 1 + 2 + 3 + 4 done. All flags **default OFF**. No behavior change until enabled.

This guide covers Stages 1 (Form 4 + 8-K), 2 (13D activist), 3 (Form 144 + 13G), and 4 (cross-source dedup, 8-K body fetch, umbrella flag, source-tier doc).

## What this adds

Five new SEC EDGAR ingestion paths in `retail_news_agent.py`:

| Source | Form | Tier | Trigger | Flag |
|---|---|---|---|---|
| `edgar_form4` | Form 4 | 1 | Insider transactions ≥ $50K (P/S codes) | `EDGAR_FORM4_ENABLED` |
| `edgar_8k` | 8-K | 2 | Items 1.03 / 2.02 / 5.02 / 8.01 | `EDGAR_8K_ENABLED` |
| `edgar_13d` | SC 13D + 13D/A | 1 | Filer in `data/activists.json` registry | `EDGAR_13D_ENABLED` |
| `edgar_form144` | Form 144 | 2 | Proposed sale ≥ $50K, Officer/Director/10% | `EDGAR_FORM144_ENABLED` |
| `edgar_13g` | SC 13G + 13G/A | 2 | Filer in `data/activists.json` registry | `EDGAR_13G_ENABLED` |

Both feed the existing 22-gate news pipeline — same dedup, novelty, sentiment, gate-12 confirmation, gate-21 routing as Alpaca news. No new tables, no schema migrations.

## Required env (set before flipping any flag)

```bash
# SEC requires a real-user User-Agent.  Without these, every fetch 403s.
SEC_EDGAR_UA_NAME=Synthos
SEC_EDGAR_UA_EMAIL=<your contact email>

# Feature flags (default false in the absence of these vars)
EDGAR_FORM4_ENABLED=false
EDGAR_8K_ENABLED=false
EDGAR_13D_ENABLED=false
EDGAR_FORM144_ENABLED=false
EDGAR_13G_ENABLED=false

# Stage 4 E — cross-source dedup (cluster SEC + Benzinga coverage of
# same event). Default OFF; flip ON alongside any EDGAR_*_ENABLED.
# With Alpaca-only ingestion this is pure overhead (singletons everything).
CROSS_SOURCE_DEDUP_ENABLED=false

# Stage 4 G — umbrella flag.  When "true", treats every per-source flag
# above as on UNLESS one is explicitly set to "false".  Useful once the
# operator has stabilized the per-source rollout. Explicit per-source
# "false" still takes precedence (so e.g. you can flip umbrella on with
# EDGAR_13G_ENABLED=false to keep just 13G off).
EDGAR_ALL_ENABLED=false

# Stage 4 C — 8-K body fetch.  When "true", each 8-K hit triggers an
# additional HTTP fetch to the filing's primary doc URL; we extract a
# brief excerpt of the actual disclosure text and use it as the
# headline (with synthetic-header fallback on extraction failure).
# Doubles EDGAR HTTP volume per 8-K — verify rate-limit budget before
# enabling under heavy load.
EDGAR_8K_BODY_FETCH=false
```

Optional tuning vars:

```bash
# Min USD value for a Form 4 transaction to surface (default 50000).
# Lower this to test in low-volume; raise it if it floods the trader.
EDGAR_FORM4_MIN_USD=50000

# Comma-separated 8-K item codes to surface (default 1.03,2.02,5.02,8.01).
# Codes available: 1.01 1.03 2.02 2.05 5.02 7.01 8.01.
EDGAR_8K_ITEMS=1.03,2.02,5.02,8.01

# Min USD value for a Form 144 (proposed sale) to surface (default 50000).
EDGAR_FORM144_MIN_USD=50000

# Cross-source dedup tuning (Stage 4 E)
CROSS_SOURCE_DEDUP_WINDOW_MIN=60   # cluster window (default 60 minutes)
CROSS_SOURCE_JACCARD=0.40          # min headline similarity to cluster

# Override path to the activist registry JSON (default
# synthos_build/data/activists.json).  Used by 13D and 13G classifiers.
ACTIVISTS_CONFIG=/path/to/activists.json
```

## 13D activist registry — required setup before flipping `EDGAR_13D_ENABLED`

13D's value is *who* filed.  ~95% of raw 13D filings come from family
offices / foundations / employee benefit plans where the 5% crossing
isn't activism — they just don't qualify for the simpler 13G form.
Without a curated list of known-activist CIKs, the signal is noise.

`synthos_build/data/activists.json` ships with an **empty activists
array**.  Until the operator populates it, 13D fetching produces zero
signals (safe default — flag-on with empty registry is a no-op).

**Setup procedure:**

1. Open https://www.sec.gov/cgi-bin/browse-edgar in a browser.
2. Search by filer name for each activist you want to track (Icahn
   Enterprises, Pershing Square Capital Management, Third Point LLC,
   Elliott Investment Management, ValueAct Capital, Starboard Value,
   Trian Fund Management, JANA Partners, Greenlight Capital, etc.).
3. Copy the 10-digit CIK from the result page (zero-padded; the URL
   shows the unpadded form, prepend zeros to length 10).
4. Edit `data/activists.json` — append entries to the `activists`
   array. Schema:
   ```json
   {
     "cik":        "0001336528",
     "name":       "Pershing Square Capital Management LP",
     "principals": ["Bill Ackman"],
     "tier":        1,
     "notes":      "Verified at edgar.sec.gov 2026-04-28"
   }
   ```
5. Tier convention:
   - `1` — Top-tier, market-moving activists (Icahn, Ackman, Loeb,
     Singer/Elliott, ValueAct, Starboard, Trian, Pershing Square)
   - `2` — Mid-tier or sector-focused activists
   - `3` — Niche / occasional activists
6. Bump the `updated` field to today's date so future audits can tell
   when the list was last reviewed.
7. Commit + push + pull on pi5 + restart `synthos-portal` to take
   effect (the registry is lazy-loaded on the first 13D run; portal
   restart picks up the new module-level code path).

The registry never auto-promotes filers based on filing patterns —
that's a future v2 feature.  Today the operator decides who counts as
an activist.

## Rollout sequence (recommended)

The feature is built but DELIBERATELY un-deployed pending the entry conditions in `synthos_build/docs/backlog.md` (operator back from travel). When ready:

1. Set `SEC_EDGAR_UA_NAME` + `SEC_EDGAR_UA_EMAIL` in `synthos_build/user/.env` on pi5
2. Restart `synthos-portal` so the env reload takes effect (the news agent re-reads on each run)
3. Verify with `python3 synthos_build/agents/news/edgar_client.py` (will fail-soft if UA missing)
4. Flip `EDGAR_FORM4_ENABLED=true` for **one** weekday morning. Watch:
   - `journalctl -u synthos-market-daemon | grep "EDGAR Form 4"` — should show `N signal items added`
   - `news_dedup_scanner` log — should still be 0 hard dups (form4 won't dedup against existing news; just confirm no flood)
   - Trader signal pool — verify `source='edgar_form4'` rows appearing in `signals` table on pi5 master DB
5. After 1-2 days clean, flip `EDGAR_8K_ENABLED=true`. Watch dedup carefully — 8-K headlines may overlap Benzinga reports. Gate 8 (NOVELTY) should catch them but verify.
6. After Form 4 + 8-K have run cleanly for 2+ weeks, populate `data/activists.json` (see "13D activist registry" section below) then flip `EDGAR_13D_ENABLED=true`. 13D volume is much lower than Form 4/8-K — expect 0-5 signals/week post-classifier. If you see zero for several weeks, recheck the registry against recent EDGAR 13D filings (a known activist may have filed under an entity name whose CIK isn't in your list).
7. After 1-2 weeks with 13D running cleanly, optionally enable `EDGAR_FORM144_ENABLED=true` and/or `EDGAR_13G_ENABLED=true`. Volume notes:
   - **Form 144** is higher-volume than Form 4 (proposal vs action). Expect 5-15 signals/day post-filter at the default $50K threshold. If it floods, raise `EDGAR_FORM144_MIN_USD` to 100000 or 250000.
   - **13G** is much higher-volume than 13D (~100x), but the registry filter cuts that to whatever subset of your activist list happens to file 13G in the window. Expect 0-2 signals/week. A known-activist 13G is itself an interesting signal — accumulating before public activist declaration.
8. Once ≥2 EDGAR sources are running cleanly, enable `CROSS_SOURCE_DEDUP_ENABLED=true` to cluster same-event coverage from SEC + Alpaca. Watch the [X-DEDUP] log lines — every merge is logged with primary source, secondary count, and ticker. If you see clusters that look wrong (different events being merged), tighten `CROSS_SOURCE_JACCARD` from 0.40 to 0.50 or 0.55.

## What's tested vs what's not

**Tested on Mac (111 unit tests, all green):**
- EDGAR client User-Agent enforcement (3)
- Token-bucket rate limiter (2)
- Full-text search hit normalization (3)
- HTTP failure paths (1)
- External fetch injection (circuit breaker reuse) (1)
- Form 4 parser: buy emits, threshold filter, grant codes skipped, ticker missing, env-override (5)
- Form 4 amount formatting (1)
- 8-K item filter: default set, ticker-missing skip, env override widens, env-empty falls back to default (4)
- 8-K headline construction (3)
- 8-K signal shape conformance (1)
- Activist registry: CIK normalization (5), JSON loading + empty / malformed / missing-file fallbacks (5), lookup across cik formats (5), env path override (1)
- 13D fetcher: empty-registry short-circuit, known/unknown filer routing, amendment tagging, no-ticker skip, metadata carry-through, search-form correctness (6)
- 13D headline construction with/without principal, amended (3)
- Form 144 parser: officer-above-threshold, affiliate skipped, below-threshold skipped, env-override, fallback ticker, combined Officer/Director role, empty-XML (7)
- Form 144 relationship normalization (1)
- 13G fetcher: empty-registry short-circuit, known/unknown filer (incl Vanguard/BlackRock noise filter), amendment, no-ticker, search-form correctness, metadata, tier-2 vs 13D's tier-1 (7)
- 13G headline construction with-principal, amended (2)
- Form 4 documentType-based amendment detection (2 — Fix A 2026-04-28)
- Form 4 M+S option-exercise tax-cover skip (3 — Fix B 2026-04-28)
- 13D/13G form-type query fallback (5 — Fix D 2026-04-28)
- Cross-source dedup: pass-through, cluster matching, tier/source preference, ticker boundaries, time window, Jaccard threshold, metadata preservation, env overrides (18 — Stage 4 E 2026-04-28)
- 8-K HTML strip: tag stripping, script/style removal, entity decoding, whitespace normalization, empty-input (5 — Stage 4 C 2026-04-28)
- 8-K excerpt extraction: item-header anchor, lead-paragraph fallback, empty HTML, max-chars cap (4 — Stage 4 C)
- 8-K body-fetch wiring: excerpt replaces synthetic, fetch failure falls back, default no-fetch, per-filing fetch count (4 — Stage 4 C)

**Not tested on Mac (defer to pi5 staged rollout):**
- Real EDGAR HTTP responses (their full-text-search payload shape may drift; we use a fixture)
- Real Form 4 XML diversity (officer titles, partial fills, multi-owner filings — only one canonical sample tested)
- Real 8-K item-code distribution under live load
- Real 13D filer-CIK accuracy (depends entirely on operator's `activists.json` accuracy)
- Trader Gate 5 scoring with new `source_tier=1` Form 4 / 13D vs existing `source_tier=2` news
- Cross-source dedup quality between 8-K and Benzinga
- EDGAR rate-limit response under sustained load (10 req/sec cap with token-bucket margin)

## Rollback

If anything breaks after enabling:
1. Set `EDGAR_FORM4_ENABLED=false` and `EDGAR_8K_ENABLED=false` in `user/.env`
2. Restart `synthos-portal` (no full revert needed — flags gate the entire feature)
3. The next news-agent run will skip EDGAR entirely; no data corruption to clean up
4. Existing `edgar_form4` / `edgar_8k` rows in `signals` and `news_feed` are inert (they'll expire under the existing tier-based expiry policy)

## Source tier rationale (Stage 4 H)

Each EDGAR source emits items at a fixed `source_tier` that the news-
agent's gate pipeline uses for confidence weighting and member-weight
calibration. Tier 1 is "Official" (SEC primary documents reporting
**actual** insider conviction); tier 2 is "Wire" (SEC filings reporting
**intent** or **passive events** that need interpretation).

| Source | Tier | Why this tier |
|---|---|---|
| `edgar_form4` | **1** | Direct insider transaction, filed within 2 business days of trade. The insider personally signed the filing. Open-market buys/sells (codes P/S) are the highest-signal public-disclosure insider data. Excludes M (option exercise) and tax-cover S patterns (Fix B). |
| `edgar_13d` | **1** | Activist crossing 5% **with stated intent to influence**. Filer is in operator's curated activist registry. Combination of (a) SEC primary doc + (b) registry-verified known activist + (c) explicit non-passive intent puts this at top tier. |
| `edgar_8k` | **2** | Corporate disclosure of a material event. SEC primary, but interpretation needed — "Item 5.02" could mean a board chair retirement (tier-2 noise) or the CEO being fired (tier-1 catalyst). The item code is a category, not an event severity. With body fetch (Stage 4 C) the headline carries actual filing text; without it, gate-7 sentiment defaults to NEUTRAL. |
| `edgar_form144` | **2** | INTENT to sell, not action. ~70% of modern Form 144s are 10b5-1 plan executions where the trade decision was made months ago — the timing is mechanical, not current information. Some 144s never result in actual sales. Tier-2 captures "this is informational but not insider conviction." |
| `edgar_13g` | **2** | Passive ≥5% crossing. By definition NOT activism, even when filed by a known activist. A known-activist 13G is interesting (quiet accumulation before possible 13D conversion) but the form itself signals "I'm not (currently) here to push for change." |
| Alpaca news (existing) | 2-3 | Wire/aggregator coverage. Tier varies by sub-source (Bloomberg/Reuters tier 2; Benzinga/PR Newswire tier 3 or 4). EDGAR primary docs at the same tier rank above news coverage of the same event via `SOURCE_PREFERENCE` in cross_source_dedup. |

**Why tier matters in the gate pipeline:**
- **Gate 12 (confirmation)** — tier-1 + has_primary_source → confirmation_score 1.0
- **Gate 18 (risk discounts)** — tier-3-with-no-primary triggers misinformation discount; tier-1 sources sidestep it entirely
- **Member-weight scoring** — uses source tier as input weight; bigger swings on tier-1 hits

**Why we don't use tier-1 for everything**
The gate pipeline was designed for a world of tier-2-3 wire/aggregator news, where occasional tier-1 STOCK Act filings stood out. Promoting all SEC sources to tier 1 would erode that signal — every 8-K (3-5k/business day across the market) would arrive at the same priority as a CEO Form 4 buy. The 1 vs 2 split distinguishes "insider acted with conviction" from "company filed a routine disclosure."

**If you disagree with a tier**
Edit `agents/news/edgar_<source>.py` — `source_tier` is set in one place per fetcher.  Update the rationale row above when you change it.

## Files

- `synthos_build/agents/news/edgar_client.py` — rate-limited HTTP client
- `synthos_build/agents/news/edgar_form4.py` — Form 4 parser + ingestion (Stage 1)
- `synthos_build/agents/news/edgar_8k.py` — 8-K item-code filter + ingestion (Stage 1)
- `synthos_build/agents/news/edgar_13d.py` — 13D activist filer ingestion (Stage 2)
- `synthos_build/agents/news/edgar_form144.py` — Form 144 parser + ingestion (Stage 3)
- `synthos_build/agents/news/edgar_13g.py` — 13G activist filer ingestion (Stage 3)
- `synthos_build/agents/news/activist_registry.py` — known-activist CIK lookup (shared by 13D + 13G)
- `synthos_build/agents/news/cross_source_dedup.py` — cluster cross-source coverage (Stage 4 E)
- `synthos_build/data/activists.json` — operator-curated CIK list (empty by default; populate before enabling 13D/13G)
- `synthos_build/agents/retail_news_agent.py` — wiring point in `run()` (look for `EDGAR sources` block)
- `synthos_build/tests/edgar/` — unit tests + fixtures
