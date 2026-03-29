# SYNTHOS MILESTONES

**Last Updated:** 2026-03-29
**Authority:** Tracks future-phase work items. For active sprint and phase progress, see PROJECT_STATUS.md.

---

## Backup System Evolution / Hardening

These milestones implement and extend the canonical backup policy defined in `docs/specs/BACKUP_STRATEGY_INITIAL.md`. They belong in a future phase after the normalization sprint and deployment pipeline are stable. They must not be silently merged into the current normalization sprint unless explicitly scheduled there.

### Core Implementation (required for baseline backup capability)

- [ ] Implement monthly baseline snapshot generation (Strongbox)
- [ ] Implement nightly incremental backup chain (Strongbox)
- [ ] Implement baseline-linked cleanup: delete prior incremental chain on new baseline
- [ ] Implement 6-month baseline retention deletion (automatic)
- [ ] Verify restore path from baseline + incremental chain
- [ ] Integrate backup health reporting into morning digest (Patches / Strongbox)

### Future Evaluation (not in scope until core is stable)

- [ ] Evaluate future RAID / NAS backup target
- [ ] Evaluate future cloud / off-device backup (e.g. Cloudflare R2 or equivalent)
- [ ] Evaluate encrypted backup support
- [ ] Evaluate backup integrity verification (spot-check after write)
- [ ] Evaluate raw block-device snapshot scope if SD card resilience requires it

---

## Retail Entitlement / License System

**Classification:** FUTURE_RETAIL_ENTITLEMENT_WORK — DEFERRED_FROM_CURRENT_BASELINE

Retail license validation was formally deferred on 2026-03-29. The LICENSE_KEY is collected during setup and stored in `.env`. No validation occurs in the current release. These milestones implement the full retail entitlement gate after ground truth declaration and ground truth lock.

Do not attempt any of these tasks during the current normalization sprint, ground truth synthesis, or deployment pipeline phases unless explicitly reprioritized.

### Core Implementation (required for retail entitlement enforcement)

- [ ] Build `license_validator.py` — HMAC key validation, Pi ID binding, online registry check
- [ ] Define retail boot-time entitlement flow (wire license_validator.py into boot_sequence.py)
- [ ] Define behavior for invalid / expired / revoked key at boot
- [ ] Define behavior for offline operation: cache model, grace period
- [ ] Wire `LICENSE_KEY` env var into validator; define required key format
- [ ] Add `license_validator.py` back to `REQUIRED_CORE_FILES` in install_retail.py once built
- [ ] Add `LICENSE_KEY` back to installer verification required_keys once validator is wired
- [ ] Define and test retail Pi key re-validation cadence (cache expiry model from ADDENDUM_1 §2)

### Integration and Validation

- [ ] Validate retail entitlement flow end-to-end: install → boot → online validation → cached operation
- [ ] Validate revoked key behavior: halt, clear cache, log
- [ ] Validate offline grace period behavior
- [ ] Validate Pi ID binding (key cannot be copied to a different Pi)
- [ ] Add HMAC anti-spoofing validation to validator

### Post-Trading Hardening

- [ ] Vault: implement stricter license validation in post-trading mode (no grace period, immediate halt on revoked key)
- [ ] Vault: key rotation flow (SUPERSEDED → new key transparent to customer)
- [ ] Vault: rate limiting on validation endpoint (per ADDENDUM_1 §2.4)

---

## Notes

- "Incremental" is the canonical backup requirement. Implementation mechanism may be staged.
- Cloud, encryption, and RAID are explicitly deferred. Do not conflate evaluation with implementation.
- Milestone status here does not imply these are scheduled — they are tracked, not committed.
