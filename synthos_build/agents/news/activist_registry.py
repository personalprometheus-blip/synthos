"""
agents/news/activist_registry.py — known-activist CIK lookup
============================================================

13D filings carry the "filed an activist position" signal only when the
*filer* is a known activist.  ~95% of raw 13D filings come from family
offices / foundations / employee benefit plans where the 5% crossing
isn't actually activism — they just don't qualify for the simpler 13G
form.  The classifier is what turns 13D from noise into signal.

Source of truth
---------------
JSON config at `synthos_build/data/activists.json` (path overridable
via env `ACTIVISTS_CONFIG`).  Schema:

    {
      "version": 1,
      "updated": "YYYY-MM-DD",
      "activists": [
        {
          "cik":        "0001336528",        # EDGAR filer CIK, zero-padded
          "name":       "Pershing Square Capital Management",
          "principals": ["Bill Ackman"],
          "tier":        1,                  # 1 = top-tier, 2 = mid, 3 = niche
          "notes":      "Verified at edgar.sec.gov YYYY-MM-DD"
        },
        ...
      ]
    }

Why this is operator-owned
--------------------------
CIK numbers are stable but easy to misattribute.  Baking unverified CIKs
into the agent code risks:

  * Silent miss — real Icahn 13D filings get skipped because the wrong
    CIK is in the registry.
  * False positive — a random foundation gets tagged as Icahn, the
    trader weights it tier-1, and acts on noise.

The operator is responsible for verifying each CIK against
edgar.sec.gov (search by filer name, confirm CIK matches expected legal
entity) before adding to the config.  This module ships with an empty
default so 13D fetching produces zero signals until the operator
populates real entries.

API
---
    registry = ActivistRegistry()              # default path
    registry.load()                            # idempotent
    info = registry.lookup("0001336528")       # → {name, tier, ...} or None
    if registry.is_known(cik): ...
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional


log = logging.getLogger("activist_registry")

_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "activists.json"
)


def _normalize_cik(cik: str | int) -> str:
    """EDGAR CIKs vary in zero-padding across data sources.  Normalize
    to a 10-character zero-padded string for consistent dict lookup."""
    if cik is None:
        return ""
    s = str(cik).strip().lstrip("0")
    if not s:
        return "0".zfill(10)
    if not s.isdigit():
        return ""
    return s.zfill(10)


class ActivistRegistry:
    def __init__(self, config_path: Optional[str] = None):
        self._by_cik: dict[str, dict] = {}
        self._loaded: bool = False
        self._path: str = config_path or os.environ.get(
            "ACTIVISTS_CONFIG", _DEFAULT_PATH
        )

    def load(self) -> "ActivistRegistry":
        """Load the JSON config.  Idempotent — safe to call multiple times.
        Missing file → empty registry (the safe default)."""
        if self._loaded:
            return self
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            log.info(f"activists config not found at {self._path} — "
                     f"registry empty (13D classifier will skip all filers)")
            self._loaded = True
            return self
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"activists config unreadable ({self._path}): {e} — "
                        f"registry empty")
            self._loaded = True
            return self

        for entry in (data.get("activists") or []):
            cik = _normalize_cik(entry.get("cik"))
            if not cik or cik == "0".zfill(10):
                continue
            # Carry only the fields we'll use; ignore extras for forward-compat
            self._by_cik[cik] = {
                "cik":        cik,
                "name":       entry.get("name") or "(unnamed)",
                "principals": entry.get("principals") or [],
                "tier":        int(entry.get("tier") or 1),
                "notes":      entry.get("notes") or "",
            }
        log.info(f"activist registry loaded: {len(self._by_cik)} entries "
                 f"from {self._path}")
        self._loaded = True
        return self

    def lookup(self, cik: str | int) -> Optional[dict]:
        """Return the registry entry for a CIK or None if unknown."""
        if not self._loaded:
            self.load()
        return self._by_cik.get(_normalize_cik(cik))

    def is_known(self, cik: str | int) -> bool:
        return self.lookup(cik) is not None

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._by_cik)
