# synthos_build/tools/

Diagnostic and maintenance scripts for the retail stack. Everything in
here is **run on the Pi** (usually pi5) from the `synthos_build/`
directory. All tools are stdlib-only except where they deliberately
import from `src/` or `agents/` — so they work without any venv
gymnastics.

Customer discovery is dynamic: `_fleet.iter_customers()` walks
`data/customers/` and returns `(customer_id, display_name, tier)` for
every on-disk customer. New customers show up automatically; no edits
needed per tool.

## How to run

```bash
cd ~/synthos/synthos_build
python3 tools/<script>.py
```

All paths inside the tools resolve relative to `synthos_build/`
regardless of where you launch from, so a fully-qualified invocation
(`python3 /home/pi516gb/synthos/synthos_build/tools/customer_audit.py`)
works the same way.

## The tools

### Read-only — fleet & data

| Tool | Purpose |
|------|---------|
| `customer_audit.py` | Per-customer headline settings + recent positions + current cash. Quick "what does the fleet look like?" check. |
| `trader_audit.py` | Last few trader-related events from `system_log` per customer — TRADE_DECISION, ACCOUNT_SKIP, HALT, etc. Takes an optional row-limit argument (default 6). |
| `tier_readout.py` | Calibration-experiment snapshot: open/closed trades since `EXPERIMENT_START`, realised PnL, win rate, top skip reasons per customer, per-tier aggregates, exit-reason breakdown. Markdown-formatted output. |
| `validator_investigate.py` | Dumps `_FAULT_SCAN_LAST`, `_BIAS_SCAN_LAST`, `_VALIDATOR_DETAIL` for master + every customer. Shows only WARNING/CRITICAL severities — INFO noise is suppressed. |
| `sentiment_smoke.py` | Live smoke test of the sentiment agent's `_fetch_volume_from_alpaca` + its `ThreadPoolExecutor` parallelism. Prints single-ticker baseline and an 8-ticker parallel run so you can see per-ticker cost. |
| `verify_schema.py` | Checks that a given `table.column` exists in master + every customer signals.db. Default: `signals.screener_score`. Pass two positional args to check any other column: `python3 tools/verify_schema.py positions exit_reason`. |

### Read-only — code health

These wrap third-party analyzers. They install as `pip3 install --break-system-packages vulture ruff radon` once per machine; each wrapper tells you how if missing.

| Tool | Purpose |
|------|---------|
| `portal_lint.py` | Static linter for the command portal — missing Flask routes, decorator-order bugs, `getElementById` / `id` mismatches, unsafe Jinja filters, undeclared template vars, unclosed HTML tags, unused IDs. Stdlib-only; safe to run as part of CI. |
| `dead_code.py` | Unused imports, functions, classes, variables via **vulture**. Default confidence 70 (balanced). Pass `--min-confidence 100` to see only certain dead code. |
| `lint.py` | Fast lint pass via **ruff** (pyflakes + pycodestyle by default). Read-only unless you pass `--fix`. Narrow rule set via `--select`. Expect ~200 findings on the current codebase — most are E402 from our `sys.path.insert` bootstrap pattern. A curated `ruff.toml` baseline is a future task. |
| `complexity.py` | Cyclomatic complexity + maintainability index via **radon**. Defaults to showing functions rated C or worse. Pass `--cc` or `--mi` to narrow to one report. |

### Write (danger zone)

| Tool | Purpose |
|------|---------|
| `apply_tier_ladder.py` | ⚠️ **WRITES** settings for every customer in `FLEET`. One-shot setup for the 5-tier calibration experiment — tags each customer with `TIER=Tn`, `EXPERIMENT_ID`, `EXPERIMENT_START`, `EXPERIMENT_FREEZE=true`. Only run when you're deliberately applying the ladder; the tier_readout/customer_audit tools are how you inspect results afterwards. |

## Adding a new tool

1. Drop it in `tools/`.
2. Import shared fleet discovery:
   ```python
   from _fleet import iter_customers, project_root
   ```
3. Prefer stdlib + `src.retail_database.DB` over ad-hoc sqlite3 calls.
4. Default to read-only. If the tool writes, call it out in the
   docstring **and** this README with a ⚠️ marker.
5. Tools should be idempotent — safe to re-run any number of times.
