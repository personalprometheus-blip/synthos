"""
seed_backlog.py — Initial Suggestions Backlog Seeder
Synthos · Company Node Utility

Populates data/suggestions.json with all known open TODO items on first install.
Idempotent: skips any suggestion whose title already exists in the file.

Usage:
  python3 seed_backlog.py              # preview only — shows what would be written
  python3 seed_backlog.py --write      # write to suggestions.json
  python3 seed_backlog.py --force      # re-seed even if file already has entries

Called by: company installer (install_company.py) on first-run setup.
Operator may also run manually: python3 synthos_build/seed_backlog.py --write
"""

import os
import sys
import json
import uuid
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── PATH RESOLUTION ───────────────────────────────────────────────────────
# Dynamic — never hardcoded. Works regardless of install location or username.
BASE_DIR       = Path(__file__).resolve().parent
DATA_DIR       = BASE_DIR / "data"
SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
ARCHIVE_FILE   = DATA_DIR / "suggestions_archive.json"

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s seed_backlog: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('seed_backlog')

SEEDED_BY = "seed_backlog"


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def make_suggestion(title, description, category, scope, priority, target_files):
    """
    Build a fully valid `proposed` suggestion object per SUGGESTIONS_JSON_SPEC.md.
    Required fields only — no lifecycle fields populated at creation.
    """
    ts = now_utc()
    sid = str(uuid.uuid4())
    return {
        "suggestion_id":        sid,
        "status":               "proposed",
        "category":             category,
        "scope":                scope,
        "priority":             priority,
        "title":                title,
        "description":          description,
        "target_files":         target_files,
        "created_by":           SEEDED_BY,
        "created_at":           ts,
        "reviewed_by":          None,
        "review_started_at":    None,
        "approved_by":          None,
        "approved_at":          None,
        "rejected_by":          None,
        "rejected_at":          None,
        "rejection_reason":     None,
        "blocked_by":           None,
        "blocked_at":           None,
        "blocked_reason":       None,
        "assigned_to":          None,
        "started_at":           None,
        "staged_at":            None,
        "staging_manifest":     None,
        "deployed_at":          None,
        "deploy_commit":        None,
        "post_deploy_watch_id": None,
        "completed_at":         None,
        "validated_by":         None,
        "superseded_by":        None,
        "superseded_at":        None,
        "audit_log": [
            {
                "timestamp": ts,
                "agent":     SEEDED_BY,
                "action":    "proposed",
                "note":      f"Seeded from known open TODO backlog. Title: {title}"
            }
        ]
    }


# ── SEED CATALOGUE ────────────────────────────────────────────────────────
# Each entry maps directly to a known open item from SYNTHOS_MASTER_STATUS.md.
# Organised by priority: medium first, then low.
# target_files uses paths relative to SYNTHOS_HOME (synthos_build/).

SEED_ITEMS = [

    # ── MEDIUM PRIORITY ───────────────────────────────────────────────────

    make_suggestion(
        title       = "BB-02: Build UDP interrogation ACK receiver (peer corroboration)",
        description = (
            "Scout (agent2_research.py) broadcasts HAS_DATA_FOR_INTERROGATION on UDP port 5556 "
            "and waits 30s for an ACK on port 5557. Currently no agent listens and responds. "
            "All signals are therefore marked UNVALIDATED, which forces them to WATCH under "
            "Option B rules instead of MIRROR. "
            "Build the receiving side: a lightweight UDP listener that reads the broadcast, "
            "performs a cross-validation check (at minimum: confirm ticker is tradeable and "
            "signal is not a duplicate of a recent signal), and sends ACK back on port 5557. "
            "This enables VALIDATED signals and allows HIGH-confidence signals to MIRROR. "
            "Blocked until a second retail Pi is available OR a stub validator is built for "
            "single-Pi operation."
        ),
        category     = "improvement",
        scope        = "retail",
        priority     = "medium",
        target_files = ["synthos_build/agent2_research.py"],
    ),

    make_suggestion(
        title       = "BB-03: Build company-side news feed consumer endpoint",
        description = (
            "Scout POSTs signal data to MONITOR_URL/api/news-feed when COMPANY_SUBSCRIPTION=true. "
            "No company agent currently handles this endpoint. "
            "Build a receiver on the company node that: (1) accepts the POST, (2) writes to a "
            "company-side news_feed table or log, (3) makes signal history available to Patches "
            "for pattern analysis and to Blueprint for backlog suggestions. "
            "This enables company-side learning from retail signal activity over time."
        ),
        category     = "improvement",
        scope        = "company_internal",
        priority     = "medium",
        target_files = ["synthos_build/synthos_monitor.py"],
    ),

    make_suggestion(
        title       = "T-07: Add authentication and HTTPS to installer web UI",
        description = (
            "install_retail.py serves a local web UI during installation that is currently "
            "unprotected — no password, no TLS. On a local network this is low risk, but any "
            "operator running the installer on a shared or exposed network could have their "
            "API keys captured during the install flow. "
            "Add basic auth (INSTALLER_PASSWORD env var) and optionally a self-signed cert "
            "for the installer's temporary Flask server. Deactivate immediately after install "
            "completes. This is a hardening measure, not a blocker for Phase 1."
        ),
        category     = "security",
        scope        = "retail",
        priority     = "medium",
        target_files = ["synthos_build/install_retail.py"],
    ),

    make_suggestion(
        title       = "T-08: Wire seed_backlog.py into company installer automatically",
        description = (
            "seed_backlog.py exists but must be run manually after company Pi install. "
            "install_company.py (or boot_sequence.py on company node) should call "
            "seed_backlog.py --write automatically on first boot if suggestions.json is empty. "
            "This ensures Blueprint and Patches have a populated backlog from day one "
            "without requiring an operator manual step. "
            "Implementation: add a first-boot sentinel check in boot_sequence.py for the "
            "company node; if data/suggestions.json is missing or empty, run seed_backlog.py."
        ),
        category     = "improvement",
        scope        = "company_internal",
        priority     = "medium",
        target_files = ["synthos_build/boot_sequence.py"],
    ),

    make_suggestion(
        title       = "T-10: Fix first_run.sh hardcoded /home/pi/synthos path",
        description = (
            "first_run.sh contains a hardcoded reference to /home/pi/synthos. "
            "Per ADDENDUM 1 §1, no script may hardcode /home/pi/. "
            "If a customer creates a different username (e.g. /home/alice/synthos), "
            "first_run.sh will fail. "
            "Fix: replace hardcoded path with dynamic resolution using "
            'SYNTHOS_DIR="$(cd "$(dirname "$0")" && pwd)" pattern. '
            "Verify all downstream commands in the script use the variable."
        ),
        category     = "arch_violation",
        scope        = "retail",
        priority     = "medium",
        target_files = ["synthos_build/first_run.sh"],
    ),

    make_suggestion(
        title       = "T-15/T-16: Activate IP allowlisting enforcement in Sentinel",
        description = (
            "config/allowed_ips.json is written by the installer with a stub list. "
            "Sentinel reads this file but does not yet enforce it — heartbeat POSTs from "
            "unknown IPs are logged but not rejected with 403. "
            "When activated: Sentinel should return 403 for heartbeat POSTs from IPs not "
            "in the allowed list, log the attempt, and alert Patches after 3+ attempts "
            "from the same unknown IP within one hour. "
            "Deferred until: (1) the IP inventory is stable, (2) SSH access is confirmed "
            "from all expected locations. Activating prematurely will lock out the operator."
        ),
        category     = "security",
        scope        = "system_wide",
        priority     = "medium",
        target_files = ["synthos_build/sentinel.py", "synthos_build/config/allowed_ips.json"],
    ),

    # ── LOW PRIORITY ──────────────────────────────────────────────────────

    make_suggestion(
        title       = "T-04: Gmail SMTP activation via command portal toggle",
        description = (
            "agent1_trader.py has a Gmail SMTP fallback path (commented out) for P0 alerts "
            "when SendGrid is unavailable. The code is present but disabled. "
            "Add a portal toggle (GMAIL_SMTP_ENABLED env var) so the operator can activate "
            "the Gmail path without editing code. Requires GMAIL_USER and GMAIL_APP_PASSWORD "
            "in .env. The portal settings page should expose this toggle alongside the "
            "existing SendGrid configuration section."
        ),
        category     = "improvement",
        scope        = "retail",
        priority     = "low",
        target_files = ["synthos_build/agent1_trader.py", "synthos_build/portal.py"],
    ),

    make_suggestion(
        title       = "T-09: Migrate to named Cloudflare tunnel with synth-cloud.com domain",
        description = (
            "The current Cloudflare tunnel uses a temporary random subdomain. "
            "A named tunnel with permanent subdomains on synth-cloud.com is defined in "
            "CLAUDE_CODE_INSTRUCTIONS_domain_integration.md (repo root). "
            "Subdomain map: portal→portal.synth-cloud.com (5001), "
            "monitor/console→console.synth-cloud.com (5000). "
            "This requires: (1) creating the named tunnel in Cloudflare dashboard, "
            "(2) updating tunnel config files, (3) replacing all placeholder tunnel URLs "
            "in .env examples and documentation. "
            "Low priority — temporary tunnel works fine for Phase 1 single-Pi operation."
        ),
        category     = "improvement",
        scope        = "system_wide",
        priority     = "low",
        target_files = [],
    ),

    make_suggestion(
        title       = "T-20: Scoop active transport toggle via command portal",
        description = (
            "Scoop (scoop.py) has a fixed transport path today (SendGrid primary, "
            "queue fallback). The command portal has no control over which transport "
            "Scoop uses at runtime. "
            "Add a SCOOP_TRANSPORT env var (sendgrid | smtp | queue_only) and wire "
            "a portal toggle so the operator can switch transports without restarting "
            "the agent. Useful for testing and for graceful fallback when SendGrid "
            "is unavailable."
        ),
        category     = "improvement",
        scope        = "company_internal",
        priority     = "low",
        target_files = ["synthos_build/scoop.py"],
    ),

    make_suggestion(
        title       = "INC-004: Verify retail utils/ directory existence and register in manifest",
        description = (
            "SYNTHOS_TECHNICAL_ARCHITECTURE.md references a retail utils/ directory "
            "(core/utils/) but it may not exist on disk. "
            "Action: run `ls synthos_build/` on the retail Pi and confirm whether "
            "core/utils/ exists. If yes: register all files in SYSTEM_MANIFEST.md. "
            "If no: strike the reference from SYNTHOS_TECHNICAL_ARCHITECTURE.md. "
            "This is a documentation hygiene item — no code change required."
        ),
        category     = "documentation",
        scope        = "retail",
        priority     = "low",
        target_files = [],
    ),

    make_suggestion(
        title       = "INC-007: Fix hardcoded /home/pi/ log paths in TOOL_DEPENDENCY_ARCHITECTURE.md",
        description = (
            "TOOL_DEPENDENCY_ARCHITECTURE.md contains log path examples that hardcode "
            "/home/pi/ in violation of ADDENDUM 1 §1. "
            "Replace all hardcoded /home/pi/ references in the logging section with "
            "${LOG_DIR}/ variable notation. No code change — documentation only."
        ),
        category     = "documentation",
        scope        = "system_wide",
        priority     = "low",
        target_files = ["synthos_build/TOOL_DEPENDENCY_ARCHITECTURE.md"],
    ),

    make_suggestion(
        title       = "INC-008: Fix hardcoded /home/pi/ paths in INSTALLER_STATE_MACHINE.md",
        description = (
            "INSTALLER_STATE_MACHINE.md detection criteria reference hardcoded /home/pi/ "
            "paths in violation of ADDENDUM 1 §1. "
            "Replace with ${SYNTHOS_HOME}/ variable notation throughout. "
            "No code change — documentation only."
        ),
        category     = "documentation",
        scope        = "retail",
        priority     = "low",
        target_files = ["synthos_build/INSTALLER_STATE_MACHINE.md"],
    ),

    make_suggestion(
        title       = "T-21: Build cross-Pi comparison log and behavioral analysis report",
        description = (
            "As the fleet grows beyond one retail Pi, there is no mechanism to compare "
            "how different Pis are behaving — which signals they acted on, how their "
            "member weights have diverged, relative portfolio performance, interrogation "
            "validation rates, and trade outcome differences. "
            "Build a comparison report that: "
            "(1) Collects heartbeat + daily report data already sent to synthos_monitor.py per Pi; "
            "(2) Adds per-Pi signal decision tracking (MIRROR/WATCH/SKIP rates, adjusted score distribution); "
            "(3) Adds member weight divergence — same politician, different weight on different Pis; "
            "(4) Compares portfolio performance (realized P&L, win rate, deployed %) across Pis; "
            "(5) Tracks interrogation validation rates per Pi (% VALIDATED vs UNVALIDATED); "
            "(6) Surfaces anomalies: a Pi consistently diverging from peers warrants investigation. "
            "Output: a structured comparison JSON at /api/pi-comparison on synthos_monitor.py, "
            "and a summary section in Patches' morning digest. "
            "Requires: all retail Pis already POST heartbeat + daily report to monitor — "
            "comparison log extends this existing data stream, no new Pi-side code needed."
        ),
        category     = "observability",
        scope        = "system_wide",
        priority     = "medium",
        target_files = ["synthos_build/synthos_monitor.py"],
    ),

    make_suggestion(
        title       = "T-22: Build RSS/news feed distribution system",
        description = (
            "Synthos agents currently use hardcoded or ad-hoc feed lists for news ingestion. "
            "A distributed, rate-controlled feed system is needed. "
            "Existing file: synthos_build/free_public_api_source_list.html — also check "
            "GitHub repo root for the latest version. "
            "Design: "
            "(1) Company node parses free_public_api_source_list.html into a feed_sources "
            "DB table (url, name, tier, pull_count_today, is_active, disabled_reason, last_reset_at). "
            "(2) Company node exposes GET /api/feed endpoint — returns one randomly selected "
            "active feed URL to the caller. "
            "(3) Each retail Pi call increments pull_count_today for the returned feed. "
            "When pull_count_today exceeds a configurable threshold (e.g. FEED_PULL_LIMIT_PER_DAY), "
            "the feed is marked is_active=False (temporarily disabled) as a web attack prevention measure. "
            "(4) A cron job on the company node runs at 00:01 daily: resets pull_count_today=0 "
            "and re-enables all feeds. "
            "(5) Retail Pi agent3_sentiment.py calls GET /api/feed to get a URL instead of "
            "using any hardcoded list. "
            "Implementation steps: parse HTML into DB, write /api/feed endpoint, "
            "add pull counter + disable logic, add cron reset, update retail agent caller."
        ),
        category     = "improvement",
        scope        = "system_wide",
        priority     = "medium",
        target_files = [
            "synthos_build/free_public_api_source_list.html",
            "synthos_build/agent3_sentiment.py",
            "synthos_build/database.py",
        ],
    ),

    make_suggestion(
        title       = "INC-009: Add company agent TDA classifications to TOOL_DEPENDENCY_ARCHITECTURE.md",
        description = (
            "TOOL_DEPENDENCY_ARCHITECTURE.md classifies tool types (Bootstrap, Runtime, "
            "Maintenance, Repair, Security, Data, Observability) but does not explicitly "
            "classify the company agents (Blueprint, Patches, Vault, Sentinel, Fidget, "
            "Scoop, Librarian, Timekeeper, Strongbox). "
            "Add a company agents section with the correct TDA classification for each. "
            "No code change — documentation only."
        ),
        category     = "documentation",
        scope        = "company_internal",
        priority     = "low",
        target_files = ["synthos_build/TOOL_DEPENDENCY_ARCHITECTURE.md"],
    ),

]


# ── MAIN ──────────────────────────────────────────────────────────────────

def load_existing():
    """Load existing suggestions.json, return list. Returns [] if not found."""
    if not SUGGESTIONS_FILE.exists():
        return []
    try:
        with open(SUGGESTIONS_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("suggestions.json exists but is not a list — treating as empty")
            return []
        return data
    except Exception as e:
        log.error(f"Failed to load suggestions.json: {e}")
        return []


def get_existing_titles(existing):
    return {s.get('title', '') for s in existing}


def run(write=False, force=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing = load_existing()
    existing_titles = get_existing_titles(existing)

    if existing and not force:
        log.info(f"suggestions.json already has {len(existing)} entries.")
        log.info("Use --force to re-seed all missing items into an existing file.")

    to_add = []
    skipped = 0
    for item in SEED_ITEMS:
        if item['title'] in existing_titles:
            log.info(f"  SKIP (exists): {item['title'][:70]}")
            skipped += 1
        else:
            to_add.append(item)
            log.info(f"  ADD: {item['title'][:70]}")

    log.info(f"\nSummary: {len(to_add)} to add, {skipped} already present, {len(SEED_ITEMS)} total seed items.")

    if not to_add:
        log.info("Nothing to add — backlog is already fully seeded.")
        return

    if not write:
        log.info("\nDRY RUN — no changes written. Use --write to apply.")
        return

    merged = existing + to_add
    tmp_path = SUGGESTIONS_FILE.with_suffix('.tmp')
    try:
        with open(tmp_path, 'w') as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp_path, SUGGESTIONS_FILE)
        log.info(f"Written: {SUGGESTIONS_FILE} ({len(merged)} total suggestions)")
    except Exception as e:
        log.error(f"Write failed: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        sys.exit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Synthos — seed initial suggestions backlog'
    )
    parser.add_argument('--write', action='store_true',
                        help='Write to suggestions.json (default: dry run preview)')
    parser.add_argument('--force', action='store_true',
                        help='Add missing seed items even if file already has entries')
    args = parser.parse_args()
    run(write=args.write, force=args.force)
