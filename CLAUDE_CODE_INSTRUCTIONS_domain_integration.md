# Claude Code Instructions — Domain Integration
## Task: Replace all temporary tunnel URLs with synth-cloud.com subdomains

---

### OBJECTIVE

Replace every temporary, random, or placeholder Cloudflare tunnel URL across
the entire Synthos codebase with permanent named tunnel subdomains on
`synth-cloud.com`. This is a search-and-replace plus configuration task.
Do not change any logic, ports, or agent behavior.

---

### SUBDOMAIN MAP

| Service | Port | Permanent URL |
|---|---|---|
| Portal | 5001 | `portal.synth-cloud.com` |
| Console / Monitor | 5000 | `console.synth-cloud.com` |
| Installer delivery | 5003 | `install.synth-cloud.com` |
| Heartbeat receiver | 5004 | `heartbeat.synth-cloud.com` |

---

### FIND AND REPLACE — ALL FILES

Search the entire project for every instance of the following and replace
with the correct subdomain from the map above:

- `trycloudflare.com` (any subdomain variant e.g. `random-words.trycloudflare.com`)
- `your-tunnel.com`
- `YOUR_TUNNEL_URL`
- `<your-tunnel>.trycloudflare.com`
- Any string matching the pattern `https://[random].trycloudflare.com`
- Any comment referencing "random URL", "URL changes every restart",
  "URLs change each restart", or "permanent URLs"

---

### setup_tunnel.sh — SPECIFIC CHANGES REQUIRED

This file requires structural changes beyond simple find-and-replace:

1. **Replace anonymous tunnel command** — change every instance of:
   ```
   cloudflared tunnel --url http://localhost:<PORT>
   ```
   with named tunnel syntax:
   ```
   cloudflared tunnel run --url http://localhost:<PORT> <tunnel-name>
   ```
   Use tunnel names that match the subdomain: `portal`, `console`,
   `install`, `heartbeat`

2. **Remove the end-of-file warning block** — delete the section that reads:
   > "URLs change each restart. For permanent URLs: ..."
   This is no longer relevant.

3. **Update the URL grep pattern** — the script waits for a URL to appear
   in the log file using:
   ```
   grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
   ```
   Replace with the correct permanent URL for each tunnel so the script
   confirms the named tunnel is live.

---

### config/allowed_ips.json — UPDATE COMMENT

The file contains a placeholder comment referencing tunnel setup.
Update any reference to `trycloudflare.com` or temporary URLs to reflect
`synth-cloud.com`.

---

### DOCUMENTATION FILES — UPDATE ALL REFERENCES

Search and update URL references in:
- `SYNTHOS_TECHNICAL_ARCHITECTURE.md`
- `SYNTHOS_OPERATIONS_SPEC.md`
- `SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md`
- `SYSTEM_MANIFEST.md`
- `SYNTHOS_INSTALLER_ARCHITECTURE.md`
- `TOOL_DEPENDENCY_ARCHITECTURE.md`
- Any README or setup guide files

Replace example URLs in documentation with the correct permanent subdomains.
Do not alter the meaning or structure of any documentation section.

---

### DO NOT TOUCH

- Port numbers (5000, 5001, 5003, 5004)
- `.env` files or any API keys
- Agent logic or behavior
- Database schema
- Authentication or HMAC signing logic
- The Cloudflare dashboard configuration — that is handled separately
  outside this codebase

---

### COMPLETION REQUIREMENT

When finished, produce a report listing:
1. Every file modified
2. Every line changed, showing old value → new value
3. Any instance where the replacement was ambiguous or skipped, with reason
