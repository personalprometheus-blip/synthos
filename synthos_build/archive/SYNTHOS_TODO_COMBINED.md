# Synthos — Combined TODO List
> Generated: 2026-03-27 | Sources: all project files scanned for TODO, FIXME, deferred, TBD, and open risk items.
> Deduplicated. Organized by source file. Priority inferred from context.

---

## agent1_trader.py

| # | Item | Priority | Notes |
|---|---|---|---|
| ~~T-01~~ | ~~Flip `TRADEABLE_PCT=0.80` / `IDLE_RESERVE_PCT=0.20`~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — Flipped in `agent1_trader.py`. `TRADEABLE_PCT=0.80`, `IDLE_RESERVE_PCT=0.20`. BIL sweep (T-02) still outstanding. |
| ~~T-02~~ | ~~Implement idle reserve → BIL sweep~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — `sync_bil_reserve()` added to `agent1_trader.py`. Runs every session as Step 2b after reconcile. Buys/sells BIL notional orders to maintain `IDLE_RESERVE_PCT` (20%) of total liquid. Excluded from position count and P&L. `BIL_REBALANCE_THRESHOLD=$10`. |
| ~~T-03~~ | ~~Subtract BIL position value from cash before storing~~ | ~~MEDIUM~~ | **RESOLVED 2026-03-27** — Fixed in `reconcile_with_alpaca()` cash sync: `DB cash = alpaca_free_cash + BIL_market_value` so `tradeable = total_liquid * TRADEABLE_PCT` stays correct. Uses `get_position_safe()` (404-tolerant) to avoid log noise when no BIL held. |
| T-04 | Gmail SMTP path — activate via command portal | LOW | Currently a placeholder. Toggle when Gmail credentials are configured. |

---

## agent3_sentiment.py

| # | Item | Priority | Notes |
|---|---|---|---|
| ~~T-05~~ | ~~Signals queued by The Daily not yet acted on by Trader~~ | ~~MEDIUM~~ | **RESOLVED 2026-03-27** — Three-part integration: (1) `database.py`: added `annotate_signal_pulse()` — appends pre-trade finding to `corroboration_note` without clobbering The Daily's existing note; (2) `agent3_sentiment.py`: Step 2 now calls `annotate_signal_pulse()` for elevated/critical findings (tier ≤ 2); (3) `agent1_trader.py`: `analyze_signal_with_claude()` now includes any `[PULSE ...]` annotation in the prompt as `⚠ PRE-TRADE PULSE WARNING`. |

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
| ~~T-11~~ | ~~Company restore workflow (`restore.sh`) does not exist~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — `restore.sh` implemented per Addendum 3.1 §3.1. Accepts encrypted (.enc) or pre-decrypted archive; decrypts via Fernet; extracts to `~/synthos-company/`; restores company.db and .env; sets permissions; installs Python deps; registers Strongbox cron; starts all company agents. Also fixed `strongbox.py` archive to include `user/` and `config/` (required for restore step c). |
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

## synthos_monitor.py / company node

| # | Item | Priority | Notes |
|---|---|---|---|
| T-21 | Pi comparison log — analyze differences between Pis | MEDIUM | Build a cross-Pi comparison report: signal decisions, member weight divergence, portfolio performance, validation rates, trade outcomes. Surfaces which Pis are behaving differently and why. Feeds Patches morning digest. |

---

## agent3_sentiment.py / company node — RSS Feed Distribution

| # | Item | Priority | Notes |
|---|---|---|---|
| T-22 | Build RSS/news feed distribution system | MEDIUM | Existing file: `synthos_build/free_public_api_source_list.html` (also check GitHub repo for latest version). Company node parses this file into a `feed_sources` DB table (url, name, tier, pull_count_today, is_active). Retail Pi calls `GET /api/feed` on company node to receive one random available feed URL. Each pull increments `pull_count_today` for that feed. When count exceeds threshold → feed temporarily disabled (web attack prevention). Cron at 00:01 resets `pull_count_today = 0` and re-enables all feeds. Replaces any hardcoded feed lists on retail Pi. Requires: parse `free_public_api_source_list.html`, `feed_sources` table in company DB, `/api/feed` endpoint on company node, retail-side `get_feed_url()` caller, cron reset job. |

---

## sentinel_daynight.py

| # | Item | Priority | Notes |
|---|---|---|---|
| T-30 | Fix astral 3.x timezone compatibility in `_calculate_mode` | LOW | `loc.timezone` returns a string in astral 3.x but `sun()` requires a `tzinfo` object. Fix: `from zoneinfo import ZoneInfo` and pass `tzinfo=ZoneInfo(tz_name)`. Calculation is currently commented out; defaults to day mode. |

---

## Summary by Priority

| Priority | Count | Items |
|---|---|---|
| HIGH | 7 | T-23, T-24, T-25, T-26, T-27, T-28, T-29 — PRE-BETA SECURITY (all blocking) |
| MEDIUM | 7 | T-07, T-10, T-15, T-16, T-21, T-22 (T-08 resolved) |
| LOW | 8 | T-04, T-09, T-12, T-17, T-18, T-19, T-20, T-30 |

---

## Deduplication Notes

- T-15 and T-16 both reference IP allowlisting activation — kept as separate items because they appear in different spec docs with slightly different context (architecture vs. operations). Treat as one task in practice.
- `TBD` effort tags in `patches.py` and `migrate_agents.py` (T-18) are the same pattern — consolidated into one item.
- Strongbox references appear throughout multiple docs — all consolidated under T-13 and T-14.

---

## PRE-BETA SECURITY HARDENING

> **BLOCKING:** All items below must be completed before the system goes to beta.
> The current setup uses default/dev credentials throughout. Every access point
> must be secured before the system is exposed to customers or real trading capital.

| # | Item | Priority | Notes |
|---|---|---|---|
| T-23 | Rotate all Raspberry Pi user passwords | HIGH | Default or dev passwords on all Pi nodes (retail, monitor, company) must be replaced with strong unique passwords before beta. |
| T-24 | Set a strong Samba (SMB) password | HIGH | Current SMB share on monitor Pi uses dev credentials. Replace with a strong password and confirm guest access is disabled. |
| T-25 | Secure all web-facing domains and portals | HIGH | All domains and subdomains (portal, monitor, Cloudflare tunnels) must use HTTPS with valid certs. Confirm no unauthenticated endpoints are exposed. |
| T-26 | Harden portal.py authentication | HIGH | Web portal (port 5001) must require authentication before any kill switch, approval, or news feed access is permitted. See T-07. |
| T-27 | Audit and rotate all API keys and secrets | HIGH | Alpaca, SendGrid, Cloudflare R2, Anthropic — all keys in `.env` files should be reviewed, rotated, and confirmed to have minimum required permissions. |
| T-28 | Restrict SSH access | HIGH | Disable password-based SSH login; enforce key-only auth on all Pi nodes. Activate IP allowlisting (see T-15/T-16) once IP list is stable. |
| T-29 | Review firewall rules on all nodes | HIGH | Confirm only required ports are open (e.g. 5000, 5001) and all other inbound traffic is blocked. |
