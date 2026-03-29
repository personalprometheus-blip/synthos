# BACKUP STRATEGY — INITIAL POLICY

**Version:** 1.0
**Date:** 2026-03-29
**Status:** Active — canonical backup policy for Synthos
**Owned by:** Project lead
**Implementation owner:** Strongbox (Agent 12)

---

## OVERVIEW

This document defines the canonical backup policy for Synthos. It establishes the tiered snapshot model that governs backup creation, retention, and cleanup. It is the authoritative source for backup policy. Where other documents reference backup behavior, this document takes precedence.

This document defines the policy target. Implementation is staged and may not yet be complete. Nothing in this document implies that described mechanics are fully built.

---

## BACKUP MODEL

### A. MONTHLY BASELINE SNAPSHOT

- A full system backup is taken once per month
- This baseline snapshot becomes the new recovery anchor for that month
- "Full system backup" means a full required system recovery snapshot within the Synthos-managed scope: configuration files, primary database, and all explicitly defined required backup artifacts
- This is not necessarily a block-level clone of the entire device unless later expanded to that scope

### B. NIGHTLY INCREMENTAL BACKUPS

- Nightly backups capture only changes since the last backup
- These incrementals are associated with the current monthly baseline chain
- "Incremental" is the canonical backup requirement
- The exact implementation mechanism for delta tracking may be staged; this policy defines the requirement, not the implementation internals

### C. CHAIN RESET RULE

When a new monthly baseline snapshot is created:
- The previous incremental backup chain is deleted
- The new baseline becomes the active anchor for future nightly incrementals

### D. RETENTION RULE

- Retain full baseline snapshots for 6 months
- Delete full baseline snapshots older than 6 months
- Delete obsolete incremental chains once superseded by a new baseline

---

## BACKUP SCOPE

Synthos backups cover the following explicitly defined artifacts:

| Artifact | Description |
|----------|-------------|
| Primary database | company.db (company node) |
| Configuration files | .env, config/ directory contents |
| Required recovery artifacts | Any explicitly designated files needed for restore |

Backup scope does NOT automatically include:
- Logs (unless explicitly added to required artifacts)
- Full uncontrolled filesystem copies

The monthly "full baseline snapshot" is a full snapshot within the Synthos-managed recovery scope. It is not a raw block-device image of the entire SD card unless that is later explicitly implemented and designated.

---

## SCHEDULING

- **Nightly incremental backup window:** 11:00 PM – 12:00 AM local time
- Backup execution is eligible during this window
- Scheduling and execution must align with the timekeeper-controlled access model where applicable
- No new locking semantics are introduced by this document; scheduling defers to existing timekeeper governance

---

## RETENTION AND CLEANUP

| Object | Retention |
|--------|-----------|
| Full monthly baseline snapshot | 6 months |
| Nightly incremental chain | Until superseded by new baseline |

Cleanup behavior:
- Each new monthly baseline snapshot triggers deletion of the prior incremental chain
- Full baseline snapshots older than 6 months are deleted
- Cleanup must be automatic, not manual-only

---

## AGENT RESPONSIBILITIES

| Agent | Role |
|-------|------|
| Strongbox | Backup creation, retention enforcement, cleanup execution |
| Timekeeper | Scheduling / access timing governance where applicable |

Strongbox is accountable for:
- Creating monthly baseline snapshots on schedule
- Creating nightly incrementals within the defined window
- Executing baseline-linked cleanup of obsolete incremental chains
- Enforcing 6-month baseline retention deletion

Timekeeper governs scheduling and access timing where applicable. This document does not redefine timekeeper as a new global lock manager.

---

## CURRENT LIMITATIONS

The following limitations apply to the current implementation state. They are known, accepted, and tracked for future resolution.

| Limitation | Status |
|-----------|--------|
| Incremental mechanism | May require staged implementation; incremental tracking mechanics are not yet fully built |
| External redundancy | None — backups are local-only |
| RAID / NAS storage target | Not implemented; deferred to future evaluation |
| Cloud / off-device backup | Not implemented; deferred to future evaluation |
| Encryption | Not implemented; deferred to future evaluation |
| Backup integrity verification | Not implemented; deferred to future evaluation |
| Full snapshot scope | Defined as Synthos-managed recovery scope; not a complete raw-device image unless later expanded |

---

## IMPLEMENTATION STATUS

| Component | Status |
|-----------|--------|
| Policy defined | YES — this document |
| Monthly baseline snapshot | NOT YET IMPLEMENTED |
| Nightly incremental chain | NOT YET IMPLEMENTED |
| Baseline-linked retention cleanup | NOT YET IMPLEMENTED |
| Restore path from baseline + chain | NOT YET VERIFIED |

Strongbox (Agent 12) is pre-approved and pending implementation. The backup policy is established ahead of implementation to provide a clear, stable target.
