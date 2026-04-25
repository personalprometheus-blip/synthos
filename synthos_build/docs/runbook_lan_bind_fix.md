# Runbook — HIGH-4 LAN-bind architectural fix

**Goal.** Move `portal.synth-cloud.com` from pi4b's tunnel to pi5's
own tunnel so it terminates over `localhost`. Then bind gunicorn to
`127.0.0.1:5001` instead of `0.0.0.0:5001`. Closes the LAN-side
attack surface flagged in the 2026-04-24 security audit (HIGH-4).

**Estimated duration.** 15 minutes including verification.
**Rollback time if anything breaks.** ~30 seconds (one DNS click + one git revert).

---

## Pre-flight checklist (do this before starting)

- [ ] You have ~20 uninterrupted minutes
- [ ] You have access to Cloudflare dashboard (`dash.cloudflare.com`)
- [ ] You have a working SSH session to pi4b open in another terminal
- [ ] No critical real-time activity is depending on portal.synth-cloud.com right now
- [ ] You've read this whole runbook once before starting

---

## The two tunnel IDs you need

| Node | Tunnel ID | Currently serves |
|---|---|---|
| **pi4b** | `9b277739-29ec-463f-86e4-13ea3fc4305c` | `portal.synth-cloud.com`, `monitor.synth-cloud.com`, `command.synth-cloud.com`, `ssh.synth-cloud.com` |
| **pi5**  | `419ec665-f5c2-4bc3-b338-fbc6d02094a9` | `app.synth-cloud.com` (configured but DNS not yet set), `ssh2.synth-cloud.com` |

**The change:** move `portal.synth-cloud.com` from pi4b's tunnel ID to pi5's.

---

## Step 1 — DNS change in Cloudflare dashboard (1 min, reversible in 30s)

1. Go to `dash.cloudflare.com`.
2. Select the `synth-cloud.com` zone.
3. Click **DNS → Records**.
4. Find the row where **Name = `portal`** (full name resolves to `portal.synth-cloud.com`).
5. Click **Edit**.
6. The current value will look like `9b277739-29ec-463f-86e4-13ea3fc4305c.cfargotunnel.com` (pi4b's tunnel).
7. Change the value to `419ec665-f5c2-4bc3-b338-fbc6d02094a9.cfargotunnel.com` (pi5's tunnel).
8. **Leave "Proxy status" set to Proxied (orange cloud).**
9. Click **Save**.
10. Note the timestamp — DNS propagation through Cloudflare's edge usually takes <30 seconds for a change inside their own zone, but can be up to 5 min for stragglers.

**Rollback for step 1:** edit the same row, paste back the pi4b tunnel ID, save. Within 30 seconds public traffic is back on the old path.

---

## Step 2 — Update pi5 cloudflared config (2 min)

SSH into pi5 (via pi4b → SentinelRetail), edit cloudflared config:

```bash
ssh pi4b
ssh SentinelRetail
sudo nano /etc/cloudflared/config.yml
```

The current file looks like this:

```yaml
tunnel: 419ec665-f5c2-4bc3-b338-fbc6d02094a9
credentials-file: /etc/cloudflared/419ec665-f5c2-4bc3-b338-fbc6d02094a9.json

ingress:
  - hostname: app.synth-cloud.com
    service: http://localhost:5001
  - hostname: ssh2.synth-cloud.com
    service: ssh://localhost:22
  - service: http_status:404
```

Add the `portal` ingress entry **above** the `ssh2` entry (order doesn't strictly matter; group HTTP-class entries together):

```yaml
tunnel: 419ec665-f5c2-4bc3-b338-fbc6d02094a9
credentials-file: /etc/cloudflared/419ec665-f5c2-4bc3-b338-fbc6d02094a9.json

ingress:
  - hostname: app.synth-cloud.com
    service: http://localhost:5001
  - hostname: portal.synth-cloud.com   # ← ADD THIS
    service: http://localhost:5001     # ← AND THIS
  - hostname: ssh2.synth-cloud.com
    service: ssh://localhost:22
  - service: http_status:404
```

Save (`Ctrl-O`, `Enter`, `Ctrl-X`).

Test config validity before restarting:
```bash
sudo cloudflared tunnel ingress validate
```
Should print `OK`.

Restart cloudflared:
```bash
sudo systemctl restart cloudflared.service
sleep 3
sudo systemctl is-active cloudflared.service   # should be: active
```

**Rollback for step 2:** remove the two added lines, save, restart cloudflared.

---

## Step 3 — Verify portal.synth-cloud.com still works (30 seconds)

From your Mac:
```bash
curl -sI --max-time 15 https://portal.synth-cloud.com/login | head -3
```
Should return `HTTP/2 200`.

Also check the cf-ray header to confirm it's now routing through pi5's tunnel (different cf-ray pattern than before):
```bash
curl -sI --max-time 15 https://portal.synth-cloud.com/login | grep cf-ray
```

If you see `HTTP/2 502` instead, the DNS hasn't propagated yet OR pi5's cloudflared config is wrong. Wait 30 seconds, retry. If still 502 after 2 min, **proceed to rollback** — see bottom of doc.

---

## Step 4 — Remove portal entry from pi4b cloudflared config (2 min)

```bash
ssh pi4b
sudo nano /etc/cloudflared/config.yml
```

Delete the two lines:
```yaml
  - hostname: portal.synth-cloud.com   # ← DELETE
    service: http://10.0.0.11:5001     # ← DELETE
```

Save, validate, restart:
```bash
sudo cloudflared tunnel ingress validate
sudo systemctl restart cloudflared.service
sleep 3
sudo systemctl is-active cloudflared.service
```

Verify other pi4b-tunnel hostnames still work:
```bash
curl -sI --max-time 10 https://command.synth-cloud.com | head -1
curl -sI --max-time 10 https://monitor.synth-cloud.com | head -1
```
Both should return their normal status codes.

**Rollback for step 4:** add the two lines back exactly as they were, restart cloudflared.

---

## Step 5 — Tighten gunicorn bind (3 min)

This is the actual security fix. Pull the prepped change:

```bash
ssh pi4b
ssh SentinelRetail
cd ~/synthos
git pull --ff-only origin main
```

The change to `synthos_build/ops/systemd/pi5/synthos-portal.service` will already be in the repo (I'll prep this commit but not push until you're ready — see "Pre-staged commit" section below).

Apply it:
```bash
sudo cp synthos_build/ops/systemd/pi5/synthos-portal.service \
        /etc/systemd/system/synthos-portal.service
sudo systemctl daemon-reload
sudo systemctl restart synthos-portal.service
sleep 3
sudo systemctl is-active synthos-portal.service   # should be: active
```

---

## Step 6 — Verify (1 min)

From your Mac:
```bash
# Public still works:
curl -sI --max-time 15 https://portal.synth-cloud.com/login | head -3
# Should return HTTP/2 200

# LAN-side now refused:
curl -sI --max-time 5 http://10.0.0.11:5001/login | head -3
# Should fail / connection refused (this proves the LAN exposure is closed)
```

The first must succeed, the second must fail. If the first fails: rollback. If the second succeeds: pi5 didn't actually pick up the new bind — re-check `ss -tlnp | grep 5001` on pi5 (should show `127.0.0.1:5001` not `0.0.0.0:5001`).

---

## Step 7 — Update security audit + review docs (1 min)

Both `docs/security_audit_2026-04-24.md` and `docs/security_review.md`
mention HIGH-4 as deferred. After successful deploy, mark it resolved
in both. Commit + push.

---

## Step 8 — Sanity check (passive, over the next 24h)

- The portal is now reached **only** via pi5's tunnel from any external network
- Nobody on your home Wi-Fi can reach pi5:5001 directly anymore
- pi4b's tunnel still serves `command.synth-cloud.com` + `monitor.synth-cloud.com` unchanged

If anything looks wrong over the next 24h (auth issues, slow page loads,
cf-ray mismatches), the rollback below restores the prior state.

---

## Full rollback procedure (if any step fails midway)

1. Cloudflare dashboard → DNS → edit `portal` row → change CNAME target back to pi4b's tunnel ID (`9b277739-29ec-463f-86e4-13ea3fc4305c.cfargotunnel.com`) → save.
2. `ssh pi4b` then add the `portal` ingress block back to pi4b's `/etc/cloudflared/config.yml` and restart `cloudflared.service`.
3. `ssh pi4b` then `ssh SentinelRetail` then `git revert <bind-commit-hash>` and `sudo cp / daemon-reload / restart synthos-portal.service`.
4. Verify `curl -sI https://portal.synth-cloud.com/login` returns 200.

Total rollback: ~3 minutes from "something's wrong" to "back to known good."

---

## Pre-staged commit

I'll prepare the gunicorn bind change as a **local commit** (not pushed)
so the runbook works step-by-step. When you're ready to execute the
runbook, I push it on your signal so the `git pull` at step 5 has
something to pull. This way you don't have a half-finished change
sitting on origin/main while you wait for focus to do this.

---

## Post-deploy verification checklist

- [ ] `curl -sI https://portal.synth-cloud.com/login` returns HTTP/2 200
- [ ] `curl -sI https://command.synth-cloud.com` returns its normal code
- [ ] `curl -sI https://monitor.synth-cloud.com` returns its normal code
- [ ] `curl -sI http://10.0.0.11:5001` from a device on the LAN: connection refused
- [ ] Login + dashboard load works end-to-end
- [ ] cf-ray on portal.synth-cloud.com is different than before (proves new tunnel path)
- [ ] pi5 `ss -tlnp | grep 5001` shows `127.0.0.1:5001` not `0.0.0.0:5001`
- [ ] pi5 portal/watchdog/cloudflared all `active`
- [ ] pi4b cloudflared `active` after the config change

---

## When to do this

Plan for a 15-min window when:
- You're at your desk (not phone-only)
- You have Cloudflare dashboard access
- You can read SSH output
- You can curl-test from your Mac
- You have 2 hours of buffer afterward in case something subtle surfaces

**Worst times to do this:** during market hours (9:30-16:00 ET weekdays),
right before sleep, mid-travel.

**Best times:** weekend mornings, evenings before you'd next use the
portal heavily.
