"""
install_v1.0.py — Synthos Guided Installer
Synthos · v1.0

Designed for End Users setting up Synthos on a Raspberry Pi 5.
Requires no terminal knowledge beyond running one command.

USAGE (End User):
  python3 install.py

This script:
  1. Runs a brief terminal pre-flight check
  2. Launches a local web UI on port 8080
  3. End user opens browser: http://raspberrypi.local:8080
  4. Web form collects all configuration
  5. Tests each API connection live before saving
  6. Writes .env, creates folders, installs packages
  7. Sets up cron schedule
  8. Runs health_check.py to verify
  9. Shuts down — Synthos is ready

SECURITY NOTE (v1.0):
  The web UI runs on HTTP with no authentication.
  It is only accessible on your local WiFi network.
  Do not run the installer on a public or untrusted network.
  Authentication and HTTPS are planned for a future release.
  See: SECURITY_TASKS in VERSION_MANIFEST.txt

Developer:  Patrick McGuire
Support:    synthos.signal@gmail.com
Version:    1.0
"""

import os
import sys
import json
import time
import socket
import signal
import secrets
import logging
import platform
import subprocess
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote_plus

SYNTHOS_VERSION = "1.0"

# ── CONFIG ────────────────────────────────────────────────────────────────
PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
ENV_PATH      = os.path.join(PROJECT_DIR, '.env')
LOG_DIR       = os.path.join(PROJECT_DIR, 'logs')
BACKUP_DIR    = os.path.join(PROJECT_DIR, 'backups')
PORT          = 8080
SUPPORT_EMAIL = "synthos.signal@gmail.com"

REQUIRED_PACKAGES = [
    'anthropic',
    'alpaca-trade-api',
    'python-dotenv',
    'requests',
    'feedparser',
    'flask',
    'sendgrid',
]

CARRIER_GATEWAYS = {
    'T-Mobile':    'tmomail.net',
    'AT&T':        'txt.att.net',
    'Verizon':     'vtext.com',
    'Sprint':      'messaging.sprintpcs.com',
    'US Cellular': 'email.uscc.net',
    'Xfinity Mobile': 'vtext.com',
    'Other':       '',
}

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s install: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('install')

# ── INSTALLATION STATE ────────────────────────────────────────────────────
# Shared state between web server and installer logic

state = {
    'step':           'welcome',      # current UI step
    'config':         {},             # collected configuration
    'test_results':   {},             # connection test results
    'install_log':    [],             # log lines for UI display
    'install_done':   False,
    'install_error':  None,
    'shutdown':       False,
    'reboot_countdown': False,
    'install_started':  False,
}

PROGRESS_FILE = os.path.join(PROJECT_DIR, '.install_progress.json')

def save_progress():
    """Save current config to disk so a crash doesn't lose everything."""
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(state['config'], f, indent=2)
    except Exception as e:
        log.warning(f"Could not save progress: {e}")

def load_progress():
    """Restore config from previous interrupted install."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                saved = json.load(f)
            state['config'].update(saved)
            log.info("Resumed from previous install session")
            return True
        except Exception:
            pass
    return False

def log_ui(message, level='info'):
    """Add a message to the install log shown in the UI."""
    ts    = datetime.now().strftime('%H:%M:%S')
    entry = {'time': ts, 'level': level, 'message': message}
    state['install_log'].append(entry)
    if level == 'error':
        log.error(message)
    else:
        log.info(message)


# ── CONNECTION TESTS ──────────────────────────────────────────────────────

def test_anthropic_key(api_key):
    """Test Anthropic API key by making a minimal API call."""
    try:
        import requests as req
        r = req.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':           api_key,
                'anthropic-version':   '2023-06-01',
                'content-type':        'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens':  10,
                'messages':   [{'role': 'user', 'content': 'Hi'}],
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True, "Connected — API key valid"
        elif r.status_code == 401:
            return False, "Invalid API key — check and try again"
        elif r.status_code == 400 and 'credit' in r.text.lower():
            return False, "API key valid but no credits — add credits at console.anthropic.com"
        else:
            return False, f"Unexpected response: {r.status_code}"
    except Exception as e:
        return False, f"Connection failed: {str(e)[:100]}"


def test_alpaca_keys(api_key, secret_key, base_url):
    """Test Alpaca API keys."""
    try:
        import requests as req
        # Strip any whitespace that may have crept in
        api_key    = api_key.strip()
        secret_key = secret_key.strip()
        log.info(f"Testing Alpaca — key length: {len(api_key)} secret length: {len(secret_key)} key prefix: {api_key[:4] if api_key else 'EMPTY'}")
        r = req.get(
            f"{base_url.rstrip('/')}/v2/account",
            headers={
                'APCA-API-KEY-ID':     api_key,
                'APCA-API-SECRET-KEY': secret_key,
            },
            timeout=10,
        )
        log.info(f"Alpaca response: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            cash = float(data.get('cash', 0))
            mode = 'PAPER' if 'paper' in base_url else 'LIVE'
            return True, f"Connected — {mode} account, cash: ${cash:,.2f}"
        elif r.status_code == 401:
            return False, f"Unauthorized — key or secret incorrect (key starts with: {api_key[:4] if api_key else 'EMPTY'}, length: {len(api_key)})"
        elif r.status_code == 403:
            return False, "Invalid Alpaca keys — check key ID and secret"
        else:
            return False, f"Alpaca error: {r.status_code} — {r.text[:100]}"
    except Exception as e:
        return False, f"Connection failed: {str(e)[:100]}"


def test_congress_key(api_key):
    """Test Congress.gov API key."""
    try:
        import requests as req
        r = req.get(
            'https://api.congress.gov/v3/bill',
            params={'api_key': api_key, 'limit': 1, 'format': 'json'},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "Connected — Congress.gov API active"
        elif r.status_code == 403:
            return False, "Invalid API key"
        else:
            return False, f"Response: {r.status_code}"
    except Exception as e:
        return False, f"Connection failed: {str(e)[:100]}"


def test_gmail(gmail_user, gmail_app_password):
    """Test Gmail SMTP connection."""
    try:
        import smtplib
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as s:
            s.login(gmail_user, gmail_app_password)
        return True, "Gmail SMTP connected — alerts will work"
    except Exception as e:
        err = str(e)
        if 'BadCredentials' in err or 'Username and Password' in err:
            return False, "Wrong Gmail address or app password — note: use App Password not regular password"
        return False, f"Gmail connection failed: {err[:100]}"


# ── INSTALLATION LOGIC ────────────────────────────────────────────────────

def install_packages():
    """Install required Python packages."""
    import platform
    is_mac = platform.system() == 'Darwin'
    log_ui("Installing Python packages...")
    for pkg in REQUIRED_PACKAGES:
        try:
            log_ui(f"  Installing {pkg}...")
            cmd = [sys.executable, '-m', 'pip', 'install', pkg, '-q']
            if not is_mac:
                cmd.append('--break-system-packages')
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                log_ui(f"  ✓ {pkg}")
            else:
                # Already installed is fine
                if 'already satisfied' in result.stdout.lower() or 'already installed' in result.stdout.lower():
                    log_ui(f"  ✓ {pkg} (already installed)")
                else:
                    log_ui(f"  ✗ {pkg}: {result.stderr[:100]}", 'error')
        except Exception as e:
            log_ui(f"  ✗ {pkg}: {e}", 'error')


def create_directories():
    """Create required project directories."""
    dirs = [LOG_DIR, BACKUP_DIR,
            os.path.join(PROJECT_DIR, '.patches'),
            os.path.join(LOG_DIR, 'crash_reports')]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        log_ui(f"  ✓ {d.replace(PROJECT_DIR, '.')}")


def write_env_file(config):
    """Write .env file from collected configuration."""
    # Never overwrite existing .env without explicit confirmation
    if os.path.exists(ENV_PATH):
        backup = ENV_PATH + f'.backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        import shutil
        shutil.copy2(ENV_PATH, backup)
        log_ui(f"Existing .env backed up to {os.path.basename(backup)}")

    carrier = CARRIER_GATEWAYS.get(config.get('carrier', ''), config.get('carrier_custom', ''))

    # Preserve existing fields not collected by installer
    existing = {}
    if os.path.exists(ENV_PATH + '.backup_' + datetime.now().strftime('%Y%m%d') + '_*') or os.path.exists(ENV_PATH):
        try:
            from dotenv import dotenv_values
            existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
        except Exception:
            pass

    github_token  = config.get('github_token', existing.get('GITHUB_TOKEN', ''))
    alert_phone   = config.get('alert_phone', '').replace('-','').replace(' ','').replace(carrier,'').strip()

    env_content = f"""# Synthos v{SYNTHOS_VERSION} — Environment Configuration
# Generated by installer on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# DO NOT SHARE THIS FILE — contains private API keys

# ── OWNER INFORMATION ─────────────────────────────────────────────────────
OWNER_NAME={config.get('owner_name', '')}
OWNER_EMAIL={config.get('owner_email', '')}

# ── ANTHROPIC API ─────────────────────────────────────────────────────────
ANTHROPIC_API_KEY={config.get('anthropic_key', '')}

# ── ALPACA TRADING ────────────────────────────────────────────────────────
ALPACA_API_KEY={config.get('alpaca_key', '')}
ALPACA_SECRET_KEY={config.get('alpaca_secret', '')}
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TRADING_MODE=PAPER

# ── CONGRESS.GOV API ──────────────────────────────────────────────────────
CONGRESS_API_KEY={config.get('congress_key', '')}

# ── MONITOR SERVER (DEAD MAN SWITCH) ─────────────────────────────────────
MONITOR_URL={config.get('monitor_url', 'http://localhost:5000')}
MONITOR_TOKEN={config.get('monitor_token', 'synthos-default-token')}
PI_ID={config.get('pi_id', 'synthos-pi-1')}
PI_LABEL={config.get('owner_name', '')}
PI_EMAIL={config.get('owner_email', '')}
SECRET_TOKEN={config.get('monitor_token', '')}
OPERATOR_EMAIL={config.get('owner_email', '')}

# ── ALERTS ────────────────────────────────────────────────────────────────
SENDGRID_API_KEY={config.get('sendgrid_key', '')}
ALERT_FROM=alerts@synthos.local
ALERT_TO={config.get('owner_email', '')}
USER_EMAIL={config.get('owner_email', '')}
SUPPORT_EMAIL={SUPPORT_EMAIL}

# SMS crash alerts (via Gmail gateway — optional)
GMAIL_USER={config.get('gmail_user', '')}
GMAIL_APP_PASSWORD={config.get('gmail_app_password', '')}
ALERT_PHONE={config.get('alert_phone', '').replace('-','').replace(' ','')}
CARRIER_GATEWAY={CARRIER_GATEWAYS.get(config.get('carrier','T-Mobile'), 'tmomail.net')}

# ── GITHUB (UPDATE SYSTEM) ────────────────────────────────────────────────
GITHUB_TOKEN={github_token}

# ── OPERATING MODE (framing 4.1 — SUPERVISED is default) ─────────────────
# SUPERVISED: user approves each trade via portal before execution
# AUTONOMOUS: requires unlock key from onboarding call
OPERATING_MODE=SUPERVISED
AUTONOMOUS_UNLOCK_KEY=

# ── PORTAL ────────────────────────────────────────────────────────────────
PORTAL_PORT=5001
PORTAL_PASSWORD=
PORTAL_SECRET_KEY={secrets.token_hex(32)}

# ── PORTAL ADVANCED SETTINGS (defaults — adjust in portal UI) ────────────
MAX_POSITION_PCT=0.10
MAX_SECTOR_PCT=25
MIN_CONFIDENCE=MEDIUM
MAX_STALENESS=Aging
CLOSE_SESSION_MODE=conservative
SPOUSAL_WEIGHT=reduced
RSS_FEEDS_JSON=

# ── SYSTEM ────────────────────────────────────────────────────────────────
STARTING_CAPITAL={config.get('starting_capital', '100')}
"""

    with open(ENV_PATH, 'w') as f:
        f.write(env_content)

    # Lock down permissions
    os.chmod(ENV_PATH, 0o600)
    log_ui("✓ .env file written and locked (chmod 600)")


def setup_cron():
    """Install cron jobs for all agents."""
    log_ui("Setting up cron schedule...")
    cron_jobs = f"""
# ── SYNTHOS v{SYNTHOS_VERSION} — Agent Schedule ────────────────────────────
# All times US Eastern. Pi timezone must be set to America/New_York.
# STAGGERED to avoid DB conflicts — agents never start at the same minute.
BASH_ENV={ENV_PATH}

# The Daily — :05 past the hour during market hours (after Pulse clears)
5 9-16 * * 1-5  cd {PROJECT_DIR} && python3 agent2_research.py >> {LOG_DIR}/daily.log 2>&1
5 0,4,8,20 * * * cd {PROJECT_DIR} && python3 agent2_research.py --session=overnight >> {LOG_DIR}/daily.log 2>&1

# The Pulse — :00 and :30 during market hours (runs fast, done before :05)
0  9-16 * * 1-5 cd {PROJECT_DIR} && python3 agent3_sentiment.py >> {LOG_DIR}/pulse.log 2>&1
30 9-16 * * 1-5 cd {PROJECT_DIR} && python3 agent3_sentiment.py >> {LOG_DIR}/pulse.log 2>&1

# The Trader — :45 past the hour (after Daily finishes at :05-:35)
45 9  * * 1-5  cd {PROJECT_DIR} && python3 agent1_trader.py --session=open >> {LOG_DIR}/trader.log 2>&1
45 12 * * 1-5  cd {PROJECT_DIR} && python3 agent1_trader.py --session=midday >> {LOG_DIR}/trader.log 2>&1
45 15 * * 1-5  cd {PROJECT_DIR} && python3 agent1_trader.py --session=close >> {LOG_DIR}/trader.log 2>&1

# Heartbeat — :20 past the hour (gap between Pulse and Daily)
20 9-16  * * 1-5  cd {PROJECT_DIR} && python3 heartbeat.py >> {LOG_DIR}/heartbeat.log 2>&1
20 0,4,8 * * *    cd {PROJECT_DIR} && python3 heartbeat.py >> {LOG_DIR}/heartbeat.log 2>&1

# Audit agent — :50 past the hour (after Trader clears)
50 9-16 * * 1-5   cd {PROJECT_DIR} && python3 agent4_audit.py >> {LOG_DIR}/audit.log 2>&1
0  8    * * 1-5   cd {PROJECT_DIR} && python3 agent4_audit.py --deep >> {LOG_DIR}/audit.log 2>&1

# Nightly cleanup — midnight, nothing else running
0 0 * * * cd {PROJECT_DIR} && python3 cleanup.py >> {LOG_DIR}/cleanup.log 2>&1

# Daily digest — 4:55pm after all market sessions done
55 16 * * 1-5 cd {PROJECT_DIR} && python3 daily_digest.py >> {LOG_DIR}/digest.log 2>&1

# Saturday maintenance window
55 3 * * 6  cd {PROJECT_DIR} && python3 shutdown.py >> {LOG_DIR}/shutdown.log 2>&1
57 3 * * 6  cp {PROJECT_DIR}/signals.db {BACKUP_DIR}/signals_$(date +\\%Y\\%m\\%d).db
58 3 * * 6  find {BACKUP_DIR} -name "signals_*.db" -mtime +28 -delete
0  4 * * 6  apt-get update -qq && apt-get upgrade -y -qq && reboot

# Boot sequence — starts all services in order
@reboot sleep 90 && cd {PROJECT_DIR} && python3 boot_sequence.py >> {LOG_DIR}/boot.log 2>&1
"""

    try:
        # Get existing crontab
        existing = subprocess.run(
            ['crontab', '-l'], capture_output=True, text=True
        )
        existing_content = existing.stdout if existing.returncode == 0 else ''

        # Remove any existing Synthos jobs
        lines = [l for l in existing_content.split('\n')
                 if 'SYNTHOS' not in l and 'synthos' not in l.lower()
                 and PROJECT_DIR not in l]
        clean_existing = '\n'.join(lines).strip()

        new_crontab = (clean_existing + '\n' + cron_jobs).strip() + '\n'

        proc = subprocess.run(
            ['crontab', '-'],
            input=new_crontab, text=True,
            capture_output=True,
        )
        if proc.returncode == 0:
            log_ui("✓ Cron schedule installed")
        else:
            log_ui(f"Cron error: {proc.stderr[:100]}", 'error')
    except Exception as e:
        log_ui(f"Cron setup failed: {e}", 'error')


def set_timezone():
    """Set Pi timezone to America/New_York. Skips gracefully on Mac/non-Pi."""
    import platform
    if platform.system() == 'Darwin':
        log_ui("· Timezone: skipped on Mac (Pi-only step)")
        return
    try:
        result = subprocess.run(
            ['sudo', 'timedatectl', 'set-timezone', 'America/New_York'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log_ui("✓ Timezone set to America/New_York (ET)")
        else:
            log_ui("Could not set timezone — set manually: sudo timedatectl set-timezone America/New_York", 'error')
    except Exception as e:
        log_ui(f"Timezone: {e}", 'error')


def run_health_check():
    """Run health_check.py to verify installation."""
    import platform
    if platform.system() == 'Darwin':
        log_ui("· Health check: skipped on Mac (runs automatically on Pi after reboot)")
        return True
    hc_path = os.path.join(PROJECT_DIR, 'health_check.py')
    if not os.path.exists(hc_path):
        log_ui("health_check.py not found — skipping", 'error')
        return False
    try:
        result = subprocess.run(
            [sys.executable, hc_path],
            capture_output=True, text=True,
            timeout=60, cwd=PROJECT_DIR,
        )
        output = result.stdout + result.stderr
        passed = 'All checks passed' in output or result.returncode == 0
        if passed:
            log_ui("✓ Health check passed — Synthos is ready")
        else:
            log_ui("Health check found issues — check logs", 'error')
            # Only show lines that are actual failures, not normal log lines
            for line in output.split('\n'):
                if '✗' in line or 'FAILED' in line or 'CRITICAL' in line:
                    log_ui(f"  {line.strip()}", 'error')
        # Clean up progress file
        try:
            if os.path.exists(PROGRESS_FILE):
                os.remove(PROGRESS_FILE)
        except Exception:
            pass
        return passed
    except Exception as e:
        log_ui(f"Health check error: {e}", 'error')
        return False
        return False




def run_full_install(config):
    """Execute full installation sequence in a background thread."""
    try:
        log_ui("=" * 50)
        log_ui(f"Starting Synthos v{SYNTHOS_VERSION} installation")
        log_ui(f"Owner: {config.get('owner_name', 'Not provided')}")
        log_ui("=" * 50)

        log_ui("Step 1/7 — Setting timezone...")
        set_timezone()

        log_ui("Step 2/7 — Creating directories...")
        create_directories()

        log_ui("Step 3/7 — Installing Python packages...")
        install_packages()

        log_ui("Step 4/7 — Writing configuration...")
        write_env_file(config)

        log_ui("Step 5/7 — Setting up cron schedule...")
        setup_cron()

        log_ui("Step 6/7 — Installing system commands...")
        import platform
        is_mac = platform.system() == 'Darwin'

        # Ensure ~/bin exists and is in PATH
        if is_mac:
            bin_dir = os.path.expanduser('~/bin')
            os.makedirs(bin_dir, exist_ok=True)
            log_ui(f"· Mac: installing commands to {bin_dir}")
            # Add ~/bin to PATH in .zshrc if not already there
            zshrc = os.path.expanduser('~/.zshrc')
            path_line = 'export PATH="$HOME/bin:$PATH"  # Added by Synthos installer'
            try:
                existing = open(zshrc).read() if os.path.exists(zshrc) else ''
                if '$HOME/bin' not in existing:
                    with open(zshrc, 'a') as f:
                        f.write(f'\n{path_line}\n')
                    log_ui("✓ ~/bin added to PATH in ~/.zshrc")
            except Exception as e:
                log_ui(f"  · Could not update ~/.zshrc: {e}")
        else:
            bin_dir = os.path.expanduser('~/bin')
            os.makedirs(bin_dir, exist_ok=True)
            # Add ~/bin to PATH in .bashrc if not already there
            bashrc = os.path.expanduser('~/.bashrc')
            path_line = 'export PATH="$HOME/bin:$PATH"  # Added by Synthos installer'
            try:
                existing = open(bashrc).read() if os.path.exists(bashrc) else ''
                if '$HOME/bin' not in existing:
                    with open(bashrc, 'a') as f:
                        f.write(f'\n{path_line}\n')
                    log_ui("✓ ~/bin added to PATH in ~/.bashrc")
            except Exception as e:
                log_ui(f"  · Could not update ~/.bashrc: {e}")

        commands = {
            # name       : (script,          description)
            'synthos'    : ('install.py',      'Run Synthos installer'),
            'qsync'      : ('sync.py',         'Sync files to GitHub'),
            'qpush'      : ('qpush.sh',        'Push changes to GitHub'),
            'qpull'      : ('qpull.sh',        'Pull latest from GitHub + restart'),
            'qpatch'     : ('patch.py',        'Apply a file update'),
            'qbackup'    : (None,              'Backup signals.db'),
            'wdog'       : ('watchdog.py',     'Watchdog status/history'),
            'portal'     : ('portal_cmd.sh',   'Start portal + open browser'),
            'console'    : ('console_cmd.sh',  'Start command console + open browser'),
            'digest'     : ('daily_digest.py', 'Send daily digest manually'),
        }

        installed = []
        for cmd_name, (script_name, desc) in commands.items():
            if script_name is None:
                continue
            try:
                dest = os.path.join(bin_dir, cmd_name)
                if script_name.endswith('.sh'):
                    # Shell scripts — copy directly
                    src = os.path.join(PROJECT_DIR, script_name)
                    if not os.path.exists(src):
                        log_ui(f"  · {cmd_name} skipped (script not found: {script_name})")
                        continue
                    if is_mac:
                        r = subprocess.run(['cp', src, dest],
                                           capture_output=True, text=True, timeout=5)
                    else:
                        r = subprocess.run(['sudo', 'cp', src, dest],
                                           capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            subprocess.run(['sudo', 'chmod', '+x', dest],
                                           capture_output=True, timeout=5)
                else:
                    # Python scripts — wrap in bash launcher
                    script = (
                        f'#!/bin/bash\n'
                        f'cd {PROJECT_DIR} && python3 {script_name} "$@"\n'
                    )
                    tmp = os.path.join(PROJECT_DIR, f'.tmp_{cmd_name}')
                    with open(tmp, 'w') as tf:
                        tf.write(script)
                    os.chmod(tmp, 0o755)
                    if is_mac:
                        r = subprocess.run(['cp', tmp, dest],
                                           capture_output=True, text=True, timeout=5)
                    else:
                        r = subprocess.run(['sudo', 'cp', tmp, dest],
                                           capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            subprocess.run(['sudo', 'chmod', '+x', dest],
                                           capture_output=True, timeout=5)
                    os.remove(tmp)
                if r.returncode == 0:
                    installed.append(cmd_name)
                    log_ui(f"  ✓ {cmd_name:12} — {desc}")
                else:
                    log_ui(f"  · {cmd_name} skipped ({r.stderr.strip()[:60] or 'permission denied'})")
            except Exception as e:
                log_ui(f"  · {cmd_name} skipped: {e}")

        # qbackup — bash one-liner, path differs Mac vs Pi
        try:
            if is_mac:
                backup_dest = os.path.expanduser('~/synthos/signals.db')
                backup_out  = os.path.expanduser('~/synthos/backups/signals_manual_$(date +%Y%m%d_%H%M%S).db')
            else:
                backup_dest = '/home/pi/synthos/signals.db'
                backup_out  = '/home/pi/backups/signals_manual_$(date +%Y%m%d_%H%M%S).db'
            backup_script = (
                f'#!/bin/bash\n'
                f'cp {backup_dest} {backup_out} '
                f'&& echo "Backup saved" || echo "Backup failed"\n'
            )
            tmp  = os.path.join(PROJECT_DIR, '.tmp_qbackup')
            dest = os.path.join(bin_dir, 'qbackup')
            with open(tmp, 'w') as tf:
                tf.write(backup_script)
            os.chmod(tmp, 0o755)
            r = subprocess.run(
                ['cp', tmp, dest] if is_mac else ['sudo', 'cp', tmp, dest],
                capture_output=True, timeout=5 if is_mac else 15
            )
            if r.returncode == 0:
                if not is_mac:
                    subprocess.run(['sudo', 'chmod', '+x', dest], capture_output=True)
                installed.append('qbackup')
                log_ui(f"  ✓ qbackup      — Backup signals.db")
            os.remove(tmp)
        except Exception as e:
            log_ui(f"  · qbackup skipped: {e}")

        if installed:
            log_ui(f"\n  ✓ {len(installed)} commands ready: {', '.join(installed)}")
            if is_mac:
                # Add ~/bin to PATH in .zshrc if not already there
                zshrc = os.path.expanduser('~/.zshrc')
                path_line = 'export PATH="$HOME/bin:$PATH"  # Added by Synthos installer'
                try:
                    existing = open(zshrc).read() if os.path.exists(zshrc) else ''
                    if '$HOME/bin' not in existing:
                        with open(zshrc, 'a') as f:
                            f.write(f'\n{path_line}\n')
                        log_ui(f"  ✓ Added ~/bin to PATH in ~/.zshrc")
                        log_ui(f"  Run: source ~/.zshrc  (or open a new terminal)")
                    else:
                        log_ui(f"  · ~/bin already in PATH")
                except Exception as e:
                    log_ui(f"  Run manually: echo '{path_line}' >> ~/.zshrc")
            else:
                log_ui(f"  Type any command from anywhere on this Pi")
        else:
            log_ui("  Commands not installed — run scripts directly with python3")

        if installed:
            log_ui(f"✓ Commands installed: {', '.join(installed)}")
            log_ui(f"  Type any command from anywhere on this Pi")
        else:
            log_ui("Commands not installed — use python3 <script>.py instead", 'error')

        log_ui("Step 7/7 — Dead man switch...")
        monitor_url = config.get('monitor_url', '')
        if monitor_url and monitor_url != 'https://monitor.synthos.ai':
            log_ui(f"✓ Monitor server: {monitor_url} (custom)")
        elif monitor_url == 'https://monitor.synthos.ai':
            log_ui("✓ Monitor server: Synthos monitor (default)")
            log_ui("  Your Pi will report to the Synthos monitor server")
        else:
            log_ui("⚠ No monitor server URL set — heartbeat will log locally only")
            log_ui("  Contact synthos.signal@gmail.com to get your monitor credentials")

        log_ui("Step 7/7 — Running health check...")
        import platform as _platform
        if _platform.system() == 'Darwin':
            log_ui("· Health check: skipped on Mac (runs automatically on Pi after reboot)")
            health_ok = True
        else:
            health_ok = run_health_check()

        log_ui("=" * 50)
        if health_ok:
            log_ui("✓ Installation complete — Synthos is ready!")
            log_ui(f"  Support: {SUPPORT_EMAIL}")
            log_ui("  Monday 9:30am ET — first trading session")
            log_ui("=" * 50)
            state['install_done']    = True
            state['reboot_countdown'] = True
            # Schedule reboot after 30 second countdown
            def do_reboot():
                import time as _time
                _time.sleep(32)  # slightly longer than countdown
                log_ui("Rebooting Pi now...")
                try:
                    subprocess.run(['sudo', 'reboot'], check=False)
                except Exception as e:
                    log_ui(f"Reboot failed — please reboot manually: {e}", 'error')
            threading.Thread(target=do_reboot, daemon=True).start()
        else:
            log_ui("Installation completed with warnings — review above", 'error')
            log_ui(f"  For help: {SUPPORT_EMAIL}", 'error')
            state['install_done']  = True
            state['install_error'] = "Health check found issues — reboot skipped"

    except Exception as e:
        log_ui(f"Installation failed: {e}", 'error')
        state['install_error'] = str(e)
        state['install_done']  = True


# ── WEB UI ────────────────────────────────────────────────────────────────

def get_local_ip():
    """Get Pi's local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


HTML_BASE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Installer v{version}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:#f5f5f5; color:#111; min-height:100vh; }}
  .header {{ background:#111; color:#fff; padding:16px 24px; display:flex; justify-content:space-between; align-items:center; }}
  .header-title {{ font-size:18px; font-weight:700; letter-spacing:0.1em; }}
  .header-sub {{ font-size:10px; color:#888; letter-spacing:0.12em; text-transform:uppercase; }}
  .container {{ max-width:680px; margin:32px auto; padding:0 16px; }}
  .card {{ background:#fff; border:1px solid #ddd; padding:24px; margin-bottom:16px; }}
  .card-title {{ font-size:14px; font-weight:700; margin-bottom:4px; }}
  .card-sub {{ font-size:11px; color:#666; margin-bottom:18px; }}
  label {{ display:block; font-size:11px; font-weight:600; color:#555; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.06em; }}
  input, select {{ width:100%; border:1.5px solid #ddd; padding:9px 11px; font-size:13px; margin-bottom:14px; font-family:inherit; }}
  input:focus, select:focus {{ outline:none; border-color:#111; }}
  input[type=password] {{ letter-spacing:0.1em; }}
  .btn {{ display:block; width:100%; padding:12px; background:#111; color:#fff; border:none; font-size:13px; font-weight:600; cursor:pointer; letter-spacing:0.06em; text-transform:uppercase; }}
  .btn:hover {{ background:#333; }}
  .btn-test {{ background:#fff; color:#111; border:1.5px solid #111; margin-bottom:8px; }}
  .btn-test:hover {{ background:#f5f5f5; }}
  .status {{ padding:8px 11px; font-size:11px; margin-bottom:14px; }}
  .status.ok {{ background:#edf4f0; border:1px solid #b8d8c4; color:#2c6e49; }}
  .status.fail {{ background:#fdf0ee; border:1px solid #f0b8b2; color:#c0392b; }}
  .status.pending {{ background:#fdf6e3; border:1px solid #e8d090; color:#8f6a1d; }}
  .note {{ font-size:10px; color:#888; margin-top:-10px; margin-bottom:14px; line-height:1.6; }}
  .divider {{ border:none; border-top:1px solid #eee; margin:16px 0; }}
  .step-indicator {{ display:flex; gap:6px; margin-bottom:20px; }}
  .step {{ flex:1; height:3px; background:#ddd; }}
  .step.done {{ background:#111; }}
  .step.active {{ background:#888; }}
  .log-box {{ background:#111; color:#ccc; font-family:monospace; font-size:11px; padding:14px; height:260px; overflow-y:auto; line-height:1.7; }}
  .log-ok {{ color:#7ec89a; }}
  .log-err {{ color:#e07070; }}
  .log-ts {{ color:#555; }}
  .success-box {{ background:#edf4f0; border:2px solid #2c6e49; padding:20px; text-align:center; }}
  .success-title {{ font-size:18px; font-weight:700; color:#2c6e49; margin-bottom:8px; }}
  .success-sub {{ font-size:12px; color:#444; line-height:1.7; }}
  .optional {{ color:#aaa; font-weight:400; }}
  .section-label {{ font-size:9px; font-weight:700; letter-spacing:0.18em; text-transform:uppercase; color:#aaa; margin-bottom:12px; padding-bottom:6px; border-bottom:1px solid #eee; }}
  .btn.loading {{ opacity:0.6; cursor:wait; pointer-events:none; }}
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('form').forEach(function(form) {{
    form.addEventListener('submit', function() {{
      var btn = form.querySelector('button[type="submit"]:not(:disabled)');
      if (btn) {{
        btn.classList.add('loading');
        var txt = btn.textContent.replace('\u2192','').trim();
        btn.textContent = txt + '...';
      }}
    }});
  }});
}});
</script>
</head>
<body>
<div class="header">
  <div>
    <div class="header-title">Synthos</div>
    <div class="header-sub">Congressional Trade System · Installer v{version}</div>
  </div>
  <div style="font-size:10px;color:#555">{step_text}</div>
</div>
<div class="container">
{body}
</div>
</body>
</html>"""


def render_page(body, step_text=""):
    return HTML_BASE.format(
        version=SYNTHOS_VERSION,
        step_text=step_text,
        body=body,
    )


def page_welcome():
    body = """
<div class="card">
  <div class="card-title">Welcome to Synthos</div>
  <div class="card-sub">A three-agent AI trading system that follows congressional stock disclosures. This installer will guide you through setup in about 10 minutes.</div>

  <div class="section-label">What you'll need</div>
  <div style="font-size:12px;color:#444;line-height:2;margin-bottom:16px">
    ✓ &nbsp;Anthropic API key — <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a><br>
    ✓ &nbsp;Alpaca paper trading account — <a href="https://alpaca.markets" target="_blank">alpaca.markets</a><br>
    ✓ &nbsp;Congress.gov API key — <a href="https://api.congress.gov" target="_blank">api.congress.gov</a> (free)<br>
    ✓ &nbsp;Gmail account for alerts — any Gmail address works<br>
    ○ &nbsp;Google Sheets heartbeat — set up later (optional at first)
  </div>

  <div class="note" style="background:#fdf6e3;border:1px solid #e8d090;padding:10px;margin-bottom:16px;color:#8f6a1d">
    <strong>Paper trading only.</strong> Synthos starts in paper mode — no real money moves until you explicitly change the configuration. You can run it for weeks or months before deciding to go live.
  </div>

  <form method="POST" action="/start">
    <button class="btn" type="submit">Begin Setup →</button>
  </form>
</div>

<div style="font-size:10px;color:#aaa;text-align:center">
  Developer: Patrick McGuire &nbsp;·&nbsp; Support: synthos.signal@gmail.com &nbsp;·&nbsp; v{version}
</div>
""".format(version=SYNTHOS_VERSION)
    return render_page(body, "Step 1 of 6 — Welcome")


def page_personal():
    body = """
<div class="card">
  <div class="card-title">About You <span class="optional">(optional)</span></div>
  <div class="card-sub">This information is only used to pre-fill your crash reports and support emails. It is stored locally on your Pi and never sent anywhere without your action.</div>

  <form method="POST" action="/save-personal" id="personalform">
    <div class="section-label">Personal Information</div>
    <div class="note" style="background:#fdf6e3;border:1px solid #e8d090;padding:8px 10px;margin-bottom:12px;color:#8f6a1d;margin-top:0">
      ⚠ Required for future updates. Without your name and email you will not be issued a license key for receiving Synthos updates. Your information is stored locally on your Pi and never shared.
    </div>
    <label>Your Name</label>
    <input type="text" id="owner_name" name="owner_name" placeholder="e.g. Jane Smith" oninput="checkPersonal()">
    <label>Your Email Address</label>
    <input type="email" id="owner_email" name="owner_email" placeholder="e.g. jane@example.com" oninput="checkPersonal()">
    <div class="note">Your email is used to pre-fill support requests and crash reports so we can reply to you directly at synthos.signal@gmail.com.</div>

    <hr class="divider">
    <button class="btn" id="personal-continue" type="submit" disabled style="opacity:0.4;cursor:not-allowed">Continue →</button>
    <div style="display:flex;justify-content:space-between;margin-top:8px;align-items:center">
      <a href="/" style="font-size:11px;color:#aaa">← Back</a>
      <a href="/skip-personal" style="font-size:11px;color:#aaa;text-decoration:line-through;opacity:0.4" title="Skipping disables future updates">Skip</a>
    </div>
  </form>
<script>
function checkPersonal() {{
  var name  = document.getElementById('owner_name').value.trim();
  var email = document.getElementById('owner_email').value.trim();
  var btn   = document.getElementById('personal-continue');
  if (name && email) {{
    btn.disabled = false;
    btn.style.opacity = '1';
    btn.style.cursor = 'pointer';
  }} else {{
    btn.disabled = true;
    btn.style.opacity = '0.4';
    btn.style.cursor = 'not-allowed';
  }}
}}
</script>
</div>"""
    return render_page(body, "Step 2 of 6 — Personal Info")


def page_api_keys():
    tests = state.get('test_results', {})

    def status_html(key):
        r = tests.get(key)
        if not r:
            return ''
        cls  = 'ok' if r['ok'] else 'fail'
        icon = '✓' if r['ok'] else '✗'
        return f'<div class="status {cls}">{icon} {r["message"]}</div>'

    body = """
<div class="card">
  <div class="card-title">API Keys</div>
  <div class="card-sub">These keys connect Synthos to its data sources. Use the Test buttons to verify each one before continuing.</div>

  <form method="POST" action="/test-and-save-keys" id="keyform">
    <div class="section-label">Anthropic — AI Reasoning</div>
    {anthropic_status}
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      <strong>How to get this key (10 minutes, requires $5 credit):</strong><br>
      1. Click the button below — create an Anthropic account<br>
      2. Go to <strong>Billing</strong> → <strong>Buy Credits</strong> → add $5<br>
      &nbsp;&nbsp;&nbsp;(this covers ~10 months of running Synthos)<br>
      3. Go to <strong>API Keys</strong> → <strong>Create Key</strong><br>
      4. Copy the key — it starts with sk-ant-<br>
      <br>
      ⚠ If the Buy Credits button appears greyed out, try Chrome instead of Safari.
      <br><br>
      <a href="https://console.anthropic.com" target="_blank" style="display:inline-block;padding:6px 14px;background:#111;color:#fff;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Open Anthropic Console →</a>
      &nbsp;
      <a href="https://console.anthropic.com/settings/billing" target="_blank" style="display:inline-block;padding:6px 14px;background:#fff;color:#111;border:1.5px solid #111;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Go to Billing →</a>
    </div>
    <label>Anthropic API Key <span style="color:#888;font-weight:400">(starts with sk-ant-)</span></label>
    <div style="position:relative;margin-bottom:14px"><input type="password" id="anthropic_key" name="anthropic_key" placeholder="sk-ant-api03-..." autocomplete="off" style="width:100%;border:1.5px solid #ddd;padding:9px 11px;padding-right:60px;font-size:13px;margin-bottom:0"><button type="button" onclick="togglePassword('anthropic_key',this)" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;font-size:11px;color:#888;cursor:pointer;padding:4px 6px">Show</button></div>
    <button class="btn btn-test" type="button" onclick="testKey('anthropic')">Test Anthropic Key</button>

    <hr class="divider">
    <div class="section-label">Alpaca — Paper Trading</div>
    {alpaca_status}
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      <strong>How to get these keys (5 minutes, free):</strong><br>
      1. Click the button below — create a free Alpaca account<br>
      2. Verify your email address<br>
      3. On the dashboard, look for <strong>Paper Trading</strong> in the left menu<br>
      4. Click <strong>Your API Keys</strong> → <strong>Generate New Key</strong><br>
      5. Copy both the Key ID and Secret Key — the secret is only shown once<br>
      <br>
      ⚠ Make sure you are in <strong>Paper Trading</strong> — not live trading. Paper keys start with PK.
      <br><br>
      <a href="https://app.alpaca.markets/signup" target="_blank" style="display:inline-block;padding:6px 14px;background:#111;color:#fff;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Create Alpaca Account →</a>
      &nbsp;
      <a href="https://app.alpaca.markets/paper/dashboard/overview" target="_blank" style="display:inline-block;padding:6px 14px;background:#fff;color:#111;border:1.5px solid #111;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Go to Paper Dashboard →</a>
    </div>
    <label>Alpaca API Key ID <span style="color:#888;font-weight:400">(starts with PK)</span></label>
    <div style="position:relative;margin-bottom:14px"><input type="password" id="alpaca_key" name="alpaca_key" placeholder="PKxxxxxxxxxxxxxxxxxx" autocomplete="off" style="width:100%;border:1.5px solid #ddd;padding:9px 11px;padding-right:60px;font-size:13px;margin-bottom:0"><button type="button" onclick="togglePassword('alpaca_key',this)" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;font-size:11px;color:#888;cursor:pointer;padding:4px 6px">Show</button></div>
    <label>Alpaca Secret Key <span style="color:#c0392b;font-weight:400">(only shown once — save it somewhere safe)</span></label>
    <div style="position:relative;margin-bottom:14px"><input type="password" id="alpaca_secret" name="alpaca_secret" placeholder="xxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off" style="width:100%;border:1.5px solid #ddd;padding:9px 11px;padding-right:60px;font-size:13px;margin-bottom:0"><button type="button" onclick="togglePassword('alpaca_secret',this)" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;font-size:11px;color:#888;cursor:pointer;padding:4px 6px">Show</button></div>
    <button class="btn btn-test" type="button" onclick="testKey('alpaca')">Test Alpaca Keys</button>

    <hr class="divider">
    <div class="section-label">Congress.gov — Legislative Data</div>
    {congress_status}
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      <strong>How to get this key (2 minutes, free):</strong><br>
      1. Click the button below — it opens the signup page<br>
      2. Enter your name and email address<br>
      3. Check your email — the key arrives within 1 minute<br>
      4. Copy and paste it into the field below
      <br><br>
      <a href="https://api.congress.gov/sign-up/" target="_blank" style="display:inline-block;padding:6px 14px;background:#111;color:#fff;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Get Congress.gov Key →</a>
    </div>
    <label>Congress.gov API Key</label>
    <input type="text" name="congress_key" placeholder="Paste your Congress.gov API key here">
    <button class="btn btn-test" type="button" onclick="testKey('congress')">Test Congress.gov Key</button>

    <hr class="divider">
    <div style="font-size:10px;color:#888;margin-bottom:10px;padding:8px;background:#f9f9f9;border:1px solid #eee">
      ⚠ Make sure all fields above are filled in before clicking Continue.
      Fields must contain your keys — testing is optional but recommended.
    </div>
    <button class="btn" type="submit">Save Keys &amp; Continue →</button>
  </form>
</div>

<script>
function testKey(type) {{
  var form = document.getElementById('keyform');
  var params = new URLSearchParams();
  for (var pair of new FormData(form).entries()) {{ params.append(pair[0], pair[1]); }}
  params.append('test_type', type);
  var btns = document.querySelectorAll('button.btn-test');
  var btn = null;
  for (var i=0; i<btns.length; i++) {{
    if (btns[i].getAttribute('onclick') === "testKey('" + type + "')") {{ btn = btns[i]; break; }}
  }}
  if (btn) {{ btn.textContent = 'Testing...'; btn.disabled = true; }}
  fetch('/test-key', {{method:'POST', body:params}})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      var old = document.getElementById('status-' + type);
      if (old) old.remove();
      var div = document.createElement('div');
      div.id = 'status-' + type;
      div.className = 'status ' + (d.ok ? 'ok' : 'fail');
      div.textContent = (d.ok ? '\u2713 ' : '\u2717 ') + d.message;
      if (btn) {{
        btn.parentNode.insertBefore(div, btn);
        btn.textContent = d.ok ? '\u2713 Tested OK' : 'Retry Test';
        btn.disabled = false;
      }}
    }})
    .catch(function(e) {{
      if (btn) {{ btn.textContent = 'Retry Test'; btn.disabled = false; }}
    }});
}}

function togglePassword(inputId, btn) {{
  var input = document.getElementById(inputId);
  if (input.type === 'password') {{ input.type = 'text'; btn.textContent = 'Hide'; }}
  else {{ input.type = 'password'; btn.textContent = 'Show'; }}
}}
</script>
""".format(
        anthropic_status=status_html('anthropic'),
        alpaca_status=status_html('alpaca'),
        congress_status=status_html('congress'),
    )
    return render_page(body, "Step 3 of 6 — API Keys")


def page_alerts():
    tests = state.get('test_results', {})
    gmail_result = tests.get('gmail', {})
    gmail_status = ''
    if gmail_result:
        cls  = 'ok' if gmail_result['ok'] else 'fail'
        icon = '✓' if gmail_result['ok'] else '✗'
        gmail_status = f'<div class="status {cls}">{icon} {gmail_result["message"]}</div>'

    carrier_options = '\n'.join(
        f'<option value="{k}">{k}</option>'
        for k in CARRIER_GATEWAYS.keys()
    )

    body = f"""
<div class="card">
  <div class="card-title">Alert Settings</div>
  <div class="card-sub">Synthos sends you SMS alerts when something needs attention — Pi went offline, agent crashed, cascade detected. All alerts go through your Gmail account for free.</div>

  <form method="POST" action="/save-alerts">
    <div class="section-label">Gmail — Alert Sender</div>
    {gmail_status}
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      <strong>How to set up Gmail alerts (10 minutes):</strong><br>
      1. Use any Gmail account — or create a new one just for Synthos<br>
      2. You must enable <strong>2-Step Verification</strong> first (required by Google)<br>
      &nbsp;&nbsp;&nbsp;→ myaccount.google.com → Security → 2-Step Verification<br>
      3. Then create an <strong>App Password</strong> for Synthos:<br>
      &nbsp;&nbsp;&nbsp;→ myaccount.google.com → Security → App passwords<br>
      &nbsp;&nbsp;&nbsp;→ Select app: Mail → Select device: Other → type "Synthos"<br>
      &nbsp;&nbsp;&nbsp;→ Google gives you a 16-character password (xxxx xxxx xxxx xxxx)<br>
      4. Enter your Gmail address and that 16-character password below<br>
      <br>
      ⚠ Do NOT use your regular Gmail password here — it will not work.
      <br><br>
      <a href="https://myaccount.google.com/security" target="_blank" style="display:inline-block;padding:6px 14px;background:#111;color:#fff;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Open Google Security →</a>
      &nbsp;
      <a href="https://myaccount.google.com/apppasswords" target="_blank" style="display:inline-block;padding:6px 14px;background:#fff;color:#111;border:1.5px solid #111;font-size:11px;font-weight:600;text-decoration:none;letter-spacing:0.06em">Create App Password →</a>
    </div>
    <label>Gmail Address</label>
    <input type="email" name="gmail_user" placeholder="your-gmail@gmail.com">
    <label>Gmail App Password <span style="color:#888;font-weight:400">(16 characters, spaces OK)</span></label>
    <div style="position:relative;margin-bottom:14px"><input type="password" id="gmail_app_password" name="gmail_app_password" placeholder="xxxx xxxx xxxx xxxx" autocomplete="off" style="width:100%;border:1.5px solid #ddd;padding:9px 11px;padding-right:60px;font-size:13px;margin-bottom:0"><button type="button" onclick="togglePassword('gmail_app_password',this)" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;font-size:11px;color:#888;cursor:pointer;padding:4px 6px">Show</button></div>
    <button class="btn btn-test" type="button" onclick="testGmail()">Test Gmail Connection</button>

    <hr class="divider">
    <div class="section-label">SMS Alerts — Your Phone</div>
    <label>Your Phone Number (10 digits, no dashes)</label>
    <input type="tel" name="alert_phone" placeholder="5551234567">
    <label>Mobile Carrier</label>
    <select name="carrier">
      {carrier_options}
    </select>
    <div class="note">SMS is sent via your carrier's free email-to-text gateway. No Twilio account needed. You'll receive alerts when the Pi goes offline or an agent fails.</div>

    <hr class="divider">
    <div class="section-label">Monitor Server — Dead Man Switch</div>
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      Your Pi posts a heartbeat every hour. If it goes silent for 4+ hours during market hours you get an email alert.<br><br>
      <strong>Default:</strong> Monitor runs on this Pi at <code>http://localhost:5000</code>. Change this if you have a dedicated monitor Pi.
    </div>
    <label>Monitor Server URL</label>
    <input type="url" name="monitor_url" placeholder="http://localhost:5000" value="{state.get('config') and state['config'].get('monitor_url','') or 'http://localhost:5000'}">
    <label>Monitor Token <span style="color:#888;font-weight:400">(leave default unless you have your own server)</span></label>
    <input type="password" name="monitor_token" placeholder="synthos-default-token" value="{state.get('config') and state['config'].get('monitor_token','') or 'synthos-default-token'}">
    <label>Pi ID <span style="color:#888;font-weight:400">(unique name for your Pi on the monitor console)</span></label>
    <input type="text" name="pi_id" placeholder="synthos-pi-1" value="{state.get('config') and state['config'].get('pi_id','synthos-pi-1') or 'synthos-pi-1'}">
    <hr class="divider">
    <div style="display:flex;gap:8px">
      <a href="/api-keys" style="flex:1;display:block;padding:10px;border:1.5px solid #111;text-align:center;font-size:12px;font-weight:600;text-decoration:none;color:#111">← Edit Keys</a>
      <button class="btn" type="submit" style="flex:2;margin:0">Save &amp; Continue →</button>
    </div>
  </form>
</div>

<script>
function testGmail() {{
  var form = document.forms[0];
  var params = new URLSearchParams();
  params.append('gmail_user', form.gmail_user.value);
  params.append('gmail_app_password', form.gmail_app_password.value);
  var btn = document.querySelector('[onclick="testGmail()"]');
  btn.textContent = 'Testing...';
  btn.disabled = true;
  fetch('/test-gmail', {{method:'POST', body:params}})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      var old = document.getElementById('status-gmail');
      if (old) old.remove();
      var div = document.createElement('div');
      div.id = 'status-gmail';
      div.className = 'status ' + (d.ok ? 'ok' : 'fail');
      div.textContent = (d.ok ? '\u2713 ' : '\u2717 ') + d.message;
      btn.parentNode.insertBefore(div, btn);
      btn.textContent = d.ok ? '\u2713 Gmail Connected' : 'Retry Test';
      btn.disabled = false;
    }})
    .catch(function(e) {{ btn.textContent = 'Retry Test'; btn.disabled = false; }});
}}

function togglePassword(inputId, btn) {{
  var input = document.getElementById(inputId);
  if (input.type === 'password') {{ input.type = 'text'; btn.textContent = 'Hide'; }}
  else {{ input.type = 'password'; btn.textContent = 'Show'; }}
}}
</script>
"""
    return render_page(body, "Step 4 of 6 — Alerts")


def page_capital():
    current = state.get('config', {}).get('starting_capital', '100')
    body = f"""
<div class="card">
  <div class="card-title">Starting Capital</div>
  <div class="card-sub">Synthos tracks an internal portfolio separate from your Alpaca account balance. Set the amount you want to trade with. You can change this later in the portal.</div>

  <form method="POST" action="/save-capital">
    <div class="section-label">Starting Capital</div>
    <div style="background:#f9f9f9;border:1px solid #eee;padding:10px 12px;margin-bottom:12px;font-size:11px;line-height:1.8;color:#444">
      <strong>How capital works in Synthos:</strong><br>
      Synthos tracks its own internal balance starting at whatever you set here.<br>
      Your Alpaca paper account shows $100,000 by default — ignore that number.<br>
      Synthos only ever sizes positions against this internal balance.<br><br>
      In paper trading mode, no real money moves. This is just the simulation amount.<br>
      In live trading mode, this should match actual cash you've deposited in Alpaca.
    </div>
    <label>Starting Capital ($)</label>
    <input type="number" name="starting_capital" value="{current}" min="10" max="1000000" step="1" placeholder="100">
    <div class="note">Minimum $10. We recommend starting with $100–$500 in paper trading mode to validate the strategy before committing more capital.</div>

    <hr class="divider">
    <div style="display:flex;gap:8px">
      <a href="/alerts" style="flex:1;display:block;padding:10px;border:1.5px solid #111;text-align:center;font-size:12px;font-weight:600;text-decoration:none;color:#111">← Edit Alerts</a>
      <button class="btn" type="submit" style="flex:2;margin:0">Save &amp; Continue →</button>
    </div>
  </form>
</div>
"""
    return render_page(body, "Step 5 of 6 — Capital")


def page_disclaimer():
    body = """
<div class="card">
  <div class="card-title">Important Disclosures</div>
  <div class="card-sub">Please read and acknowledge the following before completing setup.</div>

  <form method="POST" action="/accept-disclaimer">
    <div style="background:#fdf6e3;border:1px solid #e8d090;border-radius:4px;padding:14px 16px;margin-bottom:16px;font-size:12px;line-height:1.9;color:#444">
      <strong style="color:#7a5200;font-size:13px">Synthos is Software — Not Investment Advice</strong><br><br>

      <strong>1. Software tool only.</strong> Synthos is a software tool. It is not a registered investment adviser, broker-dealer, or financial services provider. It does not manage, pool, or hold your funds.<br><br>

      <strong>2. No personalized advice.</strong> Synthos does not account for your personal financial situation, risk tolerance, investment objectives, or tax circumstances. Nothing Synthos does constitutes personalized investment advice.<br><br>

      <strong>3. Congressional disclosures may be stale.</strong> STOCK Act disclosures can be filed up to 45 days after the actual trade. By the time Synthos acts on a signal, the informational edge may already be priced into the market.<br><br>

      <strong>4. You can lose money.</strong> Past performance of any trading strategy does not guarantee future results. Small position sizes mean transaction costs can meaningfully impact returns. You may lose some or all of your trading capital.<br><br>

      <strong>5. Experimental software.</strong> Synthos is experimental software provided as-is with no warranty. The developer is not a registered investment adviser. There is no circuit breaker for market crashes or black swan events.<br><br>

      <strong>6. You are responsible.</strong> You are responsible for all trading activity in your account, all tax reporting, and all decisions to use or continue using Synthos. You can revoke Synthos's API access at any time through your Alpaca account.
    </div>

    <div style="display:flex;align-items:flex-start;gap:10px;padding:12px;background:#f9f9f9;border:1.5px solid #ddd;border-radius:4px;margin-bottom:16px;cursor:pointer;box-sizing:border-box;width:100%;overflow:hidden" onclick="document.getElementById('disc_check').click()">
      <input type="checkbox" id="disc_check" name="disclaimer_accepted" value="yes" style="margin-top:2px;flex-shrink:0;width:16px;height:16px">
      <label for="disc_check" style="font-size:12px;line-height:1.7;cursor:pointer;color:#111;min-width:0;word-wrap:break-word;overflow-wrap:break-word;flex:1">
        I have read and understood the above disclosures. I understand that Synthos is a software tool, not investment advice, and that trading involves real financial risk. I accept full responsibility for my trading activity.
      </label>
    </div>

    <hr class="divider">
    <div style="display:flex;gap:8px">
      <a href="/capital" style="flex:1;display:block;padding:10px;border:1.5px solid #111;text-align:center;font-size:12px;font-weight:600;text-decoration:none;color:#111">← Edit Capital</a>
      <button class="btn" type="submit" style="flex:2;margin:0">I Accept — Complete Setup →</button>
    </div>
  </form>
</div>
"""
    return render_page(body, "Step 6 of 6 — Disclosures")


def page_install():
    cfg = state.get('config', {})
    tests = state.get('test_results', {})

    def check(key):
        r = tests.get(key, {})
        if r.get('ok'):
            return '<span style="color:#2c6e49">✓ Tested OK</span>'
        elif r:
            return '<span style="color:#c0392b">✗ Test failed — will save anyway</span>'
        return '<span style="color:#888">Not tested</span>'

    def mask(val):
        if not val: return '<span style="color:#c0392b">NOT SET</span>'
        if len(val) > 8: return val[:4] + '••••' + val[-4:]
        return '••••'

    body = f"""
<div class="card">
  <div class="card-title">Review &amp; Install</div>
  <div class="card-sub">Confirm your configuration before installing.</div>

  <table style="width:100%;font-size:11px;margin-bottom:16px;border-collapse:collapse">
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555;width:160px">Owner name</td><td>{cfg.get('owner_name','—')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Owner email</td><td>{cfg.get('owner_email','—')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Anthropic key</td><td>{mask(cfg.get('anthropic_key',''))} &nbsp;{check('anthropic')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Alpaca key</td><td>{mask(cfg.get('alpaca_key',''))} &nbsp;{check('alpaca')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Congress.gov key</td><td>{mask(cfg.get('congress_key',''))} &nbsp;{check('congress')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Gmail</td><td>{cfg.get('gmail_user','—')} &nbsp;{check('gmail')}</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:6px 0;color:#555">Alert phone</td><td>{cfg.get('alert_phone','—')}</td></tr>
    <tr><td style="padding:6px 0;color:#555">Monitor Server</td><td>{cfg.get('monitor_url','—') or '— (set up later)'}</td></tr>
  </table>

  <div style="font-size:11px;color:#555;line-height:1.9;margin-bottom:12px">
    <strong>The installer will:</strong><br>
    ✓ &nbsp;Set timezone to US Eastern<br>
    ✓ &nbsp;Create project folders<br>
    ✓ &nbsp;Install Python packages<br>
    ✓ &nbsp;Write your .env configuration file<br>
    ✓ &nbsp;Set up the automatic cron schedule<br>
    ✓ &nbsp;Run a health check
  </div>

  <div class="note" style="background:#fdf6e3;border:1px solid #e8d090;padding:8px 10px;margin-bottom:12px;color:#8f6a1d">
    Keys marked NOT SET will be saved as empty. You can edit /home/pi/synthos/.env manually after installation.
  </div>
  <div class="note" style="background:#fdf0ee;border:1px solid #f0b8b2;padding:8px 10px;margin-bottom:16px;color:#c0392b">
    Do not close this tab during installation. Takes 2–5 minutes.
  </div>

  <div style="display:flex;gap:8px">
    <a href="/api-keys" style="flex:1;display:block;padding:10px;border:1.5px solid #111;text-align:center;font-size:12px;font-weight:600;text-decoration:none;color:#111">← Edit Keys</a>
    <form method="POST" action="/run-install" style="flex:2">
      <button class="btn" type="submit" style="margin:0">Install Synthos →</button>
    </form>
  </div>
</div>"""
    return render_page(body, "Step 7 of 7 — Review &amp; Install")


def page_installing():
    # Calculate current step from log
    current_step = 0
    for entry in state['install_log']:
        msg = entry.get('message', '')
        for s in range(1, 8):
            if f'Step {s}' in msg:
                current_step = s
    pct        = int((current_step / 7) * 100) if not state['install_done'] else 100
    step_label = f"Step {current_step} of 7" if current_step > 0 and not state['install_done'] else ("Complete" if state['install_done'] else "Starting...")

    done_section = ''
    if state['install_done']:
        if state['install_error']:
            done_section = f'''
<div style="background:#fdf0ee;border:2px solid #c0392b;padding:16px;margin-top:12px">
  <strong style="color:#c0392b">Installation completed with issues</strong><br>
  <span style="font-size:12px;color:#666">{state["install_error"]}</span><br>
  <span style="font-size:11px;color:#888">Email synthos.signal@gmail.com for help</span>
</div>'''
        elif state.get('reboot_countdown'):
            done_section = '''
<div class="success-box" style="margin-top:12px">
  <div class="success-title">✓ Synthos is Ready</div>
  <div class="success-sub">
    Installation complete. Your Pi is rebooting now.<br>
    Synthos will start automatically and run on schedule.<br><br>
    <strong>First trading session:</strong> Monday 9:30 AM ET<br>
    <strong>Support:</strong> synthos.signal@gmail.com<br><br>
    <div id="countdown" style="font-size:22px;font-weight:700;color:#2c6e49;margin-top:8px"></div>
    <div style="font-size:11px;color:#888;margin-top:4px">You can close this tab</div>
  </div>
</div>
<script>
var secs = 30;
function tick() {
  var el = document.getElementById("countdown");
  if (!el) return;
  if (secs <= 0) { el.textContent = "Rebooting now..."; return; }
  el.textContent = "Rebooting in " + secs + " second" + (secs !== 1 ? "s" : "") + "...";
  secs--;
  setTimeout(tick, 1000);
}
tick();
</script>'''
        else:
            done_section = '''
<div class="success-box" style="margin-top:12px">
  <div class="success-title">✓ Synthos is Ready</div>
  <div class="success-sub">
    Installation complete. Synthos will start automatically<br>
    on the next system boot and run on schedule.<br><br>
    <strong>First trading session:</strong> Monday 9:30 AM ET<br>
    <strong>Support:</strong> synthos.signal@gmail.com
  </div>
</div>'''

    poll_script = ''' '''  if state['install_done'] else '''
<script>
var STEPS = ["Starting...","Setting timezone...","Creating directories...",
  "Installing packages...","Writing configuration...","Setting up cron...",
  "Installing commands...","Running health check..."];
function updateProgress(logs) {
  var cur = 0;
  for (var i = 0; i < logs.length; i++) {
    var m = logs[i].message || '';
    for (var s = 1; s <= 7; s++) { if (m.indexOf('Step ' + s) !== -1) cur = s; }
  }
  var pct  = Math.round((cur / 7) * 100);
  var bar  = document.getElementById('pb');
  var lbl  = document.getElementById('pp');
  var step = document.getElementById('ps');
  var tail = document.getElementById('pt');
  if (bar)  bar.style.width = pct + '%';
  if (lbl)  lbl.textContent = pct + '%';
  if (step) step.textContent = 'Step ' + cur + ' of 7 — ' + (STEPS[cur] || 'Complete');
  if (tail && logs.length > 0) {
    tail.innerHTML = logs.slice(-3).map(function(l) {
      return '<div style="color:' + (l.level==='error'?'#e07070':'#888') + '">' + l.message + '</div>';
    }).join('');
  }
}
function poll() {
  fetch('/status').then(function(r){return r.json();}).then(function(d){
    updateProgress(d.logs||[]);
    if(!d.done){setTimeout(poll,2000);}else{location.reload();}
  }).catch(function(){setTimeout(poll,3000);});
}
poll();
</script>'''

    body = f"""
<div class="card">
  <div class="card-title">{"Installing..." if not state["install_done"] else "Installation Complete"}</div>
  <div class="card-sub">{"Please wait — do not close this tab." if not state["install_done"] else "Setup finished."}</div>

  <div style="margin:16px 0">
    <div style="display:flex;justify-content:space-between;font-size:11px;color:#555;margin-bottom:6px">
      <span id="ps">{step_label}</span>
      <span id="pp">{pct}%</span>
    </div>
    <div style="background:#eee;height:10px;border-radius:5px;overflow:hidden">
      <div id="pb" style="background:#111;height:100%;width:{pct}%;transition:width 0.6s ease;border-radius:5px"></div>
    </div>
    <div id="pt" style="margin-top:10px;font-family:monospace;font-size:10px;min-height:48px;color:#888;line-height:1.8"></div>
  </div>

  {done_section}
</div>
{poll_script}"""
    return render_page(body, "Installing...")



# ── HTTP REQUEST HANDLER ──────────────────────────────────────────────────

class InstallerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default HTTP logging

    def send_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path):
        self.send_response(302)
        self.send_header('Location', path)
        self.end_headers()

    def read_post(self):
        length  = int(self.headers.get('Content-Length', 0))
        raw     = self.rfile.read(length).decode('utf-8')
        parsed  = parse_qs(raw, keep_blank_values=True)
        return {k: unquote_plus(v[0]) for k, v in parsed.items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/' or path == '/welcome':
            self.send_html(page_welcome())
        elif path == '/personal':
            self.send_html(page_personal())
        elif path == '/skip-personal':
            self.redirect('/api-keys')
        elif path == '/api-keys':
            state['test_results'] = {}  # clear stale results on fresh load
            self.send_html(page_api_keys())
        elif path == '/alerts':
            self.send_html(page_alerts())
        elif path == '/capital':
            self.send_html(page_capital())
        elif path == '/disclaimer':
            self.send_html(page_disclaimer())
        elif path == '/install':
            self.send_html(page_install())
        elif path == '/installing':
            self.send_html(page_installing())
        elif path == '/status':
            self.send_json({
                'done':  state['install_done'],
                'error': state['install_error'],
                'logs':  state['install_log'][-20:],
            })
        else:
            self.redirect('/')

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_post()

        if path == '/start':
            self.redirect('/personal')

        elif path == '/save-personal':
            state['config']['owner_name']  = data.get('owner_name', '')
            state['config']['owner_email'] = data.get('owner_email', '')
            save_progress()
            self.redirect('/api-keys')

        elif path == '/test-key':
            test_type = data.get('test_type', '')
            try:
                if test_type == 'anthropic':
                    ok, msg = test_anthropic_key(data.get('anthropic_key', ''))
                    state['test_results']['anthropic'] = {'ok': ok, 'message': msg}
                elif test_type == 'alpaca':
                    ok, msg = test_alpaca_keys(
                        data.get('alpaca_key', ''),
                        data.get('alpaca_secret', ''),
                        'https://paper-api.alpaca.markets',
                    )
                    state['test_results']['alpaca'] = {'ok': ok, 'message': msg}
                elif test_type == 'congress':
                    ok, msg = test_congress_key(data.get('congress_key', ''))
                    state['test_results']['congress'] = {'ok': ok, 'message': msg}
                else:
                    ok, msg = False, 'Unknown test type'
                self.send_json({'ok': ok, 'message': msg})
            except Exception as e:
                log.error(f"Test key error: {e}")
                self.send_json({'ok': False, 'message': str(e)})

        elif path == '/test-and-save-keys':
            # Always save directly from form submission
            # If field is empty here the user left it blank intentionally
            state['config']['anthropic_key'] = data.get('anthropic_key', '').strip()
            state['config']['alpaca_key']    = data.get('alpaca_key', '').strip()
            state['config']['alpaca_secret'] = data.get('alpaca_secret', '').strip()
            state['config']['congress_key']  = data.get('congress_key', '').strip()
            log.info(f"Keys saved — anthropic={'set' if state['config'].get('anthropic_key') else 'EMPTY'} alpaca={'set' if state['config'].get('alpaca_key') else 'EMPTY'} congress={'set' if state['config'].get('congress_key') else 'EMPTY'}")
            save_progress()
            self.redirect('/alerts')

        elif path == '/test-gmail':
            try:
                ok, msg = test_gmail(
                    data.get('gmail_user', '').strip(),
                    data.get('gmail_app_password', '').strip(),
                )
            except Exception as e:
                ok, msg = False, f"Gmail test error: {str(e)[:100]}"
            state['test_results']['gmail'] = {'ok': ok, 'message': msg}
            self.send_json({'ok': ok, 'message': msg})

        elif path == '/save-alerts':
            state['config']['sendgrid_key']    = data.get('sendgrid_key', '').strip()
            state['config']['monitor_url']     = data.get('monitor_url', '').strip()
            state['config']['monitor_token']   = data.get('monitor_token', '').strip()
            state['config']['pi_id']           = data.get('pi_id', '').strip()
            save_progress()
            self.redirect('/capital')

        elif path == '/save-capital':
            raw = data.get('starting_capital', '100').strip().replace('$','').replace(',','')
            try:
                cap = float(raw)
                if cap < 10:   cap = 10
                if cap > 100000: cap = 100000
            except Exception:
                cap = 100
            state['config']['starting_capital'] = str(int(cap)) if cap == int(cap) else str(cap)
            save_progress()
            self.redirect('/disclaimer')

        elif path == '/accept-disclaimer':
            accepted = data.get('disclaimer_accepted', '') == 'yes'
            if not accepted:
                self.redirect('/disclaimer')
                return
            state['config']['disclaimer_accepted'] = True
            save_progress()
            self.redirect('/install')

        elif path == '/run-install':
            # Guard against double-submission — only allow one install
            if state['install_started']:
                log.warning("Duplicate /run-install request ignored")
                self.redirect('/installing')
                return
            state['install_started'] = True

            def install_with_timeout():
                thread = threading.Thread(
                    target=run_full_install,
                    args=(state['config'],),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=900)  # 15 minute max
                if thread.is_alive():
                    log_ui("Installation timed out after 15 minutes", 'error')
                    state['install_done']  = True
                    state['install_error'] = "Timed out — check logs and retry"

            threading.Thread(target=install_with_timeout, daemon=True).start()
            self.redirect('/installing')

        else:
            self.redirect('/')


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    # Pre-flight check
    print()
    print("=" * 60)
    print(f"  SYNTHOS v{SYNTHOS_VERSION} — INSTALLER")
    print("=" * 60)

    # Confirm running on Pi
    if platform.machine() not in ('aarch64', 'armv7l', 'armv6l'):
        print(f"\n  Platform: {platform.machine()} (not a Raspberry Pi)")
        print("  This installer is designed for Raspberry Pi only.")
        resp = input("  Continue anyway? (yes/no): ").strip().lower()
        if resp != 'yes':
            print("  Exiting.")
            sys.exit(0)

    # Resume previous session if available
    if load_progress():
        print(f"  Resumed previous install session")
        print(f"  Owner: {state['config'].get('owner_name','unknown')}")

    # Check Python version
    if sys.version_info < (3, 9):
        print(f"\n  Python {sys.version_info.major}.{sys.version_info.minor} found.")
        print("  Synthos requires Python 3.9 or higher.")
        sys.exit(1)

    local_ip = get_local_ip()
    print(f"\n  Starting installer web UI...")
    print(f"\n  Open your browser and go to:")
    print(f"\n    ➜  http://raspberrypi.local:{PORT}")
    print(f"    ➜  http://{local_ip}:{PORT}  (if above doesn't work)")
    print(f"\n  Keep this terminal open until setup is complete.")
    print(f"\n  Press Ctrl+C to cancel at any time.")
    print("=" * 60 + "\n")

    # Start web server
    from socketserver import ThreadingMixIn
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    server = ThreadedHTTPServer(('0.0.0.0', PORT), InstallerHandler)

    def shutdown_handler(signum, frame):
        print("\n\nInstaller cancelled.")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Monitor for install completion in background
    def watch_for_completion():
        while not state['install_done']:
            time.sleep(2)
        time.sleep(10)  # give user time to read success page
        print("\nInstallation complete — shutting down installer.")
        server.shutdown()

    threading.Thread(target=watch_for_completion, daemon=True).start()

    # Auto-open browser after short delay to let server bind
    def open_browser():
        import time as _t
        _t.sleep(1.5)
        # Try localhost first (works on Mac/desktop Pi)
        # Fall back to local IP (works when accessed from another device)
        url = f"http://localhost:{PORT}"
        try:
            webbrowser.open(url)
            print(f"  Browser opened automatically → {url}")
        except Exception:
            pass  # headless — user uses the URL printed above
    threading.Thread(target=open_browser, daemon=True).start()

    server.serve_forever()


if __name__ == '__main__':
    main()
