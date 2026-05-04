# Signal Funnel Investigation — 2026-05-04

Investigation into why customer accounts have idle cash and the trader rarely opens new positions, despite an apparently healthy signal pipeline.

## TL;DR

- **Signal supply is healthy** — 1,092 signals in the last 7 days fleet-wide. Source mix: 79% Alpaca News (benzinga), 17% sector-screener candidates, 4% edgar_form4. Confidence: 90% MEDIUM, 9% LOW, 1% HIGH.
- **The dominant blocker is `BLOCK_SECTOR_UNKNOWN`** — a validator restriction that blocks new signals that map to the "unknown" sector. It's been continuously active.
- **Root cause:** existing positions with `sector=NULL` accumulate in the bias-detection agent as "unknown sector" concentration, trigger a CRITICAL bias finding, and the validator translates that into a `BLOCK_SECTOR_UNKNOWN` restriction.
- **Funnel reality:** 1,092 signals → 928 ticker-specific evaluations → 13 ACTED_ON → 33 positions opened in 7 days fleet-wide.
- **What got traded:** 12/13 from Alpaca News, 1 from sector-screener candidate. ZERO from politician/congressional sources. All trades were tier=2, mostly MEDIUM confidence (1 HIGH, 1 LOW, 11 MEDIUM).
- **Profit-taking control-flow bug** (separate finding, fixed in commit `581e31e` 2026-05-04): the elif chain in Gate 10 made the profit-taking branch unreachable for non-urgent positions. Fixed.

## Stage 1: Validator-stage funnel (shared signals DB)

Last 7 days, fleet-wide, in `synthos_build/user/signals.db`:

```
Status               Count    Sources                      Confidence
----------          ------   --------                     ----------
EXPIRED                526   Alpaca News (benzinga) 861   MEDIUM   985
EVALUATED              472   candidate              185   LOW       95
QUEUED                  55   edgar_form4             46   HIGH      12
WATCHING                26
ACTED_ON                13   Total: 1,092                 Total: 1,092
TOTAL              1,092
```

Conversion rate: **1.2% (signals to action)**.

### EXPIRED sample includes strong signals
e.g. AMZN: HIGH confidence + 0.76 screener + 0.85 sentiment, expired without trading. Indicates the funnel rejects strong signals, not just weak ones.

## Stage 2: Trader-gate funnel (per-customer system_log)

### v1 fleet (14 customers, 7 days)

```
TRADE_DECISIONs total:  19,872
With ticker:               928
Per-customer per-day:     ~200

Decisions:                  Top blocking gates:
  HALT      4,972             1_VALIDATOR     3,539  *  (see below — see Stage 4)
  SKIP        783             1_MARKET_TIME   1,978   (out-of-hours, expected)
  HOLD         48             1_API_HEALTH    1,139   (Alpaca flake)
  WATCH        40             4_LIQUIDITY       606
  EXIT         19             1_DAILY_LOSS      294
  MIRROR       19             4_SPREAD           61
  PARTIAL      18             4_CORRELATION      41
  ROTATE        1             4_EVENT_RISK       37
                              4_TICKER_DEDUP     32
```

\* `1_VALIDATOR` count is misleading — the gate **passes** (`result=True`) but flags the active restriction in its `inputs`. The actual block happens downstream when a signal's resolved sector matches the restricted sector.

### v2 customer (d88a744d)

```
TRADE_DECISIONs total:   2,050
With ticker:                34
Decisions:                   Top blocking gates:
  HALT      1,139              1_API_HEALTH   1,139   ← 56% of cycles fail Alpaca health
  SKIP         31              4_LIQUIDITY      20
  WATCH         2              4_SPREAD          3
  ROTATE        1              4_TICKER_DEDUP    2
                               11_POSITION_COUNT 1
                               4_EVENT_RISK      1
                               1_MARKET_TIME     1
```

**v2 is API-blocked:** 1,139 of 2,050 (56%) cycles fail at API_HEALTH. Likely flaky Alpaca paper credentials. Signal-evaluation only ran 34 times, almost all SKIPped.

Recent v2 SKIPs: LYFT, STX, SPOT, SPY, FCF, PFE, ARM, BX, PURR, PODD, BOIL, PSX, MA, SWK, MSFT.

## Stage 3: What actually traded (33 positions opened last 7 days)

```
Customer    Trades   Notable
66babea0       13   Most active. SPY, GOOGL, HAL, BB, QQQ, AAPL, NVDA, BBAI, OWL, BIL
46b10ff0        7   Same-ticker open/close pairs (TSLA×2, INTC×2, UBER×2)
f313a3d9        4   IREN, QQQ, JOBY×2
d88a744d        4   v2 customer — BIL, PTON, AMZN, AAPL
c5fc97cc        3   BIL, OPEN, CLF
30eff008        2   You — OPEN, CLF
```

Signal-source profile of the 13 ACTED_ON:
- All 13 from `tier=2` sources
- 12 from Alpaca News, 1 from sector-screener candidate
- 0 from politician/congressional sources
- Confidence mix: 11 MEDIUM, 1 HIGH, 1 LOW (NVDA, saved by 0.90 screener score)
- Many have null `screener_score` and null `sentiment_score` — the trader is acting on news + sentiment alone, not coordinated multi-source

### Data integrity gap

`positions.signal_id` is `None` for **every row**. Lost back-link from a position to the signal that triggered it. Worth fixing as a small TODO.

## Stage 4: The actual blocker — `BLOCK_SECTOR_UNKNOWN`

Validator's most recent decisions (sample):
```
2026-05-04 20:01:14  validator verdict=CAUTION restrictions=['BLOCK_SECTOR_UNKNOWN']
2026-05-04 20:00:03  validator verdict=CAUTION restrictions=['BLOCK_SECTOR_UNKNOWN']
2026-05-04 19:59:09  validator verdict=CAUTION restrictions=['BLOCK_SECTOR_UNKNOWN']
... continuous, at least last 2 days
```

### Source code path

`agents/retail_validator_stack_agent.py:447-449`:
```python
if 'sector' in bias_type.lower() or 'concentration' in bias_type.lower():
    sector = f.get('sector', f.get('detail', 'unknown'))   # ← falls back to 'unknown'
    messages.append(f"Sector concentration CRITICAL: {sector}")
    restrictions.append(f"BLOCK_SECTOR_{sector.upper().replace(' ', '_')}")
```

The bias-detection agent emits a CRITICAL finding with `sector='unknown'` (or no sector field) → validator turns it into `BLOCK_SECTOR_UNKNOWN`.

### Why bias-detection flags 'unknown' sector

Existing customer positions have `sector=NULL` rows (visible in Stage 3 data — many trades show sector='-'). These accumulate in bias-detection's per-sector concentration check, exceed the threshold for the "unknown" bucket, trigger CRITICAL.

### Two-sided fix needed

1. **Backfill sectors on existing positions** (so the alert clears)
2. **Fix sector resolution at signal-creation** (so new signals don't add to the unknown bucket)

## Action items, organized by ease

### Tier 1 — quick wins (under 1 hour each)

- [ ] **A1.** Verify `ticker_sectors` lookup table is populated; sample 10 rows. → if good, we have a way to backfill.
- [ ] **A2.** Backfill `positions.sector` where `sector IS NULL OR sector=''` using `ticker_sectors` join. One-shot script per customer DB.
- [ ] **A3.** Locate where new signals get a sector at creation time (`retail_news_agent.py` insertion path) and check if the lookup is being called. → grep + read.
- [ ] **A4.** Pull v2 customer's most recent API_HEALTH failure detail to see what error Alpaca returns. → SQL query, 5 min.
- [ ] **A5.** Backfill `signals.sector` where it's null using the same `ticker_sectors` lookup.
- [ ] **A6.** Save this investigation report (DONE — this file).

### Tier 2 — medium fixes (1-3 hours each)

- [ ] **B1.** Add post-creation sector backfill in `retail_news_agent.py` (or wherever benzinga news inserts signals): if `sector` is null after parse, call `ticker_sectors.get(ticker)` and stamp it.
- [ ] **B2.** Same backfill for `candidate_generator.py` (sector-screener candidates) and `edgar_form4` ingestion paths.
- [ ] **B3.** Fix `retail_bias_detection_agent.py` so a CRITICAL "unknown sector concentration" finding emits a more specific message + only fires when there genuinely are unknown-sector positions (not phantom).
- [ ] **B4.** Fix `signal_id` back-link on `positions` rows so trade audit trail is restored.
- [ ] **B5.** v2 customer API: rotate Alpaca paper creds; if expired, regenerate via `register_v2_test_customer.py --refresh`.

### Tier 3 — bigger work (multi-hour each)

- [ ] **C1.** Investigate why edgar_form4 / politician signals never get traded (0 of 13 ACTED_ON were politician-sourced despite being v1's central thesis).
- [ ] **C2.** Loosen the `BLOCK_SECTOR_UNKNOWN` restriction's stamping logic — maybe whitelist mid-cap tickers without ETF sector classification, or use TICKER_REMAP fallback.
- [ ] **C3.** Roll v2 (stat-arb-first scoring) fleet-wide once Tier 1 + Tier 2 have run for 1 trading day and the funnel is observably more open.

## Re-run plan after Tier 1 fixes

1. Apply A1-A5
2. Restart the trader on pi5 (subprocess respawn picks up code; no service restart needed for daemon-mode customers)
3. Wait 1 trading session for the funnel to flow
4. Re-run `funnel_investigation.py` and `funnel_v2_fix.py`
5. Compare: did `BLOCK_SECTOR_UNKNOWN` clear? Did `ACTED_ON` count rise above 13/week? Did v2's signal-evaluation count rise above 34/week?

## Side findings (note for later)

1. **`positions.sector='-'` is a common pattern.** Suggests sector resolution failed at trade-open time too, not just at signal-creation.
2. **Same-day open/close pairs in 46b10ff0** (TSLA×2, INTC×2, UBER×2) — possibly buy-then-immediate-stop pattern. Worth a separate look.
3. **66babea0 has 13 trades — most active customer.** What's different about their funnel that lets more signals through? Worth a separate investigation, may surface more unblockers.
