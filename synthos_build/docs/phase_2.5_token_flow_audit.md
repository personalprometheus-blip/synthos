# Phase 2.5 — Token-Flow Security Audit

**Date:** 2026-04-25
**Scope:** All token-bearing flows in `synthos_build/src/retail_portal.py`
and `synthos_build/src/auth.py`. Static analysis + live test-customer
smoke verification of fixes.
**Prior audit:** [`security_audit_2026-04-24.md`](./security_audit_2026-04-24.md) §"Token-flow audit" gap

---

## RESOLUTION SUMMARY (2026-04-25 same-day fix)

All 5 actionable findings from this audit (HIGH-1 + 4 MEDIUMs) were
fixed and verified in commit `8c5b158`. Live smoke test against pi5
returned **14/14 pass**:

| Finding | Status | Verification |
|---|---|---|
| 🔴 HIGH-1 missing /reset-password handler | ✅ FIXED | Bad token→400, good token→200 form, POST→302 /login?reset=1 |
| 🟡 MED-1 session revocation on credential change | ✅ FIXED | Old cookie force-logged-out, new pw works, old pw rejected |
| 🟡 MED-2 old-email alert | ✅ FIXED | Code path exercised (Resend dispatch unverified — would burn API credits) |
| 🟡 MED-3 verify new email | ✅ FIXED | email_hash unchanged at request, only updated after token verify; consumed_at set |
| 🟡 MED-4 password length 8→12 | ✅ FIXED | 11-char pw correctly rejected |
| 🟢 LOW-1/2/3 | DEFERRED | per original audit, defensive improvements for after launch |

The original findings text is preserved below for record.

---

---

## Routes audited

| Route | Handler | Token source | Static result |
|---|---|---|---|
| `/forgot-password` GET/POST | `forgot_password_page` | issues a token via `auth.create_password_reset_token` | OK |
| `/reset-password/<token>` GET/POST | **MISSING** | — | 🔴 BUG |
| `/setup-account/<token>` GET/POST | `setup_account` | `auth.consume_verify_token` | OK with notes |
| `/verify-email/<token>` GET | `verify_email` | `auth.verify_signup_email` | OK with note |
| `/sso?t=<token>` GET | `sso_login` | `URLSafeTimedSerializer` | OK with note |
| `/api/account/change-password` POST | `api_change_password` | requires session + current password | OK with notes |
| `/api/account/change-email` POST | `api_change_email` | requires session + current password | OK with notes |

---

## Findings

### 🔴 HIGH — `/reset-password/<token>` handler is MISSING

**Severity:** HIGH (customer-blocking, no account recovery path)
**Status:** Pre-launch must-fix.

**The bug.**
- `forgot_password_page` (line 754) generates a token, sends an email
  with `https://portal.synth-cloud.com/reset-password/<token>`.
- `before_request` is configured to allow `/reset-password/` paths
  through without authentication (line 700, in the public token-prefix
  list).
- `auth.reset_password(token, new_password)` exists, validates
  expiration, clears token after use, hashes new password — all correct.
- **But there's NO `@app.route('/reset-password/<token>')` decorator
  anywhere in `retail_portal.py`.** Customer clicks the email link →
  Flask 404.

**How it's been hidden.** Forgot-password flow has likely never been
exercised end-to-end with a real customer. Pre-launch makes this fine
for the moment but it's a customer-blocking bug.

**Fix scope.** ~50 lines: a new route that GET-renders a "set new password"
form (template can mirror `setup_account.html`), POST calls
`auth.reset_password`, redirects to `/login?reset=1` on success or
re-renders with error.

---

### 🟡 MEDIUM-1 — Password reset / change does not invalidate other active sessions

**Severity:** MEDIUM (security best practice; account-takeover residual exposure)
**Status:** Pre-launch fix recommended.

**The gap.**
- `auth.reset_password()` (line 745) updates `password_hash` only.
- `auth.update_password()` (called by `/api/account/change-password`) also
  updates only `password_hash`.
- Active sessions tied to the OLD password keep working until 8h cookie
  expiry or 15min idle timeout.

**Attack scenario.** Customer realizes "my account was compromised" →
forgets password → resets → but the attacker's authenticated session is
still valid. They have up to 8h of continued access before the cookie
expires.

**Industry standard.** Reset/change should clear the server-side
`_session_activity` map for that customer, forcing re-login on every
existing session. Flask sessions are signed cookies (no server-side
revocation by default), but the in-memory `_session_activity` dict at
`retail_portal.py:325` already gates idle-timeout behavior — clearing
the customer's entry is enough to force re-login on next request.

**Fix scope.** ~10 lines per call site (reset_password + update_password):
clear `_session_activity[customer_id]`, rotate `session.permanent_session_lifetime`
salt, or set a `password_changed_at` column and check it against the
session's issued-at timestamp.

---

### 🟡 MEDIUM-2 — `change-email` doesn't notify the OLD address

**Severity:** MEDIUM
**Status:** Pre-launch fix recommended.

**The gap.** `/api/account/change-email` requires current password (good),
validates the new email format, and writes the new email to auth.db. But:
- No notification is sent to the **old** email address that "your account
  email was just changed."
- This is the standard mechanism for the original owner to detect a
  hijacked account.

**Attack scenario.** Attacker has session (via XSS, cookie theft, or
compromised password). They change the email to attacker-controlled,
then immediately reset password via the new email. Original owner has
no signal anything happened until they try to log in days later.

**Fix scope.** ~15 lines: in `auth.update_email`, before commit, send a
notification email to the old address with the change attempt + a
"this wasn't me" link (which would call a new endpoint that reverses the
change for ~24h post-change).

---

### 🟡 MEDIUM-3 — `change-email` doesn't verify new address ownership

**Severity:** MEDIUM
**Status:** Pre-launch fix recommended.

**The gap.** New email address is trusted on submission. There's no
"check your inbox to confirm" step.

**Attack scenarios.**
- Typo: customer types `gmail.con` instead of `gmail.com` and locks
  themselves out (forgot-password emails go to nobody).
- Malicious: attacker with session sets attacker-controlled email; later
  password resets via that email take over the account permanently.

**Fix scope.** Two-step flow:
1. POST creates a `pending_email_change` row with a 1h verification
   token, sends email to **new** address.
2. New `/verify-email-change/<token>` route confirms ownership and
   actually updates the email.

~30 lines + 1 schema column.

---

### 🟡 MEDIUM-4 — Password length inconsistency

**Severity:** MEDIUM (policy gap, customer confusion)
**Status:** Easy fix.

**The gap.**
- `setup_account` (line 1266): `if len(password) < 12: return error`
- `api_change_password` (line 2955): `if len(new_pw) < 8: return error`

A new account requires 12 characters; a password change requires only 8.
A user could set a strong password at setup, then "change" it to a weak
8-character password.

**Fix scope.** 1 line. Pick a value (12 is the modern OWASP recommendation
for non-MFA accounts; 8 is the legacy minimum). Recommend standardizing
to 12 across both. Ideally also add a "passwords are this long because…"
hint in the UI so customer doesn't get confused.

---

### 🟢 LOW-1 — SSO tokens have no replay protection

**Severity:** LOW
**Status:** Defer until SSO becomes a real attack surface.

**The gap.** `URLSafeTimedSerializer` validates signature + age, but a
token can be replayed within its 15-min window if the attacker captures
it (e.g., from URL referrer leakage, browser history, third-party JS).

**Mitigation present.** 15-min TTL is short. Token is sent over HTTPS only
(Cloudflare HSTS). Same-site session cookie blocks most cross-site replay.

**Fix scope (when prioritized).** ~20 lines: track consumed JTIs in a
small `sso_consumed_tokens` table with TTL cleanup. Reject any token whose
JTI has been seen before.

---

### 🟢 LOW-2 — Password reset tokens stored unhashed in auth.db

**Severity:** LOW
**Status:** Defensive improvement — defer.

**The gap.** `password_reset_token` column stores the raw token. If
auth.db escapes the Fernet+0600+host-isolation perimeter (e.g., backup
encryption fails, R2 leak, host compromise), an attacker reads any
active reset token and uses it to take over the account.

**Mitigation present.** auth.db is 0600, Fernet-encrypted at rest for
PII (but not for tokens themselves), 30-min token TTL.

**Fix scope.** ~10 lines: store HMAC-SHA256(token, server_secret) in DB,
compare HMAC at validation time. Server keeps the secret in the
ENCRYPTION_KEY env var. Clean rotation: dual-write during a 30-min
transition window.

---

### 🟢 LOW-3 — `verify-email` → `setup-account` redirect briefly exposes token in 302

**Severity:** LOW
**Status:** No fix — informational.

**The gap.** When `verify_email(token)` raises ValueError ("not a signup
token, maybe a setup token"), it does `redirect(f'/setup-account/{token}')`.
The token now appears in the 302 Location header and the browser's URL
history both routes.

**Real impact.** Negligible. The token is used twice in the same request
chain anyway. Browser extensions snooping on URL history would see both.
Single-use prevents replay.

**No fix needed.** Document and move on.

---

## Audit method

For each route:
1. Read the route body for auth checks, input validation, and side-effect
   ordering.
2. Trace token-handling functions in `auth.py` for entropy, expiration,
   single-use, hashing-at-rest, and replay protection.
3. Look for missing route handlers referenced from URL generators.
4. Check session-invalidation behavior on credential changes.
5. Verify error messages don't leak account existence (email enumeration).

No live HTTP probing was performed in this static pass. Live probing
(creating a sandbox customer, exercising forgot-password against a test
inbox, replay tests, etc.) is a follow-up Phase 2.5b that needs explicit
operator authorization because:
- Real Resend API calls (low cost but real)
- Test customer rows in auth.db (handled by same delete-on-completion
  pattern as Phase 4)

---

## Recommended fix priority

| Severity | Item | Effort | Pre-launch? |
|---|---|---|---|
| 🔴 HIGH-1 | Add `/reset-password/<token>` handler | ~50 LOC + template | **YES (customer-blocking)** |
| 🟡 MED-1 | Invalidate sessions on password change/reset | ~10 LOC × 2 sites | YES |
| 🟡 MED-2 | Notify old address on email change | ~15 LOC | YES |
| 🟡 MED-3 | Verify new email before change | ~30 LOC + 1 column | YES |
| 🟡 MED-4 | Standardize password min-length to 12 | 1 LOC | YES |
| 🟢 LOW-1 | SSO replay protection | ~20 LOC + 1 table | After launch |
| 🟢 LOW-2 | Hash reset tokens at rest | ~10 LOC | After launch |
| 🟢 LOW-3 | verify-email→setup-account redirect | informational | No fix |

Total pre-launch effort: ~120 LOC across 5 fixes. Could be one focused
afternoon of work.

---

## What's working well

| Area | Detail |
|---|---|
| Token entropy | All flows use `secrets.token_urlsafe(32)` (256 bits) — not guessable |
| Token expiry | 30 min for password reset, 48 h for setup-account, 15 min for SSO. All bounded. |
| Single-use | Reset and setup-account both consume the token after successful use. |
| Email enumeration | `forgot-password` always returns the same UI regardless of whether the email exists. `create_password_reset_token` returns `None` quietly. |
| Session fixation on SSO | `session.clear()` before populating new session — correct. |
| Constant-time compare | `auth.verify_password` uses passlib's PBKDF2 implementation which uses constant-time HMAC compare under the hood. |
| Pre-flight access check | `is_access_allowed` runs on every login (and SSO) — subscription / verification gates can't be bypassed. |
| TOS gate | login_required + before_request enforce TOS acceptance current version — token flows that bypass login_required (e.g., reset-password — when wired) need to be checked separately for TOS acceptance, but pre-launch this isn't an issue since TOS v1 is the only existing version. |

---

## Next steps

Surface findings to operator. Top-line ask: **HIGH-1 (missing reset-password
handler) is customer-blocking and should be fixed before any real customer
goes through forgot-password.** The four MEDIUMs are best-practice gaps
that should be closed before customer launch but aren't blocking today.
The LOWs can wait.
