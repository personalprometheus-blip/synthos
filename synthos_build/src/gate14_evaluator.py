"""
gate14_evaluator.py — Pure-compute strategy-kill check (extracted from trader).

Created 2026-05-04 as Tier 5 of the distributed-trader migration.

Background — why this lives outside retail_trade_logic_agent.py:

The original gate14_evaluation function inside the trader did three things:
  (1) DB read     — fetch the last 100 trade outcomes
  (2) PURE COMPUTE — compute Sharpe ratio, expectancy, drawdown, kill flag
  (3) DB write    — log STRATEGY_KILL_CONDITION event if kill triggered

In `daemon` mode the trader has its own DB connection so all three steps
fit inline. In `distributed` mode the trader is stateless (no DB) so the
DB I/O has to move out — but the pure compute belongs on the dispatcher
side anyway, since gate 14 runs AFTER all trades for the cycle and does
not influence the current cycle's trade decisions.

This module exposes only the pure compute. The caller is responsible for:
  - Providing the outcomes list (from wherever — DB, work packet, etc.)
  - Providing the portfolio dict
  - Acting on the returned verdict (logging the kill event, suspending,
    sending alerts — whatever the deployment context requires)

The behavior is bit-for-bit identical to the prior gate14_evaluation
body — same thresholds, same comparison, same return semantics.

Usage from daemon-mode trader (kept for backward compat):
    verdict = evaluate_strategy_kill(
        outcomes=db.get_recent_outcomes(limit=100),
        portfolio=portfolio,
        min_sharpe=C.EVAL_MIN_SHARPE,
        max_drawdown=C.EVAL_MAX_DRAWDOWN,
        window_days=C.PERFORMANCE_WINDOW_DAYS,
    )
    if verdict['kill']:
        db.log_event("STRATEGY_KILL_CONDITION", agent="trade_logic_agent",
                     details=verdict['reason'])

Usage from dispatcher (post-trade):
    verdict = evaluate_strategy_kill(
        outcomes=master_db.get_recent_outcomes(customer_id, limit=100),
        portfolio=master_db.get_portfolio(customer_id),
        ...,
    )
    if verdict['kill']:
        master_db.log_event(customer_id, "STRATEGY_KILL_CONDITION", ...)
"""

from __future__ import annotations
from typing import Any


def evaluate_strategy_kill(
    *,
    outcomes: list[dict[str, Any]],
    portfolio: dict[str, Any],
    min_sharpe: float,
    max_drawdown: float,
    window_days: int,
) -> dict[str, Any]:
    """Decide whether the strategy should be suspended.

    Returns a dict with the verdict and the metrics, never raises:
        {
          'kill':     bool,        # True = suspend new entries
          'reason':   str,         # human-readable explanation
          'metrics':  {            # always present, even when kill=False
            'trades_in_window': int,
            'win_rate':         float,
            'avg_win':          float,
            'avg_loss':         float,
            'expectancy':       float,
            'rolling_sharpe':   float,
            'current_drawdown': float,  # 0–1.0
            'sharpe_threshold': float,
            'dd_threshold':     float,
          },
          'verdict_label': str,    # 'CONTINUE' | 'SUSPEND' | 'INSUFFICIENT_DATA'
        }

    Decision rule (unchanged from original gate14_evaluation):
        kill = sharpe < min_sharpe AND drawdown > max_drawdown

    Both conditions must hold. A bad Sharpe alone OR a bad drawdown alone
    is not enough — the trader requires evidence on both axes before it
    suspends.
    """
    if len(outcomes) < 5:
        return {
            'kill': False,
            'reason': 'insufficient trade history',
            'metrics': {
                'trades_in_window': len(outcomes),
                'win_rate': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'expectancy': 0.0,
                'rolling_sharpe': 0.0,
                'current_drawdown': 0.0,
                'sharpe_threshold': float(min_sharpe),
                'dd_threshold': float(max_drawdown),
            },
            'verdict_label': 'INSUFFICIENT_DATA',
        }

    window = list(outcomes[-window_days:])
    wins   = [o for o in window if o.get('verdict') == 'WIN']
    losses = [o for o in window if o.get('verdict') == 'LOSS']

    win_rate = len(wins) / len(window) if window else 0.0
    avg_win  = sum(o.get('pnl_dollar', 0) for o in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(o.get('pnl_dollar', 0) for o in losses) / len(losses) if losses else 0.0
    expectancy = avg_win * win_rate + avg_loss * (1 - win_rate)

    pnl_series = [o.get('pnl_pct', 0) for o in window]
    mean_ret   = sum(pnl_series) / len(pnl_series) if pnl_series else 0.0
    std_ret    = (sum((r - mean_ret) ** 2 for r in pnl_series) / len(pnl_series)) ** 0.5 \
                 if pnl_series else 0.0
    # Annualize to daily-vol Sharpe; matches the original calculation.
    sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0.0

    equity = portfolio.get('cash', 0)
    peak   = portfolio.get('peak_equity') or equity
    dd     = (peak - equity) / peak if peak > 0 else 0.0

    kill = sharpe < min_sharpe and dd > max_drawdown

    metrics = {
        'trades_in_window': len(window),
        'win_rate':         win_rate,
        'avg_win':          avg_win,
        'avg_loss':         avg_loss,
        'expectancy':       expectancy,
        'rolling_sharpe':   sharpe,
        'current_drawdown': dd,
        'sharpe_threshold': float(min_sharpe),
        'dd_threshold':     float(max_drawdown),
    }

    if kill:
        return {
            'kill':    True,
            'reason':  f"Sharpe={sharpe:.3f} DD={dd*100:.1f}% — suspended pending human review",
            'metrics': metrics,
            'verdict_label': 'SUSPEND',
        }
    return {
        'kill':    False,
        'reason':  'performance within limits',
        'metrics': metrics,
        'verdict_label': 'CONTINUE',
    }
