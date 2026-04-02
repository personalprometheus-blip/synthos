# Alpaca API Reference — Synthos Integration Notes

Filed: 2026-04-02
Purpose: Central reference for all Alpaca data feeds used by Synthos agents,
plus regulatory fee structure for cost calculation in trade logic.

---

## 1. Authentication

All Alpaca REST and WebSocket endpoints share the same credentials.

| Header                  | Value                         |
|-------------------------|-------------------------------|
| `APCA-API-KEY-ID`       | `ALPACA_API_KEY` (from .env)  |
| `APCA-API-SECRET-KEY`   | `ALPACA_SECRET_KEY` (from .env) |

For WebSocket connections, send an auth message immediately after connect:
```json
{"action": "auth", "key": "<ALPACA_API_KEY>", "secret": "<ALPACA_SECRET_KEY>"}
```
For OAuth tokens: `{"action": "auth", "key": "oauth", "secret": "<token>"}`

---

## 2. News API

### 2a. Historical News (REST)

- **Endpoint:** `GET https://data.alpaca.markets/v1beta1/news`
- **Coverage:** 2015–present via Benzinga content feed, ~130 articles/day
- **Usage in Synthos:** `retail_news_agent.py` — `fetch_alpaca_news_historical()`

**Query parameters:**

| Param                 | Type    | Default | Description                                      |
|-----------------------|---------|---------|--------------------------------------------------|
| `symbols`             | string  | –       | Comma-separated tickers, e.g. `AAPL,MSFT`        |
| `start`               | ISO-8601| –       | Range start (e.g. `2026-04-01T00:00:00Z`)        |
| `end`                 | ISO-8601| –       | Range end                                        |
| `limit`               | int     | 10      | Max articles per page (1–50)                     |
| `sort`                | string  | `desc`  | `asc` or `desc`                                  |
| `include_content`     | bool    | false   | Include full article body                        |
| `exclude_contentless` | bool    | false   | Skip articles with no body                       |
| `page_token`          | string  | –       | Cursor for next page (from `next_page_token`)    |

**Response:**
```json
{
  "news": [
    {
      "id": 12345,
      "headline": "Apple Reports Record Q2 Earnings",
      "summary": "Apple Inc. reported...",
      "author": "John Doe",
      "created_at": "2026-04-01T18:00:00Z",
      "updated_at": "2026-04-01T18:05:00Z",
      "url": "https://www.benzinga.com/...",
      "content": "...",
      "symbols": ["AAPL"],
      "source": "Benzinga"
    }
  ],
  "next_page_token": "abc123"
}
```

### 2b. Real-Time News Stream (WebSocket)

- **Endpoint:** `wss://stream.data.alpaca.markets/v1beta1/news`
- **Subscribe message:**
  ```json
  {"action": "subscribe", "news": ["*"]}
  ```
  Use `["*"]` for all news, or `["AAPL","TSLA"]` for specific symbols.

- **Message type `"n"` (news):**
  ```json
  {
    "T": "n",
    "id": 12345,
    "headline": "...",
    "summary": "...",
    "author": "...",
    "created_at": "2026-04-01T18:00:00Z",
    "updated_at": "2026-04-01T18:00:00Z",
    "url": "https://...",
    "content": "...",
    "symbols": ["AAPL"],
    "source": "Benzinga"
  }
  ```

**Future integration:** A real-time streaming process (`news_stream.py`) could
subscribe to `["*"]` and push articles directly into the signal pipeline for
near-zero-latency classification during market hours.

---

## 3. Real-Time Stock Data (WebSocket)

- **Endpoint:** `wss://stream.data.alpaca.markets/v2/{feed}`
- **Feeds:**
  - `sip` — Full SIP feed (requires paid subscription)
  - `iex` — IEX feed, ~2.5% of market volume (free tier / paper trading)
  - `delayed_sip` — 15-minute delayed SIP (free)

**Subscribe:**
```json
{"action": "subscribe", "trades": ["AAPL"], "quotes": ["AAPL"], "bars": ["AAPL"]}
```

**Message types:**

| Type | Event         | Key fields                                              |
|------|---------------|---------------------------------------------------------|
| `t`  | Trade         | S (symbol), p (price), s (size), t (timestamp), c (conditions) |
| `q`  | Quote         | S, ap (ask price), as (ask size), bp (bid price), bs (bid size) |
| `b`  | Minute bar    | S, o, h, l, c, v, t                                    |
| `d`  | Daily bar     | S, o, h, l, c, v, t                                    |
| `u`  | Updated bar   | S, o, h, l, c, v, t (bar update mid-period)            |
| `s`  | Subscription  | Confirmation of current subscriptions                  |

---

## 4. Real-Time Crypto Data (WebSocket)

- **Endpoint:** `wss://stream.data.alpaca.markets/v1beta3/crypto/{loc}`
- **Locations:** `us` (default), `us-1` (Kraken), `eu-1`

**Subscribe:**
```json
{"action": "subscribe", "trades": ["BTC/USD"], "quotes": ["BTC/USD"], "orderbooks": ["BTC/USD"]}
```

**Additional message type vs stocks:**

| Type | Event     | Key fields                                      |
|------|-----------|-------------------------------------------------|
| `o`  | Orderbook | S, b (bids [[price, size]]), a (asks), t        |

---

## 5. Real-Time Options Data (WebSocket)

- **Endpoints:**
  - `wss://stream.data.alpaca.markets/v1beta1/indicative` (indicative quotes, free)
  - `wss://stream.data.alpaca.markets/v1beta1/opra` (OPRA full feed, paid)

> **IMPORTANT:** Options WebSocket uses **binary MessagePack encoding only**.
> Connection must include `Content-Type: application/msgpack` header.
> Requires `msgpack` Python library to decode messages.

**Subscribe:**
```json
{"action": "subscribe", "trades": ["AAPL240119C00150000"], "quotes": ["AAPL*"]}
```
Wildcard `AAPL*` subscribes to all AAPL options.

---

## 6. Trade Updates Stream (WebSocket)

- **Endpoint:** `wss://paper-api.alpaca.markets/stream` (paper)
              `wss://api.alpaca.markets/stream` (live)
- **Auth:** Standard key/secret auth message
- **Subscribe:**
  ```json
  {"action": "listen", "data": {"streams": ["trade_updates"]}}
  ```

**Event types:** `new`, `fill`, `partial_fill`, `canceled`, `replaced`,
`pending_cancel`, `stopped`, `expired`, `pending_replace`, `calculated`,
`suspended`, `order_replace_rejected`, `order_cancel_rejected`

**Usage in Synthos:** Should be consumed by `retail_trade_logic_agent.py` for
real-time fill confirmation and position tracking, instead of polling the REST
orders endpoint.

---

## 7. Regulatory Fees (Cost Calculation)

These fees are **charged by Alpaca** on behalf of regulators.
They appear on end-of-day statements and reduce net P&L.

### 7a. TAF — Trading Activity Fee

- **Charged on:** Equity **sell** orders only
- **Rate:** $0.000166 per share (FINRA rate as of 2024)
- **Minimum:** $0.01 per trade
- **Maximum:** $8.30 per trade
- **Formula:** `min(max(shares * 0.000166, 0.01), 8.30)`
- **Rounding:** Up to nearest cent

```python
def calc_taf(shares: int, side: str) -> float:
    if side.upper() != 'sell':
        return 0.0
    fee = shares * 0.000166
    fee = max(fee, 0.01)
    fee = min(fee, 8.30)
    return round(math.ceil(fee * 100) / 100, 2)
```

### 7b. ORF — Options Regulatory Fee

- **Charged on:** Options **buys AND sells**
- **Rate:** $0.02905 per contract (OCC rate, adjusts annually)
- **Formula:** `contracts * 0.02905`
- **Rounding:** Up to nearest cent

```python
def calc_orf(contracts: int) -> float:
    fee = contracts * 0.02905
    return round(math.ceil(fee * 100) / 100, 2)
```

### 7c. OCC Fee — Options Clearing Corporation

- **Charged on:** Options **buys AND sells**
- **Rate:** $0.02 per contract
- **Cap:** $55.00 per trade (2,750 contracts × $0.02)
- **Formula:** `min(contracts * 0.02, 55.00)`
- **Rounding:** Up to nearest cent

```python
def calc_occ_fee(contracts: int) -> float:
    fee = min(contracts * 0.02, 55.00)
    return round(math.ceil(fee * 100) / 100, 2)
```

### 7d. CAT — Consolidated Audit Trail Fee

- **Charged on:** ALL transactions (equities, options)
- **Rate:** $0.000048 per share / per contract side
- **Formula:** `quantity * 0.000048`
- **Rounding:** Up to nearest cent

```python
def calc_cat(quantity: int) -> float:
    fee = quantity * 0.000048
    return round(math.ceil(fee * 100) / 100, 2)
```

### 7e. Total Fee Calculator (Utility)

```python
import math

def calc_total_fees(
    side: str,         # 'buy' or 'sell'
    asset_class: str,  # 'equity' or 'option'
    quantity: int,     # shares or contracts
) -> dict:
    """
    Calculate all applicable regulatory fees for a trade.
    Returns dict with individual fees and total.

    Fees are added to cost for buys, subtracted from proceeds for sells.
    All values in USD.
    """
    fees = {}

    if asset_class == 'equity':
        fees['taf'] = calc_taf(quantity, side)          # sell only
        fees['cat'] = calc_cat(quantity)                # all sides
        fees['orf'] = 0.0
        fees['occ'] = 0.0

    elif asset_class == 'option':
        fees['orf'] = calc_orf(quantity)                # all sides
        fees['occ'] = calc_occ_fee(quantity)            # all sides
        fees['cat'] = calc_cat(quantity)                # all sides
        fees['taf'] = 0.0                               # options exempt

    fees['total'] = round(sum(fees.values()), 2)
    return fees
```

### 7f. Fee Impact on Position Sizing

When calculating expected net P&L, always include round-trip fee costs:

```python
def net_pnl_after_fees(
    buy_price: float, sell_price: float,
    quantity: int, asset_class: str = 'equity'
) -> dict:
    gross_pnl = (sell_price - buy_price) * quantity
    buy_fees  = calc_total_fees('buy',  asset_class, quantity)['total']
    sell_fees = calc_total_fees('sell', asset_class, quantity)['total']
    net_pnl   = gross_pnl - buy_fees - sell_fees
    return {
        'gross_pnl':  round(gross_pnl, 2),
        'buy_fees':   buy_fees,
        'sell_fees':  sell_fees,
        'total_fees': round(buy_fees + sell_fees, 2),
        'net_pnl':    round(net_pnl, 2),
    }
```

**Practical note:** For small positions (< 100 shares), TAF and CAT fees are
negligible (< $0.02 total). ORF + OCC on options are more significant —
e.g. 10 contracts = $0.49 ORF + $0.20 OCC = $0.69 per side, $1.38 round-trip.

---

## 8. Advanced Order Routing (Elite Smart Router)

> Requires Elite Program membership. Not active in base Synthos.

- **DMA:** Direct Market Access — route directly to exchange
- **VWAP:** Volume Weighted Average Price algorithm
- **TWAP:** Time Weighted Average Price algorithm

Specified via `advanced_instructions` in the order payload:
```json
{
  "symbol": "AAPL",
  "qty": "100",
  "side": "buy",
  "type": "market",
  "time_in_force": "day",
  "advanced_instructions": {
    "order_type": "VWAP",
    "start_time": "09:30",
    "end_time": "15:30"
  }
}
```

---

## 9. Paper Trading Notes

- Initial balance: $100,000
- PDT (Pattern Day Trader) rules enforced — 3 day trades per rolling 5 days
  unless account equity ≥ $25,000
- Orders fill only when marketable (limit orders must cross the spread)
- Only IEX data feed available for paper accounts — no SIP
- Paper endpoint: `https://paper-api.alpaca.markets`
- Live endpoint:  `https://api.alpaca.markets`

The `TRADING_MODE` env var controls which endpoint is used:
- `PAPER` → paper-api.alpaca.markets
- `LIVE`  → api.alpaca.markets

---

## 10. OAuth2 (Future / Third-Party Accounts)

Authorization code flow for connecting third-party Alpaca accounts:

1. Redirect user to:
   `https://app.alpaca.markets/oauth/authorize?response_type=code&client_id=<id>&redirect_uri=<uri>&scope=account:write trading data`

2. Exchange code for token:
   ```
   POST https://api.alpaca.markets/oauth/token
   grant_type=authorization_code&code=<code>&client_id=<id>&client_secret=<secret>&redirect_uri=<uri>
   ```

3. Use token in WebSocket auth:
   ```json
   {"action": "auth", "key": "oauth", "secret": "<access_token>"}
   ```

Scopes: `account:write`, `trading`, `data`
Token expiry: varies — check `expires_in` in token response.
