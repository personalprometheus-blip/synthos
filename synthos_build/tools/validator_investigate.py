#!/usr/bin/env python3
"""Validator stack findings dump — faults, biases, per-customer detail.

Reads the cached scan results the validator stack writes into settings
keys (_FAULT_SCAN_LAST, _BIAS_SCAN_LAST, _VALIDATOR_DETAIL) and prints
only WARNING/CRITICAL-severity findings. INFO-level noise is suppressed
so the output reflects what actually needs attention.

Covers:
  - master DB fault scan (fleet-wide)
  - per-customer fault scan
  - per-customer bias scan
  - per-customer validator verdict + restrictions

Read-only. Fleet discovery is dynamic via _fleet.iter_customers().

Run:
    cd ~/synthos/synthos_build && python3 tools/validator_investigate.py
"""
import sys
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import iter_customers, project_root

from src.retail_database import DB, get_db


_SEV_SHOW = ('WARNING', 'CRITICAL')


def _fault_line(f: dict) -> str:
    sev  = f.get('severity', '?')
    gate = f.get('gate', '?')
    code = f.get('code', '?')
    msg  = (f.get('message', '') or '')[:110]
    return f"    [{sev}] {gate}/{code}: {msg}"


def _bias_line(f: dict) -> str:
    sev  = f.get('severity', '?')
    code = f.get('code') or f.get('type') or '?'
    msg  = (f.get('message', '') or '')[:110]
    return f"    [{sev}] {code}: {msg}"


def _dump_master_fault() -> None:
    print("=" * 70)
    print("FAULT SCAN (master DB)")
    print("=" * 70)
    master = get_db()
    raw = master.get_setting('_FAULT_SCAN_LAST')
    if not raw:
        print("  (no fault scan data in master)")
        return
    try:
        scan = json.loads(raw)
    except Exception as e:
        print(f"  (unparseable: {e})")
        return
    print(f"timestamp: {scan.get('timestamp')}")
    print(
        f"worst: {scan.get('worst_severity')}  "
        f"critical: {scan.get('critical')}  "
        f"warnings: {scan.get('warnings')}  "
        f"total: {scan.get('total', '?')}"
    )
    print("\nAll findings (WARNING and CRITICAL only):")
    for f in scan.get('findings', []):
        if f.get('severity') in _SEV_SHOW:
            print(_fault_line(f))
            det = (f.get('detail') or '')[:140]
            if det:
                print(f"         det: {det}")


def _dump_customer(cid: str, name: str, tier: str) -> None:
    path = project_root() / 'data' / 'customers' / cid / 'signals.db'
    try:
        cdb = DB(path=str(path))
    except Exception as e:
        print(f"\n{name} [{tier}]  ERROR opening db: {e}")
        return

    print(f"\n{name} [{tier}]  ({cid[:8]})")

    # Fault scan
    raw = cdb.get_setting('_FAULT_SCAN_LAST')
    if raw:
        try:
            scan = json.loads(raw)
            print(
                f"  FAULT:  worst={scan.get('worst_severity')} "
                f"W={scan.get('warnings')} C={scan.get('critical')}"
            )
            for f in scan.get('findings', []):
                if f.get('severity') in _SEV_SHOW:
                    print(_fault_line(f))
        except Exception as e:
            print(f"  FAULT:  unparseable ({e})")
    else:
        print("  FAULT:  none")

    # Bias scan
    raw = cdb.get_setting('_BIAS_SCAN_LAST')
    if raw:
        try:
            scan = json.loads(raw)
            print(
                f"  BIAS:   worst={scan.get('worst_severity')} "
                f"W={scan.get('warnings')} C={scan.get('critical')}"
            )
            for f in (scan.get('findings') or scan.get('biases') or []):
                if f.get('severity') in _SEV_SHOW:
                    print(_bias_line(f))
        except Exception as e:
            print(f"  BIAS:   unparseable ({e})")
    else:
        print("  BIAS:   none")

    # Validator verdict
    raw = cdb.get_setting('_VALIDATOR_DETAIL')
    if raw:
        try:
            v = json.loads(raw)
            restrictions = v.get('restrictions', v.get('all_restrictions', []))
            print(
                f"  VALIDATOR: verdict={v.get('verdict')} "
                f"restrictions={restrictions}"
            )
        except Exception:
            pass


def main() -> int:
    _dump_master_fault()
    print()
    print("=" * 70)
    print("FAULT + BIAS per customer")
    print("=" * 70)
    for cid, name, tier in iter_customers():
        _dump_customer(cid, name, tier)
    return 0


if __name__ == '__main__':
    sys.exit(main())
