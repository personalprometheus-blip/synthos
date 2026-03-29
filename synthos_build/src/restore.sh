#!/usr/bin/env bash
# restore.sh — Company Pi Fast Restore
# Synthos Operations Spec Addendum 1 §3.1
#
# Restores the Company Pi from a Strongbox backup archive.
# Target: all company agents running within 5 minutes of invocation.
#
# Usage:
#   bash restore.sh <backup_file>
#   bash restore.sh <backup_file> --dry-run
#
# <backup_file> is either:
#   synthos_backup_company-pi_YYYY-MM-DD.tar.gz.enc   (encrypted — asks for key)
#   synthos_backup_company-pi_YYYY-MM-DD.tar.gz       (pre-decrypted by Strongbox)
#
# To pre-decrypt with Strongbox on another machine:
#   python3 strongbox.py --restore company-pi [--date YYYY-MM-DD]
#   # outputs: data/restore_staging/company-pi/synthos_backup_company-pi_DATE.tar.gz
#   # copy that .tar.gz to the new Pi and run this script
#
# What this script does (Addendum 3.1 §4):
#   a. Extracts archive to ~/synthos-company/
#   b. Restores company.db from backup
#   c. Restores .env from backup (project lead provides BACKUP_ENCRYPTION_KEY for .enc)
#   d. Sets correct permissions
#   e. Installs Python dependencies
#   f. Registers cron entry for Strongbox daily backup
#   g. Starts company agents
#
# Prerequisites (fresh Pi OS Lite):
#   sudo apt-get update && sudo apt-get install -y python3 python3-pip
#   # This script installs cryptography and other deps automatically.
#
# No license key required. Company Pi uses COMPANY_MODE=true in .env,
# which bypasses all license validation (Addendum 3.1 §3.2).

set -euo pipefail

# ── ARGS ──────────────────────────────────────────────────────────────────────

BACKUP_FILE="${1:-}"
DRY_RUN=false
if [[ "${2:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

if [[ -z "$BACKUP_FILE" ]]; then
    echo "Usage: bash restore.sh <backup_file> [--dry-run]"
    echo ""
    echo "  backup_file: path to .tar.gz or .tar.gz.enc archive"
    echo "  --dry-run:   show what would happen without making changes"
    exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "ERROR: Backup file not found: $BACKUP_FILE"
    exit 1
fi

# ── CONFIG ────────────────────────────────────────────────────────────────────

RESTORE_TARGET="${HOME}/synthos-company"
RESTORE_LOG="${HOME}/restore_$(date +%Y%m%d_%H%M%S).log"
PYTHON="python3"

# ── LOGGING ───────────────────────────────────────────────────────────────────

log() {
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] $*" | tee -a "$RESTORE_LOG"
}

log_step() {
    echo "" | tee -a "$RESTORE_LOG"
    log "=== $* ==="
}

log_dry() {
    log "[DRY RUN] $*"
}

die() {
    log "FATAL: $*"
    echo ""
    echo "Restore failed. Log: $RESTORE_LOG"
    exit 1
}

# ── PREFLIGHT ─────────────────────────────────────────────────────────────────

log_step "Preflight checks"

log "Backup file : $BACKUP_FILE"
log "Target dir  : $RESTORE_TARGET"
log "Dry run     : $DRY_RUN"
log "Log file    : $RESTORE_LOG"

# Python 3 required
if ! command -v "$PYTHON" &>/dev/null; then
    die "python3 not found. Run: sudo apt-get install -y python3"
fi
PYTHON_VER=$("$PYTHON" --version 2>&1)
log "Python      : $PYTHON_VER"

# If target dir already exists, back it up first
if [[ -d "$RESTORE_TARGET" ]]; then
    EXISTING_BACKUP="${RESTORE_TARGET}.pre_restore_$(date +%Y%m%d_%H%M%S)"
    log "WARNING: $RESTORE_TARGET already exists."
    log "         Moving to: $EXISTING_BACKUP"
    if [[ "$DRY_RUN" == "false" ]]; then
        mv "$RESTORE_TARGET" "$EXISTING_BACKUP"
    else
        log_dry "Would move $RESTORE_TARGET → $EXISTING_BACKUP"
    fi
fi

# ── DECRYPT (if .enc) ────────────────────────────────────────────────────────

ARCHIVE_TO_EXTRACT="$BACKUP_FILE"

if [[ "$BACKUP_FILE" == *.enc ]]; then
    log_step "Decrypting archive"

    # Ensure cryptography is available
    if ! "$PYTHON" -c "from cryptography.fernet import Fernet" 2>/dev/null; then
        log "Installing cryptography..."
        if [[ "$DRY_RUN" == "false" ]]; then
            pip3 install --quiet cryptography --break-system-packages \
                || pip3 install --quiet cryptography \
                || die "Failed to install cryptography. Run manually: pip3 install cryptography"
        else
            log_dry "Would install: cryptography"
        fi
    fi

    # Get encryption key
    if [[ -z "${BACKUP_ENCRYPTION_KEY:-}" ]]; then
        echo ""
        echo "Enter the BACKUP_ENCRYPTION_KEY (the Fernet key held by the project lead):"
        read -r -s BACKUP_ENCRYPTION_KEY
        echo ""
        if [[ -z "$BACKUP_ENCRYPTION_KEY" ]]; then
            die "BACKUP_ENCRYPTION_KEY is required to decrypt this archive."
        fi
    fi

    DECRYPTED_ARCHIVE="${BACKUP_FILE%.enc}"
    log "Decrypting: $BACKUP_FILE → $DECRYPTED_ARCHIVE"

    if [[ "$DRY_RUN" == "false" ]]; then
        "$PYTHON" - <<PYEOF
import sys
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

key = "$BACKUP_ENCRYPTION_KEY".encode()
try:
    f = Fernet(key)
except Exception as e:
    print(f"ERROR: Invalid BACKUP_ENCRYPTION_KEY: {e}", file=sys.stderr)
    sys.exit(1)

enc_path = Path("$BACKUP_FILE")
out_path = Path("$DECRYPTED_ARCHIVE")

try:
    ciphertext = enc_path.read_bytes()
    plaintext = f.decrypt(ciphertext)
    out_path.write_bytes(plaintext)
    print(f"Decrypted: {enc_path.name} → {out_path.name} ({len(plaintext)//1024} KB)")
except InvalidToken:
    print("ERROR: Decryption failed — wrong BACKUP_ENCRYPTION_KEY or corrupt archive.",
          file=sys.stderr)
    sys.exit(1)
PYEOF
    else
        log_dry "Would decrypt: $BACKUP_FILE → $DECRYPTED_ARCHIVE"
    fi

    ARCHIVE_TO_EXTRACT="$DECRYPTED_ARCHIVE"
    log "Decryption complete."
fi

# ── EXTRACT ARCHIVE ───────────────────────────────────────────────────────────

log_step "Extracting archive"
log "Source : $ARCHIVE_TO_EXTRACT"
log "Target : $RESTORE_TARGET"

if [[ "$DRY_RUN" == "false" ]]; then
    mkdir -p "$RESTORE_TARGET"
    tar -xzf "$ARCHIVE_TO_EXTRACT" -C "$RESTORE_TARGET"
    log "Extraction complete."
    # List top-level contents for confirmation
    log "Extracted contents:"
    ls "$RESTORE_TARGET" | while read -r item; do log "  $item"; done
else
    log_dry "Would mkdir -p $RESTORE_TARGET"
    log_dry "Would tar -xzf $ARCHIVE_TO_EXTRACT -C $RESTORE_TARGET"
fi

# ── RESTORE DIRECTORY STRUCTURE ───────────────────────────────────────────────

log_step "Creating required directories"

for dir in data logs ".backup_staging" "logs/crash_reports"; do
    full="${RESTORE_TARGET}/${dir}"
    if [[ "$DRY_RUN" == "false" ]]; then
        mkdir -p "$full"
        log "  mkdir: $full"
    else
        log_dry "Would mkdir -p $full"
    fi
done

# ── VERIFY CRITICAL FILES ─────────────────────────────────────────────────────

log_step "Verifying critical restore files"

MISSING=()
for expected in "data/company.db" "user/.env"; do
    full="${RESTORE_TARGET}/${expected}"
    if [[ -f "$full" ]]; then
        SIZE=$(du -sh "$full" 2>/dev/null | cut -f1)
        log "  OK: $expected ($SIZE)"
    else
        log "  MISSING: $expected"
        MISSING+=("$expected")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    log ""
    log "WARNING: ${#MISSING[@]} expected file(s) not found in archive: ${MISSING[*]}"
    log "         The restore will continue but may be incomplete."
    log "         If user/.env is missing, agents will not start."
    log "         Manually copy a valid .env to ${RESTORE_TARGET}/user/.env before starting."
fi

# ── PERMISSIONS ───────────────────────────────────────────────────────────────

log_step "Setting permissions"

if [[ "$DRY_RUN" == "false" ]]; then
    # .env must be owner-readable only — contains API keys
    if [[ -f "${RESTORE_TARGET}/user/.env" ]]; then
        chmod 600 "${RESTORE_TARGET}/user/.env"
        log "  chmod 600 user/.env"
    fi

    # DB file: owner rw only
    if [[ -f "${RESTORE_TARGET}/data/company.db" ]]; then
        chmod 640 "${RESTORE_TARGET}/data/company.db"
        log "  chmod 640 data/company.db"
    fi

    # Agent scripts: executable
    if [[ -d "${RESTORE_TARGET}/agents" ]]; then
        chmod 750 "${RESTORE_TARGET}/agents"
        find "${RESTORE_TARGET}/agents" -name "*.py" -exec chmod 640 {} \;
        find "${RESTORE_TARGET}/agents" -name "*.sh" -exec chmod 750 {} \;
        log "  chmod applied to agents/"
    fi
else
    log_dry "Would chmod 600 user/.env"
    log_dry "Would chmod 640 data/company.db"
    log_dry "Would chmod 750/640 agents/"
fi

# ── PYTHON DEPENDENCIES ───────────────────────────────────────────────────────

log_step "Installing Python dependencies"

PACKAGES=(
    "anthropic"
    "boto3"
    "cryptography"
    "flask"
    "python-dotenv"
    "requests"
    "schedule"
    "sendgrid"
)

if [[ "$DRY_RUN" == "false" ]]; then
    log "Running pip3 install for ${#PACKAGES[@]} package(s)..."
    pip3 install --quiet "${PACKAGES[@]}" --break-system-packages \
        || pip3 install --quiet "${PACKAGES[@]}" \
        || log "WARNING: pip3 install failed — agents may fail on import. Install manually."
    log "Dependencies installed."
else
    log_dry "Would pip3 install: ${PACKAGES[*]}"
fi

# ── CRON REGISTRATION ─────────────────────────────────────────────────────────

log_step "Registering cron entries"

AGENT_DIR_PATH="${RESTORE_TARGET}/agents"
LOG_DIR_PATH="${RESTORE_TARGET}/logs"

# Strongbox: daily backup at 2am ET (UTC−5 standard / UTC−4 daylight)
# Using 7am UTC covers both EST (2am ET) and EDT (3am ET)
STRONGBOX_CRON="0 7 * * * ${PYTHON} ${AGENT_DIR_PATH}/strongbox.py >> ${LOG_DIR_PATH}/strongbox.log 2>&1"

register_cron() {
    local entry="$1"
    local label="$2"
    if crontab -l 2>/dev/null | grep -qF "$entry"; then
        log "  Already registered: $label"
    else
        (crontab -l 2>/dev/null; echo "$entry") | crontab -
        log "  Registered: $label"
    fi
}

if [[ "$DRY_RUN" == "false" ]]; then
    register_cron "$STRONGBOX_CRON" "Strongbox daily backup (2am ET)"
else
    log_dry "Would register cron: $STRONGBOX_CRON"
fi

# ── START AGENTS ──────────────────────────────────────────────────────────────

log_step "Starting company agents"

# Verify .env exists before attempting agent start
ENV_FILE="${RESTORE_TARGET}/user/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "SKIP: user/.env not found — cannot start agents."
    log "      Copy a valid .env to ${ENV_FILE} and start agents manually:"
    log "      cd ${AGENT_DIR_PATH} && python3 patches.py &"
    log "      cd ${AGENT_DIR_PATH} && python3 sentinel.py &"
else
    # Agents to start. Order: monitoring first, then operations.
    declare -A AGENTS=(
        ["sentinel.py"]="Sentinel — customer Pi heartbeat monitor"
        ["patches.py"]="Patches — log scanning, morning report"
        ["vault.py"]="Vault — license compliance"
        ["scoop.py"]="Scoop — alert delivery"
        ["fidget.py"]="Fidget — cost efficiency monitor"
        ["timekeeper.py"]="Timekeeper — resource scheduler"
    )

    STARTED=0
    SKIPPED=0

    for agent in sentinel.py patches.py vault.py scoop.py fidget.py timekeeper.py; do
        agent_path="${AGENT_DIR_PATH}/${agent}"
        agent_log="${LOG_DIR_PATH}/${agent%.py}.log"

        if [[ ! -f "$agent_path" ]]; then
            log "  SKIP: $agent — not found in ${AGENT_DIR_PATH}/"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi

        if [[ "$DRY_RUN" == "false" ]]; then
            "$PYTHON" "$agent_path" >> "$agent_log" 2>&1 &
            PID=$!
            log "  Started: $agent (PID $PID) — log: $agent_log"
            STARTED=$((STARTED + 1))
            # Brief pause to let each agent initialise before the next
            sleep 2
        else
            log_dry "Would start: $PYTHON $agent_path >> $agent_log 2>&1 &"
        fi
    done

    if [[ "$DRY_RUN" == "false" ]]; then
        log ""
        log "Agents started: $STARTED | Skipped (not found): $SKIPPED"
        if [[ $SKIPPED -gt 0 ]]; then
            log "NOTE: Skipped agents have not been built yet or were not in this archive."
            log "      They can be started manually once their files are in place."
        fi
    fi
fi

# ── SUMMARY ───────────────────────────────────────────────────────────────────

log_step "Restore complete"

if [[ "$DRY_RUN" == "false" ]]; then
    log "Company Pi restored to: $RESTORE_TARGET"
    log "Log file: $RESTORE_LOG"
    log ""
    log "Next steps:"
    log "  1. Verify agents are running:  ps aux | grep python3"
    log "  2. Check Strongbox status:     python3 ${AGENT_DIR_PATH}/strongbox.py --status"
    log "  3. Review logs:                ls ${LOG_DIR_PATH}/"
    log "  4. Confirm COMPANY_MODE=true is set in ${ENV_FILE}"
    log ""
    log "No license key required — COMPANY_MODE bypasses all license checks."
else
    log "[DRY RUN] No changes were made."
    log "Remove --dry-run to perform the restore."
fi
