"""Unit tests for synthos_build/agents/news/edgar_client.py — runs on
Mac system Python 3.9. Mocks requests with unittest.mock.patch — no
real HTTP."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

from news import edgar_client  # noqa: E402


def _mock_response(status=200, text="", json_data=None):
    r = MagicMock()
    r.status_code = status
    r.text        = text
    if json_data is not None:
        r.json = lambda: json_data
    if status >= 400:
        from requests import HTTPError
        r.raise_for_status = MagicMock(side_effect=HTTPError(f"HTTP {status}"))
    else:
        r.raise_for_status = MagicMock()
    return r


class TestUserAgentEnforcement(unittest.TestCase):

    def test_missing_ua_raises(self):
        os.environ.pop("SEC_EDGAR_UA_NAME", None)
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)
        with self.assertRaises(edgar_client.EdgarUserAgentMissing):
            edgar_client.EdgarClient()

    def test_partial_ua_raises(self):
        os.environ["SEC_EDGAR_UA_NAME"] = "Bot"
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)
        try:
            with self.assertRaises(edgar_client.EdgarUserAgentMissing):
                edgar_client.EdgarClient()
        finally:
            os.environ.pop("SEC_EDGAR_UA_NAME", None)

    def test_explicit_ua_overrides_env(self):
        os.environ.pop("SEC_EDGAR_UA_NAME", None)
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)
        c = edgar_client.EdgarClient(user_agent="Manual/1.0 a@b.com")
        self.assertEqual(c.user_agent, "Manual/1.0 a@b.com")


class TestRateLimiter(unittest.TestCase):

    def test_enforces_min_gap(self):
        rl = edgar_client._RateLimiter(rps=10)  # 100ms gap
        t0 = time.monotonic()
        for _ in range(3):
            rl.wait()
        elapsed = time.monotonic() - t0
        # 3 ticks at 10rps should take ~200ms (first is free, then 100ms × 2).
        # Allow generous slack for CI variance: 150-400ms.
        self.assertGreater(elapsed, 0.15)
        self.assertLess(elapsed, 0.4)

    def test_zero_rps_no_wait(self):
        rl = edgar_client._RateLimiter(rps=0)
        t0 = time.monotonic()
        for _ in range(5):
            rl.wait()
        self.assertLess(time.monotonic() - t0, 0.05)


class TestSearchFilings(unittest.TestCase):

    def setUp(self):
        os.environ["SEC_EDGAR_UA_NAME"]  = "TestBot"
        os.environ["SEC_EDGAR_UA_EMAIL"] = "test@example.com"

    def tearDown(self):
        os.environ.pop("SEC_EDGAR_UA_NAME", None)
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)

    def test_search_parses_hits(self):
        # EDGAR full-text-search response shape (real-API-shaped fixture)
        fake_payload = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001234567-26-000001:form4.xml",
                        "_source": {
                            "form":     "4",
                            "forms":    ["4"],
                            "ciks":     ["0001234567"],
                            "display_names": ["Acme Insider, Director"],
                            "file_date": "2026-04-25",
                            "tickers":  ["ACME"],
                        },
                    },
                    {
                        "_id": "0009876543-26-000002:form4.xml",
                        "_source": {
                            "form":     "4",
                            "forms":    ["4"],
                            "ciks":     ["0009876543"],
                            "display_names": ["Other Corp Officer"],
                            "file_date": "2026-04-24",
                            "tickers":  ["OTHR"],
                        },
                    },
                ]
            }
        }

        c = edgar_client.EdgarClient(rps=0)  # no rate limit in test
        with patch.object(c._session, "get",
                          return_value=_mock_response(200, json_data=fake_payload)):
            results = c.search_filings(form_type="4", since_days=2, max_results=10)

        self.assertEqual(len(results), 2)
        r0 = results[0]
        self.assertEqual(r0["accession"], "0001234567-26-000001")
        self.assertEqual(r0["filer_cik"], "0001234567")
        self.assertEqual(r0["filed_date"], "2026-04-25")
        self.assertIn("ACME", r0["tickers"])
        # URL has CIK without leading zeros
        self.assertIn("/1234567/", r0["primary_doc_url"])

    def test_search_handles_empty_hits(self):
        c = edgar_client.EdgarClient(rps=0)
        with patch.object(c._session, "get",
                          return_value=_mock_response(200,
                              json_data={"hits": {"hits": []}})):
            self.assertEqual(c.search_filings(form_type="4"), [])

    def test_search_returns_empty_on_403(self):
        c = edgar_client.EdgarClient(rps=0, max_retries=1)
        with patch.object(c._session, "get",
                          return_value=_mock_response(403)):
            self.assertEqual(c.search_filings(form_type="4"), [])


class TestFetchUrl(unittest.TestCase):

    def setUp(self):
        os.environ["SEC_EDGAR_UA_NAME"]  = "TestBot"
        os.environ["SEC_EDGAR_UA_EMAIL"] = "test@example.com"

    def tearDown(self):
        os.environ.pop("SEC_EDGAR_UA_NAME", None)
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)

    def test_fetch_url_returns_text_on_200(self):
        c = edgar_client.EdgarClient(rps=0)
        with patch.object(c._session, "get",
                          return_value=_mock_response(200, text="<xml/>")):
            self.assertEqual(c.fetch_url("https://www.sec.gov/x.xml"), "<xml/>")

    def test_fetch_url_returns_none_on_failure(self):
        c = edgar_client.EdgarClient(rps=0, max_retries=1)
        with patch.object(c._session, "get",
                          return_value=_mock_response(500)):
            self.assertIsNone(c.fetch_url("https://www.sec.gov/x.xml"))


class TestExternalFetchInjection(unittest.TestCase):

    def setUp(self):
        os.environ["SEC_EDGAR_UA_NAME"]  = "TestBot"
        os.environ["SEC_EDGAR_UA_EMAIL"] = "test@example.com"

    def tearDown(self):
        os.environ.pop("SEC_EDGAR_UA_NAME", None)
        os.environ.pop("SEC_EDGAR_UA_EMAIL", None)

    def test_external_fetch_routed(self):
        called = {}
        def fake_external(url, params=None, headers=None):
            called["url"]     = url
            called["headers"] = headers
            return _mock_response(200, text="external result")

        c = edgar_client.EdgarClient(rps=0, external_fetch=fake_external)
        result = c.fetch_url("https://www.sec.gov/y.xml")
        self.assertEqual(result, "external result")
        self.assertEqual(called["url"], "https://www.sec.gov/y.xml")
        self.assertIn("User-Agent", called["headers"])


if __name__ == "__main__":
    unittest.main()
