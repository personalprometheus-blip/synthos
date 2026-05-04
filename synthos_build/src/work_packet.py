"""
work_packet.py — Schema for distributed-trader work packets.

Created 2026-05-03 as part of the distributed-trader migration.

A WorkPacket is a self-contained job description sent over HTTP from the
dispatcher (process node) to the trader server (retail node). Everything
the trader needs to evaluate gates and place orders for one customer in
one cycle is bundled in here — no callbacks to the process DB during the
cycle.

Design rules:
- Pure dataclass + plain dict. No ORM, no Pydantic dependency added.
- Stable wire format: JSON-serializable. msgpack-compatible if we want
  binary later (all fields are primitive / list / dict).
- Versioned via `schema_version` so dispatcher and trader can negotiate.
- DISPATCH_MODE=daemon never produces or consumes these — the toggle on
  the trader entry point bypasses this whole path.

NOT in this module:
- Network transport (lives in synthos_dispatcher / synthos_trader_server)
- Result delta (see WorkResult below — outbound from trader)

Cross-references:
- memory/orchestration_master_plan.md — architectural context
- conflict audit findings B2, H1, H2, H4 — fields here directly address
  the I/O extraction mandated by the audit
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import json
import time
import uuid


WORK_PACKET_SCHEMA_VERSION = "1.0.0"


@dataclass
class AlpacaCreds:
    """Per-customer Alpaca API credentials. Required because each customer
    has their own key/rate-limit bucket. Was loaded from auth.db by the
    trader's CLI parser; in distributed mode the dispatcher loads it and
    ships it in the packet so the trader has no DB dependency."""
    key: str
    secret: str
    base_url: str  # paper-api.alpaca.markets or api.alpaca.markets
    data_url: str = "https://data.alpaca.markets"


@dataclass
class CustomerStateSnapshot:
    """Frozen view of the customer's account at packet-build time.
    Trader runs gates against this; mutations accumulate into the result
    delta (NOT applied to the snapshot in place — keeps reasoning easy)."""
    portfolio: dict[str, Any]            # cash, equity, realized_gains, ...
    positions: list[dict[str, Any]]      # all OPEN positions
    cooling_off: list[dict[str, Any]]    # per-ticker hold windows
    recent_bot_orders: list[dict[str, Any]]  # settlement-lag guard rows
    customer_settings: dict[str, Any]    # risk tier, trading params, ...
    operating_mode: str                  # MANAGED or AUTOMATIC
    trading_mode: str                    # PAPER or LIVE


@dataclass
class MarketContext:
    """Cycle-wide market data. Same for every customer in the same
    dispatch batch — dispatcher fetches once and broadcasts."""
    regime: str                          # BULL / BEAR / NORMAL ...
    vix: float | None
    market_state: str                    # OPEN / CLOSED / HALT
    market_state_score: float
    session: str                         # premarket / open / midday / close / after
    timestamp_utc: str                   # ISO 8601


@dataclass
class WorkPacket:
    """One trade-cycle job for one customer. Fully self-contained."""
    work_id: str
    schema_version: str
    cycle: str                           # 'open' / 'midday' / 'close' / 'overnight'
    customer_id: str
    alpaca_creds: AlpacaCreds
    state_snapshot: CustomerStateSnapshot
    signals: list[dict[str, Any]]        # validated signals to evaluate
    market_context: MarketContext
    quotes: dict[str, dict[str, float]]  # ticker -> {bid, ask, mid, last}
    validator_verdict: str               # GO / NO_GO / DEGRADED
    recent_outcomes: list[dict[str, Any]]  # for gate1's recent-loss check
    deadline_ts: str                     # ISO 8601 — trader bails if exceeded
    dispatched_at_ts: str                # for staleness detection by trader

    @classmethod
    def new(cls, *, customer_id: str, cycle: str, state_snapshot,
            alpaca_creds, signals, market_context, quotes,
            validator_verdict: str, recent_outcomes: list,
            deadline_seconds: int = 240) -> "WorkPacket":
        """Convenience constructor — fills work_id, timestamps, version."""
        now = time.time()
        return cls(
            work_id=f"wrk-{int(now)}-{uuid.uuid4().hex[:8]}",
            schema_version=WORK_PACKET_SCHEMA_VERSION,
            cycle=cycle,
            customer_id=customer_id,
            alpaca_creds=alpaca_creds,
            state_snapshot=state_snapshot,
            signals=signals,
            market_context=market_context,
            quotes=quotes,
            validator_verdict=validator_verdict,
            recent_outcomes=recent_outcomes,
            deadline_ts=_iso(now + deadline_seconds),
            dispatched_at_ts=_iso(now),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> "WorkPacket":
        d = json.loads(payload)
        d["alpaca_creds"]    = AlpacaCreds(**d["alpaca_creds"])
        d["state_snapshot"]  = CustomerStateSnapshot(**d["state_snapshot"])
        d["market_context"]  = MarketContext(**d["market_context"])
        return cls(**d)


# ─────────────────────────────────────────────────────────────────────────
# Outbound: result returned by trader to dispatcher
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class TradeAction:
    """One Alpaca order placed (or attempted) by the trader."""
    signal_id: str
    ticker: str
    side: str                            # buy / sell
    qty: float
    order_type: str                      # market / bracket / ...
    requested_at_ts: str
    alpaca_order_id: str | None
    fill_price: float | None
    status: str                          # filled / rejected / pending / error
    error_msg: str | None = None


@dataclass
class StateDelta:
    """All state changes the dispatcher must apply to the master DB.
    The trader returns this; it never writes to a DB itself in distributed
    mode (audit blocker B2, H1, H2 fix)."""
    positions_added: list[dict[str, Any]] = field(default_factory=list)
    positions_closed: list[dict[str, Any]] = field(default_factory=list)
    cash_delta: float = 0.0
    cooling_off_added: list[dict[str, Any]] = field(default_factory=list)
    recent_bot_orders_added: list[dict[str, Any]] = field(default_factory=list)
    acknowledged_signal_ids: list[str] = field(default_factory=list)
    log_events: list[dict[str, Any]] = field(default_factory=list)
    trade_outcomes_for_gate14: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkResult:
    """Trader's response to one WorkPacket. Dispatcher applies this."""
    work_id: str
    customer_id: str
    schema_version: str
    completed_at_ts: str
    executed_by: str                     # retail node identifier
    actions: list[TradeAction]
    delta: StateDelta
    alpaca_reconciliation: dict[str, Any] | None  # post-trade Alpaca snapshot
    gate_timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None             # set if cycle bailed for any reason

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> "WorkResult":
        d = json.loads(payload)
        d["actions"] = [TradeAction(**a) for a in d["actions"]]
        d["delta"]   = StateDelta(**d["delta"])
        return cls(**d)


def _iso(epoch_seconds: float) -> str:
    """UTC ISO8601, no microseconds, no timezone suffix beyond Z."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
