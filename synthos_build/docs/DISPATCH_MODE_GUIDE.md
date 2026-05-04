# Dispatch Mode Guide

**Created:** 2026-05-04
**Audience:** operator, support team, anyone troubleshooting "is this customer being traded?"
**Companion docs:** [CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md) (operational migration), [WORK_PACKET_PROTOCOL.md](WORK_PACKET_PROTOCOL.md) (HTTP wire shape)
**Code source of truth:** `src/dispatch_mode.py`, `agents/synthos_migration.py`

---

## What is dispatch_mode?

Every active customer is owned by exactly one of two trade-execution
paths:

- **`daemon`** — legacy (and current default). The retail_market_daemon
  spawns a `retail_trade_logic_agent.py` subprocess for the customer
  every 30 seconds during market hours, with parallelism cap 3.
- **`distributed`** — new (Tier 5+). The `synthos_dispatcher` builds a
  WorkPacket for the customer, POSTs it to a `synthos_trader_server`
  HTTP endpoint, applies the returned StateDelta to the master DB
  single-writer.

Both paths run the SAME 13 trader gates against the SAME data. The
difference is execution model + concurrency:

| Aspect | daemon | distributed |
|---|---|---|
| Trader process | Subprocess per customer per cycle | One async HTTP server, customers parallelized via `asyncio.to_thread` |
| State source | Reads SQLite live | Reads from in-memory WorkPacket pre-built by dispatcher |
| Customers per Pi5 | ~3 in parallel (subprocess pool) | ~50–100 in parallel (one async process) |
| Failure isolation | One hung customer blocks a slot | One slow customer blocks a thread, not the loop |
| Dispatch latency | ~30s cycle | ~30s cycle |
| Per-cycle overhead | ~100ms subprocess spawn | ~5–15ms HTTP RPC |

---

## Where dispatch_mode is set

**Per-customer setting** (highest precedence): `_DISPATCH_MODE` row in
the customer's `signals.db` `customer_settings` table. Values:
`"daemon"` or `"distributed"`. Anything else (including unset) means
"use env default".

**Env default**: `DISPATCH_MODE` env var on the process node.
Defaults to `"daemon"` if unset. Set in `synthos_build/user/.env`.

**Resolution function**: `src/dispatch_mode.py` →
`resolve_customer_dispatch_mode(customer_id) -> 'daemon' | 'distributed'`

```python
# Pseudocode of resolution:
raw = customer_db.get_setting("_DISPATCH_MODE")
if raw in ("daemon", "distributed"):
    return raw
return os.environ.get("DISPATCH_MODE", "daemon")
```

---

## Inspecting current state

The migration CLI is the sanctioned tool. Always run it from the Pi5:

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py status"'
```

Output:
```
env DISPATCH_MODE default: daemon

CUSTOMER_ID                               MODE          SOURCE
----------------------------------------  ------------  ------------------------------
d88a744d-2f14-4be9-9382-a8111052d194      daemon        env default
9889c8c8-7ca4-475d-b6a9-4b7916e2952c      distributed   per-customer setting (_DISPATCH_MODE=distributed)
... etc
summary: 12 daemon, 1 distributed, 13 total
```

---

## Migrating a single customer

```bash
# Migrate to distributed
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py enable <customer_uuid>"'

# Roll back to daemon (instant — next cycle picks it up)
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py disable <customer_uuid>"'
```

Both write a `DISPATCH_MODE_CHANGE` audit row to the customer's
`events` table.

---

## Resetting a stuck customer

If the per-customer setting is corrupt or you want to fall back to env
default:

```bash
ssh pi4b 'ssh SentinelRetail "cd ~/synthos/synthos_build && python3 agents/synthos_migration.py reset <customer_uuid>"'
```

This clears `_DISPATCH_MODE` (writes empty string, treated as unset by
the resolver). Customer reverts to whatever env default is.

---

## Bulk operations

Use sparingly — these touch every active customer:

```bash
# Migrate everyone to distributed
python3 agents/synthos_migration.py enable-all --confirm

# Roll everyone back to daemon
python3 agents/synthos_migration.py disable-all --confirm
```

The `--confirm` flag is required. Without it, the CLI errors out and
prints a warning. Both subcommands write an `ENABLE_DISTRIBUTED_ALL` /
`DISABLE_DISTRIBUTED_ALL` audit row per customer.

---

## How market_daemon and dispatcher interact

Both consult the resolver on every cycle:

| Component | Filter | What it owns |
|---|---|---|
| `retail_market_daemon.run_trade_all_customers` | `filter_customers_by_mode(active, 'daemon')` | Daemon-mode customers only |
| `synthos_dispatcher.run_cycle` | `filter_customers_by_mode(active, 'distributed')` | Distributed-mode customers only |

Every active customer is owned by exactly one path on every cycle. **No
customer is ever traded twice in the same cycle.**

If env default is `daemon` and zero customers have the per-customer
setting, the dispatcher idles cheaply (filtered set is empty, fast no-op).

If env default is `distributed`, the daemon idles (its filter returns
empty) and dispatcher takes over.

---

## Common questions

### "Is this customer being traded?"

Run `synthos_migration.py status` — find the customer row, see the MODE column.
Then check the corresponding service's logs:
- daemon-mode: `/home/pi516gb/synthos/synthos_build/logs/trade_daemon.log`
- distributed-mode: `/home/pi516gb/synthos/synthos_build/logs/trader_server.log`
  AND `/home/pi516gb/synthos/synthos_build/logs/dispatcher.log`

### "Why isn't my customer being traded?"

Run through this checklist:
1. Customer is in `active_customers` (check via `python3 -c "from retail_shared import get_active_customers; print('<cid>' in get_active_customers())"`)
2. `synthos_migration.py status` shows them on the expected mode
3. Validator verdict for them isn't NO_GO (`SELECT * FROM customer_settings WHERE key='_VALIDATOR_VERDICT'` in their signals.db)
4. No active halt (`grep '_KILL_SWITCH' synthos_build/data/`)
5. Mode-specific service is alive:
   - daemon: `systemctl is-active synthos-trade-daemon.timer`
   - distributed: `systemctl is-active synthos-dispatcher.service synthos-trader-server.service`

### "I just enabled distributed for a customer; nothing happened. Why?"

Check the dispatcher log immediately:
```bash
ssh pi4b 'ssh SentinelRetail "tail -f /home/pi516gb/synthos/synthos_build/logs/dispatcher.log"'
```

Within 30 seconds you should see:
```
[2026-05-04 ...] INFO dispatcher: cycle=open dispatching 1/13 customers retail=...
```

If you see `dispatching 0/13`, the customer's per-customer setting
didn't actually persist. Re-run `synthos_migration.py status` to verify.

If you see `dispatching 1/13` but no actual trade, check trader_server
log + look for the work_id in both:
```bash
grep "wrk-" /home/pi516gb/synthos/synthos_build/logs/dispatcher.log | tail -3
grep "wrk-" /home/pi516gb/synthos/synthos_build/logs/trader_server.log | tail -3
```

### "Can I migrate all customers in one command?"

Yes: `enable-all --confirm`. But please don't on production. Migrate
one customer per market session, watch them, then add the next. The
runbook has the recommended cadence.

### "What if the dispatcher is down?"

Distributed-mode customers don't trade until dispatcher comes back.
Daemon-mode customers are unaffected — their path is independent.

To roll affected customers back instantly:
```bash
python3 agents/synthos_migration.py disable-all --confirm
```
This sets every customer to daemon, market_daemon takes over on next cycle.

---

## Audit trail

Every mode change writes a row to the customer's `events` table:

```
event:        DISPATCH_MODE_CHANGE
agent:        synthos_migration
details:      ENABLE_DISTRIBUTED: daemon → distributed
created_at:   2026-05-04T13:30:00Z
```

Pull the history for a customer:
```sql
SELECT created_at, details FROM events
WHERE event='DISPATCH_MODE_CHANGE'
ORDER BY created_at DESC LIMIT 20;
```

---

## Related operating modes

`DISPATCH_MODE` is one of three per-customer modes. The other two are
unaffected by the migration:

- **`OPERATING_MODE`** — `MANAGED` (manual approval queue) vs
  `AUTOMATIC` (auto-execute on validated signals)
- **`TRADING_MODE`** — `PAPER` vs `LIVE` (Alpaca account type)

All three compose independently. A customer can be `AUTOMATIC` + `PAPER`
+ `distributed`, or any other combination. See
`system_architecture.json` → `operating_modes` for the full matrix.
