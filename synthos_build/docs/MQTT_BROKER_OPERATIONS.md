# MQTT Broker Operations Guide

**Created:** 2026-05-04
**Audience:** operator + anyone debugging telemetry
**Companion docs:** [CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md), [TRADER_GATE_IO_AUDIT.md](TRADER_GATE_IO_AUDIT.md)
**Architecture entry:** `system_architecture.json` → `telemetry_agents` section

---

## What this is

The Synthos telemetry plane: a Mosquitto MQTT broker on Pi5 that
carries heartbeats from every agent, dual-write market data (prices +
regime), and is subscribed by `company_mqtt_listener.py` on pi4b which
persists every observation to `auditor.db.mqtt_observations`.

The trading path does NOT go through MQTT — that's HTTP. MQTT is
strictly telemetry / fan-out.

---

## Topology

```
Pi5 (10.0.0.11)                        pi4b (10.0.0.10)
─────────────────                      ─────────────────
mosquitto:1883  ←──── 24 publishers     synthos_mqtt_listener
   │                  (heartbeats,         │
   │                   prices, regime)     │ subscribes process/+/+
   │                                       │
   └─── retains last message               ↓
        per topic                       auditor.db
        (autosave 30s)                  mqtt_observations table
```

---

## Broker config

**Location on Pi5:** `/etc/mosquitto/conf.d/synthos.conf`
**Source of truth in repo:** `synthos_build/config/mosquitto/synthos.conf`

Key settings:
- `listener 1883 0.0.0.0` — LAN-only (Cloudflare tunnel does NOT forward this port)
- `allow_anonymous false`
- `password_file /etc/mosquitto/passwd` — bcrypt-hashed
- `autosave_interval 30` — retained-message durability window
- `max_queued_messages 1000`

`persistence true` and `persistence_location /var/lib/mosquitto/` come
from the default `mosquitto.conf` and must NOT be redeclared in the
conf.d snippet (mosquitto 2.x errors on duplicate values).

`keepalive_interval` was a mosquitto-1.x global directive; removed in
2.x. Each client negotiates its own keepalive at CONNECT (paho default
60s, set explicitly in `src/mqtt_client.py`).

---

## Authentication

Single username/password pair shared across all clients. Stored in
`user/.env` on every node that publishes or subscribes:

```
MQTT_HOST=10.0.0.11
MQTT_PORT=1883
MQTT_USER=synthos_broker
MQTT_PASS=<random base64 24-char>
NODE_TYPE=process    # or 'company' on pi4b, 'retail-N' for future retail nodes
NODE_ID=process      # or 'company', 'retail-N'
```

**Password rotation:** generate `openssl rand -base64 24`, update
`user/.env` on every node, run `sudo mosquitto_passwd -c
/etc/mosquitto/passwd synthos_broker`, restart mosquitto. All clients
auto-reconnect on next publish (paho).

---

## Topic schema (locked)

| Topic | Publisher | Subscriber(s) | QoS | Retained? | Purpose |
|---|---|---|---|---|---|
| `process/heartbeat/<node>/<agent>` | every wired agent | mqtt_listener (pi4b) | 0 | yes | Agent liveness pulse |
| `process/regime` | macro_regime_agent | mqtt_listener, future trader_server | 0 | yes | Market regime broadcast (BULL/BEAR/NORMAL + detail) |
| `process/prices/<ticker>` | price_poller | mqtt_listener, future trader_server | 0 | yes | Live quote dual-write |
| (LWT) `process/heartbeat/<node>/<agent>` payload `"offline"` | broker auto-publishes on client crash | mqtt_listener | 0 | yes | Detected via paho will_set() |

**Wildcard subscriptions:**
- `process/heartbeat/+/+` — every agent's pulse
- `process/prices/+` — every ticker's quote
- `process/#` — everything (used by mqtt_listener)

---

## Publisher lifecycle (24 agents)

Wired via `register_telemetry()` in each agent's `__main__`:

| Lifecycle | Count | Pattern |
|---|---|---|
| **long-running** | 9 | Background thread, 30s interval, LWT enabled. (portal, watchdog, market_daemon, trade_daemon, interrogation_listener, price_poller, dispatcher, trader_server, mqtt_listener) |
| **one-shot** | 18 | Single publish at startup via `publish_one_shot()`. Subprocess agents (news, sentiment, validator, etc.) — each cycle re-publishes. |

Full enumeration: `system_architecture.json` → `telemetry_agents`.

Helper modules:
- `src/mqtt_client.py` — paho wrapper, `get_publisher()` lazy singleton for publishers
- `src/heartbeat.py` — `register_telemetry()`, `quick_start/stop()`, `HeartbeatPublisher`

---

## Subscriber: company_mqtt_listener (pi4b)

**File:** `synthos-company/company_mqtt_listener.py`
**Service:** `synthos-mqtt-listener.service` (systemd, Type=simple, Restart=always)
**Persists to:** `/home/pi/synthos-company/data/auditor.db` `mqtt_observations` table

Schema:
```sql
CREATE TABLE mqtt_observations (
    topic         TEXT PRIMARY KEY,
    last_seen_ts  REAL NOT NULL,
    last_payload  TEXT,    -- bounded at 2048 chars
    msg_count     INTEGER NOT NULL DEFAULT 0
);
```

Logs minute-window summaries to
`/home/pi/synthos-company/logs/mqtt_listener.log`:
```
[2026-05-04 09:30:42] INFO mqtt_listener: summary 60s window:
  heartbeats=24 regime=0 prices=180 other=0
[2026-05-04 09:31:42] INFO mqtt_listener: summary 60s window:
  heartbeats=24 regime=0 prices=180 other=0
  | STALE: process/news_agent(135s)
```

`STALE: <agent>(<seconds>)` warning fires for any agent that hasn't
pulsed in >120s.

---

## Common operations

### Watch the live stream from any node

```bash
mosquitto_sub -h 10.0.0.11 -u synthos_broker -P "$MQTT_PASS" -t 'process/#' -v
```

Filters:
- `-t 'process/heartbeat/+/+'` — just heartbeats
- `-t 'process/prices/AAPL'` — just AAPL quotes
- `-t 'process/heartbeat/+/news_agent'` — just news_agent pulse from any node

### Inject a test message

```bash
mosquitto_pub -h 10.0.0.11 -u synthos_broker -P "$MQTT_PASS" \
  -t 'test/synthos/probe' -m 'hello from operator' -q 0 -r
```

### Read what the auditor has captured (from pi4b)

```bash
ssh pi4b 'python3 -c "
import sqlite3, datetime
conn = sqlite3.connect(\"/home/pi/synthos-company/data/auditor.db\")
rows = conn.execute(\"SELECT topic, datetime(last_seen_ts, \\\"unixepoch\\\"), msg_count FROM mqtt_observations ORDER BY last_seen_ts DESC LIMIT 20\").fetchall()
for r in rows: print(r)
"'
```

### Check broker health

```bash
ssh pi4b 'ssh SentinelRetail "systemctl is-active mosquitto"'
ssh pi4b 'ssh SentinelRetail "sudo ss -tn | grep \":1883 \""'   # active connections
```

### Wipe a stale retained message

If a TEST message is sitting in the broker forever (e.g. from a smoke
test), republish empty payload with retain flag:

```bash
mosquitto_pub -h 10.0.0.11 -u synthos_broker -P "$MQTT_PASS" \
  -t 'process/regime' -m '' -r
```

---

## Failure modes + recovery

| Symptom | Likely cause | Fix |
|---|---|---|
| `connect refused: rc=5` from a publisher | Wrong MQTT_USER/MQTT_PASS in that node's `user/.env` | Verify env, re-source, restart agent |
| `auditor.db.mqtt_observations` row count plateaus | Listener disconnected from broker | `systemctl restart synthos-mqtt-listener.service` on pi4b |
| Same agent shows STALE in listener log every minute | That agent crashed or its `register_telemetry()` is unwired | Check the agent's process; check journalctl for crashes |
| Broker eats SD card writes | autosave_interval too low | Already at 30s; increase only if SD wear is measured |
| `Duplicate ... value in configuration` on mosquitto restart | Someone added persistence/persistence_location to synthos.conf | Remove — those come from default `/etc/mosquitto/mosquitto.conf` |

---

## What MQTT is NOT

To prevent future architectural drift:

- ❌ **MQTT is NOT the trading transport.** Trading uses HTTP RPC
  (dispatcher → trader_server). This was decided after considering
  MQTT job dispatch and rejecting it as over-engineered for our scale.
- ❌ **MQTT is NOT the source of truth for prices/regime.** SQLite
  `live_prices` table and `customer_settings._MACRO_REGIME` remain
  authoritative; MQTT is dual-write fan-out.
- ❌ **MQTT does NOT replace the existing heartbeat mechanisms.**
  `retail_heartbeat.py` still POSTs to monitor.db; `node_heartbeat.py`
  still writes system metrics. MQTT is additive — gives sub-second
  visibility of the same data the monitor was already tracking on
  longer cadences.

---

## Adding a new MQTT publisher

1. Decide lifecycle: long-running (background thread) vs one-shot (cycle-fired).
2. In the agent's `__main__` block, after argparse:
   ```python
   try:
       from heartbeat import register_telemetry as _register_telemetry
       _register_telemetry("agent_name_here", long_running=True)  # or False
   except Exception:
       pass
   ```
3. (For dual-write publishers) at the SQLite write site, add:
   ```python
   try:
       from mqtt_client import get_publisher
       _mqtt = get_publisher()
       if _mqtt is not None:
           _mqtt.publish("process/<your_topic>", payload_dict, qos=0, retain=True)
   except Exception as _e:
       log.debug(f"MQTT publish failed (non-fatal): {_e}")
   ```
4. Add the agent to `system_architecture.json` → `telemetry_agents` list (per the maintenance contract in CLAUDE.md).
5. Smoke-test:
   ```bash
   mosquitto_sub -h 10.0.0.11 -u synthos_broker -P "$MQTT_PASS" -t 'process/heartbeat/+/<agent_name>' -v
   ```
   Then trigger the agent. Should see one publish.
