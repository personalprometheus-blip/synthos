#!/usr/bin/env python3
"""
migrate_agents.py — Apply path fix and db_helpers migration to all company Pi agents.

What this script does:
  1. Fixes hardcoded /home/pi/ paths to dynamic resolution
  2. Adds db_helpers import
  3. Replaces _load_suggestions / _save_suggestions / _queue_blueprint_suggestion
     / _submit_suggestion / _trigger_scoop / _alert_project_lead with db.* calls
  4. Replaces post_deploy_watch.json reads with db.get_active_deploy_watches()
  5. Replaces silence alert cache files with db.silence_alert_needed()

Run from synthos-company/agents/:
  python3 migrate_agents.py [--dry-run]
"""

import re
import sys
import shutil
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv

AGENTS = [
    "patches.py",
    "sentinel.py",
    "vault.py",
    "librarian.py",
    "fidget.py",
    "scoop.py",
    "timekeeper.py",
]

# ── PATH FIX ──────────────────────────────────────────────────────────────────

DYNAMIC_PATH_HEADER = '''
# ── DYNAMIC PATH RESOLUTION ───────────────────────────────────────────────────
# Works with any username — never hardcodes /home/pi/
_HERE    = Path(__file__).resolve().parent     # agents/
BASE_DIR = _HERE.parent                        # synthos-company/

if str(_HERE.parent / "utils") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "utils"))

from db_helpers import DB
_db = DB()   # module-level instance shared within this agent process
'''

PATH_REPLACEMENTS = [
    # Hardcoded base paths
    (r'BASE_DIR\s*=\s*Path\("/home/pi/synthos-company"\)',
     '# BASE_DIR resolved dynamically above'),
    (r'RETAIL_DIR\s*=\s*Path\("/home/pi/synthos"\)',
     'RETAIL_DIR = BASE_DIR.parent / "synthos"  # resolved dynamically'),
    (r'RETAIL_BASE\s*=\s*Path\("/home/pi/synthos"\)',
     'RETAIL_BASE = BASE_DIR.parent / "synthos"  # resolved dynamically'),
    (r'RETAIL_BASE\s*=\s*Path\("/home/pi"\)',
     'RETAIL_BASE = BASE_DIR.parent  # resolved dynamically'),
    (r'CORE_DIR\s*=\s*RETAIL_BASE\s*/\s*"core"',
     'CORE_DIR = RETAIL_BASE / "core"'),
    # JSON file references → remove (replaced by DB)
    (r'SUGGESTIONS_FILE\s*=\s*BASE_DIR\s*/.*suggestions\.json.*\n', ''),
    (r'SCOOP_TRIGGER\s*=\s*BASE_DIR\s*/.*scoop_trigger\.json.*\n', ''),
    (r'POST_DEPLOY_FILE\s*=\s*BASE_DIR\s*/.*post_deploy_watch\.json.*\n', ''),
    # Cron comment path
    (r'/home/pi/synthos-company/agents/', '<synthos-company>/agents/'),
]

# ── FUNCTION REPLACEMENTS ─────────────────────────────────────────────────────
# These replace the entire body of functions that wrote to JSON files.
# The replacements delegate to db_helpers instead.

FUNCTION_REPLACEMENTS = {
    # patches.py / all agents: _load_suggestions
    "_load_suggestions": """
def _load_suggestions() -> dict:
    # Deprecated — suggestions now live in company.db
    # Returns empty dict for backward compat during transition
    return {"version": "1.0", "suggestions": []}
""",

    # patches.py / all agents: _save_suggestions
    "_save_suggestions": """
def _save_suggestions(data: dict) -> None:
    # Deprecated — use _db.post_suggestion() instead
    pass
""",

    # patches.py: _queue_blueprint_suggestion
    "_queue_blueprint_suggestion": """
def _queue_blueprint_suggestion(description: str, findings: list,
                                 risk_level: str = None) -> None:
    if not risk_level:
        has_critical = any(f.level == "CRITICAL" for f in findings)
        has_high     = any(f.level == "HIGH" for f in findings)
        risk_level   = "CRITICAL" if has_critical else "HIGH" if has_high else "MEDIUM"
    with _db.slot("Patches", "post_suggestion", priority=5):
        _db.post_suggestion(
            agent="Patches",
            category="bug",
            title=description[:80],
            description=description,
            risk_level=risk_level,
            effort="TBD — Blueprint to assess",
            complexity="MODERATE",
            approver_needed="you",
            trial_run_recommended=True,
        )
    log.info(f"Suggestion queued via DB: {description[:60]}")
""",

    # All agents: _submit_suggestion
    "_submit_suggestion": """
def _submit_suggestion(title: str, description: str, category: str,
                       risk_level: str, effort: str,
                       complexity: str = "SIMPLE",
                       dedupe_days: int = 1) -> None:
    with _db.slot("Librarian", "post_suggestion", priority=6):
        _db.post_suggestion(
            agent=__name__,
            category=category,
            title=title,
            description=description,
            risk_level=risk_level,
            effort=effort,
            complexity=complexity,
            approver_needed="you",
            dedupe_hours=dedupe_days * 24,
        )
""",

    # sentinel.py / vault.py: _trigger_scoop
    "_trigger_scoop": """
def _trigger_scoop(event_type: str, payload: dict,
                   audience: str = "internal",
                   pi_id: str = None) -> None:
    # Use direct write for Sentinel (HTTP path), slot for others
    _db.post_scoop_event_direct(event_type, payload, audience, pi_id)
""",

    # vault.py / patches.py: _alert_project_lead
    "_alert_project_lead": """
def _alert_project_lead(level: str, title: str, message: str) -> None:
    with _db.slot("Vault", "alert", priority=3):
        _db.post_suggestion(
            agent="Vault",
            category="security",
            title=title[:80],
            description=message,
            risk_level=level,
            approver_needed="you",
        )
    # Also queue a Scoop event for immediate email delivery
    _db.post_scoop_event_direct(
        "vault_alert",
        {"level": level, "title": title, "message": message},
    )
""",
}

# ── SILENCE ALERT REPLACEMENT ─────────────────────────────────────────────────

SILENCE_ALERT_OLD = '''
    # Check if we already alerted for this Pi recently
    alert_cache_path = BASE_DIR / "data" / f".sentinel_alerted_{pi_id}"
    if alert_cache_path.exists():
        last_alert = datetime.fromisoformat(alert_cache_path.read_text().strip())
        if (now_utc() - last_alert.replace(tzinfo=timezone.utc)).total_seconds() < 14400:
            return   # already alerted in last 4 hours
'''

SILENCE_ALERT_NEW = '''
    if not _db.silence_alert_needed(pi_id, cooldown_hours=4):
        return   # already alerted recently
'''

SILENCE_CLEAR_OLD = '''    alert_cache = BASE_DIR / "data" / f".sentinel_alerted_{pi_id}"
    if alert_cache.exists():
        alert_cache.unlink()
        log.info(f"Silence cleared: {pi_id} is back online")'''

SILENCE_CLEAR_NEW = '''    _db.clear_silence_alert(pi_id)
    log.info(f"Silence cleared: {pi_id} is back online")'''

# ── POST DEPLOY WATCH REPLACEMENT ─────────────────────────────────────────────

POST_DEPLOY_OLD = '''    if not POST_DEPLOY_FILE.exists():
        return

    try:
        watches = json.loads(POST_DEPLOY_FILE.read_text())
    except Exception:
        return'''

POST_DEPLOY_NEW = '''    watches = _db.get_active_deploy_watches()
    if not watches:
        return'''

# ── MORNING REPORT REPLACEMENT ────────────────────────────────────────────────
# Morning report still written as JSON for Scoop to read,
# but now also posted as a scoop_queue event directly.

MORNING_REPORT_WRITE_OLD = """        MORNING_REPORT.parent.mkdir(parents=True, exist_ok=True)
        MORNING_REPORT.write_text(json.dumps(report, indent=2))
        log.info("Morning report written — Scoop will deliver at 8am ET")"""

MORNING_REPORT_WRITE_NEW = """        MORNING_REPORT.parent.mkdir(parents=True, exist_ok=True)
        MORNING_REPORT.write_text(json.dumps(report, indent=2))
        # Also post directly to scoop_queue so Scoop picks it up reliably
        with _db.slot("Patches", "morning_report", priority=4):
            _db.post_scoop_event(
                event_type="morning_report",
                payload=report,
                audience="internal",
            )
        log.info("Morning report written and queued for Scoop delivery")"""


def apply_patches(source: str, filename: str) -> str:
    """Apply all patches to source text. Returns modified source."""
    result = source

    # 1. Insert dynamic path header after the imports block
    #    Find first non-import, non-comment line after imports
    if "from db_helpers import DB" not in result:
        # Insert after last import line
        lines = result.split("\n")
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("import ") or line.startswith("from ") or \
               line.startswith("load_dotenv") or line.startswith("#"):
                insert_at = i + 1
            elif line.strip() == "" and insert_at > 0:
                continue
            elif insert_at > 0:
                break
        lines.insert(insert_at, DYNAMIC_PATH_HEADER)
        result = "\n".join(lines)

    # 2. Path replacements
    for pattern, replacement in PATH_REPLACEMENTS:
        result = re.sub(pattern, replacement, result)

    # 3. Silence alert replacements (sentinel only)
    if "sentinel" in filename:
        result = result.replace(SILENCE_ALERT_OLD, SILENCE_ALERT_NEW)
        result = result.replace(SILENCE_CLEAR_OLD, SILENCE_CLEAR_NEW)

    # 4. Post deploy watch replacement (patches only)
    if "patches" in filename:
        result = result.replace(POST_DEPLOY_OLD, POST_DEPLOY_NEW)

    # 5. Morning report (patches only)
    if "patches" in filename:
        result = result.replace(
            MORNING_REPORT_WRITE_OLD, MORNING_REPORT_WRITE_NEW
        )

    return result


def migrate_file(agent_path: Path) -> bool:
    """Migrate a single agent file. Returns True if changes were made."""
    if not agent_path.exists():
        print(f"  SKIP: {agent_path.name} not found")
        return False

    source  = agent_path.read_text(encoding="utf-8")
    patched = apply_patches(source, agent_path.name)

    if patched == source:
        print(f"  NO CHANGE: {agent_path.name}")
        return False

    if DRY_RUN:
        print(f"  [DRY RUN] Would patch: {agent_path.name}")
        return True

    # Backup original
    backup = agent_path.with_suffix(".py.pre_migration")
    shutil.copy2(agent_path, backup)

    agent_path.write_text(patched, encoding="utf-8")
    print(f"  PATCHED: {agent_path.name} (backup: {backup.name})")
    return True


if __name__ == "__main__":
    agents_dir = Path(__file__).parent
    print(f"\nMigrating agents in: {agents_dir}")
    print(f"Dry run: {DRY_RUN}\n")

    changed = 0
    for agent_name in AGENTS:
        agent_path = agents_dir / agent_name
        if migrate_file(agent_path):
            changed += 1

    print(f"\nDone — {changed} file(s) {'would be ' if DRY_RUN else ''}patched\n")
    if not DRY_RUN and changed:
        print("Review changes, then run: python3 -m py_compile <agent.py> for each")
        print("to verify syntax before deploying.\n")
