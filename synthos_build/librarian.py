"""
librarian.py — Librarian > Tool Agent
Synthos Company Pi | /home/pi/synthos-company/agents/librarian.py

Role:
  Dependency manager, security auditor, and tool discovery agent.
  Librarian is Blueprint's upstream gate for all package decisions.

  Librarian is accountable for:
    - Knowing what packages are installed on every Pi
    - Flagging CVEs before they become incidents
    - Maintaining the approved package manifest
    - Identifying unused, duplicated, or consolidatable code
    - Suggesting library improvements to Blueprint

  Librarian does not install anything. It finds, documents, and recommends.
  Blueprint implements after project lead approval.

  Librarian is Blueprint's dependency gate:
    - Blueprint cannot add a package without it being in the manifest
    - Librarian reviews and approves new packages first
    - If a CVE is found, Librarian submits CRITICAL — Blueprint gets event-triggered

Schedule:
  Weekly full audit:   Monday 2am ET (before build window opens)
  CVE check:           Daily 5am ET (before market open)
  On-demand:           python3 librarian.py --audit

Manifest:
  /home/pi/synthos-company/config/package_manifest.json
  The authoritative list of approved packages for all Pis.
  Blueprint reads this before adding any dependency.

USAGE:
  python3 librarian.py --audit          # full dependency audit
  python3 librarian.py --cve-check      # CVE scan only
  python3 librarian.py --show-manifest  # print approved package manifest
  python3 librarian.py --approve <pkg>  # add package to manifest
  python3 librarian.py --diff <pi_id>   # compare Pi packages against manifest
"""

import os
import sys
import json
import uuid
import shutil
import logging
import argparse
import subprocess
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Paths resolved dynamically — works for any username or install directory.
# Override with SYNTHOS_BASE_DIR / SYNTHOS_RETAIL_DIR env vars if needed.

import sys as _sys
import os.path as _osp
_AGENTS_DIR = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in _sys.path:
    _sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import (
    BASE_DIR, DATA_DIR, LOGS_DIR, CONFIG_DIR, DB_PATH,
    SUGGESTIONS_FILE, ENV_PATH, RETAIL_DIR,
)

LOG_FILE       = LOGS_DIR   / "tool_agent.log"
MANIFEST_FILE  = CONFIG_DIR / "package_manifest.json"
CVE_CACHE_FILE = DATA_DIR   / ".cve_cache.json"
AUDIT_RESULT   = DATA_DIR   / "librarian_latest.json"

# Retail tree — used for package scanning. May not exist on all deployments.
# Override with SYNTHOS_RETAIL_DIR env var if installed elsewhere.
RETAIL_BASE = RETAIL_DIR if RETAIL_DIR else BASE_DIR.parent / "synthos"
CORE_DIR    = RETAIL_BASE / "core"

load_dotenv(ENV_PATH, override=True)

# Minimum package versions — anything below these is flagged regardless of CVE
MINIMUM_VERSIONS = {
    "requests":     "2.31.0",
    "cryptography": "41.0.0",
    "flask":        "3.0.0",
    "urllib3":      "2.0.0",
}

# Packages that should always be present on retail Pis
REQUIRED_PACKAGES = [
    "anthropic",
    "alpaca-trade-api",
    "requests",
    "flask",
    "python-dotenv",
    "cryptography",
]

# Packages that are NEVER allowed — security risk or unnecessary
BANNED_PACKAGES = [
    "telnetlib3",
    "pysftp",        # insecure SFTP — use paramiko
    "pickle",        # not a pip package but flag if used in code
]

SYNTHOS_VERSION = "1.0"

load_dotenv(BASE_DIR / ".env", override=True)

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s librarian: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("librarian")


# ── TIME HELPERS ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    """
    Load the approved package manifest.
    If it doesn't exist, create a minimal default manifest.
    """
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not MANIFEST_FILE.exists():
        default = {
            "version":      "1.0",
            "last_updated": now_iso(),
            "updated_by":   "Librarian (bootstrap)",
            "packages": {
                "anthropic":          {"min_version": "0.18.0", "required": True,  "notes": "Claude API client"},
                "alpaca-trade-api":   {"min_version": "3.0.0",  "required": True,  "notes": "Alpaca paper trading"},
                "requests":           {"min_version": "2.31.0", "required": True,  "notes": "HTTP client"},
                "flask":              {"min_version": "3.0.0",  "required": True,  "notes": "Portal web server"},
                "python-dotenv":      {"min_version": "1.0.0",  "required": True,  "notes": "Environment config"},
                "cryptography":       {"min_version": "41.0.0", "required": True,  "notes": "Vault encryption"},
                "boto3":              {"min_version": "1.26.0", "required": False, "notes": "R2 backup uploads"},
                "pytz":               {"min_version": "2023.3", "required": False, "notes": "Timezone handling"},
            },
        }
        MANIFEST_FILE.write_text(json.dumps(default, indent=2))
        log.info(f"Created default manifest at {MANIFEST_FILE}")

    return json.loads(MANIFEST_FILE.read_text())


def save_manifest(manifest: dict) -> None:
    if MANIFEST_FILE.exists():
        shutil.copy2(MANIFEST_FILE, MANIFEST_FILE.with_suffix(".json.backup"))
    manifest["last_updated"] = now_iso()
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest saved")


def approve_package(package_name: str, min_version: str = "",
                    notes: str = "", required: bool = False) -> None:
    """Add a package to the approved manifest."""
    manifest = load_manifest()
    manifest["packages"][package_name] = {
        "min_version": min_version,
        "required":    required,
        "notes":       notes,
        "approved_at": now_iso(),
        "approved_by": "project_lead",
    }
    manifest["updated_by"] = "project_lead via CLI"
    save_manifest(manifest)
    log.info(f"Approved package: {package_name} (min={min_version})")


def is_package_approved(package_name: str) -> bool:
    """Check if a package is in the approved manifest."""
    manifest = load_manifest()
    return package_name.lower() in {
        k.lower() for k in manifest.get("packages", {}).keys()
    }


# ── PACKAGE SCANNING ──────────────────────────────────────────────────────────

def get_installed_packages(target_dir: Path = None) -> dict[str, str]:
    """
    Get installed packages and their versions.
    Returns {package_name: version}.

    If target_dir is a retail Pi directory, tries to read its pip freeze output
    from the heartbeat metadata or by running pip on its virtualenv.
    Falls back to scanning the current environment.
    """
    packages = {}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            for pkg in json.loads(result.stdout):
                packages[pkg["name"].lower()] = pkg["version"]
    except Exception as e:
        log.warning(f"pip list failed: {e}")
    return packages


def get_retail_pi_packages(pi_id: str) -> dict[str, str]:
    """
    Get packages installed on a retail Pi.
    Strategy: check heartbeat metadata in company.db first.
    If not available, try SSH (if configured).
    Falls back to reading requirements.txt from the Pi's core directory.
    """
    # Try reading from the retail Pi's requirements.txt
    req_path = RETAIL_BASE / "requirements.txt"
    if req_path.exists():
        packages = {}
        for line in req_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "==" in line:
                name, version = line.split("==", 1)
                packages[name.lower()] = version.strip()
            elif ">=" in line:
                name = line.split(">=")[0]
                packages[name.lower()] = "unknown"
            else:
                packages[line.lower()] = "unknown"
        return packages

    # Fall back to current environment scan
    log.warning(f"Cannot read Pi packages for {pi_id} — using local environment")
    return get_installed_packages()


# ── CVE SCANNING ──────────────────────────────────────────────────────────────

def check_cves(packages: dict[str, str]) -> list[dict]:
    """
    Check installed packages against known CVEs using pip-audit.
    Falls back to checking MINIMUM_VERSIONS if pip-audit is unavailable.
    Returns list of vulnerability dicts.
    """
    vulnerabilities = []

    # Try pip-audit first (most thorough)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--format=json", "--output=-"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode in (0, 1):   # 1 = vulns found, not an error
            audit_data = json.loads(result.stdout)
            for dep in audit_data.get("dependencies", []):
                for vuln in dep.get("vulns", []):
                    vulnerabilities.append({
                        "package":     dep.get("name"),
                        "version":     dep.get("version"),
                        "cve_id":      vuln.get("id"),
                        "description": vuln.get("description", "")[:200],
                        "fix_version": vuln.get("fix_versions", ["unknown"])[0]
                            if vuln.get("fix_versions") else "unknown",
                        "severity":    _cve_severity(vuln.get("id", "")),
                        "source":      "pip-audit",
                    })
            log.info(f"pip-audit complete: {len(vulnerabilities)} vulnerabilities found")
            return vulnerabilities
    except FileNotFoundError:
        log.warning("pip-audit not installed — falling back to version checks")
    except Exception as e:
        log.warning(f"pip-audit failed: {e} — falling back to version checks")

    # Fallback: check minimum versions
    for pkg, min_ver in MINIMUM_VERSIONS.items():
        installed = packages.get(pkg.lower(), "")
        if not installed or installed == "unknown":
            continue
        if _version_lt(installed, min_ver):
            vulnerabilities.append({
                "package":     pkg,
                "version":     installed,
                "cve_id":      "VERSION_CHECK",
                "description": f"Version {installed} is below minimum {min_ver}",
                "fix_version": min_ver,
                "severity":    "HIGH",
                "source":      "version_check",
            })

    # Check banned packages
    for banned in BANNED_PACKAGES:
        if banned.lower() in packages:
            vulnerabilities.append({
                "package":     banned,
                "version":     packages[banned.lower()],
                "cve_id":      "BANNED_PACKAGE",
                "description": f"{banned} is not permitted in Synthos deployments",
                "fix_version": "remove",
                "severity":    "HIGH",
                "source":      "policy",
            })

    return vulnerabilities


def _cve_severity(cve_id: str) -> str:
    """Approximate severity from CVE ID — pip-audit often includes this."""
    # pip-audit includes severity in vuln data; this is a fallback
    cve_id = cve_id.upper()
    if "CRITICAL" in cve_id:
        return "CRITICAL"
    if "HIGH" in cve_id:
        return "HIGH"
    return "MEDIUM"


def _version_lt(v1: str, v2: str) -> bool:
    """Simple version comparison: True if v1 < v2."""
    try:
        def parts(v):
            return [int(x) for x in v.strip().split(".")[:3]]
        return parts(v1) < parts(v2)
    except Exception:
        return False


# ── CODE ANALYSIS ─────────────────────────────────────────────────────────────

def scan_unused_imports(directory: Path) -> list[dict]:
    """
    Scan Python files for unused imports.
    Uses basic AST analysis — not as thorough as a linter but Pi-safe.
    """
    findings = []

    for py_file in directory.glob("**/*.py"):
        if py_file.name.startswith("."):
            continue
        try:
            import ast
            source = py_file.read_text(errors="replace")
            tree   = ast.parse(source)

            imported = set()
            used     = set()

            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imported.add(name.split(".")[0])
                elif isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)

            unused = imported - used - {"__future__"}
            for name in unused:
                findings.append({
                    "file":   str(py_file.relative_to(directory)),
                    "issue":  f"Possibly unused import: {name}",
                    "type":   "unused_import",
                })

        except SyntaxError:
            pass   # Patches handles syntax errors
        except Exception:
            pass

    return findings


def find_duplicate_utilities(directory: Path) -> list[dict]:
    """
    Find functions with identical or near-identical names across files.
    Simple heuristic — flag for Blueprint to consolidate.
    """
    import ast
    findings   = []
    func_names: dict[str, list[str]] = {}

    for py_file in directory.glob("**/*.py"):
        if py_file.name.startswith("."):
            continue
        try:
            tree = ast.parse(py_file.read_text(errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    fname = node.name
                    fpath = str(py_file.relative_to(directory))
                    func_names.setdefault(fname, []).append(fpath)
        except Exception:
            pass

    for fname, files in func_names.items():
        if len(files) > 1 and not fname.startswith("_"):
            findings.append({
                "function": fname,
                "files":    files,
                "issue":    f"Function '{fname}' defined in {len(files)} files — consider consolidating",
                "type":     "duplicate_function",
            })

    return findings


# ── AUDIT ─────────────────────────────────────────────────────────────────────

def run_full_audit() -> dict:
    """
    Full dependency audit — runs Monday 2am ET.
    Returns audit results dict.
    """
    log.info("Starting full dependency audit")
    start = now_utc()

    results = {
        "timestamp":        now_iso(),
        "vulnerabilities":  [],
        "missing_required": [],
        "unapproved":       [],
        "unused_imports":   [],
        "duplicates":       [],
        "suggestions":      [],
    }

    # Get packages
    packages = get_installed_packages()
    manifest = load_manifest()
    approved = {k.lower() for k in manifest.get("packages", {}).keys()}

    # CVE scan
    vulns = check_cves(packages)
    results["vulnerabilities"] = vulns

    # Missing required packages
    for pkg in REQUIRED_PACKAGES:
        if pkg.lower() not in packages:
            results["missing_required"].append(pkg)

    # Unapproved packages (installed but not in manifest)
    for pkg in packages:
        if pkg not in approved and pkg not in ("pip", "setuptools", "wheel", "pkg-resources"):
            results["unapproved"].append({
                "package": pkg,
                "version": packages[pkg],
            })

    # Code analysis on core directory
    if CORE_DIR.exists():
        results["unused_imports"] = scan_unused_imports(CORE_DIR)[:20]   # cap at 20
        results["duplicates"]     = find_duplicate_utilities(CORE_DIR)[:10]

    elapsed = (now_utc() - start).total_seconds()
    results["elapsed_sec"] = round(elapsed, 1)

    # Build suggestions for Blueprint
    _generate_suggestions(results)

    # Save latest result
    AUDIT_RESULT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_RESULT.write_text(json.dumps(results, indent=2))

    log.info(
        f"Audit complete in {elapsed:.1f}s: "
        f"{len(vulns)} CVEs | "
        f"{len(results['missing_required'])} missing | "
        f"{len(results['unapproved'])} unapproved | "
        f"{len(results['duplicates'])} duplicates"
    )

    return results


def run_cve_check() -> list[dict]:
    """
    Daily CVE check only — faster than full audit.
    Submits CRITICAL suggestions immediately for HIGH/CRITICAL CVEs.
    """
    log.info("Running CVE check")
    packages = get_installed_packages()
    vulns    = check_cves(packages)

    critical_vulns = [v for v in vulns if v["severity"] in ("CRITICAL", "HIGH")]

    for vuln in critical_vulns:
        _submit_suggestion(
            title=f"CVE: {vuln['package']} {vuln['version']} — {vuln['cve_id']}",
            description=(
                f"{vuln['package']} v{vuln['version']} has a {vuln['severity']} vulnerability "
                f"({vuln['cve_id']}): {vuln['description']}. "
                f"Fix: upgrade to {vuln['fix_version']}."
            ),
            category="security",
            risk_level=vuln["severity"],
            effort="30 min",
            complexity="TRIVIAL",
        )
        log.warning(
            f"CVE FOUND: {vuln['package']} {vuln['version']} — "
            f"{vuln['cve_id']} [{vuln['severity']}]"
        )

    log.info(f"CVE check complete: {len(vulns)} total, {len(critical_vulns)} critical/high")
    return vulns


# ── SUGGESTIONS ───────────────────────────────────────────────────────────────

def _generate_suggestions(results: dict) -> None:
    """Convert audit findings into Blueprint suggestions."""

    # CVE suggestions
    for vuln in results.get("vulnerabilities", []):
        _submit_suggestion(
            title=f"Update {vuln['package']} — {vuln['cve_id']}",
            description=(
                f"{vuln['package']} v{vuln['version']} has {vuln['severity']} "
                f"vulnerability {vuln['cve_id']}: {vuln['description']}. "
                f"Upgrade to {vuln['fix_version']}."
            ),
            category="security",
            risk_level=vuln["severity"],
            effort="30 min",
            complexity="TRIVIAL",
        )

    # Missing required packages
    if results.get("missing_required"):
        missing = ", ".join(results["missing_required"])
        _submit_suggestion(
            title=f"Install missing required packages: {missing[:50]}",
            description=(
                f"Required packages not found in environment: {missing}. "
                f"These are needed for Synthos agents to function correctly."
            ),
            category="bug",
            risk_level="HIGH",
            effort="1 hour",
            complexity="SIMPLE",
        )

    # Consolidation opportunities
    for dup in results.get("duplicates", []):
        _submit_suggestion(
            title=f"Consolidate '{dup['function']}' across {len(dup['files'])} files",
            description=(
                f"Function '{dup['function']}' is defined in multiple files: "
                f"{', '.join(dup['files'][:3])}. "
                f"Consolidate to /utils/ to reduce maintenance overhead."
            ),
            category="optimization",
            risk_level="LOW",
            effort="2 hours",
            complexity="MODERATE",
        )


def _submit_suggestion(title: str, description: str, category: str,
                       risk_level: str, effort: str,
                       complexity: str) -> None:
    """Submit a suggestion to suggestions.json. Deduplicates by title prefix."""
    try:
        if SUGGESTIONS_FILE.exists():
            data = json.loads(SUGGESTIONS_FILE.read_text())
        else:
            data = {"version": "1.0", "suggestions": []}

        # Deduplicate
        existing_titles = {
            s.get("title", "")[:50]
            for s in data.get("suggestions", [])
            if s.get("status") in ("pending", "approved")
        }
        if title[:50] in existing_titles:
            log.debug(f"Duplicate suggestion skipped: {title[:50]}")
            return

        suggestion = {
            "id":        str(uuid.uuid4()),
            "timestamp": now_iso(),
            "agent":     "Librarian",
            "category":  category,
            "title":     title[:80],
            "description": description,
            "impact": {
                "tokens_saved_per_week":    None,
                "execution_time_saved":     None,
                "risk_level":              risk_level,
                "affected_component":      "All Pis" if category == "security" else "Core",
                "affected_customers_count": None if category == "security" else 1,
                "estimated_improvement":   "Closes vulnerability" if category == "security"
                                           else "Reduces technical debt",
            },
            "implementation": {
                "effort":                effort,
                "complexity":           complexity,
                "approver_needed":      "you",
                "trial_run_recommended": risk_level in ("HIGH", "CRITICAL"),
                "breaking_changes":     False,
                "rollback_difficulty":  "EASY",
            },
            "details": {
                "root_cause":           description,
                "solution_approach":    "Blueprint to implement after approval",
                "alternative_approaches": [],
                "dependencies":         [],
                "metrics_to_track":     ["Issue resolved, no regressions"],
            },
            "status":            "pending",
            "status_updated_at": now_iso(),
            "approver_notes":    None,
            "implementation_status": None,
            "implementation_notes":  None,
        }

        data["suggestions"].append(suggestion)
        data["last_updated"] = now_iso()

        if SUGGESTIONS_FILE.exists():
            shutil.copy2(SUGGESTIONS_FILE, SUGGESTIONS_FILE.with_suffix(".json.backup"))
        SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2))
        log.info(f"Suggestion submitted: {title[:60]}")

    except Exception as e:
        log.error(f"Failed to submit suggestion: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_manifest() -> None:
    manifest = load_manifest()
    packages = manifest.get("packages", {})
    print(f"\n{'=' * 60}")
    print(f"LIBRARIAN MANIFEST — {len(packages)} approved packages")
    print(f"Last updated: {manifest.get('last_updated', 'unknown')}")
    print(f"{'=' * 60}")
    for name, details in sorted(packages.items()):
        req = "✓" if details.get("required") else " "
        ver = details.get("min_version", "any")
        print(f"  {req} {name:30} min={ver:12} {details.get('notes', '')}")
    print(f"{'=' * 60}\n")


def diff_pi(pi_id: str) -> None:
    """Compare a Pi's packages against the manifest."""
    packages = get_retail_pi_packages(pi_id)
    manifest = load_manifest()
    approved = manifest.get("packages", {})

    print(f"\n{'=' * 60}")
    print(f"PACKAGE DIFF — {pi_id}")
    print(f"{'=' * 60}")

    for pkg, details in approved.items():
        installed = packages.get(pkg.lower(), "NOT INSTALLED")
        min_ver   = details.get("min_version", "")
        required  = details.get("required", False)
        status    = "✓"

        if installed == "NOT INSTALLED":
            status = "✗ MISSING" if required else "— not installed"
        elif min_ver and _version_lt(installed, min_ver):
            status = f"⚠ outdated ({installed} < {min_ver})"
        else:
            status = f"✓ {installed}"

        print(f"  {pkg:30} {status}")

    print(f"{'=' * 60}\n")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Librarian — Tool Agent")
    parser.add_argument("--audit",         action="store_true",
                        help="Run full dependency audit")
    parser.add_argument("--cve-check",     action="store_true",
                        help="Run CVE scan only")
    parser.add_argument("--show-manifest", action="store_true",
                        help="Print approved package manifest")
    parser.add_argument("--approve",       metavar="PKG",
                        help="Add package to approved manifest")
    parser.add_argument("--approve-version", metavar="VERSION", default="",
                        help="Minimum version for --approve")
    parser.add_argument("--approve-notes",   metavar="NOTES", default="",
                        help="Notes for --approve")
    parser.add_argument("--diff",          metavar="PI_ID",
                        help="Diff Pi packages against manifest")
    args = parser.parse_args()

    if args.audit:
        results = run_full_audit()
        print(f"\nAudit complete:")
        print(f"  CVEs found:        {len(results['vulnerabilities'])}")
        print(f"  Missing required:  {len(results['missing_required'])}")
        print(f"  Unapproved pkgs:   {len(results['unapproved'])}")
        print(f"  Duplicate fns:     {len(results['duplicates'])}")
        print(f"  Suggestions filed: see suggestions.json\n")

    elif args.cve_check:
        vulns = run_cve_check()
        if vulns:
            for v in vulns:
                print(f"  [{v['severity']}] {v['package']} {v['version']} — {v['cve_id']}")
        else:
            print("  ✓ No vulnerabilities found")

    elif args.show_manifest:
        show_manifest()

    elif args.approve:
        approve_package(
            args.approve,
            min_version=args.approve_version,
            notes=args.approve_notes,
        )
        print(f"Approved: {args.approve}")

    elif args.diff:
        diff_pi(args.diff)

    else:
        parser.print_help()
