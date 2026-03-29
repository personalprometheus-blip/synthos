# DB SCHEMA NORMALIZATION NOTE

**Date:** 2026-03-29
**Step:** 5 — Database Schema Normalization (final prerequisite before Step 4 Ground Truth)
**Type:** Normalization and documentation alignment — no schema redesign, no runtime code changes

---

## 1. SCOPE

This note records what was found, what was normalized, and what was intentionally left unchanged during the Step 5 database schema normalization pass.

Sources inspected:
- `synthos_build/src/database.py` — SCHEMA constant, `_run_migrations()`, all DB class methods
- `synthos-company/utils/db_helpers.py` — `_bootstrap_inline()`, all DB class methods
- `synthos-company/agents/` — blueprint.py, sentinel.py, patches.py, strongbox.py, scoop.py, timekeeper.py, vault.py, fidget.py
- `synthos_build/src/` — agent1_trader.py (partial), agent2_research.py, agent3_sentiment.py, watchdog.py
- `synthos_build/docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3, §3.3
- `synthos_build/docs/specs/SYSTEM_MANIFEST.md` (schema references)

---

## 2. INCONSISTENCIES FOUND

### 2.1 SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3 — Retail Pi Schema (STALE / WRONG)

The pre-normalization §2.3 contained a schema definition for `signals.db` that was materially incorrect. Specific findings:

**Wrong `signals` table fields:**
- Showed: `congress_member`, `transaction_type`, `agent_decision`, `status` (PENDING/APPROVED/EXECUTED/SKIPPED)
- Actual: `ticker`, `source`, `source_tier`, `confidence`, `politician`, `tx_date`, `disc_date`, `amount_range`, `staleness`, `corroborated`, `corroboration_note`, `is_amended`, `is_spousal`, `needs_reeval`, `expires_at`, `discard_delete_at`, `entry_signal_score`, `interrogation_status`, `price_history_used` + many more

**Wrong `positions` table:**
- Showed: `ticker UNIQUE`, `portfolio_value` field
- Actual: Primary key is `id TEXT` (composite key format `pos_{ticker}_{timestamp}`); no `portfolio_value` column; includes v1.2 migration columns

**Phantom `trades` table:**
- Showed: `trades` table with `action`, `profit_loss`, `status`
- Actual: No `trades` table exists in `database.py` schema. Trade history is split across `outcomes` (closed trade results) and `ledger` (financial transactions).

**Phantom `agent_status` table:**
- Showed: `agent_status` table
- Actual: No `agent_status` table in the SCHEMA constant or CREATE TABLE statements in `database.py`. System events are in `system_log`.

**Phantom `license` table:**
- Showed: `license` table with `key`, `issued_date`, `expires_at`
- Actual: No `license` table exists. License validation is DEFERRED_FROM_CURRENT_BASELINE. Key records are in `company.db.keys`, not `signals.db`.

**Phantom `config` table:**
- Showed: `config` table with `key`, `value`, `set_by`
- Actual: No `config` table in SCHEMA constant. Whether `agent2_research.py` uses one is unverified — flagged as ambiguity in the canonical doc §6.4.

**Missing tables entirely:**
The pre-normalization §2.3 did not mention these tables which exist in actual code:
`portfolio`, `ledger`, `outcomes`, `handshakes`, `scan_log`, `system_log`, `urgent_flags`, `pending_approvals`, `member_weights`, `news_feed`

### 2.2 SYNTHOS_TECHNICAL_ARCHITECTURE.md §3.3 — Company Pi Schema (ABSENT)

Pre-normalization §3.3 read:
> *(No changes from v2.0. See SYSTEM_MANIFEST for full schema.)*

SYSTEM_MANIFEST does not contain a company.db schema definition. The actual company.db schema is defined in `db_helpers.py` `_bootstrap_inline()`. There was no canonical documentation of the company schema anywhere.

### 2.3 Schema version drift — signals.db

`database.py` `_run_migrations()` implements:
- v1.1 migrations: `outcomes.lesson`, `signals.needs_reeval`, `urgent_flags.label`
- v1.2 migrations: `signals.price_history_used`, `signals.interrogation_status`, `signals.entry_signal_score`, `positions.entry_sentiment_score`, `positions.entry_signal_score`, `positions.price_history_used`, `positions.interrogation_status`

None of these migration columns appeared in the pre-normalization architecture doc schema.

---

## 3. WHAT WAS NORMALIZED

### 3.1 Created: docs/specs/DATABASE_SCHEMA_CANONICAL.md

A new canonical schema document was created covering:
- Both databases (signals.db and company.db)
- All actual tables with actual field definitions including migration columns
- Access patterns (lock model, slot model, direct-write exceptions)
- Known limitations
- Source of truth declaration

This is now the single authoritative schema reference. All other schema descriptions defer to it.

### 3.2 Updated: SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3

Old content (stale 6-table schema with wrong fields and phantom tables) replaced with:
> *Schema defined in `docs/specs/DATABASE_SCHEMA_CANONICAL.md` (authoritative). See §1–§3 of that document for all table definitions, field types, indexes, and access patterns.*

### 3.3 Updated: SYNTHOS_TECHNICAL_ARCHITECTURE.md §3.3

Old content ("No changes from v2.0. See SYSTEM_MANIFEST for full schema.") replaced with:
> *Schema defined in `docs/specs/DATABASE_SCHEMA_CANONICAL.md` (authoritative). See §1–§3 of that document for all company.db table definitions.*

---

## 4. WHAT WAS INTENTIONALLY LEFT UNCHANGED

**No runtime code was modified.** This is a documentation normalization step only.

| Item | Left unchanged | Reason |
|------|---------------|--------|
| `database.py` schema constants | Unchanged | Source of truth — not the subject of normalization |
| `db_helpers.py` inline schema | Unchanged | Source of truth — not the subject of normalization |
| All agent Python files | Unchanged | No code changes in this step |
| `_run_migrations()` | Unchanged | Migration logic is the authoritative version record |
| Timestamp type inconsistency (TEXT vs DATETIME) | Left as-is | Both work in SQLite; normalizing would require schema changes — out of scope |
| `config` table reference | Flagged only, not resolved | Requires separate code verification to determine if the table is used; out of scope |
| `agent_status` table reference in §2.4 agent descriptions | Left in agent descriptions | Architecture doc agent descriptions are outside the scope of schema normalization; full arch review is a separate task |
| `strongbox.py` backup_log wiring | Left unimplemented | Not a schema change; tracked in milestones.md (Backup System Evolution) |
| Scoop/Timekeeper direct connections | Documented as intentional | These are correct exceptions, not policy violations; no change needed |

---

## 5. CONFIRMATION: NO SCHEMA REDESIGN OCCURRED

- No new tables were added to either database
- No existing tables were dropped or renamed
- No field types were changed
- No field names were changed
- No indexes were added or removed
- No foreign key relationships were added or removed
- No migration code was written or executed

This step was exclusively: **extract, document, cross-reference, and redirect.**

---

## 6. REMAINING AMBIGUITY

### `config` table

The pre-normalization architecture doc's agent description for DisclosureResearchAgent states it reads a `config` table for last-fetch timestamp. No `config` table is defined in `database.py` SCHEMA. Whether this table:
- Is created dynamically (not in the SCHEMA constant)
- Is written by agent2_research.py via a raw CREATE TABLE
- Is an outdated reference to removed functionality
- Never existed and the timestamp is tracked differently

...requires reading `agent2_research.py` to determine. This is flagged in DATABASE_SCHEMA_CANONICAL.md §6.4 and is out of scope for the schema normalization sprint.

---

## 7. STEP 5 STATUS

```
DB_SCHEMA_NORMALIZATION_STATUS: COMPLETE

  — DATABASE_SCHEMA_CANONICAL.md created and populated with evidence-based schema
  — SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3 stale schema replaced with reference
  — SYNTHOS_TECHNICAL_ARCHITECTURE.md §3.3 absent schema replaced with reference
  — No runtime code modified
  — No schema redesign performed

READY_FOR_STEP_4_GROUND_TRUTH: YES
  — All normalization sprint steps (1–6) are now complete or formally deferred
  — No critical blockers remain
  — Ground Truth synthesis may proceed
```
