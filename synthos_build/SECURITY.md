# Synthos Security Configuration

*Last updated: 2026-04-02*

This document records the security posture of all Synthos infrastructure — GitHub repositories, Cloudflare DNS/proxy, and the Retail Pi portal. It covers what is configured, why it matters, what still needs to be done, and what to hold off on until after live testing.

---

## 1. GitHub Repositories

Two active public-facing repos: `synthos` (retail Pi code) and `synthos-company` (company Pi code).

### 1.1 Completed

| Setting | Repo | Status | Why It Matters |
|---|---|---|---|
| Dependency graph | synthos, synthos-company | ✅ Enabled | Required for Dependabot to map your Python dependencies |
| Dependabot alerts | synthos, synthos-company | ✅ Enabled | Sends an alert when a known CVE is found in a dependency you're using |
| Dependabot security updates | synthos, synthos-company | ✅ Enabled | Automatically opens a PR with the patched version when a fix exists |
| Grouped security updates | synthos, synthos-company | ✅ Enabled | Batches multiple Dependabot PRs into one, reducing noise |
| CodeQL code scanning | synthos, synthos-company | ✅ Enabled (scanning) | Static analysis — catches injection flaws, path traversal, hardcoded secrets, unsafe deserialization. Runs on every push to main and weekly. |
| Secret scanning | synthos, synthos-company | ✅ Auto-enabled (public repos) | GitHub scans every push for API keys, tokens, and credentials. Alerts immediately if a secret is committed. |
| Push protection | synthos, synthos-company | ✅ Auto-enabled (public repos) | Blocks the push entirely if a known secret pattern is detected before it lands in history |

### 1.2 Required — Manual Steps

#### Make both repos private
**Why:** Both repos are currently Public. Anyone can read your agent logic, trading gate conditions, API integration patterns, and installer flow. This is the single highest-risk item in the entire audit.

**Steps:**
1. Go to `github.com/personalprometheus-blip/synthos/settings`
2. Scroll to **Danger Zone** → **Change visibility** → **Change to private**
3. Type the repo name to confirm
4. Repeat for `github.com/personalprometheus-blip/synthos-company/settings`

#### Add branch protection to `main` (both repos)
**Why:** Without branch protection, any collaborator (or a compromised account) can force-push to main, rewrite history, or delete the branch entirely.

**Recommended rule — solo-dev friendly (no PR requirement):**
- ✅ Disallow force pushes
- ✅ Disallow branch deletion
- ❌ Do NOT require pull requests (too much friction for solo rapid iteration)

**Steps:**
1. Go to `github.com/personalprometheus-blip/synthos/settings/branches`
2. **Add classic branch protection rule**
3. Branch name pattern: `main`
4. Check: **Do not allow bypassing the above settings**
5. Check: **Restrict force pushes** (leave all others unchecked)
6. Save — repeat for `synthos-company`

#### Enable 2FA on GitHub account
**Why:** Without 2FA, a compromised password gives full access to both repos, all deploy keys, all secrets, and the ability to push malicious code to a live trading system.

**Steps:** `github.com/settings/security` → Enable two-factor authentication → use an authenticator app (not SMS)

**Note:** Do not use SMS-based 2FA — SIM swap attacks are trivial.

### 1.3 Hold Off Until After Live Test

| Setting | Why Wait |
|---|---|
| Require PR before merge | Adds friction to solo rapid iteration during active development. Add after v1.0 stabilises. |
| Require status checks to pass | CodeQL needs a few runs to establish a baseline before using it as a merge gate |
| Enable signed commits | Useful for audit trail but adds workflow overhead |

---

## 2. Cloudflare

Synthos uses Cloudflare for DNS and Cloudflare Tunnel for exposing the retail portal publicly.

### 2.1 Safe to Configure Now (Zero Workflow Impact)

These settings should be applied immediately. None affect automated agent traffic.

| Setting | Path in Dashboard | Recommended Value | Why |
|---|---|---|---|
| SSL/TLS mode | SSL/TLS → Overview | **Full (strict)** | Flexible mode leaves traffic unencrypted between Cloudflare and your origin. Full strict verifies your origin certificate. Cloudflare Tunnel handles this automatically — no extra cert needed. |
| Always Use HTTPS | SSL/TLS → Edge Certificates | **On** | Redirects all HTTP requests to HTTPS — eliminates plain-text portal access |
| Minimum TLS Version | SSL/TLS → Edge Certificates | **TLS 1.2** | Blocks connections from clients using deprecated TLS 1.0/1.1 |
| HSTS | SSL/TLS → Edge Certificates → HSTS | Max-age: 15768000, Include subdomains: On, No-sniff: On, Preload: Off | Tells browsers to never attempt HTTP at all for this domain. **Set preload to Off** — preloading is permanent and very hard to undo. |
| Browser Integrity Check | Security → Settings | **On** (confirm it's already on) | Challenges requests with suspicious or missing browser headers — stops basic scrapers and bots |
| Hotlink Protection | Scrape Shield | **On** | Prevents external sites from embedding your portal resources |

### 2.2 Hold Off Until After Live Test

These settings could interfere with automated Pi agent traffic hitting the portal through the public Cloudflare URL.

| Setting | Why Wait | When to Enable |
|---|---|---|
| **Bot Fight Mode** | If any Pi agent (heartbeat, watchdog, API poller) makes HTTP requests to the public portal URL, Bot Fight Mode may classify these as bots and block or JS-challenge them. | After confirming all automated traffic uses direct Cloudflare Tunnel paths, not the public URL |
| **Security Level → Medium/High** | Medium security can show a JS challenge page to IPs it considers suspicious. Automated scripts can't solve JS challenges. | After confirming all agents that hit the portal are browser-based (i.e. human-operated only) |
| **IP Access Rules** | Locking the portal to specific IP ranges is ideal long-term but would block customers in early testing | After customer base is known and geo-patterns are established |

### 2.3 Steps to Apply Cloudflare Settings

1. Log in at `dash.cloudflare.com`
2. Select your domain
3. Apply each setting in section 2.1 above
4. After live test is complete and you've verified no agent traffic goes through the public portal URL, return and apply section 2.2 settings

---

## 3. Retail Pi Portal

Security hardening applied directly in code.

| Control | File | Status | Notes |
|---|---|---|---|
| Login rate limiting | retail_portal.py | ✅ Done | Max 10 attempts per 300s window per IP |
| OTP rate limiting | retail_portal.py | ✅ Done | Max 5 attempts per 300s window per IP |
| Timing-safe token comparison | synthos_monitor.py | ✅ Done | `hmac.compare_digest()` — prevents timing side-channel on SECRET_TOKEN |
| Newline injection prevention | retail_portal.py | ✅ Done | Values stripped of `\n`/`\r` before writing to .env |
| Env key format validation | retail_portal.py | ✅ Done | Only `[A-Z][A-Z0-9_]*` keys accepted in `/api/keys` |
| File upload restricted to admin | retail_portal.py | ✅ Done | Customers cannot upload files; uploads staged outside live dirs |
| `.env` excluded from upload types | retail_portal.py | ✅ Done | Prevents overwriting environment config via upload |
| `ENCRYPTION_KEY` generated at install | env_writer.py | ✅ Done | Fernet key generated fresh; preserved on repair — losing it destroys all encrypted customer PII |
| Hard fail on blank SECRET_TOKEN | company_server.py | ✅ Done | Server refuses to start if token is empty |
| Hard fail on missing ENCRYPTION_KEY | auth.py | ✅ Done | Raises RuntimeError — prevents operating with no encryption |
| Auth DB chmod 600 | auth.py | ✅ Done | Prevents other OS users reading the auth database |
| .env chmod 600 | env_writer.py | ✅ Done | Prevents other OS users reading credentials |
| Terms of Service gate | retail_portal.py | ✅ Done | First-login redirect; acceptance filed to customer folder. Note: a separate early-access beta TOS modal exists behind `EARLY_ACCESS_TOS_ENABLED = False` in the same file — that is dormant pending copy review. |

### 3.1 Suggested Future Hardening

| Suggestion | Priority | Notes |
|---|---|---|
| Add `Secure`, `HttpOnly`, `SameSite=Strict` flags to session cookie | High | Prevents session hijacking via XSS or cross-site requests |
| Implement CSRF token on all POST forms | High | Portal forms are currently vulnerable to cross-site request forgery |
| Add Content-Security-Policy header | Medium | Restricts what scripts/resources the portal page can load |
| Add `X-Frame-Options: DENY` header | Medium | Prevents the portal being embedded in an iframe (clickjacking) |
| Rotate `PORTAL_SECRET_KEY` on a schedule | Low | Currently set once at install — add a rotation mechanism |
| Log all failed login attempts with IP to audit log | Medium | Currently rate-limited but not persisted for forensics |
| Add IP allowlist option for admin routes | Low | Lock `/admin/*` to known IPs as an optional layer |

---

## 4. Encryption Key — Critical Operational Note

`ENCRYPTION_KEY` in `.env` is a Fernet symmetric key used to encrypt all customer PII (emails, hashed passwords, Alpaca credentials) stored in `auth.db`.

**If this key is lost or regenerated:**
- All encrypted data in `auth.db` becomes permanently unreadable
- All customer accounts are effectively destroyed
- There is no recovery path

**Required procedures:**
- Always include `auth.db` AND `.env` in the encrypted backup archive (strongbox handles this)
- Never run `install_retail.py` in repair mode on a live system without first confirming the backup is current
- The key is preserved automatically in repair mode — but only if `.env` exists before running the installer

---

## 5. Secrets — What Lives Where

| Secret | Location | Encrypted? | Backed Up? |
|---|---|---|---|
| ENCRYPTION_KEY | .env (chmod 600) | No — this IS the key | Via strongbox |
| ADMIN_PASSWORD | .env (chmod 600) | No | Via strongbox |
| PORTAL_SECRET_KEY | .env (chmod 600) | No | Via strongbox |
| ANTHROPIC_API_KEY | .env (chmod 600) | No | Via strongbox |
| ALPACA_API_KEY / SECRET | .env (chmod 600) | No | Via strongbox |
| Customer emails | auth.db | Yes (Fernet) | Via strongbox |
| Customer passwords | auth.db | Yes (bcrypt hash) | Via strongbox |
| Customer Alpaca creds | auth.db | Yes (Fernet) | Via strongbox |
| SECRET_TOKEN (company) | company .env (chmod 600) | No | Via strongbox |

---

## 6. Outstanding Security Items (Prioritised)

| Priority | Item | Blocking launch? |
|---|---|---|
| 🔴 Critical | Make `synthos` and `synthos-company` repos **private** | No, but high risk |
| 🔴 Critical | Enable **2FA** on GitHub account | No, but account takeover risk |
| 🟠 High | Apply Cloudflare section 2.1 settings | No |
| 🟠 High | Add branch protection to `main` (no PR requirement) | No |
| 🟡 Medium | Add `Secure`/`HttpOnly`/`SameSite` to session cookie | No |
| 🟡 Medium | Add CSRF tokens to portal forms | No |
| 🟡 Medium | Add security headers (CSP, X-Frame-Options) | No |
| 🟢 Low | Enable Bot Fight Mode (post-live-test) | No |
| 🟢 Low | Enable Security Level → Medium (post-live-test) | No |
| 🟢 Low | Signed commits | No |
