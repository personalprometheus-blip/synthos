"""
agents/news/edgar_client.py — SEC EDGAR HTTP client
====================================================

Generic, rate-limited fetcher for SEC EDGAR endpoints.  Built for the
news-agent EDGAR expansion (Form 4 / 8-K / 13D / 13G / 144).

SEC compliance posture
----------------------
EDGAR enforces a 10 req/sec cap and *requires* a User-Agent that
identifies the requester (name + contact email).  Requests without it
are 403'd or, with sustained abuse, IP-banned.  See:
  https://www.sec.gov/os/accessing-edgar-data

This client:
  * always sets `User-Agent: <name> <email>` from env (`SEC_EDGAR_UA_NAME`,
    `SEC_EDGAR_UA_EMAIL`); refuses to fetch if either is missing.
  * limits concurrency to a configurable RPS via a token-bucket
    (default 8/sec — leaves headroom under the 10/sec cap).
  * routes through fetch_with_retry() if available, so the news_agent's
    existing circuit breaker covers EDGAR too.

Endpoints used
--------------
  * Full-text search: https://efts.sec.gov/LATEST/search-index?q=&...
    Returns JSON list of recent filings matching form/date filters.
  * Filing index:     https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/
    Used to discover the primary doc (XML/HTML) inside a filing.
  * Submissions:      https://data.sec.gov/submissions/CIK<10digits>.json
    Per-filer history (used by 13D activist classifier).

Dependency injection
--------------------
This module imports nothing from retail_database / news_agent.  All
state (HTTP session, rate-limiter clock) lives on the EdgarClient
instance.  fetch_with_retry is *optional* — if the caller passes one,
we route through it; otherwise we use plain requests.get with our own
exponential backoff.  This keeps the module unit-testable on Mac
Python 3.9 where retail_database can't be imported.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests


log = logging.getLogger("edgar_client")

EDGAR_SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVES_BASE   = "https://www.sec.gov/Archives/edgar/data"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions"

DEFAULT_RPS              = 8        # under SEC's 10/sec cap
DEFAULT_TIMEOUT_S        = (5, 15)  # (connect, read)
DEFAULT_MAX_RETRIES      = 3
DEFAULT_RETRY_BACKOFF_S  = 1.5      # exponential


class EdgarUserAgentMissing(RuntimeError):
    """Raised when SEC_EDGAR_UA_NAME or SEC_EDGAR_UA_EMAIL is unset."""


def _get_user_agent() -> str:
    name  = os.environ.get("SEC_EDGAR_UA_NAME", "").strip()
    email = os.environ.get("SEC_EDGAR_UA_EMAIL", "").strip()
    if not name or not email:
        raise EdgarUserAgentMissing(
            "SEC EDGAR fetches require SEC_EDGAR_UA_NAME and "
            "SEC_EDGAR_UA_EMAIL in env. SEC enforces this. See "
            "https://www.sec.gov/os/accessing-edgar-data"
        )
    return f"{name} {email}"


class _RateLimiter:
    """Simple token bucket: at most `rps` requests per second."""
    def __init__(self, rps: float):
        self.rps        = float(rps)
        self.min_gap_s  = 1.0 / self.rps if self.rps > 0 else 0.0
        self._last_at_s = 0.0

    def wait(self) -> None:
        if self.min_gap_s <= 0:
            return
        now    = time.monotonic()
        elapse = now - self._last_at_s
        gap    = self.min_gap_s - elapse
        if gap > 0:
            time.sleep(gap)
        self._last_at_s = time.monotonic()


class EdgarClient:
    """Rate-limited, retry-aware HTTP client for SEC EDGAR.

    Usage:
        client = EdgarClient()  # picks UA from env
        rows = client.search_filings(form_type="4", since_days=2,
                                     max_results=200)
        xml  = client.fetch_url(rows[0]["primary_doc_url"])
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        rps: float = DEFAULT_RPS,
        timeout: tuple = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        external_fetch: Optional[Callable] = None,
    ):
        self.user_agent      = user_agent or _get_user_agent()
        self.timeout         = timeout
        self.max_retries     = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._rate           = _RateLimiter(rps)
        # If caller passes news_agent.fetch_with_retry, route through it
        # so the circuit breaker covers EDGAR.  Signature:
        #   external_fetch(url, params=None, headers=None) -> requests.Response | None
        self._external_fetch = external_fetch
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent":       self.user_agent,
            "Accept-Encoding":  "gzip, deflate",
        })

    # ── Low-level fetch ─────────────────────────────────────────────────────

    def _fetch(self, url: str, params: Optional[dict] = None,
               extra_headers: Optional[dict] = None) -> Optional[requests.Response]:
        """Fetch a URL with rate-limit + retries.  Returns None on failure."""
        self._rate.wait()
        if self._external_fetch is not None:
            # Route through caller's circuit breaker.  External fetcher
            # is responsible for its own retry policy.
            headers = dict(self._session.headers)
            if extra_headers:
                headers.update(extra_headers)
            return self._external_fetch(url, params=params, headers=headers)
        # Local retry path
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                r = self._session.get(url, params=params, headers=extra_headers,
                                      timeout=self.timeout)
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    wait = self.retry_backoff_s * (2 ** attempt)
                    log.warning(f"EDGAR fetch failed ({url[:80]}) "
                                f"attempt {attempt+1}/{self.max_retries}: {e} "
                                f"— retrying in {wait:.1f}s")
                    time.sleep(wait)
        log.error(f"EDGAR fetch permanently failed: {url[:80]} — {last_err}")
        return None

    def fetch_url(self, url: str) -> Optional[str]:
        """Fetch an arbitrary EDGAR URL and return the body text.  Used
        for filing primary docs (Form 4 XML, 8-K HTML)."""
        r = self._fetch(url)
        return r.text if (r is not None and r.status_code == 200) else None

    # ── Full-text search ────────────────────────────────────────────────────

    def search_filings(
        self,
        form_type: str,
        since_days: int = 2,
        max_results: int = 200,
        ciks: Optional[list[str]] = None,
    ) -> list[dict]:
        """Search EDGAR full-text index for recent filings of a given form.

        Args:
            form_type:    "4", "8-K", "SC 13D", "SC 13G", "144"
            since_days:   Only return filings filed in the last N days
            max_results:  Cap results returned (EDGAR pages 100 at a time)
            ciks:         Optional list of filer CIKs to restrict search

        Returns a list of normalized hit dicts:
            {
                "accession":     "0001234567-26-000001",
                "form":          "4",
                "filer_cik":     "0001234567",
                "filer_name":    "Acme Insider, Director",
                "filed_date":    "2026-04-25",
                "primary_doc":   "ownership.xml",
                "primary_doc_url": "https://www.sec.gov/Archives/edgar/data/...",
                "tickers":       ["AAPL"],   # if EDGAR provided
                "raw_hit":       {...},      # original hit for debugging
            }
        """
        end   = datetime.now(timezone.utc).date()
        start = end - timedelta(days=since_days)
        params = {
            "q":         "",
            "forms":     form_type,
            "dateRange": "custom",
            "startdt":   start.isoformat(),
            "enddt":     end.isoformat(),
        }
        if ciks:
            params["ciks"] = ",".join(c.lstrip("0").zfill(10) for c in ciks)

        results: list[dict] = []
        offset = 0
        while len(results) < max_results:
            params_page = dict(params)
            params_page["from"] = offset
            r = self._fetch(EDGAR_SEARCH_URL, params=params_page)
            if r is None:
                break
            try:
                payload = r.json()
            except ValueError:
                log.warning(f"EDGAR search returned non-JSON: {r.text[:200]!r}")
                break
            hits = (payload.get("hits") or {}).get("hits") or []
            if not hits:
                break
            for h in hits:
                norm = self._normalize_hit(h)
                if norm:
                    results.append(norm)
                    if len(results) >= max_results:
                        break
            if len(hits) < 10:  # EDGAR's default page size — short page = end
                break
            offset += len(hits)
        return results

    # Ticker(s) embedded inside display_names — EDGAR returns this format
    # consistently for 8-K / Form 144 / 13D / 13G hits. Examples:
    #   'STURM RUGER & CO INC  (RGR)  (CIK 0000095029)'        → ['RGR']
    #   'Pasithea Therapeutics Corp.  (KTTA, KTTAW)  (CIK …)' → ['KTTA', 'KTTAW']
    #   'Benchmark 2026-B43 Mortgage Trust  (CIK 0002121298)'  → []
    # Handles multi-class tickers and skips hits with only a CIK in parens.
    _TICKER_IN_DISPLAY_RE = re.compile(r'\(([A-Z][A-Z0-9,\s/.-]{0,30})\)')

    @classmethod
    def _tickers_from_display_names(cls, display_names) -> list[str]:
        if not display_names:
            return []
        names = display_names if isinstance(display_names, list) else [display_names]
        out: list[str] = []
        for name in names:
            if not name:
                continue
            for m in cls._TICKER_IN_DISPLAY_RE.finditer(str(name)):
                token = m.group(1).strip()
                if token.upper().startswith('CIK '):
                    continue  # parenthesized CIK, not a ticker
                # Multi-class: 'KTTA, KTTAW' or 'BRK.A, BRK.B'
                for sym in re.split(r'[,\s/]+', token):
                    sym = sym.strip().upper()
                    if 1 <= len(sym) <= 8 and re.match(r'^[A-Z][A-Z0-9.-]*$', sym):
                        out.append(sym)
        # Preserve order, dedup
        seen = set(); deduped = []
        for s in out:
            if s not in seen:
                seen.add(s); deduped.append(s)
        return deduped

    def _normalize_hit(self, hit: dict) -> Optional[dict]:
        """Pull the bits we care about out of an EDGAR search hit.
        Returns None if the hit is malformed."""
        try:
            src = hit.get("_source") or {}
            ad  = (hit.get("_id") or "").split(":")
            if len(ad) != 2:
                return None
            accession_dashed, primary_doc = ad[0], ad[1]
            # accession in URL form: 0001234567-26-000001 → 000123456726000001
            acc_path = accession_dashed.replace("-", "")
            cik_raw  = (src.get("ciks") or [""])[0] or ""
            cik_path = cik_raw.lstrip("0") or "0"
            url = (f"{EDGAR_ARCHIVES_BASE}/{cik_path}/"
                   f"{acc_path}/{primary_doc}")
            # Ticker extraction: prefer the dedicated 'tickers' array if
            # present; fall back to parsing display_names. EDGAR's full-text
            # search consistently omits 'tickers' for 8-K, Form 144, 13D/13G,
            # but always embeds them in display_names. Without this fallback
            # those four sources lose every signal at the empty-tickers
            # check in their respective fetchers.
            tickers = src.get("tickers") or []
            if not tickers:
                tickers = self._tickers_from_display_names(src.get("display_names"))
            return {
                "accession":       accession_dashed,
                "form":             src.get("forms", [""])[0] if isinstance(src.get("forms"), list) else src.get("forms") or "",
                "filer_cik":       cik_raw,
                "filer_name":      (src.get("display_names") or [""])[0],
                "filed_date":      src.get("file_date") or "",
                "primary_doc":     primary_doc,
                "primary_doc_url": url,
                "tickers":         tickers,
                "raw_hit":         hit,
            }
        except Exception as e:
            log.debug(f"hit normalization failed: {e}")
            return None
