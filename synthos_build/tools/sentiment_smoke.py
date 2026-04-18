#!/usr/bin/env python3
"""Sentiment-agent volume-fetch smoke test.

Exercises the Alpaca-primary / Finviz-fallback volume fetch and the
ThreadPoolExecutor parallelism that the sentiment agent uses in the
real loop. Prints a single-ticker baseline and then an 8-ticker
parallel run so you can see the effective per-ticker cost with
SENTIMENT_FETCH_WORKERS workers.

Read-only. Makes live HTTP calls to Alpaca and (on fallback) Finviz,
so run it during business hours to get representative numbers.

Run:
    cd ~/synthos/synthos_build && python3 tools/sentiment_smoke.py
"""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / 'agents'))

from retail_market_sentiment_agent import (  # type: ignore
    _fetch_volume_from_alpaca,
    SENTIMENT_FETCH_WORKERS,
)


_TICKERS = ["AAPL", "MSFT", "NVDA", "AMD", "GOOG", "META", "TSLA", "AMZN"]


def main() -> int:
    print(f"SENTIMENT_FETCH_WORKERS = {SENTIMENT_FETCH_WORKERS}")

    # Single-ticker baseline
    t0 = time.monotonic()
    r = _fetch_volume_from_alpaca("AAPL")
    print(
        f"AAPL volume: rel_vol={r.get('today_vs_avg')} "
        f"sellers={r.get('seller_dominance')} "
        f"source={r.get('source')} "
        f"available={r.get('available')}  "
        f"({time.monotonic() - t0:.2f}s)"
    )

    # Parallel batch
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=SENTIMENT_FETCH_WORKERS) as pool:
        results = list(pool.map(_fetch_volume_from_alpaca, _TICKERS))
    elapsed = time.monotonic() - t0

    print(
        f"\n{len(_TICKERS)} tickers parallel "
        f"({SENTIMENT_FETCH_WORKERS} workers): {elapsed:.2f}s  "
        f"(avg {elapsed / len(_TICKERS):.2f}s/ticker)"
    )
    for ticker, rr in zip(_TICKERS, results):
        print(
            f"  {ticker}: rel_vol={rr.get('today_vs_avg')} "
            f"source={rr.get('source')} avail={rr.get('available')}"
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
