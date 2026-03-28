# SYNTHOS AGENT ENHANCEMENT PLAN
## Retail Agent Logic Refinements (v3.1 Planning)

**Date:** March 28, 2026
**Status:** Design Phase
**Scope:** DisclosureResearchAgent, MarketSentimentAgent, ExecutionAgent refinements

---

## PART 1: DISCLOSURE RESEARCH AGENT ENHANCEMENTS

### 1.1 News Source Architecture

**Master List (Company-level):** 200+ sources
- Organized by region (Americas, Europe, Asia-Pacific, etc.)
- Tiered by trustworthiness: Government → Financial News → Independent Research → Opinion/Analysis
- Connection types: API, RSS, HTML scraping
- Language: tracked for translation pipeline
- **Future:** Custom translation system (move to Company Pi for scale)

**Retail Subset:** 50 curated feeds
- Heavy on government sources (Congressional disclosures, SEC filings)
- Major financial news (Bloomberg, Reuters, WSJ, FT)
- Regional government announcements (trade, policy)
- Independent trusted research
- Excludes: conspiracy theories, sensationalism, pure opinion

### 1.2 Deduplication & Staleness Logic

**Staleness Curve (Publication Date as Gate):**
```
0-24 hours:        100% value (fresh)
1-7 days:          100% → 20% value (linear decay)
7-45 days:         20% → 0% value (linear decay)
45+ days:          DISCARD (completely stale)
```

**Event Date Extraction:**
- Use publication date as gate
- Parse article for event date references
- If event date >> publication date → apply staleness curve to event date

**Deduplication Method:**
- Semantic similarity (NOT LLM-based)
- Ticker/company name matching
- Fuzzy headline matching (Levenshtein distance)
- Time window check (same story within 4 hours = duplicate)
- Track: which sources + spread pattern

**[FUTURE REVISION NEEDED]** Semantic similarity algorithm needs tuning for financial vs. policy vs. disclosure news.

### 1.3 Research Agent Output

For each news article that passes the gate:

```json
{
  "ticker": "AAPL",
  "headline": "Apple announces renewable energy initiative",
  "published_date": "2026-03-28",
  "event_date": "2026-03-28",
  "sentiment_score": +0.8,
  "industry_affected": "technology",
  "industry_impact": "positive",
  "duplicate_counter": 4,
  "duplicate_regions": ["global", "regional"],
  "relevance_to_us_stocks": 0.95,
  "staleness_value": 1.0,
  "source": "Reuters",
  "source_tier": "trusted_financial",
  "article_url": "..."
}
```

---

## PART 2: MARKET SENTIMENT AGENT ENHANCEMENTS

### 2.1 Priority Handling Stack

```
1. Current holdings (open positions) — HIGHEST PRIORITY
2. ExecutionAgent pre-purchase requests
3. New market information
```

### 2.2 Sentiment Wave Tracking

Receives from ResearchAgent:
- `duplicate_counter`: how many sources reporting
- `duplicate_regions`: local/regional/global spread
- Wave = momentum indicator (not exit signal by itself)

**Wave Interpretation:**
- Single source with bad news = low signal
- 4+ sources globally = potential sentiment shift worth monitoring
- Rapid spread across regions = accelerating sentiment

### 2.3 Holdings-First Analysis

For each open position:

```
1. Receive news + sentiment from ResearchAgent
2. Determine: is this news GOOD or BAD for THIS position?
   Example: "Renewable energy spreading" = GOOD for green stocks, BAD for fossil fuel holdings
3. Cross-reference 3-month price history:
   - Does sentiment align with price movement?
   - Does sentiment contradict price trend? (red flag)
4. Check wave spread:
   - Is this isolated or gaining momentum globally?
5. Exit flag if:
   - Sentiment direction = negative FOR YOUR HOLDING
   - Price history confirms vulnerability
   - Wave spreading globally (not isolated news)
```

### 2.4 Context-Aware Analysis

- News is not universally good/bad — context matters
- Same news can be positive for one industry, negative for another
- Agent must understand position exposure + industry exposure

---

## PART 3: EXECUTION AGENT ENHANCEMENTS

### 3.1 Load Modes (Updated)

```
SUPERVISED:  Customer approves trades via retail portal
AUTONOMOUS:  Pre-defined rules + unlock key
DEACTIVATED: Agent disabled, no execution
```

### 3.2 Pending Approvals Flow (CHANGED)

**OLD:** Pending approvals → project manager
**NEW:** Pending approvals → retail customer portal

Customer sees:
- Proposed trade (buy/sell ticker + quantity)
- Rationale (news, sentiment, company analysis)
- Risk alerts (overvaluation warning, etc.)
- Customer approves or rejects

### 3.3 Decision Matrix

**STRONG Company (Good financials + revenue + market position):**

```
+ GOOD news + STRONG industry
  → BUY (growth opportunity)
  → ⚠️ Alert: Watch for overvaluation
       Is market pushing price past intrinsic value?
       [FUTURE: Bubble detection logic]

+ BAD news + STRONG industry
  → BUY (contrarian, temporary depression)

+ GOOD news + sentiment collapsing
  → SELL (reversal signal)
```

**WEAK Company (Declining financials or weak position):**

```
+ GOOD sentiment + GOOD news
  → CLAUDE SONNET ANALYSIS:
      - Legitimate turnaround or pump-and-dump?
      - Institutional accumulation or retail hype?
      - Financial recovery signs?
      → BUY if credible, PASS if manipulation detected

+ BAD news + BAD sentiment
  → SELL (definite exit)
```

### 3.4 Claude Sonnet Integration

ExecutionAgent calls Claude Sonnet to analyze:

1. **Company fundamentals vs. sentiment** (1-year + 5-year scales)
2. **Industry health** (company-specific issue vs. sector-wide?)
3. **Contrarian opportunity detection** (strong company, temporary bad news)
4. **Weak company credibility** (turnaround vs. manipulation)
5. **Overvaluation risk** (momentum bubble detection)
6. **Financial analysis** (revenue trends, profitability, debt, growth)

Claude inputs:
- News content (headline, summary)
- Company financials (from SEC Edgar)
- Sentiment scores
- Price history (3-month + 1-year)
- Industry context

### 3.5 S&P 500 Baseline Tracking

**Daily tracked metrics:**
- Volatility (VIX proxy)
- Average P/E ratio
- Dividend yield
- Sector rotation (leading/lagging sectors)
- YTD return
- 3-month momentum
- Market breadth (% stocks above 200-day moving average)

**ExecutionAgent success metric:**
- Beat S&P 500 returns
- Track daily performance vs. baseline
- Report to customer portal

### 3.6 Risk Management

**Trailing Stop-Loss:**
- Set on every purchase
- Monitor continuously
- Sell at earliest ability if breached
- Log all stop-loss triggers

**Overvaluation Alert:**
- Detect if purchase price puts stock significantly above historical P/E + sector average
- Alert customer on portal (do NOT prevent purchase, just warn)
- Flag for future review

**Financial Disclosure Analysis:**
- Pull SEC Edgar filings automatically
- Analyze: revenue trends, profitability, debt/equity ratio, growth trajectory
- Use in company strength assessment

### 3.7 Stock Outperformance List

**Maintain list of:**
- Stocks that occasionally beat S&P 500
- Historical outperformance frequency
- Sectors/characteristics that outperform

**Use as:**
- Secondary benchmark
- Pattern recognition for agent learning
- Portfolio allocation hints

---

## PART 4: IMPLEMENTATION ORDER

### Phase 1 (Foundation)
1. Master news source list (company-level)
2. Retail 50-feed curated subset
3. S&P 500 daily metrics tracking
4. Stock outperformance list

### Phase 2 (Agent Logic)
5. ResearchAgent: deduplication + staleness decay
6. ResearchAgent: regional spread tracking
7. SentimentAgent: priority handling + holdings-first
8. SentimentAgent: wave tracking + context analysis

### Phase 3 (Execution)
9. ExecutionAgent: decision matrix + Claude Sonnet integration
10. ExecutionAgent: trailing stop-loss
11. ExecutionAgent: overvaluation detection
12. ExecutionAgent: financial disclosure analysis

### Phase 4 (Future)
13. Custom translation system (move to Company Pi)
14. Bubble detection refinement
15. Semantic similarity tuning
16. Performance tracking + reporting

---

## PART 5: DATABASE SCHEMA ADDITIONS

**signals table** (additions):
- `duplicate_counter` — how many sources reported this
- `duplicate_regions` — ['local', 'regional', 'global']
- `sentiment_wave_flag` — boolean
- `industry_impact` — string (positive/negative/neutral for THIS holding)
- `staleness_value` — decimal (1.0 → 0.0 decay curve)

**positions table** (additions):
- `trailing_stop_price` — decimal
- `entry_reason` — string (contrarian, growth, turnaround, etc.)
- `overvaluation_risk` — boolean
- `company_strength` — enum (strong, moderate, weak)

**signals_sentiment_history** (new):
- Track sentiment score over time per ticker
- Compare to price movement
- Identify sentiment-price divergence

---

## PART 6: FUTURE REVISIONS

**[FUTURE]** Overvaluation/bubble detection:
- P/E ratio vs. historical + sector average
- Institutional flow vs. retail sentiment
- Distance from intrinsic value estimates
- Momentum reversal patterns

**[FUTURE]** Custom translation system:
- Move to Company Pi for scale
- Support 30+ languages
- Preserve source context in translation

**[FUTURE]** Semantic similarity tuning:
- Financial vs. policy vs. disclosure nuances
- Event correlation detection
- Multi-story narrative tracking

---

**Version:** 1.0 (Design Phase)
**Last Updated:** March 28, 2026
**Next Review:** After Phase 1 completion
