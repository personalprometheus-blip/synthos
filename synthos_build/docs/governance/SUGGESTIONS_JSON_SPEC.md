# SUGGESTIONS.JSON — FULL SYSTEM SPECIFICATION
## Object Semantics, Lifecycle, Authority, Mutation, Validation, and Schema

**Document Version:** 1.0
**Date:** 2026-03-27
**Status:** Active — governing contract for all agents
**Node:** company_node
**Supersedes:** Partial references in TOOL_DEPENDENCY_ARCHITECTURE and SYNTHOS_TECHNICAL_ARCHITECTURE

---

## 1. OBJECT DEFINITION

### What a Suggestion Is

A **suggestion** is a formally proposed change to the Synthos system. It is a versioned, auditable record that carries a change from initial observation through approval, implementation, deployment, and validation. A suggestion is not a task, note, or log entry. It is a system contract object: once created, every field has defined mutation rules, every state transition has an authorized actor, and every terminal state is permanent.

Suggestions are the exclusive mechanism by which system improvements are proposed, approved, and tracked. No agent may implement a change to any production file without a suggestion in `approved` or later state.

### File Characteristics

| Property | Value |
|---|---|
| File type | Single global JSON file containing an array of suggestion objects |
| Mutation model | Hybrid: identity fields are immutable; lifecycle fields are mutable; `audit_log` is append-only |
| Partitioned | No — single file, all suggestions, all agents |
| Node | company_node only |
| Canonical path | `${SYNTHOS_HOME}/data/suggestions.json` |
| File ownership | system-level; written only via db_helpers.py slot system — no agent writes directly |
| Initial state | Empty array `[]` on first boot (seeded by `seed_backlog.py` if initial backlog is provided) |

### Why Single File

A single file ensures that Blueprint, Patches, and the project lead always read from one authoritative source. Partitioned-by-agent files would allow conflicting views of what is approved or in-progress. The file's size is bounded: completed and superseded suggestions are archived to `${SYNTHOS_HOME}/data/suggestions_archive.json` by Patches on a weekly basis, keeping the active file manageable.

---

## 2. LIFECYCLE MODEL

### State Definitions

| State | Meaning | Terminal |
|---|---|---|
| `proposed` | Submitted, not yet reviewed | No |
| `under_review` | Patches is actively evaluating | No |
| `approved` | Authorized for implementation | No |
| `rejected` | Explicitly refused with documented reason | Yes* |
| `blocked` | Cannot proceed; dependency or conflict prevents movement | No |
| `in_progress` | Blueprint is actively implementing | No |
| `staged` | Implementation complete; files in `.blueprint_staging/`; awaiting deploy | No |
| `deployed` | Merged to main; pushed to Pi(s); post-deploy watch active | No |
| `completed` | Deployed AND post-deploy validation passed | Yes |
| `superseded` | Replaced by a newer suggestion | Yes |

*`rejected` is terminal by default. The Project Lead may manually re-open a rejected suggestion to `proposed` via the command portal. No agent may do this.

### Entry Conditions

**proposed**
All required creation fields present. `status` set to `proposed`. `created_at` and `created_by` set. `audit_log` contains exactly one entry (the creation event). Suggestion ID is UUID v4 and globally unique in the file.

**under_review**
Patches has read the suggestion and written `reviewed_by` and `review_started_at`. Transition is explicit — Patches does not auto-review at read time.

**approved**
`approved_by` and `approved_at` are set. For scope `company_internal`: Patches may approve. For scope `retail` or `system_wide`: Project Lead must approve. No other agent may write this state.

**rejected**
`rejected_by`, `rejected_at`, and `rejection_reason` are all set. Rejection reason must be non-empty and specific. Vague reasons (e.g., "not ready") are invalid.

**blocked**
`blocked_by`, `blocked_at`, and `blocked_reason` are all set. A blocked suggestion retains its `approved_by` if it was previously approved — the approval is not revoked.

**in_progress**
`assigned_to` (must be `"blueprint"`) and `started_at` are set. Blueprint may only enter `in_progress` if no other suggestion is `in_progress` targeting an overlapping file in `target_files`. File-level mutual exclusion is enforced at transition time.

**staged**
`staged_at` is set. `staging_manifest` object is populated (see schema). Blueprint verifies atomic deploy completion before writing this state.

**deployed**
`deployed_at` is set. `deploy_commit` (40-char hex git SHA) is set. `post_deploy_watch_id` is set and links to an active entry in `post_deploy_watch.json`. Project Lead is the sole authority to trigger this transition.

**completed**
`completed_at` is set. `validated_by` must be `"patches"`. Post-deploy watch for this suggestion has closed with outcome `"stable"`.

**superseded**
`superseded_by` is set (the suggestion_id of the replacing suggestion). `superseded_at` is set. The replacing suggestion must exist in the file.

### Valid State Transitions

```
proposed        → under_review       [Patches]
proposed        → approved           [Project Lead only]
proposed        → rejected           [Project Lead or Patches]
proposed        → superseded         [Patches or Project Lead]

under_review    → approved           [Patches (company_internal scope only) or Project Lead]
under_review    → rejected           [Patches or Project Lead]
under_review    → blocked            [Patches]
under_review    → superseded         [Patches or Project Lead]

approved        → in_progress        [Blueprint]
approved        → blocked            [Patches or Blueprint]
approved        → rejected           [Project Lead only]
approved        → superseded         [Project Lead only]

blocked         → under_review       [Patches (blocker resolved)]
blocked         → approved           [Project Lead override]
blocked         → rejected           [Project Lead only]

in_progress     → staged             [Blueprint]
in_progress     → blocked            [Blueprint (implementation hit dependency)]
in_progress     → approved           [Blueprint (abandoning attempt; resets for re-attempt)]

staged          → deployed           [Project Lead only — sole authority]
staged          → in_progress        [Patches (revision required before deploy)]
staged          → rejected           [Project Lead only]

deployed        → completed          [Patches (post-deploy watch closed stable)]
deployed        → in_progress        [Patches (rollback triggered — un-deploys suggestion)]

completed       → [TERMINAL]
superseded      → [TERMINAL]
rejected        → proposed           [Project Lead only — manual re-open via command portal]
```

### Explicitly Invalid Transitions (FORBIDDEN)

The following transitions are structurally prohibited. Any agent that attempts one must log an error and abort without modifying the file:

```
proposed     → deployed       FORBIDDEN (no approval)
proposed     → in_progress    FORBIDDEN (no approval)
proposed     → completed      FORBIDDEN (no approval)
proposed     → staged         FORBIDDEN (no approval)

in_progress  → deployed       FORBIDDEN (must stage first)
in_progress  → completed      FORBIDDEN (must stage and deploy first)

staged       → completed      FORBIDDEN (must deploy first)

completed    → any state      FORBIDDEN (terminal — immutable)
superseded   → any state      FORBIDDEN (terminal — immutable)

Any state    → proposed       FORBIDDEN for all agents except Project Lead
```

---

## 3. AUTHORITY MODEL

### Role Assignments

| Agent | Propose | Approve (company_internal) | Approve (retail/system_wide) | Reject | Block | Implement | Stage | Deploy |
|---|---|---|---|---|---|---|---|---|
| Patches | Yes | Yes | No | Yes | Yes | No | No | No |
| Blueprint | Yes | No | No | No | Yes (self only) | Yes (sole) | Yes (sole) | No |
| Librarian | Yes | No | No | No | Yes (security risk) | No | No | No |
| Sentinel | Yes | No | No | No | No | No | No | No |
| Fidget | Yes | No | No | No | No | No | No | No |
| Vault | Yes | No | No | No | No | No | No | No |
| Scoop | Yes | No | No | No | No | No | No | No |
| Timekeeper | Yes | No | No | No | Yes (scheduling conflict) | No | No | No |
| Project Lead | Yes | Yes | Yes | Yes | Yes | No | No | Yes (sole) |

### Expanded Authority Descriptions

**Patches**
Patches is the operational gatekeeper for the suggestion pipeline. It reads all suggestions continuously, moves proposals through `under_review`, approves or rejects company-internal changes, flags blockers, and marks completed after post-deploy validation. Patches has the highest non-human authority in the pipeline. However, Patches may not approve retail-scope or system-wide changes — these always require the Project Lead.

In post-trading mode, Patches may autonomously trigger `deployed → in_progress` (rollback) when the linked `post_deploy_watch.json` enters `rollback_triggered` state. In pre-trading mode, this requires Project Lead confirmation.

**Blueprint (engineer.py)**
Blueprint is the sole implementation agent. It may self-assign suggestions by transitioning `approved → in_progress`. It may NOT approve, reject, or deploy its own work. Blueprint's authority ends at `staged`. It may abandon an implementation attempt by transitioning `in_progress → approved` (resetting for a future attempt). When a file-level mutex conflict exists, Blueprint must not begin — it logs the conflict and waits.

**Librarian**
Librarian monitors dependencies and security. It may block any suggestion (regardless of state below `in_progress`) if it detects a security or dependency risk. Librarian documents the specific vulnerability or conflict in `blocked_reason`. Librarian cannot unblock — only Patches or Project Lead may unblock.

**Timekeeper**
Timekeeper may block `approved` suggestions when a scheduling conflict would make implementation impossible without database deadlock or slot starvation. Timekeeper must specify in `blocked_reason` what resource is contested and when it would become available.

**Sentinel, Fidget, Vault, Scoop**
These agents are proposal-only. They identify issues within their domains (observability, cost, compliance, communications) and submit suggestions. They do not participate in the approval or implementation pipeline.

**Project Lead (human)**
The Project Lead has override authority on every transition except terminal states. Terminal states (`completed`, `superseded`) may only be overridden via explicit manual action in the command portal with a documented reason. The Project Lead is the sole authority to trigger deployment (`staged → deployed`). This is the only human-gated step in the pipeline and it is non-negotiable.

---

## 4. MUTATION RULES

### Immutable Fields (set at `proposed`, never changed)

These fields are sealed at creation. No agent, including the Project Lead, may overwrite them:

- `suggestion_id`
- `created_by`
- `created_at`
- `category`
- `title`
- `target_files`
- `scope`

If an error was made in an immutable field, the correct action is to create a new suggestion and supersede the incorrect one. The erroneous suggestion is not edited.

### Mutable Fields (change per lifecycle state)

| Field | Who Writes It | When |
|---|---|---|
| `status` | Authorized agent per transition table | At each transition |
| `priority` | Patches or Project Lead | Under review or at proposal |
| `assigned_to` | Blueprint | When entering in_progress |
| `started_at` | Blueprint | When entering in_progress |
| `reviewed_by` | Patches | When entering under_review |
| `review_started_at` | Patches | When entering under_review |
| `approved_by` | Patches or Project Lead | When entering approved |
| `approved_at` | Patches or Project Lead | When entering approved |
| `rejected_by` | Patches or Project Lead | When entering rejected |
| `rejected_at` | Patches or Project Lead | When entering rejected |
| `rejection_reason` | Patches or Project Lead | When entering rejected |
| `blocked_by` | Patches, Blueprint, Librarian, Timekeeper | When entering blocked |
| `blocked_at` | Same as blocked_by | When entering blocked |
| `blocked_reason` | Same as blocked_by | When entering blocked |
| `staged_at` | Blueprint | When entering staged |
| `staging_manifest` | Blueprint | When entering staged |
| `deployed_at` | System (via Project Lead deploy action) | When entering deployed |
| `deploy_commit` | System (via Project Lead deploy action) | When entering deployed |
| `post_deploy_watch_id` | Blueprint (created before deploy) | When entering deployed |
| `completed_at` | Patches | When entering completed |
| `validated_by` | Patches | When entering completed |
| `superseded_by` | Patches or Project Lead | When entering superseded |
| `superseded_at` | Patches or Project Lead | When entering superseded |

### Append-Only Fields

**`audit_log`** is an array of event objects. It is the authoritative history of every state transition and significant action. Rules:
- Every state transition appends one entry minimum
- Entries must have: `timestamp` (ISO 8601 UTC), `agent` (writer), `action` (transition or action label), `note` (human-readable context)
- No entry may be modified after writing
- No entry may be deleted under any condition
- The first entry is always the creation event

### Conflict Resolution

**Patches vs Blueprint disagreement on staged revision:**
If Patches writes `staged → in_progress` and Blueprint disagrees with the rejection, Blueprint must:
1. Append an objection to `audit_log` with `action: "implementation_objection"` and full reasoning
2. NOT override Patches' transition
3. The disagreement must be resolved by the Project Lead via command portal

**Simultaneous writes:**
All writes are serialized via the db_helpers.py slot system. The slot system guarantees no two agents hold write access simultaneously. Read-modify-write is atomic from the perspective of the file state. If a write attempt times out waiting for a slot, the agent logs the failure and retries once at next scheduled cycle.

**Agents cannot overwrite each other's field values:**
- Blueprint cannot overwrite `rejection_reason` set by Patches
- Patches cannot overwrite `staging_manifest` set by Blueprint
- Any agent that attempts to overwrite a field it did not originally set must log an error and abort

---

## 5. VALIDATION RULES

### Required Fields by State

**At `proposed` (all must be present):**
- `suggestion_id`: string, UUID v4 format, globally unique in file
- `status`: exactly `"proposed"`
- `created_by`: string, must be a known agent name or `"project_lead"`
- `created_at`: string, ISO 8601 UTC
- `category`: enum (see schema)
- `scope`: enum (`"company_internal"`, `"retail"`, `"system_wide"`)
- `priority`: enum (`"low"`, `"medium"`, `"high"`, `"critical"`)
- `title`: string, 1–200 characters
- `description`: string, non-empty
- `target_files`: array of strings (may be `[]` if category is `"process"` or `"documentation"`)
- `audit_log`: array with exactly one entry (the creation event)

**At `under_review` (additional):**
- `reviewed_by`: string, must be `"patches"`
- `review_started_at`: ISO 8601 UTC

**At `approved` (additional):**
- `approved_by`: string, valid agent or `"project_lead"`
- `approved_at`: ISO 8601 UTC, must be >= `review_started_at` (or >= `created_at` if approved directly from `proposed`)

**At `rejected` (additional):**
- `rejected_by`: string
- `rejected_at`: ISO 8601 UTC
- `rejection_reason`: string, non-empty, min 10 characters

**At `blocked` (additional):**
- `blocked_by`: string
- `blocked_at`: ISO 8601 UTC
- `blocked_reason`: string, non-empty, min 10 characters

**At `in_progress` (additional):**
- `assigned_to`: exactly `"blueprint"`
- `started_at`: ISO 8601 UTC, must be >= `approved_at`

**At `staged` (additional):**
- `staged_at`: ISO 8601 UTC, must be >= `started_at`
- `staging_manifest`: object (see schema), non-null

**At `deployed` (additional):**
- `deployed_at`: ISO 8601 UTC, must be >= `staged_at`
- `deploy_commit`: string, exactly 40 lowercase hex characters
- `post_deploy_watch_id`: string, UUID v4, must reference an existing entry in `post_deploy_watch.json`

**At `completed` (additional):**
- `completed_at`: ISO 8601 UTC, must be >= `deployed_at`
- `validated_by`: exactly `"patches"`

**At `superseded` (additional):**
- `superseded_by`: string, UUID v4, must be a valid `suggestion_id` in the file (not self-referential)
- `superseded_at`: ISO 8601 UTC

### Forbidden Fields by State

**At `proposed`:** Must NOT have `approved_by`, `approved_at`, `assigned_to`, `staged_at`, `staging_manifest`, `deployed_at`, `deploy_commit`, `completed_at`, `post_deploy_watch_id`

**At `rejected`:** Must NOT have `staging_manifest`, `deployed_at`, `deploy_commit`, `completed_at`, `post_deploy_watch_id`

**At `completed`:** All prior state fields must be present. No fields are forbidden (history is preserved).

### Cross-Field Dependencies

- `started_at` >= `approved_at` (Blueprint cannot start before approval)
- `staged_at` >= `started_at`
- `deployed_at` >= `staged_at`
- `completed_at` >= `deployed_at`
- A suggestion with scope `"retail"` or `"system_wide"` with `approved_by` != `"project_lead"` is invalid
- `post_deploy_watch_id` must be present in `post_deploy_watch.json` with matching `suggestion_ids`
- `superseded_by` must not equal `suggestion_id` (no self-supersession)
- `target_files` paths must not contain hardcoded `/home/pi/` — must use relative paths from `SYNTHOS_HOME`

### ID Uniqueness Rules

- `suggestion_id` must be globally unique within the active suggestions file
- Uniqueness check is performed before write; if collision detected, the write is aborted and the creating agent regenerates a new UUID
- Archive file maintains uniqueness within itself; active and archive files may not share IDs (checked at archive time)

### Ordering Constraints

A suggestion cannot be validated as `completed` if:
- It has no `approved_by` (was never approved)
- It has no `deploy_commit` (was never deployed)
- Its linked `post_deploy_watch.json` entry has `outcome != "stable"`

A suggestion in `in_progress` is invalid if another suggestion in `in_progress` shares any path in `target_files`. This is checked by Blueprint before self-assignment. If found, Blueprint must not enter `in_progress` and must append a note to `audit_log` documenting the conflict.

---

## 6. SCHEMA

### Full JSON Structure

```json
[
  {
    "suggestion_id": "string (UUID v4)",
    "status": "proposed | under_review | approved | rejected | blocked | in_progress | staged | deployed | completed | superseded",
    "category": "bug_fix | improvement | arch_violation | cost_efficiency | security | compliance | dependency | scheduling | observability | communication | process | documentation",
    "scope": "company_internal | retail | system_wide",
    "priority": "low | medium | high | critical",
    "title": "string (1–200 chars)",
    "description": "string (non-empty)",
    "target_files": ["string (relative path from SYNTHOS_HOME)"],

    "created_by": "string (agent name or project_lead)",
    "created_at": "ISO 8601 UTC",

    "reviewed_by": "patches | null",
    "review_started_at": "ISO 8601 UTC | null",

    "approved_by": "patches | project_lead | null",
    "approved_at": "ISO 8601 UTC | null",

    "rejected_by": "string | null",
    "rejected_at": "ISO 8601 UTC | null",
    "rejection_reason": "string | null",

    "blocked_by": "string | null",
    "blocked_at": "ISO 8601 UTC | null",
    "blocked_reason": "string | null",

    "assigned_to": "blueprint | null",
    "started_at": "ISO 8601 UTC | null",

    "staged_at": "ISO 8601 UTC | null",
    "staging_manifest": {
      "staging_dir": "string (absolute path to .blueprint_staging/<suggestion_id>/)",
      "files": [
        {
          "original": "string (absolute path to live file)",
          "staged": "string (absolute path to .staged file)",
          "bak": "string (absolute path to .bak file)",
          "size_original_bytes": "integer",
          "size_staged_bytes": "integer",
          "syntax_checked": "boolean",
          "truncation_check_passed": "boolean"
        }
      ]
    },

    "deployed_at": "ISO 8601 UTC | null",
    "deploy_commit": "string (40-char hex SHA) | null",
    "post_deploy_watch_id": "string (UUID v4) | null",

    "completed_at": "ISO 8601 UTC | null",
    "validated_by": "patches | null",

    "superseded_by": "string (UUID v4) | null",
    "superseded_at": "ISO 8601 UTC | null",

    "audit_log": [
      {
        "timestamp": "ISO 8601 UTC",
        "agent": "string (agent name or project_lead)",
        "action": "string (created | status_changed | field_updated | objection | note)",
        "from_status": "string | null",
        "to_status": "string | null",
        "note": "string"
      }
    ]
  }
]
```

### Category Enum Definitions

| Value | Meaning | Typical Author |
|---|---|---|
| `bug_fix` | Corrects incorrect behavior | Patches |
| `improvement` | Enhances existing behavior | Patches, Blueprint, any agent |
| `arch_violation` | Corrects deviation from TDA or system spec | Patches, Librarian |
| `cost_efficiency` | Reduces token or API spend | Fidget |
| `security` | Addresses security or key management issue | Librarian, Vault |
| `compliance` | Addresses licensing or legal requirement | Vault |
| `dependency` | Adds, updates, or removes a package dependency | Librarian |
| `scheduling` | Addresses resource scheduling or deadlock | Timekeeper |
| `observability` | Improves monitoring or alerting | Sentinel, Patches |
| `communication` | Improves customer-facing alerts or reports | Scoop, Patches |
| `process` | Improves agent workflow (no file changes) | Any agent |
| `documentation` | Updates spec documents | Any agent |

---

### Example: `proposed`

```json
{
  "suggestion_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "proposed",
  "category": "bug_fix",
  "scope": "retail",
  "priority": "high",
  "title": "Fix research agent crash on empty Congress.gov response",
  "description": "agent2_research.py crashes with KeyError when Congress.gov returns an empty disclosures array. Should handle gracefully and log a warning.",
  "target_files": ["core/agent2_research.py"],
  "created_by": "patches",
  "created_at": "2026-03-27T08:00:00Z",
  "reviewed_by": null,
  "review_started_at": null,
  "approved_by": null,
  "approved_at": null,
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": null,
  "blocked_at": null,
  "blocked_reason": null,
  "assigned_to": null,
  "started_at": null,
  "staged_at": null,
  "staging_manifest": null,
  "deployed_at": null,
  "deploy_commit": null,
  "post_deploy_watch_id": null,
  "completed_at": null,
  "validated_by": null,
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-27T08:00:00Z",
      "agent": "patches",
      "action": "created",
      "from_status": null,
      "to_status": "proposed",
      "note": "Identified in retail-pi-01 crash log at 2026-03-26T14:22:00Z. Stack trace: KeyError 'disclosures' in process_response(). Reproducible with empty API response."
    }
  ]
}
```

---

### Example: `approved`

```json
{
  "suggestion_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "approved",
  "category": "bug_fix",
  "scope": "retail",
  "priority": "high",
  "title": "Fix research agent crash on empty Congress.gov response",
  "description": "agent2_research.py crashes with KeyError when Congress.gov returns an empty disclosures array. Should handle gracefully and log a warning.",
  "target_files": ["core/agent2_research.py"],
  "created_by": "patches",
  "created_at": "2026-03-27T08:00:00Z",
  "reviewed_by": "patches",
  "review_started_at": "2026-03-27T08:05:00Z",
  "approved_by": "project_lead",
  "approved_at": "2026-03-27T09:30:00Z",
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": null,
  "blocked_at": null,
  "blocked_reason": null,
  "assigned_to": null,
  "started_at": null,
  "staged_at": null,
  "staging_manifest": null,
  "deployed_at": null,
  "deploy_commit": null,
  "post_deploy_watch_id": null,
  "completed_at": null,
  "validated_by": null,
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-27T08:00:00Z",
      "agent": "patches",
      "action": "created",
      "from_status": null,
      "to_status": "proposed",
      "note": "Identified in retail-pi-01 crash log."
    },
    {
      "timestamp": "2026-03-27T08:05:00Z",
      "agent": "patches",
      "action": "status_changed",
      "from_status": "proposed",
      "to_status": "under_review",
      "note": "Beginning review. Scope is retail — project lead approval required."
    },
    {
      "timestamp": "2026-03-27T09:30:00Z",
      "agent": "project_lead",
      "action": "status_changed",
      "from_status": "under_review",
      "to_status": "approved",
      "note": "Approved. Confirmed this is a real crash. Blueprint to implement this week."
    }
  ]
}
```

---

### Example: `blocked`

```json
{
  "suggestion_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "status": "blocked",
  "category": "dependency",
  "scope": "retail",
  "priority": "medium",
  "title": "Upgrade feedparser to 6.1.0 for improved RSS parsing",
  "description": "feedparser 6.1.0 fixes a UTF-8 edge case that causes silent data loss on some RSS feeds.",
  "target_files": [],
  "created_by": "librarian",
  "created_at": "2026-03-24T10:00:00Z",
  "reviewed_by": "patches",
  "review_started_at": "2026-03-24T10:30:00Z",
  "approved_by": "project_lead",
  "approved_at": "2026-03-25T09:00:00Z",
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": "librarian",
  "blocked_at": "2026-03-26T11:00:00Z",
  "blocked_reason": "feedparser 6.1.0 requires Python 3.10+. Retail Pi 2W may be running 3.9. Cannot upgrade without verifying minimum Python version across all customer Pis. Blocker: unknown Pi Python version distribution.",
  "assigned_to": null,
  "started_at": null,
  "staged_at": null,
  "staging_manifest": null,
  "deployed_at": null,
  "deploy_commit": null,
  "post_deploy_watch_id": null,
  "completed_at": null,
  "validated_by": null,
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-24T10:00:00Z",
      "agent": "librarian",
      "action": "created",
      "from_status": null,
      "to_status": "proposed",
      "note": "Routine dependency audit."
    },
    {
      "timestamp": "2026-03-24T10:30:00Z",
      "agent": "patches",
      "action": "status_changed",
      "from_status": "proposed",
      "to_status": "under_review",
      "note": "Starting review."
    },
    {
      "timestamp": "2026-03-25T09:00:00Z",
      "agent": "project_lead",
      "action": "status_changed",
      "from_status": "under_review",
      "to_status": "approved",
      "note": "Approved in principle."
    },
    {
      "timestamp": "2026-03-26T11:00:00Z",
      "agent": "librarian",
      "action": "status_changed",
      "from_status": "approved",
      "to_status": "blocked",
      "note": "Python version dependency discovered during pre-implementation check."
    }
  ]
}
```

---

### Example: `in_progress`

```json
{
  "suggestion_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "in_progress",
  "category": "bug_fix",
  "scope": "retail",
  "priority": "high",
  "title": "Fix research agent crash on empty Congress.gov response",
  "description": "agent2_research.py crashes with KeyError when Congress.gov returns an empty disclosures array.",
  "target_files": ["core/agent2_research.py"],
  "created_by": "patches",
  "created_at": "2026-03-27T08:00:00Z",
  "reviewed_by": "patches",
  "review_started_at": "2026-03-27T08:05:00Z",
  "approved_by": "project_lead",
  "approved_at": "2026-03-27T09:30:00Z",
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": null,
  "blocked_at": null,
  "blocked_reason": null,
  "assigned_to": "blueprint",
  "started_at": "2026-03-28T10:00:00Z",
  "staged_at": null,
  "staging_manifest": null,
  "deployed_at": null,
  "deploy_commit": null,
  "post_deploy_watch_id": null,
  "completed_at": null,
  "validated_by": null,
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-27T08:00:00Z",
      "agent": "patches",
      "action": "created",
      "from_status": null,
      "to_status": "proposed",
      "note": "Identified in crash log."
    },
    {
      "timestamp": "2026-03-27T08:05:00Z",
      "agent": "patches",
      "action": "status_changed",
      "from_status": "proposed",
      "to_status": "under_review",
      "note": "Review started."
    },
    {
      "timestamp": "2026-03-27T09:30:00Z",
      "agent": "project_lead",
      "action": "status_changed",
      "from_status": "under_review",
      "to_status": "approved",
      "note": "Approved."
    },
    {
      "timestamp": "2026-03-28T10:00:00Z",
      "agent": "blueprint",
      "action": "status_changed",
      "from_status": "approved",
      "to_status": "in_progress",
      "note": "Self-assigned. No file mutex conflict on core/agent2_research.py. Beginning implementation."
    }
  ]
}
```

---

### Example: `completed`

```json
{
  "suggestion_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "completed",
  "category": "bug_fix",
  "scope": "retail",
  "priority": "high",
  "title": "Fix research agent crash on empty Congress.gov response",
  "description": "agent2_research.py crashes with KeyError when Congress.gov returns an empty disclosures array.",
  "target_files": ["core/agent2_research.py"],
  "created_by": "patches",
  "created_at": "2026-03-27T08:00:00Z",
  "reviewed_by": "patches",
  "review_started_at": "2026-03-27T08:05:00Z",
  "approved_by": "project_lead",
  "approved_at": "2026-03-27T09:30:00Z",
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": null,
  "blocked_at": null,
  "blocked_reason": null,
  "assigned_to": "blueprint",
  "started_at": "2026-03-28T10:00:00Z",
  "staged_at": "2026-03-28T14:30:00Z",
  "staging_manifest": {
    "staging_dir": "/home/pi/synthos-company/.blueprint_staging/a1b2c3d4-e5f6-7890-abcd-ef1234567890/",
    "files": [
      {
        "original": "/home/pi/synthos/core/agent2_research.py",
        "staged": "/home/pi/synthos-company/.blueprint_staging/a1b2c3d4-e5f6-7890-abcd-ef1234567890/agent2_research.py.staged",
        "bak": "/home/pi/synthos-company/.blueprint_staging/a1b2c3d4-e5f6-7890-abcd-ef1234567890/agent2_research.py.bak",
        "size_original_bytes": 8240,
        "size_staged_bytes": 8391,
        "syntax_checked": true,
        "truncation_check_passed": true
      }
    ]
  },
  "deployed_at": "2026-04-04T21:00:00Z",
  "deploy_commit": "a3f9b2c1d4e5f6789012345678901234abcdef01",
  "post_deploy_watch_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "completed_at": "2026-04-06T10:00:00Z",
  "validated_by": "patches",
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-27T08:00:00Z",
      "agent": "patches",
      "action": "created",
      "from_status": null,
      "to_status": "proposed",
      "note": "Identified in crash log."
    },
    {
      "timestamp": "2026-03-28T10:00:00Z",
      "agent": "blueprint",
      "action": "status_changed",
      "from_status": "approved",
      "to_status": "in_progress",
      "note": "Self-assigned."
    },
    {
      "timestamp": "2026-03-28T14:30:00Z",
      "agent": "blueprint",
      "action": "status_changed",
      "from_status": "in_progress",
      "to_status": "staged",
      "note": "Atomic deploy complete. Added disclosures.get() with empty-list default. Syntax and truncation checks passed."
    },
    {
      "timestamp": "2026-04-04T21:00:00Z",
      "agent": "project_lead",
      "action": "status_changed",
      "from_status": "staged",
      "to_status": "deployed",
      "note": "Friday push executed. Commit a3f9b2c1."
    },
    {
      "timestamp": "2026-04-06T10:00:00Z",
      "agent": "patches",
      "action": "status_changed",
      "from_status": "deployed",
      "to_status": "completed",
      "note": "Post-deploy watch c3d4e5f6 closed stable at 2026-04-06T09:55:00Z. No crashes, no regressions. Validated."
    }
  ]
}
```

---

### Example: Disagreement Case (Blueprint Objection)

```json
{
  "suggestion_id": "d4e5f6a7-b8c9-0123-def0-234567890123",
  "status": "in_progress",
  "category": "improvement",
  "scope": "retail",
  "priority": "medium",
  "title": "Refactor trader agent signal scoring loop",
  "description": "Replace nested if-else chain in score_signal() with dictionary dispatch for readability.",
  "target_files": ["core/agent1_trader.py"],
  "created_by": "blueprint",
  "created_at": "2026-03-25T09:00:00Z",
  "reviewed_by": "patches",
  "review_started_at": "2026-03-25T09:10:00Z",
  "approved_by": "project_lead",
  "approved_at": "2026-03-25T10:00:00Z",
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "blocked_by": null,
  "blocked_at": null,
  "blocked_reason": null,
  "assigned_to": "blueprint",
  "started_at": "2026-03-27T10:00:00Z",
  "staged_at": null,
  "staging_manifest": null,
  "deployed_at": null,
  "deploy_commit": null,
  "post_deploy_watch_id": null,
  "completed_at": null,
  "validated_by": null,
  "superseded_by": null,
  "superseded_at": null,
  "audit_log": [
    {
      "timestamp": "2026-03-27T13:00:00Z",
      "agent": "patches",
      "action": "status_changed",
      "from_status": "staged",
      "to_status": "in_progress",
      "note": "Staged implementation rejected. Dictionary dispatch pattern requires Python 3.10+ match-case syntax per linting review. Current retail Pi minimum is 3.9. Implementation must be revised to use compatible syntax."
    },
    {
      "timestamp": "2026-03-27T13:15:00Z",
      "agent": "blueprint",
      "action": "objection",
      "from_status": null,
      "to_status": null,
      "note": "OBJECTION: The implementation does NOT use match-case syntax. It uses a plain dict of callables (Python 3.8 compatible). Patches may have confused this with a different pattern. Requesting project lead review of the rejection before re-implementing. See staged file at .blueprint_staging/d4e5f6a7/."
    }
  ]
}
```

---

**Document Version:** 1.0
**Status:** Active
**Owner:** Patches (monitors), Blueprint (implements), Project Lead (approves)
