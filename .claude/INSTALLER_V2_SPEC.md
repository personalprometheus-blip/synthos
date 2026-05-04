# Synthos Installer v2 — Specification

**Status:** Approved for implementation pending backup-manifest contract sign-off — distributed-trader corrections appended 2026-05-04
**Author:** Installer session, 2026-05-03
**Predecessors:** [INSTALLER_V2_IMPROVEMENTS.md](INSTALLER_V2_IMPROVEMENTS.md), [AUDIT_PHASE1.md](AUDIT_PHASE1.md)
**Sibling docs:** [BACKUP_MANIFEST_CONTRACT.md](BACKUP_MANIFEST_CONTRACT.md), [INSTALLER_PROFILES.md](../synthos_build/docs/INSTALLER_PROFILES.md)

---

> ## ⚠️ DISTRIBUTED-TRADER MIGRATION DELTA (added 2026-05-04, Phase E)
>
> This spec was drafted 2026-05-03 BEFORE the Tier 1-7 distributed-trader
> migration shipped. Until §3/§5/§6/§15 are rewritten, here is the
> supplemental delta:
>
> **New apt deps (process):** `python3-paho-mqtt`, `python3-httpx`,
> `python3-fastapi`, `python3-uvicorn`, `mosquitto`, `mosquitto-clients`.
>
> **New apt deps (retail-N):** subset of above (no broker). See
> `synthos_build/src/install_retail_node.py` `APT_DEPS`.
>
> **New systemd units (process):** `synthos-mqtt-broker` (apt-supplied
> `mosquitto.service` + `synthos.conf` snippet at
> `/etc/mosquitto/conf.d/`), `synthos-dispatcher.service.j2`,
> `synthos-trader-server.service.j2` (with `TRADER_DB_MODE=local`).
>
> **New systemd unit (retail-N):** `synthos-trader-server.service` with
> `TRADER_DB_MODE=packet` + `NODE_TYPE=retail-N`.
>
> **New systemd unit (company):** `synthos-mqtt-listener.service`.
>
> **New env vars per profile:**
>
> | Var | process | retail-N | company |
> |---|---|---|---|
> | MQTT_HOST/PORT/USER/PASS | yes (master) | yes (match) | yes (match) |
> | NODE_TYPE / NODE_ID | `process` | `retail-N` | `company` |
> | DISPATCH_AUTH_TOKEN | yes (master) | yes (match) | — |
> | RETAIL_URL | `http://127.0.0.1:8443` → `http://10.0.0.20:8443` later | — | — |
> | DISPATCH_MODE | `daemon` | `distributed` | — |
> | TRADER_DB_MODE | `local` | `packet` | — |
>
> **New COLLECTING-phase prompts:** confirm/regenerate MQTT_PASS +
> DISPATCH_AUTH_TOKEN on process; prompt-or-USB-read on retail-N +
> company.
>
> **§15 acceptance criteria additions:**
> - process: `mosquitto`, `synthos-trader-server`, `synthos-dispatcher`
>   all `is-active`
> - process: `mosquitto_sub ... -t 'process/heartbeat/+/+' -C 1 -W 90`
>   returns at least one message within 90s
> - retail-N: `curl http://127.0.0.1:8443/readyz` → 200 with
>   `dispatch_mode: distributed`
> - company: `synthos-mqtt-listener` `is-active`; `auditor.db.mqtt_observations`
>   has rows < 60s old
>
> Companion: [INSTALLER_PROFILES.md](../synthos_build/docs/INSTALLER_PROFILES.md)
> describes the as-shipped three-installer architecture (process /
> retail-N / company) which fills the gap until v2 incorporates it.

---

## 1. Goal

Replace the current bash + Python bifurcated installer with a single, unified, pure-Python installer that:

- Supports three node types: **company**, **process**, **retail-N**
- Scales horizontally (retail-1, retail-2, retail-3, ...) without code edits per instance
- Restores from R2 backup as a first-class install path
- Validates a single operator license offline via signed USB token
- Configures Cloudflare tunnel + SSH alias migration on the company node
- Is testable via pytest without burning a Pi

**Non-goals (deferred):**
- Web wizard (dropped — shell-only)
- Monitor node (defer to v2.1)
- Customer-facing self-service (out of scope; customers don't own nodes)
- License server / phone-home validation (offline signature is sufficient)
- Auto-update / hot-patch (separate concern)
- Auto-deploy from company portal to other nodes (defer to v2.1+)

---

## 2. Architecture

### 2.1 Two-layer model

```
┌─────────────────────────────────────────────────────────┐
│  install.sh (~150 lines, thin bootstrap)                │
│  - Detect Python ≥3.9, install if missing               │
│  - Create venv at synthos-{role}/.venv                  │
│  - Bootstrap pip                                         │
│  - exec python3 -m installers.cli "$@"                  │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  installers/ (Python package — does all real work)      │
│  - cli.py: argument parsing, dispatch                   │
│  - nodes/: per-node-type installers                     │
│  - common/: shared modules (preflight, env, cron, etc.) │
│  - units/: systemd unit templates (.service.j2 files)   │
│  - tests/: pytest suite                                 │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Why pure Python

- Single source of truth for state, .env, cron, systemd
- Testable without Pi hardware
- Type hints + dataclasses for config schemas
- No bash/python format drift bugs
- Same install logic in dev (Mac) and prod (Pi)

### 2.3 Bootstrap problem solved

The thin `install.sh` exists ONLY to ensure Python is available and hand off. If Python is already installed (typical case), the bash phase runs in <5 seconds.

---

## 3. File layout

```
synthos-{role}/                         # node home directory
├── install.sh                          # thin bootstrap (~150 lines)
├── installers/
│   ├── __init__.py
│   ├── cli.py                          # entry: python3 -m installers.cli
│   ├── config.py                       # constants, paths, schema
│   │
│   ├── nodes/
│   │   ├── __init__.py
│   │   ├── base.py                     # NodeInstaller abstract class
│   │   ├── company.py                  # CompanyNodeInstaller
│   │   ├── process.py                  # ProcessNodeInstaller
│   │   └── retail.py                   # RetailNodeInstaller (handles retail-N)
│   │
│   ├── common/
│   │   ├── __init__.py
│   │   ├── preflight.py                # system checks (already exists; expand)
│   │   ├── progress.py                 # state machine + .install_progress.json (already exists)
│   │   ├── env_writer.py               # .env generation (already exists; expand for process)
│   │   ├── systemd.py                  # NEW: render+install unit files
│   │   ├── cron.py                     # NEW: generate+register cron entries
│   │   ├── packages.py                 # NEW: apt + pip package management
│   │   ├── network.py                  # NEW: static IP / DHCP-reservation config
│   │   ├── usb_license.py              # NEW: read+verify signed license from USB
│   │   ├── restore.py                  # NEW: 3-tier restore (file / company / R2)
│   │   ├── manifest.py                 # NEW: parse + validate backup manifests
│   │   ├── cloudflare.py               # NEW: cloudflared install + tunnel config
│   │   └── rollback.py                 # NEW: .known_good restore
│   │
│   ├── units/
│   │   ├── company/
│   │   │   ├── synthos-login-server.service.j2
│   │   │   ├── synthos-company-server.service.j2
│   │   │   ├── synthos-archivist.service.j2
│   │   │   ├── synthos-auditor.service.j2
│   │   │   └── cloudflared-tunnel.service.j2
│   │   ├── process/
│   │   │   ├── synthos-process-portal.service.j2
│   │   │   ├── synthos-process-watchdog.service.j2
│   │   │   └── synthos-process-scheduler.service.j2
│   │   └── retail/
│   │       ├── synthos-trader@.service.j2          # systemd template, agent N
│   │       └── synthos-retail-heartbeat.service.j2
│   │
│   └── tests/
│       ├── __init__.py
│       ├── test_env_writer.py
│       ├── test_cron.py
│       ├── test_systemd.py
│       ├── test_manifest.py
│       ├── test_preflight.py
│       └── conftest.py                 # pytest fixtures (mock filesystem, etc.)
│
└── (rest of node-specific code per current layout)
```

### 3.1 Why systemd unit templates as Jinja2 (.j2)

- Live as plain text files (versioned, lintable, diff-able)
- Substituted at install time with node-specific values (paths, ports, user, etc.)
- Operator can read units without running installer
- Tests can render them and verify shape

Rendering is trivial:
```python
template = (UNITS_DIR / "process" / "synthos-process-portal.service.j2").read_text()
rendered = template.format(home_dir=home_dir, user=current_user, port=5001)
(SYSTEMD_DIR / "synthos-process-portal.service").write_text(rendered)
```

(Plain `str.format` may suffice; only escalate to Jinja2 if conditional logic is needed.)

---

## 4. CLI interface

```bash
# install.sh forwards everything to: python3 -m installers.cli <args>

# Fresh install
./install.sh --node=company
./install.sh --node=process
./install.sh --node=retail --node-num=1

# Restore-on-install (defaults applied per node type — process always restores by default)
./install.sh --node=process --restore=auto              # default: try local→company→R2
./install.sh --node=process --restore=file:/path/to/backup.tar.gz.enc
./install.sh --node=process --restore=via-company
./install.sh --node=process --restore=via-r2
./install.sh --node=process --no-restore                # explicit skip (testing only)

# Diagnostics + maintenance
./install.sh --diagnose                                 # print state, services, env keys, cron
./install.sh --verify                                   # re-run verification phase only
./install.sh --repair                                   # re-run INSTALLING + VERIFYING
./install.sh --rollback                                 # restore from .known_good/

# License
./install.sh --license-file=/media/synthos-usb/license.json   # explicit path
                                                              # (default: auto-detect /media/*)

# Verbose
./install.sh --node=process -v                          # info logging
./install.sh --node=process -vv                         # debug logging
```

### 4.1 Flag rules

- `--node=<type>` is **required** for any install action.
- `--node-num=N` is **required for retail**; ignored for company/process.
- `--restore=<source>` is opt-in for company/retail, opt-out for process (process restores by default).
- `--diagnose`, `--verify`, `--repair`, `--rollback` are mutually exclusive with `--node`.

---

## 5. State machine

Same as v1.1 but extended with RESTORING and ROLLBACK states.

```
        ┌────────────────────┐
        │  UNINITIALIZED     │
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  PREFLIGHT         │   system checks, USB license verify
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  COLLECTING        │   shell prompts for config (admin email/pw, etc.)
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  RESTORING         │   (process node, optional for others)
        │                    │   download backup → verify manifest → extract
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  INSTALLING        │   write .env, dirs, code, units, cron
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  VERIFYING         │   run all checks
        └──────────┬─────────┘
                   ▼
        ┌────────────────────┐
        │  COMPLETE          │
        └────────────────────┘

  At any point on failure:
        ┌────────────────────┐
        │  DEGRADED          │   --repair or --rollback to recover
        └────────────────────┘
```

State persisted to `.install_progress.json` after every transition. Resume on re-run.

---

## 6. Per-node install flows

### 6.1 Company node

```
1. PREFLIGHT
   - Python ≥3.9, pip, sqlite3, cron, hostname
   - USB license detected + signature verified
   - Disk space ≥4GB free
   - Network reachable to internet (for Cloudflare tunnel + R2)

2. COLLECTING (shell prompts)
   - Static IP or DHCP-reservation? → which IP?
   - Admin email + password
   - Operator email (alerts)
   - R2 credentials (read from USB if present, else prompt)
   - BACKUP_ENCRYPTION_KEY (read from USB if present, else prompt)
   - Cloudflare tunnel credentials (read from USB if present)

3. RESTORING (optional)
   - Download latest company.tar.gz.enc from R2
   - Decrypt with BACKUP_ENCRYPTION_KEY
   - Validate manifest.json (must be node_type=company)
   - Extract per manifest.contents

4. INSTALLING
   - apt: python3, sqlite3, cloudflared, dnsutils
   - pip: flask, boto3, anthropic, cryptography, ...
   - mkdir: data/, logs/, agents/, utils/, login_server/
   - Render systemd units from units/company/*.j2 → /etc/systemd/system/
   - Render cron from common/cron.py
   - Write company.env (preserved if exists from RESTORING)
   - Configure cloudflared tunnel
   - Configure static IP per network.py if requested
   - Bootstrap company.db schema if absent

5. VERIFYING
   - All systemd units enabled
   - Cron registered
   - company.env has all required keys
   - cloudflared tunnel reachable
   - company.db schema valid
   - Login server returns 200 on /healthz

6. COMPLETE
   - Print SSH alias snippet for ~/.ssh/config
   - Print "Next: install process node, point at this company URL"
```

### 6.2 Process node

```
1. PREFLIGHT (same as company, plus: company node URL reachable)

2. COLLECTING
   - Static IP or DHCP-reservation? → which IP?
   - Company node URL + SECRET_TOKEN
   - BACKUP_ENCRYPTION_KEY (if not in USB license)
   - Admin/operator email
   - Restore source: auto / file / via-company / via-r2 / none

3. RESTORING (default for process — REQUIRED unless --no-restore)
   - Source dispatch:
     a. local file path → read directly
     b. via-company → POST /restore_backup with pi_id=process-pi
     c. via-r2 → direct boto3 download (requires R2 creds in USB or prompt)
   - Decrypt with BACKUP_ENCRYPTION_KEY
   - Validate manifest.json (must be node_type=process)
   - Verify size + checksum
   - Extract per manifest.contents (auth.db, customers/, .env, signals)

4. INSTALLING
   - apt: same as company minus cloudflared
   - pip: flask, anthropic, alpaca-trade-api, cryptography, ...
   - mkdir: src/, agents/, user/, data/customers/, logs/, .known_good/
   - Render systemd units from units/process/*.j2
   - Render cron (process-specific schedule)
   - .env preserved from RESTORING (or written from prompts if no-restore)
   - Network static IP per network.py

5. VERIFYING
   - All units enabled
   - Cron registered
   - .env complete (all required keys present, no empty Alpaca placeholders if restoring)
   - auth.db schema valid + decryptable with .env's ENCRYPTION_KEY
   - Customer DBs accessible
   - Process portal returns 200 on /healthz
   - Scheduler can reach company node

6. COMPLETE
   - Print "Process node ready. Customer count: N. Next: install retail-1."
```

### 6.3 Retail-N node

```
1. PREFLIGHT (same as process, plus: process node URL reachable, company URL reachable)

2. COLLECTING
   - Static IP or DHCP-reservation? → which IP?
   - Node number N (must be 1, 2, 3, ...)
   - Process node URL + SECRET_TOKEN
   - Company node URL + SECRET_TOKEN
   - Number of trader agent slots (default: 4)

3. RESTORING (NOT APPLICABLE for retail — stateless workers)
   - Skip entirely. Retail nodes hold no persistent state.

4. INSTALLING
   - apt: minimal (python3, etc.)
   - pip: flask (for trade agent listener), alpaca-trade-api, requests, cryptography
   - mkdir: src/agents/, logs/, run/ (no data/, no user/)
   - Render systemd units:
     - synthos-trader@1.service, synthos-trader@2.service, ... up to N agents
       (using systemd template syntax: synthos-trader@.service.j2)
     - synthos-retail-heartbeat.service
   - Render cron (minimal: heartbeat only)
   - Write retail-N.env (URLs, tokens, node_num, agent_count)

5. VERIFYING
   - 4 trader services enabled (or as configured)
   - Heartbeat service enabled
   - Cron registered
   - retail-N.env complete
   - Process node confirms registration ("retail-N online, capacity=4")

6. COMPLETE
   - Print "Retail-N ready. Process node has registered this node."
```

---

## 7. Restore integration

### 7.1 Three-tier dispatch

```python
# installers/common/restore.py

def restore(source: str, pi_id: str, backup_key: str) -> Path:
    """
    Restore a backup tarball to a temp location and return the extracted path.
    
    source ∈ {
        "auto",           # try in order: local file (if --restore-file), via-company, via-r2
        "file:/path",     # local file path
        "via-company",    # company node /restore_backup HTTP endpoint
        "via-r2",         # direct R2 boto3 download
    }
    """
    if source.startswith("file:"):
        return _restore_from_file(source[5:], backup_key)
    elif source == "via-company":
        return _restore_via_company(pi_id, backup_key)
    elif source == "via-r2":
        return _restore_via_r2(pi_id, backup_key)
    elif source == "auto":
        # Try in order, fall through on failure
        for fn in (_try_local, _try_company, _try_r2):
            result = fn(pi_id, backup_key)
            if result is not None:
                return result
        raise RestoreError("No restore source available")
```

### 7.2 Company node `/restore_backup` endpoint

Belongs to backup session's scope, but installer session needs to consume it. Spec:

```
POST /restore_backup
Headers: X-Auth-Token: <SECRET_TOKEN>
Body: {"pi_id": "process-pi", "date": "latest"}
Response: 200 OK with body = encrypted .tar.gz.enc as binary stream
         (Content-Type: application/octet-stream)
         (X-Backup-Date: 2026-05-02 header for verification)
         (X-Manifest-Version: 1.0 header)

Errors:
  401 — invalid token
  404 — no backup found for pi_id
  500 — R2 download failed
```

The endpoint needs to be added to `company_server.py` as part of v2 work.

### 7.3 Manifest validation flow

```python
def restore_with_manifest_validation(tar_path: Path, target_home: Path, expected_node_type: str):
    # 1. Extract manifest.json first
    with tarfile.open(tar_path) as tar:
        manifest_member = tar.getmember("manifest.json")
        manifest = json.loads(tar.extractfile(manifest_member).read())
    
    # 2. Validate schema version
    if manifest["manifest_version"].split(".")[0] not in SUPPORTED_MAJOR_VERSIONS:
        raise IncompatibleManifest(f"Unsupported version: {manifest['manifest_version']}")
    
    # 3. Validate node type matches
    if manifest["node_type"] != expected_node_type:
        raise WrongNodeType(f"Manifest is for {manifest['node_type']}, target is {expected_node_type}")
    
    # 4. Verify checksum BEFORE extracting any other file
    if not verify_checksum(tar_path, manifest["checksum_sha256"]):
        raise CorruptBackup("Checksum mismatch")
    
    # 5. Pre-check disk space
    if get_free_disk_bytes(target_home) < manifest["size_bytes_decrypted"] * 1.5:
        raise InsufficientSpace()
    
    # 6. Extract per contents[]
    for entry in manifest["contents"]:
        extract_entry(tar, entry, target_home)
```

---

## 8. USB stick / license handling

### 8.1 USB layout

```
/media/<mountpoint>/synthos-key/
├── license.json          # signed license, mandatory
├── r2_credentials.json   # company node only — R2 keys
├── backup_key.txt        # BACKUP_ENCRYPTION_KEY (or in license.json)
├── cloudflared/          # company node only — Cloudflare tunnel creds
│   ├── credentials.json
│   └── config.yml
└── README.txt            # operator-facing reminder
```

### 8.2 License format

`license.json`:
```json
{
  "deployment_id": "synthos-prod-001",
  "issued_at": "2026-05-03T12:00:00Z",
  "expires_at": "2027-05-03T12:00:00Z",
  "max_customers": 50,
  "plan_tier": "operator",
  "permitted_nodes": ["company", "process", "retail-1", "retail-2", "retail-3"]
}
```

Plus a detached signature: `license.json.sig` (or embedded `.signature` field in JSON).

Signature verified against an embedded public key compiled into the installer (so the installer can verify offline without network).

### 8.3 License read flow

```python
def load_license_from_usb(explicit_path: Optional[Path] = None) -> License:
    if explicit_path:
        return _load_and_verify(explicit_path)
    
    # Auto-detect: scan /media/* and /mnt/*
    for mount in glob.glob("/media/*") + glob.glob("/mnt/*"):
        candidate = Path(mount) / "synthos-key" / "license.json"
        if candidate.exists():
            return _load_and_verify(candidate)
    
    # Fall back to prompting
    path = input("USB license not found. Path to license.json: ")
    return _load_and_verify(Path(path))
```

### 8.4 License verification

- Parse JSON, extract signature
- Verify signature against embedded public key (Ed25519 recommended — small + fast)
- Validate `expires_at` > now
- Validate `node_type` being installed is in `permitted_nodes`
- Reject installation on any failure

---

## 9. Cloudflare tunnel setup

### 9.1 Company node only

```python
# installers/common/cloudflare.py

def install_cloudflared(creds_dir: Path) -> None:
    # 1. apt-get install cloudflared
    # 2. mkdir -p /etc/cloudflared
    # 3. cp <creds_dir>/credentials.json /etc/cloudflared/<tunnel-uuid>.json
    # 4. cp <creds_dir>/config.yml /etc/cloudflared/config.yml
    # 5. Render cloudflared-tunnel.service.j2 → /etc/systemd/system/
    # 6. systemctl enable + start cloudflared-tunnel
    # 7. Verify tunnel reachable
    pass
```

### 9.2 SSH config snippet output

After company-node install completes:

```
Installation complete.

Add the following to ~/.ssh/config on your Mac:

Host company
    HostName <company-fqdn-or-ip>
    User <username>
    ProxyCommand cloudflared access ssh --hostname company.<your-domain>

Host process
    HostName 10.0.0.11
    User <username>

Host retail-1
    HostName 10.0.0.20
    User <username>

After process node and retail-1 are installed, you can test with:
    ssh company        # via Cloudflare tunnel
    ssh process        # direct LAN
    ssh retail-1       # direct LAN
```

(Optional: write this to a file the installer can copy to clipboard or output as a paste-able block.)

---

## 10. Network configuration

### 10.1 Scope

Per-Pi only. No DHCP server, no DNS server. (Defer to v2.1.)

### 10.2 Configuration paths

```python
# installers/common/network.py

def configure_network(mode: str, ip: str, gateway: str, dns: list[str]) -> None:
    """
    mode ∈ {"static", "dhcp-reservation", "dhcp-dynamic"}
    """
    if mode == "static":
        _write_dhcpcd_static(ip, gateway, dns)
    elif mode == "dhcp-reservation":
        # Pi side: just DHCP. Operator must configure router-side reservation.
        _ensure_dhcp_only()
        print(f"⚠ Configure router DHCP reservation for MAC {get_mac()} → {ip}")
    elif mode == "dhcp-dynamic":
        _ensure_dhcp_only()
```

### 10.3 Default IP scheme (suggested)

| Node type | Default IP |
|-----------|-----------|
| company | 10.0.0.10 |
| process | 10.0.0.11 |
| retail-1 | 10.0.0.20 |
| retail-2 | 10.0.0.21 |
| retail-3 | 10.0.0.22 |
| (future monitor) | 10.0.0.12 |

Operator can override at COLLECTING phase.

---

## 11. Testing strategy

### 11.1 pytest setup

```
installers/tests/
├── conftest.py             # fixtures
├── test_env_writer.py      # .env content + permissions
├── test_manifest.py        # manifest parse + validate
├── test_cron.py            # cron entry generation + syntax
├── test_systemd.py         # unit file rendering
├── test_preflight.py       # check results for various system states
├── test_restore.py         # 3-tier dispatch (with mocked HTTP + S3)
├── test_state_machine.py   # transition correctness
├── test_usb_license.py     # signature validation
└── test_integration.py     # end-to-end with tmpdir as fake SYNTHOS_HOME
```

### 11.2 Coverage targets (not 100%, just key invariants)

- `env_writer`: every required key present per node type; permissions = 0600
- `manifest`: rejects bad versions, bad node types, bad checksums
- `cron`: generated entries parse with `crontab -` syntax check (invokable without writing to actual crontab)
- `systemd`: rendered unit files pass `systemd-analyze verify` if available, else regex sanity
- `usb_license`: rejects expired, wrong-signature, wrong-node-type
- `restore`: dispatch order (auto: local → company → R2), failure fallback
- `state_machine`: all valid transitions documented, invalid transitions raise

### 11.3 Run tests on Mac during development

```bash
cd /Users/patrickmcguire/synthos/synthos_build
python3 -m pytest installers/tests/ -v
```

No Pi hardware needed. Tests use `tmpdir` for filesystem ops, `unittest.mock` for HTTP/subprocess.

---

## 12. Migration plan (v1 → v2)

### 12.1 Approach

**Option chosen: wipe + reinstall.** The user confirmed this is acceptable for the v1→v2 transition.

This means:
1. Verify v1 R2 backups are decryptable (Phase 0 of first-time setup)
2. Wipe and reflash all Pis
3. Run v2 installer fresh
4. Restore from R2 on process node

This avoids legacy v1 install detection, partial-state handling, and migration logic. Cleanest possible cutover.

### 12.2 Backwards compat for backups

The v2 installer must restore **v1 backups** (which lack manifest.json). Two handling paths:

**Path A:** Backup session updates `company_strongbox.py` to add manifest.json to existing v1 backups (re-pack + re-upload). Then v1 backups become v1.0-manifest backups.

**Path B:** v2 installer has a "legacy v1 backup" code path that hardcodes the file layout (auth.db at root, customers/ at root, .env at user/.env). When manifest.json is absent, fall back to legacy layout.

**My recommendation: Path B.** Reasons:
- Avoids re-uploading large backups
- Legacy code path is small (~30 lines)
- Eventually gets retired when no v1 backups remain in retention window (30 days post-cutover)

### 12.3 Pre-cutover checklist

- [ ] Backup session has produced manifest contract response (accepted or modified version of [BACKUP_MANIFEST_CONTRACT.md](BACKUP_MANIFEST_CONTRACT.md))
- [ ] retail→process rename PR is merged (separate prep PR)
- [ ] v2 installer code is on a branch, all pytest passes
- [ ] Latest v1 backup is verified decryptable on Mac (Phase 0)
- [ ] USB stick is prepared with license + R2 creds + tunnel creds
- [ ] Cloudflare tunnel UUID is known + creds backed up
- [ ] All 3 new Pis have NVMe + Pi OS Lite flashed + network access verified individually

---

## 13. Open dependencies / coordination points

### 13.1 With backup session

- **Manifest contract** must be reviewed and accepted (or modified)
- `company_strongbox.py` must produce manifests in agreed format
- `retail_backup.py` (or successor) likewise
- `/restore_backup` HTTP endpoint must be added to `company_server.py`

### 13.2 With process node code (post-rename)

- `retail_*` files renamed to `process_*` files in separate prep PR
- New `process_scheduler.py` must implement work-packet dispatch (B2 architecture)
- New `process_trade_proxy.py` (or equivalent) must implement `/trade_result` callback endpoint for retail-N to POST results
- New retail-side `trade_agent` listener must accept work packets

### 13.3 With license signing infrastructure

- Need a build-time process to sign `license.json` files
- Private key held by operator (you), public key compiled into installer
- Recommend: Ed25519 keypair, tooling = `python -m cryptography` or `signify`
- One-time setup, not part of v2 install code itself but blocking dependency

### 13.4 With Cloudflare tunnel

- Existing tunnel UUID + creds must be available on USB stick
- DNS records at Cloudflare unchanged (still point to tunnel UUID)
- Old company node (pi4b) tunnel must be stopped before new company node tunnel starts (otherwise both compete)

---

## 14. Implementation timeline

Estimated 4 weeks of focused work.

### Week 1 — Common modules + tests
- [ ] `installers/common/{systemd, cron, packages, network, usb_license, manifest, restore}.py`
- [ ] Extract embedded systemd units to `units/{company,process,retail}/*.j2`
- [ ] pytest scaffolding + first tests

### Week 2 — Node installers
- [ ] `installers/nodes/{base, company, process, retail}.py`
- [ ] `installers/cli.py`
- [ ] Reduce `install.sh` to ~150 lines (bootstrap only)
- [ ] Manual test on Mac (dry-run mode)

### Week 3 — Restore + Cloudflare integration
- [ ] Coordinate with backup session: manifest schema sign-off
- [ ] Coordinate with process-node code: `/restore_backup` endpoint
- [ ] `installers/common/cloudflare.py`
- [ ] License signing pipeline (operator-side)
- [ ] End-to-end test on a spare Pi (if available) or VM

### Week 4 — Verification, docs, cutover prep
- [ ] `--diagnose`, `--rollback`, `--repair` flags
- [ ] `docs/INSTALLER.md` operator quick-start + troubleshooting
- [ ] Update `system_architecture.json`
- [ ] Phase 0 backup verification dry-run
- [ ] First-time setup ready

---

## 15. Acceptance criteria

v2 installer is ready for first-time setup when:

- [ ] All 3 node types install cleanly on a fresh Pi OS Lite
- [ ] Process node restores correctly from a v1.0-manifest backup
- [ ] Retail-1 registers with process node and receives work packets
- [ ] Cloudflare tunnel on company node accepts SSH from Mac
- [ ] Backup manifest contract accepted by backup session
- [ ] All pytest tests pass
- [ ] `docs/INSTALLER.md` documents the operator quick-start
- [ ] `system_architecture.json` reflects new node topology
- [ ] License signing pipeline produces a valid signed `license.json` that the installer accepts

---

## 16. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Backup session disagrees with manifest schema | Medium | High | Send contract early, iterate before code freeze |
| v1 backup format differs from assumed legacy layout | Medium | High | Path B fallback code; test on real v1 backup before wipe |
| Cloudflare tunnel has stale state when migrated | Low | Medium | Stop pi4b tunnel before new company tunnel starts; test ssh access immediately |
| USB license signature verification fails on Pi | Low | High | Test signature flow on Pi before issuing real license |
| Pytest mocks miss real subprocess behavior | Medium | Low | Run integration test on actual Pi before production wipe |
| pip package installs break on Pi (network, ARM compatibility) | Low | Medium | Pin versions; have offline wheel cache as fallback |
| Static IP setup conflicts with router DHCP | Medium | Medium | Recommend DHCP reservation as default; static as opt-in |
| Process node restore takes too long (large customer DBs) | Low | Low | Show progress bar; set realistic time expectation |

---

## 17. Sign-off

- **v2 spec authored:** 2026-05-03, installer session ✓
- **Architecture decisions:** all 14 questions answered, all decisions locked
- **Backup manifest contract:** drafted, awaiting backup-session review
- **User approval:** awaiting

If this looks right, the next steps are:
1. Hand backup contract to backup session for review
2. Start the retail→process rename prep PR
3. Begin Week 1 of v2 implementation

If anything in this spec needs adjustment, flag it now before code work begins.
