"""Unit tests for synthos_build/agents/news/cross_source_dedup.py
Runs on Mac py3.9.  No DB, no network."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

# Drop env overrides so we test against module defaults
for k in ("CROSS_SOURCE_DEDUP_WINDOW_MIN", "CROSS_SOURCE_JACCARD"):
    os.environ.pop(k, None)

from news import cross_source_dedup as csd  # noqa: E402


def _item(headline, ticker, source, source_tier, disc_date="2026-04-25",
          tx_date="2026-04-25"):
    return {
        "headline":    headline,
        "ticker":      ticker,
        "source":      source,
        "source_tier": source_tier,
        "disc_date":   disc_date,
        "tx_date":     tx_date,
        "metadata":    {},
    }


class TestSingleSourcePassthrough(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(csd.cluster_and_pick_primary([]), [])

    def test_single_item_unchanged(self):
        items = [_item("Apple Q3 EPS Beats", "AAPL", "alpaca_news", 2)]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        # Singleton gets corroboration_count=1, empty source list
        md = out[0]["metadata"]
        self.assertEqual(md["corroboration_count"], 1)
        self.assertEqual(md["corroborating_sources"], [])

    def test_unrelated_items_no_merge(self):
        items = [
            _item("Apple Q3 EPS Beats", "AAPL", "edgar_8k", 2),
            _item("Microsoft Layoffs Announced", "MSFT", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 2)
        for it in out:
            self.assertEqual(it["metadata"]["corroboration_count"], 1)


class TestCrossSourceClustering(unittest.TestCase):

    def test_two_sources_same_event_merge(self):
        """8-K + Benzinga article on same earnings → merge."""
        items = [
            _item("AAPL 8-K: Item 2.02 — Earnings Results",
                  "AAPL", "edgar_8k", 2),
            _item("Apple Inc 8-K Item 2.02 Earnings Results",
                  "AAPL", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1, "Should collapse to one primary")
        primary = out[0]
        # SEC source wins via SOURCE_PREFERENCE
        self.assertEqual(primary["source"], "edgar_8k")
        md = primary["metadata"]
        self.assertEqual(md["corroboration_count"], 2)
        self.assertEqual(md["corroborating_sources"], ["alpaca_news"])
        self.assertEqual(len(md["corroborating_headlines"]), 1)

    def test_three_source_cluster(self):
        """SEC + 2 Benzinga articles all about Tim Cook stock sale.
        Realistic headlines share entity + amount + ticker tokens, so
        Jaccard clears the 0.40 default threshold pairwise. SEC is
        primary; both aggregator headlines noted as corroborators."""
        # All three contain: tim, cook, ceo, sold, $1.5m, aapl, form 4
        # → high pairwise Jaccard
        items = [
            _item("Form 4: Tim Cook CEO sold $1.5M AAPL stock",
                  "AAPL", "edgar_form4", 1),
            _item("Tim Cook CEO sold $1.5M AAPL stock Form 4 filing",
                  "AAPL", "alpaca_news", 2),
            _item("Tim Cook CEO sold $1.5M AAPL stock today disclosure",
                  "AAPL", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        primary = out[0]
        self.assertEqual(primary["source"], "edgar_form4")
        # Form 4 also wins on tier (1 < 2)
        self.assertEqual(primary["source_tier"], 1)
        md = primary["metadata"]
        self.assertEqual(md["corroboration_count"], 3)

    def test_tier_beats_source_preference(self):
        """tier 1 alpaca_news (hypothetical) should win over tier 2 SEC.
        (We never set alpaca_news to tier 1 today, but the logic should
        still pick lowest tier if it happens.)"""
        items = [
            _item("AAPL Form 4 sample alpaca", "AAPL", "alpaca_news", 1),
            _item("AAPL Form 4 sample alpaca more", "AAPL", "edgar_8k", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        # Tier 1 beats tier 2 even though alpaca isn't in SOURCE_PREFERENCE
        self.assertEqual(out[0]["source"], "alpaca_news")

    def test_same_source_items_dont_cluster(self):
        """Two Benzinga articles about the same event should NOT merge
        here — that's gate 8's job. Cross-source dedup only handles
        different-source clusters."""
        items = [
            _item("Apple Q3 EPS Beats Estimates", "AAPL", "alpaca_news", 2),
            _item("Apple Inc Q3 EPS Beats Estimates Massive", "AAPL", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 2,
            "Same-source duplicates should pass through (gate 8 handles them)")


class TestTickerBoundaries(unittest.TestCase):

    def test_different_tickers_dont_cluster(self):
        """Same headline on different tickers stays separate."""
        items = [
            _item("Earnings Beat Q3", "AAPL", "edgar_8k", 2),
            _item("Earnings Beat Q3", "MSFT", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 2)

    def test_tickerless_items_passthrough(self):
        """No-ticker items (rare — macro news) should pass through with
        corroboration_count=1."""
        items = [
            {"headline": "Fed Says Rate Cut Possible", "ticker": "",
             "source": "alpaca_news", "source_tier": 2, "metadata": {}},
            _item("AAPL earnings", "AAPL", "edgar_8k", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 2)
        # Tickerless item has corroboration_count = 1 (not zero)
        tickerless = [o for o in out if not o.get("ticker")][0]
        self.assertEqual(tickerless["metadata"]["corroboration_count"], 1)


class TestTimeWindow(unittest.TestCase):

    def test_within_window_clusters(self):
        items = [
            _item("AAPL Earnings Beat", "AAPL", "edgar_8k", 2,
                  disc_date="2026-04-25 09:00:00"),
            _item("AAPL Earnings Beat", "AAPL", "alpaca_news", 2,
                  disc_date="2026-04-25 09:30:00"),
        ]
        # 30 min apart, default 60-min window → cluster
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)

    def test_outside_window_separate(self):
        items = [
            _item("AAPL Earnings Beat", "AAPL", "edgar_8k", 2,
                  disc_date="2026-04-25 09:00:00"),
            _item("AAPL Earnings Beat", "AAPL", "alpaca_news", 2,
                  disc_date="2026-04-25 12:00:00"),
        ]
        # 3 hours apart, default 60-min window → don't cluster
        out = csd.cluster_and_pick_primary(items, time_window_minutes=60)
        self.assertEqual(len(out), 2)

    def test_missing_timestamp_falls_open(self):
        """If timestamp parsing fails for either item, treat as
        candidates (over-cluster bias is intentional — better to
        merge an actual dup than miss it for sparse-timestamp data)."""
        items = [
            _item("AAPL Earnings Beat", "AAPL", "edgar_8k", 2,
                  disc_date="", tx_date=""),
            _item("AAPL Earnings Beat", "AAPL", "alpaca_news", 2,
                  disc_date="", tx_date=""),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)


class TestJaccardThreshold(unittest.TestCase):

    def test_low_jaccard_no_cluster(self):
        """Headlines too dissimilar → don't cluster."""
        items = [
            _item("AAPL Q3 Earnings Beat", "AAPL", "edgar_8k", 2),
            _item("Apple Just Bought Some Random Company", "AAPL",
                  "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items, jaccard_threshold=0.40)
        self.assertEqual(len(out), 2)

    def test_threshold_override_loosens(self):
        """Lower threshold = more aggressive clustering."""
        items = [
            _item("AAPL Earnings Beat Today", "AAPL", "edgar_8k", 2),
            _item("Apple Inc Reports Today", "AAPL", "alpaca_news", 2),
        ]
        out_strict = csd.cluster_and_pick_primary(items, jaccard_threshold=0.50)
        out_loose  = csd.cluster_and_pick_primary(items, jaccard_threshold=0.10)
        self.assertEqual(len(out_strict), 2)  # too dissimilar at 0.50
        self.assertEqual(len(out_loose),  1)  # close enough at 0.10


class TestSourcePreferenceTieBreak(unittest.TestCase):

    def test_form4_beats_form144_same_tier_question(self):
        """At same tier, SOURCE_PREFERENCE breaks ties: edgar_form4 (idx 0)
        beats edgar_form144 (idx 2)."""
        # Both tier 2 (hypothetically — Form 4 is normally tier 1)
        items = [
            _item("AAPL insider sale", "AAPL", "edgar_form144", 2),
            _item("AAPL insider sale activity", "AAPL", "edgar_form4", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "edgar_form4")

    def test_unknown_source_lowest_priority(self):
        """A source not in SOURCE_PREFERENCE gets DEFAULT_PRIORITY."""
        items = [
            _item("AAPL news", "AAPL", "some_unknown_source", 2),
            _item("AAPL news similar", "AAPL", "edgar_8k", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "edgar_8k")


class TestMetadataPreservation(unittest.TestCase):

    def test_primary_keeps_original_fields(self):
        """The primary's headline, ticker, tx_date, etc. are not
        modified — we only enrich metadata."""
        items = [
            _item("AAPL Form 4 insider buy CEO disclosure",
                  "AAPL", "edgar_form4", 1,
                  disc_date="2026-04-25", tx_date="2026-04-24"),
            _item("AAPL Form 4 insider buy CEO Apple stock",
                  "AAPL", "alpaca_news", 2),
        ]
        out = csd.cluster_and_pick_primary(items)
        self.assertEqual(len(out), 1)
        primary = out[0]
        self.assertEqual(primary["headline"],
                         "AAPL Form 4 insider buy CEO disclosure")
        self.assertEqual(primary["ticker"], "AAPL")
        self.assertEqual(primary["tx_date"], "2026-04-24")
        self.assertEqual(primary["disc_date"], "2026-04-25")
        self.assertEqual(primary["source_tier"], 1)


class TestEnvOverrides(unittest.TestCase):

    def test_env_window_override(self):
        try:
            os.environ["CROSS_SOURCE_DEDUP_WINDOW_MIN"] = "5"
            os.environ["CROSS_SOURCE_JACCARD"]          = "0.70"
            import importlib
            importlib.reload(csd)
            # 30 minutes apart should now NOT cluster (window=5)
            items = [
                _item("AAPL Earnings Beat", "AAPL", "edgar_8k", 2,
                      disc_date="2026-04-25 09:00:00"),
                _item("AAPL Earnings Beat", "AAPL", "alpaca_news", 2,
                      disc_date="2026-04-25 09:30:00"),
            ]
            out = csd.cluster_and_pick_primary(items)
            self.assertEqual(len(out), 2)
        finally:
            os.environ.pop("CROSS_SOURCE_DEDUP_WINDOW_MIN", None)
            os.environ.pop("CROSS_SOURCE_JACCARD", None)
            import importlib
            importlib.reload(csd)


if __name__ == "__main__":
    unittest.main()
