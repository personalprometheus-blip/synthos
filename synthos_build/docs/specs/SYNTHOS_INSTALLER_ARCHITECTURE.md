# SYNTHOS INSTALLER ARCHITECTURE
## Implementation-Ready Specification + Full Code

**Version:** 1.0  
**Date:** 2026-03-26  
**Authority:** SYSTEM_MANIFEST.md v2.0 (ground truth)  
**Addendum:** SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md v1.0  
**Status:** Implementation-ready

---

## 1. EXECUTIVE DECISION SUMMARY

### Architectural Decisions Made

| Decision | Choice | Rationale |
|---|---|---|
| Folder name — retail | `synthos-retail/` | Manifest v2.0 node_specific.retail_node |
| Folder name — company | `synthos-company/` | Manifest v2.0 node_specific.company_node |
| Retail installer | `install_retail.py` replaces `install.py` as the canonical installer | `install.py` violates addendum rule 1 (hardcoded paths). New file is the clean implementation. Old `install.py` is superseded. |
| Company installer | `install_company.py` — greenfield | No equivalent exists. Bootstraps company node from USB-copied files. |
| Startup mechanism | **cron only** | Manifest lifecycle section specifies cron exclusively. No systemd. `@reboot` for persistent processes, scheduled entries for agents. |
| Company DB init | **Installer directly** | Addendum 3.1 restore sequence shows DB restored at install time. `db_helpers.py` bootstraps schema on import but installer must create the file and dirs first. |
| Company services layout | **`agents/` only** | Manifest FILE_LOCATIONS company_node lists all files under `${SYNTHOS_HOME}/agents/`. No `services/` subdirectory. |
| License validation at retail install | **Collect key, write to `.env`, defer validation to `boot_sequence.py`** | Addendum 2.3: validation flow starts at boot. Installer has no network path to Vault. Key format recorded; Vault validates on first boot. |
| Company restore scope | **Phase-1 bootstrap only** | Full restore workflow (Strongbox) is pre-approved but not implemented. Installer creates restore-friendly structure; `restore.sh` is out of scope for this spec. |
| Protected files at retail rerun | **Hard list from manifest UPGRADE_RULES** | `user/.env`, `data/signals.db`, `data/backup/`, `.known_good/`, `consent_log.jsonl` |
| Shared helpers location | `installers/common/` | Direction item 6. |
| Installer log location | `logs/install.log` under respective `SYNTHOS_HOME` | Consistent with all other tool log locations. |
| Retail completion sentinel | `synthos-retail/.install_complete` | Manifest runtime_files.install_complete = `${SYNTHOS_HOME}/.install_complete` |
| Company completion sentinel | `synthos-company/.install_complete` | Same pattern, separate system. |
| Python minimum | 3.9 | Manifest EXECUTION_CONTEXT |
| pip flag | `--break-system-packages` on Pi OS | Manifest EXECUTION_CONTEXT |

### Conflict Resolution

**Conflict:** Architecture docs reference `/home/pi/synthos/` paths throughout. Addendum 1 explicitly prohibits this.  
**Resolution:** All paths in both installers derive from `Path(__file__).resolve().parent`. No hardcoded user paths anywhere.

**Conflict:** `install.py` exists and is listed as active in FILE_STATUS. The prompt requires `install_retail.py`.  
**Resolution:** `install_retail.py` is the new canonical retail installer. `install.py` is the predecessor. Both can coexist during transition. New deployments use `install_retail.py`.

**Conflict:** Manifest agent names use aliases (Bolt, Scout, Pulse) but file names are `agent1_trader.py`, etc.  
**Resolution:** File names are canonical (manifest FILE_REGISTRY). Aliases are display names only.

---

## 2. EXPLICIT INSTALLER ARCHITECTURE

### install_retail.py

**Purpose:** First-time setup and safe rerun/repair for a retail customer Pi.

**Responsibilities:**
- Preflight: validate Python version, check for required system tools
- Detect prior install state from `.install_progress.json`; resume safely
- Collect customer configuration via local web wizard (Flask on port 8080)
- Write `user/.env` safely — never overwrite without backup
- Create all required directories
- Install Python packages via pip
- Register cron schedule using resolved absolute paths
- Set timezone to America/New_York
- Bootstrap `data/signals.db` schema via `database.py`
- Run `health_check.py` to verify
- Write `.install_complete` sentinel on success

**NOT responsible for:**
- Company agent installation or configuration
- Vault key generation (operator tool, not on Pi)
- License validation (boot_sequence.py owns this)
- Network tunnel setup
- GitHub repo cloning (files arrive by USB)

**Protected files — never touched on rerun:**
- `user/.env` (backed up, never overwritten)
- `data/signals.db`
- `data/backup/`
- `.known_good/`
- `user/agreements/`
- `consent_log.jsonl`

---

### install_company.py

**Purpose:** Bootstrap the company operations Pi from USB-copied files.

**Responsibilities:**
- Preflight: validate Python version, system tools
- Detect prior install state; resume safely
- Collect company configuration via CLI prompts (no web wizard — internal tool)
- Write `company.env` safely
- Create all required directories under `synthos-company/`
- Install Python packages via pip
- Initialize `data/company.db` schema directly via `db_helpers.py`
- Register cron schedule using resolved absolute paths
- Write `.install_complete` sentinel on success

**NOT responsible for:**
- Retail agent installation or configuration
- License key generation (operator tool, separate machine)
- Customer onboarding flow
- Cloudflare tunnel setup (separate `setup_tunnel.sh`)
- Full restore workflow (Strongbox, future)

**No retail dependency:**
- No license key collected or validated
- No Alpaca / Congress.gov / trading API setup
- No `OPERATING_MODE` or `AUTONOMOUS_UNLOCK_KEY` fields
- `COMPANY_MODE=true` written to env; all agents skip license checks

---

## 3. CANONICAL DIRECTORY TREES

### synthos-retail/

```
synthos-retail/
│
├── install_retail.py              ← entry point
├── .install_progress.json         ← installer state (created by installer)
├── .install_complete              ← completion sentinel (created by installer)
├── .kill_switch                   ← runtime (created by portal/operator)
├── .pending_approvals.json        ← runtime (created by agent1)
├── .known_good/                   ← watchdog rollback snapshot [PROTECTED]
│
├── core/
│   ├── agent1_trader.py
│   ├── agent2_research.py
│   ├── agent3_sentiment.py
│   ├── database.py
│   ├── boot_sequence.py
│   ├── watchdog.py
│   ├── health_check.py
│   ├── shutdown.py
│   ├── cleanup.py
│   ├── synthos_heartbeat.py
│   ├── portal.py
│   ├── patch.py
│   ├── sync.py
│   ├── license_validator.py
│   └── uninstall.py
│
├── user/                          ← CUSTOMER-OWNED — NEVER WRITTEN BY INSTALLER AFTER CREATION
│   ├── .env                       ← [PROTECTED after first write]
│   └── agreements/                ← [PROTECTED — immutable]
│       ├── framing.txt
│       ├── operating_agreement.txt
│       └── beta_agreement.txt
│
├── data/
│   ├── signals.db                 ← [PROTECTED — never touched after creation]
│   ├── backup/                    ← [PROTECTED — daily DB backups]
│   └── license_cache.json         ← written by license_validator.py at boot
│
├── logs/
│   ├── install.log                ← installer log
│   ├── boot.log
│   ├── trader.log
│   ├── research.log
│   ├── sentiment.log
│   ├── heartbeat.log
│   ├── system.log
│   ├── health.log
│   └── crash_reports/
│
└── installers/
    └── common/
        ├── __init__.py
        ├── preflight.py
        ├── env_writer.py
        └── progress.py
```

---

### synthos-company/

```
synthos-company/
│
├── install_company.py             ← entry point
├── .install_progress.json         ← installer state (created by installer)
├── .install_complete              ← completion sentinel (created by installer)
│
├── agents/
│   ├── patches.py
│   ├── blueprint.py
│   ├── sentinel.py
│   ├── fidget.py
│   ├── librarian.py
│   ├── scoop.py
│   ├── vault.py
│   └── timekeeper.py
│
├── utils/
│   └── db_helpers.py
│
├── data/
│   ├── company.db                 ← initialized by installer
│   └── backup/                    ← Vault/Strongbox writes here
│
├── config/
│   ├── allowed_ips.json
│   ├── agent_policies.json
│   └── market_calendar.json
│
├── logs/
│   ├── install.log
│   ├── patches.log
│   ├── blueprint.log
│   ├── sentinel.log
│   ├── fidget.log
│   ├── librarian.log
│   ├── scoop.log
│   ├── vault.log
│   └── timekeeper.log
│
├── company.env                    ← company configuration [PROTECTED after first write]
│
└── installers/
    └── common/
        ├── __init__.py
        ├── preflight.py
        ├── env_writer.py
        └── progress.py
```

---

## 4. INSTALLER STATE MACHINES

### Retail State Machine

```
UNINITIALIZED
  Detection: .install_progress.json absent AND user/.env absent AND data/signals.db absent
  Transition: → PREFLIGHT on first run

PREFLIGHT
  Actions:
    - Verify Python >= 3.9
    - Verify pip available
    - Verify sqlite3 available
    - Verify cron available (warn only — not fatal)
    - Write .install_progress.json {"state": "PREFLIGHT", "started_at": <ts>}
  Pass:  → COLLECTING
  Fail:  → EXIT(1) with explicit error message

COLLECTING
  Detection: progress["state"] == "COLLECTING" OR progress absent after PREFLIGHT pass
  Actions:
    - Launch Flask wizard on port 8080
    - Collect: owner_name, owner_email, anthropic_key, alpaca_key, alpaca_secret,
               alpaca_base_url, congress_key, operating_mode, starting_capital,
               license_key, pi_id, monitor_url, monitor_token,
               sendgrid_key, alert_from, user_email,
               portal_password, gmail_user, gmail_app_password, alert_phone,
               disclaimer_accepted
    - Test API connections live (Anthropic, Alpaca, Congress.gov)
    - Write collected config to progress file (no keys — only test results)
    - On disclaimer acceptance: progress["disclaimer_accepted"] = true
  Pass:  → INSTALLING
  Fail:  Stay in COLLECTING (user fixes and resubmits)
  Rerun: If progress["disclaimer_accepted"] == true → skip to INSTALLING

INSTALLING
  Detection: progress["disclaimer_accepted"] == true AND user/.env absent
  Actions:
    - create_directories()
    - write_env_file() — safe write with backup if exists
    - install_packages()
    - bootstrap_database()
    - register_cron()
    - set_timezone()
    - progress["install_started"] = true
    - progress["install_complete"] = true after all steps
  Pass:  → VERIFYING
  Fail:  → DEGRADED (log which step failed, progress preserved for rerun)

VERIFYING
  Detection: user/.env present AND progress["install_complete"] == true AND .install_complete absent
  Actions:
    - Run health_check.py as subprocess
    - Check exit code
    - Verify all required packages importable
    - Verify cron entries present
    - Verify data/signals.db exists
  Pass:  → COMPLETE
  Fail:  → DEGRADED

DEGRADED
  Detection: user/.env present AND .install_complete absent AND health_check failed
  Actions:
    - Log all failed checks
    - Print repair instructions
    - Exit(2) — operator must investigate
  Rerun: Installer re-enters at INSTALLING (skips COLLECTING if env present)

COMPLETE
  Detection: .install_complete present
  Actions on rerun:
    - Print status summary
    - Offer repair mode (--repair flag)
    - Exit(0) without modifying anything
  Sentinel content: JSON {version, installed_at, synthos_home, pi_id}
```

---

### Company State Machine

```
UNINITIALIZED
  Detection: .install_progress.json absent AND company.env absent AND data/company.db absent
  Transition: → PREFLIGHT on first run

PREFLIGHT
  Actions:
    - Verify Python >= 3.9
    - Verify pip available
    - Verify sqlite3 available
    - Verify cron available (warn only)
    - Write .install_progress.json {"state": "PREFLIGHT", "started_at": <ts>}
  Pass:  → COLLECTING
  Fail:  → EXIT(1)

COLLECTING
  Detection: progress["state"] == "COLLECTING"
  Actions:
    - CLI prompts (no web wizard)
    - Collect: sendgrid_key, sendgrid_from, operator_email,
               command_port, installer_port, heartbeat_port,
               github_token, github_repo,
               scheduler_timeout, vault_url (optional),
               key_signing_secret
    - Write to progress file (no secrets stored in progress)
    - progress["config_complete"] = true
  Pass:  → INSTALLING
  Rerun: If progress["config_complete"] == true AND company.env absent → INSTALLING

INSTALLING
  Detection: progress["config_complete"] == true AND company.env absent
  Actions:
    - create_directories()
    - write_env_file()    ← writes company.env, COMPANY_MODE=true
    - install_packages()
    - init_company_db()   ← imports db_helpers, calls bootstrap_schema()
    - register_cron()
    - set_timezone()
    - progress["install_complete"] = true
  Pass:  → VERIFYING
  Fail:  → DEGRADED

VERIFYING
  Detection: company.env present AND progress["install_complete"] == true AND .install_complete absent
  Actions:
    - Verify all packages importable
    - Verify data/company.db exists and tables present
    - Verify all agent files present in agents/
    - Verify cron entries written
  Pass:  → COMPLETE
  Fail:  → DEGRADED

DEGRADED
  Detection: company.env present AND .install_complete absent AND verification failed
  Actions:
    - Log all failed checks
    - Exit(2)
  Rerun: Re-enters at INSTALLING

COMPLETE
  Detection: .install_complete present
  Actions on rerun:
    - Print status
    - Offer repair (--repair flag)
    - Exit(0)
  Sentinel content: JSON {version, installed_at, synthos_home}
```

---

## 5. FIRST-RUN OPERATOR COMMANDS

### Retail Pi

```bash
# 1. Insert USB, mount it
sudo mount /dev/sda1 /mnt/usb

# 2. Copy files to home directory
cp -r /mnt/usb/synthos-retail ~/synthos-retail

# 3. Enter the directory
cd ~/synthos-retail

# 4. Run the installer
python3 install_retail.py

# 5. On the same local network, open a browser to:
#    http://<pi-hostname>.local:8080
#    Complete the 7-step wizard.

# 6. Verify success
cat .install_complete
```

**Rerun / repair:**
```bash
cd ~/synthos-retail
python3 install_retail.py --repair
```

**Installer log:**
```bash
tail -f logs/install.log
```

---

### Company Pi

```bash
# 1. Insert USB, mount it
sudo mount /dev/sda1 /mnt/usb

# 2. Copy files to home directory
cp -r /mnt/usb/synthos-company ~/synthos-company

# 3. Enter the directory
cd ~/synthos-company

# 4. Run the installer (CLI — no browser needed)
python3 install_company.py

# 5. Follow CLI prompts to enter company configuration.

# 6. Verify success
cat .install_complete
```

**Rerun / repair:**
```bash
cd ~/synthos-company
python3 install_company.py --repair
```

**Installer log:**
```bash
tail -f logs/install.log
```

---

## 6. REQUIRED CONFIG / ENV SCHEMA

### Retail — Required Variables [R]

| Key | Source | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Entered by customer | Tested live during install |
| `ALPACA_API_KEY` | Entered by customer | Tested live during install |
| `ALPACA_SECRET_KEY` | Entered by customer | Tested live during install |
| `ALPACA_BASE_URL` | Entered or default | Default: `https://paper-api.alpaca.markets` |
| `TRADING_MODE` | Entered or default | Default: `PAPER` |
| `CONGRESS_API_KEY` | Entered by customer | Tested live during install |
| `OPERATING_MODE` | Entered or default | Default: `SUPERVISED` |
| `STARTING_CAPITAL` | Entered by customer | Integer, dollars |
| `OWNER_NAME` | Entered by customer | |
| `OWNER_EMAIL` | Entered by customer | |
| `PORTAL_SECRET_KEY` | **Generated by installer** | `secrets.token_hex(32)` |
| `LICENSE_KEY` | Entered by customer | Format validated at install; Vault validates at boot |
| `PI_ID` | Entered or default | Default: `synthos-pi-1` |

### Retail — Optional Variables [O]

| Key | Source | Notes |
|---|---|---|
| `MONITOR_URL` | Entered by customer | Heartbeat target |
| `MONITOR_TOKEN` | Entered or default | Default: `changeme` |
| `PI_LABEL` | Entered by customer | Display name |
| `PI_EMAIL` | Entered by customer | |
| `SENDGRID_API_KEY` | Entered by customer | Protective exit emails |
| `ALERT_FROM` | Entered by customer | |
| `USER_EMAIL` | Entered by customer | |
| `PORTAL_PASSWORD` | Entered or blank | Blank = open LAN access |
| `PORTAL_PORT` | Default `5001` | |
| `GMAIL_USER` | Entered by customer | SMS crash alerts |
| `GMAIL_APP_PASSWORD` | Entered by customer | |
| `ALERT_PHONE` | Entered by customer | |
| `CARRIER_GATEWAY` | Default `tmomail.net` | |
| `SUPPORT_EMAIL` | Default | `synthos.signal@gmail.com` |
| `GITHUB_TOKEN` | Entered by customer | For sync.py |
| `AUTONOMOUS_UNLOCK_KEY` | Operator-issued | Only if `OPERATING_MODE=AUTONOMOUS` |

---

### Company — Required Variables [R]

| Key | Source | Notes |
|---|---|---|
| `COMPANY_MODE` | **Written by installer** | Always `true` |
| `SENDGRID_API_KEY` | Entered by operator | Scoop delivery |
| `SENDGRID_FROM` | Entered by operator | Verified sender |
| `OPERATOR_EMAIL` | Entered by operator | Internal alerts destination |
| `KEY_SIGNING_SECRET` | Entered by operator | HMAC seed — never logs, never prints |
| `DATABASE_PATH` | **Written by installer** | Resolved absolute path |

### Company — Optional Variables [O]

| Key | Source | Notes |
|---|---|---|
| `COMMAND_PORT` | Default `5002` | |
| `INSTALLER_PORT` | Default `5003` | |
| `HEARTBEAT_PORT` | Default `5004` | |
| `SCHEDULER_TIMEOUT_SEC` | Default `120` | |
| `MARKET_HOURS_START` | Default `0930` | |
| `MARKET_HOURS_END` | Default `1600` | |
| `MARKET_TIMEZONE` | Default `US/Eastern` | |
| `GITHUB_TOKEN` | Entered by operator | For pushing customer forks |
| `GITHUB_REPO` | Entered by operator | |
| `VAULT_URL` | Entered by operator | Self-reference; retail Pis call this |

---

## 7. ACCEPTANCE CRITERIA

### Retail Install — COMPLETE when ALL of the following are true:

- [ ] `user/.env` exists and contains all [R] keys with non-empty values
- [ ] `PORTAL_SECRET_KEY` present and is a 64-char hex string
- [ ] `data/signals.db` exists and all required tables present
- [ ] All core files present in `core/`
- [ ] All required packages importable: `flask`, `requests`, `alpaca-trade-api`, `anthropic`, `python-dotenv`, `sendgrid`
- [ ] Cron entries written: `@reboot boot_sequence`, `@reboot watchdog`, `@reboot portal`, `55 3 * * 6 shutdown`, `0 4 * * 6 sudo reboot`
- [ ] `health_check.py` exits 0
- [ ] `.install_complete` sentinel written with valid JSON

### Company Install — COMPLETE when ALL of the following are true:

- [ ] `company.env` exists and contains all [R] keys with non-empty values
- [ ] `COMPANY_MODE=true` present in `company.env`
- [ ] `data/company.db` exists and all schema tables present
- [ ] All agent files present in `agents/`
- [ ] `utils/db_helpers.py` present
- [ ] All required packages importable: `flask`, `requests`, `anthropic`, `python-dotenv`, `sendgrid`
- [ ] Cron entries written for all company agents
- [ ] `.install_complete` sentinel written with valid JSON

---

## 8. FILE PLAN

| File | Action | Purpose |
|---|---|---|
| `synthos-retail/install_retail.py` | CREATE | Retail installer entry point |
| `synthos-company/install_company.py` | CREATE | Company installer entry point |
| `synthos-retail/installers/common/__init__.py` | CREATE | Package marker |
| `synthos-retail/installers/common/preflight.py` | CREATE | Shared preflight checks |
| `synthos-retail/installers/common/env_writer.py` | CREATE | Safe env file writer |
| `synthos-retail/installers/common/progress.py` | CREATE | Install progress state manager |
| `synthos-company/installers/common/` | SYMLINK or COPY | Same helpers, company side |

> **Note:** `installers/common/` is duplicated into both deployment trees. These are USB-copied deployments — no shared filesystem between retail and company Pi. The common helpers are identical files in both trees.

---

## 9. IMPLEMENTATION

See attached files:
- `install_retail.py`
- `install_company.py`
- `installers/common/preflight.py`
- `installers/common/env_writer.py`
- `installers/common/progress.py`

---

## 10. OPERATOR RUNBOOK

### Install from USB

**Retail:**
```bash
sudo mount /dev/sda1 /mnt/usb
cp -r /mnt/usb/synthos-retail ~/synthos-retail
cd ~/synthos-retail
python3 install_retail.py
# Open browser on same LAN: http://<pi>.local:8080
# Complete wizard. Wait for "Installation complete."
cat .install_complete   # confirm sentinel exists
```

**Company:**
```bash
sudo mount /dev/sda1 /mnt/usb
cp -r /mnt/usb/synthos-company ~/synthos-company
cd ~/synthos-company
python3 install_company.py
# Follow CLI prompts.
cat .install_complete   # confirm sentinel exists
```

### Rerun / Repair

```bash
python3 install_retail.py --repair   # retail
python3 install_company.py --repair  # company
```

Repair mode re-runs INSTALLING and VERIFYING. Skips COLLECTING if `user/.env` exists. Never overwrites protected files.

### Where Logs Go

| Log | Path |
|---|---|
| Retail installer | `synthos-retail/logs/install.log` |
| Company installer | `synthos-company/logs/install.log` |
| Retail boot | `synthos-retail/logs/boot.log` |
| Retail health | `synthos-retail/logs/health.log` |

### Verify Success

```bash
# Retail
cat synthos-retail/.install_complete

# Company
cat synthos-company/.install_complete

# Both return JSON: {version, installed_at, synthos_home, ...}
```

---

## 11. OPEN RISKS / FOLLOW-UP ITEMS

| Item | Risk | Decision Made |
|---|---|---|
| `install.py` still present in repo | Operators may run the old installer by mistake | Decided: `install_retail.py` is canonical. Old `install.py` should be renamed `install.py.deprecated` after this ships. |
| License validation at install | Installer collects key but cannot validate it (no Vault yet) | Decided: collect and write to `.env`. `boot_sequence.py` validates on first boot. Customer sees clear error if key is wrong — not a silent failure. |
| `first_run.sh` hardcodes `/home/pi/synthos` | Flagged in manifest as experimental/known issue | Out of scope for installers. Separate refactor task. |
| Company restore workflow | Addendum 3.1 describes `restore.sh` but it doesn't exist | Out of scope for this spec. Strongbox (Agent 12) owns this when implemented. |
| `seed_backlog.py` for company | Should run after company install to bootstrap agent suggestion queue | Not wired into installer yet. Operator runs manually after install: `python3 agents/../seed_backlog.py` |
| Pi platform check | Installer warns on non-Pi but continues | Correct behavior per manifest EXECUTION_CONTEXT. |
