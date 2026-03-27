# BLUEPRINT WORKFLOW SPECIFICATION
## Addendum 1: Implementation Safety Patterns

**Version:** 1.0
**Date:** March 2026
**Appends:** BLUEPRINT_WORKFLOW_SPEC.md v2.0
**Source:** Patterns extracted from agent4_audit.py improvement engine

---

## PURPOSE

Four implementation patterns from the original audit agent's improvement engine
are worth carrying forward into Blueprint. They are not in the base spec. This
addendum makes them mandatory.

---

## PATTERN 1: ATOMIC DEPLOY

Blueprint never writes directly to a live file. Every file change follows this
sequence exactly:

```
1. Write new content to .staging/<filename>.staged
2. Read it back and verify the write is byte-perfect
3. Syntax check (ast.parse) on staged file
4. Size guard (see Pattern 2)
5. Write original to <filename>.bak
6. Write staged content to <filename>.tmp
7. os.replace(<filename>.tmp, <filename>)   ← POSIX atomic rename
8. Delete .staged file
```

If anything fails at steps 1–6, `.staged` and `.tmp` are cleaned up and the
live file is never touched. `os.replace()` is atomic on POSIX — the file is
either the old version or the new version, never a partial write.

**Blueprint must clean up staging artifacts on any failure path.** A leftover
`.staged` file is a signal that a previous run failed mid-deploy. Blueprint
checks for and removes stale staging files at the start of each run.

---

## PATTERN 2: TRUNCATION GUARD

Claude occasionally returns a partial file — correctly formatted, syntactically
valid, but missing large sections. Syntax checks alone do not catch this.

Before deploying any generated file, Blueprint enforces:

```python
if len(staged_content) < len(original_content) * 0.60:
    # Abort — possible truncation
    raise TruncationError(
        f"Staged file too short: {len(staged_content)} bytes "
        f"vs {len(original_content)} bytes original. "
        f"Possible truncation — live file untouched."
    )
```

The 60% threshold catches catastrophic truncation while allowing legitimate
cases where a refactor genuinely shrinks a file. If a suggestion is expected
to significantly reduce file size, Blueprint notes this in `BLUEPRINT_NOTES.md`
so the threshold can be evaluated manually.

---

## PATTERN 3: STAGING DIRECTORY

Blueprint maintains a dedicated scratch space for all in-progress work:

```
/home/pi/synthos-company/.blueprint_staging/
```

This directory is:
- **Never committed to git** (in `.gitignore`)
- **Never read by other agents** (Blueprint's private workspace)
- **Cleaned at the start of each run** (stale artifacts from failed runs removed)
- **Inspectable by the project lead** if a deployment fails mid-run

Contents during an active implementation:
```
.blueprint_staging/
├── <suggestion-id>/
│   ├── <filename>.staged     ← generated content, pre-deploy
│   ├── <filename>.bak        ← original, pre-deploy backup
│   └── manifest.json         ← what's in progress, for crash recovery
```

---

## PATTERN 4: TOKEN SCALING BY FILE SIZE

Blueprint scales `max_tokens` for each Claude API call based on the size of
the file being modified. Sending a fixed token limit wastes money on small
files and risks truncation on large ones.

```python
def max_tokens_for_file(file_lines: int) -> int:
    # Floor of 2000 for small files
    # Ceiling of 16000 for large files
    # Scales linearly at 8 tokens per line of original content
    return min(16000, max(2000, file_lines * 8))
```

Blueprint reads the line count of the target file before calling Claude and
sets `max_tokens` accordingly. This is applied per-file when a suggestion
affects multiple files — each file gets its own appropriately scaled call.

---

## INTERACTION WITH BASE SPEC

These patterns slot into Step 3 (Implement) and Step 5 (Stage) of the base
spec's decision flow. They are implementation-level details — the base spec
governs *what* Blueprint does, this addendum governs *how* it does it safely.

The truncation guard and atomic deploy are **non-negotiable**. Blueprint must
not bypass them even for trivial changes. The cost of a corrupted live file
on a customer Pi is higher than the cost of a failed deployment.

---

**Addendum Version:** 1.0
**Status:** Active — applies to all Blueprint implementations
