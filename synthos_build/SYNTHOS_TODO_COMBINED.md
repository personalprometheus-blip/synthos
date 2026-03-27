# Synthos — Combined TODO List
> Generated: 2026-03-27 | Sources: all project files scanned for TODO, FIXME, deferred, TBD, and open risk items.
> Deduplicated. Organized by source file. Priority inferred from context.

---

## agent1_trader.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-01 | Flip `TRADEABLE_PCT=0.80` / `IDLE_RESERVE_PCT=0.20` | HIGH | Post-validation only. Currently conservative defaults. |
| T-02 | Implement idle reserve → BIL sweep | HIGH | Cash held idle instead of swept to BIL. Cash sync from Alpaca is live; BIL logic is the remaining step. |
| T-03 | Subtract BIL position value from cash before storing | MEDIUM | After BIL sweep is implemented. Required so tradeable math stays correct. See `reconcile_with_alpaca()`. |
| T-04 | Gmail SMTP path — activate via command portal | LOW | Currently a placeholder. Toggle when Gmail credentials are configured. |

---

## agent3_sentiment.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-05 | Signals queued by The Daily not yet acted on by Trader | MEDIUM | Sentiment signals are being queued but the Trader is not consuming them. Integration step needed. |

---

## install.py (legacy installer)

| # | Item | Priority | Notes |
|---|---|---|---|
| ~~T-06~~ | ~~Rename `install.py` → `install.py.deprecated`~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — `install.py` was never committed to the repo. All docs (SYSTEM_MANIFEST.md, TOOL_DEPENDENCY_ARCHITECTURE.md, SYNTHOS_GROUND_TRUTH.md) updated to reference `install_retail.py` as canonical; `install.py` marked deprecated in FILE_STATUS. |
| T-07 | Authentication and HTTPS for installer web UI | MEDIUM | Currently unprotected. Planned for a future release. |

---

## install_retail.py / install_company.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-08 | Wire `seed_backlog.py` into company installer | MEDIUM | Currently not run automatically. Operator must run manually after install: `python3 agents/../seed_backlog.py` |

---

## setup_tunnel.sh

| # | Item | Priority | Notes |
|---|---|---|---|
| T-09 | Migrate to named Cloudflare tunnel with real domain | LOW | Currently using temporary tunnel. Named tunnel + real domain deferred. |

---

## SYNTHOS_INSTALLER_ARCHITECTURE.md — Open Risks

| # | Item | Priority | Notes |
|---|---|---|---|
| T-10 | `first_run.sh` hardcodes `/home/pi/synthos` path | MEDIUM | Known issue flagged in manifest. Out of scope for installers. Separate refactor task required. |
| T-11 | Company restore workflow (`restore.sh`) does not exist | HIGH | Addendum 3.1 references it. Strongbox (Agent 12) owns this when implemented. |
| T-12 | License key validation at install time | LOW | Installer collects key but cannot validate (no Vault at install time). Validation deferred to `boot_sequence.py` on first boot. Decided/acceptable — not a silent failure. |

---

## SYNTHOS_TECHNICAL_ARCHITECTURE__1_.md

| # | Item | Priority | Notes |
|---|---|---|---|
| ~~T-13~~ | ~~Strongbox (`strongbox.py`) not yet implemented~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — `strongbox.py` implemented as Agent 12 (Backup Manager). Covers: company.db backup, staged retail Pi archive processing, Cloudflare R2 upload + retention (30 days), integrity verification, restore orchestration, Scoop alerts. See file for full details. |
| ~~T-14~~ | ~~Session-end trigger mechanism for post-trading backup~~ | ~~MEDIUM~~ | **RESOLVED 2026-03-27** — Resolved by decision: daily 2am cron schedule is the primary trigger for Phase 1. Session-end triggering deferred; when implemented, `synthos_heartbeat.py` will include a backup payload in its POST to the company Pi. Documented in `strongbox.py` header. |
| T-15 | IP allowlisting (`config/allowed_ips.json`) — deferred | MEDIUM | Will block SSH from unexpected IPs. Deferred until IP list is stable and SSH access is confirmed from all locations. |

---

## SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md

| # | Item | Priority | Notes |
|---|---|---|---|
| T-16 | IP allowlisting activation | MEDIUM | Duplicate of T-15. Config stub written by installer; enforcement in Sentinel is not yet active. |
| T-17 | Direct Pi-to-Pi communication (mutual TLS) | LOW | If implemented in future, requires mutual TLS with Vault-issued certificates. No current use case. |

---

## patches.py / migrate_agents.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-18 | Blueprint effort estimates marked `TBD` | LOW | Both files emit `effort="TBD — Blueprint to assess"` when queuing suggestions. Blueprint should fill these in during its first run on any new suggestion. Not a blocker. |

---

## SYNTHOS_OPERATIONS_SPEC.md — Future Considerations

| # | Item | Priority | Notes |
|---|---|---|---|
| T-19 | Hybrid cloud model at ~20–30 customers | LOW | Physical Pi logistics may not scale. Fidget will flag when cost/complexity warrants. No action required now. |

---

## scoop.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-20 | Active transport toggle via command portal | LOW | Scoop currently has a fixed transport path. Command portal control of active transport is a future feature. |

---

## Summary by Priority

| Priority | Count | Items |
|---|---|---|
| HIGH | 3 | T-01, T-02, T-11 (T-06, T-13 resolved) |
| MEDIUM | 7 | T-03, T-05, T-07, T-08, T-10, T-15, T-16 (T-14 resolved) |
| LOW | 7 | T-04, T-09, T-12, T-17, T-18, T-19, T-20 |

---

## Deduplication Notes

- T-15 and T-16 both reference IP allowlisting activation — kept as separate items because they appear in different spec docs with slightly different context (architecture vs. operations). Treat as one task in practice.
- `TBD` effort tags in `patches.py` and `migrate_agents.py` (T-18) are the same pattern — consolidated into one item.
- Strongbox references appear throughout multiple docs — all consolidated under T-13 and T-14.
