# EDGAR Ingestion — Operator Guide

**Status:** code landed 2026-04-27 on `patch/2026-04-27-news-edgar-expansion`.
Stages 1 + 2 done; Stages 3 + 4 in progress. All flags **default OFF**. No behavior change until enabled.

This guide covers Stages 1 (Form 4 + 8-K) and 2 (13D activist filings). Stages 3 (Form 144, 13G) and 4 (polish) will extend this doc as they land.

## What this adds

Three new SEC EDGAR ingestion paths in `retail_news_agent.py`:

| Source | Form | Tier | Trigger | Flag |
|---|---|---|---|---|
| `edgar_form4` | Form 4 | 1 | Insider transactions ≥ $50K (P/S codes) | `EDGAR_FORM4_ENABLED` |
| `edgar_8k` | 8-K | 2 | Items 1.03 / 2.02 / 5.02 / 8.01 | `EDGAR_8K_ENABLED` |
| `edgar_13d` | SC 13D + 13D/A | 1 | Filer in `data/activists.json` registry | `EDGAR_13D_ENABLED` |

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
```

Optional tuning vars:

```bash
# Min USD value for a Form 4 transaction to surface (default 50000).
# Lower this to test in low-volume; raise it if it floods the trader.
EDGAR_FORM4_MIN_USD=50000

# Comma-separated 8-K item codes to surface (default 1.03,2.02,5.02,8.01).
# Codes available: 1.01 1.03 2.02 2.05 5.02 7.01 8.01.
EDGAR_8K_ITEMS=1.03,2.02,5.02,8.01

# Override path to the activist registry JSON (default
# synthos_build/data/activists.json).  Used by 13D classifier.
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

## What's tested vs what's not

**Tested on Mac (53 unit tests, all green):**
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

## Files

- `synthos_build/agents/news/edgar_client.py` — rate-limited HTTP client
- `synthos_build/agents/news/edgar_form4.py` — Form 4 parser + ingestion (Stage 1)
- `synthos_build/agents/news/edgar_8k.py` — 8-K item-code filter + ingestion (Stage 1)
- `synthos_build/agents/news/edgar_13d.py` — 13D activist filer ingestion (Stage 2)
- `synthos_build/agents/news/activist_registry.py` — known-activist CIK lookup (Stage 2)
- `synthos_build/data/activists.json` — operator-curated CIK list (empty by default; populate before enabling 13D)
- `synthos_build/agents/retail_news_agent.py` — wiring point in `run()` (look for `EDGAR sources` block)
- `synthos_build/tests/edgar/` — unit tests + fixtures
