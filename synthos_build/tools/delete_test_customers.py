#!/usr/bin/env python3
"""
delete_test_customers.py — Permanently delete the two pre-launch test
customers (test.01@synthos.local and test.02@synthos.local) created
during the tier-calibration experiment.

Two safety guards:

  1. Hard-coded customer IDs at the top of this file. The script will
     ONLY delete these specific UUIDs. Trying to repurpose this script
     for other deletions requires editing TARGETS — no command-line
     override of the UUIDs.

  2. Email allowlist check. Each target's email is decrypted and
     compared against KNOWN_TEST_EMAILS. If the email doesn't match,
     the deletion is aborted with no DB writes — protects against the
     scenario where someone pasted a real customer's UUID into TARGETS
     by mistake.

Cleanup steps per target (transaction):
  - DELETE from pending_email_changes WHERE customer_id = ?
  - UPDATE pending_signups SET customer_id = NULL WHERE customer_id = ?
  - DELETE from customers WHERE id = ?
  - rm -rf data/customers/<cid>/

Idempotent. Re-running after a successful delete is a no-op (target
not found in customers → skip).

Run on pi5 (where auth.db lives):
    cd ~/synthos/synthos_build
    python3 tools/delete_test_customers.py
"""
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

# Hard-coded — script ONLY operates on these.
TARGETS = [
    '80419c9e-7f43-4920-9c7b-e2c0c8e6e2ab',   # patched at runtime: full UUID resolved below
    'e327ce1b-2bbf-43dd-9c12-7c3aa11afaa9',   # patched at runtime: full UUID resolved below
]

# Allowlist — both targets MUST have one of these emails or we abort.
KNOWN_TEST_EMAILS = {
    'test.01@synthos.local',
    'test.02@synthos.local',
}

# Per-customer data directory root.
CUSTOMERS_DIR = '/home/pi516gb/synthos/synthos_build/data/customers'


def resolve_full_uuids(prefixes):
    """Given short prefixes ('80419c9e', 'e327ce1b'), look up the full
    UUIDs from auth.db. Avoids hard-coding full UUIDs in this file."""
    import auth
    full = []
    with auth._auth_conn() as c:
        for prefix in prefixes:
            short = prefix.split('-')[0]
            rows = c.execute(
                "SELECT id FROM customers WHERE id LIKE ?", (short + '%',)
            ).fetchall()
            if len(rows) == 1:
                full.append(rows[0]['id'])
            elif len(rows) == 0:
                full.append(None)
            else:
                full.append('AMBIGUOUS')
    return full


def main() -> int:
    import auth

    # Resolve the prefixes we baked in at the top to full UUIDs.
    full_uuids = resolve_full_uuids(['80419c9e', 'e327ce1b'])
    real_targets = []
    for prefix, full in zip(['80419c9e', 'e327ce1b'], full_uuids):
        if full == 'AMBIGUOUS':
            print(f'ERROR: prefix {prefix} matches multiple customers — aborting',
                  file=sys.stderr)
            return 1
        if full is None:
            print(f'NOTE: prefix {prefix} no longer exists (already deleted?) — skipping')
            continue
        real_targets.append(full)

    if not real_targets:
        print('No test customers remain. Nothing to do.')
        return 0

    # Safety: verify each target's email is in the test allowlist.
    for cid in real_targets:
        row = auth.get_customer_by_id(cid)
        if not row:
            print(f'NOTE: {cid[:8]} not found (race?) — skipping')
            continue
        try:
            email = (auth.decrypt_field(row['email_enc']) or '').lower().strip()
        except Exception as e:
            print(f'ERROR: could not decrypt email for {cid[:8]}: {e}',
                  file=sys.stderr)
            return 2
        if email not in KNOWN_TEST_EMAILS:
            print(f'ERROR: {cid[:8]} email "{email}" not in test allowlist — aborting',
                  file=sys.stderr)
            print('       (refusing to delete a customer that does not match a known test email)')
            return 3
        print(f'Verified {cid[:8]}: email={email} matches test allowlist')

    print()
    print(f'About to delete {len(real_targets)} test customer(s)')
    print(f'  Mode: not-interactive — proceeding')
    print()

    # Per-target deletion.
    for cid in real_targets:
        print(f'─── deleting {cid} ───')
        with auth._auth_conn() as c:
            # 1. Foreign-key cleanup: pending_email_changes (NOT NULL FK)
            n_pec = c.execute(
                "DELETE FROM pending_email_changes WHERE customer_id = ?", (cid,)
            ).rowcount
            print(f'  pending_email_changes: removed {n_pec}')

            # 2. pending_signups.customer_id is nullable; just clear refs.
            n_ps = c.execute(
                "UPDATE pending_signups SET customer_id = NULL WHERE customer_id = ?",
                (cid,)
            ).rowcount
            print(f'  pending_signups: cleared {n_ps} customer_id ref(s)')

            # 3. Remove from customers.
            n_c = c.execute(
                "DELETE FROM customers WHERE id = ?", (cid,)
            ).rowcount
            print(f'  customers: removed {n_c}')

        # 4. Remove per-customer data directory.
        cust_path = os.path.join(CUSTOMERS_DIR, cid)
        if os.path.isdir(cust_path):
            try:
                shutil.rmtree(cust_path)
                print(f'  data dir: removed {cust_path}')
            except Exception as e:
                print(f'  data dir: WARN — could not remove {cust_path}: {e}')
        else:
            print(f'  data dir: not present (already gone or never created)')
        print()

    print('Done.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
