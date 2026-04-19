# DEPLOY_NOTES — `patch/2026-04-24`

**Target merge date:** 2026-04-24
**Purpose:** C8 news-agent gate-pipeline refactor prep (not the refactor itself)

This file lives on the `patch/2026-04-24` branch only. Do not merge it to `main` —
delete it as part of the merge commit, or roll its contents up into the merge PR
description.

---

## Files landing on main when this patch merges

### New tooling (safe — inert until invoked)

| File | What it does | Deploy impact |
|---|---|---|
| `tests/news_baseline/capture_baseline.py` | Deterministic news-agent cycle capture | None — manual invocation only |
| `tests/news_baseline/diff_baseline.py` | Parity diff for C8 refactor | None — manual invocation only |
| `tests/news_baseline/fixtures/cycle_*_input.json` | Test fixtures | None — not read at runtime |
| `tests/news_baseline/cycle_*.json` | Captured baselines | None — reference fixtures |
| `tests/news_baseline/README.md` | Workflow docs | None |

### Production code changes

| File | Change | Why | Runtime impact |
|---|---|---|---|
| `synthos_build/agents/retail_news_agent.py` | `from __future__ import annotations` added at top | Lets the harness import the module on macOS Python 3.9 (Pi runs 3.13) | **None.** Makes all type hints strings at parse time; we do not call `typing.get_type_hints()` anywhere. Python 3.14 will make this default. |

### Meta

| File | Change | Why |
|---|---|---|
| `.gitignore` | Add `.venv-baseline/` | Keep local Python 3.13 venv out of git |
| `DEPLOY_NOTES.md` | this file | **DELETE before merging to main** |

---

## Pre-merge checklist

Before running `git merge --ff-only patch/2026-04-24` on main:

- [ ] `tools/c8_readiness.py` reports all four entry conditions PASS
- [ ] At least 5 `cycle_*.json` fixtures exist in `tests/news_baseline/`
- [ ] Every cycle re-captures byte-identical (run harness twice, diff must say IDENTICAL)
- [ ] This DEPLOY_NOTES.md is either deleted or its contents rolled into the merge commit message
- [ ] Pi 5 uptime > 24 h with no PIPELINE_STALL (sanity-check; `system_health_daily` table should already confirm this)

## Post-merge verification

After Pi pulls `main`:
- [ ] `python3 synthos_build/agents/retail_news_agent.py --session=overnight` runs without import errors
- [ ] `tail -n 50 logs/scheduler.log` — no new ERROR lines after the news session fires
- [ ] `python3 synthos_build/tools/c8_readiness.py` still runs (it reads the same shared DB)

## Rollback plan

`git revert` the merge commit → `git push main` → `git pull` on Pi → restart `synthos-portal.service`. The `from __future__` line reverting is safe on Pi since it was a no-op anyway.

---

## Why the patch-branch workflow

`main` is the Pi's deploy path — any push reaches the retail trading agent on the next `git pull`. The C8 refactor (future) and its baseline harness need to land together so that the baselines remain valid. Splitting them or landing the refactor without the harness would leave no way to catch a regression.

See `~/.claude/projects/-Users-patrickmcguire-synthos/memory/feedback_patch_branch_workflow.md`
for the standing rule.
