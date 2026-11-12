# ApexForge (read_me)

## Overview
ApexForge is a Python-based, terminal-focused MUD client/automation engine (MegaMUD/MegaMMUD-inspired, not byte-identical) for `D:\ApexForge`. It targets async operation, buffered/compiled parsing, priority command dispatch, node-based pathing, analytics, and party coordination.

**Current focus:** building a robust headless/core engine first (networking/parsing/state/path/combat hooks) with an optional Textual dashboard.

## What it does
- **Core engine (`apexforge.py`)**: async TCP connection and line/ANSI handling, tri-tier priority command queue (P1 survival/interrupt/snipe, P2 combat, P3 utility), state machine (`NAVIGATING`/`COMBAT`/`RESTING`/`IDLE`), BBS/login scripting hooks, config-driven statline/prompt/message parsing, vitals/XP/mana-tick handling, room-arrival verification and resync, path/loop execution with timing/waits, safety/recovery hooks (flee/rest/orientation/lockpick/lost), combat/economy/party helper hooks, admin command handling, event emission.
- **Navigation (`navigation.py`)**: weighted directed room graph (NetworkX Dijkstra), edge-weight/risk adjustment, zone/drop/name lookups, ASCII map rendering.
- **Analytics (`analytics.py`)**: XP/hr (rolling), path efficiency and status, RTK/RTC, walk/lap stats, state-time tracking, best-in-slot scoring, bounded immunity memory DB, summaries.
- **Coordination (`coordination.py`)**: local IPC bus (leader broadcast, subscriber handling), webhook alert dispatch, follower node/client primitives for state sync and callback-driven follower execution.
- **Dashboard (`main.py`)**: Textual UI (MegaMUD/zMUD/Mudlet-inspired baseline) with vitals/analytics, map/zone/risk, party/coordination, queue/IPC/alerts panels, split server stream and comms/events logs, and an interactive controls area (command input + feature buttons) for user interaction (offline or connected modes).
- **Orchestrator (`ultra_orchestrator.ps1`)**: provisions baseline JSON configs if missing, verifies Python/deps, launches engine or dashboard.
- **Importer (`import_megamud.py`)**: optional tool to import/reference MegaMUD data (chars/BBS INIs, rooms/messages text, `.mp` path traversals) into ApexForge JSON artifacts.
- **Data/config**: `profiles.json`, `paths.json`, `database.json`, `items.json`, `messages.json`, `requirements.txt` (`networkx`, `textual`).

## Repository layout
- `apexforge.py` — canonical runtime engine and CLI entry.
- `main.py` — Textual dashboard entry.
- `navigation.py`, `analytics.py`, `coordination.py` — subsystems.
- `import_megamud.py` — optional MegaMUD import/reference utility.
- `ultra_orchestrator.ps1` — bootstrap/launcher.
- `profiles.json`, `paths.json`, `database.json`, `items.json`, `messages.json` — runtime/config/data.
- `requirements.txt` — Python dependencies.

## Setup
1. **Python**: Python 3.12+ recommended (known path in this environment: `D:\Users\notmy\AppData\Local\Programs\Python\Python312\python.exe`).
2. **Dependencies**:
   - `pip install -r requirements.txt` (installs `networkx`, `textual`)
   - Optional: `curl` for webhook delivery.
3. **Config/data**: ensure `profiles.json`, `paths.json`, `database.json`, `items.json` exist. `ultra_orchestrator.ps1` can provision minimal baseline files if missing (note: existing project data may be richer than baselines).

## How to use
### Run headless engine
```powershell
python .\apexforge.py --profile <ProfileName> [--path <path_key> | --target <room_id>] [--host <host> --port <port>] [--no-connect] [--ipc-bus]
```
- `--no-connect`: initialize subsystems without opening a MUD socket (offline/local).
- `--ipc-bus`: start local coordination bus (default host/port from profile/config).
- `--follower`: run as follower, connect to leader IPC bus for state/command sync and status reporting (`--leader-host/--leader-port` override profile).

### Run dashboard
```powershell
python .\main.py --profile <ProfileName> [--connect] [--path <path_key>] [--target <room_id>] [--ipc-bus]
```
- Dashboard defaults to offline unless `--connect` is provided.
- UI provides interactive controls: feature buttons and a command input to send raw commands or `@...` admin/engine commands (e.g. `@status`, `@path <key>`, `@route <room>`, `@stop`).

### Orchestrator (Windows/PowerShell)
```powershell
.\ultra_orchestrator.ps1 [-CharacterProfile <name>] [-MudHost <host>] [-Port <port>] [-PathKey <key>] [-TargetRoom <id>] [-NoConnect] [-IpcBus] [-Dashboard]
```
- Resolves `python` on PATH with fallback to the known local Python install; checks for `networkx`/`textual`.

### Import MegaMUD data (optional/reference)
```powershell
python .\import_megamud.py --megamud E:\megamud --char <CharName> --bbs <BBSName> --out . --import-paths --paths-limit <n>
```
- Produces/merges ApexForge JSON artifacts; MegaMUD binary MDB2 databases are not fully decoded.

### Key in-game/admin commands (engine-supported, subject to config/game strings)
Examples: `@status @where @speed @route @run @path @loop @flee @stop @reset @say @time @lap @recalc @map @help @profile @weapon @bsweapon @altweapon @health @heal @inventory @uses @wealth @deposit-all @share` (availability depends on wiring and permissions).

## Configuration notes
- **Profiles (`profiles.json`)**: character/connection, survival thresholds, spells/automation, party/coordination, combat profiles/swaps, navigation, inventory/economy, BBS/login, parsing/statline/prompt settings, IPC/webhook/admin options.
- **Paths (`paths.json`)**: named loop/path definitions, level/XP thresholds, timing, fallbacks, per-step overrides (`direction/pre_cmd/post_cmd/delay/lockpick/party_cmd/min_monsters`), and graph validation/annotation metadata.
- **Database (`database.json`)**: room graph (nodes, exits/weights/zones/coords/drops/room commands/waits) used for routing and mapping.
- **Items (`items.json`)**: item stats/value/weight/uses and immunity-related data.
- **Messages (`messages.json`)**: trigger/status patterns used for message-driven parsing/state tracking.
- Behavior is **config/regex/command-template driven** and game/server-specific; imports are optional aids, not ApexForge’s canonical native format.

## Changes (timestamped)

### 2026-11-12T01:47Z (session, Step 1 audit/repair + Step 2 smokes + Step 3 runtime resolution)
- `import_megamud.py` (restored on-disk): added `--extract-mdb2/--validate-paths/--annotate-db/--no-annotate-db`, Jet/MDB2 detection hooks, `room_deterministic_hash`, dual-key/metadata room parsing, conditional-aware `.mp` step overrides, and path validation/annotation (`path_validation_report.json`). `py_compile`/`--help` OK.
- **Validation run executed:** `python import_megamud.py --validate-paths --out D:\ApexForge` → `787/1809 valid, 1022 invalid` (`path_validation_report.json`, paths/db annotated). **Side effects (non-merge run):** BBS/char import (`Justa` from `E:\megamud\Chars\sysop.INI`), Rooms parsed 783/added 783/total 7427, Messages 511, `requirements.txt` rewritten. **Policy:** future `E:\megamud` re-imports must be non-destructive/merge-only overlays (no blind overwrite of repaired edges/edge_meta, keep `graph_validated:true` locked unless forced, do not overwrite tuned profiles/messages—use `*.imported.json` staging when needed).
- **Step 1 (reachability audit/repair):** 7208 unconnected gaps all `no_direct_or_routed_edge` (0 missing nodes), 1045 conditional; `0` step overrides contained inferable cardinal directions, so no safe direct-edge auto-inference applied (no fake edges). Multi-hop waypoint splits not inferable from graph. All 1022 invalid paths annotated `manual_review:true`, `repair_status:manual_review_unreachable` with notes; conditional gaps retained/annotated as edge metadata; 787 valid paths preserved.
- **Step 2 (deterministic smokes):** `silver_mines_low` routable/valid (`ok`); `route 1->4 ok`; `ACCDARFL` correctly flagged unroutable/manual-review (`CA705555->CA705455`, `CA705455->44E00004` missing). Smokes confirm repaired/valid graph behavior and explicit reporting for unresolved imports.
- **Step 3 (runtime room resolution):** `navigation.py` adds `normalize_room_name/exit`, `room_deterministic_hash`, expanded indexes (`name/hash/code`), and `resolve_room(name, obvious_exits, megamud_code/id)` with priority `(name+exits hash) -> name-only hash -> code/id -> normalized name` (exit-disambiguated). `apexforge.py` tracks `current_room_exits`, resolves arrivals via resolver (legacy fallback retained), and GMCP room handling resolves by name+exits+id/code. Smoke: town/cave/landing/code resolution OK.
- **Gaps:** 1022 paths remain manual-review (need authoritative MDB2/RoomID data or server-verified hops); resolver/hash fidelity needs live validation against real room/exit strings; importer merge-only/staging mode and host-fingerprinted overlays not yet enforced in CLI.
- Verification: `python -m py_compile apexforge.py navigation.py import_megamud.py` → `PY=0`; resolver and routing smokes executed.

### 2026-11-11T20:32Z (session, solo resilience: accumulator, avoidance, gates)
- `apexforge.py`: **Line-buffered paragraph accumulator** — inbound TCP stream is ANSI-stripped and accumulated into `_stream_line_buffer`, flushed into `_process_stream_chunk()` on verified prompt boundaries (and prompt-only/no-newline fragments) so multi-line/wrapped MajorMUD/BBS text is parsed with full paragraph context; chunk-level P1 control intercept runs before line dispatch.
- `analytics.py` + `navigation.py` + `apexforge.py`: **Bounded immunity/avoidance (anti-loop)** — rolling 15-minute failure registry, avoidance at >=3 failures per room, and temporary `9999` edge-weight penalties restored on expiry to force Dijkstra reroute. Wired from flee paths (low-HP, min-monsters, admin/leader-commanded) and **paragraph-level lethal/player-death detection** (`you have been slain/die/are dead...`) which records a hostile failure and enters prompt-locked `RECOVERY`.
- `apexforge.py` + `navigation.py`: **Conditional edge/gate validation** — route steps carry conditional metadata (`requires_lockpick/lockpick_cmd`, `required_item_id|required_key`, `requires_command/use_command/unlock_command`, monster/closed/edge_meta). On blocked/closed/locked-door cues (line and paragraph), engine attempts gate recovery (use key / unlock command / lockpick) before reroute; step dispatch performs best-effort pre-move checks and emits `edge_condition_blocked`/`gate_recovery`.
- **Solo/multi-box strength (integrated):** tri-tier priority queue (P1 survival interrupts flush P2/P3 on recovery/spin-out), adaptive combat tick pacing, prompt-bounded parsing, avoidance-aware routing, and IPC role/STATE telemetry with HEALER P1 assist on leader `COMBAT` + low HP% support fast/resilient solo and coordinated party loops.
- **Gaps:** avoidance triggers still need broader lethal/teleport/spin-out text coverage per realm; gate recovery is heuristic (needs server-specific key/door strings and verified success parsing); accumulator/trigger engine not yet full packet-safe capture-persistent; conditional schema/mutation/persistence and per-host fingerprinted overlays remain pending.
- Verification: `python -m py_compile apexforge.py main.py analytics.py navigation.py coordination.py import_megamud.py` → `PY=0`. Commit/push intentionally not performed.

### 2026-11-11T19:59Z (session, anti-loop avoidance, party combo sync, GMCP fallback scaffold)
- `analytics.py`: added bounded **immunity/avoidance anti-loop registry** — `record_room_failure(room_id, monsters, reason)` logs flee/lethal/block failures in a rolling **15-minute** window, flags avoidance at **>=3 failures**, and tracks expiry/active state (`failure_count_recent`, `is_room_avoided`, `expired_avoidances`, `active_avoidances`; volatile, room-bounded). `get_summary()` reports `avoidance_active`.
- `navigation.py`: added `apply_avoidance_penalty(room_id, penalty=9999.0)` and `clear_avoidance_penalty(room_id)` to temporarily max-weight all edges to/from hostile rooms and restore base weights after expiry (enables Dijkstra reroute around danger zones).
- `apexforge.py`: integrated avoidance hooks — `_record_hostile_failure/_on_flee_event` record failures and apply `9999` edge penalties on threshold; wired into low-HP/min-monsters/admin/leader-commanded flee paths; `_maintain_avoidance_penalties` expires/restores penalties during reroute ticks and emits `room_avoided/room_failure/room_avoidance_expired` events.
- `apexforge.py` (multi-instance combo): party telemetry now carries role/state/`hp_pct` (`party_role`, configurable heal-assist thresholds/spells). Leader `set_state` broadcasts `STATE` with vitals/role; follower `handle_party_state` processes leader `STATE` and status packets and, when role is `HEALER`, injects a **P1 targeted heal** to leader if leader is in `COMBAT` and HP% below configured threshold (rate-limited). Baseline for TANK/HEALER/DPS role-matrix coordination.
- `apexforge.py` (GMCP/OOB): added telnet negotiation + GMCP fallback scaffold — on connect sends `IAC WILL GMCP`, parses telnet/GMCP subnegotiations, emits `gmcp` events, and best-effort maps GMCP vitals/room payloads into engine state; read loop is telnet-aware with graceful fallback to existing ANSI/regex/statline parsing when GMCP is unavailable. **MSDP not implemented** (GMCP-only best-effort).
- **Gaps:** lethal/death/slain text triggers not yet wired to avoidance recording beyond flee-driven paths; combo sync is heal-focused (no full DPS/tank action matrix), relies on IPC/broadcast timing; GMCP package coverage and room/vitals mapping are best-effort and need live validation; avoidance/risk state is volatile (not persisted per host/realm).
- Verification: `python -m py_compile apexforge.py main.py analytics.py navigation.py coordination.py import_megamud.py` → `PY=0`. Commit/push intentionally not performed.

### 2026-11-11T19:34Z (session, UI grid + safety/self-heal groundwork)
- `main.py`: rebuilt Textual dashboard to MegaMUD/zMUD-inspired 2x4 grid — `[1]` primary server stream (`RichLog`, high-throughput), `[2]` comms/alerts/events log (separate, scrollable), `[3]` automapper (room name/id/zone, obvious exits, ASCII `NetworkX` map), `[4]` party/IPC telemetry, `[5]` analytics & monitor, `[6]` full-width HP/MANA/XP-state progress bars above input (HP color-thresholded green/yellow/red), `[7]` rigid bottom command input with active `Q: P1/P2/P3` queue badge. Added keybinds `F1..F12` (flee/stop/map/status/recalc/where/@health/look/rest/stand/loot/inventory) plus `f/r/q`.
- `apexforge.py`: `get_map_snapshot()` now reports room `exits`; parser captures `Obvious/Visible/Apparent exits` and uses more resilient normalized room-name arrival matching. Added `STATE_RECOVERY` (PvP/control/spin-out) with `flush_command_queue()` (P2/P3 drain), prompt-locked recovery entry/unlock hooks, and best-effort P1 control interceptor (config `parsing.patterns` keys + conservative fallbacks for stun/blind/freeze/drag/forced teleport). Movement/path stepping is blocked while in recovery.
- `apexforge.py`: inbound stream now uses prompt-bounded line/chunk accumulation (`_process_stream_chunk`) to better handle multi-line/packet splits and prompt-only fragments before line dispatch; P1 intercept evaluated on chunk/line. Dispatcher combat pacing now consults adaptive tick delay.
- `analytics.py`: added dynamic combat tick/burst tracking (`record_combat_burst`, EMA tick/cast-delay helpers, round-marker timing) to adapt to server-specific round speeds (replacing rigid assumptions over time).
- `navigation.py`: route steps now carry conditional edge metadata when present (`requires_lockpick/lockpick_cmd/required_item_id|required_key/min|max monsters/closed/edge_meta`) for self-healing/path-condition handling. `apexforge.py` step dispatch performs best-effort pre-move lockpick/required-item checks and emits `edge_condition_blocked` when unmet.
- **Blind spots (documented, partial):** PvP/control recovery + queue flush scaffolded but needs server-specific patterns and status-confirm prompts; prompt-bounded chunking added but full packet-safe trigger engine/lookahead pending; dynamic tick tracking added but combat/cast scheduling not fully refactored to it; conditional edge metadata supported in routing/dispatch but `database.json` schema migration and automated scan/heal/mutation on failure are pending.
- Verification: `python -m py_compile apexforge.py main.py analytics.py navigation.py coordination.py import_megamud.py` → `PY=0`. Commit/push intentionally not performed.

### 2026-11-11T18:58Z (session, UI/UX & MegaMUD-zMUD-Mudlet alignment)
- `main.py`: expanded Textual dashboard toward MegaMUD/zMUD/Mudlet-inspired layout — added **Party/Coordination** panel, split **Server/Engine Stream** and **Comms/Events** logs, enlarged interactive controls (Flee/Stop/Refresh/Status/Recalc/Where/@health plus Look/Rest/Stand/Loot/Inventory/Wealth buttons) and command input for raw/`@admin` control; event routing now surfaces comms/party/state/admin/alerts into comms log and refreshes on party/cast/lap events.
- `apexforge.py`: added config-driven **aliases** and **gag_patterns** (zMUD/Mudlet-style convenience) with alias expansion in `inject_command`; stream emit now carries `gagged` flag; telepath/page lines emit `comms` events for UI split/routing. (Step 2/UX groundwork.)
- Research baseline incorporated (documented in Paradigm mapping): MegaMUD (state-machine/path loops, party telemetry, checkbox automation, diagnostics), zMUD/cMUD (triggers/aliases/classes, MDI/split windows, automapper, scripting), Mudlet (async high-throughput, GMCP/MSDP/MXP/MSP OOB, Geyser-style GUI/gauges/split, Lua/scripts, mapper, multi-session).
- Verification: `py_compile` passed (`apexforge.py`, `main.py`); engine init and prior offline smokes remain consistent.

### 2026-11-11T18:52Z (session, UI interactivity)
- `main.py` (Textual dashboard): added interactive **User Controls & Interaction** area with feature buttons (Flee, Stop, Refresh Map, Status, Recalc Route, Send @health) and a command `Input` for sending raw commands or `@admin`/engine commands; UI command handler routes `@...` through engine admin handling and queues other input as commands, with response logging.
- `read_me.MD`: recorded UI interactivity requirement and this change; TODO updated to reflect baseline interactive controls while noting richer docks/split views/gauges remain planned.

### 2026-11-11T17:24Z (session, design mapping)
- `read_me.MD`: added **Paradigm mapping** section translating MegaMUD (state-machine/path loops, party sync, diagnostics, checkbox automation), zMUD/cMUD (multi-line triggers/classes, stream splitting, automapper), and Mudlet (async/event-driven UI, GMCP/OOB, sandboxed scripting, resiliency) into ApexForge’s current implementation, target gaps, and a unified profile/UI-layout direction (JSON today; schema-validated/YAML ergonomics as target).

### 2026-11-11T16:28Z (session, continued)
- `apexforge.py` (Step 1/3/2): added follower-mode wiring (`is_follower`, bus writer/task state) and CLI flags `--follower`, `--leader-host`, `--leader-port`; follower lifecycle connects to leader IPC bus, handles leader `ATTACK/PARTY_CMD/FLEE/RETREAT/STOP` via `handle_party_state`, and reports status/heartbeats/room/vitals back to leader. Leader broadcasts `MOVE` on room arrival and `PATH` on path start; path start now normalizes consecutive duplicate `loop_nodes` and rejects self-loop/unroutable segments with clearer validation.
- `apexforge.py` (Step 2): added config-driven parsing hooks (`parsing.patterns`) for mob/player counts, also-here overrides, round cues, economy/corpse/lock/door/attack/immunity events with best-effort end-of-round cast flush scheduling.
- `apexforge.py` (Step 3/status): `@status` now includes `immunity_count`, `party_status`, and `party_waiting`; follower vitals parsing reports status to bus.
- Verification: `py_compile` passed for updated core modules; offline smokes confirmed follower `ATTACK/PARTY_CMD` handling and status payloads, config-pattern mob-count parsing, vitals parsing/status bus reporting, and enriched `@status` fields.

### 2026-11-11T16:22Z (session)
- `coordination.py`: added `ApexForgePartyFollowerClient` (flexible callback/object handler, sends follower status/hello and forwards responses) alongside existing follower node/bus primitives.
- `apexforge.py`: added follower/party status-sync handling path in `handle_admin_bus_command` (accepts known party/admin/leader status updates, updates party status/wait flags; control commands remain restricted).
- `apexforge.py`: `start_path` now validates loop continuity/routability across adjacent `loop_nodes` before starting and emits `route_failed` with reason/details on unroutable segments.
- `main.py`: dashboard vitals/analytics panels extended to show MV, need XP, login/bless state, active status effects, and party-wait flags.
- `navigation.py`: fixed `render_ascii_map` to actually place room markers in the grid and cap render window size for readability.
- `ultra_orchestrator.ps1`: improved Python runtime resolution (`$pyExe` with PATH/fallback) and use it for dependency checks and launch.
- Verification: `py_compile` passed for updated modules; offline smokes confirmed unroutable imported path rejection and valid path routing start; dashboard/coordination edits compile.

### 2026-11-11 (prior recorded milestones in workspace state)
- Core engine/subsystems established: async engine, priority dispatcher, mapper/analytics/coordination, Textual dashboard baseline, orchestrator, `requirements.txt`.
- MegaMUD-aligned tuning: login/BBS scripting, statline/prompt/message-trigger parsing, vitals/Need/status tracking, party wait/heal/coordination hooks, combat/navigation/recovery/economy hooks, expanded admin command set, per-step path overrides.
- Data/import: MegaMUD importer added; profiles/paths/messages enriched; `database.json` expanded via `.mp`-derived traversals (large room/exit graph); immunity DB support added.
- Verification (recorded): module `py_compile` and multiple offline smoke checks passed at prior checkpoints.

## TODO / next steps
- **Step 1 follow-up (reachability):** 1022 paths are `manual_review_unreachable` (no safe cardinal inference/known intermediates). Resolve via authoritative MDB2/RoomID data, server-verified hops, or explicit conditional/waypoint metadata; re-run `--validate-paths` and add deterministic route/loop smokes for repaired paths.
- **Importer safety:** add/maintain `--merge-only`/staging behavior so re-imports never overwrite repaired edges/edge_meta or tuned `profiles.json`/`messages.json` (`*.imported.json`), and keep `graph_validated:true` locked unless forced.
- **Step 3 follow-up:** validate `resolve_room` hash/name+exit behavior against live strings; persist learned room aliases/hashes and host-fingerprinted overlays.
- Deepen P0 deterministic parsing: config-driven/fixture-tested statline/prompt, room/mob/player/“Also here”/exits, round-end, corpse/loot/economy/shop/bank/stash/inventory/`@uses`, lockpick/door outcomes, immunities, and `RECOVERY` cues per target game strings.
- Complete P0 party/follower semantics: reliable `ATTACK/PARTY_CMD/FLEE/RETREAT/STOP` handling, `@wait/@ok/@health` across variants, authorization/reconnect/heartbeat, low-latency multi-session bridging, and leader/healer combo validation.
- Harden P0 combat/spell scheduler: tick-aware scheduling as primary, end-of-round accuracy, Pre/Attack/Mult/Spl chains, mana%/cast/dmg caps, BS/alt/shield swaps, bless/affliction/vampiric retargeting, and immunity-aware targeting.
- P1 robustness & config: schema validation/migration for profiles/paths/db/items/messages, richer Textual UI (party/diagnostics views, editors), packet-stitch/stream hardening with chunked fixture tests, and orchestrator/env refinements.
- **UI/UX (critical, pending):** build a fully interactive, intuitive Textual interface (not headless-only): panels/views for stream, comms, map, party/telemetry, diagnostics (HP/MP/XP/hr, RTK/RTC, wealth, lap timers), and controls/editors for profiles/paths/triggers/commands with keybindings, status bars, and safe command input—targeting MegaMUD/MegaMMUD-style usability while remaining ApexForge-native.
- **Paradigm unification (MegaMUD + zMUD/cMUD + Mudlet):** converge on schema-validated profiles with automation states, nested trigger/alias classes and variables, event listeners/hooks (incl. future GMCP callbacks), and declarative UI layout/stream splitters; keep JSON canonical now with a migration path to ergonomic YAML.
- **Parsing/stream fidelity:** implement packet-safe multi-line trigger/alias engine with capture persistence, stream routers (comms/combat/system) and gag/routing rules, and input-buffer protection.
- **Navigation/mapping:** live topology refinement, stronger room-exit validation, conditional-step/door/lock recovery improvements, and longer-term clickable/richer map.
- **Protocols & extensibility:** add GMCP/MSDP/MXP/MSP negotiation/pipelines, sandboxed scripting runtime (permissioned), and optional package-style modularization of triggers/UI/hooks.
- **Party/session resiliency:** harden reconnect/identity/auth and multi-session/“bouncer-like” recovery behaviors.
- Decode MegaMUD MDB2/Jet `.md` binaries (Items/Monsters/Spells/Paths/Races/Classes/Macros) and cross-validate derived `database.json`/`paths.json` against authoritative sources.
- Capture live/representative server output to finalize regex/statline/prompt/party/economy/inventory strings and add deterministic parser/path fixtures.

## Known limitations / blockers
- **MegaMUD MDB2 binaries not decoded** (`Items/Monsters/Spells/Paths/Races/Classes/Macros.md` under `Default/`): full-fidelity data requires reverse-engineering or an exporter.
- **Game/server-specific strings unknown/incomplete**: live prompt/statline variants, room/mob/“Also here” formats, telepath/page forms, lockpick/door/lost/corpse/economy/shop/bank/attack/immunity confirmations vary and require tuning/fixtures.
- **Control/PvP & resiliency gaps (partially scaffolded):** `STATE_RECOVERY`/P1 intercept/queue-flush and prompt-bounded chunking are best-effort; stuns/blinds/freezes/drags/forced teleports/arena spin-outs need server-specific patterns, verified status-prompt unlock rules, and reconnect/session-persistence hardening to run unattended.
- **Anti-loop/avoidance (scaffolded):** rolling 15m failure tracking + 9999 edge penalty/restore on >=3 failures is wired for flee events, but lethal death/slain/teleport-back detections, avoidance-aware reroute selection, and per-host persistence are incomplete.
- **Party combo sync (scaffolded):** role/`hp_pct` telemetry and HEALER P1 assist on leader COMBAT+low HP exist; full role matrices, coordinated targeting/aggro, and timing/authority guarantees under lag or reconnect are incomplete.
- **OOB (scaffolded):** GMCP negotiation and best-effort vitals/room mapping with regex fallback exist; package coverage is limited, **MSDP/MXP/MSP are not implemented**, and GMCP-derived state must be validated per server.
- **Parsing/timing robustness:** multi-line wraps, terminal pagination, and packet splits are only partially handled (prompt-bounded accumulator added); dynamic combat-tick tracking is recorded but not yet the primary scheduler for all combat/cast actions.
- **Derived graph not authoritative / self-healing incomplete:** `database.json` exits largely derived from `.mp` traversals; conditional edge metadata and basic dispatch hooks exist, but conditional-edge schema coverage, runtime graph mutation/exit-scanning on mismatch, fuzzy room-description healing, and host-fingerprinted learned overlays are not yet implemented.
- **Not implemented (yet)**: proxy/bouncer session shielding, sandboxed third-party scripting runtime, rich TUI docks/clickable maps beyond current baseline.
- **Security/safety**: imported/reference BBS/profile data may contain sensitive values in local files; treat configs as local secrets where applicable.

## Paradigm mapping (MegaMUD / zMUD-cMUD / Mudlet -> ApexForge)

### 1) MegaMUD — autonomous state-machine & path-loop engine
| “Feel” / capability | ApexForge translation (current) | Target / gaps |
|---|---|---|
| Node-based path loops, lap timing, safe reroute/fallback | `navigation.py` weighted DiGraph + Dijkstra; `apexforge.py` `start_path`/loop advance, lap stats, dynamic reroute, risk marking, resync/orientation recovery, per-step overrides (`paths.json`) | Full `go`/special-step semantics, authoritative graph repair for imported `.mp` paths, richer door/lock/lost recovery |
| Leader/follower party sync (`@wait/@ok/@health`), multi-box coordination | IPC bus (`coordination.py`), leader broadcasts (MOVE/PATH/ATTACK/PARTY_CMD/FLEE), follower bus client/`handle_party_state`, status heartbeats, wait-flag blocking | Robust reconnect/identity/auth, low-latency multi-session bridging, complete assist/heal/report semantics across game variants |
| Self-heal/nuke safeguards, end-of-round/threshold automation | Survival thresholds, auto-heal/bless/cure hooks, nuke safety vs non-party, priority queue (P1/P2/P3), combat/rest/nav states, config-driven booleans | Deeper spell/combat scheduler (pre/attack/multi/spl, mana%/caps, swaps, vampiric retarget, immunity-aware targeting), accurate round boundaries |
| Diagnostics docks (HP/MP, XP/hr, lap, wealth) | `analytics.py` (XP/hr, RTK/RTC, walk/lap, time, BiS, immunity); Textual dashboard panels (vitals/analytics/map/queue) | Dedicated party monitor & diagnostics/status-grid docks, wealth/ULL and loop timers surfaced prominently (MegaMUD-style) |
| Checkbox/form configuration (no scripting required) | Profile-driven JSON flags/thresholds/commands/templates in `profiles.json` | Schema validation/migration, clearer “checkbox” automation sections; optional YAML profile support mirroring the unified paradigm |

### 2) zMUD / cMUD — power-user parsing, classes & windowing
| “Feel” / capability | ApexForge translation (current) | Target / gaps |
|---|---|---|
| Priority triggers/aliases/macros, multi-line/packet-safe parsing | Compiled regexes, statline template compiler, `parsing.patterns`, `messages.json` triggers, ANSI strip, line buffer + prompt handling | True multi-line stream stitcher/lookahead, priority trigger/alias/macro/keybind engine with capture persistence across packet splits |
| Trigger/variable “classes” (toggle groups) | Ad-hoc config groups (survival/spells/combat/navigation/automation/parsing) | Nested trigger classes with enable/disable by state/room/stance, first-class variable store and class toggles |
| Split windows (chat/combat/guild/tells) & stream routing | Single stream log in dashboard; events emitted by type | Stream routers/split docks (comms vs combat vs system), gagging/routing rules, input-buffer protection |
| Automapper / topology awareness | `navigation.py` ASCII map, zone/risk visualization, room graph in `database.json` | Live topology refinement from movement/exits, richer map panel; optional graphical/clickable map (longer-term) |

### 3) Mudlet — modern speed, events & extensible UI/scripting
| “Feel” / capability | ApexForge translation (current) | Target / gaps |
|---|---|---|
| High-throughput async core & modern TUI | Async engine/queues, Textual dashboard, event listeners/emission | Performance profiling under heavy spam, richer responsive panel layout (Geyser-like docks/gauges) |
| Event-driven hooks & OOB data (GMCP/MSDP/MXP/MSP) | Internal event bus + IPC JSON state; webhook alerts | Telnet negotiation and GMCP/MSDP/MXP/MSP pipelines feeding state/triggers/UI (no raw-text scraping when OOB available) |
| Embedded scripting & modular packages (safe) | Config/command-template driven behavior (no user script runtime yet) | Sandboxed scripting runtime (Python/Lua) with explicit permissioned FS/network/system access, event hooks, composable packages |
| Network resiliency & session safety | Basic connect/read loop, reconnect primitives limited | Proxy/bouncer-style session persistence, auto-recovery/defensive actions on drop, reconnect policy |

### Unified profile/layout direction (target)
- Evolve toward a unified, schema-validated profile expressing: **automation states** (MegaMUD-style flags/thresholds), **trigger classes/aliases** (zMUD-style), **event listeners/hooks** (Mudlet-style, incl. GMCP callbacks), and **UI layout/stream splitters** (panels, comms dock, diagnostics grid). Current canonical format is JSON; YAML and the example structure above are target ergonomics to converge on without breaking existing configs.

## Design intent / alignment notes
- Prioritize **deterministic, testable, config-driven behavior** over hardcoded game assumptions.
- Keep ApexForge-native config/schema canonical; treat MegaMUD imports as optional migration/reference inputs.
- Favor **async, non-blocking** processing and bounded resource use (queues, history/DB limits, map render caps).

---
*Maintainer note: update the Changes and TODO sections with timestamps on each meaningful iteration.*

## Competitive & Paradigm Benchmark

Baseline comparison (as of 2026-11-11, from code/docs inspection in D:\ApexForge and external references reviewed this session; not live-validated against a MajorMUD server).

| Capability / area | ApexForge (current) | MegaMUD 1.03x / MegaMud Online | MegaMMUD v2 (mudinfo, drop-in exe/dll) | OmegaMUD (Java) | zMUD/cMUD paradigm | Mudlet paradigm |
|---|---|---|---|---|---|---|
| Transport & session | Async TCP/ANSI, BBS/login hooks, prompt-bounded accumulator, GMCP negotiation scaffold + regex fallback | Telnet/BBS, stable legacy runtime | Drop-in client runtime, updated BBS/prompts/compat fixes | Telnet + ANSI/terminal, command throttle/queue | Mature telnet, robust session handling | High-throughput async, GMCP/MSDP/MXP/MSP, proxy/bouncer common |
| Parsing & triggers | Compiled/config-driven regex, statline/messages, exits/comms cues, aliases/gags (hooks) | Extensive tuned game triggers/paths | Known prompt/exit/equip/encumbrance behavior parity target | Parsed statline/rooms/inventory/stats/exp/shops/spells/party/who/chat/combat (reported) | Priority triggers/aliases/macros, trigger classes, multi-line/packet-safe | Event-driven pipelines, rich trigger/temp/regex, OOB-first when available |
| Room identity & mapping | NetworkX DiGraph, Dijkstra, ASCII map, derived 6644/12394 graph (non-authoritative) | .mp paths/loops, mature RoomID/path ecosystem | Bundled stock paths, improved pathfinding loops (reported) | **MegaMUD RoomID generation** (backward compat), MME DB open/query | Automapper/topology, config-driven room matching | Advanced mapper, GMCP room events, scriptable map |
| Navigation & safety | Loop/lap/reroute, risk, conditional-edge/gate hooks, 9999 avoidance penalty (15m, >=3), RECOVERY/P1 queue flush | Reliable path loops, timing/door handling (matured over time) | Pathing/compat refinements | Command pacing/automation focus | Robust movement/trigger recovery patterns | Scriptable recovery, resilient event handling |
| Combat & automation | Tri-tier queue (P1/P2/P3), survival/rest/heal hooks, EMA tick tracking (partial), combat states | State-machine automation, end-of-round/threshold behaviors | Parity target for timing/equip/edge cases | Basic combat parsing/actions (reported) | Deep trigger/macro-driven combat scripting | Fully scriptable/evented combat systems |
| Party/multi-box | IPC bus, follower client/sync, role/hp_pct telemetry, HEALER P1 assist scaffold | Leader/follower coordination (established) | Multi-window/client workflows | Remote commands @health/@exp/@join/@do/@party | Multi-session scripting patterns | Multi-profile/session + script coordination |
| Data & import | JSON configs, import_megamud.py (INI/Rooms.md/Messages.md/.mp); MDB2 .md not decoded | Native .md/.mdb/.mp, editors/utilities | Native MegaMUD data/paths, Paramud compat | MME DB integration (reported) | Profile/trigger data portability | Packages/profiles, extensible formats |
| UI/UX | Textual 2x4 (stream/comms split, map, vitals bars, queue badge, F-keys/input) | Classic Windows GUI/forms/docks | Classic Windows GUI (drop-in) | Java GUI (info panels, input modes, inventory) | MDI/split windows, editors | Modern GUI, Geyser gauges, clickable map, packages |
| Extensibility | Config/regex/command-template driven, event hooks; no sandboxed runtime yet | Limited (non-scripted core) | Limited (client) | Code-level (Java) | Scripting/aliases/triggers (rich) | **Lua scripting**, packages, APIs (sandbox expectations vary) |
| Maturity / live readiness | **Prototype/core** — strong scaffolding, needs live fixtures/tuning and authoritative data | **Mature/battle-tested** for MajorMUD workflows | **Mature drop-in** (community-maintained) | **Functional replacement** (scope per project) | **Mature** general client | **Mature** modern client |

### Architectural baselines (locked)
- Async, deterministic, config-driven core with tri-tier priority dispatch (P1 survival/interrupt, P2 combat, P3 utility) and prompt-bounded stream accumulation.
- ApexForge-native JSON schema canonical; MegaMUD/MDB/.mp imports are reference/migration inputs (not runtime truth).
- NetworkX weighted directed graph as navigation source; safety via RECOVERY, avoidance penalties, conditional edges, and evented observability/IPC.

### Gap tracking (active)
- **Data authority:** decode/export MegaMUD MDB2/.md (and MDB variants), implement/validate **MegaMUD RoomID hash**, reconcile derived graph/paths against canonical data.
- **Parsing fidelity:** packet-safe multi-line trigger engine, per-realm prompt/statline/exit/encumbrance/arena fixtures (incl. [PARADIGM]:, None exits), adaptive-tick-driven combat/cast.
- **Self-healing/persistence:** host-fingerprinted overlays (learned rooms, avoidance, items/vitals), fuzzy room healing + runtime graph mutation/exit-scan, reconnect/session resilience.
- **Parity/productization:** OOB (GMCP/MSDP/MXP/MSP) depth, sandboxed scripting/packages, richer UI/editors, role-matrix party automation, live validation harness.
