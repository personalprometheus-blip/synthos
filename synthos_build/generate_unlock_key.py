"""
generate_unlock_key.py — Autonomous Mode Key Issuance Tool
Synthos Resurgens LLC — Operator Use Only

Run this AFTER completing a live onboarding call with a customer.
Generates an account-bound unlock key, logs the consent record,
and outputs the key + instructions to send to the customer.

Usage:
  python3 generate_unlock_key.py

Requirements (operator machine only — not on Pi):
  pip install cryptography python-dotenv

What this does (per framing section 4.2):
  - Generates a unique, account-bound unlock key
  - Logs the consent record: customer name, email, Alpaca key prefix,
    timestamp, operator name, call duration
  - Writes record to consent_log.jsonl (append-only)
  - Outputs the key and a ready-to-send email to the customer

The customer pastes the key into their portal → autonomous mode unlocks.
Keys are account-bound (tied to Alpaca key prefix) and non-transferable.
Synthos can revoke by issuing a new AUTONOMOUS_UNLOCK_KEY that doesn't match.
"""

import os
import json
import hmac
import hashlib
import secrets
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

ET             = ZoneInfo("America/New_York")
CONSENT_LOG    = os.path.join(os.path.dirname(__file__), 'consent_log.jsonl')
OPERATOR_EMAIL = "synthos.signal@gmail.com"

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('keygen')


def generate_key(alpaca_key_prefix: str, customer_email: str) -> str:
    """
    Generate a deterministic but secret account-bound key.
    Bound to the customer's Alpaca key prefix so the key only works
    for their specific account configuration.

    The key is: HMAC-SHA256(secret_seed, alpaca_prefix + customer_email)
    truncated to 32 hex chars, formatted as QKEY-XXXX-XXXX-XXXX-XXXX.
    """
    # Secret seed — store this securely, never share it
    # If not set, generate a random one and warn
    seed = os.environ.get('SYNTHOS_KEY_SEED', '')
    if not seed:
        seed = secrets.token_hex(32)
        log.warning("SYNTHOS_KEY_SEED not set in .env — using random seed this session only")
        log.warning("Set SYNTHOS_KEY_SEED in operator .env to generate reproducible keys")

    payload = f"{alpaca_key_prefix.upper().strip()}:{customer_email.lower().strip()}"
    raw     = hmac.new(seed.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32].upper()
    # Format as QKEY-XXXX-XXXX-XXXX-XXXX
    key = f"QKEY-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:24]}-{raw[24:28]}-{raw[28:32]}"
    return key


def log_consent_record(record: dict):
    """Append consent record to the audit log — never overwrite."""
    try:
        with open(CONSENT_LOG, 'a') as f:
            f.write(json.dumps(record) + '\n')
        log.info(f"Consent record logged to {CONSENT_LOG}")
    except Exception as e:
        log.error(f"FAILED to write consent log: {e}")
        log.error("MANUAL ACTION REQUIRED — record this consent manually:")
        log.error(json.dumps(record, indent=2))


def collect_onboarding_info() -> dict:
    """Interactive prompt to collect onboarding call details."""
    print("\n" + "="*60)
    print("SYNTHOS — Autonomous Mode Key Issuance")
    print("Complete AFTER onboarding call. All fields required.")
    print("="*60 + "\n")

    info = {}

    info['customer_name']  = input("Customer full name: ").strip()
    if not info['customer_name']:
        raise ValueError("Customer name required")

    info['customer_email'] = input("Customer email: ").strip().lower()
    if '@' not in info['customer_email']:
        raise ValueError("Valid email required")

    info['alpaca_key_prefix'] = input("Customer Alpaca API key (first 8 chars OK): ").strip()[:8]
    if not info['alpaca_key_prefix']:
        raise ValueError("Alpaca key prefix required — needed to bind the unlock key")

    info['pi_id'] = input("Customer Pi ID (from their .env PI_ID): ").strip()
    if not info['pi_id']:
        info['pi_id'] = f"synthos-pi-{info['customer_email'].split('@')[0]}"
        print(f"  → Using: {info['pi_id']}")

    info['call_duration_mins'] = input("Onboarding call duration (minutes): ").strip()

    info['operator_name'] = input("Your name (operator): ").strip()
    if not info['operator_name']:
        info['operator_name'] = "Patrick McGuire"

    print("\nTopics covered on call (press Enter to accept defaults):")
    defaults = [
        "Operating model and three-agent architecture",
        "Risk disclosures and capital at risk",
        "Supervised vs autonomous mode differences",
        "Kill switch procedure",
        "How to revoke API access via Alpaca",
        "Tax responsibility and no-advice disclaimer",
    ]
    topics_covered = []
    for t in defaults:
        resp = input(f"  [{t}] — covered? (Y/n): ").strip().lower()
        if resp != 'n':
            topics_covered.append(t)

    extra = input("Any additional topics covered (or press Enter to skip): ").strip()
    if extra:
        topics_covered.append(extra)

    info['topics_covered']       = topics_covered
    info['recording_consented']  = input("\nDid customer consent to call recording? (y/n): ").strip().lower() == 'y'
    info['recording_file']       = input("Recording filename/location (or 'n/a'): ").strip() or 'n/a'

    return info


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

    try:
        info = collect_onboarding_info()
    except (ValueError, KeyboardInterrupt) as e:
        print(f"\nAborted: {e}")
        return

    # Generate the key
    key = generate_key(info['alpaca_key_prefix'], info['customer_email'])

    # Build consent record
    now_str = datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')
    record = {
        "timestamp":            now_str,
        "customer_name":        info['customer_name'],
        "customer_email":       info['customer_email'],
        "pi_id":                info['pi_id'],
        "alpaca_key_prefix":    info['alpaca_key_prefix'],
        "unlock_key_issued":    key,
        "operator":             info['operator_name'],
        "call_duration_mins":   info['call_duration_mins'],
        "recording_consented":  info['recording_consented'],
        "recording_file":       info['recording_file'],
        "topics_covered":       info['topics_covered'],
        "framing_reference":    "Synthos framing document section 4.2 — Autonomous Mode Unlock Process",
    }

    # Log it
    log_consent_record(record)

    # Output
    print("\n" + "="*60)
    print("KEY ISSUED SUCCESSFULLY")
    print("="*60)
    print(f"\nCustomer:  {info['customer_name']} <{info['customer_email']}>")
    print(f"Pi ID:     {info['pi_id']}")
    print(f"Issued:    {now_str}")
    print(f"Operator:  {info['operator_name']}")
    print(f"\nUNLOCK KEY:\n\n  {key}\n")

    print("-"*60)
    print("READY-TO-SEND EMAIL:\n")
    print(f"To: {info['customer_email']}")
    print(f"Subject: Your Synthos Autonomous Mode Unlock Key\n")
    print(f"""Hi {info['customer_name'].split()[0]},

Thanks for completing your Synthos onboarding call.

Your autonomous mode unlock key is:

  {key}

To activate:
  1. Open your Synthos portal: http://raspberrypi.local:5001
  2. Scroll to "Autonomous Mode" at the bottom
  3. Enter the key and click Submit
  4. The portal will confirm activation

Important:
  - This key is bound to your specific Alpaca account
  - It cannot be transferred to another account
  - We can revoke it at any time if needed
  - You can return to supervised mode by contacting us

Your portal URL: http://raspberrypi.local:5001
Support: {OPERATOR_EMAIL}

Patrick McGuire
Synthos Resurgens LLC""")
    print("\n" + "="*60)
    print(f"Consent record saved to: {CONSENT_LOG}")
    print("="*60 + "\n")


if __name__ == '__main__':
    main()
