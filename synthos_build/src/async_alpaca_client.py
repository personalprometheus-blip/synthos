"""
async_alpaca_client.py — Async-native Alpaca client built on httpx.

⚠️  DEFERRED — NOT CURRENTLY ACTIVATED (audit 2026-05-04)
   Defined in this file but not instantiated anywhere. The trader
   (synthos_trader_server.py and the daemon-mode trader) still uses the
   sync `AlpacaClient` in agents/retail_trade_logic_agent.py. This class
   is the planned async successor for when /work execution is rewritten
   to issue Alpaca calls concurrently within a single customer cycle
   (Tier 6 follow-up). Until then it's intentional WIP — do not delete,
   do not depend on it being maintained for current trading paths.
   See project_pi4b_cleanup_followups.md memory for the broader context.

Created 2026-05-04 as Tier 6 of the distributed-trader migration.

WHY THIS IS A SEPARATE CLASS (not a refactor of AlpacaClient):

The existing `AlpacaClient` in retail_trade_logic_agent.py:1183 has years
of carefully-tuned business logic — circuit breaker, bar caching,
DB-logged API audits, 401/422 fast-fail. Daemon-mode trades through it
every cycle and depends on its sync, blocking semantics. Touching it
risks breaking trading.

This module provides a SEPARATE async class with the same external
contract (method names, return shapes) but using httpx.AsyncClient
under the hood. The trader_server (and any future fully-async code path)
can use this class to issue concurrent Alpaca calls within a single
customer cycle — e.g. fetch latest quotes for 5 tickers in parallel,
or place 3 bracket orders concurrently.

In daemon mode, nothing changes. The original AlpacaClient is untouched.

When fully wired (post-Tier 7), the trader_server will:
  client = AsyncAlpacaClient(creds_from_packet)
  quotes = await asyncio.gather(*[client.get_latest_quote(t) for t in tickers])
  fills  = await asyncio.gather(*[client.submit_bracket(o) for o in orders])

Both `gather`s run concurrently — Alpaca calls overlap, total wall-clock
is bounded by the slowest single call, not the sum.

Design rules carried over from AlpacaClient:
  - Persistent HTTP connection (httpx.AsyncClient with pool)
  - Circuit breaker after N consecutive failures (open = silent skip)
  - Caller-provided per-customer credentials (no global env reads)
  - Best-effort failure semantics: return None instead of raising
"""

from __future__ import annotations
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CIRCUIT_BREAKER_N = int(os.environ.get("ALPACA_CIRCUIT_BREAKER_N", "5"))
DEFAULT_TIMEOUT_S = (5.0, 15.0)  # (connect, read)


class AsyncAlpacaClient:
    """Async-native counterpart to the trader's sync AlpacaClient.

    Lifecycle: instantiate, use as `async with`, or call connect()/aclose()
    explicitly. One client per customer cycle is the expected pattern —
    the underlying httpx.AsyncClient holds a connection pool, so multiple
    concurrent gathers share connections efficiently within one client.

    All methods are best-effort: return None on failure rather than raise,
    matching AlpacaClient's contract so callers can use identical
    "if result is None: skip" patterns.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = "https://paper-api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
        circuit_breaker_n: int = DEFAULT_CIRCUIT_BREAKER_N,
    ):
        self.api_key    = api_key
        self.secret_key = secret_key
        self.base_url   = base_url.rstrip("/")
        self.data_url   = data_url.rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type":        "application/json",
        }
        self._circuit_n      = circuit_breaker_n
        self._consec_fail    = 0
        self._circuit_open   = False
        self._client: Any | None = None  # httpx.AsyncClient — lazy-imported

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncAlpacaClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def connect(self) -> bool:
        """Lazy-create the httpx.AsyncClient. Returns True on success.
        Returns False if httpx is not installed (caller continues without
        async Alpaca capability — daemon path AlpacaClient still works)."""
        try:
            import httpx
        except ImportError:
            log.warning("[ASYNC ALPACA] httpx not installed — async Alpaca disabled")
            return False
        if self._client is None:
            timeout = httpx.Timeout(connect=DEFAULT_TIMEOUT_S[0], read=DEFAULT_TIMEOUT_S[1],
                                    write=DEFAULT_TIMEOUT_S[1], pool=DEFAULT_TIMEOUT_S[0])
            self._client = httpx.AsyncClient(
                headers=self.headers, timeout=timeout, http2=False,
            )
        return True

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                log.debug(f"[ASYNC ALPACA] aclose noise: {e}")
            self._client = None

    # ── Circuit breaker ───────────────────────────────────────────────

    def _record(self, success: bool) -> None:
        if success:
            self._consec_fail = 0
        else:
            self._consec_fail += 1
            if self._consec_fail >= self._circuit_n and not self._circuit_open:
                self._circuit_open = True
                log.warning(
                    f"[ASYNC ALPACA] circuit breaker opened after "
                    f"{self._consec_fail} consecutive failures"
                )

    def _circuit_ok(self) -> bool:
        return not self._circuit_open

    # ── Trading API ───────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any] | None:
        return await self._get(self.base_url, "/v2/account")

    async def get_positions(self) -> list[dict[str, Any]]:
        result = await self._get(self.base_url, "/v2/positions")
        return result if isinstance(result, list) else []

    async def get_position(self, ticker: str) -> dict[str, Any] | None:
        return await self._get(self.base_url, f"/v2/positions/{ticker}")

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return await self._get(self.base_url, f"/v2/orders/{order_id}")

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Submit a single order. Pass the full Alpaca order dict; the
        wrapper does no schema massaging so callers can use bracket /
        OCO / etc. just by setting the right keys (order_class, etc.).
        """
        return await self._post(self.base_url, "/v2/orders", json=payload)

    # ── Data API ──────────────────────────────────────────────────────

    async def get_latest_quote(self, ticker: str) -> tuple[float | None, float | None, float | None]:
        """Return (bid, ask, mid) or (None, None, None)."""
        data = await self._get(self.data_url, f"/v2/stocks/{ticker}/quotes/latest",
                                params={"feed": "iex"})
        if not data:
            return (None, None, None)
        q = data.get("quote") or {}
        bid = float(q.get("bp", 0) or 0)
        ask = float(q.get("ap", 0) or 0)
        mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        return (bid or None, ask or None, mid or None)

    async def get_latest_quotes_batch(
        self, tickers: list[str]
    ) -> dict[str, dict[str, float | None]]:
        """Multi-symbol batched fetch — one call returns up to ~100 quotes."""
        if not tickers:
            return {}
        data = await self._get(
            self.data_url, "/v2/stocks/quotes/latest",
            params={"symbols": ",".join(t.upper() for t in tickers), "feed": "iex"},
        )
        if not data:
            return {}
        out: dict[str, dict[str, float | None]] = {}
        for sym, q in (data.get("quotes") or {}).items():
            bid = float(q.get("bp", 0) or 0)
            ask = float(q.get("ap", 0) or 0)
            mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
            out[sym.upper()] = {
                "bid": bid or None, "ask": ask or None, "mid": mid or None,
            }
        return out

    async def get_bars(self, ticker: str, days: int = 60) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        end   = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (now_utc - timedelta(days=days + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._get(
            self.data_url, f"/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "start": start, "end": end,
                    "limit": days + 10, "feed": "iex"},
        )
        return (data or {}).get("bars", []) if data else []

    # ── Internal HTTP helpers ─────────────────────────────────────────

    async def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        if not self._circuit_ok():
            return None
        if self._client is None:
            if not await self.connect():
                return None
        url = f"{base}{path}"
        try:
            r = await self._client.get(url, params=params)
            if r.status_code in (404,):
                # Treat 404 as a "no such resource" success (matches
                # AlpacaClient.get_position_safe semantics).
                self._record(True)
                return None
            r.raise_for_status()
            self._record(True)
            return r.json() if r.text else {}
        except Exception as e:
            log.warning(f"[ASYNC ALPACA] GET {path} failed: {e}")
            self._record(False)
            return None

    async def _post(self, base: str, path: str, json: dict) -> Any:
        if not self._circuit_ok():
            return None
        if self._client is None:
            if not await self.connect():
                return None
        url = f"{base}{path}"
        try:
            r = await self._client.post(url, json=json)
            r.raise_for_status()
            self._record(True)
            return r.json() if r.text else {}
        except Exception as e:
            log.warning(f"[ASYNC ALPACA] POST {path} failed: {e}")
            self._record(False)
            return None
