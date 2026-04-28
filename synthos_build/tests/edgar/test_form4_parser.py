"""Unit tests for synthos_build/agents/news/edgar_form4.py — runs on
Mac system Python 3.9. Self-contained: no DB, no network."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Make agents/news/ importable as 'news'
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

# Reset relevant env so we don't pick up dev overrides between tests
os.environ.pop("EDGAR_FORM4_MIN_USD", None)

from news import edgar_form4  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TestForm4Parser(unittest.TestCase):

    def test_buy_above_threshold_emits_signal(self):
        items = edgar_form4.parse_form4(load("form4_buy_sample.xml"),
                                        filed_date="2026-04-26")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["ticker"], "AAPL")
        self.assertEqual(it["source"], "edgar_form4")
        self.assertEqual(it["source_tier"], 1)
        self.assertEqual(it["politician"], "COOK TIMOTHY D")
        self.assertEqual(it["disc_date"], "2026-04-26")
        self.assertEqual(it["tx_date"], "2026-04-25")
        # 10000 shares * $175.50 = $1.755M → "$1.8M"
        self.assertEqual(it["amount_range"], "$1.8M")
        self.assertEqual(it["all_symbols"], ["AAPL"])
        self.assertIn("CEO Tim", it["headline"]) if "Tim" in it["headline"] else None
        # Headline includes role + verb + amount + ticker
        self.assertIn("CEO", it["headline"])
        self.assertIn("bought", it["headline"])
        self.assertIn("AAPL", it["headline"])
        self.assertIn("$1.8M", it["headline"])
        # Metadata carries the granular fields
        md = it["metadata"]
        self.assertEqual(md["tx_code"], "P")
        self.assertEqual(md["tx_shares"], 10000.0)
        self.assertEqual(md["tx_price"], 175.5)
        self.assertEqual(md["tx_value_usd"], 1755000.0)
        self.assertTrue(md["is_officer"])
        self.assertFalse(md["is_director"])

    def test_below_threshold_skipped(self):
        items = edgar_form4.parse_form4(load("form4_below_threshold.xml"),
                                        filed_date="2026-04-25")
        # 100 shares * $10 = $1000 ≪ $50k threshold → no signal
        self.assertEqual(len(items), 0)

    def test_grant_code_skipped(self):
        items = edgar_form4.parse_form4(load("form4_grant_skipped.xml"),
                                        filed_date="2026-04-25")
        # Code A (grant) is not in INTERESTING_CODES → no signal
        self.assertEqual(len(items), 0)

    def test_threshold_env_override(self):
        try:
            os.environ["EDGAR_FORM4_MIN_USD"] = "10"  # $10 — way below sample
            # Force module-level constant re-read by re-loading module
            import importlib
            importlib.reload(edgar_form4)
            items = edgar_form4.parse_form4(load("form4_below_threshold.xml"),
                                            filed_date="2026-04-25")
            # Now the $1000 sale should pass the relaxed threshold
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["ticker"], "TINY")
        finally:
            os.environ.pop("EDGAR_FORM4_MIN_USD", None)
            import importlib
            importlib.reload(edgar_form4)

    def test_empty_xml_returns_empty(self):
        self.assertEqual(edgar_form4.parse_form4("", filed_date=""), [])
        self.assertEqual(edgar_form4.parse_form4("not xml", filed_date=""), [])

    def test_no_ticker_skipped(self):
        xml_no_ticker = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0001234</issuerCik>
    <issuerName>Private Co</issuerName>
    <issuerTradingSymbol></issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>X</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable></nonDerivativeTable>
</ownershipDocument>"""
        self.assertEqual(edgar_form4.parse_form4(xml_no_ticker), [])


class TestAmountFormatting(unittest.TestCase):

    def test_amount_buckets(self):
        self.assertEqual(edgar_form4._format_amount(500_000), "$500K")
        self.assertEqual(edgar_form4._format_amount(1_500_000), "$1.5M")
        self.assertEqual(edgar_form4._format_amount(3_200_000), "$3.2M")
        self.assertEqual(edgar_form4._format_amount(800), "$800")


if __name__ == "__main__":
    unittest.main()
