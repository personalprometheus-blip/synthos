"""
installers/common/progress.py — Install Progress State Manager
Synthos · shared installer helper

Manages .install_progress.json — the idempotent state file that allows
both retail and company installers to resume safely after interruption.

Rules:
  - Progress file lives at SYNTHOS_HOME/.install_progress.json
  - State is read on every installer entry
  - State is written before every transition
  - No sensitive values (keys, secrets) are ever written to this file
  - Safe to delete — installer re-enters at UNINITIALIZED
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("installer.progress")

PROGRESS_FILENAME = ".install_progress.json"


class ProgressManager:
    """
    Read/write installer progress state. Thread-safe via atomic file write.
    """

    def __init__(self, synthos_home: Path) -> None:
        self.path: Path = synthos_home / PROGRESS_FILENAME
        self._state: dict[str, Any] = {}
        self._loaded: bool = False

    # ── PUBLIC API ────────────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """
        Load progress from disk. Returns empty dict if file absent.
        Does NOT raise — missing file is normal for first run.
        """
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
                self._state = json.loads(raw)
                self._loaded = True
                log.info("Resumed install state: %s", self._state.get("state", "UNKNOWN"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read progress file (%s) — starting fresh", exc)
                self._state = {}
        else:
            self._state = {}
            log.info("No prior install state found — starting fresh")
        return self._state

    def save(self) -> None:
        """
        Write current state to disk atomically (write temp, rename).
        Raises OSError if filesystem write fails.
        """
        self._state["_updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.error("Failed to write progress file: %s", exc)
            raise

    def set(self, key: str, value: Any) -> None:
        """Set a key in state and persist immediately."""
        self._state[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def transition(self, new_state: str) -> None:
        """Record a state machine transition with timestamp."""
        old = self._state.get("state", "NONE")
        self._state["state"] = new_state
        self._state[f"entered_{new_state.lower()}_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        log.info("State transition: %s → %s", old, new_state)
        self.save()

    def delete(self) -> None:
        """Remove progress file. Used on clean uninstall only."""
        if self.path.exists():
            self.path.unlink()
            log.info("Progress file removed")

    @property
    def state(self) -> str:
        return self._state.get("state", "UNINITIALIZED")

    @property
    def is_fresh(self) -> bool:
        return not self._loaded and not self.path.exists()
