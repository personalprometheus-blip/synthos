# news_baseline — deterministic parity fixtures for C8 refactor

This directory captures byte-identical output fixtures for the news-agent
classification pipeline. Used by **backlog item C8** (news-agent gate-pipeline
refactor) to verify the refactored `retail_news_agent.run()` produces the same
`signal_decisions`, `signals`, and `system_log` rows as the pre-refactor
implementation.

## Files

```
fixtures/
  cycle_01_input.json      # fixed headlines + stubbed bars + frozen clock
capture_baseline.py        # run the agent deterministically, write cycle_NN.json
diff_baseline.py           # compare a fresh capture against the stored baseline
cycle_01.json              # the stored baseline (REFERENCE — do not regenerate
                           #   without project lead sign-off)
README.md                  # this file
```

## How capture works

`capture_baseline.py`:
1. Loads `fixtures/<cycle>_input.json` — fixed headlines, stubbed Alpaca bar
   data, frozen clock, optional member-weight seed.
2. Creates a temp sqlite DB, schema initialized by `retail_database.DB()`.
3. Monkey-patches every external dependency of `retail_news_agent`:
   - `fetch_alpaca_news_historical` / `fetch_alpaca_news_for_ticker` →
     return fixture articles.
   - `_alpaca_bars` / `fetch_price_history_1yr` → return fixture price data.
   - `fetch_and_store_alpaca_display_news` → no-op.
   - `fetch_with_retry` → defensive no-op for any remaining network call.
   - `announce_for_interrogation` → no-op (no UDP broadcast).
   - `post_to_company_pi` → no-op (no HTTP POST).
   - `datetime.datetime` (in both `retail_news_agent` and `retail_database`)
     → a `FrozenDatetime` whose `now()`/`utcnow()` return the fixture clock.
4. Calls `retail_news_agent.run(session=...)`.
5. Dumps `signals`, `signal_decisions`, and `system_log` tables to JSON with
   `sort_keys=True`.

Result is deterministic across runs — only the top-level `captured_at`
metadata varies (wall-clock time of the run, ignored by the diff tool).

## Typical workflow (C8 parity check)

```bash
# 1. Capture a fresh cycle from the current agent
python3 tests/news_baseline/capture_baseline.py --cycle cycle_01 \
    --out /tmp/fresh_cycle_01.json

# 2. Diff against the stored baseline
python3 tests/news_baseline/diff_baseline.py \
    tests/news_baseline/cycle_01.json \
    /tmp/fresh_cycle_01.json

# Exit 0 = IDENTICAL → parity OK
# Exit 1 = DIFFERENCES — refactor changed classifier output
# Exit 2 = file-load error
```

Before landing a refactored agent on main, run the full cycle suite and
confirm every `cycle_*.json` fixture still diffs IDENTICAL.

## Adding a new cycle fixture

1. Create `fixtures/cycle_NN_input.json` with new headlines covering the
   behavior you want to lock down (e.g. cross-validation, cascades,
   multi-ticker articles, macro vs. single-name).
2. Run `capture_baseline.py --cycle cycle_NN` once against the trusted
   pre-refactor agent — this writes `cycle_NN.json`.
3. Commit both the input and the output to the `patch/YYYY-MM-DD` branch
   alongside the eventual refactor. Do NOT commit fixtures to `main` in
   isolation — they're meaningful only paired with the refactor they
   protect.

## What's covered / what's not

The fixture is engineered so the frozen clock (`frozen_clock_utc` in the
input JSON) sits within the TRADEABLE_WINDOW_HOURS of the articles'
`disc_date` — otherwise every article short-circuits at gate 13 and the
baseline has zero pipeline output.

The fixture does NOT exercise:
- Alpaca news *display* (`fetch_and_store_alpaca_display_news` is no-op'd).
- Company-Pi POST-back (`post_to_company_pi` is no-op'd).
- UDP peer corroboration (`announce_for_interrogation` is no-op'd).
- Real historical price bar reactions (bars are summary-shaped stubs).

Those paths are tested elsewhere (see `tests/synthos_test.py` and the
live agent in production). The baseline's job is specifically to freeze
the **22-gate classification decision output** for parity.

## Entry condition #3 for C8

Backlog item C8 requires "at least 5 full enrichment cycles" captured
before the refactor can begin. Track progress with:

```bash
python3 synthos_build/tools/c8_readiness.py
```

The readiness tool counts files matching `cycle_*.json` in this directory.
