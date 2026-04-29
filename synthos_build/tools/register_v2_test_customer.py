#!/usr/bin/env python3
"""
register_v2_test_customer.py — One-shot admin script to register the
internal V2 test customer for the parallel-trader experiment
(2026-04-30).

Reads the Alpaca paper credentials from environment variables so the
keys never end up in git. Generates a strong random password and
prints it to stdout for the operator to capture (the password is not
stored anywhere recoverable; if you lose it, run the password-reset
flow against the email).

Sets two per-customer settings on the customer's signals.db:
  trader_variant = 'v2'   → routes Gate 5 to _gate5_signal_score_v2
  is_internal    = 'true' → marks the customer as not-real for any
                            future fleet-metric filtering

Idempotent: if the email is already registered, refreshes the Alpaca
credentials and the trader_variant setting, then exits without
re-creating the customer or rotating the password.

Usage (run on the pi where auth.db lives):
    cd /home/pi516gb/synthos/synthos_build
    V2_ALPACA_KEY=PK... V2_ALPACA_SECRET=... \
        python3 tools/register_v2_test_customer.py
"""
import os
import sys
import secrets
import string

# Make synthos_build/src importable regardless of where the script is run from.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

EMAIL   = 'personal_prometheus+v2test@icloud.com'
DISPLAY = 'V2 Test Account'


def gen_password(length: int = 18) -> str:
    """Strong, readable password. Mixed case + digits + a small handful of
    safe punctuation. Drops 0/O/1/l/I for legibility."""
    alpha = ''.join(c for c in (string.ascii_letters + string.digits)
                    if c not in '0O1lI')
    pw = [secrets.choice(alpha) for _ in range(length - 3)]
    pw.append(secrets.choice('-._'))   # one safe symbol
    pw.append(secrets.choice(string.digits))
    pw.append(secrets.choice(string.ascii_uppercase))
    secrets.SystemRandom().shuffle(pw)
    return ''.join(pw)


def main() -> int:
    key    = os.environ.get('V2_ALPACA_KEY', '').strip()
    secret = os.environ.get('V2_ALPACA_SECRET', '').strip()
    if not key or not secret:
        print('ERROR: V2_ALPACA_KEY and V2_ALPACA_SECRET must be set in env',
              file=sys.stderr)
        return 1

    import auth
    from retail_database import get_customer_db

    # Idempotency check — refresh creds + setting if customer already exists.
    existing = auth.get_customer_by_email(EMAIL)
    if existing:
        cid = existing['id']
        auth.set_alpaca_credentials(cid, key, secret)
        db = get_customer_db(cid)
        db.set_setting('trader_variant', 'v2')
        db.set_setting('is_internal', 'true')
        print('=' * 60)
        print('V2 customer already existed — refreshed creds + settings')
        print('=' * 60)
        print(f'  customer_id: {cid}')
        print(f'  email:       {EMAIL}')
        print(f'  trader:      v2 (stat-arb-first)')
        print(f'  internal:    true')
        print(f'  password:    (unchanged — use forgot-password flow if lost)')
        print('=' * 60)
        return 0

    # Fresh registration.
    pw = gen_password(18)
    cid = auth.create_customer(
        email          = EMAIL,
        password       = pw,
        display_name   = DISPLAY,
        role           = 'customer',
        auto_activate  = True,           # bypass email + subscription gates
        pricing_tier   = 'early_adopter',
    )

    auth.set_alpaca_credentials(cid, key, secret)

    db = get_customer_db(cid)
    db.set_setting('trader_variant', 'v2')
    db.set_setting('is_internal',    'true')

    print('=' * 60)
    print('V2 TEST CUSTOMER CREATED')
    print('=' * 60)
    print(f'  customer_id: {cid}')
    print(f'  email:       {EMAIL}')
    print(f'  password:    {pw}')
    print(f'  trader:      v2 (stat-arb-first)')
    print(f'  internal:    true')
    print('=' * 60)
    print('Save the password — it is not stored anywhere recoverable.')
    print('Login: https://portal.synth-cloud.com/login')
    return 0


if __name__ == '__main__':
    sys.exit(main())
