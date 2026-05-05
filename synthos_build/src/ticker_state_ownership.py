"""ticker_state_ownership.py — single source of truth for ticker_state field ownership.

Mirrors the field-ownership table in docs/TICKER_STATE_ARCHITECTURE.md but
makes it importable as Python data. Two consumers:

  - retail_ticker_state_auditor.py — uses this to map gaps to owning agents
  - future agents that want to declare "I own field X" by importing the
    name and asserting against it (catches drift between doc + code)

Ownership has THREE columns now (vs the doc's two), because "owner" was
doing two jobs in the original spec:

  - owner          : which agent writes this field
  - refresh_policy : how often the field is *expected* to be filled
  - severity_rule  : how to grade a NULL when the policy says it should
                     have been filled by now

This module is read-only. Agents do NOT register themselves here — the
map is the contract. If you add a new ticker_state field, edit this map
in the same commit as the schema change.
"""

# Refresh policy semantics:
#   once_on_first_seen — value should be filled within minutes of first
#                        time the ticker appears anywhere in the system.
#                        Identity metadata (sector, company, exchange).
#                        NULL after >1h on an active ticker = bug.
#
#   per_cycle          — owning agent runs on a tight schedule
#                        (5-10 min) and refreshes the value each pass.
#                        NULL after >1h on an active ticker = bug.
#
#   per_run            — owning agent runs less often (twice daily,
#                        on-demand). NULL after >12h on an active
#                        ticker = bug. NULL on a brand-new ticker is
#                        not a bug — owner just hasn't run yet.
#
#   per_event          — owning agent only fills the value when a
#                        triggering event arrives (news article,
#                        Form 4, etc.). NULL is never a bug — it
#                        means no event yet, or no data for this
#                        ticker. Reported but not flagged.

OWNERSHIP: dict[str, dict] = {
    # ── identity (refresh once, never again unless corp action) ──────────
    "sector":                {"owner": "ticker_identity_first_fill",     "refresh_policy": "once_on_first_seen"},
    "company":               {"owner": "ticker_identity_first_fill",     "refresh_policy": "once_on_first_seen"},
    "exchange":              {"owner": "ticker_identity_first_fill",     "refresh_policy": "once_on_first_seen"},

    # ── price / market microstructure (price poller cycle) ───────────────
    "price":                 {"owner": "retail_price_poller",            "refresh_policy": "per_cycle"},
    "price_at":              {"owner": "retail_price_poller",            "refresh_policy": "per_cycle"},
    "vol_bucket":            {"owner": "retail_price_poller",            "refresh_policy": "per_cycle"},
    "atr":                   {"owner": "retail_price_poller",            "refresh_policy": "per_cycle"},
    "spy_correlation":       {"owner": "retail_price_poller",            "refresh_policy": "per_cycle"},

    # ── news (only when news arrives; NULL is normal off-cycle) ──────────
    "news_score_4h":         {"owner": "retail_news_agent",              "refresh_policy": "per_event"},
    "news_evaluated_at":     {"owner": "retail_news_agent",              "refresh_policy": "per_event"},

    # ── sentiment (5-10 min cadence) ─────────────────────────────────────
    "sentiment_score":       {"owner": "retail_market_sentiment_agent",  "refresh_policy": "per_cycle"},
    "sentiment_evaluated_at":{"owner": "retail_market_sentiment_agent",  "refresh_policy": "per_cycle"},
    "cascade_tier":          {"owner": "retail_market_sentiment_agent",  "refresh_policy": "per_cycle"},
    "volume_anomaly":        {"owner": "retail_market_sentiment_agent",  "refresh_policy": "per_cycle"},
    "volume_evaluated_at":   {"owner": "retail_market_sentiment_agent",  "refresh_policy": "per_cycle"},

    # ── screener (twice daily + on-demand) ───────────────────────────────
    "screener_score":        {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},
    "screener_evaluated_at": {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},
    "momentum_score":        {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},
    "momentum_evaluated_at": {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},
    "sector_score":          {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},
    "sector_evaluated_at":   {"owner": "retail_sector_screener",         "refresh_policy": "per_run"},

    # ── insider (future EDGAR daemon, currently unowned) ─────────────────
    "insider_signal":        {"owner": "future_edgar_daemon",            "refresh_policy": "per_event"},
    "insider_evaluated_at":  {"owner": "future_edgar_daemon",            "refresh_policy": "per_event"},
}


# Hours after which a NULL field is considered an anomaly (per policy).
# Tuned conservative — the auditor flags WHAT, not WHEN-EXACTLY.
STALENESS_THRESHOLDS_HOURS = {
    "once_on_first_seen": 1,    # identity should fill within minutes
    "per_cycle":          1,    # 5-10min cadence; 1h grace
    "per_run":            12,   # twice-daily; 12h grace
    "per_event":          None, # never anomaly — flag only as info
}


def fields_by_owner() -> dict[str, list[str]]:
    """Reverse-index: owner -> [fields it owns]. Used for per-agent reports."""
    out: dict[str, list[str]] = {}
    for field, meta in OWNERSHIP.items():
        out.setdefault(meta["owner"], []).append(field)
    return out


def owner_of(field: str) -> str | None:
    """Lookup helper. Returns None if field is not in the ownership map
    (e.g., raw column like ticker / is_active / first_seen_at)."""
    meta = OWNERSHIP.get(field)
    return meta["owner"] if meta else None


def policy_of(field: str) -> str | None:
    meta = OWNERSHIP.get(field)
    return meta["refresh_policy"] if meta else None


# Export convenience constant — set of all owned fields
OWNED_FIELDS = frozenset(OWNERSHIP.keys())
