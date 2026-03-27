"""
installers/common/preflight.py — Installer Preflight Checks
Synthos · shared installer helper

Validates the system environment before any installation work begins.
All checks are explicit, logged, and return a structured result.

Rules:
  - No check raises — all failures return PreflightResult with passed=False
  - Python version check is fatal (installer cannot continue)
  - All other checks are warnings if non-fatal, errors if fatal
  - Non-Pi platform triggers confirmation prompt, not hard stop
"""

import logging
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("installer.preflight")

REQUIRED_PYTHON = (3, 9)


@dataclass
class CheckResult:
    name: str
    passed: bool
    fatal: bool
    detail: str = ""


@dataclass
class PreflightResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.fatal)

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.fatal]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.fatal]

    def report(self) -> str:
        lines = ["PREFLIGHT RESULTS"]
        lines.append("─" * 40)
        for c in self.checks:
            icon = "✓" if c.passed else ("✗" if c.fatal else "⚠")
            line = f"  {icon}  {c.name}"
            if c.detail:
                line += f" — {c.detail}"
            lines.append(line)
        lines.append("─" * 40)
        if self.passed:
            lines.append("  Preflight: PASS")
        else:
            lines.append("  Preflight: FAIL — cannot continue")
            for f in self.failures:
                lines.append(f"    → {f.name}: {f.detail}")
        return "\n".join(lines)


# ── INDIVIDUAL CHECKS ─────────────────────────────────────────────────────────

def check_python_version() -> CheckResult:
    """Python >= 3.9 required — fatal if not met."""
    major, minor = sys.version_info[:2]
    ver_str = f"{major}.{minor}"
    req_str = f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}"
    if (major, minor) >= REQUIRED_PYTHON:
        return CheckResult("Python version", True, True, f"{ver_str} (>= {req_str} required)")
    return CheckResult(
        "Python version", False, True,
        f"{ver_str} found — {req_str}+ required. Upgrade Python before continuing."
    )


def check_pip() -> CheckResult:
    """pip must be available."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return CheckResult("pip", True, True, result.stdout.split()[1] if result.stdout else "ok")
    return CheckResult("pip", False, True, "pip not available — install python3-pip")


def check_sqlite3() -> CheckResult:
    """sqlite3 must be importable."""
    try:
        import sqlite3  # noqa: F401
        return CheckResult("sqlite3", True, True, "available")
    except ImportError:
        return CheckResult("sqlite3", False, True, "sqlite3 not available — rebuild Python with sqlite support")


def check_cron() -> CheckResult:
    """cron must be available — warning only (operator may configure manually)."""
    if shutil.which("crontab"):
        return CheckResult("cron", True, False, "crontab found")
    return CheckResult("cron", False, False, "crontab not found — cron entries must be added manually")


def check_platform() -> CheckResult:
    """Warn if not running on a Raspberry Pi — not fatal, but notable."""
    machine = platform.machine()
    system = platform.system()
    pi_archs = {"aarch64", "armv7l", "armv6l"}
    if system == "Linux" and machine in pi_archs:
        return CheckResult("Platform", True, False, f"Raspberry Pi detected ({machine})")
    return CheckResult(
        "Platform", False, False,
        f"{system}/{machine} — not a Raspberry Pi. "
        "Installer will continue but is designed for Pi OS Lite."
    )


def check_git() -> CheckResult:
    """git is optional but logged."""
    if shutil.which("git"):
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
        ver = result.stdout.strip() if result.returncode == 0 else "found"
        return CheckResult("git", True, False, ver)
    return CheckResult("git", False, False, "git not found — sync.py will not work")


# ── RUNNER ────────────────────────────────────────────────────────────────────

def run_preflight(*, require_pi: bool = False) -> PreflightResult:
    """
    Run all preflight checks and return a PreflightResult.

    Args:
        require_pi: If True, non-Pi platform is treated as fatal.
    """
    result = PreflightResult()

    checks = [
        check_python_version(),
        check_pip(),
        check_sqlite3(),
        check_cron(),
        check_git(),
        check_platform(),
    ]

    if require_pi:
        # Elevate platform check to fatal
        for i, c in enumerate(checks):
            if c.name == "Platform" and not c.passed:
                checks[i] = CheckResult("Platform", False, True, c.detail)

    for c in checks:
        result.checks.append(c)
        if c.passed:
            log.info("PREFLIGHT ✓ %s — %s", c.name, c.detail)
        elif c.fatal:
            log.error("PREFLIGHT ✗ %s — %s", c.name, c.detail)
        else:
            log.warning("PREFLIGHT ⚠ %s — %s", c.name, c.detail)

    return result
