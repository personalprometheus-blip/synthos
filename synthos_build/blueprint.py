"""
blueprint.py — Blueprint > Engineer Agent
Synthos Company Pi | /home/<user>/synthos-company/agents/blueprint.py

Role:
  Implement approved suggestions. Accountable for every line of code shipped.
  Blueprint does not rubber-stamp — it triages, self-reviews, flags edge cases,
  and files its own disagreements on the record when something looks wrong.

  Blueprint is accountable for:
    - Implementing only what the project lead has approved
    - The atomic deploy pattern (staging → validate → rename)
    - The truncation guard (Claude can return partial files)
    - Honest risk assessment in BLUEPRINT_NOTES.md
    - Notifying Patches to monitor post-deploy
    - Never touching /user/, .env, or any live database

Trigger conditions:
  Scheduled: Mon–Thu 8pm ET via Timekeeper (build window)
  Event:     CRITICAL bug from Patches, CVE from Librarian, manual trigger
  Friday:    BLACKOUT — push day, no new Blueprint work
  Weekend:   Standby for hot-fix if regression detected

Git workflow:
  Feature branch per suggestion: blueprint/<suggestion-id[:8]>
  Merges to update-staging after self-review passes
  Never merges to main — project lead approves Friday push

Pre/post trading modes:
  Pre-trading:  up to 5 suggestions per weekly cycle, lighter review
  Post-trading: capped at 3 retail Pi changes, Patches sign-off required

USAGE:
  python3 blueprint.py --mode scheduled     # nightly build window run
  python3 blueprint.py --mode event         # event-triggered (CRITICAL)
  python3 blueprint.py --dry-run            # show queue without executing
  python3 blueprint.py --status             # show current week's progress
"""

import os
import sys
import ast
import json
import shutil
import logging
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ── DYNAMIC PATH RESOLUTION ───────────────────────────────────────────────────

_HERE    = Path(__file__).resolve().parent     # agents/
BASE_DIR = _HERE.parent                        # synthos-company/
_UTILS   = BASE_DIR / "utils"

if str(_UTILS) not in sys.path:
    sys.path.insert(0, str(_UTILS))

from db_helpers import DB

load_dotenv(BASE_DIR / ".env", override=True)

# Retail Pi codebase — dynamic, not /home/pi/
# Blueprint reads the RETAIL_CODEBASE_DIR env var set at install time
RETAIL_DIR   = Path(os.environ.get(
    "RETAIL_CODEBASE_DIR",
    BASE_DIR.parent / "synthos"
))
COMPANY_DIR  = BASE_DIR
OUTPUT_DIR   = BASE_DIR / "data" / "pending-changes"
STAGING_DIR  = BASE_DIR / "data" / ".blueprint_staging"
LOG_FILE     = BASE_DIR / "logs" / "engineer.log"

ET = ZoneInfo("America/New_York")

# Trading mode gate
TRADING_MODE_FILE = BASE_DIR / "data" / "trading_mode.json"

# Model selection — scale to risk
MODEL_CAPABLE = "claude-sonnet-4-6"   # CRITICAL/HIGH
MODEL_FAST    = "claude-haiku-4-5-20251001"    # MEDIUM/LOW

# Allowed read/write paths
READABLE_DIRS = [
    RETAIL_DIR  / "core",
    COMPANY_DIR / "agents",
    COMPANY_DIR / "utils",
]
PROTECTED_PATHS = [
    RETAIL_DIR / "user",
    RETAIL_DIR / "data",
]

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s blueprint: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("blueprint")

# ── MODE DETECTION ────────────────────────────────────────────────────────────

def is_post_trading() -> bool:
    try:
        if TRADING_MODE_FILE.exists():
            return json.loads(TRADING_MODE_FILE.read_text()).get(
                "trading_mode") == "post-trading"
    except Exception:
        pass
    return False


def is_friday_blackout() -> bool:
    now = datetime.now(ET)
    return now.weekday() == 4 and now.hour >= 16


def is_build_window() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 4 and 20 <= now.hour < 24


def weekly_cap() -> int:
    """Max suggestions per weekly cycle based on trading mode."""
    return 3 if is_post_trading() else 5


# ── FILE SAFETY ───────────────────────────────────────────────────────────────

def is_readable(path: Path) -> bool:
    resolved = path.resolve()
    for protected in PROTECTED_PATHS:
        if resolved.is_relative_to(protected.resolve()):
            return False
    for allowed in READABLE_DIRS:
        if resolved.is_relative_to(allowed.resolve()):
            return True
    return False


def is_protected(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        resolved.is_relative_to(p.resolve()) for p in PROTECTED_PATHS
    )


def read_source_file(rel_path: str) -> str | None:
    for base in [RETAIL_DIR, COMPANY_DIR]:
        candidate = (base / rel_path).resolve()
        if candidate.exists() and is_readable(candidate):
            return candidate.read_text(encoding="utf-8", errors="replace")
    log.warning(f"Cannot read: {rel_path}")
    return None


COMPONENT_MAP = {
    "Bolt":            ["core/agent1_trader.py"],
    "Trader agent":    ["core/agent1_trader.py"],
    "Scout":           ["core/agent2_research.py"],
    "Research agent":  ["core/agent2_research.py"],
    "Pulse":           ["core/agent3_sentiment.py"],
    "Sentiment agent": ["core/agent3_sentiment.py"],
    "Patches":         ["agents/patches.py"],
    "Blueprint":       ["agents/blueprint.py"],
    "Sentinel":        ["agents/sentinel.py"],
    "Vault":           ["agents/vault.py"],
    "Librarian":       ["agents/librarian.py"],
    "Fidget":          ["agents/fidget.py"],
    "Scoop":           ["agents/scoop.py"],
    "Timekeeper":      ["agents/timekeeper.py"],
    "Watchdog":        ["core/watchdog.py"],
    "All Pis":         ["core/agent1_trader.py", "core/agent2_research.py",
                        "core/agent3_sentiment.py"],
}

ALWAYS_INCLUDE = [
    "utils/db_helpers.py",
    "core/license_validator.py",
]


def collect_relevant_files(suggestion: dict) -> dict[str, str]:
    component    = suggestion.get("affected_component", "") or \
                   suggestion.get("impact", {}).get("affected_component", "")
    target_files = []

    for key, files in COMPONENT_MAP.items():
        if key.lower() in component.lower():
            target_files.extend(files)
            break

    target_files.extend(ALWAYS_INCLUDE)

    result = {}
    for rel_path in dict.fromkeys(target_files):   # deduplicate, preserve order
        content = read_source_file(rel_path)
        if content:
            result[rel_path] = content

    return result


def max_tokens_for_file(file_lines: int) -> int:
    """Scale max_tokens to file size — avoid truncation, avoid waste."""
    return min(16000, max(2000, file_lines * 8))


# ── BLUEPRINT SYSTEM PROMPT ───────────────────────────────────────────────────

BLUEPRINT_SYSTEM_PROMPT = """
You are Blueprint, the Engineer agent for Synthos — a congressional stock
disclosure trading system running on Raspberry Pi hardware.

Your job: Implement an approved suggestion. Be precise, minimal, and honest.

CONSTRAINTS:
- NEVER modify /user/, .env, or any database file
- NEVER add external dependencies without flagging them explicitly
- NEVER touch trading logic, position sizing, or financial calculations
- Make ONLY the specific change described — nothing broader
- Python 3.9+ compatibility (Pi OS Lite)
- Memory-conscious: Pi 2W has 512MB shared across all agents
- Return the COMPLETE file — every line, no placeholders, no truncation

OUTPUT FORMAT — JSON only, nothing else:
{
  "summary": "One sentence: what changed and why",
  "risk_assessment": "What could go wrong and how it is mitigated",
  "confidence": "HIGH|MEDIUM|LOW",
  "files_changed": [
    {
      "path": "core/agent1_trader.py",
      "change_type": "modify|create|delete",
      "description": "Specific change and reasoning",
      "full_content": "... complete file ..."
    }
  ],
  "new_dependencies": [],
  "test_steps": ["Step 1", "Step 2"],
  "rollback_instructions": "How to revert",
  "blueprint_notes": "Honest concerns, edge cases not resolved, what to watch"
}

Be honest. If a suggestion has edge cases you cannot fully resolve, say so
in blueprint_notes. You are accountable for this code. Your track record
is visible to the project lead.
""".strip()


def call_claude(suggestion: dict, source_files: dict[str, str]) -> dict | None:
    import urllib.request

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    risk = suggestion.get("impact", {}).get("risk_level",
           suggestion.get("risk_level", "LOW"))
    model = MODEL_CAPABLE if risk in ("CRITICAL", "HIGH") else MODEL_FAST

    # Scale tokens to largest file being modified
    max_lines  = max(
        (len(c.splitlines()) for c in source_files.values()), default=200
    )
    max_tokens = max_tokens_for_file(max_lines)

    files_block = ""
    for path, content in source_files.items():
        files_block += f"\n\n--- FILE: {path} ---\n```python\n{content}\n```"

    user_msg = (
        f"APPROVED SUGGESTION:\n{json.dumps(suggestion, indent=2)}\n\n"
        f"RELEVANT SOURCE FILES:{files_block or '(none found)'}\n\n"
        f"Implement this suggestion. Return only the JSON object."
    )

    body = json.dumps({
        "model":      model,
        "max_tokens": max_tokens,
        "system":     BLUEPRINT_SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": user_msg}],
    }).encode()

    log.info(
        f"Calling Claude ({model}) for {suggestion['id'][:8]} "
        f"— max_tokens={max_tokens}"
    )

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            data = json.loads(resp.read())
        raw = data["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude call failed: {e}")
        return None


# ── SELF-REVIEW ───────────────────────────────────────────────────────────────

def self_review(implementation: dict, original_files: dict[str, str]) -> list[str]:
    """
    Blueprint's own quality gate. Returns list of failure reasons.
    Empty list = pass.
    """
    failures = []

    for file_change in implementation.get("files_changed", []):
        path    = file_change.get("path", "")
        content = file_change.get("full_content", "")

        # Protected path check
        candidate = (RETAIL_DIR / path).resolve()
        if is_protected(Path(path)):
            failures.append(f"PROTECTED PATH: {path}")
            continue

        # Syntax check
        if path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as e:
                failures.append(f"SYNTAX ERROR in {path}: {e}")
                continue

        # Truncation guard — staged must be ≥60% of original
        original = original_files.get(path, "")
        if original and len(content) < len(original) * 0.60:
            failures.append(
                f"TRUNCATION GUARD: {path} is {len(content)} bytes, "
                f"original was {len(original)} bytes (<60%)"
            )

    # Confidence check
    if implementation.get("confidence") == "LOW":
        failures.append(
            "LOW CONFIDENCE: Blueprint flagged low confidence. "
            "Review blueprint_notes before proceeding."
        )

    return failures


# ── ATOMIC DEPLOY (staging area) ─────────────────────────────────────────────

def stage_files(suggestion_id: str,
                implementation: dict,
                original_files: dict[str, str]) -> Path:
    """
    Write generated content to staging area.
    Validates each file before touching anything in the codebase.
    Returns staging directory path.
    Raises on any validation failure — live files are never touched.
    """
    stage_dir = STAGING_DIR / suggestion_id[:8]
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Write manifest
    (stage_dir / "manifest.json").write_text(json.dumps({
        "suggestion_id": suggestion_id,
        "staged_at":     _now_iso(),
        "files":         [f["path"] for f in implementation.get("files_changed", [])],
    }, indent=2))

    for file_change in implementation.get("files_changed", []):
        path    = file_change["path"]
        content = file_change.get("full_content", "")

        staged = stage_dir / path.replace("/", "__")

        # Write to staging
        staged.write_text(content, encoding="utf-8")

        # Verify write
        if staged.read_text(encoding="utf-8") != content:
            staged.unlink(missing_ok=True)
            raise IOError(f"Staging write verification failed for {path}")

        # Backup original
        backup = stage_dir / (path.replace("/", "__") + ".original")
        original = original_files.get(path)
        if original:
            backup.write_text(original, encoding="utf-8")

    return stage_dir


def cleanup_staging(suggestion_id: str) -> None:
    """Remove staging artifacts after successful packaging or on failure."""
    stage_dir = STAGING_DIR / suggestion_id[:8]
    if stage_dir.exists():
        shutil.rmtree(stage_dir)


# ── PACKAGE FOR REVIEW ────────────────────────────────────────────────────────

def package_for_review(suggestion: dict, implementation: dict,
                        original_files: dict[str, str]) -> Path:
    """
    Produce the review package that appears in the Command Portal
    Pending Changes tab. Returns the package directory.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug      = suggestion["id"][:8]
    pkg_dir   = OUTPUT_DIR / f"{ts}_{slug}"
    pkg_dir.mkdir()

    # Changelog JSON
    changelog = {
        "generated_at":      _now_iso(),
        "agent":             "Blueprint",
        "suggestion_id":     suggestion["id"],
        "suggestion_title":  suggestion["title"],
        "category":          suggestion.get("category"),
        "approver_notes":    suggestion.get("approver_notes"),
        "summary":           implementation.get("summary"),
        "risk_assessment":   implementation.get("risk_assessment"),
        "confidence":        implementation.get("confidence"),
        "new_dependencies":  implementation.get("new_dependencies", []),
        "test_steps":        implementation.get("test_steps", []),
        "rollback_instructions": implementation.get("rollback_instructions"),
        "files_changed": [
            {
                "path":        f["path"],
                "change_type": f["change_type"],
                "description": f["description"],
            }
            for f in implementation.get("files_changed", [])
        ],
        "trading_mode":  "post-trading" if is_post_trading() else "pre-trading",
    }
    (pkg_dir / "CHANGELOG.json").write_text(json.dumps(changelog, indent=2))

    # BLUEPRINT_NOTES.md — Blueprint's direct communication to project lead
    notes_lines = [
        "# BLUEPRINT NOTES",
        "",
        f"**Suggestion:** {suggestion['title']}",
        f"**Confidence:** {implementation.get('confidence', 'unknown')}",
        f"**Risk:** {implementation.get('risk_assessment', 'not assessed')}",
        "",
        "## What Changed",
        implementation.get("summary", "No summary provided."),
        "",
        "## Edge Cases / Concerns",
        implementation.get("blueprint_notes", "None flagged."),
        "",
        "## Test Steps",
    ]
    for step in implementation.get("test_steps", ["No test steps provided."]):
        notes_lines.append(f"- {step}")
    notes_lines += [
        "",
        "## Rollback",
        implementation.get("rollback_instructions", "Revert commit via git."),
        "",
        "---",
        f"*Generated by Blueprint at {_now_iso()}*",
    ]
    (pkg_dir / "BLUEPRINT_NOTES.md").write_text("\n".join(notes_lines))

    # File contents
    files_dir = pkg_dir / "files"
    pre_dir   = files_dir / "pre-change"
    post_dir  = files_dir / "post-change"
    pre_dir.mkdir(parents=True)
    post_dir.mkdir(parents=True)

    for file_change in implementation.get("files_changed", []):
        flat_name = file_change["path"].replace("/", "__")

        # Pre-change snapshot (for hard rollback)
        original = original_files.get(file_change["path"])
        if original:
            (pre_dir / flat_name).write_text(original, encoding="utf-8")

        # Post-change (new content)
        content = file_change.get("full_content", "")
        (post_dir / flat_name).write_text(content, encoding="utf-8")

    # Zip everything
    zip_base = OUTPUT_DIR / f"{ts}_{slug}_changes"
    shutil.make_archive(str(zip_base), "zip", pkg_dir)

    log.info(f"Package ready: {pkg_dir.name}")
    return pkg_dir


# ── GIT WORKFLOW ──────────────────────────────────────────────────────────────

def create_feature_branch(suggestion_id: str) -> str | None:
    branch = f"blueprint/{suggestion_id[:8]}"
    try:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=str(RETAIL_DIR), check=True,
            capture_output=True, text=True, timeout=30
        )
        log.info(f"Branch created: {branch}")
        return branch
    except subprocess.CalledProcessError as e:
        log.warning(f"Could not create branch: {e.stderr.strip()}")
        return None


def commit_to_staging(suggestion: dict,
                      implementation: dict,
                      branch: str) -> bool:
    """
    Write generated files to codebase and commit to update-staging.
    Only called after package_for_review — project lead must still
    approve before merge to main.
    """
    try:
        changed_files = []
        for file_change in implementation.get("files_changed", []):
            path    = file_change["path"]
            content = file_change.get("full_content", "")

            # Determine target directory
            if path.startswith("core/") or path.startswith("agents/") or \
               path.startswith("utils/"):
                target = RETAIL_DIR / path if path.startswith("core/") \
                         else COMPANY_DIR / path
            else:
                log.warning(f"Unknown path prefix for {path} — skipping commit")
                continue

            if is_protected(target):
                log.error(f"PROTECTED PATH blocked at commit stage: {path}")
                continue

            # Atomic write: tmp → rename
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            changed_files.append(str(target.relative_to(RETAIL_DIR)
                                  if target.is_relative_to(RETAIL_DIR)
                                  else target))

        if not changed_files:
            return False

        # Git add + commit
        subprocess.run(
            ["git", "add"] + changed_files,
            cwd=str(RETAIL_DIR), check=True,
            capture_output=True, timeout=30
        )
        commit_msg = (
            f"[Blueprint] {suggestion['title'][:60]}\n\n"
            f"Implements: {suggestion['id']}\n"
            f"Risk: {suggestion.get('impact',{}).get('risk_level','?')}\n"
            f"Mode: {'post-trading' if is_post_trading() else 'pre-trading'}\n\n"
            f"{implementation.get('summary','')}"
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(RETAIL_DIR), check=True,
            capture_output=True, timeout=30
        )
        log.info(f"Committed to {branch}")
        return True

    except Exception as e:
        log.error(f"Commit failed: {e}")
        return False


# ── NOTIFICATION TO PATCHES ───────────────────────────────────────────────────

def notify_patches(db: DB, suggestion_id: str,
                   affected_files: list, risk_level: str) -> None:
    """Post a deploy watch for Patches to monitor post-Friday-push."""
    watch_hours = 48 if is_post_trading() else 24
    rollback_trigger = "3 crashes in 1 hour on any Pi" if is_post_trading() else None

    with db.slot("Blueprint", "post_deploy_notify", priority=4):
        db.post_deploy_watch(
            suggestion_id=suggestion_id,
            deployed_at=_now_iso(),
            affected_files=affected_files,
            watch_for=["ERROR", "CRITICAL", "crash", "traceback"],
            rollback_trigger=rollback_trigger,
            watch_duration_hours=watch_hours,
        )
    log.info(f"Patches notified: deploy watch posted ({watch_hours}h)")


# ── TRIAGE ────────────────────────────────────────────────────────────────────

def triage(suggestion: dict, db: DB) -> str | None:
    """
    Check for blockers before implementing.
    Returns None if safe to proceed, or a reason string if blocked.
    """
    # Friday blackout
    if is_friday_blackout() and suggestion.get("agent") != "Watchdog":
        return "Friday push window — Blueprint deferred to Monday"

    # Breaking changes need migration plan
    impl = suggestion.get("implementation", {})
    if impl.get("breaking_changes") and not suggestion.get("approver_notes"):
        return "breaking_changes=true but no migration plan in approver_notes"

    # Post-trading: retail Pi changes need Patches sign-off
    if is_post_trading():
        component = suggestion.get("impact", {}).get("affected_component", "")
        retail_components = ["Bolt", "Scout", "Pulse", "Trader", "Research",
                             "Sentiment", "All Pis", "Retail"]
        if any(rc.lower() in component.lower() for rc in retail_components):
            # Check if Patches has a matching approved suggestion
            patches_ok = db.query("""
                SELECT id FROM suggestions
                WHERE agent='Patches' AND status='approved'
                AND title LIKE ? LIMIT 1
            """, (f"%{suggestion['id'][:8]}%",))
            if not patches_ok:
                return (
                    "Post-trading mode: retail Pi change requires Patches sign-off. "
                    "Patches must submit a corroborating approval suggestion."
                )

    # Staleness: approved >30 days ago
    ts_str = suggestion.get("timestamp", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).days
            if age_days > 30:
                return (
                    f"Suggestion approved {age_days} days ago — "
                    "codebase may have changed. Re-validate before implementing."
                )
        except Exception:
            pass

    return None  # clear to proceed


# ── MAIN PROCESSING LOOP ──────────────────────────────────────────────────────

def process_suggestion(suggestion: dict, db: DB) -> bool:
    """
    Full implementation pipeline for one suggestion.
    Returns True on success, False on failure.
    """
    sid   = suggestion["id"]
    title = suggestion["title"]
    log.info(f"Processing: {sid[:8]} — {title}")

    # Triage
    blocker = triage(suggestion, db)
    if blocker:
        log.warning(f"BLOCKED: {sid[:8]} — {blocker}")
        with db.slot("Blueprint", "block_suggestion", priority=4):
            db.update_suggestion_status(
                sid, implementation_status="blocked",
                notes=f"BLOCKED by Blueprint triage: {blocker}"
            )
            db.post_suggestion(
                agent="Blueprint",
                category="bug",
                title=f"BLOCKED: {title[:60]}",
                description=f"Blueprint cannot implement {sid[:8]}: {blocker}",
                risk_level="MEDIUM",
                approver_needed="you",
            )
        return False

    # Mark in progress
    with db.slot("Blueprint", "mark_in_progress", priority=4):
        db.update_suggestion_status(sid, implementation_status="in_progress")

    # Collect source files
    source_files = collect_relevant_files(suggestion)
    log.info(f"Collected {len(source_files)} source file(s)")

    # Call Claude
    implementation = call_claude(suggestion, source_files)
    if not implementation:
        with db.slot("Blueprint", "mark_failed", priority=4):
            db.update_suggestion_status(
                sid, implementation_status="failed",
                notes="Claude API call failed or returned invalid JSON"
            )
        return False

    # Log token usage
    db.log_api_call(
        agent_name="Blueprint",
        provider="anthropic",
        operation="implement_suggestion",
        token_count=implementation.get("_tokens_used", 0),
        model=MODEL_CAPABLE if suggestion.get("impact", {}).get(
            "risk_level") in ("CRITICAL", "HIGH") else MODEL_FAST,
    )

    # Self-review
    failures = self_review(implementation, source_files)
    if failures:
        log.warning(f"Self-review failed for {sid[:8]}:")
        for f in failures:
            log.warning(f"  → {f}")
        with db.slot("Blueprint", "mark_failed", priority=4):
            db.update_suggestion_status(
                sid, implementation_status="failed",
                notes=f"Self-review failed: {'; '.join(failures)}"
            )
        return False

    # Stage files
    try:
        stage_dir = stage_files(sid, implementation, source_files)
    except Exception as e:
        log.error(f"Staging failed: {e}")
        with db.slot("Blueprint", "mark_failed", priority=4):
            db.update_suggestion_status(
                sid, implementation_status="failed",
                notes=f"Staging failed: {e}"
            )
        return False

    # Package for portal
    try:
        pkg_dir = package_for_review(suggestion, implementation, source_files)
    except Exception as e:
        log.error(f"Packaging failed: {e}")
        cleanup_staging(sid)
        with db.slot("Blueprint", "mark_failed", priority=4):
            db.update_suggestion_status(
                sid, implementation_status="failed",
                notes=f"Packaging failed: {e}"
            )
        return False

    # Create git branch and commit to staging
    branch = create_feature_branch(sid)
    if branch:
        commit_to_staging(suggestion, implementation, branch)

    cleanup_staging(sid)

    # Update suggestions table
    notes = (
        f"Implementation packaged at {pkg_dir.name}. "
        f"Confidence: {implementation.get('confidence','?')}. "
        f"Summary: {implementation.get('summary','N/A')}. "
        f"Blueprint notes: {implementation.get('blueprint_notes','None')}. "
        f"Awaiting project lead review in command portal before Friday push."
    )
    with db.slot("Blueprint", "mark_complete", priority=4):
        db.update_suggestion_status(
            sid, implementation_status="complete", notes=notes
        )

    # Notify Patches
    affected_files = [f["path"] for f in implementation.get("files_changed", [])]
    notify_patches(db, sid, affected_files,
                   suggestion.get("impact", {}).get("risk_level", "LOW"))

    log.info(f"Done: {sid[:8]} — packaged and ready for portal review")
    return True


def run(mode: str = "scheduled") -> None:
    db = DB()

    if is_friday_blackout() and mode != "event":
        log.info("Friday push window — Blueprint deferred to Monday")
        return

    if mode == "scheduled" and not is_build_window():
        log.info("Outside build window (Mon–Thu 8pm–midnight ET) — skipping")
        return

    # Get approved, not-yet-started suggestions
    approved = [
        s for s in db.get_approved_suggestions()
        if s.get("implementation_status") is None
    ]

    if not approved:
        log.info("No approved suggestions awaiting implementation")
        return

    cap = weekly_cap()
    log.info(
        f"Found {len(approved)} approved suggestion(s). "
        f"Cap: {cap} ({'post' if is_post_trading() else 'pre'}-trading)"
    )

    # Sort by risk: CRITICAL first
    risk_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4}
    approved.sort(key=lambda s: risk_order.get(
        s.get("risk_level", s.get("impact", {}).get("risk_level", "NONE")), 5
    ))

    processed = 0
    for suggestion in approved[:cap]:
        try:
            success = process_suggestion(suggestion, db)
            if success:
                processed += 1
        except Exception as e:
            log.exception(f"Unhandled error on {suggestion['id'][:8]}: {e}")
            with db.slot("Blueprint", "mark_failed", priority=4):
                db.update_suggestion_status(
                    suggestion["id"],
                    implementation_status="failed",
                    notes=f"Unhandled exception: {e}"
                )

    log.info(f"Run complete — {processed}/{min(len(approved), cap)} processed")


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status(db: DB) -> None:
    in_prog = db.get_approved_suggestions("in_progress")
    complete = db.get_approved_suggestions("complete")
    blocked  = db.get_approved_suggestions("blocked")
    pending  = [s for s in db.get_approved_suggestions()
                if s.get("implementation_status") is None]

    print(f"\n{'='*60}")
    print(f"BLUEPRINT STATUS — {'post' if is_post_trading() else 'pre'}-trading")
    print(f"Friday blackout: {'YES' if is_friday_blackout() else 'NO'}")
    print(f"Build window:    {'YES' if is_build_window() else 'NO'}")
    print(f"Weekly cap:      {weekly_cap()}")
    print(f"{'='*60}")
    print(f"  Pending (approved, not started): {len(pending)}")
    print(f"  In progress:                     {len(in_prog)}")
    print(f"  Complete (awaiting portal):       {len(complete)}")
    print(f"  Blocked:                         {len(blocked)}")
    print(f"{'='*60}\n")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blueprint — Engineer Agent")
    parser.add_argument(
        "--mode",
        choices=["scheduled", "event"],
        default="scheduled",
        help="scheduled=build window, event=CRITICAL trigger"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show queue without executing"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current week progress"
    )
    args = parser.parse_args()

    db = DB()

    if args.status:
        show_status(db)
    elif args.dry_run:
        approved = [
            s for s in db.get_approved_suggestions()
            if s.get("implementation_status") is None
        ]
        print(f"\nDry run — {len(approved)} suggestion(s) queued:")
        for s in approved:
            risk = s.get("risk_level",
                         s.get("impact", {}).get("risk_level", "?"))
            print(f"  [{risk:8}] {s['id'][:8]} — {s['title'][:60]}")
        print()
    else:
        run(mode=args.mode)
