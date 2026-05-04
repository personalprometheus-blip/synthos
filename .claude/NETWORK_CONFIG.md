# Network Configuration — v2.0 Design

**Status:** Draft for review (v1) → AS-BUILT corrections appended 2026-05-04 (v2)
**Author:** Claude Code, 2026-05-03; corrected 2026-05-04
**Related docs:** [INSTALLER_PROFILES.md](../synthos_build/docs/INSTALLER_PROFILES.md), [WORK_PACKET_PROTOCOL.md](../synthos_build/docs/WORK_PACKET_PROTOCOL.md), [MQTT_BROKER_OPERATIONS.md](../synthos_build/docs/MQTT_BROKER_OPERATIONS.md), [CUTOVER_RUNBOOK.md](../synthos_build/docs/CUTOVER_RUNBOOK.md)

---

> ## 🚨 IMPORTANT — AS-BUILT DEVIATES FROM v1 DESIGN BELOW
>
> This document was drafted 2026-05-03 BEFORE the architectural decision
> to use HTTP RPC (not MQTT) for trader work-packet dispatch. The
> sections below that show MQTT carrying work packets and trade results
> are **the original design proposal, not the shipped system.**
>
> **What actually shipped (Phases A+B, 2026-05-04):**
>
> | Channel | Transport | Topic / Endpoint |
> |---|---|---|
> | Trader work packets (process → retail-N) | **HTTP POST** (NOT MQTT) | `http://retail-N:8443/work` with `X-Dispatch-Token` auth header |
> | Trade results (retail-N → process) | **HTTP response body** (NOT MQTT) | Synchronous response to the same `/work` POST — `WorkResult` JSON containing the `StateDelta` |
> | Live prices (1:N broadcast) | MQTT QoS 0, retained | `process/prices/<ticker>` |
> | Market regime (1:N broadcast) | MQTT QoS 0, retained | `process/regime` |
> | Agent heartbeats (N:1 telemetry) | MQTT QoS 0, retained, LWT | `process/heartbeat/<node>/<agent>` |
> | Auditor monitoring | MQTT subscribe wildcard | `process/#` (from pi4b) |
>
> **The MQTT topics `process/signals/pending` and `retail-{N}/trade/result`
> shown in §2.3 below are NOT used.** Work packets travel via HTTP for
> the reasons documented in [WORK_PACKET_PROTOCOL.md](../synthos_build/docs/WORK_PACKET_PROTOCOL.md):
> synchronous request/response semantics, simpler auth, no broker
> dependency for trading itself, easier debugging via curl.
>
> **What's correct in v1 below:**
> - §1.1 three-node topology
> - §2.1 broker location (process node)
> - §2.2 mosquitto install procedure
> - §3 IP addressing
> - The CONCEPT of QoS levels (just applied to telemetry only)
>
> **See §6.4 (added) for AS-BUILT data flows.**
>
> Future cleanup tracked in synthos/TODO.md: rewrite §1.2, §1.3, §2.3,
> §2.4 to match shipped reality. For now this banner is the
> source-of-truth correction.

---

## 1. Architecture Overview

### 1.1 Three-Node Topology

```
┌────────────────────────────────────────────────────────────┐
│ YOUR LAPTOP (Mac)                                          │
│ ├─ SSH via Cloudflare tunnel → company (10.0.0.10)        │
│ ├─ SSH direct LAN → process (10.0.0.11)                   │
│ └─ SSH direct LAN → retail-N (10.0.0.20+)                 │
└────────────┬───────────────────────────────────────────────┘
             │
             │ (ISP modem)
             │
    ┌────────┴─────────┐
    │                  │
    ▼                  ▼
┌─────────────┐  ┌──────────────┐
│ Switch 1    │  │ Switch 2     │
└──────┬──────┘  └──────┬───────┘
       │                │
       ▼                ▼
┌────────────────┐  ┌────────────────┐
│ PROCESS NODE   │  │ COMPANY NODE   │
│ (pi5)          │  │ (pi4b)         │
│ 10.0.0.11      │  │ 10.0.0.10      │
│                │  │                │
│ • Signals hub  │  │ • Ops hub      │
│ • MQTT broker  │  │ • Backups      │
│ • Price poll   │  │ • Monitoring   │
│ • Customer auth│  │ • Command      │
│ • Work packets │  │ • Cloudflare   │
│   to retail-N  │  │   tunnel       │
└────────┬───────┘  └────────┬───────┘
         │                   │
         │ (MQTT 1883)       │ (HTTPS to monitor)
         │                   │
    ┌────┴──────┬─────────┐  │
    │            │         │  │
    ▼            ▼         ▼  ▼
┌─────────┐ ┌─────────┐ ┌────────────┐
│Retail-1 │ │Retail-2 │ │  Monitor   │
│10.0.0.20│ │10.0.0.21│ │ (WiFi)     │
│Pi5/Pi4  │ │Pi5/Pi4  │ │ Pi2W       │
│         │ │         │ │            │
│ Trader  │ │ Trader  │ │ Heartbeat  │
│ agents  │ │ agents  │ │ Watchdog   │
└─────────┘ └─────────┘ └────────────┘
```

### 1.2 Data Flow During Market Hours

```
market_hours = 09:30–16:00 EST (US equity trading)

1. Price polling (Process node)
   ├─ Alpaca API → live prices (every 1-5 sec)
   └─ Publish: mqtt://10.0.0.11:1883/process/signals/price (QoS 0)

2. Signal generation (Process node)
   ├─ Read prices, compute signals
   ├─ Generate work packets for retail traders
   └─ Publish: mqtt://10.0.0.11:1883/process/signals/pending (QoS 1)

3. Retail trade execution
   ├─ retail-N subscribes: process/signals/pending (QoS 1)
   ├─ Receive work packet (~100ms latency target)
   ├─ Execute trade via Alpaca API
   └─ Publish result: mqtt://10.0.0.11:1883/retail-{N}/trade/result (QoS 1)

4. Process aggregates results
   ├─ Subscribe: retail-*/trade/result
   ├─ Aggregate customer fills, margin, P&L
   └─ Store in auth.db + signals.db
```

### 1.3 Data Flow Outside Market Hours

```
outside_market_hours = 16:00–09:30 EST, weekends

1. Price polling (Process node)
   ├─ Drops to 1 poll/hour (not critical)
   └─ Publish: same topic, lower frequency

2. Retail nodes
   ├─ Backgrounded (no work packets)
   ├─ Services remain running (for fast restart at market open)
   └─ Heartbeat only (1 msg/min to process)

3. Company monitoring
   ├─ Continuous MQTT subscription to process node
   ├─ Can query /healthz endpoints anytime
   └─ Backups run on schedule (no market-hours interference)
```

---

## 2. MQTT Broker Setup

### 2.1 Choice: Mosquitto on Process Node

**Decision: Process node (pi5) runs embedded MQTT broker (`mosquitto`).**

**Rationale:**
- Lightweight (minimal resource overhead on pi5)
- No extra hardware needed for v2.0
- Operator has direct SSH access to restart if needed
- Future (v2.1): can move to dedicated node if volume grows

**Alternative (deferred to v2.1):**
- Dedicated MQTT node (separate pi2w with mosquitto)
- Frees up process node CPU for signals work
- Requires additional hardware + network setup

### 2.2 Mosquitto Installation (installer v2 responsibility)

```bash
# installers/common/mqtt.py (NEW module)

def install_mosquitto(node_type: str, process_node_ip: str, home_dir: Path) -> None:
    """Install + configure mosquitto (process node only)."""
    
    if node_type != "process":
        return  # Only process node runs broker
    
    # 1. apt install mosquitto mosquitto-clients
    subprocess.run(["apt-get", "install", "-y", "mosquitto", "mosquitto-clients"], check=True)
    
    # 2. Write /etc/mosquitto/conf.d/synthos.conf
    config = f"""
# Synthos MQTT broker configuration
listener 1883
protocol mqtt

# Bind to LAN only (not localhost, not external)
bind_address 10.0.0.11

# No authentication for now (LAN-internal only)
allow_anonymous true

# Persistence
persistence true
persistence_location /var/lib/mosquitto/

# Max connections (sufficient for 10 retail nodes + 1 company monitor)
max_connections -1

# Message queuing
max_queued_messages 1000
"""
    Path("/etc/mosquitto/conf.d/synthos.conf").write_text(config)
    
    # 3. systemctl enable mosquitto
    subprocess.run(["systemctl", "enable", "mosquitto"], check=True)
    subprocess.run(["systemctl", "start", "mosquitto"], check=True)
    
    # 4. Verify
    result = subprocess.run(
        ["mosquitto_sub", "-h", "10.0.0.11", "-p", "1883", "-t", "$SYS/broker/version", "-W", "1"],
        capture_output=True, timeout=5
    )
    if result.returncode != 0:
        raise InstallError("Mosquitto broker not responding")
```

### 2.3 MQTT Topics & Publish/Subscribe

| Topic | Publisher | Subscribers | QoS | Frequency | Payload |
|-------|-----------|-------------|-----|-----------|---------|
| `process/signals/price` | Process | Retail-N, Company (monitor) | 0 | 1–5 sec (market hrs) | `{"symbol": "SPY", "price": 445.23, "ts": "2026-05-03T14:30:00Z"}` |
| `process/signals/pending` | Process | Retail-N | 1 | ~100ms when signal fires | `{"work_id": "uuid", "customer_id": 123, "symbol": "SPY", "qty": 10, "side": "buy"}` |
| `retail-{N}/trade/result` | Retail-N | Process, Company (monitor) | 1 | ~200ms after trade | `{"work_id": "uuid", "status": "filled", "executed_qty": 10, "price": 445.25, "ts": "2026-05-03T14:30:00.2Z"}` |
| `process/heartbeat` | Process | Company (monitor) | 0 | 1/min (always) | `{"uptime_sec": 3600, "customer_count": 50, "retail_nodes": 3, "active_signals": 5}` |
| `retail-{N}/heartbeat` | Retail-N | Company (monitor), Process | 0 | 1/min (always) | `{"node_num": 1, "status": "ready", "active_agents": 4, "uptime_sec": 7200}` |

### 2.4 QoS Rationale

- **QoS 0 (fire-and-forget):** price updates, heartbeats. Missed updates don't cause problems (next one comes in seconds).
- **QoS 1 (at-least-once):** work packets, trade results. Duplicates are idempotent (work_id ensures we don't double-execute).

---

## 3. IP Addressing Scheme

### 3.1 Static IP Assignment

| Node | Default IP | MAC | Range | Role |
|------|-----------|-----|-------|------|
| company | 10.0.0.10 | `b8:27:eb:xx:xx:xx` (pi4b) | — | Ops hub, Cloudflare tunnel, backups |
| process | 10.0.0.11 | `b8:27:eb:yy:yy:yy` (pi5) | — | Signals hub, MQTT broker, price polling |
| retail-1 | 10.0.0.20 | `b8:27:eb:zz:zz:zz` (pi5 or pi4) | — | Trader agent 1 |
| retail-2 | 10.0.0.21 | — | — | Trader agent 2 |
| retail-3 | 10.0.0.22 | — | — | Trader agent 3 |
| retail-N | 10.0.0.(20+N-1) | — | — | Trader agent N |
| monitor | 10.0.0.12 | `b8:27:eb:mm:mm:mm` (pi2w) | WiFi (separate SSID) | Heartbeat, watchdog (WiFi only) |

### 3.2 Auto-Detection During Install

The installer will detect the current IP and prompt:

```bash
./install.sh --node=process

=== NETWORK CONFIGURATION ===
Detected current IP: 10.0.0.42 (via DHCP)
Process node default IP: 10.0.0.11

Options:
  1. Keep current (10.0.0.42) — DHCP reservation required
  2. Switch to default (10.0.0.11) — static config
  3. Enter custom IP

Choice [1]: 2

Switching to 10.0.0.11 (static).

⚠ After installation, run:
  sudo ip addr show eth0
  sudo systemctl restart networking

To make permanent (survive reboot), a static IP config will be written to:
  /etc/dhcpcd.conf (or /etc/network/interfaces on Bullseye)
```

### 3.3 Implementation: `installers/common/network.py` (Enhanced)

```python
def configure_ip(node_type: str, node_num: Optional[int], home_dir: Path) -> str:
    """
    Detect current IP, prompt operator, configure static or DHCP reservation.
    Returns: assigned IP address (e.g. "10.0.0.11")
    """
    
    default_ips = {
        "company": "10.0.0.10",
        "process": "10.0.0.11",
        "retail": lambda n: f"10.0.0.{20 + n - 1}",
    }
    
    default_ip = (
        default_ips[node_type] 
        if node_type != "retail"
        else default_ips["retail"](node_num)
    )
    
    current_ip = _detect_current_ip()
    print(f"Detected current IP: {current_ip}")
    print(f"Recommended IP for {node_type}: {default_ip}")
    
    choice = input("Keep current? (y/n) [n]: ").lower() or "n"
    
    if choice == "y":
        # DHCP reservation: operator must configure router
        print(f"⚠ Configure DHCP reservation on your router:")
        print(f"  MAC: {_get_mac_address()}")
        print(f"  IP:  {current_ip}")
        _ensure_dhcp_only()
        return current_ip
    else:
        custom = input(f"Enter static IP [{default_ip}]: ") or default_ip
        _write_static_ip_config(custom)
        return custom

def _write_static_ip_config(ip: str) -> None:
    """Write static IP to dhcpcd.conf (Bullseye+) or /etc/network/interfaces."""
    # Detect which interface (eth0, wlan0, etc.)
    iface = _get_primary_interface()
    
    config = f"""
interface {iface}
static ip_address={ip}/24
static routers=10.0.0.1
static domain_name_servers=8.8.8.8 8.8.4.4
"""
    
    dhcpcd_path = Path("/etc/dhcpcd.conf")
    if dhcpcd_path.exists():
        # Append to dhcpcd.conf (Bullseye+)
        dhcpcd_path.write_text(config)
    else:
        # Fallback to /etc/network/interfaces (older Raspberry Pi OS)
        interfaces_path = Path("/etc/network/interfaces")
        interfaces_path.write_text(config)
    
    # Restart networking
    subprocess.run(["systemctl", "restart", "networking"], check=True)
```

---

## 4. Network Isolation & Firewall Rules

### 4.1 Security Goal

**"Only specific devices on my network can see and connect to the Pi systems. If an external attacker compromises a retail node, they cannot reach company or process nodes."**

### 4.2 Current Isolation (Modem-level)

```
Modem (single ISP connection)
  ├─ Output 1 → Switch 1 (process node)
  ├─ Output 2 → Switch 2 (company node)
  └─ WiFi → Monitor node (separate SSID)

Benefit: Physical separation between process + company LAN segments.
Limitation: If retail-N gets compromised, attacker is on the same switch as process.
```

### 4.3 Firewall Rules (Per-Node iptables)

**Option 1: Retail-N firewall only (RECOMMENDED for v2.0)**

Install iptables rules on each retail-N node to restrict egress/ingress:

```bash
# /etc/rc.local or systemd service (installed by installer v2)

# Default deny all
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Loopback (always needed)
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# MQTT inbound from process node (10.0.0.11:1883)
iptables -A INPUT -s 10.0.0.11 -p tcp --dport 1883 -j ACCEPT
iptables -A INPUT -s 10.0.0.11 -p tcp --sport 1883 -j ACCEPT

# DNS (8.8.8.8 / 8.8.4.4) for Alpaca API resolution
iptables -A OUTPUT -d 8.8.8.8 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -d 8.8.4.4 -p udp --dport 53 -j ACCEPT

# HTTPS to Alpaca API (external IPs via DNS)
# Note: Allow all TCP 443 outbound (Alpaca + NTP)
iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT
iptables -A INPUT -p tcp --sport 443 -m state --state ESTABLISHED,RELATED -j ACCEPT

# NTP (time sync, critical for trading)
iptables -A OUTPUT -p udp --dport 123 -j ACCEPT
iptables -A INPUT -p udp --sport 123 -j ACCEPT

# Allow SSH inbound from company node (10.0.0.10, for operator debugging)
iptables -A INPUT -s 10.0.0.10 -p tcp --dport 22 -j ACCEPT
iptables -A OUTPUT -d 10.0.0.10 -p tcp --sport 22 -j ACCEPT

# DENY: retail → company (10.0.0.10) explicitly
iptables -A OUTPUT -d 10.0.0.10 -j DROP
iptables -A INPUT -s 10.0.0.10 -j DROP  # Company can't reach retail either

# DENY: retail → retail (prevent lateral movement)
iptables -A OUTPUT -d 10.0.0.0/25 ! -d 10.0.0.11 -j DROP
iptables -A INPUT -s 10.0.0.0/25 ! -s 10.0.0.11 -j DROP

# Log dropped packets (optional, for debugging)
iptables -A INPUT -j LOG --log-prefix "[DROPPED_IN] " --log-level 7
iptables -A OUTPUT -j LOG --log-prefix "[DROPPED_OUT] " --log-level 7
```

**Option 2: Process + Company firewall (future hardening, v2.1)**

Lock down process + company nodes to accept connections only from authorized sources (laptop, specific IP ranges).

**Option 3: Router-level ACLs (if your switch/modem supports it)**

Configure switch to filter traffic between ports (process port ≠ retail port). Requires managed switch.

### 4.4 Firewall Installation (Installer v2 Responsibility)

```python
# installers/common/firewall.py (NEW module)

def install_firewall_rules(node_type: str, home_dir: Path) -> None:
    """Install iptables firewall rules (retail-N nodes only)."""
    
    if node_type != "retail":
        return  # Only retail nodes run firewall
    
    rules_script = f"""#!/bin/bash
# Generated by installer v2 on {datetime.now().isoformat()}
# Firewall rules for retail-N node (restrict to MQTT + Alpaca API only)

# ... (rules from 4.3 above)

# Save rules persistently (survives reboot)
iptables-save > /etc/iptables/rules.v4
"""
    
    rules_path = Path(home_dir) / ".firewall_rules.sh"
    rules_path.write_text(rules_script)
    rules_path.chmod(0o755)
    
    # Execute
    subprocess.run([str(rules_path)], check=True)
    
    # Install iptables-persistent (auto-load on boot)
    subprocess.run(["apt-get", "install", "-y", "iptables-persistent"], check=True)
```

---

## 5. .env Network Configuration

### 5.1 Process Node `.env` (MQTT broker config)

```bash
# user/.env (process node)

# MQTT broker (running locally on this node)
MQTT_BROKER_HOST=10.0.0.11
MQTT_BROKER_PORT=1883
MQTT_BROKER_USER=  # empty (no auth on LAN)
MQTT_BROKER_PASSWORD=  # empty

# Topics
MQTT_TOPIC_PRICE=process/signals/price
MQTT_TOPIC_PENDING=process/signals/pending
MQTT_TOPIC_RESULTS=retail-{N}/trade/result

# Alpaca API (for price polling)
ALPACA_API_KEY=<redacted>
ALPACA_API_SECRET=<redacted>
ALPACA_API_BASE_URL=https://api.alpaca.markets

# ... (other keys)
```

### 5.2 Retail-N Node `.env` (MQTT client config)

```bash
# user/.env (retail-N node)

# MQTT broker (connect to process node)
MQTT_BROKER_HOST=10.0.0.11
MQTT_BROKER_PORT=1883
MQTT_BROKER_USER=  # empty
MQTT_BROKER_PASSWORD=  # empty

# Node identity
NODE_TYPE=retail
NODE_NUM=1
PROCESS_NODE_URL=http://10.0.0.11:5001  # for work-packet polling fallback
PROCESS_NODE_TOKEN=<redacted>

# Alpaca API (trade execution)
ALPACA_API_KEY=<redacted>
ALPACA_API_SECRET=<redacted>

# ... (other keys)
```

### 5.3 Company Node `.env` (MQTT monitor config)

```bash
# company/.env (company node)

# MQTT (optional, for monitoring only)
MQTT_BROKER_HOST=10.0.0.11
MQTT_BROKER_PORT=1883

# R2 (backup destination)
R2_ACCOUNT_ID=<redacted>
R2_ACCESS_KEY=<redacted>
R2_SECRET_KEY=<redacted>

# ... (other keys)
```

---

## 6. Operator Runbook

### 6.1 First-Time Setup Checklist

```
Pre-installation:
  [ ] All 3 Pis are online (ping 10.0.0.x after boot)
  [ ] SSH keys are in place (company pi has keys to all others)
  [ ] USB license stick is ready with credentials
  [ ] Cloudflare tunnel credentials are ready

Installation order:
  1. [ ] Install company node:   ./install.sh --node=company
  2. [ ] Install process node:   ./install.sh --node=process --restore=via-company
  3. [ ] Install retail-1:       ./install.sh --node=retail --node-num=1
  4. [ ] Install retail-2:       ./install.sh --node=retail --node-num=2

Post-installation verification:
  [ ] SSH to company:  ssh company (via Cloudflare)
  [ ] SSH to process:  ssh process (direct LAN)
  [ ] SSH to retail-1: ssh retail-1 (direct LAN)
  
  [ ] Check MQTT broker:
      mosquitto_sub -h 10.0.0.11 -p 1883 -t "process/heartbeat" -v
  
  [ ] Check firewall on retail-1:
      ssh retail-1 sudo iptables -L -n
  
  [ ] Verify process node heartbeat (via MQTT):
      Expected: message every 60 seconds on process/heartbeat
```

### 6.2 Troubleshooting

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Retail node can't reach MQTT broker | `telnet 10.0.0.11 1883` fails | Check firewall rules on retail; check mosquitto on process |
| Retail node can reach MQTT but won't start trade agents | Check `.env` MQTT_BROKER_HOST | Update retail-N.env, restart agent |
| Process node keeps losing MQTT connection | Check mosquitto logs: `journalctl -u mosquitto -f` | Restart mosquitto, check broker IP binding |
| Company can SSH to process but not retail | Firewall blocks company → retail | Expected (secure). Operator goes through process if needed. |
| Static IP reverts after reboot | dhcpcd.conf not persisted | Re-run `./install.sh --verify`, check /etc/dhcpcd.conf exists |

---

## 6.4 AS-BUILT Data Flows + HTTP Endpoints + SSH Chain (added 2026-05-04)

### 6.4.1 What actually flows over which channel

```
                  ┌─────────────────────────────────────┐
                  │  PROCESS NODE (Pi5, 10.0.0.11)      │
                  │                                     │
                  │  Mosquitto :1883  (MQTT telemetry)  │◄────────────┐
                  │  Dispatcher       (every 30s)       │             │ subscribes
                  │  Trader-server :8443 (loopback)     │             │ process/#
                  │  Master DBs                         │             │
                  └────┬───────────────────────┬────────┘             │
                       │                       │                      │
                       │ HTTP POST /work       │ MQTT QoS 0 (retain)  │
                       │ (work packets)        │ pub/sub:             │
                       │                       │  - process/prices/X  │
                       │                       │  - process/regime    │
                       │                       │  - process/heartbeat │
                       ▼                       ▼                      │
                  ┌─────────────────────┐                              │
                  │  RETAIL-N           │                              │
                  │  10.0.0.20+         │                              │
                  │                     │                              │
                  │  trader_server :8443│  publishes                   │
                  │  TRADER_DB_MODE=    │  process/heartbeat/retail-N/ │
                  │      packet         │       trader_server          │
                  │  (no DBs, no portal,│       (every 30s, LWT-on)    │
                  │   no signal agents) │                              │
                  └─────────────────────┘                              │
                                                                       │
                  ┌─────────────────────────────────────┐               │
                  │  COMPANY NODE (pi4b, 10.0.0.10)     │───────────────┘
                  │                                     │
                  │  synthos_monitor (admin portal)     │
                  │  company_mqtt_listener  (subscriber)│
                  │   - persists every observation to   │
                  │     auditor.db.mqtt_observations    │
                  └─────────────────────────────────────┘
```

### 6.4.2 HTTP Endpoints

| Endpoint | Service | Node | Port | Purpose |
|---|---|---|---|---|
| `POST /work` | synthos_trader_server | retail-N (loopback today) | 8443 | Receive WorkPacket, run trader, return WorkResult |
| `GET /healthz` | synthos_trader_server | retail-N | 8443 | Liveness probe |
| `GET /readyz` | synthos_trader_server | retail-N | 8443 | Readiness — reports `dispatch_mode` and `schema_version` |
| `GET /version` | synthos_trader_server | retail-N | 8443 | Service version + schema version |

All HTTP traffic is **LAN-only**. Cloudflare tunnel does NOT forward
port 8443. Auth is via the `X-Dispatch-Token` header — value must
match `DISPATCH_AUTH_TOKEN` env var on both process node (dispatcher)
and retail-N (trader_server).

### 6.4.3 Firewall rules (retail-N inbound)

When retail-N is on separate hardware (not loopback), the iptables
rules for that node need to allow HTTP 8443 from the process node ONLY:

```bash
# retail-N inbound
iptables -A INPUT -i lo -j ACCEPT                                              # localhost
iptables -A INPUT -p tcp --dport 22 -s 10.0.0.10 -j ACCEPT                     # SSH from company only
iptables -A INPUT -p tcp --dport 8443 -s 10.0.0.11 -j ACCEPT                   # HTTP /work from process only
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -j DROP                                                       # default deny

# retail-N outbound (must reach Alpaca, NTP, DNS, MQTT broker)
iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT                                # HTTPS (Alpaca)
iptables -A OUTPUT -p tcp --dport 1883 -d 10.0.0.11 -j ACCEPT                  # MQTT to process broker
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT                                 # DNS
iptables -A OUTPUT -p udp --dport 123 -j ACCEPT                                # NTP
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -j DROP                                                      # default deny
```

**Retail-N never reaches company directly** (no rule above permits
10.0.0.10 outbound). The auditor on company observes retail-N
indirectly via MQTT heartbeats subscribed from the process broker.

### 6.4.4 SSH access chain (operator workflow)

```
┌──────────────┐     Cloudflare     ┌──────────────┐
│ operator Mac │◄──────tunnel──────►│ company pi4b │
└──────────────┘   (ssh.synth-      │ 10.0.0.10    │
                    cloud.com)      └─────┬────────┘
                                          │ direct LAN SSH
                                          │ (~/.ssh/config alias:
                                          │  SentinelRetail)
                                          ▼
                                    ┌──────────────┐
                                    │ process pi5  │
                                    │ 10.0.0.11    │
                                    └─────┬────────┘
                                          │ direct LAN SSH
                                          │ (when retail-N exists)
                                          ▼
                                    ┌──────────────┐
                                    │ retail-N     │
                                    │ 10.0.0.20+   │
                                    └──────────────┘
```

**From the operator Mac:**

```bash
# To company node (via Cloudflare tunnel, single hop)
ssh pi4b                          # ProxyCommand: cloudflared access ssh

# To process node (one extra hop through company)
ssh pi4b 'ssh SentinelRetail "<command>"'

# To retail-N (two extra hops, when retail hardware exists)
ssh pi4b 'ssh SentinelRetail "ssh retail-1 <command>"'
```

This is the chain every ad-hoc operations command takes today
(verified live during the 2026-05-04 deploys). The patterns are baked
into the synthos_build/docs/CUTOVER_RUNBOOK.md commands.

### 6.4.5 Cloudflare tunnel scope

Cloudflare tunnel ONLY exposes:
- `ssh.synth-cloud.com` → pi4b SSH (port 22)
- `command.synth-cloud.com` → synthos_monitor admin portal (port 5050)

Cloudflare tunnel does NOT expose:
- ❌ MQTT broker (port 1883) — LAN-only
- ❌ Trader_server `/work` (port 8443) — LAN-only
- ❌ Process node SSH (operator must hop through pi4b)
- ❌ Retail-N anything

This is the security boundary: only the company node has any external
ingress, and only via authenticated tunnel.

---

## 7. Security Considerations

### 7.1 What This Design Protects

✅ Compromised retail-N node cannot reach company (10.0.0.10)  
✅ Compromised retail-N node cannot reach process (10.0.0.11) except MQTT  
✅ Only authenticated operators (Cloudflare SSO) can SSH to company  
✅ Price data and trade results flow through MQTT (logged, auditable)  
✅ Retail nodes are stateless (no customer data to steal)

### 7.2 What This Design Does NOT Protect

❌ Retail trader API keys (stored in retail-N.env) — if pi is physical compromised, keys are exposed  
❌ MQTT traffic is not encrypted (LAN-only assumption) — if someone plugs into the switch, they can sniff messages  
❌ Company/Process nodes run as `pi` user — privilege escalation possible if OS vulnerabilities exist

### 7.3 Future Hardening (v2.1+)

- Enable TLS on MQTT broker (mosquitto with certs)
- Rotate Alpaca API keys per retail node (limit blast radius)
- Run services in containers (Docker) with dropped capabilities
- Add SELinux or AppArmor policies per service
- Monitor MQTT traffic for anomalies

---

## 8. Integration with Installer v2

### 8.1 New Modules (installers/common/)

| Module | Responsibility | Called by |
|--------|-----------------|-----------|
| `network.py` (enhanced) | IP detection, static config, DHCP validation | `nodes/base.py` COLLECTING phase |
| `mqtt.py` | Mosquitto installation + config (process only) | `nodes/process.py` INSTALLING phase |
| `firewall.py` | iptables rules installation (retail only) | `nodes/retail.py` INSTALLING phase |

### 8.2 Installer CLI Integration

```bash
# Unchanged — installer handles network config during COLLECTING phase

./install.sh --node=company
  → COLLECTING: asks static IP vs DHCP, Cloudflare tunnel creds
  → INSTALLING: configures IP, deploys services (no MQTT broker)

./install.sh --node=process
  → COLLECTING: asks static IP vs DHCP
  → INSTALLING: configures IP, installs + starts mosquitto
  → VERIFYING: checks mosquitto is listening on 10.0.0.11:1883

./install.sh --node=retail --node-num=1
  → COLLECTING: asks static IP vs DHCP (default 10.0.0.20)
  → INSTALLING: configures IP, installs firewall rules
  → VERIFYING: checks firewall allows MQTT inbound from 10.0.0.11
```

---

## 9. Decisions Summary (for your review)

| Decision | Recommendation | Your Input |
|----------|-----------------|-----------|
| **MQTT Broker Location** | Embedded on process node (pi5) via `mosquitto` | ☐ Approve ☐ Defer to v2.1 ☐ Other: ___ |
| **MQTT QoS** | QoS 0 (price), QoS 1 (work packets, results) | ☐ Approve ☐ Adjust: ___ |
| **Retail Firewall** | iptables rules per retail node (Option 1) | ☐ Approve ☐ Router-level only (Option 3) ☐ Skip firewall |
| **IP Auto-detect** | Installer detects, prompts operator to confirm | ☐ Approve ☐ Always use defaults ☐ Always prompt |
| **TLS on MQTT** | Deferred to v2.1 (not critical for LAN-only) | ☐ Agree ☐ Implement in v2.0 |

---

## 10. Open Questions / Clarifications Needed

1. **Mosquitto authentication:** Should we add a username/password to mosquitto even for LAN-only (belt-and-suspenders)? Or trust the firewall?

2. **Retail node count:** Do you plan 3 retail nodes initially, or will you scale to 10+ later? Affects MQTT broker sizing.

3. **Alpaca API keys per node:** Should each retail node have its own Alpaca API account, or share a master account? (Current assumption: shared account, keys in .env)

4. **MQTT message retention:** Should the broker retain the last price/pending message, or require subscribers to be listening live? (Affects late-joining retail nodes.)

5. **Operator laptop WiFi:** Should your laptop be on the same WiFi as the monitor node, or separate? (Affects how you access monitor pi.)

---

## 11. Timeline

- **Week 1 (installer v2 Week 1):** Implement `mqtt.py`, `firewall.py`, `network.py` enhancements
- **Week 2 (installer v2 Week 2):** Integrate into node installers, test on Mac
- **Week 3 (installer v2 Week 3):** Manual Pi testing, Mosquitto broker validation
- **Week 4 (installer v2 Week 4):** Documentation, first-time setup dry-run

---

## References

- [INSTALLER_V2_SPEC.md](INSTALLER_V2_SPEC.md) — Full installer spec
- [BACKUP_MANIFEST_CONTRACT.md](BACKUP_MANIFEST_CONTRACT.md) — Backup schema
- [NETWORK_INFRASTRUCTURE_HANDOFF.md](NETWORK_INFRASTRUCTURE_HANDOFF.md) — Initial audit

---

**Next steps:** Review the decisions summary above. Flag any changes or add missing context, and I'll update this document accordingly.
