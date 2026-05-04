#!/usr/bin/env python3
"""
install_retail_node.py — Synthos Retail-N Node Installer
Synthos · 2026-05-04 (Phase B of distributed-trader migration)

⚠️  DEFERRED — NOT CURRENTLY DEPLOYED (audit 2026-05-04)
   Designed for retail-1 / retail-2 / ... hardware that doesn't exist yet.
   Production today runs trader_server in loopback on pi5 (the process
   node) — install_retail.py covers that path. Once retail-N hardware
   ships, this installer is the runway for cutover. Until then it's
   intentional WIP — keep it consistent with install_retail.py changes
   so the day we cutover the runway is short.
   See project_triple_node_migration + project_pi4b_cleanup_followups
   memories for the broader context.

This is the MINIMAL installer for a retail trader node (retail-1, retail-2,
...). A retail-N node only runs the synthos_trader_server HTTP service
that receives work packets from the dispatcher on the process node.

Unlike install_retail.py (which installs the full Pi5 stack — process
node + retail trader + portal + signal agents + master DBs), this
installer drops:
  - No customer DBs (they live on the process node)
  - No master signals.db (process node)
  - No retail_portal (process node)
  - No signal-producing agents (news, sentiment, screener, validator,
    macro, market_state, bias, fault, candidate_generator)
  - No price_poller (process node)
  - No mosquitto broker (process node)
  - No retail_backup (nothing to back up — retail nodes are stateless)

What it DOES install:
  - Python venv + 4 deps (paho-mqtt, httpx, fastapi, uvicorn, requests, dotenv)
  - synthos_trader_server.py + its dependency modules (work_packet,
    work_packet_db, mqtt_client, heartbeat, retail_trade_logic_agent,
    AlpacaClient, gate14_evaluator, async_alpaca_client, retail_constants,
    retail_database — note: retail_database needed for class definitions
    even though we never open SQLite files in packet mode)
  - synthos-trader-server.service systemd unit with TRADER_DB_MODE=packet
  - Heartbeat publisher (via synthos_trader_server's register_telemetry call)
  - Light watchdog (just to restart trader_server if it crashes)

USAGE:
    python3 install_retail_node.py                  # first install
    python3 install_retail_node.py --repair         # re-run install/verify
    python3 install_retail_node.py --status         # print state
    python3 install_retail_node.py --node-num=2     # name this node retail-2

USB BOOTSTRAP REQUIREMENTS (operator hands node a USB with):
    ~/.synthos/
        retail-N.env              MQTT credentials, DISPATCH_AUTH_TOKEN,
                                  process node IP, NODE_ID
                                  (template: see this file's _ENV_TEMPLATE)

    retail-N.env contents:
        NODE_TYPE=retail-N
        NODE_ID=retail-N
        TRADER_DB_MODE=packet
        DISPATCH_AUTH_TOKEN=<32-char random, must match process node>
        MQTT_HOST=10.0.0.11
        MQTT_PORT=1883
        MQTT_USER=synthos_broker
        MQTT_PASS=<must match Mosquitto's password file>
        TRADER_SERVER_HOST=0.0.0.0
        TRADER_SERVER_PORT=8443
        ALPACA_DATA_URL=https://data.alpaca.markets
        # Per-customer Alpaca creds arrive in WorkPackets — never persisted
        # to disk on the retail node.

EXIT CODES:
    0 — success or already complete
    1 — preflight failure (missing USB, wrong network, etc.)
    2 — install or verification failure
    3 — operator cancelled
"""

from __future__ import annotations
import argparse
import getpass
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# ── PATHS ──────────────────────────────────────────────────────────────────
# Derive everything from this file's location to avoid hardcoded paths.
SYNTHOS_HOME    = Path(__file__).resolve().parent.parent      # synthos_build/
USER_DIR        = SYNTHOS_HOME / "user"
ENV_PATH        = USER_DIR / ".env"
LOG_DIR         = SYNTHOS_HOME / "logs"
SENTINEL_PATH   = USER_DIR / ".retail_node_install_complete"

SYNTHOS_VERSION = "1.0.0-retail-node"
DEFAULT_NODE_NUM = 1

# ── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("install_retail_node")

# ── DEPENDENCY LIST ────────────────────────────────────────────────────────
# Minimal set for a trader-only node. paho-mqtt + httpx + fastapi + uvicorn
# are the new ones from Tier 4-6; requests + python-dotenv are foundation.
APT_DEPS = [
    "python3-paho-mqtt",
    "python3-httpx",
    "python3-fastapi",
    "python3-uvicorn",
    "python3-requests",
    "python3-dotenv",
    "mosquitto-clients",   # for `mosquitto_pub`/`mosquitto_sub` debugging
]

# Files this installer needs from the synthos_build/ tree to be present
# on the retail node. These are checked during VERIFYING and copied if
# missing (the operator typically clones the synthos repo on the retail
# node and runs this installer from src/).
REQUIRED_FILES = [
    "agents/synthos_trader_server.py",
    "agents/retail_trade_logic_agent.py",   # imported by trader_server
    "src/work_packet.py",
    "src/work_packet_db.py",
    "src/mqtt_client.py",
    "src/heartbeat.py",
    "src/gate14_evaluator.py",
    "src/async_alpaca_client.py",
    "src/retail_database.py",    # class defs needed at import time
    "src/retail_constants.py",
    "src/retail_shared.py",
    "src/auth.py",               # imported but auth.db never created
    "src/dispatch_mode.py",
]

# Required env vars (must be present in user/.env after install)
REQUIRED_ENV_VARS = [
    "NODE_TYPE",
    "NODE_ID",
    "TRADER_DB_MODE",
    "DISPATCH_AUTH_TOKEN",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USER",
    "MQTT_PASS",
    "TRADER_SERVER_HOST",
    "TRADER_SERVER_PORT",
]


# ── ENV TEMPLATE ───────────────────────────────────────────────────────────

def _env_template(node_num: int, dispatch_token: str, mqtt_pass: str) -> str:
    return f"""# Synthos retail node config — generated by install_retail_node.py
# {node_num=}
# Per-customer Alpaca creds arrive in WorkPackets and are NEVER persisted here.

NODE_TYPE=retail-{node_num}
NODE_ID=retail-{node_num}
TRADER_DB_MODE=packet
DISPATCH_AUTH_TOKEN={dispatch_token}
MQTT_HOST=10.0.0.11
MQTT_PORT=1883
MQTT_USER=synthos_broker
MQTT_PASS={mqtt_pass}
TRADER_SERVER_HOST=0.0.0.0
TRADER_SERVER_PORT=8443
ALPACA_DATA_URL=https://data.alpaca.markets

# Trader/dispatcher behavior
DISPATCH_MODE=distributed
LOG_LEVEL=INFO
"""


# ── SYSTEMD UNIT ───────────────────────────────────────────────────────────

_TRADER_SERVER_UNIT = """[Unit]
Description=Synthos Retail Trader Server (FastAPI on :8443, multi-worker)
After=network.target
Documentation=https://github.com/personalprometheus-blip/synthos/blob/main/synthos_build/agents/synthos_trader_server.py

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={synthos_home}
EnvironmentFile={synthos_home}/user/.env
# Per-customer concurrency: 3 uvicorn workers on a 4-core Pi5 8GB.
# Each worker is its own Python process with its own _TRADER_LOCK,
# so 3 customers can process simultaneously. Leaves 1 core for OS +
# heartbeat publisher + (eventually) the company_mqtt_listener if it
# co-locates here.
Environment=UVICORN_WORKERS=3
ExecStart=/usr/bin/python3 {synthos_home}/agents/synthos_trader_server.py
Restart=on-failure
RestartSec=15
StandardOutput=append:{synthos_home}/logs/trader_server.log
StandardError=append:{synthos_home}/logs/trader_server.log

[Install]
WantedBy=multi-user.target
"""


# ── PHASES ─────────────────────────────────────────────────────────────────

def run_preflight() -> tuple[bool, list[str]]:
    """Check the node is suitable for retail-N install. Returns (passed, reasons)."""
    failures: list[str] = []
    warnings: list[str] = []

    # 1. Python version
    if sys.version_info < (3, 10):
        failures.append(f"Python 3.10+ required, got {sys.version_info.major}.{sys.version_info.minor}")

    # 2. Network — can we reach the process node?
    process_ip = os.environ.get("MQTT_HOST", "10.0.0.11")
    try:
        with socket.create_connection((process_ip, 1883), timeout=3):
            pass
    except Exception as e:
        warnings.append(f"cannot reach Mosquitto on {process_ip}:1883 ({e}) — broker may be down or firewall blocking")

    # 3. Required source files
    missing_files = [f for f in REQUIRED_FILES if not (SYNTHOS_HOME / f).exists()]
    if missing_files:
        failures.append(f"missing source files: {missing_files[:5]}{'...' if len(missing_files) > 5 else ''}")

    # 4. systemctl available
    if not shutil.which("systemctl"):
        warnings.append("systemctl not found — service install will be skipped")

    # 5. running on Pi-class hardware (informational only)
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text() if Path("/proc/cpuinfo").exists() else ""
        if "Raspberry Pi" not in cpuinfo and not os.environ.get("FORCE_NON_PI"):
            warnings.append("not detected as Raspberry Pi — set FORCE_NON_PI=1 if intentional")
    except Exception:
        pass

    return (len(failures) == 0, failures + [f"WARN: {w}" for w in warnings])


def install_packages() -> bool:
    """apt install the minimal dep set. Idempotent."""
    log.info("Installing apt packages: %s", ", ".join(APT_DEPS))
    if not shutil.which("apt-get"):
        log.warning("apt-get not found — assuming non-Debian; skipping. Install %s manually.",
                    ", ".join(APT_DEPS))
        return True
    try:
        subprocess.check_call(
            ["sudo", "apt-get", "install", "-y", "--no-install-recommends"] + APT_DEPS,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        return True
    except subprocess.CalledProcessError as e:
        log.error("apt-get install failed: %s", e)
        return False


def collect_config(node_num: int, dispatch_token: str | None,
                    mqtt_pass: str | None) -> bool:
    """Write user/.env if not already present. Idempotent."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    if ENV_PATH.exists():
        log.info("user/.env exists — preserving (use --reset-env to overwrite)")
        return True

    if not dispatch_token:
        dispatch_token = getpass.getpass("DISPATCH_AUTH_TOKEN (must match process node): ").strip()
    if not mqtt_pass:
        mqtt_pass = getpass.getpass("MQTT_PASS (must match Mosquitto on process node): ").strip()

    if not dispatch_token or not mqtt_pass:
        log.error("Both DISPATCH_AUTH_TOKEN and MQTT_PASS are required")
        return False

    body = _env_template(node_num, dispatch_token, mqtt_pass)
    ENV_PATH.write_text(body)
    ENV_PATH.chmod(0o600)
    log.info("Wrote %s (node-num=%s)", ENV_PATH, node_num)
    return True


def install_systemd_unit() -> bool:
    """Drop the synthos-trader-server.service unit and enable it.
    Skipped if running outside systemd."""
    if not shutil.which("systemctl"):
        log.warning("systemctl not present — skipping systemd unit install")
        return True

    user = os.environ.get("USER") or getpass.getuser()
    unit_body = _TRADER_SERVER_UNIT.format(
        user=user, synthos_home=str(SYNTHOS_HOME),
    )
    unit_path = Path("/etc/systemd/system/synthos-trader-server.service")
    log.info("Writing systemd unit %s", unit_path)
    try:
        # tee via sudo because /etc requires elevation
        proc = subprocess.run(
            ["sudo", "tee", str(unit_path)],
            input=unit_body.encode(),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("failed to write unit: %s", e.stderr.decode() if e.stderr else e)
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.check_call(["sudo", "systemctl", "daemon-reload"])
        subprocess.check_call(["sudo", "systemctl", "enable", "synthos-trader-server.service"])
        log.info("synthos-trader-server.service enabled (NOT yet started — start manually after verify)")
        return True
    except subprocess.CalledProcessError as e:
        log.error("systemctl enable failed: %s", e)
        return False


def verify_install() -> tuple[bool, list[str]]:
    """Sanity checks: env vars present, source files present, broker reachable."""
    issues: list[str] = []

    if not ENV_PATH.exists():
        issues.append("user/.env missing")
        return (False, issues)

    env_text = ENV_PATH.read_text()
    for var in REQUIRED_ENV_VARS:
        if not any(line.startswith(f"{var}=") for line in env_text.splitlines()):
            issues.append(f"env var {var} not set in user/.env")

    for f in REQUIRED_FILES:
        if not (SYNTHOS_HOME / f).exists():
            issues.append(f"missing required file: {f}")

    # Broker reachability (warn-only — broker may be down at install time)
    try:
        host = "10.0.0.11"
        for line in env_text.splitlines():
            if line.startswith("MQTT_HOST="):
                host = line.split("=", 1)[1].strip()
                break
        with socket.create_connection((host, 1883), timeout=3):
            pass
    except Exception as e:
        issues.append(f"WARN: broker unreachable at install time ({e}) — verify after broker is up")

    return (len([i for i in issues if not i.startswith("WARN:")]) == 0, issues)


def print_status() -> None:
    """Print current install state."""
    print()
    print(f"  SYNTHOS_HOME:    {SYNTHOS_HOME}")
    print(f"  Sentinel exists: {SENTINEL_PATH.exists()}")
    print(f"  user/.env:       {'YES' if ENV_PATH.exists() else 'NO'}")
    if ENV_PATH.exists():
        env_text = ENV_PATH.read_text()
        print(f"    NODE_TYPE:     {_get_env_val(env_text, 'NODE_TYPE')}")
        print(f"    NODE_ID:       {_get_env_val(env_text, 'NODE_ID')}")
        print(f"    TRADER_DB_MODE:{_get_env_val(env_text, 'TRADER_DB_MODE')}")
        print(f"    MQTT_HOST:     {_get_env_val(env_text, 'MQTT_HOST')}")
    if shutil.which("systemctl"):
        try:
            out = subprocess.run(
                ["systemctl", "is-active", "synthos-trader-server.service"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"  trader-server:   {out.stdout.strip()}")
        except Exception as e:
            print(f"  trader-server:   <error: {e}>")
    print()


def _get_env_val(env_text: str, key: str) -> str:
    for line in env_text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return "<unset>"


# ── MAIN ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthos Retail-N Node Installer (minimal trader-only profile)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--node-num", type=int, default=DEFAULT_NODE_NUM,
                        help=f"Node number for NODE_ID=retail-N (default {DEFAULT_NODE_NUM})")
    parser.add_argument("--dispatch-token", default=None,
                        help="Dispatcher auth token (must match process node)")
    parser.add_argument("--mqtt-pass", default=None,
                        help="MQTT password (must match Mosquitto on process node)")
    parser.add_argument("--repair", action="store_true",
                        help="Re-run install + verify without recollecting config")
    parser.add_argument("--status", action="store_true",
                        help="Print install state and exit")
    parser.add_argument("--reset-env", action="store_true",
                        help="Overwrite user/.env (USE WITH CAUTION)")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print(f"  SYNTHOS v{SYNTHOS_VERSION} — RETAIL-N NODE INSTALLER")
    print(f"  Profile: retail-{args.node_num} (trader-only, no DBs/portal)")
    print("=" * 60)
    print()

    if args.status:
        print_status()
        return 0

    if SENTINEL_PATH.exists() and not args.repair:
        print("Already installed. Use --repair to re-run, --status to inspect.")
        return 0

    # 1. Preflight
    print("[1/4] Preflight ...")
    passed, msgs = run_preflight()
    for m in msgs:
        print(f"      {m}")
    if not passed:
        print("\n  Preflight FAILED — fix above and re-run.")
        return 1
    print("      OK")

    # 2. apt deps
    print("\n[2/4] Installing apt packages ...")
    if not install_packages():
        print("\n  apt install FAILED")
        return 2
    print("      OK")

    # 3. user/.env
    print("\n[3/4] Writing user/.env ...")
    if args.reset_env and ENV_PATH.exists():
        ENV_PATH.rename(ENV_PATH.with_suffix(".env.bak"))
        log.info("Backed up old .env to .env.bak")
    if not collect_config(args.node_num, args.dispatch_token, args.mqtt_pass):
        return 2
    print("      OK")

    # 4. systemd unit
    print("\n[4/4] Installing systemd unit ...")
    if not install_systemd_unit():
        return 2
    print("      OK")

    # Verify
    print("\nVerifying ...")
    ok, issues = verify_install()
    for i in issues:
        print(f"      {i}")
    if not ok:
        print("\n  Verification FAILED")
        return 2

    SENTINEL_PATH.write_text(json.dumps({
        "version": SYNTHOS_VERSION,
        "installed_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "node_num": args.node_num,
    }, indent=2))

    print()
    print("=" * 60)
    print(f"  ✅ retail-{args.node_num} install complete.")
    print()
    print("  Start the trader server:")
    print("    sudo systemctl start synthos-trader-server.service")
    print()
    print("  Verify it's listening:")
    print("    curl -sf http://127.0.0.1:8443/readyz")
    print()
    print("  On the process node, dispatcher must point at this node:")
    print(f"    RETAIL_URL=http://<this_node_ip>:8443  in user/.env")
    print()
    print("  Migration: enable a customer for this retail node:")
    print("    cd ~/synthos/synthos_build")
    print("    python3 agents/synthos_migration.py enable <customer_uuid>")
    print()
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
