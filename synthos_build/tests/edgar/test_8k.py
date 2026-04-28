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
    def __init__(self, hits, body_html=None):
        self._hits = hits
        self._body_html = body_html
        self.fetch_url_called_with = []

    def search_filings(self, form_type, since_days=2, max_results=200, ciks=None):
        # Confirm caller asked for 8-K specifically
        assert form_type == "8-K"
        return self._hits

    def fetch_url(self, url):
        self.fetch_url_called_with.append(url)
        return self._body_html


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


class TestStripHtml(unittest.TestCase):
    """Stage 4 C — HTML → plain text helper."""

    def test_strips_tags(self):
        html = "<p>Hello <b>world</b></p>"
        self.assertEqual(edgar_8k._strip_html(html), "Hello world")

    def test_strips_script_and_style(self):
        html = """<html><head><script>var x=1;</script>
                  <style>body{color:red}</style></head>
                  <body><p>Real text</p></body></html>"""
        out = edgar_8k._strip_html(html)
        # Script/style content should NOT leak into the output
        self.assertNotIn("var x", out)
        self.assertNotIn("color:red", out)
        self.assertIn("Real text", out)

    def test_decodes_entities(self):
        html = "<p>Q3 revenue grew &gt;8% YoY &amp; beat estimates</p>"
        out = edgar_8k._strip_html(html)
        self.assertIn(">8%", out)
        self.assertIn("&", out)

    def test_normalizes_whitespace(self):
        html = "<p>Multiple\n\n   \tspaces</p>"
        self.assertEqual(edgar_8k._strip_html(html), "Multiple spaces")

    def test_empty_returns_empty(self):
        self.assertEqual(edgar_8k._strip_html(""), "")
        self.assertEqual(edgar_8k._strip_html(None), "")


class TestExtractExcerpt(unittest.TestCase):

    def test_anchors_on_item_header(self):
        html = """<html><body>
        <p>Some boilerplate disclaimer text.</p>
        <h2>Item 2.02 — Results of Operations</h2>
        <p>Q3 revenue of $25.0 billion, up 8% year-over-year, exceeding
        analyst expectations of $24.2B. EPS came in at $2.18 vs $2.10
        consensus.</p>
        </body></html>"""
        excerpt = edgar_8k.extract_8k_excerpt(html, "2.02", max_chars=200)
        # Should include the actual disclosure, not the boilerplate
        self.assertIn("Q3 revenue", excerpt)
        self.assertNotIn("boilerplate", excerpt)

    def test_falls_back_to_lead_paragraph(self):
        """If we can't find the item header, return the first max_chars
        of body — better than nothing."""
        html = "<html><body><p>Filing text without item header here.</p></body></html>"
        excerpt = edgar_8k.extract_8k_excerpt(html, "9.99", max_chars=100)
        self.assertEqual(excerpt, "Filing text without item header here.")

    def test_empty_html_returns_empty(self):
        self.assertEqual(edgar_8k.extract_8k_excerpt("", "2.02"), "")

    def test_max_chars_limits_length(self):
        html = "<p>Item 2.02. " + ("X" * 1000) + "</p>"
        excerpt = edgar_8k.extract_8k_excerpt(html, "2.02", max_chars=100)
        self.assertLessEqual(len(excerpt), 100)


class TestBodyFetchPath(unittest.TestCase):
    """Stage 4 C — fetch_body=True wiring + fallback behavior."""

    def test_body_excerpt_replaces_synthetic_headline(self):
        body_html = """<html><body>
        <h2>Item 2.02</h2><p>Q3 revenue of $25.0 billion, up 8% YoY.</p>
        </body></html>"""
        client = FakeClient([_hit("TSLA", ["2.02"])], body_html=body_html)
        items = edgar_8k.fetch_8k_signals(client, fetch_body=True)
        self.assertEqual(len(items), 1)
        it = items[0]
        # Headline now contains the actual filing text
        self.assertIn("Q3 revenue", it["headline"])
        self.assertIn("TSLA 8-K Item 2.02", it["headline"])
        # Synthetic headline preserved in metadata for trace
        self.assertIn("Item 2.02 — Earnings Results",
                      it["metadata"]["synthetic_headline"])
        self.assertIn("Q3 revenue", it["metadata"]["body_excerpt"])

    def test_body_fetch_failure_falls_back(self):
        """When fetch_url returns None, headline stays synthetic."""
        client = FakeClient([_hit("TSLA", ["2.02"])], body_html=None)
        items = edgar_8k.fetch_8k_signals(client, fetch_body=True)
        self.assertEqual(len(items), 1)
        it = items[0]
        # Headline is the synthetic fallback
        self.assertEqual(it["headline"],
                         "TSLA 8-K: Item 2.02 — Earnings Results")
        # Metadata records empty body_excerpt
        self.assertEqual(it["metadata"]["body_excerpt"], "")

    def test_default_no_body_fetch(self):
        """Without fetch_body=True, no fetch_url calls happen."""
        client = FakeClient([_hit("TSLA", ["2.02"])],
                            body_html="<p>Should not be fetched</p>")
        items = edgar_8k.fetch_8k_signals(client)  # default fetch_body=False
        self.assertEqual(len(items), 1)
        self.assertEqual(client.fetch_url_called_with, [],
                         "fetch_url should not be called when fetch_body=False")
        # Synthetic headline
        self.assertEqual(items[0]["headline"],
                         "TSLA 8-K: Item 2.02 — Earnings Results")

    def test_body_fetch_tries_each_filing(self):
        """With multiple hits and fetch_body=True, fetch_url is called once
        per relevant filing."""
        body_html = """<h2>Item 2.02</h2><p>Earnings disclosure text.</p>"""
        client = FakeClient([
            _hit("TSLA", ["2.02"]),
            _hit("AAPL", ["5.02"]),  # different item
            _hit("MSFT", ["2.02"]),
        ], body_html=body_html)
        items = edgar_8k.fetch_8k_signals(client, fetch_body=True)
        self.assertEqual(len(items), 3)
        # All three hits had their primary_doc_url fetched
        self.assertEqual(len(client.fetch_url_called_with), 3)


if __name__ == "__main__":
    unittest.main()
