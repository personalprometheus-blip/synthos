# Distributed-Trader Cutover Runbook

**Created:** 2026-05-04
**Owner:** operator
**Audience:** anyone (human or AI agent) executing the migration from
single-box daemon trading to the distributed dispatcher + retail-server
architecture

---

## What this document covers

The end-state of Tiers 1–6 is that BOTH paths exist and work, but every
customer is still being traded by `retail_market_daemon` (the legacy
fan-out). This runbook is the procedure for moving customers, one at a
time, onto the new path: process-node dispatcher → HTTP → retail-server.

It does NOT cover building new hardware (retail-2). That's a separate
prerequisite when standing up additional retail nodes.

---

## Pre-flight checklist (one-time, before migrating any customer)

Run these once before starting. Each line either passes or you stop and
fix it before proceeding.

### 1. MQTT broker is up and reachable
```bash
ssh pi4b 'ssh SentinelRetail "mosquitto_pub -h localhost -u synthos_broker -P \"$MQTT_PASS\" -t test/preflight -m ok && echo PUB_OK"'
```
Expect: `PUB_OK`. If the broker is down, fix mosquitto.service before continuing — the new path's heartbeats depend on it.

### 2. The trader_server runs on the target retail node
For now retail-N == process node (loopback). Future retail-2 will run on
its own pi5 8GB at 10.0.0.20. Either way:
```bash
ssh pi4b 'ssh SentinelRetail "curl -sf http://127.0.0.1:8443/readyz"'
```
Expect: `{"status":"ready","dispatch_mode":"distributed",...}`. If the
server isn't running yet, it's not in systemd yet — see *Standing up the
retail trader server* below.

### 3. The dispatcher runs on the process node
```bash
ssh pi4b 'ssh SentinelRetail "systemctl is-active synthos-dispatcher.service"'
```
Expect: `active`. If not yet a systemd unit, see *Standing up the dispatcher* below.

### 4. The migration CLI works
```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py status"'
```
Expect: a table showing every active customer + their current mode (all
should show `daemon` initially).

### 5. The auditor listener on pi4b is recording observations
```bash
ssh pi4b 'python3 -c "import sqlite3; print(sqlite3.connect(\"/home/pi/synthos-company/data/auditor.db\").execute(\"SELECT COUNT(*) FROM mqtt_observations\").fetchone()[0])"'
```
Expect: a number > 0 and growing every cycle. If 0 or stale, listener
can't see the broker — fix before migrating.

---

## Standing up the retail trader server (one-time)

Drop a systemd unit so synthos_trader_server runs as a managed service:

```bash
ssh pi4b 'ssh SentinelRetail "sudo tee /etc/systemd/system/synthos-trader-server.service" << UNIT
[Unit]
Description=Synthos Retail Trader Server (FastAPI on :8443)
After=network.target

[Service]
Type=simple
User=pi516gb
Group=pi516gb
WorkingDirectory=/home/pi516gb/synthos/synthos_build
EnvironmentFile=/home/pi516gb/synthos/synthos_build/user/.env
Environment=DISPATCH_MODE=distributed
ExecStart=/usr/bin/python3 /home/pi516gb/synthos/synthos_build/agents/synthos_trader_server.py
Restart=on-failure
RestartSec=15
StandardOutput=append:/home/pi516gb/synthos/synthos_build/logs/trader_server.log
StandardError=append:/home/pi516gb/synthos/synthos_build/logs/trader_server.log

[Install]
WantedBy=multi-user.target
UNIT'

ssh pi4b 'ssh SentinelRetail "sudo systemctl daemon-reload && sudo systemctl enable --now synthos-trader-server.service"'
```

Verify: `curl -sf http://127.0.0.1:8443/readyz` returns 200.

---

## Standing up the dispatcher (one-time)

```bash
ssh pi4b 'ssh SentinelRetail "sudo tee /etc/systemd/system/synthos-dispatcher.service" << UNIT
[Unit]
Description=Synthos Distributed Dispatcher (process-node orchestrator)
After=network.target synthos-trader-server.service

[Service]
Type=simple
User=pi516gb
Group=pi516gb
WorkingDirectory=/home/pi516gb/synthos/synthos_build
EnvironmentFile=/home/pi516gb/synthos/synthos_build/user/.env
Environment=DISPATCH_MODE=daemon
Environment=RETAIL_URL=http://127.0.0.1:8443
ExecStart=/usr/bin/python3 /home/pi516gb/synthos/synthos_build/agents/synthos_dispatcher.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/pi516gb/synthos/synthos_build/logs/dispatcher.log
StandardError=append:/home/pi516gb/synthos/synthos_build/logs/dispatcher.log

[Install]
WantedBy=multi-user.target
UNIT'

ssh pi4b 'ssh SentinelRetail "sudo systemctl daemon-reload && sudo systemctl enable --now synthos-dispatcher.service"'
```

Note `Environment=DISPATCH_MODE=daemon` here — that's the **env-default**.
Per-customer settings (set via the migration CLI) override it. The
dispatcher will run continuously, but until at least one customer is
flipped to distributed, every cycle is a fast no-op.

---

## Migration: per-customer cutover

For each customer you want to migrate:

### Step 1 — Baseline the customer

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py status" | grep <customer_id_prefix>'
```
Confirm current mode is `daemon`.

### Step 2 — Flip the switch

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py enable <full_customer_id>"'
```
Output: `[xxxxxxxx] daemon → distributed`. Audit row written to that
customer's events table.

### Step 3 — Watch one full market session

For at least ONE complete market session (9:30 ET to 16:00 ET on a
trading day), monitor:

```bash
# Dispatcher activity for this customer
ssh pi4b 'ssh SentinelRetail "tail -f /home/pi516gb/synthos/synthos_build/logs/dispatcher.log"'

# Trader server processing
ssh pi4b 'ssh SentinelRetail "tail -f /home/pi516gb/synthos/synthos_build/logs/trader_server.log"'

# Auditor seeing the heartbeats
ssh pi4b 'tail -f /home/pi/synthos-company/logs/mqtt_listener.log'
```

Expected signals of health:
- Dispatcher logs `cycle complete: N ok, 0 fail` every 30s
- Trader server logs `[cust-xxx] work wrk-... done in N ms` per dispatch
- Auditor sees `process/heartbeat/process/dispatcher` and
  `process/heartbeat/<retail_node>/trader_server` pulse continuously

Failure modes to watch for:
- Dispatcher logs `dispatch failed` repeatedly → retail server unreachable
- Trader server logs `work crashed` → packet contained something
  unexpected; dispatcher built the packet wrong
- Customer reports missed/wrong trades → state divergence; immediately
  proceed to Step 4

### Step 4 — Roll back if anything looks wrong

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py disable <full_customer_id>"'
```
Effect is immediate (next cycle picks it up). The daemon trader resumes
ownership of this customer. No data loss because the master DB is the
single source of truth and both paths write through it.

### Step 5 — Migrate the next customer

Repeat Steps 1–4 for the next customer. Don't migrate two on the same
day — it's hard to attribute symptoms when both flipped at once.

Recommended cadence: one customer per market day. Faster if confidence
is high after several successful migrations.

---

## Decommission the daemon trader path (after 100% on distributed)

Only do this once `synthos_migration.py status` shows ZERO customers
on `daemon` mode for ≥1 full week of sustained operation.

### Step 1 — Snapshot the current state
```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos && git log -1 --oneline > /tmp/pre_decom_state.txt && cp synthos_build/src/retail_market_daemon.py /tmp/pre_decom_daemon.py.bak"'
```

### Step 2 — Edit retail_market_daemon.py

Delete the body of `run_trade_all_customers` and replace with:
```python
def run_trade_all_customers(session='open'):
    log.info(
        f"[TRADE] daemon trader path decommissioned 2026-XX-XX. "
        f"All trading goes through synthos_dispatcher in distributed mode."
    )
    return 0, 0
```

### Step 3 — Delete the trader entry from retail_scheduler.SESSION_PIPELINES

Each pipeline list (open, midday, close, etc.) has a tuple referencing
`retail_trade_logic_agent.py`. Remove all of them.

### Step 4 — Commit, deploy, watch one session

```bash
git commit -m "chore(decommission): remove daemon trader fan-out — distributed-only"
git push origin main
ssh pi4b 'ssh SentinelRetail "cd ~/synthos && git pull && sudo systemctl restart synthos-market-daemon.service synthos-trade-daemon.service"'
```

Monitor through one full market session. If anything breaks, revert
the commit and restart services — the daemon path comes back instantly.

### Step 5 — Update the architecture doc

Bump `data/system_architecture.json` to reflect the change. Mark
`retail_trade_daemon.py` and `retail_trade_logic_agent.py`'s daemon
status as `archived`.

---

## Adding retail-2 (future, when hardware exists)

Prerequisites:
- New pi5 8GB hardware with Pi OS installed
- Static IP 10.0.0.20 (or whatever you assign)
- iptables rules per `synthos-company/docs/NETWORK_CONFIG.md`

### Step 1 — Provision the node
- Run `install_retail.py` on it (will be a future installer revision; for
  now: clone synthos repo, `pip install paho-mqtt httpx fastapi uvicorn`,
  copy user/.env from process node, set NODE_ID=retail-2)
- Drop the synthos-trader-server.service systemd unit (see above)
- Verify `curl -sf http://10.0.0.20:8443/readyz` from process node

### Step 2 — Update the dispatcher

Currently `RETAIL_URL` is a single value. To round-robin across N retails,
extend the dispatcher to read `RETAIL_URLS` (comma-separated list) and
hash customer_id → URL. This is a small enhancement (~30 lines) — defer
until you actually have retail-2 hardware to test against.

### Step 3 — Test with one customer

Migrate ONE customer to distributed, verify dispatcher routes to retail-2
specifically (force assignment), watch one session.

### Step 4 — Distribute load

After confidence: full round-robin assignment of distributed customers
across {retail-1, retail-2}.

---

## Failure recovery cheat-sheet

| Symptom | Likely cause | Quick fix |
|---|---|---|
| `dispatcher: cycle complete: 0 ok, N fail` | retail server unreachable | `systemctl status synthos-trader-server` on retail; restart if down |
| Customer reports missed trade | dispatcher / trader_server returned error → no order placed | check trader_server.log for the `work_id`; re-run cycle if Alpaca says it didn't fill |
| Heartbeats stop appearing in auditor | broker died OR network partition | check mosquitto.service on Pi5; check pi4b mqtt_listener.log for reconnect attempts |
| One customer "stuck" | maybe their _DISPATCH_MODE setting is corrupt | `python3 synthos_migration.py reset <cid>` then re-flip explicitly |
| Bulk regression: everyone failing | bad commit deployed | `git revert HEAD && git push`, then `git pull` on Pi + restart services |

---

## Status check (any time)

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py status"'
```

This is the only command you need to know what's where.
