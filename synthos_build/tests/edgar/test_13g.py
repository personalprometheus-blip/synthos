"""Unit tests for synthos_build/agents/news/edgar_13g.py
Runs on Mac py3.9.  No DB, no network.  Reuses the activist registry."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

from news import edgar_13g  # noqa: E402
from news.activist_registry import ActivistRegistry  # noqa: E402


def _hit(form, filer_cik, filer_name, ticker, filed_date="2026-04-25"):
    return {
        "accession":      "0001234567-26-000001",
        "form":           form,
        "filer_cik":      filer_cik,
        "filer_name":     filer_name,
        "filed_date":     filed_date,
        "primary_doc":    "filing.htm",
        "primary_doc_url": "https://www.sec.gov/x.htm",
        "tickers":        [ticker] if ticker else [],
        "raw_hit":        {"_source": {"form": form}},
    }


class FakeClient:
    def __init__(self, hits):
        self._hits = hits
        self.last_form = None

    def search_filings(self, form_type, since_days=2, max_results=200, ciks=None):
        self.last_form = form_type
        return self._hits


def _build_registry(entries):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"activists": entries}, fh)
    reg = ActivistRegistry(config_path=path)
    reg.load()
    return reg, path


class TestEmptyRegistry(unittest.TestCase):

    def test_empty_registry_short_circuits(self):
        reg, path = _build_registry([])
        try:
            client = FakeClient([_hit("SC 13G", "1336528", "X", "AAPL")])
            items = edgar_13g.fetch_13g_signals(client, reg)
            self.assertEqual(items, [])
            self.assertIsNone(client.last_form)
        finally:
            os.unlink(path)


class TestFiltering(unittest.TestCase):

    def setUp(self):
        self.reg, self.path = _build_registry([
            {"cik": "1336528", "name": "Pershing Square",
             "principals": ["Bill Ackman"], "tier": 1, "notes": "verified"},
        ])

    def tearDown(self):
        os.unlink(self.path)

    def test_known_activist_filing_13g_emits_tier2(self):
        client = FakeClient([_hit("SC 13G", "1336528", "Pershing", "AAPL")])
        items = edgar_13g.fetch_13g_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "edgar_13g")
        # Critically: 13G is tier 2, not tier 1 like 13D
        self.assertEqual(it["source_tier"], 2)
        self.assertEqual(it["ticker"], "AAPL")
        self.assertEqual(it["amount_range"], "≥5%")
        self.assertIn("13G:", it["headline"])
        self.assertIn("Pershing Square", it["headline"])
        self.assertIn("passive", it["headline"])

    def test_unknown_filer_skipped(self):
        # Vanguard / BlackRock / etc. — bulk filers, not in registry → noise
        client = FakeClient([
            _hit("SC 13G", "9999999999", "Vanguard Group Inc",       "AAPL"),
            _hit("SC 13G", "8888888888", "BlackRock Inc",            "MSFT"),
            _hit("SC 13G", "1336528",   "Pershing", "TSLA"),
        ])
        items = edgar_13g.fetch_13g_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "TSLA")

    def test_amendment_tagged(self):
        client = FakeClient([_hit("SC 13G/A", "1336528", "Pershing", "MSFT")])
        items = edgar_13g.fetch_13g_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["is_amended"])
        self.assertTrue(it["headline"].startswith("13G/A:"))
        self.assertIn("amended", it["headline"])

    def test_no_ticker_skipped(self):
        client = FakeClient([
            {**_hit("SC 13G", "1336528", "Pershing", "AAPL"), "tickers": []},
            _hit("SC 13G", "1336528", "Pershing", "AAPL"),
        ])
        items = edgar_13g.fetch_13g_signals(client, self.reg)
        self.assertEqual(len(items), 1)

    def test_searches_for_sc_13g(self):
        client = FakeClient([])
        edgar_13g.fetch_13g_signals(client, self.reg)
        self.assertEqual(client.last_form, "SC 13G")

    def test_metadata_carries_activist_info(self):
        client = FakeClient([_hit("SC 13G", "1336528", "Pershing", "AAPL")])
        items = edgar_13g.fetch_13g_signals(client, self.reg)
        md = items[0]["metadata"]
        self.assertEqual(md["filer_cik"], "0001336528")
        self.assertEqual(md["activist_tier"], 1)
        self.assertEqual(md["form"], "13G")


class TestHeadline(unittest.TestCase):

    def test_with_principal(self):
        info = {"name": "Pershing Square", "principals": ["Bill Ackman"], "tier": 1}
        h = edgar_13g._build_headline(info, "AAPL", is_amended=False)
        self.assertIn("Pershing Square", h)
        self.assertIn("(Bill Ackman)", h)
        self.assertIn("filed passive ≥5% position", h)

    def test_amended(self):
        info = {"name": "X", "principals": [], "tier": 1}
        h = edgar_13g._build_headline(info, "Y", is_amended=True)
        self.assertTrue(h.startswith("13G/A:"))
        self.assertIn("amended passive", h)


if __name__ == "__main__":
    unittest.main()
