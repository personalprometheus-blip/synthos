"""
watchdog.py — Crash Monitor & Auto-Restart System
Synthos Retail Pi | /home/pi/synthos/core/watchdog.py
Version: 2.0

Runs continuously in the background via cron @reboot.
Monitors all retail Pi agents for crashes, auto-restarts up to 3 times,
then falls back to the known-good snapshot, then halts and alerts.

Alerting: Watchdog does NOT send email or SMS directly.
  All alerts are written to suggestions.json (CRITICAL) and to a
  crash_report file. Scoop on the Company Pi delivers the notification.

Rollback authority:
  Pre-trading:  Watchdog halts and alerts — project lead decides on rollback
  Post-trading: Watchdog may trigger full rollback autonomously if
                rollback_trigger condition is met in post_deploy_watch.json

Known-good snapshot:
  Taken automatically after each successful agent run (max once per 7 days).
  Used in Phase 2 recovery before going IDLE.
  Also used as the Sunday morning rollback source if Friday push breaks things.

CRON SETUP:
  @reboot sleep 90 && python3 /home/pi/synthos/core/watchdog.py &

USAGE:
  python3 watchdog.py              # start watching
  python3 watchdog.py --status     # show current agent status
  python3 watchdog.py --history    # show crash history
  python3 watchdog.py --snapshot   # take known-good snapshot now
  python3 watchdog.py --restore    # restore from known-good snapshot
  python3 watchdog.py --test-crash # simulate crash (testing only)
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
import subprocess
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────

PROJECT_DIR      = Path(__file__).parent.parent   # /home/pi/synthos
CORE_DIR         = PROJECT_DIR / "core"
LOG_DIR          = PROJECT_DIR / "logs"
DATA_DIR         = PROJECT_DIR / "data"
USER_DIR         = PROJECT_DIR / "user"
WATCHDOG_LOG     = LOG_DIR / "watchdog.log"
CRASH_REPORT_DIR = LOG_DIR / "crash_reports"
SNAPSHOT_DIR     = PROJECT_DIR / ".known_good"
SNAPSHOT_ENV     = SNAPSHOT_DIR / ".env.known_good"

# Company Pi — where suggestions.json and post_deploy_watch.json live
# Watchdog writes here to alert Patches and trigger Scoop
COMPANY_DATA_DIR     = Path("/home/pi/synthos-company/data")
SUGGESTIONS_FILE     = COMPANY_DATA_DIR / "suggestions.json"
POST_DEPLOY_FILE     = COMPANY_DATA_DIR / "post_deploy_watch.json"
TRADING_MODE_FILE    = COMPANY_DATA_DIR / "trading_mode.json"

SYNTHOS_VERSION = "2.0"

load_dotenv(USER_DIR / ".env", override=True)

PI_ID = os.environ.get("PI_ID", "retail-pi-unknown")

# Auto-restart settings
MAX_RESTARTS     = 3
MAX_RESTARTS_KG  = 3
RESTART_BACKOFF  = [30, 60, 120]   # seconds between attempts
CRASH_WINDOW     = 3600            # seconds — crash pattern window
HISTORY_HOURS    = 24

# Files included in known-good snapshot
SNAPSHOT_FILES = [
    "agent1_trader.py",
    "agent2_research.py",
    "agent3_sentiment.py",
    "database.py",
    "heartbeat.py",
    "cleanup.py",
]

# Agents Watchdog monitors
# managed=True  → long-running server, Watchdog keeps alive via process check
# managed=False → cron-managed, Watchdog monitors log for errors only
WATCHED_AGENTS = [
    {
        "name":    "Bolt",
        "alias":   "The Trader",
        "script":  "agent1_trader.py",
        "args":    [],
        "log":     "trader.log",
        "managed": False,
    },
    {
        "name":    "Scout",
        "alias":   "The Daily",
        "script":  "agent2_research.py",
        "args":    ["--session=market"],
        "log":     "daily.log",
        "managed": False,
    },
    {
        "name":    "Pulse",
        "alias":   "The Pulse",
        "script":  "agent3_sentiment.py",
        "args":    [],
        "log":     "pulse.log",
        "managed": False,
    },
    {
        "name":    "Cleanup",
        "alias":   "Cleanup",
        "script":  "cleanup.py",
        "args":    [],
        "log":     "cleanup.log",
        "managed": False,
    },
    {
        "name":    "Portal",
        "alias":   "Portal",
        "script":  "portal.py",
        "args":    [],
        "log":     "portal.log",
        "managed": True,
        "env":     {},
    },
]

IGNORE_LOG_PATTERNS = [
    "database is locked",
    "DB error — rolled back",
    "WARNING: This is a development server",
    "use a production WSGI server",
    "cold start",
    "schema verified",
]

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)
CRASH_REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s watchdog: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(WATCHDOG_LOG),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("watchdog")


# ── TRADING MODE ──────────────────────────────────────────────────────────────

def is_post_trading() -> bool:
    """
    Read the maturity gate flag from Company Pi.
    Returns True if the system is in post-trading mode.
    Defaults to pre-trading (False) if file is missing or unreadable.
    """
    try:
        if TRADING_MODE_FILE.exists():
            data = json.loads(TRADING_MODE_FILE.read_text())
            return data.get("trading_mode") == "post-trading"
    except Exception:
        pass
    return False


# ── AGENT STATE ───────────────────────────────────────────────────────────────

class AgentState:
    def __init__(self, agent_config: dict):
        self.config        = agent_config
        self.name          = agent_config["name"]
        self.alias         = agent_config.get("alias", self.name)
        self.crash_count   = 0
        self.restart_count = 0
        self.last_crash    = None
        self.last_restart  = None
        self.status        = "OK"   # OK | CRASHED | RESTARTING | FAILED | HALTED
        self.process       = None
        self.crash_history = []

    def record_crash(self, error_text: str = "", traceback_text: str = "") -> None:
        now   = datetime.now()
        event = {
            "time":            now.strftime("%Y-%m-%d %H:%M:%S"),
            "agent":           self.name,
            "error":           error_text[:500],
            "traceback":       traceback_text[:2000],
            "restart_attempt": self.restart_count,
        }
        self.crash_history.append(event)
        if len(self.crash_history) > 100:
            self.crash_history = self.crash_history[-100:]

        self.crash_count += 1
        self.last_crash   = now
        self.status       = "CRASHED"

        log.warning(
            f"CRASH #{self.crash_count}: {self.name} — "
            f"{error_text[:120] if error_text else 'Unknown error'}"
        )


agent_states = {a["name"]: AgentState(a) for a in WATCHED_AGENTS}


# ── KNOWN-GOOD SNAPSHOT ───────────────────────────────────────────────────────

def snapshot_exists() -> bool:
    return SNAPSHOT_ENV.exists()


def take_snapshot() -> bool:
    """
    Save current .env and core agent files as known-good snapshot.
    Called after a successful agent run. Max once per 7 days.
    """
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        env_path = USER_DIR / ".env"
        if env_path.exists():
            shutil.copy2(env_path, SNAPSHOT_ENV)
            SNAPSHOT_ENV.chmod(0o600)

        for fname in SNAPSHOT_FILES:
            src = CORE_DIR / fname
            dst = SNAPSHOT_DIR / fname
            if src.exists():
                shutil.copy2(src, dst)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (SNAPSHOT_DIR / "snapshot_info.json").write_text(json.dumps({
            "taken_at":       ts,
            "synthos_version": SYNTHOS_VERSION,
            "pi_id":          PI_ID,
            "files":          SNAPSHOT_FILES,
        }, indent=2))

        log.info(f"Known-good snapshot saved at {ts}")
        return True
    except Exception as e:
        log.error(f"Snapshot failed: {e}")
        return False


def restore_snapshot() -> bool:
    """
    Restore .env and core agent files from known-good snapshot.
    Called in Phase 2 recovery when current version keeps crashing.
    """
    if not snapshot_exists():
        log.error("No known-good snapshot — cannot restore")
        return False
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        env_path = USER_DIR / ".env"
        if SNAPSHOT_ENV.exists():
            if env_path.exists():
                shutil.copy2(env_path, str(env_path) + f".failed_{ts}")
            shutil.copy2(SNAPSHOT_ENV, env_path)
            env_path.chmod(0o600)
            log.info("Restored .env from known-good snapshot")

        restored = []
        for fname in SNAPSHOT_FILES:
            src = SNAPSHOT_DIR / fname
            dst = CORE_DIR / fname
            if src.exists():
                if dst.exists():
                    shutil.copy2(dst, str(dst) + f".failed_{ts}")
                shutil.copy2(src, dst)
                restored.append(fname)

        log.info(f"Restored from snapshot: {', '.join(restored)}")
        load_dotenv(USER_DIR / ".env", override=True)
        return True
    except Exception as e:
        log.error(f"Snapshot restore failed: {e}")
        return False


def get_snapshot_info() -> dict:
    info_path = SNAPSHOT_DIR / "snapshot_info.json"
    if not info_path.exists():
        return {}
    try:
        return json.loads(info_path.read_text())
    except Exception:
        return {}


def mark_env_working(agent_name: str) -> None:
    """
    Called after a successful agent run.
    Takes a fresh snapshot if none exists or if last one is >7 days old.
    """
    info = get_snapshot_info()
    should_snapshot = True

    if info.get("taken_at"):
        try:
            snap_ts  = datetime.strptime(info["taken_at"], "%Y-%m-%d %H:%M:%S")
            age_days = (datetime.now() - snap_ts).days
            if age_days < 7:
                should_snapshot = False
        except Exception:
            pass

    if should_snapshot:
        log.info(f"Taking known-good snapshot after successful run of {agent_name}")
        take_snapshot()


# ── CRASH DETECTION ───────────────────────────────────────────────────────────

def scan_log_for_crashes(
    agent_name: str,
    log_filename: str,
    since_minutes: int = 35,
    managed: bool = False,
) -> tuple[bool, str, str]:
    """
    Scan an agent's log for real crashes since `since_minutes` ago.
    Returns (crashed, error_text, traceback_text).

    Managed services (portal): only flags CRITICAL + traceback together.
    Cron agents (bolt, scout, pulse): flags any ERROR.
    """
    log_path = LOG_DIR / log_filename
    if not log_path.exists():
        return False, "", ""

    try:
        cutoff       = datetime.now() - timedelta(minutes=since_minutes)
        errors       = []
        tracebacks   = []
        in_traceback = False
        tb_lines     = []

        lines = log_path.read_text(errors="replace").splitlines()

        for line in lines[-500:]:
            try:
                ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                if ts < cutoff:
                    continue
            except (ValueError, IndexError):
                if in_traceback:
                    tb_lines.append(line.rstrip())
                continue

            line_upper = line.upper()

            if "TRACEBACK" in line_upper or "traceback (most recent" in line.lower():
                in_traceback = True
                tb_lines     = [line.rstrip()]
                continue

            if in_traceback:
                if line_upper.startswith("[20") or "INFO" in line_upper[:30]:
                    tracebacks.append("\n".join(tb_lines))
                    in_traceback = False
                    tb_lines     = []
                else:
                    tb_lines.append(line.rstrip())

            if managed:
                if ("CRITICAL" in line_upper or "FATAL" in line_upper):
                    if not any(p.lower() in line.lower() for p in IGNORE_LOG_PATTERNS):
                        errors.append(line.rstrip())
            else:
                if any(kw in line_upper for kw in ("ERROR", "FATAL", "EXCEPTION", "CRITICAL")):
                    if not any(p.lower() in line.lower() for p in IGNORE_LOG_PATTERNS):
                        errors.append(line.rstrip())

        if in_traceback and tb_lines:
            tracebacks.append("\n".join(tb_lines))

        # Managed: require errors AND traceback (avoid false positives)
        # Cron: any error is enough
        crashed = bool(errors and tracebacks) if managed else bool(errors)

        if crashed:
            return True, "\n".join(errors[-10:]), "\n\n".join(tracebacks[-3:])
        return False, "", ""

    except Exception as e:
        log.error(f"Error scanning {log_filename}: {e}")
        return False, "", ""


# ── RESTART LOGIC ─────────────────────────────────────────────────────────────

def is_process_running(script_name: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def start_managed_service(agent_cfg: dict) -> bool:
    """Start a managed service (portal) in background."""
    script   = agent_cfg["script"]
    log_file = LOG_DIR / agent_cfg["log"]
    env      = os.environ.copy()
    env.update(agent_cfg.get("env", {}))

    try:
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, str(CORE_DIR / script)] + agent_cfg.get("args", []),
                stdout=lf, stderr=lf,
                cwd=str(CORE_DIR), env=env,
            )
        time.sleep(2)
        if proc.poll() is None:
            log.info(f"Started {script} (pid={proc.pid})")
            return True
        else:
            log.error(f"Started {script} but it exited immediately")
            return False
    except Exception as e:
        log.error(f"Failed to start {script}: {e}")
        return False


def run_agent_once(state: AgentState) -> tuple[bool, str]:
    """Run a cron agent once directly. Returns (success, error_output)."""
    script = CORE_DIR / state.config["script"]
    args   = state.config.get("args", [])
    try:
        result = subprocess.run(
            [sys.executable, str(script)] + args,
            capture_output=True, text=True,
            timeout=300, cwd=str(CORE_DIR),
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "")[-500:]
    except subprocess.TimeoutExpired:
        return False, "Timed out after 300s"
    except Exception as e:
        return False, str(e)


def attempt_restart(state: AgentState) -> bool:
    """
    Full 3-phase recovery:
      Phase 1: Retry current version up to MAX_RESTARTS times
      Phase 2: Restore known-good snapshot, retry MAX_RESTARTS_KG times
      Phase 3: HALTED — alert Patches, wait for human
    """
    total    = state.restart_count
    managed  = state.config.get("managed", False)

    # Phase 1: current version
    if total < MAX_RESTARTS:
        wait = RESTART_BACKOFF[min(total, len(RESTART_BACKOFF) - 1)]
        log.info(
            f"Phase 1 — Attempt {total+1}/{MAX_RESTARTS} "
            f"current version for {state.name} (wait {wait}s)"
        )
        time.sleep(wait)
        state.status        = "RESTARTING"
        state.restart_count += 1
        state.last_restart  = datetime.now()

        success, error = (
            (start_managed_service(state.config), "")
            if managed
            else run_agent_once(state)
        )

        if success:
            log.info(f"Phase 1 restart #{state.restart_count} SUCCESS: {state.name}")
            state.status      = "OK"
            state.crash_count = 0
            mark_env_working(state.name)
            return True
        else:
            log.warning(f"Phase 1 restart #{state.restart_count} FAILED: {state.name} — {error[:120]}")
            state.record_crash(f"Phase 1 restart failed — {error[:200]}", error)
            return False

    # Phase 2: known-good snapshot
    kg_attempts = total - MAX_RESTARTS
    if kg_attempts == 0:
        if snapshot_exists():
            log.warning(f"Phase 1 exhausted for {state.name} — restoring known-good")
            if not restore_snapshot():
                log.error("Snapshot restore failed — going to Phase 3")
                state.status = "HALTED"
                return False
        else:
            log.warning(f"Phase 1 exhausted, no snapshot exists — Phase 3")
            state.status = "HALTED"
            return False

    if kg_attempts < MAX_RESTARTS_KG:
        wait = RESTART_BACKOFF[min(kg_attempts, len(RESTART_BACKOFF) - 1)]
        log.info(
            f"Phase 2 — Attempt {kg_attempts+1}/{MAX_RESTARTS_KG} "
            f"known-good for {state.name} (wait {wait}s)"
        )
        time.sleep(wait)
        state.status        = "RESTARTING"
        state.restart_count += 1
        state.last_restart  = datetime.now()

        success, error = run_agent_once(state)
        if success:
            log.info(f"Phase 2 SUCCESS: {state.name} — known-good is stable")
            state.status      = "OK"
            state.crash_count = 0
            return True
        else:
            log.warning(f"Phase 2 FAILED: {state.name} — {error[:120]}")
            state.record_crash(f"Phase 2 (known-good) failed — {error[:200]}", error)
            return False

    # Phase 3: HALTED
    log.critical(
        f"Phase 3: {state.name} exhausted {MAX_RESTARTS} current + "
        f"{MAX_RESTARTS_KG} known-good attempts — HALTED"
    )
    state.status = "HALTED"
    return False


# ── POST-DEPLOY ROLLBACK ──────────────────────────────────────────────────────

def check_post_deploy_rollback(state: AgentState) -> bool:
    """
    In post-trading mode, check if any active post-deploy watch trigger
    has been met. If so, Watchdog triggers full rollback autonomously.
    Returns True if rollback was triggered.
    """
    if not is_post_trading():
        return False
    if not POST_DEPLOY_FILE.exists():
        return False

    try:
        watches = json.loads(POST_DEPLOY_FILE.read_text())
    except Exception:
        return False

    now = datetime.now(timezone.utc)
    for watch in watches:
        try:
            deployed_at = datetime.fromisoformat(watch.get("deployed_at", ""))
            watch_hours = watch.get("watch_duration_hours", 48)
            if (now - deployed_at.replace(tzinfo=timezone.utc)).total_seconds() > watch_hours * 3600:
                continue

            trigger_condition = watch.get("rollback_trigger", "")
            # Simple trigger format: "N crashes in M hour on any Pi"
            # e.g. "3 crashes in 1 hour on any Pi"
            if not trigger_condition:
                continue

            # Parse "N crashes in M hour"
            try:
                parts   = trigger_condition.lower().split()
                n_crash = int(parts[0])
                m_hours = float(parts[3])
            except Exception:
                continue

            cutoff     = datetime.now() - timedelta(hours=m_hours)
            recent_crashes = [
                e for e in state.crash_history
                if datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S") >= cutoff
            ]

            if len(recent_crashes) >= n_crash:
                log.critical(
                    f"POST-DEPLOY ROLLBACK TRIGGERED: {state.name} hit "
                    f"trigger condition '{trigger_condition}' — "
                    f"restoring known-good (post-trading mode)"
                )
                restored = restore_snapshot()
                alert_company_pi(
                    level="CRITICAL",
                    category="PostDeployRollback",
                    message=(
                        f"Auto-rollback triggered for {PI_ID} — {state.name} "
                        f"met rollback condition: {trigger_condition}. "
                        f"Known-good {'restored' if restored else 'FAILED to restore'}."
                    ),
                    fix="Review Friday push. Check Blueprint's BLUEPRINT_NOTES.md."
                )
                return True
        except Exception:
            continue

    return False


# ── ALERT TO COMPANY PI ───────────────────────────────────────────────────────

def alert_company_pi(level: str, category: str, message: str, fix: str = "") -> None:
    """
    Write a CRITICAL suggestion to suggestions.json on the Company Pi.
    Scoop picks it up and delivers the alert email.
    Patches will see it in the next audit scan.

    This replaces the old SMS + SMTP email stack entirely.
    """
    if not COMPANY_DATA_DIR.exists():
        log.warning(f"Company Pi data dir not reachable — logging alert locally only")
        log.critical(f"ALERT [{level}] {category}: {message}")
        return

    try:
        if SUGGESTIONS_FILE.exists():
            data = json.loads(SUGGESTIONS_FILE.read_text())
        else:
            data = {"version": "1.0", "suggestions": []}

        # Deduplicate by message prefix
        prefix = message[:60]
        existing = {
            s.get("description", "")[:60]
            for s in data.get("suggestions", [])
            if s.get("status") in ("pending", "approved")
        }
        if prefix in existing:
            log.info(f"Duplicate alert suppressed: {prefix}")
            return

        suggestion = {
            "id":        str(uuid.uuid4()),
            "timestamp": now_iso(),
            "agent":     "Watchdog",
            "category":  "bug",
            "title":     f"{level}: {category} — {PI_ID}",
            "description": message,
            "impact": {
                "tokens_saved_per_week":    None,
                "execution_time_saved":     None,
                "risk_level":               level,
                "affected_component":       f"Retail Pi — {PI_ID}",
                "affected_customers_count": 1,
                "estimated_improvement":    "Resolves active crash/halt condition",
            },
            "implementation": {
                "effort":                "Emergency — immediate investigation",
                "complexity":           "MODERATE",
                "approver_needed":      "you",
                "trial_run_recommended": False,
                "breaking_changes":     False,
                "rollback_difficulty":  "EASY",
            },
            "details": {
                "root_cause":           f"Watchdog halt on {PI_ID} — see crash report in logs/crash_reports/",
                "solution_approach":    fix or "Investigate crash logs and restart manually",
                "alternative_approaches": [],
                "dependencies":         [],
                "metrics_to_track":     ["Agent stability after fix"],
            },
            "status":            "pending",
            "status_updated_at": now_iso(),
            "approver_notes":    None,
            "implementation_status": None,
            "implementation_notes":  None,
        }

        data["suggestions"].append(suggestion)
        data["last_updated"] = now_iso()

        # Backup before write
        if SUGGESTIONS_FILE.exists():
            shutil.copy2(SUGGESTIONS_FILE, SUGGESTIONS_FILE.with_suffix(".json.backup"))
        SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2))

        log.info(f"Alert written to suggestions.json — Scoop will deliver to project lead")

    except Exception as e:
        log.error(f"Failed to write alert to Company Pi: {e}")
        log.critical(f"UNDELIVERED ALERT [{level}] {category}: {message}")


# ── CRASH REPORT ──────────────────────────────────────────────────────────────

def collect_24h_crash_history() -> list:
    cutoff  = datetime.now() - timedelta(hours=HISTORY_HOURS)
    history = []
    for state in agent_states.values():
        for event in state.crash_history:
            try:
                ts = datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    history.append(event)
            except Exception:
                history.append(event)
    history.sort(key=lambda e: e["time"])
    return history


def collect_system_snapshot() -> dict:
    """Collect system state at time of crash for the crash report."""
    snapshot = {}

    try:
        import sqlite3
        db_path = DATA_DIR / "signals.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path), timeout=5)
            snapshot["db_size_mb"]     = round(db_path.stat().st_size / 1024 / 1024, 2)
            snapshot["open_positions"] = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='OPEN'"
            ).fetchone()[0]
            snapshot["queued_signals"] = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status='QUEUED'"
            ).fetchone()[0]
            cash = conn.execute(
                "SELECT cash FROM portfolio ORDER BY id DESC LIMIT 1"
            ).fetchone()
            snapshot["portfolio_cash"] = round(cash[0], 2) if cash else 0
            snapshot["db_integrity"]   = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            conn.close()
    except Exception as e:
        snapshot["db_error"] = str(e)

    try:
        stat = os.statvfs(str(PROJECT_DIR))
        snapshot["disk_free_gb"] = round((stat.f_bavail * stat.f_frsize) / (1024 ** 3), 2)
    except Exception:
        pass

    try:
        uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
        hours = int(uptime_secs // 3600)
        mins  = int((uptime_secs % 3600) // 60)
        snapshot["pi_uptime"] = f"{hours}h {mins}m"
    except Exception:
        snapshot["pi_uptime"] = "unknown"

    snapshot["trading_mode"] = "post-trading" if is_post_trading() else "pre-trading"
    return snapshot


def generate_crash_report(state: AgentState, trigger_event: dict) -> Path:
    """
    Generate a structured crash report file.
    Scoop reads this and formats the alert email.
    """
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = CRASH_REPORT_DIR / f"crash_{ts}_{state.name}.txt"

    history  = collect_24h_crash_history()
    snapshot = collect_system_snapshot()

    history_text = ""
    for evt in history:
        history_text += f"\n  [{evt['time']}] {evt['agent']} — {evt['error'][:120]}\n"
        if evt.get("traceback"):
            for line in evt["traceback"].split("\n")[:8]:
                history_text += f"    {line}\n"

    log_excerpt = "(log not found)"
    log_file    = LOG_DIR / state.config.get("log", "")
    if log_file.exists():
        try:
            lines       = log_file.read_text(errors="replace").splitlines()
            log_excerpt = "\n".join(lines[-80:])
        except Exception:
            pass

    watchdog_tail = "(watchdog log not found)"
    if WATCHDOG_LOG.exists():
        try:
            wdlines       = WATCHDOG_LOG.read_text(errors="replace").splitlines()
            watchdog_tail = "\n".join(wdlines[-50:])
        except Exception:
            pass

    report = f"""================================================================
SYNTHOS CRASH REPORT — v{SYNTHOS_VERSION}
================================================================
Generated:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}
Pi ID:        {PI_ID}
Agent:        {state.name} ({state.alias})
Crash count:  {state.crash_count} (this session)
Restarts:     {state.restart_count} of {MAX_RESTARTS} attempted
Final status: {state.status}
Trading mode: {snapshot.get('trading_mode', 'unknown')}

================================================================
TRIGGER EVENT
================================================================
Time:  {trigger_event.get('time', 'unknown')}
Error: {trigger_event.get('error', 'unknown')}

Traceback:
{trigger_event.get('traceback', '(no traceback captured)') or '(no traceback captured)'}

================================================================
SYSTEM SNAPSHOT AT TIME OF CRASH
================================================================
Portfolio cash:    ${snapshot.get('portfolio_cash', 'unknown')}
Open positions:    {snapshot.get('open_positions', 'unknown')}
Queued signals:    {snapshot.get('queued_signals', 'unknown')}
Database size:     {snapshot.get('db_size_mb', 'unknown')} MB
Database integrity:{snapshot.get('db_integrity', 'unknown')}
Disk free:         {snapshot.get('disk_free_gb', 'unknown')} GB
Pi uptime:         {snapshot.get('pi_uptime', 'unknown')}
{f"DB error: {snapshot.get('db_error')}" if snapshot.get('db_error') else ""}

================================================================
CRASH HISTORY — LAST {HISTORY_HOURS} HOURS
================================================================
{history_text or '  No prior crash events recorded.'}

================================================================
RECENT LOG — {state.name}
================================================================
{log_excerpt}

================================================================
WATCHDOG LOG — LAST 50 LINES
================================================================
{watchdog_tail}
================================================================
END OF REPORT
================================================================
"""

    report_path.write_text(report)
    log.info(f"Crash report saved: {report_path.name}")
    return report_path


def handle_failed_agent(state: AgentState) -> None:
    """Called when agent has exhausted all restart attempts."""
    log.critical(
        f"AGENT HALTED: {state.name} crashed {state.crash_count}x, "
        f"exhausted all {MAX_RESTARTS + MAX_RESTARTS_KG} restart attempts"
    )

    trigger = state.crash_history[-1] if state.crash_history else {
        "time":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error":     "Unknown — no crash event recorded",
        "traceback": "",
    }

    report_path = generate_crash_report(state, trigger)

    alert_company_pi(
        level="CRITICAL",
        category="AgentHalted",
        message=(
            f"{state.name} ({state.alias}) on {PI_ID} has HALTED after "
            f"{state.crash_count} crashes and {state.restart_count} restart attempts. "
            f"Phase 2 known-good restore {'was attempted' if snapshot_exists() else 'was NOT possible (no snapshot)'}. "
            f"Manual intervention required before market open. "
            f"Crash report: logs/crash_reports/{report_path.name}"
        ),
        fix=(
            "SSH to Pi, check crash report, restart manually. "
            "If post-trading: consider full rollback via git revert on update-staging."
        )
    )


# ── MAIN WATCH LOOP ───────────────────────────────────────────────────────────

def watch_loop() -> None:
    """
    Main monitoring loop — scans every 5 minutes.

    Managed services (Portal): checks process is alive, restarts if not.
    Cron agents (Bolt, Scout, Pulse): scans logs for errors, restarts on crash.
    Post-deploy: checks rollback trigger conditions in post-trading mode.
    """
    log.info(
        f"Watchdog v{SYNTHOS_VERSION} started on {PI_ID} — "
        f"monitoring {len(WATCHED_AGENTS)} agents | "
        f"mode: {'post-trading' if is_post_trading() else 'pre-trading'}"
    )

    alerted = set()

    while True:
        try:
            for agent_cfg in WATCHED_AGENTS:
                name    = agent_cfg["name"]
                state   = agent_states[name]
                managed = agent_cfg.get("managed", False)

                # Skip permanently halted agents that have already been alerted
                if state.status == "HALTED" and name in alerted:
                    continue

                if managed:
                    running = is_process_running(agent_cfg["script"])
                    if not running:
                        log.warning(f"{name} not running — starting now")
                        started = start_managed_service(agent_cfg)
                        if started:
                            state.status = "OK"
                        else:
                            state.record_crash(f"{name} not running and failed to restart")
                    else:
                        if state.status != "OK":
                            state.status = "OK"
                        mark_env_working(name)
                    continue

                crashed, error_text, traceback_text = scan_log_for_crashes(
                    name,
                    agent_cfg["log"],
                    since_minutes=35,
                    managed=False,
                )

                if not crashed:
                    if state.status not in ("OK", "IDLE"):
                        log.info(f"Recovery: {name} appears stable")
                        state.status      = "OK"
                        state.crash_count = 0
                    mark_env_working(name)
                    continue

                # Crash detected
                state.record_crash(error_text, traceback_text)

                # Post-trading: check rollback trigger before attempting restart
                if check_post_deploy_rollback(state):
                    alerted.add(name)
                    continue

                if state.restart_count < MAX_RESTARTS + MAX_RESTARTS_KG:
                    success = attempt_restart(state)
                    if success:
                        log.info(f"Restarted {name} successfully")
                    else:
                        log.warning(f"Restart attempt failed for {name}")
                else:
                    if name not in alerted:
                        handle_failed_agent(state)
                        alerted.add(name)

        except Exception as e:
            log.error(f"Watchdog loop error: {e}", exc_info=True)

        time.sleep(300)   # scan every 5 minutes


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status() -> None:
    print(f"\n{'=' * 60}")
    print(f"SYNTHOS WATCHDOG STATUS — v{SYNTHOS_VERSION} | {PI_ID}")
    print(f"Trading mode: {'post-trading' if is_post_trading() else 'pre-trading'}")
    print(f"{'=' * 60}")
    for name, state in agent_states.items():
        icon = {
            "OK": "✓", "CRASHED": "✗", "RESTARTING": "↺",
            "FAILED": "⛔", "HALTED": "⛔", "IDLE": "—"
        }.get(state.status, "?")
        phase = "P2" if state.restart_count > MAX_RESTARTS else "P1"
        print(
            f"  {icon} {name:20} {state.status:12} "
            f"crashes={state.crash_count} restarts={state.restart_count} ({phase})"
        )
    print(f"{'=' * 60}")

    info = get_snapshot_info()
    if info:
        print(f"  Known-good snapshot: {info.get('taken_at', 'unknown')}")
        snap_count = sum(1 for f in SNAPSHOT_FILES if (SNAPSHOT_DIR / f).exists())
        print(f"  .env backup: {'✓' if SNAPSHOT_ENV.exists() else '✗'}  |  "
              f"Agent files: {snap_count}/{len(SNAPSHOT_FILES)}")
    else:
        print("  Known-good snapshot: NOT YET TAKEN")
        print("  Will be taken after first successful agent run")
    print(f"{'=' * 60}\n")


def show_history() -> None:
    if not WATCHDOG_LOG.exists():
        print("No watchdog log found")
        return
    print(f"\n{'=' * 60}\nSYNTHOS WATCHDOG HISTORY\n{'=' * 60}")
    lines       = WATCHDOG_LOG.read_text(errors="replace").splitlines()
    crash_lines = [
        l for l in lines
        if any(kw in l for kw in ("CRASH", "FAILED", "HALTED", "RESTART", "Recovery"))
    ]
    for line in crash_lines[-50:]:
        print(f"  {line}")
    print(f"{'=' * 60}\n")


def test_crash() -> None:
    """Simulate a crash to test the alert pipeline."""
    log.info("TEST: Simulating crash for Scout")
    state = agent_states["Scout"]
    state.record_crash(
        error_text="TEST CRASH — simulated by --test-crash flag",
        traceback_text=(
            "Traceback (most recent call last):\n"
            "  File 'agent2_research.py', line 999, in test\n"
            "    raise ValueError('simulated crash')\n"
            "ValueError: simulated crash"
        ),
    )
    state.restart_count = MAX_RESTARTS
    state.status        = "HALTED"

    trigger     = state.crash_history[-1]
    report_path = generate_crash_report(state, trigger)
    log.info(f"Test crash report: {report_path}")

    alert_company_pi(
        level="CRITICAL",
        category="TestCrash",
        message=f"TEST ALERT — Watchdog test crash simulation on {PI_ID} for Scout",
        fix="This is a test. No action required."
    )

    print(f"\nTest complete:")
    print(f"  Report:  {report_path}")
    print(f"  Alert:   written to suggestions.json on Company Pi")


# ── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

def handle_shutdown(signum, frame):
    log.info(f"Watchdog received signal {signum} — shutting down cleanly")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)


# ── UTIL ──────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Synthos Watchdog — crash monitoring and auto-restart"
    )
    parser.add_argument("--status",       action="store_true", help="Show agent status")
    parser.add_argument("--history",      action="store_true", help="Show crash history")
    parser.add_argument("--test-crash",   action="store_true", help="Simulate crash (testing only)")
    parser.add_argument("--snapshot",     action="store_true", help="Take known-good snapshot now")
    parser.add_argument("--restore",      action="store_true", help="Restore from known-good snapshot")
    parser.add_argument("--snapshot-info",action="store_true", help="Show snapshot details")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.history:
        show_history()
    elif args.test_crash:
        test_crash()
    elif args.snapshot:
        ok = take_snapshot()
        print(f"Snapshot {'saved' if ok else 'FAILED'}")
    elif args.restore:
        confirm = input(
            "Restore from known-good snapshot? "
            "This will overwrite current files. (yes/no): "
        )
        if confirm.strip().lower() == "yes":
            ok = restore_snapshot()
            print(f"Restore {'complete' if ok else 'FAILED'}")
        else:
            print("Restore cancelled")
    elif args.snapshot_info:
        info = get_snapshot_info()
        if info:
            print("\nKnown-good snapshot:")
            for k, v in info.items():
                print(f"  {k}: {v}")
        else:
            print("No snapshot found")
    else:
        watch_loop()
