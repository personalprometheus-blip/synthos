# Synthos TODO

Format: `- [ ] [category] Title — action`
Categories: hardware, ops, dev, deploy, network, monitor

---

## Pending

- [ ] [hardware] Pi5 retail node SSH — get HDMI cable, login via console, fix SSH auth and install Synthos
- [ ] [deploy] Pi5 retail agents — install all retail agents on pi5 after SSH resolved
- [ ] [monitor] Monitor node SSH bootstrap — add pi4b key to monitor authorized_keys for inter-Pi deploys
- [ ] [network] Network switch static IPs — assign static LAN IPs, update MONITOR_URL and COMPANY_URL on all nodes
- [ ] [monitor] Remove debug banner — remove dbg-banner div from monitor dashboard
- [ ] [ops] Restore pi4b HDMI output — disable old sentinel.service and restore HDMI
- [ ] [dev] retail_heartbeat.py command consumer — read `commands` from heartbeat response and act on them (trading mode, kill switch, operating mode)
- [ ] [ops] Auditor boot.log findings — CRITICAL boot failure (missing .env) needs investigation and resolve

## In Progress

- [ ] [hardware] Pi5 network access — testing SSH path via monitor node

## Done

- [x] [deploy] Company auditor deployed — company_auditor.py live on pi4b, daemon mode, dedup working
- [x] [deploy] Company archivist deployed — company_archivist.py live on pi4b, nightly 2am UTC
- [x] [monitor] Auditor page rewired — renamed from Self-Improvement, shows real findings from auditor.db
- [x] [monitor] Agent fleet table — all expected agents shown with active/idle/inactive status
- [x] [monitor] Global commands panel — trading mode gate, operating mode, kill switch
- [x] [monitor] Fleet cards updated — Avg Temp replaced with Fleet Agents, Trading Mode card added
