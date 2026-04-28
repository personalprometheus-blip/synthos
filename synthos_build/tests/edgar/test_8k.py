"""Unit tests for synthos_build/agents/news/edgar_8k.py — runs on
Mac system Python 3.9. No DB, no network. Uses a fake EdgarClient that
returns a hand-crafted hit list."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

os.environ.pop("EDGAR_8K_ITEMS", None)

from news import edgar_8k  # noqa: E402


def _hit(ticker, items, accession="0001234567-26-000001", filed_date="2026-04-25"):
    return {
        "accession":      accession,
        "form":           "8-K",
        "filer_cik":      "0001234567",
        "filer_name":     f"{ticker} Inc",
        "filed_date":     filed_date,
        "primary_doc":    "form8k.htm",
        "primary_doc_url": f"https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/form8k.htm",
        "tickers":        [ticker],
        "raw_hit":        {"_source": {"items": items, "form": "8-K"}},
    }


class FakeClient:
    """Returns a fixed list of hits regardless of args.  No HTTP."""
    def __init__(self, hits):
        self._hits = hits

    def search_filings(self, form_type, since_days=2, max_results=200, ciks=None):
        # Confirm caller asked for 8-K specifically
        assert form_type == "8-K"
        return self._hits


class TestItemFilter(unittest.TestCase):

    def test_default_keeps_only_4_items(self):
        client = FakeClient([
            _hit("AAPL", ["2.02", "9.01"]),     # 2.02 ✓
            _hit("MSFT", ["1.01"]),             # not in default set
            _hit("TSLA", ["5.02"]),             # ✓
            _hit("META", ["8.01", "9.01"]),     # ✓
            _hit("GOOG", ["1.03"]),             # ✓
            _hit("AMZN", ["7.01"]),             # not in default set
        ])
        items = edgar_8k.fetch_8k_signals(client, since_days=2, max_filings=10)
        tickers = [i["ticker"] for i in items]
        self.assertEqual(sorted(tickers), ["AAPL", "GOOG", "META", "TSLA"])

    def test_skips_filings_with_no_ticker(self):
        client = FakeClient([
            {**_hit("AAPL", ["2.02"]), "tickers": []},
            _hit("TSLA", ["5.02"]),
        ])
        items = edgar_8k.fetch_8k_signals(client)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "TSLA")

    def test_env_override_widens_set(self):
        try:
            os.environ["EDGAR_8K_ITEMS"] = "1.01,2.02"
            client = FakeClient([
                _hit("AAPL", ["1.01"]),  # newly included
                _hit("MSFT", ["5.02"]),  # newly excluded
                _hit("TSLA", ["2.02"]),
            ])
            items = edgar_8k.fetch_8k_signals(client)
            tickers = sorted(i["ticker"] for i in items)
            self.assertEqual(tickers, ["AAPL", "TSLA"])
        finally:
            os.environ.pop("EDGAR_8K_ITEMS", None)

    def test_env_empty_disables_all(self):
        try:
            # An empty string should still pick the default set per the
            # current logic (treated as 'unset').  Confirm.
            os.environ["EDGAR_8K_ITEMS"] = ""
            client = FakeClient([_hit("AAPL", ["2.02"])])
            items = edgar_8k.fetch_8k_signals(client)
            self.assertEqual(len(items), 1)
        finally:
            os.environ.pop("EDGAR_8K_ITEMS", None)


class TestHeadlineShape(unittest.TestCase):

    def test_single_item_headline(self):
        h = edgar_8k._headline_for_8k("TSLA", ["2.02"])
        self.assertEqual(h, "TSLA 8-K: Item 2.02 — Earnings Results")

    def test_multi_item_headline(self):
        h = edgar_8k._headline_for_8k("AAPL", ["5.02", "8.01"])
        self.assertIn("Item 5.02 — Officer Change", h)
        self.assertIn("Item 8.01 — Other Events", h)
        self.assertTrue(h.startswith("AAPL 8-K:"))

    def test_unknown_item_falls_back_gracefully(self):
        h = edgar_8k._headline_for_8k("X", ["9.99"])
        self.assertIn("Item 9.99", h)


class TestSignalShape(unittest.TestCase):

    def test_emits_correct_pipeline_shape(self):
        client = FakeClient([_hit("AAPL", ["2.02"])])
        items  = edgar_8k.fetch_8k_signals(client)
        self.assertEqual(len(items), 1)
        it = items[0]
        # required gate-pipeline fields
        for key in ("headline", "ticker", "source", "source_tier",
                    "tx_date", "disc_date", "metadata"):
            self.assertIn(key, it, f"missing key: {key}")
        self.assertEqual(it["source"], "edgar_8k")
        self.assertEqual(it["source_tier"], 2)
        self.assertEqual(it["all_symbols"], ["AAPL"])
        self.assertEqual(it["metadata"]["items"], ["2.02"])


if __name__ == "__main__":
    unittest.main()
