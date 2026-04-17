#!/usr/bin/env python3
"""
trailing_stop_optimizer.py — Adaptive Exit Parameter Tuning
============================================================
Analyzes exit_performance data and adjusts trailing stop parameters
gradually based on actual results. Runs weekly after market close.

Parameters optimized:
    - Per-bucket volatility multipliers (low/mid/high)
    - ATR trail ratchet multiplier
    - Profit tier thresholds
    - Max holding days

Constraints:
    - Minimum 20 closed positions before first optimization
    - Max 20% adjustment per cycle per parameter
    - Respects customer preset bounds (conservative/aggressive)
    - All changes logged to optimizer_log table
"""

import json
import logging
from datetime import datetime

log = logging.getLogger('optimizer')


def run_optimization(db, min_sample=20, max_adj=0.20):
    """
    Analyze exit_performance and adjust parameters.

    Args:
        db: CustomerDatabase instance
        min_sample: minimum closed positions required
        max_adj: maximum adjustment per parameter per cycle (0.20 = 20%)

    Returns:
        dict with adjustments made, or None if insufficient data.
    """
    try:
        rows = _get_analyzed_exits(db)
    except Exception as e:
        log.warning(f"Optimizer: cannot read exit_performance: {e}")
        return None

    if len(rows) < min_sample:
        log.info(f"Optimizer: only {len(rows)} exits, need {min_sample} — skipping")
        return None

    # Snapshot current parameters
    current = _get_current_params(db)
    adjustments = []

    # A) Volatility multiplier adjustments per bucket
    for bucket in ('low', 'mid', 'high'):
        adj = _optimize_vol_multiplier(rows, bucket, current, max_adj)
        if adj:
            adjustments.append(adj)

    # B) ATR trail multiplier
    adj = _optimize_trail_multiplier(rows, current, max_adj)
    if adj:
        adjustments.append(adj)

    # C) Profit tier thresholds
    adj = _optimize_profit_tiers(rows, current, max_adj)
    if adj:
        adjustments.extend(adj)

    # D) Max holding days
    adj = _optimize_max_hold(rows, current, max_adj)
    if adj:
        adjustments.append(adj)

    if not adjustments:
        log.info("Optimizer: no adjustments recommended")
        return {'adjustments': [], 'sample_size': len(rows)}

    # Apply adjustments to customer_settings
    new_params = dict(current)
    for a in adjustments:
        key = a['key']
        new_params[key] = a['new_value']
        db.set_setting(key, str(a['new_value']))
        log.info(f"Optimizer: {key} {a['old_value']:.4f} → {a['new_value']:.4f} ({a['reason']})")

    # Log to optimizer_log (UTC — matches DB convention)
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with db.conn() as c:
            c.execute("""
                INSERT INTO optimizer_log
                    (run_at, sample_size, parameters_before, parameters_after,
                     adjustments, applied, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (
                now, len(rows),
                json.dumps({k: round(v, 4) for k, v in current.items()}),
                json.dumps({k: round(v, 4) for k, v in new_params.items()}),
                json.dumps(adjustments),
                now,
            ))
    except Exception as e:
        log.warning(f"Optimizer: failed to write log: {e}")

    return {
        'adjustments': adjustments,
        'sample_size': len(rows),
        'parameters_before': current,
        'parameters_after': new_params,
    }


def _get_analyzed_exits(db):
    """Get exit_performance rows that have been backfilled (have hindsight data)."""
    with db.conn() as c:
        rows = c.execute("""
            SELECT * FROM exit_performance
            WHERE price_5d_after_exit IS NOT NULL
            ORDER BY exit_timestamp DESC
        """).fetchall()
        return [dict(r) for r in rows]


def _get_current_params(db):
    """Read current optimizer-tunable parameters from customer_settings or defaults."""
    defaults = {
        'VOL_MULT_LOW': 1.5,
        'VOL_MULT_MID': 1.1,
        'VOL_MULT_HIGH': 0.85,
        'ATR_TRAIL_MULTIPLIER': 2.0,
        'PROFIT_TIER_1_PCT': 0.08,
        'PROFIT_TIER_2_PCT': 0.15,
        'PROFIT_TIER_3_PCT': 0.25,
        'MAX_HOLDING_DAYS': 15,
    }
    result = {}
    for key, default in defaults.items():
        val = db.get_setting(key)
        result[key] = float(val) if val else default
    return result


def _clamp_adjustment(current, target, max_adj):
    """Clamp adjustment to max_adj percentage of current value."""
    max_change = abs(current) * max_adj
    delta = target - current
    if abs(delta) > max_change:
        delta = max_change if delta > 0 else -max_change
    return round(current + delta, 4)


def _optimize_vol_multiplier(rows, bucket, current, max_adj):
    """Adjust volatility multiplier for a given bucket based on stop quality."""
    bucket_rows = [r for r in rows if (r.get('vol_bucket') or '').lower() == bucket]
    if len(bucket_rows) < 5:
        return None

    stop_exits = [r for r in bucket_rows if r.get('exit_reason') in
                  ('TRAILING_STOP_FILLED', 'STOP_LOSS')]
    if not stop_exits:
        return None

    tight_count = sum(1 for r in stop_exits if r.get('stop_too_tight'))
    loose_count = sum(1 for r in stop_exits if r.get('stop_too_loose'))
    total = len(stop_exits)
    tight_rate = tight_count / total
    loose_rate = loose_count / total

    key = f'VOL_MULT_{bucket.upper()}'
    cur_val = current.get(key, 1.1)

    if tight_rate > 0.40:
        # Stops too aggressive — widen
        target = cur_val * (1 + tight_rate * 0.15)
        new_val = _clamp_adjustment(cur_val, target, max_adj)
        return {
            'key': key, 'old_value': cur_val, 'new_value': new_val,
            'reason': f'{bucket} stops too tight ({tight_rate:.0%} of {total})'
        }
    elif loose_rate > 0.40:
        # Stops too lenient — tighten
        target = cur_val * (1 - loose_rate * 0.15)
        new_val = _clamp_adjustment(cur_val, target, max_adj)
        return {
            'key': key, 'old_value': cur_val, 'new_value': new_val,
            'reason': f'{bucket} stops too loose ({loose_rate:.0%} of {total})'
        }

    return None


def _optimize_trail_multiplier(rows, current, max_adj):
    """Adjust ATR trail ratchet multiplier based on missed gains vs excess losses."""
    stop_exits = [r for r in rows if r.get('exit_reason') in
                  ('TRAILING_STOP_FILLED', 'STOP_LOSS') and r.get('missed_gain_pct') is not None]
    if len(stop_exits) < 10:
        return None

    missed = sorted([r['missed_gain_pct'] for r in stop_exits])
    excess = sorted([r.get('excess_loss_pct', 0) or 0 for r in stop_exits])

    # Median missed gain and excess loss
    missed_med = missed[len(missed) // 2]
    excess_med = excess[len(excess) // 2]

    key = 'ATR_TRAIL_MULTIPLIER'
    cur_val = current.get(key, 2.0)

    if missed_med > 5.0:
        # Trail too tight — missing significant gains
        step = min(abs(missed_med) * 0.01, max_adj)
        new_val = _clamp_adjustment(cur_val, cur_val * (1 + step), max_adj)
        return {
            'key': key, 'old_value': cur_val, 'new_value': new_val,
            'reason': f'median missed gain {missed_med:.1f}% — trail too tight'
        }
    elif excess_med < -3.0:
        # Positions keep falling after exit — trail is fine or too loose
        step = min(abs(excess_med) * 0.01, max_adj)
        new_val = _clamp_adjustment(cur_val, cur_val * (1 - step), max_adj)
        return {
            'key': key, 'old_value': cur_val, 'new_value': new_val,
            'reason': f'median excess loss {excess_med:.1f}% — trail too loose'
        }

    return None


def _optimize_profit_tiers(rows, current, max_adj):
    """Adjust profit-taking tier thresholds based on hit rates."""
    if len(rows) < 20:
        return []

    adjustments = []
    tiers = [
        ('PROFIT_TIER_1_PCT', 0.08, 'last_profit_tier'),
        ('PROFIT_TIER_2_PCT', 0.15, 'last_profit_tier'),
        ('PROFIT_TIER_3_PCT', 0.25, 'last_profit_tier'),
    ]

    for key, default, field in tiers:
        threshold = current.get(key, default)
        hit_count = sum(1 for r in rows if (r.get(field) or 0) >= threshold)
        hit_rate = hit_count / len(rows)

        if key == 'PROFIT_TIER_3_PCT' and hit_rate < 0.05 and len(rows) >= 50:
            # Top tier rarely reached — lower it
            new_val = _clamp_adjustment(threshold, threshold * 0.85, max_adj)
            adjustments.append({
                'key': key, 'old_value': threshold, 'new_value': new_val,
                'reason': f'tier 3 hit rate {hit_rate:.0%} < 5% — lowering threshold'
            })
        elif key == 'PROFIT_TIER_1_PCT' and hit_rate > 0.80:
            # Bottom tier almost always reached — raise it
            new_val = _clamp_adjustment(threshold, threshold * 1.15, max_adj)
            adjustments.append({
                'key': key, 'old_value': threshold, 'new_value': new_val,
                'reason': f'tier 1 hit rate {hit_rate:.0%} > 80% — raising threshold'
            })

    return adjustments


def _optimize_max_hold(rows, current, max_adj):
    """Adjust max holding days based on forced exit outcomes."""
    max_hold_exits = [r for r in rows if r.get('exit_reason') == 'MAX_HOLD']
    if len(max_hold_exits) < 5:
        return None

    key = 'MAX_HOLDING_DAYS'
    cur_val = current.get(key, 15)

    # Check if forced exits recover after exit
    recoveries = [r for r in max_hold_exits
                  if r.get('price_5d_after_exit') and r.get('exit_price')
                  and r['price_5d_after_exit'] > r['exit_price'] * 1.02]
    recovery_rate = len(recoveries) / len(max_hold_exits)

    # Check if forced exits are mostly losers
    losers = [r for r in max_hold_exits if (r.get('pnl_dollar') or 0) < 0]
    loser_rate = len(losers) / len(max_hold_exits)

    if recovery_rate > 0.50:
        # More than half recover — holding too short
        new_val = min(int(cur_val * 1.15), 30)  # cap at 30 days
        if new_val != cur_val:
            return {
                'key': key, 'old_value': cur_val, 'new_value': float(new_val),
                'reason': f'{recovery_rate:.0%} of forced exits recovered — increasing hold'
            }
    elif loser_rate > 0.70:
        # Most forced exits are losers that keep falling — reduce hold time
        new_val = max(int(cur_val * 0.85), 5)  # floor at 5 days
        if new_val != cur_val:
            return {
                'key': key, 'old_value': cur_val, 'new_value': float(new_val),
                'reason': f'{loser_rate:.0%} of forced exits were losers — reducing hold'
            }

    return None
