"""
work_packet_db.py — In-memory DB substitute that satisfies the trader's
read calls from work-packet data and accumulates writes into delta dicts.

⚠️  DEFERRED — NOT CURRENTLY ACTIVATED (audit 2026-05-04)
   Activated only when TRADER_DB_MODE=packet on trader_server. Production
   today runs TRADER_DB_MODE=local (loopback: trader_server on the same
   machine as the customer DBs). This mock is the seam that lets a future
   retail-1 node run the trader without local DBs — required infrastructure
   for cross-machine deployment, but no retail-N hardware exists today.
   See project_pi4b_cleanup_followups.md memory for the broader context.

Created 2026-05-04 as Phase A of the retail-1-readiness plan
(distributed-trader migration Tier 7+).

WHY THIS EXISTS:

The trader (`agents/retail_trade_logic_agent.py`) is built around two
DB handles: `_db()` returns the per-customer signals.db and
`_shared_db()` returns the shared market-intel signals.db. In daemon
mode AND in loopback distributed mode (retail-N == process node),
those handles point at real local SQLite files and everything works.

When trader_server runs on a different machine (retail-1 hardware,
post-cutover), there are no local DBs. The trader can't read its
customer's portfolio, can't write log events, can't ack signals via
the shared DB. To make trader code work unchanged on a remote node,
we substitute a `WorkPacketDB` instance for both handles. Reads pull
from the packet snapshot; writes accumulate into delta dicts that
trader_server returns to the dispatcher.

DESIGN PROPERTIES:

- **Same external surface as the real DB.** Every method the trader
  calls on `db.X(...)` or `_shared_db().X(...)` exists here, with the
  same signature. The trader never knows it's running against a mock.

- **Mutable working copies.** The trader frequently reads its own
  position list mid-cycle (e.g. after opening a position, gate11
  re-reads positions for the concentration check). The mock keeps
  internal state mutable so reads-after-writes return current values.

- **Best-effort safe defaults for tail methods.** Methods that exist
  on the real DB but are rarely called in the gate path
  (sweep_monthly_tax, update_profit_tier, etc.) return innocuous
  defaults rather than raising. Trader code degrades gracefully.

- **Deltas are explicit.** Every mutation goes through a public
  `delta_*` field on the instance so trader_server can return them
  in the WorkResult. Dispatcher applies them single-writer to the
  master DB.

REMOTE MODE TOGGLE:

Activated via `TRADER_DB_MODE=packet` env var on trader_server. The
default is `local` — trader_server uses real local DBs (loopback
mode, current production behavior). Set `packet` only when running
on a node without local customer DBs.

WHAT THIS MOCK DOES NOT DO:

- Persist anything. Setting writes accumulate but don't survive past
  the cycle's HTTP response.
- Support raw SQL via `db.conn()`. Any code path that bypasses the
  helper methods will raise `NotImplementedError`.
- Cross-customer queries. Each WorkPacketDB instance is one
  customer; access patterns that scan all customers (admin tooling)
  must run on the process node, not the retail node.
"""

from __future__ import annotations
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class WorkPacketDB:
    """Drop-in replacement for the trader's per-customer DB or shared DB
    handle. Constructed from a work packet; satisfies reads from packet
    data and accumulates writes into per-instance delta fields."""

    def __init__(self, packet: dict, is_shared: bool = False):
        """
        Args:
            packet: the work packet dict (the result of WorkPacket.to_json
                    + parse, or asdict(WorkPacket)).
            is_shared: True for the trader's `_shared_db()` handle (acks
                       go to a separate accumulator that dispatcher routes
                       to the shared DB); False for the per-customer
                       `_db()` handle.
        """
        self._packet = packet
        self._is_shared = is_shared
        state = packet.get("state_snapshot", {}) or {}

        # Mutable working state (initial values from packet, then mutated
        # by trader writes during the cycle so subsequent reads see them).
        self._portfolio = dict(state.get("portfolio") or {})
        self._positions = list(state.get("positions") or [])
        self._cooling_off = list(state.get("cooling_off") or [])
        self._recent_bot_orders = list(state.get("recent_bot_orders") or [])
        self._recent_outcomes = list(packet.get("recent_outcomes") or [])
        self._customer_settings = dict(state.get("customer_settings") or {})
        self._signals = list(packet.get("signals") or [])
        self._news_flags = dict(packet.get("news_flags") or {})
        self._validator_verdict = (packet.get("validator_verdict") or "GO").upper()
        self._validator_restrictions = list(packet.get("validator_restrictions") or [])
        self._market_context = packet.get("market_context", {}) or {}

        # ── DELTA accumulators (read by trader_server after t.run()) ──
        self.delta_positions_added: list[dict] = []
        self.delta_positions_closed: list[dict] = []
        self.delta_cash_delta: float = 0.0
        self.delta_cooling_off_added: list[dict] = []
        self.delta_recent_bot_orders_added: list[dict] = []
        self.delta_log_events: list[dict] = []
        self.delta_signal_decisions: list[dict] = []
        self.delta_acked_signal_ids: list[str] = []
        self.delta_setting_changes: dict[str, Any] = {}

    # ──────────────────────────────────────────────────────────────────
    # READ METHODS — satisfy from packet data
    # ──────────────────────────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        return dict(self._portfolio)

    def get_open_positions(self) -> list[dict]:
        return [p for p in self._positions if (p.get("status") or "OPEN") == "OPEN"]

    def get_recent_outcomes(self, limit: int = 100) -> list[dict]:
        return list(self._recent_outcomes[-limit:])

    def get_setting(self, key: str, default: Any = None) -> Any:
        # Synthetic settings derived from packet structure
        if key == "_VALIDATOR_VERDICT":
            return self._validator_verdict
        if key == "_VALIDATOR_RESTRICTIONS":
            return json.dumps(self._validator_restrictions)
        if key == "_MACRO_REGIME":
            return self._market_context.get("regime") or "NORMAL"
        if key == "_MACRO_REGIME_DETAIL":
            return json.dumps({
                "vix": self._market_context.get("vix"),
                "regime": self._market_context.get("regime") or "NORMAL",
            })
        if key == "_MARKET_STATE":
            return self._market_context.get("market_state") or "OPEN"
        if key == "_MARKET_STATE_SCORE":
            return str(self._market_context.get("market_state_score", 0.0))
        if key == "_MARKET_STATE_UPDATED":
            return self._market_context.get("timestamp_utc")
        if key == "_ALPACA_EQUITY":
            return self._portfolio.get("equity")
        if key == "_KEYS_INVALID_AT":
            return None  # Per-cycle, no persistence in packet
        # Apply mid-cycle setting changes first (read-your-writes)
        if key in self.delta_setting_changes:
            return self.delta_setting_changes[key]
        return self._customer_settings.get(key, default)

    def get_all_settings(self) -> dict:
        merged = dict(self._customer_settings)
        merged.update(self.delta_setting_changes)
        merged.update({
            "_VALIDATOR_VERDICT": self._validator_verdict,
            "_VALIDATOR_RESTRICTIONS": json.dumps(self._validator_restrictions),
            "_MACRO_REGIME": self._market_context.get("regime") or "NORMAL",
            "_MARKET_STATE": self._market_context.get("market_state") or "OPEN",
            "_MARKET_STATE_SCORE": str(self._market_context.get("market_state_score", 0.0)),
            "_ALPACA_EQUITY": self._portfolio.get("equity"),
        })
        return merged

    def is_cooling_off(self, ticker: str) -> bool:
        t = (ticker or "").upper()
        if not t:
            return False
        return any(c.get("ticker", "").upper() == t for c in self._cooling_off)

    def get_fresh_news_flags_for_ticker(self, ticker: str) -> list:
        return list(self._news_flags.get((ticker or "").upper(), []))

    def get_validated_signals(self) -> list[dict]:
        return [s for s in self._signals if (s.get("status") or "VALIDATED") == "VALIDATED"]

    def get_signal_by_id(self, signal_id: str) -> dict | None:
        for s in self._signals:
            if s.get("id") == signal_id:
                return s
        return None

    def get_recent_bot_order(self, ticker: str, hours: int = 24) -> dict | None:
        t = (ticker or "").upper()
        for o in self._recent_bot_orders:
            if o.get("ticker", "").upper() == t:
                return o
        # Also check newly-added in this cycle
        for o in self.delta_recent_bot_orders_added:
            if o.get("ticker", "").upper() == t:
                return o
        return None

    def get_screening_score(self, ticker: str) -> dict | None:
        # Sector screening scores aren't in the packet (would need extra
        # field). Return None so gate5_signal_score's screener boost
        # degrades gracefully (no boost added for distributed-mode
        # customers until we add screening_scores to packet).
        return None

    def get_ticker_sticky(self, ticker: str) -> str | None:
        # Sticky USER preferences not in packet. Return None = no
        # USER override; bot is free to manage all tickers.
        return None

    def get_urgent_flags(self, ticker: str | None = None) -> list:
        # Real DB supports both `get_urgent_flags()` (returns all) and
        # `get_urgent_flags(ticker)` (returns for one ticker). Both call
        # patterns appear in the trader (line 4255 calls without arg).
        # No urgent flags in packet currently; return empty.
        return []

    def get_halt(self) -> dict | None:
        # Per-customer halt state isn't in packet currently. Trader's
        # 4-layer halt check (admin halt → kill_switch → customer halt →
        # last_good) will fall through. Add to packet if needed.
        return None

    def has_event_today(self, event_type: str, today_str: str) -> bool:
        # Used by trader to dedup daily reports / actions. Conservative
        # default = False = "go ahead and emit" — duplicate emits across
        # cycles are caught by the daemon-mode dedup in shared DB.
        return False

    # ──────────────────────────────────────────────────────────────────
    # RAW SQL — refuse loudly so test failures point here
    # ──────────────────────────────────────────────────────────────────

    def conn(self):
        raise NotImplementedError(
            "WorkPacketDB.conn() — raw SQL not supported in packet mode. "
            "Caller must use one of the helper methods (get_setting, "
            "open_position, etc.)."
        )

    def db(self):
        return self  # legacy chained access pattern in some helpers

    # ──────────────────────────────────────────────────────────────────
    # WRITE METHODS — accumulate into delta_* fields
    # ──────────────────────────────────────────────────────────────────

    def acknowledge_signal(self, signal_id: str, *args, **kwargs) -> None:
        if signal_id and signal_id not in self.delta_acked_signal_ids:
            self.delta_acked_signal_ids.append(signal_id)

    def acknowledge_urgent_flag(self, *args, **kwargs) -> None:
        # Urgent flags not tracked per-cycle in packet. Silently accept.
        pass

    def add_notification(self, kind: str = "", title: str = "", body: str = "", *args, **kwargs) -> None:
        self.delta_log_events.append({
            "event": "NOTIFICATION_ADDED",
            "agent": "trade_logic_agent",
            "details": json.dumps({"kind": kind, "title": title, "body": body, "meta": kwargs.get("meta") or {}}, default=str)[:1000],
        })

    def check_bil_concentration(self, *args, **kwargs) -> bool:
        # Returns True if BIL concentration is at/over limit. Without
        # concentration tracking in packet, conservative answer = False
        # (not over limit) so trader doesn't auto-exit BIL on every
        # cycle.
        return False

    def close_position(self, ticker: str, **kwargs) -> bool:
        t = (ticker or "").upper()
        for p in self._positions:
            if p.get("ticker", "").upper() == t and (p.get("status") or "OPEN") == "OPEN":
                p_close = dict(p)
                p_close.update(kwargs)
                p_close["status"] = "CLOSED"
                self.delta_positions_closed.append(p_close)
                p["status"] = "CLOSED"
                # Cash impact
                qty = float(p.get("qty", 0) or 0)
                exit_price = float(
                    kwargs.get("exit_price")
                    or kwargs.get("close_price")
                    or kwargs.get("fill_price")
                    or 0
                )
                if qty and exit_price:
                    self._portfolio["cash"] = (self._portfolio.get("cash", 0) or 0) + (qty * exit_price)
                    self.delta_cash_delta += (qty * exit_price)
                return True
        return False

    def enqueue_scoop_email(self, *args, **kwargs) -> None:
        self.delta_log_events.append({
            "event": "SCOOP_EMAIL_QUEUED",
            "agent": "trade_logic_agent",
            "details": json.dumps({"args": list(args), **kwargs}, default=str)[:1000],
        })

    def expire_stale_approvals(self, *args, **kwargs) -> int:
        # Approval queue lives on process node; retail-side cycle has
        # nothing to expire. Daemon-mode trader handles this on Pi5.
        # Accept any args (real DB takes max_age_hours kwarg) so trader
        # can call without crashing.
        return 0

    def log_api_call(self, *args, **kwargs) -> None:
        # API call logging is high-volume; skip in packet mode to keep
        # delta payload small.
        pass

    def log_event(self, event: str = "", agent: str = "", details: str = "", *args, **kwargs) -> None:
        self.delta_log_events.append({
            "event": event,
            "agent": agent,
            "details": str(details),
        })

    def log_heartbeat(self, *args, **kwargs) -> None:
        # Heartbeats already covered by MQTT register_telemetry().
        # Trader passes various kwargs (status, portfolio_value, ...).
        pass

    def log_scan(self, *args, **kwargs) -> None:
        pass

    def log_signal_decision(self, *args, **kwargs) -> None:
        self.delta_signal_decisions.append({"args": list(args), **kwargs})

    def mark_order_failed(self, *args, **kwargs) -> None:
        self.delta_log_events.append({
            "event": "ORDER_FAILED",
            "agent": "trade_logic_agent",
            "details": json.dumps({"args": list(args), **kwargs}, default=str)[:1000],
        })

    def mark_order_recorded(self, *args, **kwargs) -> None:
        pass

    def open_position(self, *args, **kwargs) -> dict:
        new_pos = dict(kwargs)
        new_pos.setdefault("status", "OPEN")
        self._positions.append(new_pos)
        self.delta_positions_added.append(dict(new_pos))
        # Cash impact
        qty = float(kwargs.get("qty", 0) or 0)
        entry_price = float(
            kwargs.get("entry_price")
            or kwargs.get("avg_entry_price")
            or kwargs.get("fill_price")
            or 0
        )
        if qty and entry_price:
            cost = qty * entry_price
            self._portfolio["cash"] = (self._portfolio.get("cash", 0) or 0) - cost
            self.delta_cash_delta -= cost
        return new_pos

    def record_submitted_order(self, *args, **kwargs) -> None:
        entry = {**({f"_arg{i}": a for i, a in enumerate(args)}), **kwargs}
        self._recent_bot_orders.append(entry)
        self.delta_recent_bot_orders_added.append(entry)

    def reduce_position(self, ticker: str, qty_delta: float, *args, **kwargs) -> bool:
        t = (ticker or "").upper()
        for p in self._positions:
            if p.get("ticker", "").upper() == t and (p.get("status") or "OPEN") == "OPEN":
                old_qty = float(p.get("qty", 0) or 0)
                new_qty = max(0.0, old_qty - float(qty_delta or 0))
                p["qty"] = new_qty
                if new_qty == 0:
                    p["status"] = "CLOSED"
                    self.delta_positions_closed.append(dict(p))
                else:
                    # Partial close — record as a delta adjustment
                    self.delta_positions_added.append({
                        **dict(p),
                        "qty_delta": -float(qty_delta or 0),
                        "partial_close": True,
                    })
                return True
        return False

    def set_setting(self, key: str, value: Any) -> None:
        self.delta_setting_changes[key] = value
        self._customer_settings[key] = value

    def sweep_monthly_tax(self, *args, **kwargs) -> int:
        return 0

    def update_member_weight_after_trade(self, *args, **kwargs) -> None:
        pass

    def update_portfolio(self, *args, **kwargs) -> None:
        for k, v in kwargs.items():
            if k == "cash_delta":
                self._portfolio["cash"] = (self._portfolio.get("cash", 0) or 0) + float(v or 0)
                self.delta_cash_delta += float(v or 0)
            else:
                self._portfolio[k] = v

    def update_position_price(self, ticker: str, price: float = 0, *args, **kwargs) -> bool:
        t = (ticker or "").upper()
        for p in self._positions:
            if p.get("ticker", "").upper() == t:
                p["last_price"] = price
                return True
        return False

    def update_profit_tier(self, *args, **kwargs) -> None:
        pass

    def update_trail_stop(self, ticker: str, **kwargs) -> bool:
        t = (ticker or "").upper()
        for p in self._positions:
            if p.get("ticker", "").upper() == t and (p.get("status") or "OPEN") == "OPEN":
                for k, v in kwargs.items():
                    p[k] = v
                return True
        return False

    def queue_approval(self, **kwargs) -> None:
        """MANAGED-mode + overnight queue. Real DB writes to
        pending_approvals table. In packet mode we accumulate into
        delta_log_events so the dispatcher's apply_delta records that
        an approval was queued (the actual approval row would need to
        live somewhere — for now it lives in the customer's local DB
        when running loopback, and is ephemeral when running fully
        remote until queue_approvals storage is added to packet)."""
        self.delta_log_events.append({
            "event": "APPROVAL_QUEUED",
            "agent": "trade_logic_agent",
            "details": json.dumps(kwargs, default=str)[:1000],
        })

    def mark_approval_executed(self, signal_id: str, **kwargs) -> None:
        """Called after a queued approval is acted on. Real DB updates
        pending_approvals.status. Packet mode logs as event."""
        self.delta_log_events.append({
            "event": "APPROVAL_EXECUTED",
            "agent": "trade_logic_agent",
            "details": json.dumps({"signal_id": signal_id, **kwargs}, default=str)[:1000],
        })

    def add_cooling_off(self, ticker: str = None, hours: float = 4, **kwargs) -> None:
        """Add a cooling-off entry for ticker. Real DB inserts into
        cooling_off table. Mock accumulates into delta + working state."""
        from datetime import datetime, timedelta, timezone
        t = (ticker or kwargs.get("ticker") or "").upper()
        if not t:
            return
        expires = datetime.now(timezone.utc) + timedelta(hours=float(hours or 4))
        entry = {
            "ticker": t,
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **kwargs,
        }
        self._cooling_off.append(entry)
        self.delta_cooling_off_added.append(entry)

    # ──────────────────────────────────────────────────────────────────
    # DELTA EXTRACTION — called by trader_server after t.run()
    # ──────────────────────────────────────────────────────────────────

    def extract_delta(self) -> dict:
        """Return the StateDelta dict matching WorkResult.delta shape."""
        return {
            "positions_added":         list(self.delta_positions_added),
            "positions_closed":        list(self.delta_positions_closed),
            "cash_delta":              float(self.delta_cash_delta),
            "cooling_off_added":       list(self.delta_cooling_off_added),
            "recent_bot_orders_added": list(self.delta_recent_bot_orders_added),
            "acknowledged_signal_ids": list(self.delta_acked_signal_ids),
            "log_events":              list(self.delta_log_events),
            "trade_outcomes_for_gate14": [],   # outcomes are built post-trade by dispatcher
            "setting_changes":         dict(self.delta_setting_changes),
            "signal_decisions":        list(self.delta_signal_decisions),
        }
