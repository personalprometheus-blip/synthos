#!/usr/bin/env python3
"""
capture_baseline.py — run a deterministic news-agent enrichment cycle,
dump the resulting signal_decisions / signals / system_log rows to JSON.

Used by the news-agent gate-pipeline refactor (backlog C8) to produce
byte-identical fixtures. The refactored agent must produce the exact
same JSON for the same input or the parity check fails.

Determinism strategy:
  - Temp sqlite DB, schema initialized from retail_database.DB(path=...)
  - All external network calls monkey-patched to return fixture data:
      retail_news_agent.fetch_alpaca_news_historical
      retail_news_agent.fetch_alpaca_news_for_ticker
      retail_news_agent._alpaca_bars
      retail_news_agent.fetch_price_history_1yr
      retail_news_agent.fetch_and_store_alpaca_display_news (no-op)
      retail_news_agent.fetch_with_retry (defensive no-op)
  - Announce / post-to-company network calls no-op'd:
      retail_news_agent.announce_for_interrogation
      retail_news_agent.post_to_company_pi
  - datetime.datetime patched in retail_news_agent AND retail_database
    modules so every now()/utcnow() call returns the fixture's frozen clock.
  - Output JSON has sort_keys=True at every level.

Usage:
  python3 tests/news_baseline/capture_baseline.py --cycle cycle_01
    # reads tests/news_baseline/fixtures/cycle_01_input.json
    # writes tests/news_baseline/cycle_01.json

  python3 tests/news_baseline/capture_baseline.py --cycle cycle_01 --session overnight

  python3 tests/news_baseline/capture_baseline.py --cycle cycle_01 --out /tmp/test.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO_ROOT        = Path(__file__).resolve().parent.parent.parent
SYNTHOS_BUILD    = REPO_ROOT / "synthos_build"
AGENTS_DIR       = SYNTHOS_BUILD / "agents"
CORE_DIR         = SYNTHOS_BUILD / "src"
FIXTURES_DIR     = Path(__file__).resolve().parent / "fixtures"
DEFAULT_OUT_DIR  = Path(__file__).resolve().parent

# Make the production modules importable
sys.path.insert(0, str(CORE_DIR))
sys.path.insert(0, str(AGENTS_DIR))

ET = ZoneInfo("America/New_York")


# ── FROZEN CLOCK ───────────────────────────────────────────────────────────

class FrozenDatetime(datetime):
    """A datetime subclass whose now()/utcnow() always return the fixture clock."""
    _frozen_utc: datetime = datetime(2026, 4, 15, 18, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._frozen_utc.replace(tzinfo=None)
        return cls._frozen_utc.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return cls._frozen_utc.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls._frozen_utc.replace(tzinfo=None)

    @classmethod
    def set_frozen(cls, iso: str):
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cls._frozen_utc = dt.astimezone(timezone.utc)


# ── FIXTURE LOADING ────────────────────────────────────────────────────────

def load_fixture(cycle_id: str) -> dict:
    path = FIXTURES_DIR / f"{cycle_id}_input.json"
    if not path.exists():
        raise SystemExit(f"fixture not found: {path}")
    return json.loads(path.read_text())


def fixture_bars_to_bar_list(summary: dict) -> list[dict]:
    """Expand a bars summary into a pseudo-bars list matching Alpaca's shape.
    The agent only cares about fields used in _summarise_bars; we echo them back."""
    last_close = summary.get("last_close", 100.0)
    high_52w   = summary.get("high_52w", last_close * 1.1)
    low_52w    = summary.get("low_52w",  last_close * 0.9)
    avg_vol    = summary.get("avg_volume", 1_000_000)
    bars_n     = summary.get("bars_available", 30)
    bars = []
    for i in range(bars_n):
        bars.append({
            "t": f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}T00:00:00Z",
            "o": last_close, "h": high_52w if i == 0 else last_close,
            "l": low_52w  if i == 1 else last_close,
            "c": last_close, "v": avg_vol,
        })
    return bars


# ── PATCH FACTORIES ────────────────────────────────────────────────────────

def build_fake_fetch_news(articles: list[dict]):
    """fetch_alpaca_news_historical replacement — returns fixture list regardless of args."""
    def _fake(symbols=None, start=None, end=None, limit=50, sort="desc"):
        return list(articles)
    return _fake


def build_fake_fetch_ticker_news(articles_by_ticker: dict[str, list[dict]]):
    def _fake(ticker: str, limit: int = 10):
        return list(articles_by_ticker.get((ticker or "").upper(), []))
    return _fake


def build_fake_alpaca_bars(bars_map: dict[str, list[dict]]):
    def _fake(ticker, days):
        return bars_map.get((ticker or "").upper(), [])
    return _fake


def build_fake_price_history(bars_summary: dict):
    def _fake(ticker, industry_etf, sector_etf):
        summaries = {}
        for sym in (ticker, industry_etf, sector_etf):
            if sym and sym in bars_summary:
                summaries[sym] = bars_summary[sym]
        return summaries, list(summaries.keys())
    return _fake


# ── HARNESS ────────────────────────────────────────────────────────────────

def _group_articles_by_ticker(articles: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in articles:
        t = (a.get("ticker") or "").upper()
        if not t:
            continue
        out.setdefault(t, []).append(a)
    return out


def _seed_members(db, members: list[dict]) -> None:
    if not members:
        return
    for m in members:
        try:
            db.upsert_member_weight(m["name"], float(m.get("weight", 1.0)))
        except AttributeError:
            # Fall back to raw SQL if helper doesn't exist
            with db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO member_weights (member_name, weight, "
                    "updated_at) VALUES (?, ?, ?)",
                    (m["name"], float(m.get("weight", 1.0)),
                     FrozenDatetime.utcnow().isoformat() + "Z"),
                )


def _dump_rows(conn, table: str, order_by: str = "id") -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def capture(cycle_id: str, session: str = "market", out_path: Path | None = None) -> Path:
    fixture = load_fixture(cycle_id)

    # Freeze the clock
    FrozenDatetime.set_frozen(fixture["frozen_clock_utc"])

    # Build bar-list map from summary fixture
    bars_summary = fixture.get("bars", {})
    bars_map = {t: fixture_bars_to_bar_list(s) for t, s in bars_summary.items()}

    articles = fixture.get("articles", [])
    articles_by_ticker = _group_articles_by_ticker(articles)

    with tempfile.TemporaryDirectory(prefix="baseline_") as tmpdir:
        tmp_db_path = Path(tmpdir) / "signals.db"

        # Import modules AFTER sys.path is set; keep them fresh by clearing any
        # previous module cache so each capture run is isolated.
        for mod in ("retail_database", "retail_news_agent"):
            sys.modules.pop(mod, None)

        import retail_database
        import retail_news_agent

        # Point the agent's _db() at our temp DB
        temp_db = retail_database.DB(path=str(tmp_db_path))
        _seed_members(temp_db, fixture.get("member_weights", []))

        patches = [
            mock.patch.object(retail_news_agent, "_db",
                              lambda: temp_db),
            mock.patch.object(retail_news_agent, "fetch_alpaca_news_historical",
                              build_fake_fetch_news(articles)),
            mock.patch.object(retail_news_agent, "fetch_alpaca_news_for_ticker",
                              build_fake_fetch_ticker_news(articles_by_ticker)),
            mock.patch.object(retail_news_agent, "_alpaca_bars",
                              build_fake_alpaca_bars(bars_map)),
            mock.patch.object(retail_news_agent, "fetch_price_history_1yr",
                              build_fake_price_history(bars_summary)),
            mock.patch.object(retail_news_agent, "fetch_and_store_alpaca_display_news",
                              lambda db: 0),
            mock.patch.object(retail_news_agent, "fetch_with_retry",
                              lambda *a, **kw: None),
            mock.patch.object(retail_news_agent, "announce_for_interrogation",
                              lambda *a, **kw: 0),
            mock.patch.object(retail_news_agent, "post_to_company_pi",
                              lambda *a, **kw: None),
            # Freeze time across both modules so every DB timestamp is stable
            mock.patch.object(retail_news_agent, "datetime", FrozenDatetime),
            mock.patch.object(retail_database,   "datetime", FrozenDatetime),
        ]
        for p in patches:
            p.start()

        try:
            retail_news_agent.run(session=session)
        finally:
            for p in patches:
                p.stop()

        # Dump the tables we care about
        import sqlite3
        conn = sqlite3.connect(str(tmp_db_path))
        try:
            rows_signals = _dump_rows(conn, "signals")
            rows_decisions = _dump_rows(conn, "signal_decisions")
            try:
                rows_log = _dump_rows(conn, "system_log", order_by="timestamp, id")
            except sqlite3.OperationalError:
                rows_log = []
        finally:
            conn.close()

    baseline = {
        "cycle_id":      cycle_id,
        "captured_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session":       session,
        "frozen_clock":  fixture["frozen_clock_utc"],
        "input_summary": {
            "article_count": len(articles),
            "tickers":       sorted({a.get("ticker", "") for a in articles if a.get("ticker")}),
        },
        "output": {
            "signals":          rows_signals,
            "signal_decisions": rows_decisions,
            "system_log":       rows_log,
        },
    }

    out = out_path or (DEFAULT_OUT_DIR / f"{cycle_id}.json")
    out.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    print(f"Captured {len(rows_decisions)} signal_decisions rows, "
          f"{len(rows_signals)} signals → {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--cycle", default="cycle_01",
                        help="fixture id (reads fixtures/<cycle>_input.json)")
    parser.add_argument("--session", default="market",
                        choices=("market", "overnight", "open", "midday", "close"),
                        help="agent session mode")
    parser.add_argument("--out", type=Path, default=None,
                        help="override output path (default: tests/news_baseline/<cycle>.json)")
    args = parser.parse_args()

    capture(args.cycle, args.session, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
