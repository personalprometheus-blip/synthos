"""News-agent submodule package.

Phase-0 of the C9 module split (backlog 2026-04-24): pure-data
extraction only. Keyword dictionaries, term sets, and sector maps live
in `keywords.py`; everything else (fetchers, classifiers, gate logic)
remains in `agents/retail_news_agent.py` until C8 lands and the full
classifier split becomes safe.

Re-exporting from this package keeps any future
`from agents.news.<module>` imports stable as the split progresses.
"""

from . import keywords  # noqa: F401  — re-export for namespace access
