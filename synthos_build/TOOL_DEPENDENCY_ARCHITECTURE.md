# SYNTHOS TECHNICAL ARCHITECTURE
## Tool Dependency Architecture

**Document Version:** 1.0
**Date:** March 2026
**Audience:** Engineers, AI agents building/maintaining the system
**Scope:** All operational tools on Retail Pi and Company Pi

---

## Design Alignment

| Decision | Synthos Principle |
|---|---|
| All tools load config from `.env` exclusively — no hardcoded values | Fully self-contained system |
| Every tool resolves its own dependencies before acting | No assumption of clean state |
| All tools are safe to re-run; prior execution is detected and handled | Idempotent execution |
| Exit codes and structured logs are mandatory — no silent failures | Deterministic and reproducible behavior |
| Tools write to `logs/` and `data/` only — never to `user/` | User data must never be modified or lost |
| All failures surface to watchdog or installer via exit code + log | Automatic recovery from failure |

---

## Tool Classification Model

### Classes

| Class | Description | Examples |
|---|---|---|
| **Bootstrap** | Run once at install or first boot; establish preconditions | `install_retail.py`, `boot_sequence.py` |
| **Runtime** | Run continuously or on schedule during normal operation | `watchdog.py`, `synthos_heartbeat.py`, `portal.py` |
| **Maintenance** | Run on schedule to preserve system integrity | `health_check.py`, `cleanup.py`, `shutdown.py`, `sync.py` |
| **Repair** | Run on failure or operator trigger to restore valid state | `patches.py`, `patch.py`, `uninstall.py` |
| **Security** | Gate access and validate authorization state | `license_validator.py`, `generate_unlock_key.py` |
| **Data** | Provide shared database access; no standalone execution path | `database.py`, `db_helpers.py` |
| **Observability** | Monitor system state; report without modifying it | `synthos_monitor.py` |

### Classification Rules

- A tool belongs to exactly one class
- Class determines allowed filesystem write scope (see Security Model)
- Class determines valid invocation lifecycle phases (see Lifecycle Model)
- Data-class tools are never invoked directly; they are imported only
- A tool that spans two classes must be split

---

## Execution Contract

All tools except Data-class must conform to this contract without exception.

### Input Handling
- All configuration read from `.env` via `python-dotenv` at startup
- No tool accepts credentials as CLI arguments
- Optional CLI flags permitted for mode control only (e.g., `--status`, `--dry-run`)
- If a required env var is absent: log the missing key, exit `2`

### Output Behavior
- All output written to the tool's designated log file under `logs/`
- No tool writes to stdout except `portal.py` (web server) and operator-facing CLI tools
- Structured log entries only (see Observability Model)
- No tool modifies files in `user/` under any condition

### Logging
- Every tool instantiates a named logger: `log = logging.getLogger('<tool_name>')`
- Log format: `[YYYY-MM-DD HH:MM:SS] LEVEL <tool_name>: message`
- Log file: `/home/pi/synthos/logs/<tool_name>.log`
- Minimum log events: startup, each major action, any failure, exit condition

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success — all operations completed as expected |
| `1` | Operational failure — tool ran but a required action failed |
| `2` | Configuration error — missing or invalid env var or required file |
| `3` | Dependency error — required package or internal module unavailable |
| `4` | State conflict — system state prevents safe execution |

### Environment Usage
- `.env` loaded at top of `main()` before any other operation
- All env vars assigned to named constants immediately after load
- No tool passes raw env vars into subprocesses

---

## Lifecycle Model

| Phase | Trigger | Permitted Classes |
|---|---|---|
| **Install** | Operator runs `install_retail.py` | Bootstrap, Security, Data |
| **Boot** | `@reboot` cron fires `boot_sequence.py` | Bootstrap, Maintenance, Security, Data |
| **Runtime** | Cron schedule or `boot_sequence.py` subprocess | Runtime, Security, Observability, Data |
| **Maintenance window** | Scheduled cron (e.g., Saturday 3:55 AM) | Maintenance, Data |
| **Repair** | Watchdog trigger or operator invocation | Repair, Maintenance, Security, Data |
| **Shutdown** | Scheduled cron or operator trigger | Maintenance, Data |

- No Repair-class tool runs during Install phase
- No Bootstrap-class tool runs during Runtime phase without explicit operator invocation
- Security-class tools (`license_validator.py`) are invoked by Runtime tools on startup and periodically — not by cron directly

---

## Dependency Model

### Layers

```
Layer 1 — System (apt):     python3, python3-pip, git, sqlite3, cron
Layer 2 — Python packages:  anthropic, alpaca-trade-api, python-dotenv,
                             requests, feedparser, flask, sendgrid
Layer 3 — Internal modules: database.py, db_helpers.py
```

### Validation Rules
- Layer 1 is validated by the installer PREFLIGHT state before any Layer 2 installation
- Layer 2 packages are import-tested individually after pip install — not assumed present
- Layer 3 modules are never pip-installed; their presence is verified by file existence check at tool startup
- A tool that imports a Layer 3 module must handle `ImportError` explicitly and exit `3`

### Declaring Dependencies
Each tool's module docstring declares its dependency layer requirements:

```python
"""
DEPENDENCIES:
  Layer 2: requests, python-dotenv
  Layer 3: database.py
"""
```

---

## State & Idempotency Model

- Every tool that modifies system state writes a sentinel or status record before acting
- On re-run, the tool checks for the sentinel and skips completed actions
- Sentinels are stored in `.install_progress.json` (installer tools) or the `system_log` table (runtime tools)
- A tool that cannot determine prior execution state must treat re-entry as a fresh run and be safe to do so
- No tool assumes the previous run completed successfully

### Partial Execution
- If a tool is interrupted mid-run, the sentinel from the incomplete action is absent
- On re-entry, the absent sentinel causes the action to re-execute
- All tool actions must be safe to repeat; non-idempotent actions (e.g., DB writes) use `INSERT OR IGNORE` or equivalent guards

---

## Error Handling Model

### Failure Reporting
- Every caught exception is logged at `ERROR` level with the exception message
- Exit code is set before `sys.exit()` — never exit without a code
- Unhandled exceptions must not produce silent exits; wrap `main()` in a top-level `try/except` that logs and exits `1`

### Failure Propagation

| Tool Class | Failure Destination |
|---|---|
| Bootstrap | Installer state → DEGRADED via exit code |
| Runtime | Watchdog detects non-zero exit or process absence; triggers restart or alert |
| Maintenance | Logged to `system_log`; Patches reads on next scan |
| Repair | Operator-visible via log; no automatic re-trigger |
| Security | Calling tool receives exit `1`; calling tool halts and logs |
| Observability | Logged only; no propagation |

### Retry vs Degrade
- Runtime tools: watchdog retries up to 3 times before escalating to DEGRADED state
- Maintenance tools: single attempt; failure logged; no automatic retry
- Repair tools: single attempt; if repair fails, operator notification is written to suggestions log
- Security tools: no retry; failure is a hard stop

---

## Integration Points

### Installer State Machine
- Bootstrap-class tools are invoked by the installer during PREFLIGHT and INSTALLING states
- `health_check.py` is the sole tool invoked during VERIFYING state
- A non-zero exit from any installer-invoked tool blocks the transition to the next state
- The installer writes all invocation results to `.install_progress.json`

### Watchdog / Monitor
- `watchdog.py` monitors all Runtime-class tool processes by PID
- Tools register their PID in `logs/<tool_name>.pid` on startup; remove it on clean exit
- A missing PID file with no running process triggers watchdog restart logic
- `synthos_monitor.py` reads `system_log` and tool log files only — never invokes tools

### Database Layer
- All write operations go through `database.py` (Retail Pi) or `db_helpers.py` (Company Pi)
- No tool opens a raw `sqlite3` connection for writes outside these modules
- Read operations may use direct connections in read-only mode (`uri=True`, `?mode=ro`)
- Data-class modules are the exclusive owners of schema migrations

---

## Security Model

### Credential Handling
- All credentials reside in `.env` exclusively
- `.env` permissions must be `600`; tools verify this on startup and log a warning if not
- No credential is logged, printed, or passed as a subprocess argument
- `generate_unlock_key.py` runs on operator hardware only — never on a deployed Pi

### Filesystem Access

| Class | Permitted Write Scope |
|---|---|
| Bootstrap | `logs/`, `data/`, project root sentinels |
| Runtime | `logs/`, `data/` |
| Maintenance | `logs/`, `data/`, `data/backup/` |
| Repair | `logs/`, `data/`, `core/` (with explicit operator flag only) |
| Security | `logs/` only |
| Data | `data/` only |
| Observability | `logs/` only |

- `user/` is never written by any tool under any condition
- `core/` is only written by Repair-class tools and only when invoked with `--repair` flag

### License Validation
- `license_validator.py` is called by `boot_sequence.py` before any agent is started
- If validation fails, boot halts; no agent process is spawned
- License state is not cached between boots; validation runs fresh on every boot
- Validation result is written to `system_log` with timestamp

---

## Observability Model

### Log Format
```
[YYYY-MM-DD HH:MM:SS] LEVEL <tool_name>: message
```

### Log Locations
```
/home/pi/synthos/logs/<tool_name>.log   — per-tool operational log
/home/pi/synthos/logs/boot.log          — boot sequence aggregate
/home/pi/synthos/logs/crash_reports/   — watchdog crash snapshots
```

### Health Reporting
- `health_check.py` is the canonical health reporter; all other tools defer to it
- Runtime tools write a heartbeat record to `system_log` on each successful cycle
- `synthos_heartbeat.py` reads `system_log` to compose the heartbeat POST — it does not generate health data itself

### Metrics
- No external metrics system; all observability is log and DB-based
- `synthos_monitor.py` queries `system_log` and presents aggregated status; it does not write

---

## Standard Interface Definition

All non-Data-class tools must implement this structure:

```python
"""
<tool_name>.py — <one-line description>
Synthos · v<version>

DEPENDENCIES:
  Layer 2: <packages>
  Layer 3: <internal modules>
"""

import os
import sys
import logging
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR     = os.path.join(PROJECT_DIR, 'logs')
ENV_PATH    = os.path.join(PROJECT_DIR, '.env')

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, '<tool_name>.log'))]
)
log = logging.getLogger('<tool_name>')


def main():
    load_dotenv(ENV_PATH)

    REQUIRED_VAR = os.environ.get('REQUIRED_VAR')
    if not REQUIRED_VAR:
        log.error("REQUIRED_VAR not set in .env")
        sys.exit(2)

    log.info("<tool_name> starting")

    try:
        # tool logic
        pass
    except Exception as e:
        log.error(f"Unhandled failure: {e}")
        sys.exit(1)

    log.info("<tool_name> complete")
    sys.exit(0)


if __name__ == '__main__':
    main()
```

---

## Enforcement Model

- Blueprint validates new tool submissions against this architecture before marking them ready for review
- Patches scans existing tools for conformance on each weekly maintenance cycle
- Nonconformance findings are written as suggestions to `suggestions.json` with category `arch_violation`
- A tool flagged for `arch_violation` is blocked from the deployment pipeline until resolved
- The standard interface definition above is the reference; no tool may substitute an alternative pattern without project lead approval recorded in the suggestions audit trail

---

**Version:** 1.0
**Last Updated:** March 2026
**Next Review:** June 2026
