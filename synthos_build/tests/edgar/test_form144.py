"""Unit tests for synthos_build/agents/news/edgar_form144.py
Runs on Mac py3.9.  No DB, no network."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

os.environ.pop("EDGAR_FORM144_MIN_USD", None)

from news import edgar_form144  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TestForm144Parser(unittest.TestCase):

    def test_officer_above_threshold_emits(self):
        items = edgar_form144.parse_form144(load("form144_officer_sample.xml"),
                                            filed_date="2026-04-25")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["ticker"], "AAPL")
        self.assertEqual(it["source"], "edgar_form144")
        self.assertEqual(it["source_tier"], 2)
        self.assertEqual(it["politician"], "SMITH JANE")
        self.assertEqual(it["amount_range"], "$875K")
        self.assertIn("plans to sell", it["headline"])
        self.assertIn("AAPL", it["headline"])
        self.assertIn("$875K", it["headline"])
        # Metadata
        md = it["metadata"]
        self.assertEqual(md["aggregate_value_usd"], 875000.0)
        self.assertEqual(md["relationship"], "Officer")
        self.assertEqual(md["securities_class"], "Common Stock")
        self.assertEqual(md["approximate_sale_date"], "2026-04-30")

    def test_affiliate_relationship_skipped(self):
        items = edgar_form144.parse_form144(load("form144_affiliate_skipped.xml"),
                                            filed_date="2026-04-25")
        # "Other" relationship → not insider → skip
        self.assertEqual(len(items), 0)

    def test_below_threshold_skipped(self):
        items = edgar_form144.parse_form144(load("form144_below_threshold.xml"),
                                            filed_date="2026-04-25")
        # $5000 < $50k threshold
        self.assertEqual(len(items), 0)

    def test_threshold_env_override(self):
        try:
            os.environ["EDGAR_FORM144_MIN_USD"] = "1000"
            import importlib
            importlib.reload(edgar_form144)
            items = edgar_form144.parse_form144(load("form144_below_threshold.xml"),
                                                filed_date="2026-04-25")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["ticker"], "TINY")
        finally:
            os.environ.pop("EDGAR_FORM144_MIN_USD", None)
            import importlib
            importlib.reload(edgar_form144)

    def test_empty_xml_returns_empty(self):
        self.assertEqual(edgar_form144.parse_form144(""), [])
        self.assertEqual(edgar_form144.parse_form144("garbage"), [])

    def test_fallback_ticker_from_hit(self):
        # Hand-craft an XML with an empty issuerTradingSymbol — parser
        # should fall back to ticker_from_hit.
        xml = """<?xml version="1.0"?>
<edgarSubmission>
  <headerData><filerInfo><filerName>X Y</filerName></filerInfo></headerData>
  <formData>
    <issuerInfo>
      <issuerCik>0001234</issuerCik>
      <issuerName>Foo Inc</issuerName>
      <issuerTradingSymbol></issuerTradingSymbol>
    </issuerInfo>
    <securitiesInformation>
      <securitiesClassTitle>Common Stock</securitiesClassTitle>
      <aggregateMarketValue>200000</aggregateMarketValue>
      <approximateDateOfSale>2026-04-30</approximateDateOfSale>
      <relationshipToIssuer>Director</relationshipToIssuer>
    </securitiesInformation>
  </formData>
</edgarSubmission>"""
        items = edgar_form144.parse_form144(xml, ticker_from_hit="FOO",
                                            filed_date="2026-04-25")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "FOO")

    def test_combined_role_passes(self):
        # "Officer/Director" should match — split on '/' and check parts
        xml = """<?xml version="1.0"?>
<edgarSubmission>
  <headerData><filerInfo><filerName>X</filerName></filerInfo></headerData>
  <formData>
    <issuerInfo>
      <issuerName>Issuer</issuerName>
      <issuerTradingSymbol>ISSR</issuerTradingSymbol>
    </issuerInfo>
    <securitiesInformation>
      <aggregateMarketValue>250000</aggregateMarketValue>
      <relationshipToIssuer>Officer/Director</relationshipToIssuer>
    </securitiesInformation>
  </formData>
</edgarSubmission>"""
        items = edgar_form144.parse_form144(xml, filed_date="2026-04-25")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "ISSR")


class TestNormalizeRelationship(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(edgar_form144._normalize_relationship("  Officer  "),
                         "officer")
        self.assertEqual(edgar_form144._normalize_relationship("OFFICER"),
                         "officer")
        self.assertEqual(edgar_form144._normalize_relationship(""),
                         "")


if __name__ == "__main__":
    unittest.main()
