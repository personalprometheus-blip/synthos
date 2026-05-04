# Installer Profiles — Which Installer For Which Node

**Created:** 2026-05-04 (Phase B of distributed-trader migration)
**Audience:** anyone provisioning new Synthos hardware
**Companion docs:** [CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md), [DISPATCH_MODE_GUIDE.md](DISPATCH_MODE_GUIDE.md)

---

## Three installers, three node types

Synthos runs as a **distributed system** where different hardware plays
different roles. Each role has its own installer:

| Node type | Installer | Repo | Purpose |
|---|---|---|---|
| **process** | `synthos_build/src/install_retail.py` | synthos | Master DBs, signal-producing agents, mosquitto broker, dispatcher, retail_portal. Today's Pi5 (16GB). |
| **retail-N** | `synthos_build/src/install_retail_node.py` | synthos | NEW 2026-05-04. Trader-only. Runs `synthos_trader_server` on :8443. Stateless — no DBs, no portal, no signal agents. Future Pi5 8GB at 10.0.0.20+. |
| **company** | `install_company.py` | synthos-company | Operations: monitor portal, auditor, scoop email, vault, fidget, librarian, archivist, mqtt_listener. pi4b. |

The three installers are deliberately separate (not one with a
`--profile` flag) so that:
- Each is small enough to fit in working memory while reviewing
- Each ships separately and can be versioned independently
- An operator who reads `install_retail_node.py` doesn't get tripped up
  by code that only runs on the process node
- Schema/dep mistakes on one don't block the others

---

## Choosing the right installer

### "I'm setting up the existing Pi5 (16GB)"
Use `install_retail.py`. Despite the name (legacy from before the
distributed-trader migration), it installs the **process node** profile
— the full stack today. Currently this Pi5 does triple duty: process
node + retail-1 (loopback trader) + still hosts `retail_portal`.

After Phase A (2026-05-04), `synthos_trader_server.service` runs in
**loopback mode** on this Pi5 (`TRADER_DB_MODE=local`), reading customer
DBs directly. Distributed-mode customers (currently just Eliana) get
their packets dispatched via HTTP to localhost:8443 and trade through
this same machine.

### "I'm setting up new retail hardware (retail-1, retail-2, ...)"
Use `install_retail_node.py`. This is the **retail-N profile** —
minimal, trader-only.

What it installs:
- Python 3 + apt deps (paho-mqtt, httpx, fastapi, uvicorn, requests, dotenv)
- The trader_server, the trader, and the helper modules required at import time
- `synthos-trader-server.service` systemd unit with `TRADER_DB_MODE=packet`
- user/.env with MQTT credentials, dispatcher auth token, NODE_ID=retail-N

What it does NOT install:
- ❌ Customer signals.dbs (those live on the process node)
- ❌ Master signals.db
- ❌ retail_portal
- ❌ signal-producing agents (news, sentiment, screener, validator,
  macro, market_state, bias, fault, candidate_generator)
- ❌ price_poller (process node only)
- ❌ Mosquitto broker (process node only)
- ❌ retail_backup (retail nodes have nothing to back up)

**Pre-install bootstrap:** the operator brings a USB or pre-creates
`~/.synthos/retail-N.env` containing the MQTT credentials and dispatcher
auth token (must match the process node's values). Per-customer Alpaca
credentials never live on this node — they arrive in WorkPackets and
are scoped to the request.

### "I'm setting up the company node (pi4b)"
Use `install_company.py` from the synthos-company repo. This installs
the operations stack: monitor portal, auditor, scoop, vault, fidget,
librarian, archivist, and the MQTT listener that subscribes to the
broker on the process node.

---

## Cross-node dependencies (what must exist before what)

Setup order matters. The dependency graph:

```
   ┌─────────────────────────────────┐
   │  process node (Pi5)             │
   │  install_retail.py              │
   │                                 │
   │  Sets up:                       │
   │   - Mosquitto broker            │
   │   - DISPATCH_AUTH_TOKEN         │
   │   - MQTT_PASS                   │
   │                                 │
   │  Dependencies: none             │
   └─────────────────────────────────┘
                 │
                 │ supplies MQTT auth + dispatch token
                 ▼
   ┌─────────────────────────────────┐         ┌──────────────────────────────┐
   │  retail-N node                  │         │  company node (pi4b)         │
   │  install_retail_node.py         │         │  install_company.py          │
   │                                 │         │                              │
   │  Connects to:                   │         │  Connects to:                │
   │   - process MQTT (1883)         │         │   - process MQTT (subscribes)│
   │   - process dispatcher (POST    │         │   - process SSH (auditor)    │
   │     packets in to retail's      │         │                              │
   │     /work endpoint)             │         │  Dependencies: process broker│
   │                                 │         │     reachable for telemetry  │
   │  Dependencies: process broker   │         └──────────────────────────────┘
   │     reachable + creds match     │
   └─────────────────────────────────┘
```

So setup order is:
1. **First:** install_retail.py on process node (Pi5) — broker comes up, dispatcher idles
2. **Second:** install_company.py on company node (pi4b) — listener connects to broker
3. **Third (or anytime after):** install_retail_node.py on retail-1 hardware —
   trader_server registers, ready to receive work
4. **Fourth:** on the process node, set `RETAIL_URL=http://<retail-1-ip>:8443`
   in user/.env, restart synthos-dispatcher.service
5. **Fifth:** migrate first customer via `synthos_migration.py enable <cid>`

---

## Cutover from loopback to retail-1

Today's reality: process node Pi5 is also serving as retail-1 (loopback
mode). When real retail-1 hardware arrives:

### One-shot transition
1. Provision retail-1 with `install_retail_node.py --node-num=1`
2. Verify retail-1's trader_server responds: `curl http://10.0.0.20:8443/readyz`
3. On process node, edit `user/.env`:
   ```
   RETAIL_URL=http://10.0.0.20:8443  # was http://127.0.0.1:8443
   ```
4. Restart dispatcher: `sudo systemctl restart synthos-dispatcher.service`
5. **Process node trader_server stays running** as a fallback in case
   retail-1 misbehaves — its `/work` endpoint is still reachable but
   the dispatcher is no longer pointed at it. To roll back, edit
   `RETAIL_URL` back to `127.0.0.1:8443` and restart dispatcher.

### Multi-retail (retail-1 + retail-2)
Future: `RETAIL_URLS` (CSV) replaces `RETAIL_URL`. Dispatcher hashes
customer_id → URL for sticky assignment. Out of scope for the first
cutover; documented in CUTOVER_RUNBOOK.md.

---

## Validating an install

Each installer has a `--status` mode that prints what's installed.
Always run after install:

```bash
# process node
python3 synthos_build/src/install_retail.py --status

# retail-N node
python3 synthos_build/src/install_retail_node.py --status

# company node
python3 install_company.py --status
```

Plus the broker round-trip from the process node:
```bash
mosquitto_sub -h localhost -u synthos_broker -P "$MQTT_PASS" -t 'process/heartbeat/+/+' -v
```
Within 60s you should see heartbeats from every long-running agent on
both process and retail-N (and the company listener publishes its own
heartbeat).

And the dispatcher → retail-N round-trip:
```bash
# From the process node, with no customers on distributed mode:
sudo systemctl restart synthos-dispatcher.service
journalctl -u synthos-dispatcher.service -f
# Should see "no customers on distributed dispatch this cycle" every 30s.
# That confirms dispatcher is alive and filtering correctly.
```

---

## Future installer work (not yet shipped)

The three installers are now coherent for the architecture as it stands.
A few things deferred:

- **`install_retail.py` → `install_process.py` rename.** The current
  name is legacy. A clean rename + symlink-for-backcompat would clarify
  the topology. Tracked in `synthos/TODO.md`.
- **Shared common installer module.** Both install_retail.py and
  install_company.py have copies of `installers/common/` (preflight.py,
  progress.py, env_writer.py). Should converge — pulled in via git
  subtree or a synthos-installer-common package.
- **Single-pane-of-glass installer matrix.** A meta-installer that
  detects the local node's role from MQTT broker presence + git remote
  URL and dispatches to the right per-profile installer. Operator
  convenience; not blocking.
