"""Unit tests for synthos_build/agents/news/edgar_13d.py
Runs on Mac py3.9.  No DB, no network.  Uses fake EdgarClient + a
test-built ActivistRegistry."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

from news import edgar_13d  # noqa: E402
from news.activist_registry import ActivistRegistry  # noqa: E402


def _hit(form, filer_cik, filer_name, ticker,
         accession="0001234567-26-000001", filed_date="2026-04-25"):
    return {
        "accession":      accession,
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
        self.last_kwargs = None

    def search_filings(self, form_type, since_days=2, max_results=200, ciks=None):
        self.last_form = form_type
        self.last_kwargs = {"since_days": since_days, "max_results": max_results}
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
            client = FakeClient([_hit("SC 13D", "1336528", "X", "AAPL")])
            items = edgar_13d.fetch_13d_signals(client, reg)
            self.assertEqual(items, [])
            # Should NOT have hit EDGAR since registry was empty
            self.assertIsNone(client.last_form)
        finally:
            os.unlink(path)


class TestFilteringByActivist(unittest.TestCase):

    def setUp(self):
        self.reg, self.path = _build_registry([
            {"cik": "1336528", "name": "Pershing Square",
             "principals": ["Bill Ackman"], "tier": 1, "notes": "verified"},
            {"cik": "0000921669", "name": "Icahn Enterprises",
             "principals": ["Carl Icahn"], "tier": 1},
        ])

    def tearDown(self):
        os.unlink(self.path)

    def test_known_activist_emits(self):
        client = FakeClient([
            _hit("SC 13D", "1336528", "Pershing Square Capital Management LP",
                 "AAPL"),
        ])
        items = edgar_13d.fetch_13d_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["ticker"], "AAPL")
        self.assertEqual(it["source"], "edgar_13d")
        self.assertEqual(it["source_tier"], 1)
        self.assertEqual(it["politician"], "Pershing Square")
        self.assertFalse(it["is_amended"])
        self.assertEqual(it["amount_range"], "≥5%")
        # Headline includes principal in parens
        self.assertIn("Pershing Square", it["headline"])
        self.assertIn("Bill Ackman", it["headline"])
        self.assertIn("AAPL", it["headline"])
        self.assertTrue(it["headline"].startswith("13D:"))

    def test_unknown_filer_skipped(self):
        client = FakeClient([
            _hit("SC 13D", "9999999999", "Random Foundation Inc", "XYZ"),
            _hit("SC 13D", "1336528",   "Pershing", "AAPL"),
        ])
        items = edgar_13d.fetch_13d_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "AAPL")

    def test_amendment_tagged(self):
        client = FakeClient([
            _hit("SC 13D/A", "0000921669", "Icahn Enterprises", "MSFT"),
        ])
        items = edgar_13d.fetch_13d_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["is_amended"])
        self.assertTrue(it["headline"].startswith("13D/A:"))
        self.assertIn("amended activist position", it["headline"])

    def test_skips_no_ticker(self):
        client = FakeClient([
            {**_hit("SC 13D", "1336528", "Pershing", "AAPL"), "tickers": []},
            _hit("SC 13D", "1336528", "Pershing", "AAPL"),
        ])
        items = edgar_13d.fetch_13d_signals(client, self.reg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "AAPL")

    def test_metadata_carries_activist_info(self):
        client = FakeClient([
            _hit("SC 13D", "1336528", "Pershing Square", "AAPL"),
        ])
        items = edgar_13d.fetch_13d_signals(client, self.reg)
        md = items[0]["metadata"]
        self.assertEqual(md["filer_cik"], "0001336528")
        self.assertEqual(md["activist_tier"], 1)
        self.assertEqual(md["principals"], ["Bill Ackman"])
        self.assertIn("verified", md["activist_notes"])

    def test_searches_for_sc_13d_form(self):
        client = FakeClient([])
        edgar_13d.fetch_13d_signals(client, self.reg, since_days=10,
                                    max_filings=99)
        self.assertEqual(client.last_form, "SC 13D")
        self.assertEqual(client.last_kwargs["since_days"], 10)
        self.assertEqual(client.last_kwargs["max_results"], 99)


class TestHeadlineConstruction(unittest.TestCase):

    def test_headline_no_principal(self):
        info = {"name": "ValueAct Capital", "principals": [], "tier": 1}
        h = edgar_13d._build_headline(info, "MSFT", is_amended=False)
        self.assertEqual(h, "13D: ValueAct Capital filed activist position in MSFT")

    def test_headline_with_principal(self):
        info = {"name": "Third Point", "principals": ["Daniel Loeb"], "tier": 1}
        h = edgar_13d._build_headline(info, "GOOG", is_amended=False)
        self.assertIn("(Daniel Loeb)", h)

    def test_headline_amended(self):
        info = {"name": "Elliott", "principals": ["Paul Singer"], "tier": 1}
        h = edgar_13d._build_headline(info, "TWTR", is_amended=True)
        self.assertTrue(h.startswith("13D/A:"))


if __name__ == "__main__":
    unittest.main()
