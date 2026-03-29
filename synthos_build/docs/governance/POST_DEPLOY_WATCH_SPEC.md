# POST_DEPLOY_WATCH.JSON — FULL SYSTEM SPECIFICATION
## Object Semantics, Lifecycle, Authority, Mutation, Validation, and Schema

**Document Version:** 1.0
**Date:** 2026-03-27
**Status:** Active — governing contract for Blueprint, Patches, and Watchdog
**Node:** company_node (written and evaluated here); retail_node Watchdog reads it remotely
**Supersedes:** Partial reference in SYSTEM_MANIFEST §11 Rollback

---

## 1. OBJECT DEFINITION

### What a Deployment Watch Is

A **deployment watch** is a monitoring record that governs whether a specific deployment is declared stable or triggers a rollback. It is created by Blueprint immediately after a Friday deployment and defines:
- The observation window (start and end timestamps)
- The thresholds that, if breached, require rollback
- The monitoring events observed during the window
- The final outcome (stable or rolled back)

A deployment watch is not a log. It is a decision-support contract. Its presence signals to all agents and to Watchdog on the retail Pi(s) that a recent deployment is under evaluation and that rollback may be triggered. Its absence signals no active monitoring.

A deployment watch is **not** a continuous monitoring system. It covers the 24–72 hour post-deployment window only. Ongoing system monitoring is Patches' responsibility independent of this file.

### File Structure

| Property | Value |
|---|---|
| File type | Single JSON file; array of watch objects |
| Node | company_node |
| Canonical path | `${SYNTHOS_HOME}/data/post_deploy_watch.json` |
| Active watches | Maximum 1 active watch at any time (status = `active`) |
| Multiple entries | Yes — old closed/stable watches are retained as history |
| Ownership | Blueprint creates; Patches updates; Project Lead may override |
| Retail Pi access | Watchdog on retail Pi reads this file (read-only) via the company Pi's monitoring interface or shared filesystem — implementation method defined in DEPLOYMENT_INTERFACE_SPEC |
| Retention | Closed watches are retained for 90 days, then archived |

### Why a File, Not a Database Table

This file must be readable by Watchdog on the retail Pi, which may not have direct access to company.db. A JSON file can be served via a simple read endpoint or copied via sync, making it accessible without database credentials. The file is small and deterministic in structure.

---

## 2. LIFECYCLE MODEL

### State Definitions

| State | Meaning | Terminal |
|---|---|---|
| `initialized` | Blueprint created the watch record; monitoring not yet started | No |
| `active` | Patches is monitoring; thresholds enforced; rollback possible | No |
| `stable` | Window closed without threshold breach; deployment confirmed good | Yes* |
| `rollback_triggered` | At least one threshold was breached; rollback in progress | No |
| `closed` | Final terminal state; outcome recorded | Yes |

*`stable` is functionally terminal — once stable, the watch is immediately transitioned to `closed` with `outcome: "stable"`. `stable` is an intermediate state held only during the brief period between declaring stability and writing the closed record.

### Entry Conditions

**`initialized`**
Blueprint creates the record after the deployment commit is confirmed. Required fields (see Validation) must all be present. The watch is not yet being monitored — no events are expected. Status is `initialized`, not `active`. Blueprint must create the watch entry before the deployment is recorded in `suggestions.json` (i.e., `post_deploy_watch_id` in the suggestion references a watch that exists).

**`active`**
Patches transitions `initialized → active` by writing `activated_at`. This transition must occur within 2 hours of `deployment_date`. If Patches fails to activate within 2 hours, the watch remains `initialized` and Patches logs an error. The Project Lead is notified via the morning report.

**`stable`**
Patches transitions `active → stable` when `window_end` timestamp has passed AND no monitoring event with `severity: "rollback"` exists in `monitoring_events`. The stable declaration is written atomically with `stable_at`.

**`rollback_triggered`**
Patches (or Project Lead) transitions `active → rollback_triggered` when any threshold is breached. In post-trading mode, Patches may do this autonomously. In pre-trading mode, Patches flags the breach and the Project Lead confirms. `triggered_at`, `triggered_by`, and `rollback_reason` are all written atomically. The rollback_reason must name the specific threshold breached and the observed value.

**`closed`**
Final state. Written by Patches after either:
- The watch is `stable` (outcome: `"stable"`)
- Rollback has been executed and confirmed (outcome: `"rolled_back"`)
- Project Lead manually closes it (outcome: `"manually_closed"`)

`closed_at`, `closed_by`, and `outcome` are required.

### Valid State Transitions

```
initialized       → active              [Patches, within 2h of deployment_date]
active            → stable              [Patches, when window_end passed and no rollback events]
active            → rollback_triggered  [Patches (post-trading: autonomous; pre-trading: after Project Lead confirm) or Project Lead]
stable            → closed              [Patches (immediate, automated)]
rollback_triggered → closed             [Patches (after rollback executed) or Project Lead]
active            → closed              [Project Lead (manual close only)]
```

### Invalid Transitions (FORBIDDEN)

```
initialized  → stable              FORBIDDEN (must activate first)
initialized  → rollback_triggered  FORBIDDEN (must activate first)
initialized  → closed              FORBIDDEN (except Project Lead manual cancel before activation)
stable       → rollback_triggered  FORBIDDEN (stable is committed; cannot retroactively trigger rollback)
closed       → any state           FORBIDDEN (terminal)
```

### Terminal States

`closed` is the only terminal state. All watch records must eventually reach `closed`. A watch that remains in `active` past `window_end + 4 hours` without Patches closing it is a monitoring failure — Patches logs an error and the morning report escalates it as CRITICAL.

---

## 3. AUTHORITY MODEL

### Who Owns Each Action

| Action | Authorized Agent(s) | Notes |
|---|---|---|
| Create watch (initialized) | Blueprint only | Blueprint creates before deployment confirmed |
| Activate watch (→ active) | Patches only | Within 2h of deployment_date |
| Append monitoring_events | Patches only | Append-only; no other agent writes events |
| Declare stable (→ stable → closed) | Patches only | Automated based on window_end + threshold state |
| Trigger rollback (→ rollback_triggered) | Patches (post-trading) or Project Lead (pre-trading) | Patches flags, Project Lead confirms in pre-trading mode |
| Close after rollback (→ closed) | Patches (after executing rollback) or Project Lead | |
| Force close (→ closed, manually_closed) | Project Lead only | Override for any unexpected situation |
| Read for rollback decision | Watchdog (retail Pi) | Read-only. Does not write to this file. |

### Watchdog's Role (Retail Pi)

Watchdog reads `post_deploy_watch.json` to determine if it should initiate a local rollback on the retail Pi. Watchdog's behavior:
- If status is `rollback_triggered`: Watchdog initiates rollback to `.known_good/` snapshot on the retail Pi
- If status is `active` or `initialized`: Watchdog remains in heightened monitoring mode (shorter restart window)
- If status is `stable` or `closed` with outcome `"stable"`: Watchdog returns to normal monitoring mode
- Watchdog does NOT write to this file under any condition

Watchdog's access to this file is via a read endpoint exposed by the company Pi's monitoring service. The exact transport is defined in TOOL_DEPENDENCY_ARCHITECTURE.

### Pre-Trading vs Post-Trading Rollback Authority

| Mode | Who can trigger rollback | Notes |
|---|---|---|
| `pre-trading` | Project Lead (manual) or Patches (with Project Lead confirmation) | Patches flags the breach in audit, does not autonomously execute |
| `post-trading` | Patches (autonomous) | Trust and money are at stake; speed matters |

---

## 4. MUTATION RULES

### Fields Blueprint Writes (at initialization)

All of the following are set at creation and are **IMMUTABLE** after initialization:

- `watch_id`
- `suggestion_ids`
- `deployed_commit`
- `deployment_date`
- `window_end`
- `thresholds` (all threshold values)
- `created_at`
- `created_by` (always `"blueprint"`)

The immutability of `thresholds` after initialization is non-negotiable. If a threshold value is wrong, the watch must be manually closed and a new one created for the remainder of the window. This forces explicit human acknowledgment of any threshold change, rather than allowing silent adjustment mid-watch.

### Fields Patches Writes (during active monitoring)

- `activated_at` (written once at activation; then immutable)
- `monitoring_events` (append-only)
- `stable_at` (written once; immutable after)
- `triggered_at`, `triggered_by`, `rollback_reason` (written once at rollback trigger; immutable after)
- `closed_at`, `closed_by`, `outcome` (written once at close; immutable after)

### Append-Only Fields

**`monitoring_events`** is an append-only array. Each event records what Patches observed and whether it was within or outside thresholds. Rules:
- Events are appended by Patches at each monitoring check
- No event may be modified or deleted after writing
- An event with `severity: "rollback"` means the associated threshold was breached and rollback must be evaluated

### Concurrent Write Rules

Only one agent (Patches) writes to this file during active monitoring. Blueprint's writes happen only at initialization, before Patches activates. Project Lead writes are explicit overrides. No two agents write simultaneously. File-level write lock via db_helpers.py slot system applies.

### What Happens on Rollback

When rollback is triggered:
1. Patches writes `rollback_triggered` state to the watch file
2. Watchdog on retail Pi reads the state and initiates local `.known_good/` restoration
3. After restoration is confirmed (via Watchdog's log or Patches' observation of next heartbeat), Patches writes `closed` with `outcome: "rolled_back"`
4. Patches also transitions the linked suggestion(s) in `suggestions.json` from `deployed → in_progress` to indicate they were un-deployed

---

## 5. VALIDATION RULES

### Required Fields at `initialized`

- `watch_id`: string, UUID v4, globally unique in file
- `suggestion_ids`: non-empty array of UUID v4 strings; each must reference a suggestion in `suggestions.json` with status `deployed`
- `deployed_commit`: string, exactly 40 lowercase hex characters
- `deployment_date`: string, ISO 8601 UTC
- `window_end`: string, ISO 8601 UTC; must be >= `deployment_date + 24 hours` and <= `deployment_date + 72 hours`
- `created_at`: string, ISO 8601 UTC; must equal `deployment_date` (within ±60 seconds)
- `created_by`: exactly `"blueprint"`
- `status`: exactly `"initialized"`
- `thresholds`: object, all sub-fields required (see below)
- `monitoring_events`: empty array `[]`

### Threshold Validation

All threshold fields are required:

```
thresholds.crash_rate_max:          float, range 0.0–1.0 (inclusive)
thresholds.error_rate_max:          float, range 0.0–1.0 (inclusive)
thresholds.agent_silence_max_hours: integer, range 1–48 (inclusive)
thresholds.api_failure_rate_max:    float, range 0.0–1.0 (inclusive)
thresholds.portfolio_variance_max_pct: float, range 0.0–100.0 (only enforced in post-trading mode; required field regardless)
```

- All values must be explicitly set. Null is not permitted.
- `crash_rate_max` of 0.0 means zero crashes are tolerated (strict mode).
- `agent_silence_max_hours` of 1 is the minimum; shorter windows are not meaningful.

### Required Fields at `active`

All initialization fields (unchanged) plus:
- `activated_at`: ISO 8601 UTC, must be within 2 hours of `deployment_date`

### Required Fields for `rollback_triggered`

- `triggered_at`: ISO 8601 UTC
- `triggered_by`: string, valid agent name or `"project_lead"`
- `rollback_reason`: string, non-empty, must include name of breached threshold and observed value (e.g., "crash_rate 0.4 exceeded max 0.1 — 2 crashes in 5 runs")

### Required Fields at `closed`

- `closed_at`: ISO 8601 UTC
- `closed_by`: string, valid agent name or `"project_lead"`
- `outcome`: enum: `"stable"`, `"rolled_back"`, `"manually_closed"`

If `outcome = "rolled_back"`: `triggered_at`, `triggered_by`, `rollback_reason` must all be present.
If `outcome = "stable"`: `stable_at` must be present.

### Time Window Constraints

- `window_end` >= `deployment_date` + 24 hours (minimum watch duration)
- `window_end` <= `deployment_date` + 72 hours (maximum watch duration)
- `activated_at` <= `deployment_date` + 2 hours
- `stable_at` >= `window_end` (stability cannot be declared before window closes)
- `triggered_at` must be within the window: >= `activated_at` AND <= `window_end` + 4 hours (4-hour grace period for post-window observation)

### Uniqueness Rules

- `watch_id` must be globally unique in the file
- Only one watch with `status = "active"` or `status = "initialized"` may exist at any time. If Blueprint attempts to create a new watch while one is active or initialized, the operation is blocked. Blueprint must wait until the active watch reaches `closed`.

---

## 6. SCHEMA

### Full JSON Structure

```json
[
  {
    "watch_id": "string (UUID v4)",
    "status": "initialized | active | stable | rollback_triggered | closed",
    "suggestion_ids": ["string (UUID v4)"],
    "deployed_commit": "string (40-char hex SHA)",
    "deployment_date": "ISO 8601 UTC",
    "window_end": "ISO 8601 UTC",

    "created_at": "ISO 8601 UTC",
    "created_by": "blueprint",

    "activated_at": "ISO 8601 UTC | null",

    "thresholds": {
      "crash_rate_max": "float (0.0–1.0)",
      "error_rate_max": "float (0.0–1.0)",
      "agent_silence_max_hours": "integer (1–48)",
      "api_failure_rate_max": "float (0.0–1.0)",
      "portfolio_variance_max_pct": "float (0.0–100.0)"
    },

    "monitoring_events": [
      {
        "timestamp": "ISO 8601 UTC",
        "observed_by": "patches",
        "metric": "string (crash_rate | error_rate | agent_silence | api_failure_rate | portfolio_variance | manual)",
        "observed_value": "number | string",
        "threshold_value": "number | null",
        "within_threshold": "boolean",
        "severity": "info | warning | rollback",
        "note": "string"
      }
    ],

    "stable_at": "ISO 8601 UTC | null",

    "triggered_at": "ISO 8601 UTC | null",
    "triggered_by": "string | null",
    "rollback_reason": "string | null",

    "closed_at": "ISO 8601 UTC | null",
    "closed_by": "string | null",
    "outcome": "stable | rolled_back | manually_closed | null"
  }
]
```

---

### Fully Populated Example: Active Monitoring with Threshold Breach

This example shows a watch that starts normal, has a warning event, then triggers rollback.

```json
[
  {
    "watch_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
    "status": "rollback_triggered",
    "suggestion_ids": [
      "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    ],
    "deployed_commit": "a3f9b2c1d4e5f6789012345678901234abcdef01",
    "deployment_date": "2026-04-04T21:00:00Z",
    "window_end": "2026-04-05T21:00:00Z",

    "created_at": "2026-04-04T21:00:00Z",
    "created_by": "blueprint",

    "activated_at": "2026-04-04T21:10:00Z",

    "thresholds": {
      "crash_rate_max": 0.1,
      "error_rate_max": 0.2,
      "agent_silence_max_hours": 4,
      "api_failure_rate_max": 0.3,
      "portfolio_variance_max_pct": 5.0
    },

    "monitoring_events": [
      {
        "timestamp": "2026-04-05T03:00:00Z",
        "observed_by": "patches",
        "metric": "crash_rate",
        "observed_value": 0.0,
        "threshold_value": 0.1,
        "within_threshold": true,
        "severity": "info",
        "note": "Morning check (6h post-deploy). 0 crashes in 3 agent runs. All agents reporting. API healthy."
      },
      {
        "timestamp": "2026-04-05T09:30:00Z",
        "observed_by": "patches",
        "metric": "crash_rate",
        "observed_value": 0.15,
        "threshold_value": 0.1,
        "within_threshold": false,
        "severity": "warning",
        "note": "1 crash detected in agent2_research.py at 09:15:00Z. Crash rate now 0.15 (1/7 runs). Approaching threshold. Continuing to monitor. Crash log shows UnboundLocalError in disclosure_fetcher() — possibly related to Friday's change."
      },
      {
        "timestamp": "2026-04-05T12:00:00Z",
        "observed_by": "patches",
        "metric": "crash_rate",
        "observed_value": 0.40,
        "threshold_value": 0.1,
        "within_threshold": false,
        "severity": "rollback",
        "note": "Crash rate now 0.40 (4/10 runs). Two additional crashes at 10:22:00Z and 11:47:00Z, both in agent2_research.py. Same UnboundLocalError. Threshold 0.1 exceeded by factor of 4. Rollback required. Flagging triggered_at."
      }
    ],

    "stable_at": null,

    "triggered_at": "2026-04-05T12:00:00Z",
    "triggered_by": "patches",
    "rollback_reason": "crash_rate 0.40 exceeded threshold 0.10. 4 crashes in 10 runs of agent2_research.py, all UnboundLocalError in disclosure_fetcher(). Consistent with regression introduced in commit a3f9b2c1. Rollback to .known_good/ initiated.",

    "closed_at": null,
    "closed_by": null,
    "outcome": null
  }
]
```

---

**Document Version:** 1.0
**Status:** Active
**Owner:** Blueprint (creates), Patches (monitors and closes), Project Lead (override authority)
