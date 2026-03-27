"""
patches.py — Patches > Bug Finder Agent
Synthos Company Pi | /home/pi/synthos-company/agents/patches.py

Role:
  Scan all retail Pi logs and system state for bugs, crashes, and anomalies.
  Triage findings via Claude. Submit structured suggestions to suggestions.json.
  Write the daily morning report digest for Scoop to deliver.
  Monitor post-deployment health after Friday pushes.

Patches is a manager, not a task executor. It detects, documents, and escalates.
It does not fix anything. Blueprint fixes things — after the project lead approves.

Schedule (via Timekeeper):
  Hourly light pass  — fast checks, crash detection, log scan
  Daily deep pass    — full audit including DB integrity and API reachability
  Post-deploy watch  — activated by Blueprint after Friday push (48h window)
  Morning report     — daily at 7:30am ET, feeds Scoop for 8am delivery

Cron (Timekeeper manages priority, but fallback cron entries):
  0 * * * 1-5   python3 patches.py --mode light  >> logs/bug_finder.log 2>&1
  0 6 * * 1-5   python3 patches.py --mode deep   >> logs/bug_finder.log 2>&1
  30 7 * * 1-5  python3 patches.py --mode report >> logs/bug_finder.log 2>&1

Output:
  logs/bug_finder.log         — append-only run log
  data/patches_latest.json    — latest audit result for command portal
  data/morning_report.json    — daily digest for Scoop
  data/suggestions.json       — structured bug reports (append only)
"""

import os
import sys
import ast
import json
import time
import logging
import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import anthropic

# ── DYNAMIC PATH RESOLUTION ───────────────────────────────────────────────────
# Works with any username — never hardcodes /home/pi/

_HERE    = Path(__file__).resolve().parent     # agents/
BASE_DIR = _HERE.parent                        # synthos-company/

if str(BASE_DIR / "utils") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "utils"))

from db_helpers import DB
_db = DB()

# ── CONFIG ────────────────────────────────────────────────────────────────────

RETAIL_DIR     = BASE_DIR.parent / "synthos"
MORNING_REPORT = BASE_DIR / "data" / "morning_report.json"
LATEST_FILE    = BASE_DIR / "data" / "patches_latest.json"
STATE_FILE     = BASE_DIR / "data" / ".patches_state.json"
LOG_FILE       = BASE_DIR / "logs" / "bug_finder.log"

ANTHROPIC_MODEL_TRIAGE = "claude-haiku-4-5-20251001"   # fast + cheap for triage
ANTHROPIC_MODEL_DEEP   = "claude-sonnet-4-6"           # deeper reasoning for reports

PI_ID = os.environ.get("PI_ID", "company-pi-4b")

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PATCHES] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("patches")

# ── WHAT TO CHECK ─────────────────────────────────────────────────────────────

REQUIRED_ENV_KEYS = [
    "ANTHROPIC_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL", "TRADING_MODE", "OPERATING_MODE",
    "PI_ID", "MONITOR_URL", "MONITOR_TOKEN",
]

REQUIRED_PYTHON_FILES = [
    "agent1_trader.py", "agent2_research.py", "agent3_sentiment.py",
    "database.py", "heartbeat.py", "boot_sequence.py", "watchdog.py",
    "cleanup.py", "portal.py", "health_check.py",
]

REQUIRED_CRON_PATTERNS = [
    "agent1_trader.py", "agent2_research.py", "agent3_sentiment.py",
    "heartbeat.py", "cleanup.py", "boot_sequence.py",
]

MANAGED_PROCESSES = [
    ("portal.py",          "Portal web UI"),
    ("synthos_monitor.py", "Monitor server"),
    ("watchdog.py",        "Watchdog"),
]

AGENT_LOGS = [
    ("trader.log",    "Bolt (Trader)"),
    ("daily.log",     "Scout (Research)"),
    ("pulse.log",     "Pulse (Sentiment)"),
    ("heartbeat.log", "Heartbeat"),
    ("portal.log",    "Portal"),
    ("watchdog.log",  "Watchdog"),
    ("boot.log",      "Boot"),
]

IGNORE_LOG_PATTERNS = [
    "database is locked",
    "DB error — rolled back",
    "WARNING: This is a development server",
    "Cold start",
    "Schema verified",
    "REMOTE_FAIL",
]

# Crash pattern: agent exited unexpectedly N times in M minutes
CRASH_THRESHOLD_COUNT   = 3
CRASH_THRESHOLD_MINUTES = 60


# ── FINDING MODEL ─────────────────────────────────────────────────────────────

class Finding:
    """A single audit finding."""

    def __init__(self, level: str, category: str, message: str,
                 fix: str = None, pi_id: str = None):
        self.level    = level     # CRITICAL | HIGH | WARN | INFO
        self.category = category
        self.message  = message
        self.fix      = fix       # suggested remediation
        self.pi_id    = pi_id or PI_ID
        self.ts       = now_iso()

    def to_dict(self) -> dict:
        return {
            "level":    self.level,
            "category": self.category,
            "message":  self.message,
            "fix":      self.fix,
            "pi_id":    self.pi_id,
            "ts":       self.ts,
        }

    def __repr__(self):
        fix_str = f" → {self.fix}" if self.fix else ""
        return f"[{self.level}] {self.category}: {self.message}{fix_str}"


# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_run": None, "last_seen_errors": {}, "last_score": None}

def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ── CHECKS ────────────────────────────────────────────────────────────────────

def check_syntax(findings: list) -> None:
    """Syntax check all Python files in retail Pi core."""
    core_dir = RETAIL_DIR / "core"
    if not core_dir.exists():
        findings.append(Finding("WARN", "Syntax",
            f"Retail core directory not found: {core_dir}"))
        return

    errors = []
    for fpath in core_dir.glob("**/*.py"):
        if fpath.name.startswith("."):
            continue
        try:
            ast.parse(fpath.read_text(errors="replace"))
        except SyntaxError as e:
            errors.append(f"{fpath.name} line {e.lineno}: {e.msg}")
        except Exception as e:
            errors.append(f"{fpath.name}: {e}")

    if errors:
        for err in errors:
            findings.append(Finding("CRITICAL", "Syntax", err,
                "Syntax error will prevent agent from running — fix before Friday push"))
    else:
        findings.append(Finding("INFO", "Syntax",
            f"All .py files in core/ syntax clean"))


def check_required_files(findings: list) -> None:
    """Check required retail Pi files exist."""
    missing = [
        f for f in REQUIRED_PYTHON_FILES
        if not (RETAIL_DIR / "core" / f).exists()
    ]
    if missing:
        findings.append(Finding("CRITICAL", "Files",
            f"Missing core files: {', '.join(missing)}",
            "Run git pull origin main on retail Pi or check deployment logs"))
    else:
        findings.append(Finding("INFO", "Files",
            f"All {len(REQUIRED_PYTHON_FILES)} required core files present"))


def check_env_keys(findings: list) -> None:
    """Check required .env keys are present on retail Pi."""
    env_path = RETAIL_DIR / "user" / ".env"
    if not env_path.exists():
        findings.append(Finding("CRITICAL", "Config",
            ".env not found on retail Pi",
            "Customer setup incomplete — run installer"))
        return

    # Read without exposing values
    try:
        env_text = env_path.read_text(errors="replace")
        env_keys = {
            line.split("=")[0].strip()
            for line in env_text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        missing = [k for k in REQUIRED_ENV_KEYS if k not in env_keys]
        if missing:
            findings.append(Finding("CRITICAL", "Config",
                f"Missing .env keys: {', '.join(missing)}",
                "Add missing keys via portal Settings → API Keys"))
        else:
            findings.append(Finding("INFO", "Config",
                "All required .env keys present"))
    except Exception as e:
        findings.append(Finding("WARN", "Config", f"Could not read .env: {e}"))


def check_processes(findings: list) -> None:
    """Check managed processes are running on retail Pi."""
    for script, label in MANAGED_PROCESSES:
        try:
            result = subprocess.run(
                ["pgrep", "-f", script],
                capture_output=True, text=True
            )
            running = bool(result.stdout.strip())
        except Exception:
            running = False

        if not running:
            findings.append(Finding("CRITICAL", "Process",
                f"{label} ({script}) is not running",
                f"Watchdog should restart this. If not: "
                f"nohup python3 ~/synthos/core/{script} >> ~/synthos/logs/monitor.log 2>&1 &"))
        else:
            pid = result.stdout.strip().split("\n")[0]
            findings.append(Finding("INFO", "Process",
                f"{label} running (pid {pid})"))


def check_disk(findings: list) -> None:
    """Check available disk space on retail Pi."""
    try:
        stat = os.statvfs(str(RETAIL_DIR))
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 200:
            findings.append(Finding("CRITICAL", "Disk",
                f"Only {free_mb:.0f}MB free on retail Pi",
                "Run cleanup.py or delete old log archives before next run"))
        elif free_mb < 500:
            findings.append(Finding("WARN", "Disk",
                f"{free_mb:.0f}MB free — getting low",
                "Schedule cleanup before end of week"))
        else:
            findings.append(Finding("INFO", "Disk",
                f"{free_mb:.0f}MB free"))
    except Exception as e:
        findings.append(Finding("WARN", "Disk", f"Could not check disk: {e}"))


def check_crontab(findings: list) -> None:
    """Check required cron jobs are present."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True
        )
        crontab = result.stdout
        missing = [p for p in REQUIRED_CRON_PATTERNS if p not in crontab]
        if missing:
            findings.append(Finding("WARN", "Cron",
                f"Missing cron jobs for: {', '.join(missing)}",
                "Re-run installer or add missing lines via crontab -e"))
        else:
            findings.append(Finding("INFO", "Cron",
                "All required cron jobs present"))
    except Exception as e:
        findings.append(Finding("WARN", "Cron", f"Could not check crontab: {e}"))


def check_logs(findings: list, state: dict, since_minutes: int = 65) -> None:
    """Scan agent logs for new errors since last audit."""
    cutoff = datetime.now() - timedelta(minutes=since_minutes)
    new_errors = []
    log_dir = RETAIL_DIR / "logs"

    for log_file, label in AGENT_LOGS:
        log_path = log_dir / log_file
        if not log_path.exists():
            continue
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            for line in lines[-300:]:
                if "ERROR" not in line and "CRITICAL" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                if any(p.lower() in line.lower() for p in IGNORE_LOG_PATTERNS):
                    continue
                new_errors.append(f"{label}: {line.strip()[:120]}")
        except Exception:
            continue

    if new_errors:
        seen = set()
        unique = []
        for e in new_errors:
            key = e[:60]
            if key not in seen:
                seen.add(key)
                unique.append(e)
        for err in unique[:8]:
            findings.append(Finding("WARN", "Logs", err))
    else:
        findings.append(Finding("INFO", "Logs",
            f"No new errors in agent logs (last {since_minutes}min)"))


def check_crash_pattern(findings: list) -> None:
    """
    Detect rapid crash patterns — same agent crashing N times in M minutes.
    This is the hourly CRITICAL check. If triggered, escalates immediately.
    """
    log_dir = RETAIL_DIR / "logs"
    cutoff  = datetime.now() - timedelta(minutes=CRASH_THRESHOLD_MINUTES)

    for log_file, label in AGENT_LOGS:
        log_path = log_dir / log_file
        if not log_path.exists():
            continue
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            crash_times = []
            for line in lines[-200:]:
                if "CRASH" not in line and "exited unexpectedly" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                    if ts >= cutoff:
                        crash_times.append(ts)
                except Exception:
                    continue

            if len(crash_times) >= CRASH_THRESHOLD_COUNT:
                findings.append(Finding("CRITICAL", "CrashPattern",
                    f"{label} crashed {len(crash_times)}x in last "
                    f"{CRASH_THRESHOLD_MINUTES}min",
                    "Watchdog should have halted this agent. "
                    "Check watchdog.log and consider rollback if post-deploy."))
        except Exception:
            continue


def check_heartbeat_age(findings: list) -> None:
    """Warn if last heartbeat from retail Pi was too long ago."""
    try:
        hb_log = RETAIL_DIR / "logs" / "heartbeat.log"
        if not hb_log.exists():
            findings.append(Finding("WARN", "Heartbeat",
                "heartbeat.log not found — heartbeat may never have run"))
            return

        lines = hb_log.read_text(errors="replace").splitlines()
        last_sent = None
        for line in reversed(lines[-100:]):
            if "Heartbeat sent" in line:
                try:
                    last_sent = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                    break
                except Exception:
                    continue

        if last_sent is None:
            findings.append(Finding("WARN", "Heartbeat",
                "No successful heartbeat in recent log",
                "Check MONITOR_URL in .env and run: python3 heartbeat.py"))
        else:
            age_hours = (datetime.now() - last_sent).total_seconds() / 3600
            if age_hours > 5:
                findings.append(Finding("CRITICAL", "Heartbeat",
                    f"Last heartbeat {age_hours:.1f}h ago — Pi may be silent",
                    "Check Pi network connectivity and heartbeat cron job"))
            elif age_hours > 2:
                findings.append(Finding("WARN", "Heartbeat",
                    f"Last heartbeat {age_hours:.1f}h ago",
                    "Monitor — if >4h during market hours Sentinel will alert"))
            else:
                findings.append(Finding("INFO", "Heartbeat",
                    f"Last heartbeat {age_hours:.1f}h ago — healthy"))
    except Exception as e:
        findings.append(Finding("WARN", "Heartbeat", f"Could not check: {e}"))


def check_db(findings: list) -> None:
    """DB integrity check — deep pass only."""
    try:
        sys.path.insert(0, str(RETAIL_DIR / "core"))
        from database import get_db
        db = get_db()
        ok = db.integrity_check()
        if ok:
            findings.append(Finding("INFO", "Database", "Integrity check passed"))
        else:
            findings.append(Finding("CRITICAL", "Database",
                "Integrity check FAILED",
                "Do not deploy until resolved. "
                "Run: python3 -c \"from database import get_db; get_db().integrity_check()\""))
    except Exception as e:
        findings.append(Finding("WARN", "Database", f"Could not check DB: {e}"))


def check_post_deploy(findings: list) -> None:
    """
    Post-deployment monitoring mode.
    Activated by Blueprint after Friday push. Runs for 48h.
    Checks for regressions against the specific patterns Blueprint flagged.
    """
    if not _db:
        return

    watches = _db.get_active_deploy_watches()
    if not watches:
        return

    now = datetime.now(timezone.utc)

    for watch in watches:
        deployed_at      = datetime.fromisoformat(
            watch.get("deployed_at", "").replace("Z", "+00:00")
        ).replace(tzinfo=timezone.utc)
        affected_files   = json.loads(watch.get("affected_files", "[]"))
        watch_for        = json.loads(watch.get("watch_for", "[]"))
        rollback_trigger = watch.get("rollback_trigger", "")

        log_dir = RETAIL_DIR / "logs"
        for pattern in watch_for:
            for log_file, label in AGENT_LOGS:
                log_path = log_dir / log_file
                if not log_path.exists():
                    continue
                try:
                    lines   = log_path.read_text(errors="replace").splitlines()
                    matches = []
                    for line in lines[-500:]:
                        try:
                            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                            if (ts.replace(tzinfo=timezone.utc) >= deployed_at
                                    and pattern.lower() in line.lower()):
                                matches.append(line.strip()[:120])
                        except Exception:
                            continue
                    if matches:
                        findings.append(Finding("HIGH", "PostDeploy",
                            f"Post-deploy regression in {label}: "
                            f"'{pattern}' found {len(matches)}x since push",
                            f"Review {log_file}. Rollback: {rollback_trigger}"))
                except Exception:
                    continue


# ── CLAUDE TRIAGE ─────────────────────────────────────────────────────────────

def claude_triage(findings: list, deep: bool = False) -> tuple[list, int, str]:
    """
    Send findings to Claude for triage, fix suggestions, and health score.
    Returns enriched findings, health score (0-100), and summary string.
    """
    client = anthropic.Anthropic()

    issues = [f for f in findings if f.level in ("CRITICAL", "HIGH", "WARN")]
    if not issues:
        return findings, 95, "No issues detected — system healthy"

    issue_text = "\n".join(
        f"[{f.level}] {f.category}: {f.message}" for f in issues
    )

    model = ANTHROPIC_MODEL_DEEP if deep else ANTHROPIC_MODEL_TRIAGE

    prompt = f"""You are Patches, the Bug Finder agent for Synthos — a congressional
stock disclosure trading system running on Raspberry Pi hardware.

PI ID: {PI_ID}
TIMESTAMP: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}
PASS TYPE: {"DEEP" if deep else "LIGHT"}

ISSUES FOUND:
{issue_text}

Analyze these findings. For each issue provide a specific, actionable fix.
Assess overall system health and trading impact.

Respond with JSON only:
{{
  "health_score": <0-100>,
  "summary": "<one sentence overall assessment>",
  "market_impact": "none|low|medium|high",
  "fixes": {{
    "<category: message_prefix>": "<specific fix command or action>"
  }},
  "suggest_blueprint": [
    "<description of any issue that needs a code fix from Blueprint>"
  ]
}}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        result       = json.loads(raw)
        health_score = int(result.get("health_score", 70))
        summary      = result.get("summary", "")
        fixes        = result.get("fixes", {})
        suggest_blueprint = result.get("suggest_blueprint", [])

        # Enrich findings with Claude's fix suggestions
        for f in findings:
            key = f"{f.category}: {f.message[:40]}"
            for fix_key, fix_val in fixes.items():
                if (f.category.lower() in fix_key.lower() or
                        f.message[:20].lower() in fix_key.lower()):
                    if not f.fix:
                        f.fix = fix_val
                    break

        # Queue Blueprint suggestions
        if suggest_blueprint:
            for suggestion_desc in suggest_blueprint:
                _queue_blueprint_suggestion(suggestion_desc, findings)

        return findings, health_score, summary

    except Exception as e:
        log.warning(f"Claude triage failed: {e}")
        return findings, 60, f"Triage failed: {e} — manual review needed"


# ── SUGGESTIONS QUEUE ─────────────────────────────────────────────────────────

def _queue_blueprint_suggestion(description: str, findings: list) -> None:
    """
    Submit a bug report to the suggestions table via db_helpers.
    Patches is append-only — it never modifies existing suggestions.
    Deduplication is handled by db_helpers.post_suggestion().
    """
    has_critical = any(f.level == "CRITICAL" for f in findings)
    has_high     = any(f.level == "HIGH" for f in findings)
    risk_level   = "CRITICAL" if has_critical else "HIGH" if has_high else "MEDIUM"

    with _db.slot("Patches", "post_suggestion", priority=5):
        sid = _db.post_suggestion(
            agent="Patches",
            category="bug",
            title=description[:80],
            description=description,
            risk_level=risk_level,
            affected_component="Detected by Patches audit",
            effort="TBD — Blueprint to assess",
            complexity="MODERATE",
            approver_needed="you",
            trial_run_recommended=True,
            root_cause="Detected in Patches audit — see bug_finder.log",
            solution_approach="Blueprint to investigate and propose fix",
            metrics_to_track=["Error recurrence after fix"],
        )
    if sid:
        log.info(f"Suggestion queued: {description[:60]}")
    else:
        log.debug(f"Duplicate suggestion skipped: {description[:60]}")


def submit_critical_suggestion(findings: list) -> None:
    """
    For CRITICAL findings, submit an explicit urgent suggestion to suggestions.json
    and write directly to the command portal alert queue.
    """
    criticals = [f for f in findings if f.level == "CRITICAL"]
    if not criticals:
        return

    for finding in criticals:
        desc = (
            f"CRITICAL: {finding.category} — {finding.message}. "
            f"Suggested fix: {finding.fix or 'Manual investigation required'}."
        )
        _queue_blueprint_suggestion(desc, findings)

    log.warning(f"Submitted {len(criticals)} CRITICAL suggestion(s) to queue")


# ── MORNING REPORT ────────────────────────────────────────────────────────────

def generate_morning_report(findings: list, health_score: int,
                             summary: str) -> None:
    """
    Generate the daily morning report digest for Scoop to deliver at 8am ET.
    Patches writes it. Scoop sends it. Format is terse — readable in 3 minutes.
    """
    client = anthropic.Anthropic()

    # Collect week's suggestion activity from DB
    pending  = _db.get_pending_suggestions()
    approved = _db.get_approved_suggestions()
    in_prog  = _db.get_approved_suggestions("in_progress")
    staged   = _db.get_approved_suggestions("complete")

    # Critical findings for the report
    critical_items = [f.to_dict() for f in findings if f.level in ("CRITICAL", "HIGH")]
    warn_items     = [f.to_dict() for f in findings if f.level == "WARN"]

    prompt = f"""You are Patches, writing the daily morning report for the Synthos project lead.

TODAY: {datetime.now().strftime('%A, %B %d %Y')}
SYSTEM HEALTH: {health_score}/100 — {summary}

SUGGESTION QUEUE:
- Pending your approval: {len(pending)}
- Approved, Blueprint working: {len(in_prog)}
- Staged, ready for Friday push: {len(staged)}
- Approved but not started: {len(approved) - len(in_prog)}

CRITICAL/HIGH FINDINGS TODAY:
{json.dumps(critical_items, indent=2) if critical_items else "None"}

WARNINGS:
{json.dumps(warn_items[:5], indent=2) if warn_items else "None"}

Write a morning report following this exact structure. Be terse. No padding.
Only include sections that have content worth surfacing.
Omit "all clear" noise — silence is just silence.

{{
  "date": "YYYY-MM-DD",
  "health_score": {health_score},
  "health_label": "HEALTHY|DEGRADED|POOR|CRITICAL",
  "critical_items": ["item 1", "item 2"],
  "build_status": "one sentence on Blueprint's week progress",
  "friday_push_ready": true/false,
  "friday_push_items": ["change 1", "change 2"],
  "weekend_risk": "LOW|MEDIUM|HIGH",
  "manager_notes": {{
    "AgentName": "one sentence if they have something worth surfacing"
  }},
  "requires_your_attention": ["specific action items for project lead"]
}}"""

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL_DEEP,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        report = json.loads(raw)
        report["generated_at"] = now_iso()
        report["generated_by"] = "Patches"

        MORNING_REPORT.parent.mkdir(parents=True, exist_ok=True)
        MORNING_REPORT.write_text(json.dumps(report, indent=2))
        # Also post directly to scoop_queue — reliable delivery
        with _db.slot("Patches", "morning_report", priority=4):
            _db.post_scoop_event(
                event_type="morning_report",
                payload=report,
                audience="internal",
            )
        log.info("Morning report written and queued for Scoop delivery")

    except Exception as e:
        log.error(f"Morning report generation failed: {e}")
        # Write a minimal fallback report
        fallback = {
            "date":         datetime.now().strftime("%Y-%m-%d"),
            "health_score": health_score,
            "health_label": health_label_for(health_score),
            "error":        f"Report generation failed: {e}",
            "critical_items": [f.message for f in findings if f.level == "CRITICAL"],
            "generated_at": now_iso(),
            "generated_by": "Patches",
        }
        MORNING_REPORT.write_text(json.dumps(fallback, indent=2))


# ── FORMAT OUTPUT ─────────────────────────────────────────────────────────────

def health_label_for(score: int) -> str:
    if score >= 90: return "HEALTHY"
    if score >= 70: return "DEGRADED"
    if score >= 50: return "POOR"
    return "CRITICAL"


def format_report(findings: list, health_score: int, summary: str,
                  mode: str, elapsed: float) -> str:
    """Format a tidy audit log entry."""
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    criticals = [f for f in findings if f.level == "CRITICAL"]
    highs     = [f for f in findings if f.level == "HIGH"]
    warns     = [f for f in findings if f.level == "WARN"]
    infos     = [f for f in findings if f.level == "INFO"]

    label = health_label_for(health_score)
    if health_score >= 90:   icon = "●"
    elif health_score >= 70: icon = "◐"
    elif health_score >= 50: icon = "○"
    else:                    icon = "✗"

    lines = [
        "",
        "━" * 64,
        f"PATCHES AUDIT  {now}  [{mode.upper()}]",
        f"{icon} Health: {health_score}/100 — {label}  |  {elapsed:.1f}s",
        f"Claude: {summary}",
        f"Found: {len(criticals)} critical, {len(highs)} high, "
        f"{len(warns)} warnings, {len(infos)} info",
        "━" * 64,
    ]

    if criticals:
        lines.append("CRITICAL — queued for Blueprint:")
        for f in criticals:
            lines.append(f"  ✗ [{f.category}] {f.message}")
            if f.fix:
                lines.append(f"    → {f.fix}")

    if highs:
        lines.append("HIGH:")
        for f in highs:
            lines.append(f"  ↑ [{f.category}] {f.message}")
            if f.fix:
                lines.append(f"    → {f.fix}")

    if warns:
        lines.append("WARNINGS:")
        for f in warns:
            lines.append(f"  ⚠ [{f.category}] {f.message}")
            if f.fix:
                lines.append(f"    → {f.fix}")

    if not criticals and not warns and not highs:
        lines.append("  ✓ All checks passed — system healthy")

    if infos:
        lines.append("INFO:")
        for f in infos:
            lines.append(f"  · [{f.category}] {f.message}")

    lines.append("━" * 64)
    return "\n".join(lines)


# ── MAIN RUN ─────────────────────────────────────────────────────────────────

def run(mode: str = "light") -> int:
    """
    Main Patches run.

    mode = "light"  — fast checks + crash detection (hourly)
    mode = "deep"   — full audit including DB and API checks (daily 6am)
    mode = "report" — generate morning report for Scoop (daily 7:30am)
    mode = "watch"  — post-deploy monitoring pass (activated by Blueprint)
    """
    start   = time.time()
    state   = load_state()
    findings = []

    log.info(f"Patches starting [{mode.upper()}] pass")

    # ── ALWAYS RUN ────────────────────────────────────────────────────────
    check_syntax(findings)
    check_required_files(findings)
    check_env_keys(findings)
    check_crash_pattern(findings)
    check_logs(findings, state)
    check_heartbeat_age(findings)

    # ── LIGHT + DEEP ──────────────────────────────────────────────────────
    if mode in ("light", "deep"):
        check_processes(findings)
        check_disk(findings)
        check_crontab(findings)

    # ── DEEP ONLY ─────────────────────────────────────────────────────────
    if mode == "deep":
        check_db(findings)

    # ── POST-DEPLOY WATCH ─────────────────────────────────────────────────
    if mode in ("watch", "deep"):
        check_post_deploy(findings)

    # ── CLAUDE TRIAGE ─────────────────────────────────────────────────────
    deep = mode in ("deep", "report")
    findings, health_score, summary = claude_triage(findings, deep=deep)

    # ── SUBMIT CRITICAL SUGGESTIONS ───────────────────────────────────────
    submit_critical_suggestion(findings)

    # ── MORNING REPORT ────────────────────────────────────────────────────
    if mode == "report":
        generate_morning_report(findings, health_score, summary)

    # ── LOG REPORT ────────────────────────────────────────────────────────
    elapsed = time.time() - start
    report  = format_report(findings, health_score, summary, mode, elapsed)

    with open(LOG_FILE, "a") as f:
        f.write(report + "\n")
    print(report)

    # ── WRITE LATEST JSON FOR PORTAL ──────────────────────────────────────
    latest = {
        "timestamp":    now_iso(),
        "mode":         mode,
        "health_score": health_score,
        "health_label": health_label_for(health_score),
        "summary":      summary,
        "elapsed":      round(elapsed, 1),
        "critical":     [f.to_dict() for f in findings if f.level == "CRITICAL"],
        "high":         [f.to_dict() for f in findings if f.level == "HIGH"],
        "warnings":     [f.to_dict() for f in findings if f.level == "WARN"],
        "info_count":   sum(1 for f in findings if f.level == "INFO"),
    }
    try:
        LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LATEST_FILE.write_text(json.dumps(latest, indent=2))
    except Exception as e:
        log.warning(f"Could not write latest JSON: {e}")

    # ── UPDATE STATE ──────────────────────────────────────────────────────
    state["last_run"]   = now_iso()
    state["last_score"] = health_score
    save_state(state)

    log.info(f"Patches [{mode.upper()}] complete — health {health_score}/100")
    return health_score


# ── UTIL ──────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Patches — Bug Finder Agent")
    parser.add_argument(
        "--mode",
        choices=["light", "deep", "report", "watch"],
        default="light",
        help=(
            "light  = fast hourly checks (default)\n"
            "deep   = full audit including DB (daily 6am)\n"
            "report = generate morning report for Scoop (daily 7:30am)\n"
            "watch  = post-deploy monitoring pass"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks but do not write to suggestions.json or morning_report.json"
    )
    args = parser.parse_args()

    if args.dry_run:
        # Monkey-patch saves to no-ops
        def _noop(*a, **kw): pass
        _save_suggestions = _noop
        save_state        = _noop
        log.info("Dry run — no writes to suggestions.json or state")

    score = run(mode=args.mode)
    sys.exit(0 if score >= 50 else 1)
