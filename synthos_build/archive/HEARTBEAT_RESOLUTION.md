# HEARTBEAT ARCHITECTURE RESOLUTION
## Decision Record — monitor_node vs. company_node Heartbeat Conflict

**Document Version:** 1.0
**Date:** 2026-03-27
**Status:** Active — supersedes all conflicting references
**Resolves:** SYNTHOS_TECHNICAL_ARCHITECTURE §2.7 (HEARTBEAT_URL), §3.2 (heartbeat_receiver.py port 5004)
**Audience:** All agents, engineers, Project Lead

---

## 1. OBJECT DEFINITION

### What a Heartbeat Receiver Is

A **heartbeat receiver** is a persistent HTTP endpoint that:
- Accepts POST requests from retail Pis carrying system status payloads
- Authenticates each POST via HMAC token validation
- Persists the received payload to a registry or database
- Triggers downstream behavior (alerts, liveness tracking, report generation)

A heartbeat receiver is NOT:
- A polling mechanism (the receiver is passive; the retail Pi initiates contact)
- A bidirectional channel (the receiver returns only HTTP 200/40x/50x)
- Part of the trading pipeline (heartbeat failure does not affect retail Pi operation)

There is exactly one authoritative heartbeat receiver in the Synthos system. It is defined below.

---

## 2. OWNERSHIP DECISION

**The heartbeat receiver is owned by: monitor_node**

**Decision rationale:**

| Factor | monitor_node | company_node |
|---|---|---|
| Purpose alignment | Dedicated to observability — this is its entire job | Business logic node — heartbeat is a distraction from its core function |
| Always-on requirement | Designed to run independently; not dependent on company agent lifecycle | Company agents are scheduled and task-based; a receiver service adds unrelated surface area |
| Isolation | Failure of monitor_node does not affect trading | A crashed receiver service on company_node would create noise in company agent logs |
| Implementation status | `synthos_monitor.py` exists and is active | `heartbeat_receiver.py` (port 5004) was never implemented — design artifact only |
| Env var consistency | `MONITOR_URL` used throughout retail installer; single env var, single target | `HEARTBEAT_URL` in SYNTHOS_TECHNICAL_ARCHITECTURE §2.7 is a conflicting legacy reference |

**This decision is final and non-negotiable.** No heartbeat receiver service is to be built on company_node.

---

## 3. FINAL ARCHITECTURE

### 3.1 Authoritative Receiver

```
Service:   synthos_monitor.py
Node:      monitor_node
Port:      5000 (configurable via PORT env var)
Endpoint:  POST /heartbeat
Auth:      HMAC via MONITOR_TOKEN
```

### 3.2 Retail Pi Configuration

Retail Pis reach the heartbeat receiver via:

```
Env var:   MONITOR_URL
Example:   MONITOR_URL=http://monitor-pi.local:5000
Token:     MONITOR_TOKEN=<hmac secret>
ID:        PI_ID=<unique identifier>
Label:     PI_LABEL=<human-readable name>
Email:     PI_EMAIL=<customer email>
```

`synthos_heartbeat.py` on the retail Pi:
1. Reads `system_log` to compose the payload (does not generate health data itself)
2. POSTs to `${MONITOR_URL}/heartbeat` with HMAC-signed payload
3. Logs success or failure locally; failure does not affect trading

### 3.3 Monitor Node Behavior

`synthos_monitor.py` on the monitor_node:
1. Receives POST at `/heartbeat`
2. Validates HMAC signature using `MONITOR_TOKEN`
3. Persists to `.monitor_registry.json` (updated after every heartbeat)
4. Triggers alert email via SendGrid if any threshold is breached
5. Serves monitor console (read-only) for Project Lead visibility

### 3.4 Company Pi Role

The company Pi has NO role in the heartbeat path. It does not:
- Receive heartbeats from retail Pis
- Forward or relay heartbeat data
- Run a heartbeat receiver service

The company Pi's `sentinel.py` reads heartbeat data indirectly via the monitor_node's reports or via the Scoop agent's report aggregation mechanism — it does not receive raw heartbeat POSTs.

---

## 4. DEPRECATIONS

### 4.1 heartbeat_receiver.py (port 5004) — DEPRECATED

| Field | Value |
|---|---|
| Referenced in | SYNTHOS_TECHNICAL_ARCHITECTURE §3.2 |
| Status | Deprecated — was never implemented |
| Node | company_node (was proposed) |
| Port | 5004 |
| Replacement | `synthos_monitor.py` on monitor_node at port 5000 |
| Action required | Remove all references in affected documents (see Section 5) |
| Build action | Do NOT build. Do NOT create this file. |

### 4.2 HEARTBEAT_URL env var — DEPRECATED

| Field | Value |
|---|---|
| Referenced in | SYNTHOS_TECHNICAL_ARCHITECTURE §2.7, example `.env` blocks |
| Status | Deprecated — superseded by MONITOR_URL |
| Replacement | `MONITOR_URL` (already canonical in SYSTEM_MANIFEST and retail installer) |
| Action required | Replace all occurrences in affected documents (see Section 5) |
| Runtime action | No existing retail Pis use HEARTBEAT_URL (never shipped); no migration needed |

### 4.3 HEARTBEAT_PORT=5004 — DEPRECATED

| Field | Value |
|---|---|
| Referenced in | SYNTHOS_TECHNICAL_ARCHITECTURE example `.env` block |
| Status | Deprecated — was part of the never-built company_node receiver |
| Replacement | PORT env var on monitor_node (defaults to 5000) |
| Action required | Remove from all `.env` examples in affected documents |

---

## 5. DOCUMENT UPDATE LIST

The following documents require corrections. Each entry specifies the document, section, current incorrect content, and the required replacement.

---

### Document 1: SYNTHOS_TECHNICAL_ARCHITECTURE.md

**Status:** Requires multiple corrections — highest priority

#### Section §2 (Architecture Diagram / Company Pi services)

**Find:**
```
│  • Heartbeat Receiver (5004)            │
```

**Replace with:**
```
│  (no heartbeat receiver — monitor_node owns this)       │
```

---

#### Section §2.7 (Optional Heartbeat)

**Find:**
```
POST /heartbeat
...
Sends to: Company Pi at `HEARTBEAT_URL` (env var, defaults to None for offline mode)
```

**Replace with:**
```
POST /heartbeat
...
Sends to: monitor_node at `MONITOR_URL` (env var; defaults to None for offline mode)
Auth: HMAC signed with MONITOR_TOKEN
```

**Also remove:** Any reference to `HEARTBEAT_URL` in this section. Replace all occurrences with `MONITOR_URL`.

---

#### Section §3.2 (Company Pi file tree — services)

**Find:**
```
│   ├── heartbeat_receiver.py      # Flask app (port 5004)
```

**Replace with:**
```
# heartbeat_receiver.py — REMOVED. Heartbeat is received by synthos_monitor.py
# on the monitor_node at port 5000. See HEARTBEAT_RESOLUTION.md.
```

---

#### Section §3 (Service: Heartbeat Receiver)

**Find:**
```
Service: Heartbeat Receiver (port 5004, internal only)
...
POST /heartbeat
...
Writes: heartbeats table
```

**Replace with:**
```
# DEPRECATED SECTION — heartbeat_receiver.py (port 5004) was never built.
# The heartbeat receiver is synthos_monitor.py on the monitor_node at port 5000.
# See HEARTBEAT_RESOLUTION.md for the full decision record.
```

---

#### Example .env block (retail Pi)

**Find:**
```
HEARTBEAT_URL=http://admin-pi-4b.local:5004
HEARTBEAT_TOKEN=abc123def456xyz
```

**Replace with:**
```
MONITOR_URL=http://monitor-pi.local:5000
MONITOR_TOKEN=abc123def456xyz
```

---

#### Example .env block (company Pi / system)

**Find:**
```
HEARTBEAT_PORT=5004
```

**Remove this line entirely.** It has no replacement — the monitor_node port is set via `PORT` on the monitor_node itself, not on the company Pi.

---

### Document 2: SYSTEM_MANIFEST.md

**Status:** Already updated via MANIFEST_PATCH.md

MANIFEST_PATCH.md §PATCH 6 adds the deprecation note to `monitor_node:` ports block and §PATCH 4 marks `services/heartbeat_receiver.py` as deprecated in FILE_STATUS.

**Remaining action:** Confirm that MANIFEST_PATCH.md has been applied. No further edits required.

---

### Document 3: SYNTHOS_OPERATIONS_SPEC.md

**Status:** Requires review — no heartbeat port references found; may use generic language

**Check for:** Any reference to "Company Pi's heartbeat receiver" or "heartbeat endpoint" that implies company_node ownership.

**If found:** Replace with "monitor_node heartbeat receiver (synthos_monitor.py, port 5000)."

**Specific reference in SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md:**

> "the Company Pi's heartbeat receiver and validation endpoints"

**Replace with:**
> "the monitor_node's heartbeat receiver (synthos_monitor.py, port 5000) and company Pi validation endpoints"

---

### Document 4: SYNTHOS_TECHNICAL_ARCHITECTURE.md — §2.7 heartbeat.py call chain

**Find:**
```
1. Heartbeat.py on Retail Pi runs hourly
```

**Confirm this reads correctly:** synthos_heartbeat.py POSTs to MONITOR_URL (monitor_node:5000), not HEARTBEAT_URL. Update if it still references company Pi as the destination.

---

### Document 5: INSTALLER_STATE_MACHINE.md

**Status:** No heartbeat port references found — no changes required.

---

### Document 6: TOOL_DEPENDENCY_ARCHITECTURE.md

**Status:** Correct — already uses MONITOR_URL and references synthos_monitor.py correctly. No changes required.

---

## 6. CONFLICT RESOLUTION RECORD

This section documents the exact source of the conflict and why it arose.

**Origin:** SYNTHOS_TECHNICAL_ARCHITECTURE.md was written early in the project when the architecture included a company_node heartbeat receiver (port 5004) as the retail Pi phone-home target. This was the v1 design.

**What changed:** The monitor_node concept was introduced as a dedicated observability node. `synthos_monitor.py` was built on the monitor_node to handle all observability functions including heartbeat reception. The retail Pi installer was updated to use `MONITOR_URL` pointing to the monitor_node.

**What was not updated:** SYNTHOS_TECHNICAL_ARCHITECTURE.md retained the old `HEARTBEAT_URL` and `heartbeat_receiver.py` (port 5004) references. These became dead references — the file was never built, the env var was never used in the canonical installer.

**Resolution:** This document is the authoritative decision record. Any document that contradicts this resolution is incorrect and must be updated per Section 5.

---

**Document Version:** 1.0
**Status:** Active
**Next action:** Apply Section 5 corrections to SYNTHOS_TECHNICAL_ARCHITECTURE.md and SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md
