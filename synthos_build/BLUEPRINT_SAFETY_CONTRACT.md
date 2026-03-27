# BLUEPRINT SAFETY CONTRACT
## Non-Negotiable Deployment Rules for Blueprint (engineer.py)

**Document Version:** 1.0
**Date:** 2026-03-27
**Status:** Active — governs all Blueprint operations
**Incorporates:** BLUEPRINT_WORKFLOW_SPEC_ADDENDUM_1.md patterns (now formally contracted)
**Audience:** Blueprint, Patches, Project Lead

---

## 1. OBJECT DEFINITION

### What a Deployment Operation Is

A **deployment operation** is any action by Blueprint that modifies, creates, or deletes a file that:
- Lives in `${SYNTHOS_HOME}/core/` on any retail Pi, OR
- Lives in `${SYNTHOS_HOME}/agents/`, `${SYNTHOS_HOME}/services/`, or `${SYNTHOS_HOME}/utils/` on the company Pi, OR
- Is a registered runtime state artifact (`suggestions.json`, `post_deploy_watch.json`, etc.)

A deployment operation is NOT:
- Writing to Blueprint's own staging workspace (`.blueprint_staging/`)
- Reading any file
- Appending to `audit_log` in `suggestions.json`
- Writing Blueprint's own log files

Every deployment operation must be traceable to an approved suggestion in `suggestions.json`. Blueprint may not perform a deployment operation without a suggestion in `approved` or `in_progress` state for the target file(s).

---

## 2. LIFECYCLE MODEL

Every Blueprint deployment passes through these stages, in this exact order. No stage may be skipped.

### Stages

**`pre-stage`**
Blueprint has an approved suggestion and is preparing to implement. Activities: read the target file(s), call Claude API, generate the staged content. The live file is never touched at this stage.

Entry condition: Suggestion in `suggestions.json` has `status: "approved"` and Blueprint has self-assigned via `approved → in_progress` transition.

Exit condition: Generated content has been written to `.staged` file in `.blueprint_staging/<suggestion_id>/`.

**`staged`**
The generated content exists in `.blueprint_staging/<suggestion_id>/`. All pre-deploy validation checks are run. The live file is still untouched.

Entry condition: `.staged` file exists and is non-empty.

Exit condition: ALL checks in the pre-deploy checklist pass (see Section 4). If any check fails, the staged file is deleted and Blueprint returns to `pre-stage` for regeneration.

**`validated`**
All pre-deploy checks have passed. The `.bak` file has been written (original content backed up). The `.tmp` file has been written (staged content under its final name). The atomic `os.replace()` has not yet fired.

Entry condition: All checklist items green. `.bak` exists. `.tmp` exists. Both verified as complete writes.

Exit condition: `os.replace(<filename>.tmp, <filename>)` executes successfully. The live file is now the new version.

**`deployed`**
The live file has been atomically replaced with the staged content. `.staged` and `.tmp` files have been deleted. `.bak` is retained until post-deploy watch closes stable.

Entry condition: `os.replace()` completed. Exit code verified.

Exit condition: `suggestions.json` entry updated to `staged` status. `post_deploy_watch.json` entry initialized. Blueprint writes `staging_manifest` to suggestion.

**`failed`**
Any stage failed before `os.replace()` completed. The live file was never modified.

Entry condition: Any exception or check failure in `pre-stage`, `staged`, or `validated`.

See Section 5 (Failure Behavior) for exact rollback and cleanup requirements.

---

## 3. RULES (NON-NEGOTIABLE)

The following rules have no exceptions. Blueprint may not bypass them for any reason, including time pressure, trivial changes, or Project Lead instruction to "just do it quickly." If the Project Lead needs a change bypassed, the Project Lead performs it manually. Blueprint's role in the pipeline is subject to these rules unconditionally.

### Rule 1: Atomic Deploy Sequence (MANDATORY)

Blueprint never writes directly to a live file. Every file change follows this exact sequence:

```
Step 1:  Write new content to .blueprint_staging/<suggestion_id>/<filename>.staged
Step 2:  Read back .staged and verify the write is byte-perfect (len match)
Step 3:  Run ast.parse() on .staged file (syntax check — Python files only)
Step 4:  Run truncation guard check (see Rule 2)
Step 5:  Write original live file content to .blueprint_staging/<suggestion_id>/<filename>.bak
Step 6:  Write .staged content to <live_file_path>.tmp (same directory as live file)
Step 7:  os.replace(<live_file_path>.tmp, <live_file_path>)   ← POSIX atomic rename
Step 8:  Delete .staged file
Step 9:  Retain .bak until post_deploy_watch closes stable
```

If anything fails at Steps 1–6: clean up `.staged` and `.tmp`. The live file is never touched.

`os.replace()` is atomic on POSIX. After Step 7, the file is either the old version or the new version — never partial.

### Rule 2: Truncation Guard (MANDATORY)

Before deploying any generated file, Blueprint enforces:

```
if len(staged_content_bytes) < len(original_content_bytes) * 0.60:
    ABORT — possible truncation
    Log: "Staged file too short: {staged_bytes} bytes vs {original_bytes} bytes.
          Ratio: {ratio:.2f}. Threshold: 0.60. Possible truncation — live file untouched."
    Delete .staged
    Return to pre-stage for regeneration
```

The 0.60 threshold is the default. If a suggestion is expected to significantly reduce file size (legitimate refactor), Blueprint must document this in `audit_log` BEFORE staging with the field `expected_size_reduction: true` and a justification. Patches must acknowledge this before Blueprint proceeds to deploy.

### Rule 3: Staging Directory (MANDATORY)

Blueprint's workspace is exclusively:

```
${SYNTHOS_HOME}/.blueprint_staging/
```

This directory is:
- Never committed to git (enforced via `.gitignore`)
- Never read by any agent other than Blueprint (Patches may inspect it for diagnostics only — never writes)
- Cleaned at the start of each Blueprint run (stale artifacts from failed previous runs are removed)
- Named per suggestion: `.blueprint_staging/<suggestion_id>/`

Contents during an active implementation:
```
.blueprint_staging/<suggestion_id>/
├── <filename>.staged      ← generated content, pre-deploy
├── <filename>.bak         ← original content, pre-deploy backup
└── manifest.json          ← active files, for crash recovery
    {
      "suggestion_id": "<uuid>",
      "files": [
        {
          "original": "<absolute path>",
          "staged":   "<absolute path>",
          "bak":      "<absolute path>",
          "stage":    "pre-stage | staged | validated | deployed"
        }
      ],
      "started_at": "<ISO 8601>",
      "last_updated": "<ISO 8601>"
    }
```

### Rule 4: Token Scaling by File Size (MANDATORY)

Blueprint scales `max_tokens` for each Claude API call based on the target file's line count:

```
max_tokens = min(16000, max(2000, file_line_count * 8))
```

- Floor: 2000 tokens (all small files)
- Ceiling: 16000 tokens (all large files)
- Scale: 8 tokens per line of original content

When a suggestion affects multiple files, each file gets its own independently scaled API call. Blueprint does not batch multiple files into one call.

This rule exists to prevent both waste (oversized limits on small files) and truncation (undersized limits on large files).

### Rule 5: File Mutex — One Suggestion Per Target File (MANDATORY)

Blueprint may not enter `in_progress` for a suggestion if any other suggestion is currently `in_progress` targeting an overlapping file in `target_files`.

Check at self-assignment time: scan all suggestions with `status: "in_progress"`, collect their `target_files` arrays, check for intersection with the candidate suggestion's `target_files`. If intersection is non-empty: do not self-assign. Append a note to `audit_log` documenting the conflict and wait.

### Rule 6: Staging Artifact Cleanup on Failure (MANDATORY)

On any failure path before `os.replace()`:
1. Delete `.staged` file (if exists)
2. Delete `.tmp` file (if exists in live directory)
3. Log the failure with the specific step that failed
4. Do NOT touch `.bak`
5. Do NOT modify the suggestion status beyond marking the `audit_log`

The live file must be in exactly the same state as before Blueprint began. If Blueprint cannot confirm the live file is unmodified, it must log this as CRITICAL and alert via morning report.

### Rule 7: Stale Staging Cleanup at Run Start (MANDATORY)

At the start of every Blueprint run, before any implementation begins:
1. Scan `.blueprint_staging/` for any directories older than 7 days
2. For each stale directory:
   - Check if the associated suggestion is still `in_progress` in `suggestions.json`
   - If yes: log a warning and do NOT delete (a previous Blueprint run may have crashed mid-deploy — human inspection required)
   - If no: delete the stale directory and log the cleanup
3. Any `.tmp` file found in a live directory (e.g., `core/agent2_research.py.tmp`) is an emergency signal — a previous `os.replace()` may have been interrupted. Do NOT delete. Halt and alert.

---

## 4. PRE-DEPLOY VALIDATION CHECKLIST

Blueprint must pass ALL of the following checks before proceeding to `os.replace()`. Each item must be explicitly evaluated and logged. A single failure aborts the deploy.

```
[ ] 1. SUGGESTION STATE
        suggestions.json entry for this suggestion has status "in_progress"
        assigned_to = "blueprint"
        approved_by is not null
        No other suggestion in "in_progress" shares a target file with this one

[ ] 2. FILE EXISTS
        Target live file exists at the path recorded in target_files
        Path does not contain hardcoded /home/pi/ (must use resolved absolute path)

[ ] 3. STAGED FILE INTEGRITY
        .staged file exists in .blueprint_staging/<suggestion_id>/
        .staged file is non-empty
        Byte count of .staged matches what was written (read-back verification)

[ ] 4. SYNTAX CHECK (Python files only)
        ast.parse() on .staged content succeeds with no SyntaxError
        (For non-Python files: appropriate syntax check per file type, or document skip reason)

[ ] 5. TRUNCATION GUARD
        len(.staged) >= len(original) * 0.60
        OR expected_size_reduction documented in audit_log AND acknowledged by Patches

[ ] 6. BACKUP WRITTEN
        .bak file written to .blueprint_staging/<suggestion_id>/
        .bak content matches original live file byte-for-byte (read-back verification)

[ ] 7. NO ACTIVE KILL SWITCH
        ${SYNTHOS_HOME}/.kill_switch file does NOT exist on target Pi

[ ] 8. NO ACTIVE POST-DEPLOY WATCH
        post_deploy_watch.json has no entry with status "active" or "initialized"
        (Cannot deploy while a previous deployment is still under evaluation)
```

Checklist is logged to Blueprint's log file with timestamp and PASS/FAIL per item before `os.replace()` fires.

---

## 5. FAILURE BEHAVIOR

### Failure Before os.replace() (Steps 1–6)

The live file is guaranteed unmodified if failure occurs before Step 7.

**Blueprint must:**
1. Log: exact step that failed, error message, file path
2. Delete: `.staged` file (if written)
3. Delete: `.tmp` file (if written)
4. Retain: `.bak` file (if written) — evidence of intent
5. Update `suggestions.json` `audit_log` with failure event
6. Do NOT change suggestion status (remains `in_progress`)
7. If the failure is retryable (e.g., Claude API timeout): Blueprint may retry at next scheduled run. Maximum 3 attempts before escalating to `blocked` with a detailed `blocked_reason`.

### Failure During os.replace() (Step 7)

`os.replace()` is atomic on POSIX. If the process is interrupted mid-replace:
- The OS guarantees either old or new file — never a partial write
- On restart, Blueprint checks: does the live file match `.bak` (old) or `.staged` (new)?
  - If matches `.bak`: `os.replace()` did not complete. Retry from Step 6.
  - If matches `.staged`: `os.replace()` completed. Proceed to post-deploy cleanup (Step 8–9).
  - If matches neither: CRITICAL — unknown state. Do not proceed. Alert immediately.

### Failure After os.replace() (Steps 8–9)

The live file is the new version. The deployment is committed.

**Blueprint must:**
1. Log: the partial cleanup state
2. Attempt cleanup of remaining staging artifacts
3. Proceed with post-deploy steps (initialize `post_deploy_watch.json`, update suggestion status)
4. A failure in post-deploy bookkeeping does NOT undo the deployment

### Rollback Behavior

Blueprint does not perform rollback of live files independently. Rollback is:
- On retail Pis: executed by Watchdog via `.known_good/` snapshot
- On company Pi: executed by the Project Lead manually, or via `patch.py`

Blueprint's role in rollback is:
1. Retain `.bak` file until `post_deploy_watch.json` closes as `stable`
2. If rollback is triggered (watch enters `rollback_triggered`): Blueprint provides `.bak` path in audit_log for manual recovery reference
3. Transition the suggestion from `deployed → in_progress` to signal the deployment was undone

### Artifact Cleanup on Stable Close

When `post_deploy_watch.json` closes with `outcome: "stable"`:
1. Blueprint may delete `.bak` files for the associated suggestion
2. Blueprint cleans the `.blueprint_staging/<suggestion_id>/` directory
3. Blueprint logs the cleanup

The `.bak` file is the last line of defense. It is never deleted before stable confirmation.

### Logging Requirements

Every deployment operation must produce log entries for:
- Run start (suggestion_id, target files, suggestion status at start)
- Each checklist item (PASS/FAIL with observed value)
- Each step of the atomic sequence (with timing)
- Any failure (step, error message, cleanup actions taken)
- Post-deploy state (files written, staging artifacts retained or cleaned)

Log file: `${LOG_DIR}/blueprint.log`
Log format: `[YYYY-MM-DD HH:MM:SS] LEVEL blueprint: message`

---

**Document Version:** 1.0
**Status:** Active — applies to all Blueprint implementations without exception
**Authority:** Patches verifies compliance. Project Lead resolves violations.
