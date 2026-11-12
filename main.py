import argparse
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, RichLog, Select, Static
from textual import on

from apexforge import ApexForgeEngine


class ApexForgeDashboard(App):
    TITLE = "ApexForge MUD Client"
    SUB_TITLE = "Telnet — Clean Classic MUD Layout"
    BINDINGS = [
        ("q", "app_quit", "Exit"),
        ("f", "emergency_flee", "Panic Flee"),
        ("r", "refresh_map", "Refresh Map"),
        ("f1", "emergency_flee", "F1 Flee"),
        ("f2", "ui_stop", "F2 Stop"),
        ("f3", "refresh_map", "F3 Map"),
        ("f4", "ui_status", "F4 Status"),
        ("f5", "ui_recalc", "F5 Recalc"),
        ("f6", "ui_where", "F6 Where"),
        ("f7", "ui_health", "F7 @health"),
        ("f8", "ui_look", "F8 Look"),
        ("f9", "ui_rest", "F9 Rest"),
        ("f10", "ui_stand", "F10 Stand"),
        ("f11", "ui_loot", "F11 Loot"),
        ("f12", "ui_inventory", "F12 Inv"),
        ("m", "toggle_mapper", "Toggle Mapper"),
        ("c", "toggle_chat", "Toggle Chat"),
    ]

    CSS = """
    Screen { background: #0b1017; color: #d7dce2; }

    /* Offline connection view */
    #offline_view { height: 1fr; padding: 1 2; }
    #offline_view.hidden { display: none; }
    #offline_banner { color: #e0af68; text-style: bold; text-align: center; height: 3; }
    #offline_hint { color: #a9b1d6; text-align: center; height: 1; margin-bottom: 1; }
    #profile_list { height: 1fr; overflow-y: scroll; }
    .profile-card { layout: horizontal; height: 5; border: solid #2b3a4d; margin: 0 0 1 0; padding: 0 1; background: #141b24; }
    .profile-info { width: 1fr; }
    .profile-name { color: #7dcfff; text-style: bold; }
    .profile-detail { color: #a9b1d6; }
    #manual_connect { height: auto; border: solid #2b3a4d; padding: 0 1; margin-top: 1; background: #141b24; }
    #manual_host { width: 18; }
    #manual_port { width: 8; }
    #manual_name { width: 16; }
    #manual_status { color: #ff9e64; height: 1; }

    /* Online classic layout */
    #root_grid {
        layout: grid;
        grid-size: 2 3;
        grid-columns: 3fr 1fr;
        grid-rows: 1fr auto auto;
        height: 1fr;
    }
    #root_grid.hidden { display: none; }

    /* [1] MAIN OUTPUT — left, largest, ALL telnet traffic */
    #stream_box { column-span: 1; row-span: 1; }
    #server_stream_log { height: 1fr; background: #080c12; border: tall #202938; scrollbar-background: #141b24; }

    /* Right sidebar — mapper + party stacked */
    #right_panel { column-span: 1; row-span: 1; height: 1fr; overflow-y: scroll; }
    #mapper_box { border: solid #2b3a4d; padding: 0 1; margin-bottom: 1; height: 1fr; }
    #party_box { border: solid #2b3a4d; padding: 0 1; margin-bottom: 1; height: 1fr; }

    /* [6] STATUS BAR — full width, compact, between output and input */
    #status_bar { column-span: 2; row-span: 1; height: auto; border: solid #2b3a4d; padding: 0 1; }
    .status-row { layout: horizontal; height: auto; }
    .status-label { width: 10; color: #a9b1d6; text-align: right; content-align: right middle; }
    #hp_bar { width: 1fr; margin: 0 1; }
    #mp_bar { width: 1fr; margin: 0 1; }
    #state_text { width: 14; color: #7aa2f7; content-align: left middle; }
    #queue_badge { width: 12; color: #ff9e64; text-style: bold; content-align: right middle; }

    /* [7] COMMAND INPUT — full width bottom */
    #input_box { column-span: 2; row-span: 1; height: auto; border: solid #2b3a4d; padding: 0 1; }
    #cmd_input { width: 1fr; }
    #cmd_hint { color: #7aa2f7; height: 1; }

    .section-title { color: #e0af68; text-style: bold; text-align: left; padding: 0 1; }
    #room_info { color: #7dcfff; padding: 0 1; text-style: bold; }
    #exits_info { color: #9ece6a; padding: 0 1; }
    #map_text { color: #e0af68; padding: 0 1; }
    #party_panel { color: #d7dce2; padding: 0 1; }
    .party-member { padding: 0 1; color: #d7dce2; }
    """

    def __init__(self, engine: Optional[ApexForgeEngine] = None, connect: bool = False, auto_path: Optional[str] = None, auto_target: Optional[str] = None) -> None:
        super().__init__()
        self.connect_flag = connect
        self.auto_path = auto_path
        self.auto_target = auto_target
        self.engine = engine
        self.engine_task: Optional[asyncio.Task[Any]] = None
        self.log_widget = None
        self.comms_widget = None
        self._queue_depths = [0, 0, 0]
        self.profiles: List[Dict[str, Any]] = self._load_all_profiles()
        self.show_mapper = True
        self.show_chat = True
        self.regex_telepath = re.compile(r"^([A-Za-z]+)\s+(?:telepaths|says|tells you),?\s+\"([^\"]*)\"", re.IGNORECASE)
        self.regex_page = re.compile(r"^(\w+)\s+pages?:?\s*@(\w+)\s+(.+)$", re.IGNORECASE)
        # Equipment options loaded from items.json for autoget/autoequip drop-downs
        self.equip_options: Dict[str, List[tuple]] = {"weapon": [], "body": [], "offhand": []}
        self._load_equip_options()
        self._equip_selection: Dict[str, str] = {}

    def _load_equip_options(self) -> None:
        """Load weapon/armor/offhand items from items.json for drop-down selection."""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "items.json")
            data = json.load(open(path, encoding="utf-8"))
            items = data.get("items", {}) or {}
            for iid, item in items.items():
                slot = str(item.get("slot", "")).lower()
                name = str(item.get("name", iid))
                label = f"{name} ({iid})"
                if slot in self.equip_options:
                    self.equip_options[slot].append((label, iid))
            # Sort each slot list by name
            for slot in self.equip_options:
                self.equip_options[slot].sort(key=lambda x: x[0])
        except Exception as e:
            print(f"[!] Could not load equip options: {e}")

    def _load_all_profiles(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
            data = json.load(open(path, encoding="utf-8"))
            for name, cfg in (data.get("characters") or {}).items():
                entry = dict(cfg)
                entry["name"] = name
                out.append(entry)
        except Exception as e:
            print(f"[!] Could not load profiles: {e}")
        return out

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # --- Offline connection view ---
        yield Container(
            Label("ApexForge MUD Client — Telnet", id="offline_banner"),
            Static("Select a character to connect, or enter host/port manually.", id="offline_hint"),
            VerticalScroll(
                Label("Configured Characters", classes="section-title"),
                id="profile_list",
            ),
            Container(
                Label("Quick Connect (manual)", classes="section-title"),
                Horizontal(
                    Input(placeholder="host", id="manual_host", value="127.0.0.1"),
                    Input(placeholder="port", id="manual_port", value="23"),
                    Input(placeholder="character name", id="manual_name"),
                    Button("Connect", id="btn_manual_connect", variant="primary"),
                ),
                Static("", id="manual_status"),
                id="manual_connect",
            ),
            id="offline_view",
        )

        # --- Online classic MUD layout ---
        yield Grid(
            # [1] MAIN OUTPUT — left, largest, ALL telnet traffic
            Container(
                Label("SERVER STREAM (telnet)", classes="section-title"),
                RichLog(id="server_stream_log", highlight=False, markup=False, wrap=False, auto_scroll=True),
                id="stream_box",
            ),
            # Right sidebar — mapper + party
            VerticalScroll(
                Container(
                    Label("MAPPER", classes="section-title"),
                    Static("Room: — (—)", id="room_info"),
                    Static("Exits: —", id="exits_info"),
                    Static("[map unavailable]", id="map_text"),
                    id="mapper_box",
                ),
                Container(
                    Label("PARTY / CHAT", classes="section-title"),
                    Static("No party telemetry.", id="party_panel"),
                    id="party_box",
                ),
                Container(
                    Label("EQUIPMENT / AUTO-EQUIP", classes="section-title"),
                    Select(
                        self.equip_options.get("weapon", []),
                        id="sel_weapon",
                        prompt="Weapon",
                    ),
                    Select(
                        self.equip_options.get("body", []),
                        id="sel_armor",
                        prompt="Armor",
                    ),
                    Select(
                        self.equip_options.get("offhand", []),
                        id="sel_offhand",
                        prompt="Offhand",
                    ),
                    Horizontal(
                        Button("Auto-Equip", id="btn_auto_equip", variant="primary"),
                        Button("Auto-Get", id="btn_auto_get", variant="default"),
                    ),
                    Static("", id="equip_status"),
                    id="equipment_box",
                ),
                id="right_panel",
            ),
            # [6] STATUS BAR — full width compact
            Container(
                Horizontal(
                    Static("HP", classes="status-label"), ProgressBar(total=100, show_eta=False, id="hp_bar"), Static("-", id="hp_text", classes="status-label")),
                Horizontal(
                    Static("MANA", classes="status-label"), ProgressBar(total=100, show_eta=False, id="mp_bar"), Static("-", id="mp_text", classes="status-label")),
                Horizontal(
                    Static("STATE", classes="status-label"), Static("-", id="state_text", classes="status-label"), Static("Q: 0/0/0", id="queue_badge", classes="status-label")),
                id="status_bar",
            ),
            # [7] COMMAND INPUT — full width bottom
            Container(
                Horizontal(
                    Label(">"),
                    Input(placeholder="Type commands or @admin hooks...", id="cmd_input", disabled=True),
                    Button("Disconnect", id="btn_disconnect", variant="error"),
                ),
                Static("F1 Flee  F2 Stop  F3 Map  F4 Status  F5 Recalc  F6 Where  F7 @health  F8 Look  F9 Rest  F10 Stand  F11 Loot  F12 Inv  M Toggle Mapper  C Toggle Chat", id="cmd_hint"),
                id="input_box",
            ),
            id="root_grid",
            classes="hidden",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one("#server_stream_log", RichLog)

        # Build profile selector cards
        plist = self.query_one("#profile_list", VerticalScroll)
        plist.remove_children()
        plist.mount(Label("Configured Characters", classes="section-title"))
        for p in self.profiles:
            name = str(p.get("name", "?"))
            cls_race = f"{p.get('class','?')}/{p.get('race','?')}"
            lvl = p.get("level", "?")
            host = p.get("host", "127.0.0.1")
            port = p.get("port", 23)
            card = Container(
                Static(f"{name}", classes="profile-name"),
                Static(f"{cls_race}  Lvl {lvl}", classes="profile-detail"),
                Static(f"{host}:{port}", classes="profile-detail"),
                Button("Connect", id=f"btn_connect_{name}", variant="primary"),
                classes="profile-card",
                id=f"card_{name}",
            )
            plist.mount(card)

        if self.connect_flag and self.engine:
            self._start_engine_task(self.engine)
            self._switch_to_connected()
        else:
            self._log("[*] Dashboard mounted (offline). Telnet connection required.")
            try:
                self.query_one("#manual_host", Input).focus()
            except Exception:
                pass

    def _make_engine(self, profile_name: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None) -> ApexForgeEngine:
        eng = ApexForgeEngine(profile_name=profile_name, host=host, port=port)
        eng.add_event_listener(self._on_engine_event)
        return eng

    def _start_engine_task(self, eng: ApexForgeEngine) -> None:
        self.engine = eng
        self.engine_task = asyncio.create_task(self.engine.run_lifecycle(auto_path=self.auto_path, auto_target=self.auto_target, connect=True))

    def _switch_to_connected(self) -> None:
        offline = self.query_one("#offline_view", Container)
        grid = self.query_one("#root_grid", Grid)
        offline.add_class("hidden")
        grid.remove_class("hidden")
        try:
            self.query_one("#cmd_input", Input).disabled = False
            self.query_one("#cmd_input", Input).focus()
        except Exception:
            pass
        self._log("[*] Connected. Telnet stream active.")

    def _switch_to_offline(self) -> None:
        offline = self.query_one("#offline_view", Container)
        grid = self.query_one("#root_grid", Grid)
        grid.add_class("hidden")
        offline.remove_class("hidden")
        try:
            self.query_one("#cmd_input", Input).disabled = True
        except Exception:
            pass

    async def _stop_engine(self) -> None:
        if self.engine:
            self.engine.is_running = False
        if self.engine_task:
            try:
                self.engine_task.cancel()
            except Exception:
                pass
            self.engine_task = None

    async def _connect_profile(self, name: str) -> None:
        await self._stop_engine()
        prof = next((p for p in self.profiles if p.get("name") == name), None)
        if not prof:
            self._log(f"[!] Unknown profile: {name}")
            return
        host = str(prof.get("host", "127.0.0.1"))
        try:
            port = int(prof.get("port", 23) or 23)
        except ValueError:
            port = 23
        self._log(f"[*] Telnet connecting to {name} @ {host}:{port} ...")
        eng = self._make_engine(profile_name=name, host=host, port=port)
        self._start_engine_task(eng)
        self._switch_to_connected()

    async def _connect_manual(self) -> None:
        host = (self.query_one("#manual_host", Input).value or "127.0.0.1").strip()
        port_s = (self.query_one("#manual_port", Input).value or "23").strip()
        name = (self.query_one("#manual_name", Input).value or "manual").strip()
        try:
            port = int(port_s)
        except ValueError:
            port = 23
        self._log(f"[*] Telnet connecting to {host}:{port} as {name} ...")
        await self._stop_engine()
        eng = self._make_engine(profile_name=None, host=host, port=port)
        eng.profile_name = name
        eng.profile = eng._load_profile(name) or {}
        eng.host = host
        eng.port = port
        self._start_engine_task(eng)
        self._switch_to_connected()
        try:
            self.query_one("#manual_status", Static).update(f"Telnet connecting to {host}:{port}...")
        except Exception:
            pass

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn_disconnect":
            asyncio.create_task(self._do_disconnect())
        elif bid == "btn_manual_connect":
            asyncio.create_task(self._connect_manual())
        elif bid == "btn_auto_equip":
            asyncio.create_task(self._do_auto_equip())
        elif bid == "btn_auto_get":
            asyncio.create_task(self._do_auto_get())
        elif bid.startswith("btn_connect_"):
            name = bid[len("btn_connect_"):]
            asyncio.create_task(self._connect_profile(name))

    @on(Select.Changed)
    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id or ""
        value = event.value
        if value is None or value == Select.BLANK:
            return
        if sid == "sel_weapon":
            self._equip_selection["weapon"] = str(value)
        elif sid == "sel_armor":
            self._equip_selection["armor"] = str(value)
        elif sid == "sel_offhand":
            self._equip_selection["offhand"] = str(value)

    async def _do_auto_equip(self) -> None:
        if self.engine is None:
            self._equip_status_update("[!] Not connected.")
            return
        sel = getattr(self, "_equip_selection", {})
        injected = []
        if sel.get("weapon"):
            await self.engine.inject_command(3, f"wield {sel['weapon']}")
            injected.append(f"wield {sel['weapon']}")
        if sel.get("armor"):
            await self.engine.inject_command(3, f"equip {sel['armor']}")
            injected.append(f"equip {sel['armor']}")
        if sel.get("offhand"):
            await self.engine.inject_command(3, f"equip {sel['offhand']}")
            injected.append(f"equip {sel['offhand']}")
        if injected:
            self._equip_status_update(f"Auto-equip: {', '.join(injected)}")
        else:
            self._equip_status_update("No equipment selected.")

    async def _do_auto_get(self) -> None:
        if self.engine is None:
            self._equip_status_update("[!] Not connected.")
            return
        sel = getattr(self, "_equip_selection", {})
        injected = []
        for key in ("weapon", "armor", "offhand"):
            iid = sel.get(key)
            if iid:
                await self.engine.inject_command(3, f"get {iid}")
                injected.append(f"get {iid}")
        if injected:
            self._equip_status_update(f"Auto-get: {', '.join(injected)}")
        else:
            self._equip_status_update("No equipment selected.")

    def _equip_status_update(self, text: str) -> None:
        try:
            self.query_one("#equip_status", Static).update(text)
        except Exception:
            pass

    async def _do_disconnect(self) -> None:
        await self._stop_engine()
        self._switch_to_offline()
        self._log("[*] Disconnected. Telnet stream closed.")

    # ── telnet stream output ──
    def _log(self, message: Any) -> None:
        if self.log_widget is None:
            return
        try:
            if isinstance(message, str):
                self.log_widget.write(Text.from_ansi(message))
            else:
                self.log_widget.write(message)
        except Exception:
            try:
                self.log_widget.write(escape(str(message)))
            except Exception:
                pass

    def _ui_call(self, func: Any, *args: Any) -> None:
        try:
            self.call_from_thread(func, *args)
        except Exception:
            try:
                func(*args)
            except Exception:
                pass

    def _isolate_comms_line(self, raw_line: str) -> Optional[Dict[str, str]]:
        line = raw_line.strip()
        m = self.regex_telepath.search(line)
        if m:
            return {"kind": "telepath", "from": m.group(1), "text": m.group(2), "line": line}
        m = self.regex_page.search(line)
        if m:
            return {"kind": "page", "from": m.group(1), "to": m.group(2), "text": m.group(3), "line": line}
        return None

    def _on_engine_event(self, event: Dict[str, Any], engine: ApexForgeEngine) -> None:
        if self.engine is None:
            return
        etype = str(event.get("type", ""))
        payload = event.get("payload", {}) or {}

        if etype == "stream":
            if not payload.get("gagged"):
                line = payload.get("raw") or payload.get("line", "")
                isolated = self._isolate_comms_line(line)
                if isolated:
                    self._ui_call(self._log, f"[{isolated['kind'].upper()}] {isolated['from']}: {isolated['text']}")
                else:
                    self._ui_call(self._log, line)
        elif etype == "raw_send":
            text = payload.get("text", "")
            if text:
                self._ui_call(self._log, f"> {text}")
        elif etype == "dispatch":
            cmd = payload.get("command", "")
            pr = payload.get("priority", 3)
            if cmd:
                self._ui_call(self._log, f"> [P{pr}] {cmd}")
        elif etype == "login_complete":
            mp = payload.get("menu_prompt", "")
            self._ui_call(self._log, f"[*] Login complete ({mp})")
        elif etype in {"party_wait", "party_ok", "party_health", "party_health_msg", "admin_trigger", "stealth_signal", "route_failed", "state", "path_started", "cast_scheduled", "cast_flushed", "immunity_recorded", "burden_drop", "risk_marked", "orientation_recovery", "room_arrival", "exits", "lap_complete", "queue", "comms"}:
            self._ui_call(self._log, f"[{etype}] {payload}")

        if etype == "queue":
            pr = int(payload.get("priority", 3) or 3)
            if 1 <= pr <= 3:
                self._queue_depths[pr - 1] += 1
        elif etype == "dispatch":
            pr = int(payload.get("priority", 3) or 3)
            if 1 <= pr <= 3:
                self._queue_depths[pr - 1] = max(0, self._queue_depths[pr - 1] - 1)

        refresh_types = {"stream", "dispatch", "queue", "route_loaded", "route_failed", "route_complete", "room_arrival", "xp", "vitals", "mana_tick", "path_started", "map_update", "state", "stealth_signal", "admin_trigger", "comms", "party_wait", "party_ok", "party_health", "party_health_msg", "lap_complete", "cast_scheduled", "cast_flushed", "exits"}
        if etype in refresh_types:
            self._ui_call(self.refresh_dashboard_metrics)

    def refresh_dashboard_metrics(self) -> None:
        if self.engine is None:
            try:
                self.query_one("#hp_bar", ProgressBar).update(progress=0)
                self.query_one("#mp_bar", ProgressBar).update(progress=0)
                self.query_one("#hp_text", Static).update("-/-")
                self.query_one("#mp_text", Static).update("-/-")
                self.query_one("#state_text", Static).update("Offline")
                self.query_one("#room_info", Static).update("Room: — (—)")
                self.query_one("#exits_info", Static).update("Exits: —")
                self.query_one("#map_text", Static).update("[connect to see map]")
                self.query_one("#party_panel", Static).update("Not connected.")
                self.query_one("#queue_badge", Static).update("Q: 0/0/0")
            except Exception:
                pass
            return

        hp_pct = (self.engine.hp / self.engine.max_hp * 100) if self.engine.max_hp else 0.0
        mp_pct = (self.engine.mp / self.engine.max_mp * 100) if self.engine.max_mp else 0.0

        try:
            self.query_one("#hp_bar", ProgressBar).update(progress=max(0.0, min(100.0, hp_pct)))
            self.query_one("#mp_bar", ProgressBar).update(progress=max(0.0, min(100.0, mp_pct)))
            self.query_one("#hp_text", Static).update(f"{int(self.engine.hp)}/{int(self.engine.max_hp)}")
            self.query_one("#mp_text", Static).update(f"{int(self.engine.mp)}/{int(self.engine.max_mp)}")
            self.query_one("#state_text", Static).update(self.engine.state)
        except Exception:
            pass

        snap = self.engine.get_map_snapshot()
        room_name = snap.get("current_room_name") or "-"
        room_id = snap.get("current_room_id", "-")
        room = self.engine.mapper.raw_room_data.get(str(room_id), {}) if self.engine.mapper else {}
        zone = room.get("zone", "-")
        exits = snap.get("exits") or list((room or {}).get("exits", {}).keys())
        self.query_one("#room_info", Static).update(f"Room: {room_name} (#{room_id}) | {zone}")
        self.query_one("#exits_info", Static).update("Exits: " + (", ".join(exits) if exits else "—"))
        self.query_one("#map_text", Static).update(str(snap.get("ascii", "[map unavailable]")))

        pstatus = getattr(self.engine, "party_member_status", {}) or {}
        pwaits = getattr(self.engine, "party_wait_flags", {}) or {}
        is_leader = bool(getattr(self.engine, "is_leader", True))
        party_lines = [f"Role: {'LEADER' if is_leader else 'FOLLOWER'} | Leader: {getattr(self.engine, 'leader_name', '')}"]
        if pstatus:
            for name, info in sorted(pstatus.items()):
                hp = info.get("hp", "?"); mhp = info.get("max_hp", "?")
                mp = info.get("mp", "?"); mmp = info.get("max_mp", "?")
                st = info.get("state", "-"); room = info.get("room_id", "-")
                w = " [WAIT]" if pwaits.get(name) else ""
                party_lines.append(f"{info.get('name', name)}: HP {hp}/{mhp} MP {mp}/{mmp} {st} @{room}{w}")
        else:
            party_lines.append("No party telemetry yet.")
        if pwaits:
            waiting = ", ".join([k for k, v in pwaits.items() if v])
            party_lines.append(f"Waiting on: {waiting}")
        self.query_one("#party_panel", Static).update("\n".join(party_lines))

        qd = self.engine.command_queue.qsize()
        p1, p2, p3 = self._queue_depths
        total_tracked = p1 + p2 + p3
        if total_tracked != qd:
            if total_tracked > qd:
                overflow = total_tracked - qd
                for i in (2, 1, 0):
                    take = min(overflow, self._queue_depths[i])
                    self._queue_depths[i] -= take
                    overflow -= take
            p1, p2, p3 = self._queue_depths
        self.query_one("#queue_badge", Static).update(f"Q: {p1}/{p2}/{p3}")

    # ── input / commands ──
    async def _execute_ui_command(self, text: str) -> None:
        cmd = (text or "").strip()
        if not cmd:
            return
        if self.engine is None:
            self._log("[!] Not connected. Select a character first.")
            return
        self._log(f"[ui-cmd] {cmd}")
        try:
            if cmd.startswith("@"):
                sender = (getattr(self.engine, "profile_name", None) or "ui").lower()
                resp = await self.engine.handle_admin_bus_command({"from": sender, "text": cmd}, None)
                if resp:
                    try:
                        obj = json.loads(resp)
                        self._log(f"[ui-resp] {json.dumps(obj, ensure_ascii=False)}")
                    except Exception:
                        self._log(f"[ui-resp] {resp}")
                else:
                    self._log("[ui-resp] (no response)")
            else:
                await self.engine.inject_command(3, cmd)
                self._log(f"[ui] queued: {cmd}")
        except Exception as e:
            self._log(f"[ui-error] {e}")
        self.refresh_dashboard_metrics()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        event.input.value = ""
        if value:
            await self._execute_ui_command(value)

    async def action_emergency_flee(self) -> None:
        if self.engine is None:
            return
        await self.engine.inject_command(1, "flee")
        self._log("[ui] Injected P1 flee command")
        self.refresh_dashboard_metrics()

    async def action_ui_stop(self) -> None:
        if self.engine:
            await self._execute_ui_command("@stop")

    async def action_ui_status(self) -> None:
        if self.engine:
            await self._execute_ui_command("@status")

    async def action_ui_recalc(self) -> None:
        if self.engine:
            await self._execute_ui_command("@recalc")

    async def action_ui_where(self) -> None:
        if self.engine:
            await self._execute_ui_command("@where")

    async def action_ui_health(self) -> None:
        if self.engine:
            await self._execute_ui_command("@health")

    async def action_ui_look(self) -> None:
        if self.engine:
            await self._execute_ui_command("look")

    async def action_ui_rest(self) -> None:
        if self.engine:
            await self._execute_ui_command("rest")

    async def action_ui_stand(self) -> None:
        if self.engine:
            await self._execute_ui_command("stand")

    async def action_ui_loot(self) -> None:
        if self.engine:
            cmd = getattr(self.engine, "corpse_loot_command", None) or "get all corpse"
            await self._execute_ui_command(str(cmd))

    async def action_ui_inventory(self) -> None:
        if self.engine:
            await self._execute_ui_command("@inventory")

    async def action_refresh_map(self) -> None:
        if self.engine:
            self.engine.emit_event("map_update", self.engine.get_map_snapshot())
            self.refresh_dashboard_metrics()

    async def action_toggle_mapper(self) -> None:
        self.show_mapper = not self.show_mapper
        try:
            mapper = self.query_one("#mapper_box", Container)
            if self.show_mapper:
                mapper.remove_class("hidden")
            else:
                mapper.add_class("hidden")
        except Exception:
            pass
        self.refresh_dashboard_metrics()

    async def action_toggle_chat(self) -> None:
        self.show_chat = not self.show_chat
        try:
            party = self.query_one("#party_box", Container)
            if self.show_chat:
                party.remove_class("hidden")
            else:
                party.add_class("hidden")
        except Exception:
            pass
        self.refresh_dashboard_metrics()

    async def action_app_quit(self) -> None:
        await self._stop_engine()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_engine()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ApexForge dashboard")
    p.add_argument("--profile", help="Profile name to auto-connect on launch")
    p.add_argument("--connect", action="store_true", help="Auto-connect the --profile on launch")
    p.add_argument("--path", dest="path_key", help="Start configured path from paths.json")
    p.add_argument("--target", help="Route to target room id on start")
    p.add_argument("--ipc-bus", action="store_true", help="Start local IPC bus")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    engine = None
    if args.profile:
        engine = ApexForgeEngine(profile_name=args.profile)
        if args.ipc_bus:
            engine.start_ipc_bus = True
    app = ApexForgeDashboard(engine, connect=bool(args.connect), auto_path=args.path_key, auto_target=args.target)
    app.run()
