#!/usr/bin/env python3
"""
synthos_test.py — Agent Test Harness
Synthos · Dev Tool

Start, stop, and observe individual agents.
Outputs a live log and a session report on exit.

USAGE:
  python3 synthos_test.py                  # interactive menu
  python3 synthos_test.py --all            # start all agents
  python3 synthos_test.py --agent trader   # start one agent
  python3 synthos_test.py --stop           # stop all running agents
  python3 synthos_test.py --status         # show what's running
  python3 synthos_test.py --report         # print last session report

AGENTS:
  Retail:   trader, research, sentiment, portal, watchdog, monitor
  Company:  patches, blueprint, sentinel, fidget,
            librarian, scoop, vault, timekeeper
"""

import os
import sys
import time
import json
import signal
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────

PROJECT_DIR  = Path(__file__).parent
LOG_DIR      = PROJECT_DIR / "logs"
REPORT_FILE  = PROJECT_DIR / "logs" / "test_session.json"
PID_FILE     = PROJECT_DIR / ".test_pids.json"

LOG_DIR.mkdir(exist_ok=True)

# Agent registry — maps short name → script + log file + args
AGENTS = {
    # ── Retail ────────────────────────────────────────────────────────────
    "trader":    {"script": "agent1_trader.py",    "log": "trader.log",    "args": []},
    "research":  {"script": "agent2_research.py",  "log": "research.log",  "args": []},
    "sentiment": {"script": "agent3_sentiment.py", "log": "sentiment.log", "args": []},
    "portal":    {"script": "portal.py",           "log": "portal.log",    "args": []},
    "watchdog":  {"script": "watchdog.py",         "log": "watchdog.log",  "args": []},
    "monitor":   {"script": "synthos_monitor.py",  "log": "monitor.log",   "args": []},
    # ── Company ───────────────────────────────────────────────────────────
    "patches":   {"script": "patches.py",          "log": "patches.log",   "args": ["--mode", "light"]},
    "blueprint": {"script": "blueprint.py",        "log": "blueprint.log", "args": []},
    "sentinel":  {"script": "sentinel.py",         "log": "sentinel.log",  "args": []},
    "fidget":    {"script": "fidget.py",           "log": "fidget.log",    "args": []},
    "librarian": {"script": "librarian.py",        "log": "librarian.log", "args": []},
    "scoop":     {"script": "scoop.py",            "log": "scoop.log",     "args": []},
    "vault":     {"script": "vault.py",            "log": "vault.log",     "args": []},
    "timekeeper":{"script": "timekeeper.py",       "log": "timekeeper.log","args": []},
}

RETAIL_AGENTS  = ["trader", "research", "sentiment", "portal", "watchdog", "monitor"]
COMPANY_AGENTS = ["patches", "blueprint", "sentinel", "fidget",
                  "librarian", "scoop", "vault", "timekeeper"]

# ── ANSI COLORS ───────────────────────────────────────────────────────────────

G  = "\033[92m"   # green
Y  = "\033[93m"   # yellow
R  = "\033[91m"   # red
B  = "\033[94m"   # blue
C  = "\033[96m"   # cyan
DIM= "\033[2m"
BOLD="\033[1m"
RST= "\033[0m"

def ok(msg):   print(f"  {G}✓{RST}  {msg}")
def err(msg):  print(f"  {R}✗{RST}  {msg}")
def warn(msg): print(f"  {Y}!{RST}  {msg}")
def info(msg): print(f"  {B}·{RST}  {msg}")
def dim(msg):  print(f"{DIM}  {msg}{RST}")


# ── PID TRACKING ──────────────────────────────────────────────────────────────

def load_pids():
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except Exception:
            pass
    return {}

def save_pids(pids):
    PID_FILE.write_text(json.dumps(pids, indent=2))

def clear_pids():
    if PID_FILE.exists():
        PID_FILE.unlink()


# ── SESSION REPORT ────────────────────────────────────────────────────────────

session = {
    "started":  datetime.now().isoformat(),
    "finished": None,
    "agents":   {},   # name → {started, stopped, pid, exit_code, log_lines}
}

def record_start(name, pid):
    session["agents"][name] = {
        "started":    datetime.now().isoformat(),
        "stopped":    None,
        "pid":        pid,
        "exit_code":  None,
        "log_lines":  0,
        "errors":     [],
    }

def record_stop(name, exit_code):
    if name in session["agents"]:
        session["agents"][name]["stopped"]   = datetime.now().isoformat()
        session["agents"][name]["exit_code"] = exit_code

def record_log_snapshot(name):
    """Count lines and grab last 5 errors from agent log."""
    cfg = AGENTS.get(name, {})
    log_path = LOG_DIR / cfg.get("log", f"{name}.log")
    if log_path.exists():
        lines = log_path.read_text(errors="replace").splitlines()
        errors = [l for l in lines if "ERROR" in l or "CRITICAL" in l or "✗" in l][-5:]
        if name in session["agents"]:
            session["agents"][name]["log_lines"] = len(lines)
            session["agents"][name]["errors"]    = errors

def save_report():
    session["finished"] = datetime.now().isoformat()
    LOG_DIR.mkdir(exist_ok=True)
    REPORT_FILE.write_text(json.dumps(session, indent=2))


# ── AGENT CONTROL ─────────────────────────────────────────────────────────────

def agent_exists(name):
    cfg = AGENTS.get(name)
    if not cfg:
        return False
    return (PROJECT_DIR / cfg["script"]).exists()

def is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

def start_agent(name):
    cfg = AGENTS.get(name)
    if not cfg:
        err(f"Unknown agent: {name}")
        return None

    script = PROJECT_DIR / cfg["script"]
    if not script.exists():
        warn(f"{name}: {cfg['script']} not found — skipping")
        return None

    log_path = LOG_DIR / cfg["log"]
    try:
        with open(log_path, "a") as logf:
            proc = subprocess.Popen(
                [sys.executable, str(script)] + cfg["args"],
                stdout=logf,
                stderr=logf,
                cwd=str(PROJECT_DIR),
            )
        time.sleep(1.0)
        if proc.poll() is None:
            ok(f"{BOLD}{name}{RST}  started  {DIM}pid={proc.pid}  log={cfg['log']}{RST}")
            record_start(name, proc.pid)
            return proc.pid
        else:
            err(f"{name}: exited immediately (exit={proc.returncode}) — check {cfg['log']}")
            return None
    except Exception as e:
        err(f"{name}: failed to start — {e}")
        return None

def stop_agent(name, pid):
    if not is_running(pid):
        dim(f"  {name}: already stopped")
        record_stop(name, 0)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            if not is_running(pid):
                break
        if is_running(pid):
            os.kill(pid, signal.SIGKILL)
        record_log_snapshot(name)
        record_stop(name, 0)
        ok(f"{name}: stopped")
    except Exception as e:
        err(f"{name}: stop failed — {e}")


# ── STATUS DISPLAY ────────────────────────────────────────────────────────────

def print_status():
    pids = load_pids()
    print(f"\n{BOLD}  SYNTHOS AGENT STATUS{RST}")
    print(f"  {'─'*46}")
    print(f"  {'AGENT':<14} {'STATUS':<12} {'PID':<8} {'LOG'}")
    print(f"  {'─'*46}")

    all_agents = RETAIL_AGENTS + COMPANY_AGENTS
    for name in all_agents:
        cfg  = AGENTS[name]
        pid  = pids.get(name)
        script_present = (PROJECT_DIR / cfg["script"]).exists()

        if not script_present:
            status = f"{DIM}NOT FOUND{RST}"
            pid_str = "—"
        elif pid and is_running(pid):
            status = f"{G}RUNNING{RST}"
            pid_str = str(pid)
        elif pid:
            status = f"{Y}STOPPED{RST}"
            pid_str = str(pid)
        else:
            status = f"{DIM}IDLE{RST}"
            pid_str = "—"

        log_str = cfg["log"] if script_present else "—"
        print(f"  {name:<14} {status:<20} {pid_str:<8} {DIM}{log_str}{RST}")

    print(f"  {'─'*46}\n")


# ── REPORT DISPLAY ────────────────────────────────────────────────────────────

def print_report():
    if not REPORT_FILE.exists():
        warn("No session report found yet. Run agents first.")
        return

    data = json.loads(REPORT_FILE.read_text())
    print(f"\n{BOLD}  SYNTHOS TEST SESSION REPORT{RST}")
    print(f"  Started:  {data.get('started','?')}")
    print(f"  Finished: {data.get('finished','still running')}")
    print(f"  {'─'*50}")

    for name, rec in data.get("agents", {}).items():
        pid  = rec.get("pid", "?")
        code = rec.get("exit_code")
        lines= rec.get("log_lines", 0)
        started = rec.get("started","?")[11:19]
        stopped = rec.get("stopped","—")
        stopped = stopped[11:19] if stopped else "running"

        status = f"{G}OK{RST}" if code == 0 else f"{Y}RUNNING{RST}" if code is None else f"{R}EXIT({code}){RST}"
        print(f"\n  {BOLD}{name}{RST}")
        print(f"    pid={pid}  {started} → {stopped}  status={status}  log_lines={lines}")

        errors = rec.get("errors", [])
        if errors:
            print(f"    {R}Last errors:{RST}")
            for e in errors:
                print(f"      {DIM}{e[:90]}{RST}")
        else:
            print(f"    {DIM}No errors logged{RST}")

    print(f"\n  {'─'*50}")
    print(f"  Full report: {REPORT_FILE}\n")


# ── INTERACTIVE MENU ──────────────────────────────────────────────────────────

def interactive_menu():
    print(f"\n{BOLD}{C}  ╔══════════════════════════════════╗")
    print(f"  ║   SYNTHOS TEST HARNESS           ║")
    print(f"  ╚══════════════════════════════════╝{RST}\n")

    running_pids = {}

    def menu():
        print(f"\n  {BOLD}What do you want to do?{RST}")
        print(f"  {B}1{RST}  Start retail agents   (trader, research, sentiment, portal)")
        print(f"  {B}2{RST}  Start company agents  (patches, sentinel, fidget, librarian...)")
        print(f"  {B}3{RST}  Start a single agent")
        print(f"  {B}4{RST}  Stop all running agents")
        print(f"  {B}5{RST}  Show status")
        print(f"  {B}6{RST}  Tail a log file")
        print(f"  {B}7{RST}  Print session report")
        print(f"  {B}q{RST}  Quit and save report")
        return input("\n  > ").strip().lower()

    while True:
        choice = menu()

        if choice == "1":
            print(f"\n  {BOLD}Starting retail agents...{RST}\n")
            for name in ["trader", "research", "sentiment", "portal"]:
                pid = start_agent(name)
                if pid:
                    running_pids[name] = pid
            save_pids(running_pids)

        elif choice == "2":
            print(f"\n  {BOLD}Starting company agents...{RST}\n")
            for name in COMPANY_AGENTS:
                pid = start_agent(name)
                if pid:
                    running_pids[name] = pid
            save_pids(running_pids)

        elif choice == "3":
            print(f"\n  Available: {', '.join(AGENTS.keys())}")
            name = input("  Agent name: ").strip().lower()
            pid = start_agent(name)
            if pid:
                running_pids[name] = pid
                save_pids(running_pids)

        elif choice == "4":
            print(f"\n  {BOLD}Stopping all agents...{RST}\n")
            pids = load_pids()
            pids.update(running_pids)
            for name, pid in pids.items():
                stop_agent(name, pid)
            running_pids.clear()
            clear_pids()

        elif choice == "5":
            print_status()

        elif choice == "6":
            print(f"\n  Log files in {LOG_DIR}:")
            logs = sorted(LOG_DIR.glob("*.log"))
            for i, l in enumerate(logs):
                print(f"  {B}{i}{RST}  {l.name}")
            try:
                idx = int(input("  Pick number: ").strip())
                log_path = logs[idx]
                print(f"\n{DIM}  --- last 30 lines of {log_path.name} ---{RST}\n")
                lines = log_path.read_text(errors="replace").splitlines()
                for line in lines[-30:]:
                    color = R if "ERROR" in line or "CRITICAL" in line else \
                            Y if "WARNING" in line or "✗" in line else \
                            G if "✓" in line else DIM
                    print(f"  {color}{line}{RST}")
                print()
            except (ValueError, IndexError):
                warn("Invalid selection.")

        elif choice == "7":
            print_report()

        elif choice in ("q", "quit", "exit"):
            print(f"\n  {BOLD}Stopping agents and saving report...{RST}\n")
            pids = load_pids()
            pids.update(running_pids)
            for name, pid in pids.items():
                stop_agent(name, pid)
            save_report()
            print_report()
            clear_pids()
            print(f"  {G}Done.{RST} Report saved to {REPORT_FILE}\n")
            break
        else:
            warn("Unknown choice.")


# ── CLI ENTRY ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Synthos Agent Test Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--all",    action="store_true", help="Start all available agents")
    parser.add_argument("--retail", action="store_true", help="Start retail agents only")
    parser.add_argument("--company",action="store_true", help="Start company agents only")
    parser.add_argument("--agent",  metavar="NAME",      help="Start a single named agent")
    parser.add_argument("--stop",   action="store_true", help="Stop all tracked agents")
    parser.add_argument("--status", action="store_true", help="Show agent status")
    parser.add_argument("--report", action="store_true", help="Print last session report")
    args = parser.parse_args()

    running_pids = {}

    if args.status:
        print_status()

    elif args.report:
        print_report()

    elif args.stop:
        print(f"\n  {BOLD}Stopping all agents...{RST}\n")
        pids = load_pids()
        for name, pid in pids.items():
            stop_agent(name, pid)
        save_report()
        clear_pids()
        print()

    elif args.agent:
        pid = start_agent(args.agent)
        if pid:
            running_pids[args.agent] = pid
            save_pids(running_pids)

    elif args.retail:
        for name in RETAIL_AGENTS:
            pid = start_agent(name)
            if pid:
                running_pids[name] = pid
        save_pids(running_pids)

    elif args.company:
        for name in COMPANY_AGENTS:
            pid = start_agent(name)
            if pid:
                running_pids[name] = pid
        save_pids(running_pids)

    elif args.all:
        for name in list(AGENTS.keys()):
            pid = start_agent(name)
            if pid:
                running_pids[name] = pid
        save_pids(running_pids)

    else:
        # No args — interactive mode
        interactive_menu()
        return

    # Non-interactive: watch for 30s then report
    if running_pids:
        print(f"\n  {DIM}Watching for 30 seconds...{RST}")
        time.sleep(30)
        print_status()
        for name in running_pids:
            record_log_snapshot(name)
        save_report()
        print_report()


if __name__ == "__main__":
    main()
