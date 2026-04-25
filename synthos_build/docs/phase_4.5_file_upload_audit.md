# Phase 4.5 — File-Upload Security Audit

**Date:** 2026-04-25
**Endpoint:** `POST /api/files/upload` (`retail_portal.py:5467`)
**Helper:** `sync_to_github()` (`retail_portal.py:5543`)
**Method:** static analysis + 21-probe non-admin probe (Phase 4.5
deliberately scoped to non-admin tests because successful admin-side
upload triggers `git add + commit + push` to origin/main).

---

## TL;DR

**Customer-side gate: SOLID.** 21/21 adversarial probes from a
non-admin customer were correctly blocked at the `@admin_required`
gate. No path-traversal, argv-injection, extension-bypass, or
shell-meta filename leaked through to file processing. `upload_staging/`
hash snapshot identical before/after the probe.

**Admin-side: 3 defense-in-depth findings** (LOW/MEDIUM severity).
None are exploitable by a customer. All only matter under the
"compromised admin session" threat model.

---

## Active probe results (21/21 pass)

A non-admin sandbox customer was created, logged in, and POSTed 18
adversarial filename/content combinations + 1 empty-form + 1 logged-out
test. Every request returned the expected 403 / 302. `upload_staging/`
state captured before + after — zero files leaked through.

| # | Test | Result |
|---|---|---|
| 0 | Logged-out → 302 /login | ✅ |
| 1 | benign `test.py` | ✅ 403 |
| 2 | benign `test.txt` | ✅ 403 |
| 3 | Path traversal `../../etc/passwd.txt` | ✅ 403 |
| 4 | Windows traversal `..\..\windows\system32\sam.txt` | ✅ 403 |
| 5 | Absolute path `/etc/cron.daily/evil.sh` | ✅ 403 |
| 6 | Argv injection `--upload-pack=evil.sh` | ✅ 403 |
| 7 | Argv injection `--malicious.py` | ✅ 403 |
| 8 | Newline injection `safe.txt\nrm -rf evil.txt` | ✅ 403 |
| 9 | Null byte `safe.txt\x00.sh` | ✅ 403 |
| 10 | Blocked extension `.exe` (with MZ header) | ✅ 403 |
| 11 | Shell script `rootkit.sh` | ✅ 403 |
| 12 | Uppercase extension `test.PY` | ✅ 403 |
| 13 | Double extension `evil.exe.txt` | ✅ 403 |
| 14 | No extension `noext` | ✅ 403 |
| 15 | Hidden file `.bashrc` | ✅ 403 |
| 16 | 5KB payload `big.txt` | ✅ 403 |
| 17 | Shell meta `safe;rm -rf evil.txt` | ✅ 403 |
| 18 | Unicode traversal `../passwd.txt` | ✅ 403 |
| 19 | Empty form (no files) | ✅ 403 |
| 20 | `upload_staging/` snapshot unchanged | ✅ |

The admin-required gate fires before any file save, basename strip,
extension check, or staging path computation, so none of the
adversarial paths reached unsafe code.

---

## Static-analysis findings (admin-side, not actively probed)

These only matter if an admin session is compromised (XSS, cookie
theft, password leak, etc.) — under that threat model the attacker
is already admin and these become escalation vectors.

### 🟡 MEDIUM-A — Argv injection on `git add` (no `--` separator)

**Location:** `retail_portal.py:5580`

```python
subprocess.run(
    ['git', 'add', '-f'] + files,    # ← files comes from user-supplied filenames
    capture_output=True, text=True, cwd=PROJECT_DIR, timeout=10
)
```

**The gap.** The `files` list comes from `os.path.basename(file.filename)`
where each filename is supplied by the uploader. `os.path.basename`
strips path components but does NOT strip a leading hyphen. A file
named `--upload-pack=...` or `-p evil.txt` would be parsed by `git`
as a flag, not as a pathspec.

`git add` doesn't have many exploitable flags compared to `git
fetch`/`git clone`/`git push` (which have the famous `--upload-pack`
argument-injection vuln, CVE-2018-17456). For `git add` specifically,
the most dangerous flags are `--force` (no-op here, already passed),
`--literal-pathspecs`, or `--pathspec-from-file=`. None obvious-RCE.

**Best practice not followed:** every git command that mixes
user-supplied paths with subprocess args should include `--` to
separate options from pathspec. Adding `--` here is a one-line fix:
```python
['git', 'add', '-f', '--'] + files
```

**Risk classification.** MEDIUM as a defense-in-depth gap. LOW
operationally because (a) admin-only access required, (b) `git add`
itself is not a known argument-injection sink for code execution.

**Note on `sync_to_github` flow.** The `git add` runs against
`PROJECT_DIR`, but the uploaded files were saved to `STAGING_DIR`
(`~/synthos/upload_staging/`). The filenames in `files` are basenames
only — `git add -f passwd.txt` would search `PROJECT_DIR` for
`passwd.txt`, not the staging dir. So this command silently FAILS to
add anything in most cases, then `git diff --cached --stat` returns
empty, and `sync_to_github` returns "Already up to date on GitHub".
**The git-push side-effect described in my probe-design comment is in
fact a no-op for files that don't already exist in PROJECT_DIR.**

This is itself a separate finding — see MEDIUM-C below.

---

### 🟡 MEDIUM-B — Newline / commit-message injection

**Location:** `retail_portal.py:5593-5597`

```python
msg = f"Portal upload: {', '.join(files)}"
subprocess.run(
    ['git', 'commit', '-m', msg],
    ...
)
```

**The gap.** Filenames with newlines or special git-revision-like
strings (`@{`, `^{`, etc.) get embedded in the commit message. Won't
RCE — `subprocess.run` with arg list (no shell) sanitises arguments —
but produces malformed commit messages that confuse later log
parsers.

**Mitigation already present.** `os.path.basename` strips paths.
Extension whitelist is strict. So filenames are short and constrained
in practice.

**Fix-if-bothered:** sanitize newlines + control chars from filenames
before joining into commit message.

**Risk:** LOW. Admin-only access, no code-execution path. Cosmetic.

---

### 🟡 MEDIUM-C — `sync_to_github` operates on the wrong directory

**Location:** `retail_portal.py:5479-5532`

**The bug.** Files are saved to `STAGING_DIR =
~/synthos/upload_staging/` (line 5482). But `sync_to_github` is
called with the file basenames and runs `git add` from `cwd=PROJECT_DIR`
(`~/synthos/synthos_build/`). The basenames don't exist in
`PROJECT_DIR` — they exist in `STAGING_DIR` — so `git add` fails
silently with "did not match any files". Subsequent `git diff
--cached --stat` returns empty → function returns
`'Already up to date on GitHub'`.

**Net effect.** The portal uploads files to a staging dir, **never
syncs them to GitHub**, and tells the operator "Pushed to GitHub" or
"Already up to date" depending on the path. Operators following the
portal's confirmation message would think their upload is on GitHub
when it's actually only on the Pi.

**This is a functional bug, not a security finding.** But it has
security implications: the staging dir accumulates files that the
operator believes have been pushed, and code review (which assumes
the GitHub repo is canonical) misses changes.

**Recommended fix.** Either:
- Save files directly to `PROJECT_DIR` and skip the staging dir
  (loses the security benefit of staging — a path-traversal bug
  would write directly to live code), OR
- Update `sync_to_github` to copy files from `STAGING_DIR` →
  `PROJECT_DIR` before `git add`, OR
- Run `git add` from `STAGING_DIR` against a different repo (would
  need staging dir to be its own git repo).

Cleanest is option 2 with explicit copy + log of the move. ~15 LOC.

---

### 🟢 LOW-D — No request-size limit

**Location:** Flask `MAX_CONTENT_LENGTH` not set in app config.

**The gap.** A multi-GB upload from an admin can fill the disk before
the upload completes. Werkzeug streams to a temp file — no Flask-level
size cap means the temp file can grow until the disk is full.

**Mitigation present.** Admin-only. Cloudflare's free tier caps
uploads at 100 MB. Pi5 NVMe has tens of GB free.

**Risk:** LOW operationally. Worth setting `MAX_CONTENT_LENGTH = 16 *
1024 * 1024` (16 MB) for defense-in-depth.

---

### 🟢 LOW-E — `retail_portal.py` upload triggers `os.execv` portal restart

**Location:** `retail_portal.py:5513-5532`

```python
if fname == 'retail_portal.py':
    restart_portal = True
...
if restart_portal and uploaded:
    def delayed_restart():
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=delayed_restart, daemon=True).start()
```

**The behavior.** Admin uploading a file named `retail_portal.py`
triggers a 2-second-delayed `os.execv` restart of the portal process.
This is intentional: live-code-update without sshing.

**The threat model.** A compromised admin session can ship arbitrary
Python to the portal process, then trigger the restart, achieving
RCE on the Pi as the `pi516gb` user. Combined with `pi516gb`'s
NOPASSWD: ALL sudo (audit MEDIUM-6, deferred), this is root-on-Pi.

**Mitigation present.** Admin-only access. The portal runs as
`pi516gb`, not `root`.

**Recommended.** When MEDIUM-6 (sudo narrowing) is eventually done,
this attack chain shortens but doesn't disappear. Could add an
explicit "live-reload" toggle so this only fires when expected.

**Risk:** LOW today (admin-only assumption), MEDIUM if `pi516gb`
sudo stays NOPASSWD: ALL.

---

## Summary

| Severity | Finding | Status | Effort to fix |
|---|---|---|---|
| 🟡 MEDIUM-A | Argv injection on git add (no `--`) | Documented | 1 LOC |
| 🟡 MEDIUM-B | Newline in commit message | Documented | 5 LOC |
| 🟡 MEDIUM-C | `sync_to_github` syncs wrong dir (functional bug) | Documented | ~15 LOC |
| 🟢 LOW-D | No request-size limit | Documented | 1 LOC |
| 🟢 LOW-E | `retail_portal.py` upload → portal restart | Documented | depends on UX choice |

None blocking. Customer-launch readiness on file-upload: **green**.
The admin gate is solid; everything below it is admin-trust-model
territory.

**MEDIUM-C is the highest-priority follow-up** because it's also a
functional bug (operators think files are on GitHub when they aren't).
Worth fixing as a standalone cleanup. Out of scope for today's
security audit batch since I haven't been asked to fix non-security
bugs.

---

## What's working well

| Area | Detail |
|---|---|
| Auth gate | `@admin_required` runs before any file processing — 21/21 customer-side probes correctly 403 |
| Path traversal | `os.path.basename()` strips `../` and absolute-path components |
| Extension whitelist | Strict allowlist (`.py .sh .md .html .txt .json`) — case-sensitive, final-extension-based |
| Staging dir | Uploads land in `~/synthos/upload_staging/`, NOT live code paths |
| No shell injection | `subprocess.run(args=[...])` with arg list, no `shell=True` anywhere |
| Audit trail | Every successful upload logs FILE_UPLOADED to `system_log` |
| Restart safety | `os.execv` reuses `sys.executable + sys.argv`, no shell |
