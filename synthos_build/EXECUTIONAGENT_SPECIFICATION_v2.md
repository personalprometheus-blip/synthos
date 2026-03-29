# EXECUTION AGENT SPECIFICATION v2.0
## Complete 14-Step Decision Tree & Logic

**Document Version:** 2.0
**Date:** March 28, 2026
**Status:** Design (not yet optimized for Pi placement)
**Scope:** Full quantitative trading decision flow
**Model:** Built with full logic; optimization/splitting to retail/company later

---

## EXECUTIVE SUMMARY

ExecutionAgent is a multi-stage decision system that evaluates trading signals, applies quantitative controls, manages positions, and monitors portfolio health. It is not a single function but a **complete trading lifecycle orchestrator** with 14 distinct decision gates and 100+ control parameters.

**Flow:**
```
Local Analysis (Retail Pi)
  ↓
System/Benchmark Gates (permission)
  ↓
Regime Detection (context)
  ↓
Trade Eligibility (filters)
  ↓
Signal Evaluation (scoring)
  ↓
SEND TO COMPANY PI ← Company Comparator (detailed analysis)
  ↓ (receive decision + conditions)
Entry Decision (local confirmation)
  ↓
Position Sizing + Risk Setup
  ↓
Order Execution
  ↓
Active Trade Management (continuous)
  ↓
Portfolio Monitoring (continuous)
  ↓
Evaluation & Adaptation
```

---

## SECTION 1: SYSTEM GATE (Hard Stops)

**Purpose:** Absolute permission layer. If any hard stop is hit, HALT immediately.

### 1.1 Market Hours Check
```
IF current_time NOT IN trading_session_hours (9:30 AM - 4:00 PM ET)
  AND market != pre-market/after-hours mode
  → LOG "Market closed"
  → SKIP all trading activity
  → return HALT_NO_MARKET
```

### 1.2 Daily Loss Circuit Breaker
```
realized_pnl_today = SUM(closed_trade_pnl) for trades closed TODAY

IF realized_pnl_today <= -daily_loss_limit (e.g., -$5,000 or -5% portfolio)
  → LOG "Daily loss limit hit: $X"
  → SET mode = HALT
  → block new entries
  → allow exits only
  → send alert to portal
  → return HALT_DAILY_LOSS
```

### 1.3 Portfolio Drawdown Circuit Breaker
```
current_equity = portfolio_value
peak_equity = max(historical_equity)
current_drawdown_pct = (current_equity - peak_equity) / peak_equity

IF current_drawdown_pct <= -max_drawdown_threshold (e.g., -20%)
  → LOG "Portfolio drawdown limit: -X%"
  → SET mode = HALT
  → force close all positions (or hedge)
  → send critical alert to portal
  → return HALT_DRAWDOWN
```

### 1.4 Data Integrity Check
```
missing_data_ratio = (missing_candles / total_expected_candles)
stale_data_delay = (now - last_data_timestamp)

IF missing_data_ratio > data_integrity_threshold (e.g., >5%)
  OR stale_data_delay > max_latency (e.g., >2 minutes)
  → LOG "Data integrity failure"
  → SET mode = HALT
  → return HALT_DATA_FAILURE
```

### 1.5 API Connectivity Check
```
IF order_ack_timeout OR repeated_execution_errors (>N failures in window)
  → LOG "API failure detected"
  → SET mode = HALT
  → attempt graceful shutdown
  → return HALT_API_FAILURE
```

### 1.6 License Validation
```
license_status = validate_license()

IF license_status == EXPIRED or INVALID or REVOKED
  → LOG "License invalid"
  → SET mode = HALT
  → send alert to portal
  → return HALT_LICENSE
```

**Gate Output:**
- `system_status` ∈ {OK, HALT_NO_MARKET, HALT_DAILY_LOSS, HALT_DRAWDOWN, HALT_DATA, HALT_API, HALT_LICENSE}
- If any HALT → skip all downstream logic, return immediately

---

## SECTION 2: BENCHMARK GATE (S&P 500 Context)

**Purpose:** Determine market regime and overall trading mode based on S&P 500 behavior.

**Prerequisite:** System gate = OK

### 2.1 Benchmark Drawdown Check
```
SPX_current = get_SPX_price()
SPX_peak_rolling = max(SPX_price[last_N_days])
SPX_drawdown_pct = (SPX_current - SPX_peak_rolling) / SPX_peak_rolling

IF SPX_drawdown_pct <= -SPX_drawdown_threshold (e.g., -5%)
  → SET benchmark_status = DEFENSIVE
  → risk mode = reduced
  → position_size_multiplier = 0.5
  → tighten all stops
  ELSE IF SPX_drawdown_pct > -2% (shallow or positive)
  → continue to trend check
```

### 2.2 Benchmark Trend Check
```
SPX_MA_short = EMA(SPX_price, 20)
SPX_MA_long = EMA(SPX_price, 50)

IF SPX_MA_short > SPX_MA_long
  → benchmark_trend = UP
  ELSE IF SPX_MA_short < SPX_MA_long
  → benchmark_trend = DOWN
  ELSE
  → benchmark_trend = SIDEWAYS
```

### 2.3 Volatility Regime Check
```
SPX_ATR = ATR(SPX, 14)
SPX_volatility = SPX_ATR / SPX_current
vol_regime_threshold_high = 0.03  # 3% ATR/price = high vol
vol_regime_threshold_low = 0.01   # 1% ATR/price = low vol

IF SPX_volatility > vol_regime_threshold_high
  → volatility_regime = HIGH
  ELSE IF SPX_volatility < vol_regime_threshold_low
  → volatility_regime = LOW
  ELSE
  → volatility_regime = NORMAL
```

### 2.4 Benchmark Mode Decision
```
IF benchmark_status == DEFENSIVE
  → mode = DEFENSIVE

ELSE IF benchmark_trend == UP AND volatility_regime ∈ {LOW, NORMAL}
  → mode = AGGRESSIVE
  → position_size_multiplier = 1.5

ELSE IF benchmark_trend == DOWN
  → mode = NEUTRAL
  → position_size_multiplier = 1.0

ELSE (sideways/mixed)
  → mode = NEUTRAL
  → position_size_multiplier = 1.0
```

**Gate Output:**
- `benchmark_mode` ∈ {AGGRESSIVE, NEUTRAL, DEFENSIVE}
- `volatility_regime` ∈ {LOW, NORMAL, HIGH}
- `SPX_trend` ∈ {UP, DOWN, SIDEWAYS}

---

## SECTION 3: REGIME DETECTION (Market Context)

**Purpose:** Identify current market regime(s) to adjust entry/exit logic.

**Prerequisite:** Benchmark gate passed

### 3.1 Volatility Regime (Asset-Specific)
```
asset_ATR = ATR(asset_price, 14)
asset_volatility = asset_ATR / asset_current_price
realized_vol = stdev(returns[last_20_days])

IF realized_vol > vol_high_threshold (e.g., 0.04)
  → volatility_regime = HIGH
  → position_size *= 0.6  # reduce
  → stop_loss_multiplier *= 1.2  # widen

ELSE IF realized_vol < vol_low_threshold (e.g., 0.01)
  → volatility_regime = LOW
  → position_size *= 1.2  # expand
  → stop_loss_multiplier *= 0.9  # tighten

ELSE
  → volatility_regime = NORMAL
```

### 3.2 Trend Regime (Asset-Specific)
```
MA_short = EMA(asset_price, 20)
MA_long = EMA(asset_price, 50)

IF MA_short > MA_long
  → trend_regime = UPTREND

ELSE IF MA_short < MA_long
  → trend_regime = DOWNTREND

ELSE IF abs(MA_short - MA_long) < flat_threshold (e.g., <0.5%)
  → trend_regime = SIDEWAYS
```

### 3.3 Mean-Reversion vs Momentum Dominance
```
# Rolling correlation between price changes and trend
correlation_to_trend = corr(price_return, trend_direction)[last_20_days]

IF correlation_to_trend > 0.6
  → regime_type = MOMENTUM_DOMINANT
  → disable mean-reversion entry logic

ELSE IF correlation_to_trend < 0.3
  → regime_type = MEAN_REVERSION_DOMINANT
  → enable mean-reversion entry logic

ELSE
  → regime_type = MIXED
  → enable both entry types
```

### 3.4 Risk-Off / Risk-On Macro Check
```
SPX_direction = UP or DOWN (from Section 2)
bond_direction = get_bond_direction()  # TLT or IEF
credit_spreads = get_credit_spreads()   # HY OAS

IF SPX_direction == DOWN
  AND bond_direction == UP
  AND credit_spreads > normal_range
  → macro_regime = RISK_OFF
  → SET mode = DEFENSIVE (override if not already)
  → reduce gross_exposure
  → increase hedge_allocation

ELSE IF SPX_direction == UP
  AND bond_direction == DOWN OR FLAT
  AND credit_spreads <= normal_range
  → macro_regime = RISK_ON

ELSE
  → macro_regime = MIXED
```

### 3.5 Correlation Regime Check
```
portfolio_assets = all open positions
pairwise_correlations = corr_matrix(all_asset_returns)
avg_correlation = mean(pairwise_correlations)

IF avg_correlation > corr_spike_threshold (e.g., 0.75)
  → correlation_regime = SPIKE
  → diversification benefit reduced
  → reduce gross_exposure
  → block highly correlated new trades

ELSE
  → correlation_regime = NORMAL
```

**Regime Output:**
- `volatility_regime` ∈ {LOW, NORMAL, HIGH}
- `trend_regime` ∈ {UPTREND, DOWNTREND, SIDEWAYS}
- `regime_type` ∈ {MOMENTUM_DOMINANT, MEAN_REVERSION_DOMINANT, MIXED}
- `macro_regime` ∈ {RISK_OFF, RISK_ON, MIXED}
- `correlation_regime` ∈ {NORMAL, SPIKE}

---

## SECTION 4: TRADE ELIGIBILITY FILTER

**Purpose:** Pre-trade checks to ensure trade is safe to attempt.

**Prerequisite:** Benchmark gate + regime detection passed

### 4.1 Liquidity Check
```
avg_volume_20d = mean(volume[last_20_days])
current_bid_ask_spread = (ask - bid) / mid_price

IF avg_volume_20d < min_volume_threshold (e.g., 1M shares for equity)
  → LOG "Insufficient liquidity: volume = X"
  → eligibility = FAIL
  → return SKIP_LOW_LIQUIDITY

ELSE IF current_bid_ask_spread > spread_threshold (e.g., 0.5%)
  → LOG "Spread too wide: X%"
  → eligibility = FAIL
  → return SKIP_WIDE_SPREAD

ELSE
  → liquidity_check = PASS
```

### 4.2 Event Risk Filter
```
event_calendar = [FOMC, CPI, NFP, earnings_dates, ...]
current_time_to_event = time_to_next_event()

IF current_time_to_event < event_risk_window (e.g., 4 hours before + 2 hours after)
  → LOG "Event risk window active"
  → eligibility = FAIL (or REDUCED_SIZE)
  → return SKIP_EVENT_RISK

ELSE
  → event_risk_check = PASS
```

### 4.3 Correlation Exposure Check
```
proposed_trade_asset = new trade ticker
portfolio_assets = all currently held tickers

correlation_to_portfolio = mean(corr(proposed_asset, portfolio_assets))

IF correlation_to_portfolio > max_corr_to_portfolio (e.g., 0.8)
  → LOG "High correlation to existing portfolio"
  → eligibility = FAIL
  → return SKIP_HIGH_CORRELATION

ELSE
  → correlation_check = PASS
```

### 4.4 Sector Exposure Check
```
proposed_sector = sector(proposed_asset)
current_sector_weight = sum(position_value[sector == proposed_sector]) / total_portfolio

sector_weight_limit = 0.25  # max 25% in one sector

IF current_sector_weight >= sector_weight_limit
  → LOG "Sector weight limit approaching"
  → eligibility = REDUCED_SIZE

ELSE
  → sector_check = PASS
```

### 4.5 Gross Exposure Check
```
current_gross_exposure = sum(abs(position_value)) / equity
gross_exposure_cap = 2.0  # 200% (2x leverage)

IF current_gross_exposure >= gross_exposure_cap
  → LOG "Gross exposure at cap"
  → eligibility = FAIL
  → return SKIP_EXPOSURE_CAP

ELSE
  → exposure_check = PASS
```

**Filter Output:**
- `eligibility` ∈ {PASS, REDUCED_SIZE, FAIL}
- `skip_reason` if FAIL ∈ {LOW_LIQUIDITY, WIDE_SPREAD, EVENT_RISK, HIGH_CORRELATION, SECTOR_CAP, EXPOSURE_CAP}

---

## SECTION 5: SIGNAL EVALUATION

**Purpose:** Score and weight incoming signals before decision.

**Prerequisite:** Eligibility filter passed

### 5.1 Signal Confidence Score
```
signal = {
  source: "DisclosureResearchAgent" or "MarketSentimentAgent",
  confidence: 0.0-1.0,
  sentiment: "BUY" or "SELL",
  score: -1.0 to +1.0,
  industry_impact: "positive" or "negative" or "neutral",
  wave_count: integer (how many sources reporting same news),
  regions: ["local", "regional", "global"]
}

IF signal.confidence < min_confidence_threshold (e.g., 0.5)
  → LOG "Signal confidence too low"
  → return SKIP_LOW_CONFIDENCE

ELSE
  → signal_passes_confidence
```

### 5.2 Multi-Factor Signal Weighting
```
# If multiple signals active for same asset
signals_active = [signal_1, signal_2, signal_3, ...]

final_score = 0
FOR each signal IN signals_active:
  weight_i = confidence_i * sector_relevance_i * temporal_decay_i
  final_score += (signal_score_i * weight_i)

final_score = final_score / sum(weights)  # normalize
```

### 5.3 Benchmark-Relative Strength Check
```
# If strategy requires alpha (beating S&P 500)
asset_return_1month = (current_price - price_1month_ago) / price_1month_ago
SPX_return_1month = (SPX_current - SPX_1month_ago) / SPX_1month_ago

relative_strength = asset_return_1month - SPX_return_1month

IF signal.sentiment == "BUY" AND relative_strength < -0.10 (significantly underperforming)
  → asset is lagging benchmark
  → may still be valid contrarian play

ELSE IF signal.sentiment == "BUY" AND relative_strength > 0.20 (significantly outperforming)
  → asset is leading benchmark
  → check for momentum vs. overextension
```

### 5.4 Signal Decay Over Time
```
# Older signals lose potency
time_since_signal = now - signal_creation_time
signal_decay_factor = exp(-0.01 * time_since_signal_hours)  # 1% per hour

adjusted_signal_score = signal.score * signal_decay_factor
```

**Signal Output:**
- `final_signal_score` ∈ {-1.0 to +1.0}
- `adjusted_confidence` ∈ {0.0 to 1.0}
- `is_valid_for_trade` ∈ {True, False}

---

## SECTION 6: ENTRY DECISION

**Purpose:** Determine WHAT trade to execute and WHY.

**Prerequisite:** Signal evaluation passed

### 6.1 Momentum Entry Condition
```
# Price breaking out or trending higher
price_current = current_price
MA_short = EMA(price, 20)
MA_long = EMA(price, 50)
ROC = rate_of_change(price, 14)

momentum_entry = (
  price_current > MA_short
  AND MA_short > MA_long
  AND ROC > momentum_threshold (e.g., +0.02)
  AND trend_regime == UPTREND
)

IF momentum_entry == TRUE
  AND signal.sentiment == "BUY"
  AND regime_type ∈ {MOMENTUM_DOMINANT, MIXED}
  → candidate_entry_type = MOMENTUM
  → candidate_score = final_signal_score * 1.2  # boost for regime alignment
```

### 6.2 Mean-Reversion Entry Condition
```
# Price oversold relative to mean
price_mean = mean(price[last_60_days])
price_std = stdev(price[last_60_days])
z_score = (price_current - price_mean) / price_std

mean_reversion_entry = (
  z_score < mean_reversion_entry_threshold (e.g., -1.5)
  AND trend_regime IN {SIDEWAYS, DOWNTREND}
  AND volatility_regime == HIGH
)

IF mean_reversion_entry == TRUE
  AND signal.sentiment == "BUY"
  AND regime_type ∈ {MEAN_REVERSION_DOMINANT, MIXED}
  → candidate_entry_type = MEAN_REVERSION
  → candidate_score = final_signal_score * 1.15
```

### 6.3 Breakout Entry Condition
```
# Price breaks above resistance
rolling_high_20d = max(price[last_20_days])
rolling_high_52w = max(price[last_252_days])
breakout_threshold = rolling_high_20d * 1.01  # 1% above high

breakout_entry = (
  price_current > breakout_threshold
  AND volume > avg_volume * 1.5  # volume confirmation
  AND trend_regime == UPTREND
)

IF breakout_entry == TRUE
  AND signal.sentiment == "BUY"
  → candidate_entry_type = BREAKOUT
  → candidate_score = final_signal_score * 1.1
```

### 6.4 Pullback Entry Condition
```
# Price retraces within uptrend
pullback_depth = (MA_short - price_current) / MA_short

pullback_entry = (
  trend_regime == UPTREND
  AND pullback_depth ∈ {0.01, 0.05}  # 1-5% pullback
  AND volume < avg_volume * 1.2  # low volume pullback
  AND price_current > MA_long  # still above long MA
)

IF pullback_entry == TRUE
  AND signal.sentiment == "BUY"
  → candidate_entry_type = PULLBACK
  → candidate_score = final_signal_score * 1.05
```

### 6.5 Entry Decision
```
candidates = [
  {type: MOMENTUM, score: X, condition: true/false},
  {type: MEAN_REVERSION, score: Y, condition: true/false},
  {type: BREAKOUT, score: Z, condition: true/false},
  {type: PULLBACK, score: W, condition: true/false}
]

valid_candidates = [c for c in candidates if c.condition == TRUE]

IF len(valid_candidates) == 0
  → return NO_ENTRY_CONDITION

ELSE IF len(valid_candidates) >= 1
  → selected_entry = argmax(valid_candidates, key="score")
  → entry_type = selected_entry.type
  → entry_confidence = selected_entry.score
```

**Entry Output:**
- `entry_type` ∈ {MOMENTUM, MEAN_REVERSION, BREAKOUT, PULLBACK, NONE}
- `entry_price` = current_price
- `entry_confidence` ∈ {0.0 to 1.0}

---

## SECTION 7: POSITION SIZING

**Purpose:** Determine trade quantity based on risk, volatility, and mode.

**Prerequisite:** Entry decision made

### 7.1 Base Position Size (Risk-Based)
```
portfolio_equity = current_equity
risk_per_trade_pct = 0.02  # risk 2% of portfolio per trade
risk_per_trade_dollars = portfolio_equity * risk_per_trade_pct

# User sets stop loss distance (see Section 8)
stop_distance = entry_price - stop_loss_price
stop_distance_pct = stop_distance / entry_price

base_size = risk_per_trade_dollars / (entry_price * stop_distance_pct)
```

### 7.2 Volatility Adjustment
```
asset_volatility = realized_vol (from Section 3)
target_volatility = 0.02  # target 2% daily vol for position
volatility_adj = target_volatility / asset_volatility

adjusted_size = base_size * volatility_adj
```

### 7.3 Mode-Based Adjustment
```
IF benchmark_mode == AGGRESSIVE
  → size_multiplier = 1.5
  → max_position_size = adjusted_size * 1.5

ELSE IF benchmark_mode == DEFENSIVE
  → size_multiplier = 0.5
  → max_position_size = adjusted_size * 0.5

ELSE (NEUTRAL)
  → size_multiplier = 1.0
  → max_position_size = adjusted_size
```

### 7.4 Drawdown-Based Scaling
```
current_drawdown_pct = (current_equity - peak_equity) / peak_equity

drawdown_scaling = (1 - abs(current_drawdown_pct)) ^ 2
# If -10% drawdown, scaling = 0.81
# If -20% drawdown, scaling = 0.64

scaled_size = max_position_size * drawdown_scaling
```

### 7.5 Conviction-Based Scaling
```
# Scale size based on signal confidence
conviction_scaling = entry_confidence  # 0.0 to 1.0

final_size = scaled_size * conviction_scaling
```

### 7.6 Apply Hard Caps
```
max_position_pct = 0.10  # max 10% of portfolio in one position
max_position_dollars = portfolio_equity * max_position_pct

IF final_size * entry_price > max_position_dollars
  → final_size = max_position_dollars / entry_price

IF final_size < min_tradeable_size (e.g., 1 share)
  → return INSUFFICIENT_SIZE
```

**Position Sizing Output:**
- `final_position_size` (shares or quantity)
- `position_value_dollars` = final_position_size * entry_price
- `position_pct_of_portfolio` = position_value_dollars / equity

---

## SECTION 8: RISK SETUP (Before Execution)

**Purpose:** Define exit conditions and risk parameters BEFORE placing order.

**Prerequisite:** Position sizing complete

### 8.1 Stop Loss Calculation (ATR-Based)
```
asset_ATR = ATR(asset_price, 14)
stop_loss_multiplier = 2.0  # 2x ATR

# For LONG entry:
stop_loss_price = entry_price - (asset_ATR * stop_loss_multiplier)

# For SHORT entry:
stop_loss_price = entry_price + (asset_ATR * stop_loss_multiplier)

# Minimum stop distance (% of price):
min_stop_distance_pct = 0.02  # at least 2%
min_stop_distance = entry_price * min_stop_distance_pct

IF abs(entry_price - stop_loss_price) < min_stop_distance
  → stop_loss_price = entry_price ± min_stop_distance
```

### 8.2 Profit Target Calculation
```
reward_multiple = 2.5  # aim for 2.5:1 reward/risk ratio
stop_distance = abs(entry_price - stop_loss_price)
profit_target_price = entry_price + (stop_distance * reward_multiple)

# For volatility-adjusted targets:
IF volatility_regime == HIGH
  → reward_multiple *= 0.8  # reduce target in high vol

IF volatility_regime == LOW
  → reward_multiple *= 1.2  # expand target in low vol
```

### 8.3 Trailing Stop Parameters
```
# Trailing stop activates after price moves favorably
trailing_stop_activation_pct = 0.03  # activate after +3% gain
trailing_stop_distance_multiple = 1.5  # trail by 1.5x ATR

trailing_activation_price = entry_price * (1 + trailing_stop_activation_pct)

# Once activated, trailing stop = max_price - (ATR * 1.5)
```

### 8.4 Partial Profit-Taking
```
# Take partial profits at intermediate levels
partial_take_profit_1 = entry_price + (stop_distance * 1.0)  # 1:1
partial_take_profit_2 = entry_price + (stop_distance * 1.5)  # 1.5:1

profit_target_fraction_1 = 0.5  # sell 50% at level 1
profit_target_fraction_2 = 0.25  # sell 25% at level 2
# Remaining 25% rides to full profit target or trailing stop
```

### 8.5 Overnight Risk Check
```
entry_time = current_time
holding_into_overnight = (entry_time > close_time_minus_1h)
  OR (expected_holding_period > 1 day)

IF holding_into_overnight AND overnight_risk_flag == TRUE
  → overnight_stop_price = entry_price - (larger_ATR_buffer)
  → OR cancel trade, retry at market open
```

### 8.6 Gap Risk Check
```
historical_gap_std = stdev(overnight_gaps[last_60_days])
gap_risk_multiplier = 1.5

IF historical_gap_std > typical_daily_ATR
  → gap_risk = HIGH
  → widen_stop_loss_price = entry_price - (gap_risk_multiplier * historical_gap_std)
  → OR reduce position size
  → OR skip trade
```

**Risk Setup Output:**
- `stop_loss_price`
- `profit_target_price`
- `trailing_stop_activation_price`
- `trailing_stop_distance`
- `partial_profit_levels` = [price_1, price_2, ...]
- `partial_profit_quantities` = [qty_1, qty_2, ...]
- `risk_reward_ratio` = (profit_target - entry) / (entry - stop_loss)

---

## SECTION 9: EXECUTION (Place Order)

**Purpose:** Safely place order on Alpaca with safeguards.

**Prerequisite:** Risk setup complete

### 9.1 Pre-Execution Validation
```
# Final safety checks before sending to broker
IF entry_price == NULL OR stop_loss_price == NULL
  → return ERROR_MISSING_PRICES

IF final_position_size <= 0
  → return ERROR_INVALID_SIZE

IF current_time NOT IN trading_session_hours
  → return ERROR_MARKET_CLOSED
```

### 9.2 Order Type Selection
```
# Default: limit order (better control, may not fill)
# Fallback: market order (fills immediately, slippage risk)

order_type = "LIMIT"
limit_price = entry_price * (1 - slippage_tolerance_pct)  # e.g., -0.1%

# If very liquid, can use market
IF liquidity_check == PASS AND spread_check == PASS
  → order_type = "MARKET"

# If signal is very high confidence, use market
IF entry_confidence > 0.85
  → order_type = "MARKET"
```

### 9.3 Smart Order Routing (if applicable)
```
# Check for better execution:
# - Dark pools (if latency < X ms)
# - Nearby exchanges
# - VWAP execution for larger orders

IF final_position_size > large_order_threshold (e.g., 5000 shares)
  → use VWAP execution over 5-10 min window
  → return after VWAP order submitted
```

### 9.4 Slippage Monitoring
```
expected_fill_price = entry_price
actual_fill_price = NULL

submit_order(
  symbol = asset,
  qty = final_position_size,
  side = "buy" or "sell",
  type = order_type,
  limit_price = limit_price if order_type == "LIMIT"
)

# Wait for order ack
order_status = wait_for_fill(timeout = 30 seconds)

IF order_status == FILLED
  → actual_fill_price = order.fill_price

  slippage_pct = abs(actual_fill_price - expected_fill_price) / expected_fill_price

  IF slippage_pct > slippage_tolerance (e.g., 0.5%)
    → LOG "Excessive slippage: X%"
    → consider canceling and retrying
```

### 9.5 Partial Fill Handling
```
IF filled_qty < order_qty
  → partial_fill = (filled_qty / order_qty)
  → LOG "Partial fill: X%"

  # Adjust position size and risk parameters
  actual_position_size = filled_qty
  actual_position_value = filled_qty * actual_fill_price

  # Scale stops proportionally
  # Retry remaining quantity
```

### 9.6 Order Retry Logic
```
retry_count = 0
max_retries = 3

WHILE order_status != FILLED AND retry_count < max_retries:
  IF order_status == REJECTED
    → update_limit_price()
    → resubmit_order()
    → retry_count += 1

  IF order_status == TIMEOUT
    → resubmit_order()
    → retry_count += 1

  wait(backoff_time = 2^retry_count seconds)

IF retry_count == max_retries AND NOT FILLED
  → LOG "Order failed after X retries"
  → return ORDER_FAILED
```

**Execution Output:**
- `order_id`
- `actual_fill_price`
- `actual_fill_qty`
- `slippage_pct`
- `execution_status` ∈ {FILLED, PARTIAL, FAILED}

---

## SECTION 10: ACTIVE TRADE MANAGEMENT (Continuous)

**Purpose:** Monitor open positions and execute exits in real-time.

**Prerequisite:** Position is open

**Schedule:** Every 5-15 minutes during market hours

### 10.1 Stop Loss Check
```
WHILE position.open == TRUE:
  current_price = get_current_price(asset)

  # For LONG position:
  IF current_price <= stop_loss_price
    → LOG "Stop loss hit at $X"
    → EXIT_POSITION(all_remaining_qty, market_order=True)
    → signal_exit_reason = "STOP_LOSS"
    → return
```

### 10.2 Profit Target Check
```
  # For LONG position:
  IF current_price >= profit_target_price
    → LOG "Profit target hit at $X"
    → EXIT_POSITION(all_remaining_qty)
    → signal_exit_reason = "PROFIT_TARGET"
    → return
```

### 10.3 Trailing Stop Check
```
  max_price_since_entry = max(all prices since entry)

  IF max_price_since_entry >= trailing_activation_price
    → trailing_stop_active = TRUE
    → trailing_stop_level = max_price_since_entry - (ATR * trailing_multiplier)

  IF trailing_stop_active AND current_price <= trailing_stop_level
    → LOG "Trailing stop hit at $X"
    → EXIT_POSITION(all_remaining_qty, market_order=True)
    → signal_exit_reason = "TRAILING_STOP"
    → return
```

### 10.4 Partial Profit-Taking
```
  # Check if at intermediate profit levels
  FOR each (level, qty) IN zip(partial_profit_levels, partial_profit_quantities):
    IF current_price >= level AND qty not yet closed:
      → EXIT_POSITION(qty)
      → signal_exit_reason = "PARTIAL_PROFIT_LEVEL_X"
```

### 10.5 Signal Reversal Exit
```
  # If original signal reverses (e.g., sentiment flips from BUY to SELL)
  current_signal = get_latest_signal(asset)

  IF signal.original_sentiment == "BUY"
    AND current_signal.sentiment == "SELL"
    AND current_signal.confidence > reversal_threshold (e.g., 0.7)
    → LOG "Signal reversed: SELL"
    → EXIT_POSITION(fraction_to_exit = 0.5)  # close half position
    → signal_exit_reason = "SIGNAL_REVERSAL"
```

### 10.6 Benchmark-Relative Underperformance
```
  # If position is underperforming S&P significantly
  position_return = (current_price - entry_price) / entry_price
  SPX_return_since_entry = (SPX_current - SPX_at_entry) / SPX_at_entry

  relative_underperformance = position_return - SPX_return_since_entry

  IF relative_underperformance < underperf_exit_threshold (e.g., -10%)
    AND holding_time > min_hold_duration (e.g., 5 days)
    → LOG "Underperforming benchmark by X%"
    → EXIT_POSITION(all_qty)
    → signal_exit_reason = "BENCHMARK_UNDERPERFORMANCE"
    → return
```

### 10.7 Max Holding Period
```
  holding_duration = now - entry_time
  max_hold = max_holding_period (e.g., 30 days)

  IF holding_duration > max_hold
    → LOG "Max holding period exceeded"
    → EXIT_POSITION(all_qty)
    → signal_exit_reason = "MAX_HOLD_TIME"
    → return
```

### 10.8 Volatility Spike Exit
```
  # If volatility spikes unexpectedly
  current_volatility = realized_vol(last_5_days)
  baseline_volatility = realized_vol(last_60_days)
  vol_ratio = current_volatility / baseline_volatility

  IF vol_ratio > vol_spike_threshold (e.g., 2.0x)
    AND position_return < 0  # position is losing
    → LOG "Volatility spike detected; exiting loser"
    → EXIT_POSITION(all_qty)
    → signal_exit_reason = "VOLATILITY_SPIKE"
    → return
```

**Active Management Output (on exit):**
- `exit_price`
- `exit_reason` ∈ {STOP_LOSS, PROFIT_TARGET, TRAILING_STOP, PARTIAL_PROFIT, SIGNAL_REVERSAL, BENCHMARK_UNDERPERF, MAX_HOLD, VOL_SPIKE}
- `holding_duration`
- `realized_pnl` = (exit_price - entry_price) * qty
- `realized_return_pct` = realized_pnl / (entry_price * qty)

---

## SECTION 11: PORTFOLIO CONTROLS (Scheduled + Real-Time)

**Purpose:** Monitor and enforce portfolio-level constraints.

**Schedule:** Every 15 minutes + after each new trade

### 11.1 Total Exposure Cap
```
current_gross_exposure = sum(abs(position_value)) / equity
gross_exposure_cap = 2.0  # max 200%

IF current_gross_exposure > gross_exposure_cap
  → LOG "Gross exposure exceeds cap: X%"
  → block_new_entries = TRUE
  → reduce_position_sizes_proportionally()
```

### 11.2 Sector Exposure Limits
```
FOR each sector:
  sector_weight = sum(position_value[ticker.sector == sector]) / total_portfolio
  sector_limit = 0.30  # max 30% per sector

  IF sector_weight > sector_limit
    → LOG "Sector X weight exceeds limit"
    → reduce_positions_in_sector()
    → block_new_trades_in_sector()
```

### 11.3 Asset Class Limits
```
# E.g., max 50% in equities, max 20% in bonds, max 10% in crypto
FOR each asset_class:
  class_weight = sum(position_value[class]) / total_portfolio
  class_limit = predetermined_limit

  IF class_weight > class_limit
    → block_new_trades_in_class()
```

### 11.4 Max Concurrent Positions
```
open_positions_count = count(position.open == TRUE)
max_concurrent = 15  # max 15 open trades

IF open_positions_count >= max_concurrent
  → block_new_entries = TRUE
  → LOG "Max concurrent positions reached"
```

### 11.5 Correlation Spike Response
```
# If portfolio correlation suddenly spikes (see Section 3.5)
IF correlation_regime == SPIKE
  → reduce_gross_exposure_by_X%()
  → close_least_conviction_positions()
  → send_alert_to_portal("High portfolio correlation")
```

### 11.6 Leverage Utilization Cap
```
# If using margin
current_margin_utilization = used_margin / available_margin
margin_utilization_cap = 0.8  # max use 80% of available

IF current_margin_utilization > margin_utilization_cap
  → block_new_entries = TRUE
  → reduce_position_sizes()
```

**Portfolio Controls Output:**
- `is_entry_allowed` ∈ {True, False}
- `blocked_reasons` = [list of active constraints]
- `current_exposure_pct`
- `sector_exposures` = {sector: weight, ...}

---

## SECTION 12: ADAPTIVE LAYER (Regime-Based Tuning)

**Purpose:** Adjust parameters based on regime changes.

**Schedule:** Daily + on regime change detection

### 12.1 Parameter Drift Detection
```
# Compare current optimal parameters to baseline
baseline_params = {
  momentum_threshold: 0.02,
  mean_reversion_entry: -1.5,
  stop_loss_multiplier: 2.0,
  profit_multiple: 2.5,
  ...
}

optimized_params = run_optimization(last_N_trades)

FOR each param:
  drift = abs(optimized_params[param] - baseline_params[param])
  drift_threshold = 0.20 * baseline  # 20% drift

  IF drift > drift_threshold
    → LOG "Parameter drift detected: X"
    → flag for review / parameter update
```

### 12.2 Regime-Based Parameter Switching
```
# Different parameter sets for different regimes
parameter_sets = {
  "MOMENTUM_DOMINANT": {stop_loss: 2.5x ATR, profit: 3.0x, enter_confidence: 0.6, ...},
  "MEAN_REVERSION_DOMINANT": {stop_loss: 1.5x ATR, profit: 2.0x, enter_confidence: 0.5, ...},
  "MIXED": {stop_loss: 2.0x ATR, profit: 2.5x, enter_confidence: 0.65, ...},
  "HIGH_VOL": {size_reduction: 0.6, stop_wider: True, ...},
  "RISK_OFF": {size_reduction: 0.4, hedge_increase: 0.3, ...}
}

IF regime_type changes
  → load_new_parameter_set(regime_type)
  → LOG "Switched parameters for regime: X"
```

### 12.3 Model Confidence Monitoring
```
# Track model prediction accuracy over time
model_accuracy = calculate_accuracy(predictions[last_100_trades])

IF model_accuracy < min_acceptable_accuracy (e.g., 45%)
  → reduce_trade_frequency_by_X%()
  → increase_confidence_threshold()
  → send_alert_to_portal("Model performance degraded")
  → flag for retraining
```

### 12.4 Performance-Based Risk Adjustment
```
# If performance drops, reduce risk
rolling_sharpe = calculate_sharpe(returns[last_30_days])

IF rolling_sharpe < min_sharpe_threshold (e.g., 0.5)
  → reduce_position_sizes_by_X%()
  → reduce_leverage()
  → increase_stop_loss_distance()

ELSE IF rolling_sharpe > optimal_sharpe
  → slightly_expand_position_sizes()
```

**Adaptive Output:**
- `active_parameter_set` = current regime's parameters
- `recommendations` = [list of suggested adjustments]

---

## SECTION 13: STRESS OVERRIDES (Black Swan Events)

**Purpose:** Immediate protective action on extreme market events.

**Schedule:** Continuous (event-driven)

### 13.1 Flash Crash Detection
```
# Detect >10% intraday drop in S&P 500
SPX_intraday_change = (SPX_current - SPX_open) / SPX_open

IF SPX_intraday_change < -0.10  (or other flash threshold)
  → LOG "FLASH CRASH DETECTED"
  → mode = EMERGENCY
  → force_exit_all_positions() or hedge_entire_portfolio()
  → halt_new_entries()
  → send_critical_alert_to_portal()
  → return FLASH_CRASH_MODE
```

### 13.2 Extreme Volatility Mode
```
# VIX spike or realized vol explosion
current_VIX = get_VIX()
normal_VIX_range = 15-20

IF current_VIX > 50  (or 3x normal)
  → mode = EXTREME_VOLATILITY
  → reduce_all_position_sizes_by_50%()
  → tighten_all_stops()
  → disable_momentum_entries()
```

### 13.3 Liquidity Crisis Handling
```
# Market-wide liquidity dries up
IF avg_volume_across_portfolio < (0.5 * normal_volume)
  AND bid_ask_spreads > 10x normal
  → mode = LIQUIDITY_CRISIS
  → halt_new_entries()
  → close_least_liquid_positions_first()
  → only allow market orders on most liquid assets
```

### 13.4 Benchmark Crash Override
```
# S&P 500 crashes (e.g., >15% in one day)
SPX_daily_change = (SPX_current - SPX_open) / SPX_open

IF SPX_daily_change < -0.15
  → mode = DEFENSIVE (override current mode)
  → force_close_all_positions()
  → move to 100% cash + bonds
  → do_not_re_enter_until_stabilization()
```

### 13.5 Correlation Breakdown
```
# Hedges stop working (correlation spikes to 1.0)
IF correlation_regime == SPIKE (maintained > 30 min)
  AND avg_portfolio_correlation > 0.95
  → assume hedges have broken
  → reduce_gross_exposure_by_50%()
  → close_all_hedges()
  → move_to_defensive_position()
```

### 13.6 Model Confidence Collapse
```
# Model gives conflicting/uncertain signals
IF avg_signal_confidence < 0.2  (very low)
  AND no clear trend in market
  → mode = IDLE
  → halt_all_trading()
  → wait_for_clarity()
  → send_alert("Model confidence collapsed")
```

### 13.7 Forced De-Risking Protocol
```
# Nuclear option: emergency capital preservation
de_risk_level = {
  "LEVEL_1": close_smallest_1_pct,
  "LEVEL_2": close_smallest_5_pct,
  "LEVEL_3": close_all_positions(),
  "LEVEL_4": liquidate_to_cash_equivalent()
}

IF system_status == CRITICAL
  OR portfolio_heat_unbearable
  → execute_de_risk(level = LEVEL_3)
```

**Stress Override Output:**
- `emergency_mode_active` ∈ {True, False}
- `override_reason` ∈ {FLASH_CRASH, EXTREME_VOL, LIQUIDITY, BENCHMARK_CRASH, CORRELATION_BREAKDOWN, CONFIDENCE_COLLAPSE, FORCED_DERISKING}
- `protective_action_taken`

---

## SECTION 14: EVALUATION & FEEDBACK LOOP

**Purpose:** Track performance and trigger adaptation.

**Schedule:** After each trade + daily + weekly

### 14.1 Trade-Level Metrics
```
FOR each closed trade:
  entry_price
  exit_price
  exit_reason
  holding_duration

  realized_pnl = (exit_price - entry_price) * qty
  realized_return_pct = realized_pnl / (entry_price * qty)

  win = (realized_pnl > 0)
  loss = (realized_pnl < 0)
```

### 14.2 Portfolio-Level Metrics
```
# Calculated daily
daily_return = (equity_today - equity_yesterday) / equity_yesterday
cumulative_return = (equity_current - equity_start) / equity_start

sharpe_ratio = (mean_daily_return - risk_free_rate) / stdev_daily_returns
sortino_ratio = (mean_daily_return - risk_free_rate) / stdev_negative_returns

max_drawdown = (peak_equity - trough_equity) / peak_equity
drawdown_duration = days_from_peak_to_trough

win_rate = (num_wins / total_trades)
avg_win = mean(profitable_trades)
avg_loss = mean(losing_trades)
profit_factor = (sum_wins / abs(sum_losses))

expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
```

### 14.3 Benchmark Comparison
```
# Daily vs S&P 500
strategy_return = daily_return
benchmark_return = SPX_daily_return

alpha = strategy_return - benchmark_return
beta = cov(strategy_return, benchmark_return) / var(benchmark_return)
information_ratio = alpha / tracking_error

# Over longer windows (monthly, quarterly, annual)
ytd_strategy_return
ytd_SPX_return
outperformance = ytd_strategy_return - ytd_SPX_return
```

### 14.4 Risk-Adjusted Returns
```
# Calmar ratio
calmar = cumulative_return / max_drawdown

# Recovery factor
recovery_factor = cumulative_return / max_drawdown_amount (in dollars)

# Omega ratio
omega = (returns above threshold) / (returns below threshold)
```

### 14.5 Kill Condition Check
```
# If performance is unacceptable, stop trading
IF sharpe_ratio < min_sharpe (e.g., 0.5)
  AND max_drawdown > max_acceptable_dd (e.g., -30%)
  AND cumulative_return < minimum_target_return (e.g., +5% YTD)
  AND time_elapsed > min_eval_period (e.g., 60 days)
  → strategy_status = KILL_CONDITION_MET
  → halt_all_trading()
  → send_critical_alert("Strategy kill condition met")
  → review required before restart
```

### 14.6 Parameter Optimization Signal
```
# If specific metrics lag, flag for optimization
IF win_rate < 0.45 over last 100 trades
  → flag: Entry logic needs tuning

IF average_holding_time > expected * 2
  → flag: Exit logic leaving gains on table

IF sharpe deteriorating
  → flag: Risk parameters need adjustment
```

### 14.7 Evaluation Loop Summary
```
AFTER each trading session:
  1. Calculate all metrics above
  2. Compare to baseline / target
  3. Store in performance_log
  4. Check kill conditions
  5. Identify optimization opportunities
  6. Update adaptive layer (Section 12)
  7. Generate report for portal
  8. Send alerts if thresholds breached
```

**Evaluation Output:**
- `daily_report` = {sharpe, return, drawdown, trades, ...}
- `weekly_report` = aggregated metrics
- `monthly_report` = benchmarked vs SPX
- `kill_conditions_met` ∈ {True, False}
- `optimization_flags` = [list of areas needing attention]

---

## DATA FLOW: LOCAL (Retail Pi) → COMPANY PI COMPARATOR → LOCAL

### Retail Pi Sends to Company Pi:
```json
{
  "signal": {
    "ticker": "AAPL",
    "sentiment": "BUY",
    "confidence": 0.78,
    "source": "DisclosureResearchAgent",
    "industry_impact": "positive",
    "wave_count": 4,
    "regions": ["global", "regional"]
  },
  "local_context": {
    "entry_price": 175.50,
    "1_year_return": 0.15,
    "5_year_return": 0.45,
    "current_positions": ["MSFT", "NVDA"],
    "portfolio_correlation": 0.72,
    "daily_pnl": 1250.00,
    "current_drawdown": -0.08
  },
  "market_context": {
    "SPX_trend": "UP",
    "volatility_regime": "NORMAL",
    "macro_regime": "RISK_ON",
    "benchmark_mode": "NEUTRAL",
    "regime_type": "MIXED"
  },
  "system_status": "OK"
}
```

### Company Pi Returns to Retail Pi:
```json
{
  "decision": {
    "action": "BUY",
    "confidence": 0.82,
    "reason": "Strong company, positive news, low correlation"
  },
  "risk_parameters": {
    "position_size": 500,
    "entry_price": 175.50,
    "stop_loss_price": 169.20,
    "profit_target_price": 189.75,
    "risk_reward_ratio": 2.5
  },
  "hold_conditions": {
    "max_holding_period": 30,
    "trailing_stop_activation": 179.10,
    "take_profit_levels": [178.50, 183.20],
    "exit_triggers": ["SIGNAL_REVERSAL", "MAX_HOLD", "VOL_SPIKE"]
  },
  "alerts": {
    "overvaluation_warning": False,
    "sector_concentration_warning": True,
    "macro_risk_warning": False
  }
}
```

---

## DATABASE SCHEMA ADDITIONS

### signals table (additions):
```sql
ALTER TABLE signals ADD COLUMN (
  duplicate_counter INTEGER DEFAULT 0,
  duplicate_regions TEXT,  -- JSON: ["local", "regional", "global"]
  sentiment_wave_flag BOOLEAN DEFAULT 0,
  industry_impact TEXT,  -- "positive", "negative", "neutral"
  staleness_value DECIMAL(3,2)  -- 1.0 to 0.0 decay
);
```

### positions table (additions):
```sql
ALTER TABLE positions ADD COLUMN (
  entry_reason TEXT,  -- "momentum", "mean_reversion", "breakout", etc.
  stop_loss_price DECIMAL(10,2),
  profit_target_price DECIMAL(10,2),
  trailing_stop_active BOOLEAN DEFAULT 0,
  trailing_stop_level DECIMAL(10,2),
  company_strength TEXT,  -- "strong", "moderate", "weak"
  overvaluation_risk BOOLEAN DEFAULT 0
);
```

### trades table (additions):
```sql
ALTER TABLE trades ADD COLUMN (
  exit_reason TEXT,  -- "STOP_LOSS", "PROFIT_TARGET", etc.
  holding_duration_seconds INTEGER,
  realized_pnl DECIMAL(12,2),
  realized_return_pct DECIMAL(5,3),
  entry_type TEXT,  -- "momentum", "mean_reversion", "breakout", "pullback"
  entry_confidence DECIMAL(3,2),
  benchmark_return_same_period DECIMAL(5,3)
);
```

### market_regime table (new):
```sql
CREATE TABLE market_regime (
  id INTEGER PRIMARY KEY,
  timestamp DATETIME,
  SPX_trend TEXT,  -- "UP", "DOWN", "SIDEWAYS"
  volatility_regime TEXT,  -- "LOW", "NORMAL", "HIGH"
  regime_type TEXT,  -- "MOMENTUM_DOMINANT", "MEAN_REVERSION_DOMINANT", "MIXED"
  macro_regime TEXT,  -- "RISK_ON", "RISK_OFF", "MIXED"
  correlation_regime TEXT,  -- "NORMAL", "SPIKE"
  VIX_value DECIMAL(5,2),
  SPX_value DECIMAL(8,2),
  SPX_daily_return DECIMAL(5,3)
);
```

### performance_metrics table (new):
```sql
CREATE TABLE performance_metrics (
  id INTEGER PRIMARY KEY,
  date DATE,
  daily_return DECIMAL(5,3),
  cumulative_return DECIMAL(6,3),
  sharpe_ratio DECIMAL(5,2),
  sortino_ratio DECIMAL(5,2),
  max_drawdown_pct DECIMAL(5,3),
  win_rate DECIMAL(3,2),
  profit_factor DECIMAL(5,2),
  trades_count INTEGER,
  SPX_return_same_day DECIMAL(5,3),
  alpha DECIMAL(5,3),
  information_ratio DECIMAL(5,2)
);
```

---

## ERROR HANDLING & EDGE CASES

### 1. Partial Fill
- Adjust position sizing and risks proportionally
- Log partial fill percentage
- Retry unfilled portion in next window

### 2. Slippage Exceeds Tolerance
- Cancel trade and retry
- Widen limit price slightly
- Log excessive slippage incidents

### 3. No Eligible Signals
- Remain in cash
- Continue monitoring
- Do not force entry

### 4. Multiple Simultaneous Signals
- Score and rank all candidates
- Execute highest-conviction trade
- Queue others for next window

### 5. Position Already Exists
- Do not double-enter
- Consider adding to existing position (if logic supports)
- Log duplicate signal

### 6. Market Holiday / Early Close
- Skip trading session
- Adjust stop/target levels for next day
- Be aware of gap risk at open

### 7. Bankruptcy / Corporate Action
- Close position immediately at market
- Log event
- Alert customer

### 8. API Timeout / Latency Spike
- Retry with exponential backoff
- Use cached data if available
- Fail safely (do not execute bad data)

---

## SUMMARY TABLE: 14 Sections → Responsibilities

| Section | Name | Function | Output | Company/Retail |
|---------|------|----------|--------|----------------|
| 1 | System Gate | Hard stops, permission layer | system_status | Retail (boot sequence) |
| 2 | Benchmark Gate | S&P 500 regime | benchmark_mode | Retail |
| 3 | Regime Detection | Market context | regime_type, vol_regime, corr_regime | Retail |
| 4 | Trade Eligibility | Pre-trade filters | eligibility, skip_reason | Retail |
| 5 | Signal Evaluation | Score + weight signals | final_signal_score | Retail + Company |
| 6 | Entry Decision | Select entry type | entry_type, entry_confidence | Company (Comparator) |
| 7 | Position Sizing | Calculate trade size | position_size | Company |
| 8 | Risk Setup | Define exit conditions | stop_price, target_price | Company |
| 9 | Execution | Place order | order_id, fill_price | Retail |
| 10 | Active Management | Monitor + exit open positions | exit_reason, pnl | Retail (TradeMonitor) |
| 11 | Portfolio Controls | Enforce exposure limits | exposure_limits, blocked_reasons | Retail |
| 12 | Adaptive Layer | Tune parameters by regime | active_parameter_set | Company (Blueprint/Patches) |
| 13 | Stress Overrides | Emergency protective actions | emergency_mode, override_reason | Retail (Watchdog) |
| 14 | Evaluation | Track performance + kill conditions | sharpe, return, kill_conditions | Company (PerformanceEvaluator) |

---

**Version:** 2.0 (Complete Specification, Pre-Optimization)
**Last Updated:** March 28, 2026
**Next Steps:** Map to specific agents, optimize for Pi 2W, assign responsibilities, implement
