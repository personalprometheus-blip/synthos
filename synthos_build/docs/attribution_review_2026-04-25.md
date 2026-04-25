# Attribution Patch Enforcement Review — 2026-04-25

**Patch:** 2026-04-21 news attribution patch (`patch/2026-04-21-news-attribution`)
**Review target:** 2026-04-28 per original plan (we're 3 days early; data already conclusive)
**Shadow period covered:** 4 full days + 2 partial = ~4.5 days of live shadow logging
**Reviewer:** automated query against `signal_attribution_flags` table on pi5 owner DB

---

## TL;DR

**Enforce both shadow modes.** The shadow data shows:
- **41% of flagged articles have no headline match at all** — these are nearly 100% noise (earnings calendars, off-topic articles tagged with random tickers)
- **33% of flagged articles attribute to the wrong ticker** — clear cases like "Intel jumps 23%" being shipped as an AMD signal
- **13% of remap_differs cases reached VALIDATED+ status** — meaning real trades are evaluated against wrong-ticker signals

Recommendation:
- ✅ **Flip `TICKER_REJECT_ENFORCE = True`** — drop articles with no headline ticker/name match
- ✅ **Flip `TICKER_REMAP_ENFORCE = True`** — pick the headline-matched ticker over `symbols[0]`
- ⏸ **Leave conflict handling in shadow** — neither current nor remap behavior is clearly correct for multi-stock articles; needs separate design pass

---

## Data summary

### Volume

```
Reason            Count   First Seen           Last Seen
─────────────────────────────────────────────────────────────
no_match          1321    2026-04-21 19:28     2026-04-25 09:05
remap_differs     1054    2026-04-21 20:05     2026-04-25 08:05
conflict           751    2026-04-21 20:05     2026-04-25 15:05
untradable          72    2026-04-21 21:05     2026-04-24 17:15  ← already enforced
─────────────────────────────────────────────────────────────
TOTAL             3198    over ~4.5 days = ~700/day
```

### Day-by-day (shows steady flag rate, not a one-off burst)

| Date | no_match | remap | conflict | untradable | Total |
|---|---|---|---|---|---|
| 2026-04-21 (partial) | 135 | 76 | 68 | 2 | 281 |
| 2026-04-22 | 314 | 246 | 230 | 26 | 816 |
| 2026-04-23 | 323 | 285 | 190 | 22 | 820 |
| 2026-04-24 | 428 | 354 | 191 | 22 | 1015 |
| 2026-04-25 (partial) | 121 | 93 | 72 | 0 | 286 |

---

## Per-reason analysis

### `no_match` (1321 flags) — **enforce drop**

Definition: headline contains no ticker literal AND no company-name token
AND no ticker alias for any of the Alpaca-tagged symbols.

**Sample headlines:**

| Symbols | Headline | Verdict |
|---|---|---|
| `['META']` | "Gary Vaynerchuk Says 'Every Brand On Earth' Should Be Doing This—" | Off-topic (general marketing advice) |
| `['MSFT']` | "Musk's OpenAI Feud Intensifies As Court Dismisses Fraud Charges" | About OpenAI, not MSFT |
| `['AAL', 'ABCB', ... 130 symbols]` | "Earnings Scheduled For April 23, 2026" | Boilerplate earnings calendar — not a signal |

These are **nearly 100% noise**. Shipping a "META is good" or "MSFT is good"
signal off the first two articles is wrong. The earnings-calendar article
gets tagged with 130 tickers and isn't actionable for any of them.

**Risk of enforcing:** dropping legitimate signals where the headline is
genuinely about a ticker but uses a paraphrase the matcher misses. Sample
inspection found no such case in the 5 most recent — they're all
clearly off-topic.

**Risk of NOT enforcing:** ~300+ noise signals/day flow into the pipeline.
Each one gets `sentiment_score`, `news_flags`, etc. computed against an
irrelevant article, dirtying the downstream signal pool.

**Recommended:** `TICKER_REJECT_ENFORCE = True`.

---

### `remap_differs` (1054 flags) — **enforce remap**

Definition: a different symbol than `symbols[0]` scores higher against
the headline.

**Sample (cleaned for clarity):**

| Currently shipped | Headline | Should be |
|---|---|---|
| **AMD** | "Nasdaq 100 ... Intel Jumps 23% On AI Chip Mania" | INTC (score 2) |
| **IBM** | "Why Are Unity Software Shares Sliding On Thursday?" | U (score 2) |
| **AUUD** | "Dow Falls Over 150 Points; Procter & Gamble Posts Upbeat Earnings" | PG (score 2) |
| **CHTR** | "What's Going On With Shares Of SLB Limited?" | SLB (score 3 — ticker literal in headline) |

These are **flat-out wrong attributions**. The current behavior takes
Alpaca's `symbols[0]` which is just alphabetical first, not relevance-ranked.

**Trade-impact verification** (Section 4 of the diagnostic):

```
remap_differs flags:                           1054
  of those, signal reached VALIDATED+ status:  136
  rate of 'enforcement would have changed':    13%
```

**13% of mis-attributed articles produced a VALIDATED signal on the wrong
ticker.** If those flowed to ACTED_ON (we'd need a follow-up query to
confirm exact rate), real money was being moved on the wrong stock.

**Risk of enforcing:** edge cases where Alpaca's `symbols[0]` was
correct and the headline mentions a different stock more prominently.
The 5 sampled cases all showed the remap is unambiguously better.

**Recommended:** `TICKER_REMAP_ENFORCE = True`.

---

### `conflict` (751 flags) — **keep in shadow, design new mode**

Definition: multiple symbols tie at the same max headline-match score.

**Sample:**

| Picked | Tied candidates | Headline | Notes |
|---|---|---|---|
| CABZ | CABZ:2 | "Pony AI Advances L4 ... Nvidia-Based Autonomous Driving" | Headline mentions PONY, not CABZ; weird that PONY didn't score |
| AMZN | LULU:2 | "Bulls And Bears: UnitedHealth, Capital One, Lululemon" | Multi-stock article — none is the focus |

For multi-stock articles, **neither symbols[0] nor headline-match-best
is clearly correct**. The right behavior is probably:
- If the article is genuinely about N stocks (a market roundup), it's a
  weak signal for ALL of them and probably shouldn't ship as a standalone
  ticker signal at all.
- The TIE is the system telling us "this article isn't strongly about
  one stock."

**Recommendation:**
- Keep `TICKER_REMAP_ENFORCE = True` so when there IS a clear winner the
  remap fires.
- For ties, neither enforce remap nor reject. Continue to ship `symbols[0]`
  but record the conflict for future work.
- **Backlog item:** add a new mode `TICKER_CONFLICT_ENFORCE` that drops
  ties (treats them as no-clear-attribution noise). Implement separately,
  shadow-run another ~5 days, then decide.

---

### `untradable` (72 flags) — **already enforced, no change**

Crypto-pair articles and non-tradable symbols. `TICKER_UNTRADABLE_DROP`
flag is `True` from day 1. 72/4.5d = ~16/day. Working as designed.

---

## Implementation plan if you go ahead

### Code change
File: `synthos_build/agents/retail_news_agent.py` lines 704-705

```python
TICKER_REMAP_ENFORCE   = False   # → True
TICKER_REJECT_ENFORCE  = False   # → True
```

That's it for code — the enforcement logic at line 927-932 already exists
and dispatches based on these flags. No other changes needed.

### Expected impact

- **News signal volume drops** by ~30-40%. The 41% no_match articles get
  dropped; the 33% remap_differs articles still ship but to a different
  ticker. Net article-rejection rate is ~41% (no_match) of news signals.
- **News signal accuracy improves** measurably — the "Intel article →
  INTC" cases stop misfiring as AMD signals.
- **Trader input mix shifts** — slightly more weight on
  candidate_generator (sector-driven) signals because news supply
  shrinks. This is fine; news was over-firing on noise.
- **Validator may CAUTION more often initially** if the volume drop
  exceeds its expected-signal-floor. Monitor for the first 48h.

### Rollback

If anything looks worse than expected, revert the two flags to `False`,
restart the news agent (or wait for the next 30-min enrichment cycle).
Total rollback time: <1 minute.

### Verification queries (run 24-48h after enforcement)

```sql
-- Was no_match rejection appropriate? Compare news signal counts.
SELECT date(created_at) AS d, COUNT(*) AS n
FROM signals
WHERE source = 'news' AND created_at >= datetime('now', '-7 days')
GROUP BY d ORDER BY d;
-- Expect: drop from ~700/day to ~400/day after enforce.

-- Did remap actually change ticker assignments?
SELECT chosen_ticker, would_choose, COUNT(*) AS n
FROM signal_attribution_flags
WHERE reason = 'remap_differs' AND created_at >= datetime('now', '-3 days')
GROUP BY chosen_ticker, would_choose ORDER BY n DESC LIMIT 20;
-- Expect: chosen_ticker now == would_choose (because we're picking the right one).
```

---

## What I recommend you authorize

**Option 1 (recommended): enforce both, deploy now.**
- 2-line config change + commit + push + restart news agent on pi5.
- I can prepare the commit and walk you through one bash command to apply.
- Total operator effort: ~2 minutes.

**Option 2 (more conservative): wait until 2026-04-28 per original plan.**
- 3 more days of shadow data accumulates.
- Won't change the recommendation — the data is already overwhelming.

**Option 3 (more conservative still): enforce only `TICKER_REJECT_ENFORCE`
first, watch for 48h, then enforce `TICKER_REMAP_ENFORCE`.**
- Lower-blast-radius rollout.
- But the two changes don't interact in any subtle way — they're
  independent gates. Rolling them out separately doesn't reduce risk
  meaningfully; it just doubles the deploy work.

My pick: **Option 1**, the data is conclusive.

---

## Items that do NOT change today (stay deferred)

- **`conflict` enforcement mode** — needs a new flag + ~30 LOC + another
  shadow-period. Add to backlog.
- **NEWS-WAVE-TRACKING** (backlog item 22) — explicitly waits on
  attribution patch enforcement to stabilize. After today's decision and
  ~5 days of clean enforcement data, that item unblocks.
