# EDGAR Ingestion — Operator Guide

**Status:** code landed 2026-04-27 on `patch/2026-04-27-news-edgar-expansion`.
All flags **default OFF**. No behavior change until enabled.

This guide covers Stage 1 (Form 4 + 8-K item-code filter). Stages 2-4 (13D, Form 144, 13G, polish) will extend this doc as they land.

## What this adds

Two new SEC EDGAR ingestion paths in `retail_news_agent.py`:

| Source | Form | Tier | Trigger | Flag |
|---|---|---|---|---|
| `edgar_form4` | Form 4 | 1 | Insider transactions ≥ $50K (P/S codes) | `EDGAR_FORM4_ENABLED` |
| `edgar_8k` | 8-K | 2 | Items 1.03 / 2.02 / 5.02 / 8.01 | `EDGAR_8K_ENABLED` |

Both feed the existing 22-gate news pipeline — same dedup, novelty, sentiment, gate-12 confirmation, gate-21 routing as Alpaca news. No new tables, no schema migrations.

## Required env (set before flipping any flag)

```bash
# SEC requires a real-user User-Agent.  Without these, every fetch 403s.
SEC_EDGAR_UA_NAME=Synthos
SEC_EDGAR_UA_EMAIL=<your contact email>

# Feature flags (default false in the absence of these vars)
EDGAR_FORM4_ENABLED=false
EDGAR_8K_ENABLED=false
```

Optional tuning vars:

```bash
# Min USD value for a Form 4 transaction to surface (default 50000).
# Lower this to test in low-volume; raise it if it floods the trader.
EDGAR_FORM4_MIN_USD=50000

# Comma-separated 8-K item codes to surface (default 1.03,2.02,5.02,8.01).
# Codes available: 1.01 1.03 2.02 2.05 5.02 7.01 8.01.
EDGAR_8K_ITEMS=1.03,2.02,5.02,8.01
```

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
6. Do NOT enable both for 2 weeks of stable operation before considering Stage 2 (13D activist filings).

## What's tested vs what's not

**Tested on Mac (26 unit tests, all green):**
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

**Not tested on Mac (defer to pi5 staged rollout):**
- Real EDGAR HTTP responses (their full-text-search payload shape may drift; we use a fixture)
- Real Form 4 XML diversity (officer titles, partial fills, multi-owner filings — only one canonical sample tested)
- Real 8-K item-code distribution under live load
- Trader Gate 5 scoring with new `source_tier=1` Form 4 vs existing `source_tier=2` news
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
- `synthos_build/agents/news/edgar_form4.py` — Form 4 parser + ingestion
- `synthos_build/agents/news/edgar_8k.py` — 8-K item-code filter + ingestion
- `synthos_build/agents/retail_news_agent.py` — wiring point in `run()` (look for `EDGAR sources` block)
- `synthos_build/tests/edgar/` — unit tests + fixtures
