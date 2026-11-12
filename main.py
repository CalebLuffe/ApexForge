import argparse
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, RichLog, Static
from textual import on

from apexforge import ApexForgeEngine


class ApexForgeDashboard(App):
    TITLE = "ApexForge MUD Client"
    SUB_TITLE = "MegaMUD-style launcher — select a character to connect"
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
    ]

    CSS = """
    Screen { background: #0b1017; color: #d7dce2; }
    /* Online grid (Natural MUD Layout) */
    #root_grid {
        layout: grid;
        grid-size: 2 4;
        grid-columns: 2.2fr 1fr;
        grid-rows: 3fr 1.2fr auto auto;
        height: 1fr;
    }
    #root_grid.hidden { display: none; }
    #stream_box { column-span: 1; row-span: 1; }
    #server_stream_log { height: 1fr; background: #080c12; border: tall #202938; scrollbar-background: #141b24; }
    #comms_box { column-span: 1; row-span: 1; }
    #comms_log { height: 1fr; background: #0b121a; border: tall #22314d; scrollbar-background: #141b24; }
    #right_top_box { column-span: 1; row-span: 1; height: 1fr; }
    #mapper_box { height: 1fr; border: solid #2b3a4d; padding: 0 1; }
    #party_box { height: 1fr; border: solid #2b3a4d; padding: 0 1; }
    #analytics_box { column-span: 1; row-span: 1; height: 1fr; border: solid #2b3a4d; padding: 0 1; }
    .section-title { color: #e0af68; text-style: bold; text-align: left; padding: 0 1; }
    #room_info { color: #7dcfff; padding: 0 1; text-style: bold; }
    #exits_info { color: #9ece6a; padding: 0 1; }
    #map_text { color: #e0af68; padding: 0 1; }
    #vitals_box { column-span: 2; row-span: 1; height: auto; border: solid #2b3a4d; padding: 0 1; }
    .vitals-row { layout: horizontal; height: auto; }
    .vlabel { width: 14; color: #a9b1d6; text-align: right; content-align: right middle; }
    #hp_bar { width: 1fr; margin: 0 1; }
    #mp_bar { width: 1fr; margin: 0 1; }
    #xp_bar { width: 1fr; margin: 0 1; }
    #hp_bar.hp-good .bar--bar { color: #9ece6a; }
    #hp_bar.hp-mid .bar--bar { color: #e0af68; }
    #hp_bar.hp-low .bar--bar { color: #f7768e; text-style: bold; }
    #mp_bar .bar--bar { color: #7aa2f7; }
    #xp_bar .bar--bar { color: #bb9af7; }
    #hp_text { width: 12; color: #9ece6a; content-align: right middle; }
    #mp_text { width: 12; color: #7aa2f7; content-align: right middle; }
    #xp_text { width: 20; color: #bb9af7; content-align: right middle; }
    #input_box { column-span: 2; row-span: 1; height: auto; border: solid #2b3a4d; padding: 0 1; }
    #cmd_input { width: 1fr; }
    #queue_badge { color: #ff9e64; text-style: bold; width: 14; content-align: right middle; }
    #cmd_hint { color: #7aa2f7; height: 1; }
    .party-member { padding: 0 1; color: #d7dce2; }
    .party-wait { color: #ff9e64; text-style: bold; }
    .party-leader { color: #9ece6a; text-style: bold; }

    /* Offline launcher / server-browser view */
    #offline_view { height: 1fr; padding: 1 2; }
    #offline_view.hidden { display: none; }
    #offline_banner { color: #e0af68; text-style: bold; text-align: center; height: 3; }
    #offline_hint { color: #a9b1d6; text-align: center; height: 1; margin-bottom: 1; }
    #profile_list { height: 1fr; overflow-y: scroll; }
    .profile-card { layout: horizontal; height: 5; border: solid #2b3a4d; margin: 0 0 1 0; padding: 0 1; background: #141b24; }
    .profile-info { width: 1fr; }
    .profile-name { color: #7dcfff; text-style: bold; }
    .profile-detail { color: #a9b1d6; }
    .profile-connect { width: 22; }
    #manual_connect { height: auto; border: solid #2b3a4d; padding: 0 1; margin-top: 1; background: #141b24; }
    #manual_host { width: 20; }
    #manual_port { width: 8; }
    #manual_name { width: 16; }
    #manual_status { color: #ff9e64; height: 1; }
    """

    def __init__(self, engine: Optional[ApexForgeEngine] = None, connect: bool = False, auto_path: Optional[str] = None, auto_target: Optional[str] = None) -> None:
        super().__init__()
        self.connect_flag = connect
        self.auto_path = auto_path
        self.auto_target = auto_target
        self.engine = engine
        self.engine_task: Optional[asyncio.Task[Any]] = None
        self.log_widget: Optional[RichLog] = None
        self.comms_widget: Optional[RichLog] = None
        self._queue_depths = [0, 0, 0]  # P1,P2,P3
        self.profiles: List[Dict[str, Any]] = self._load_all_profiles()
        self.regex_telepath = re.compile(r"^([A-Za-z]+)\s+(?:telepaths|says|tells you),?\s+\"([^\"]*)\"", re.IGNORECASE)
        self.regex_page = re.compile(r"^(\w+)\s+pages?:?\s*@(\w+)\s+(.+)$", re.IGNORECASE)

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
        # --- Offline launcher / server-browser view ---
        yield Container(
            Label("ApexForge MUD Client — Server Browser", id="offline_banner"),
            Static("Select a character to connect to its configured BBS/server (profiles.json).", id="offline_hint"),
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
                    Button("Create Character", id="btn_create_character", variant="success"),
                ),
                Horizontal(
                    Input(placeholder="class", id="create_class", disabled=True),
                    Input(placeholder="race", id="create_race", disabled=True),
                    Input(placeholder="level", id="create_level", disabled=True),
                    Button("Save", id="btn_save_character", variant="success", disabled=True),
                    Button("Cancel", id="btn_cancel_character", variant="warning", disabled=True),
                ),
                Static("", id="manual_status"),
                id="manual_connect",
            ),
            id="offline_view",
        )
        # --- Online Natural MUD Layout Grid ---
        yield Grid(
            # [1] PRIMARY SERVER STREAM — left, largest, clean game text only
            Container(
                Label("[1] PRIMARY SERVER STREAM", classes="section-title"),
                RichLog(id="server_stream_log", highlight=False, markup=False, wrap=False, auto_scroll=True),
                id="stream_box",
            ),
            # Right column top — [3] VISUAL AUTOMAPPER stacked over [4] PARTY / IPC TELEMETRY
            Vertical(
                Container(
                    Label("[3] VISUAL AUTOMAPPER", classes="section-title"),
                    Static("Room: — (—)", id="room_info"),
                    Static("Obvious exits: —", id="exits_info"),
                    Static("[map unavailable]", id="map_text"),
                    id="mapper_box",
                ),
                Container(
                    Label("[4] PARTY / IPC TELEMETRY", classes="section-title"),
                    Static("No party telemetry yet.", id="party_panel"),
                    id="party_box",
                ),
                id="right_top_box",
            ),
            # [2] COMMS, ALERTS, & EVENTS — left, beneath stream
            Container(
                Label("[2] COMMS, ALERTS, & EVENTS", classes="section-title"),
                RichLog(id="comms_log", highlight=False, markup=False, wrap=False, auto_scroll=True),
                id="comms_box",
            ),
            # [5] ANALYTICS & MONITOR — right, beneath party
            Container(
                Label("[5] ANALYTICS & MONITOR", classes="section-title"),
                Static("", id="analytics_panel"),
                Static("", id="queue_panel"),
                id="analytics_box",
            ),
            # [6] VITALS BARS — full width, color shifting
            Container(
                Horizontal(Static("HP", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="hp_bar"), Static("-", id="hp_text", classes="vlabel")),
                Horizontal(Static("MANA", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="mp_bar"), Static("-", id="mp_text", classes="vlabel")),
                Horizontal(Static("XP / STATE", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="xp_bar"), Static("-", id="xp_text", classes="vlabel")),
                id="vitals_box",
            ),
            # [7] COMMAND INPUT — full width
            Container(
                Horizontal(
                    Label("[7] COMMAND INPUT: >"),
                    Input(placeholder="Type commands or @admin hooks...", id="cmd_input", disabled=True),
                    Static("Q: 0/0/0", id="queue_badge"),
                    Button("Disconnect", id="btn_disconnect", variant="error"),
                ),
                Static("F1 Flee  F2 Stop  F3 Map  F4 Status  F5 Recalc  F6 Where  F7 @health  F8 Look  F9 Rest  F10 Stand  F11 Loot  F12 Inv", id="cmd_hint"),
                id="input_box",
            ),
            id="root_grid",
            classes="hidden",
        )
        yield Footer()

    def on_mount(self) -> None:
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

        self.log_widget = self.query_one("#server_stream_log", RichLog)
        self.comms_widget = self.query_one("#comms_log", RichLog)

        # Auto-connect if --profile/--connect given; otherwise stay offline in server browser
        if self.connect_flag and self.engine:
            self._start_engine_task(self.engine)
            self._switch_to_connected()
        else:
            self._log("[*] Dashboard mounted (offline). Select a character to connect.")
            self._comms("[*] Server browser ready. Pick a character above, or use Quick Connect.")
            self.refresh_dashboard_metrics()
            try:
                self.query_one("#manual_host", Input).focus()
            except Exception:
                pass

    # ── connection management ──
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
        except Exception:
            pass
        self._log("[*] Connected. Game stream active.")
        self.refresh_dashboard_metrics()

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
        self._log(f"[*] Connecting to {name} @ {host}:{port} (BBS login script will run) ...")
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
        self._log(f"[*] Manual connect to {host}:{port} as {name} ...")
        await self._stop_engine()
        eng = self._make_engine(profile_name=None, host=host, port=port)
        eng.profile_name = name
        eng.profile = eng._load_profile(name) or {}
        eng.host = host
        eng.port = port
        self._start_engine_task(eng)
        self._switch_to_connected()
        try:
            self.query_one("#manual_status", Static).update(f"Connecting to {host}:{port}...")
        except Exception:
            pass

    # ── character creation ──
    def _toggle_create_form(self, show: bool) -> None:
        try:
            self.query_one("#create_class", Input).disabled = not show
            self.query_one("#create_race", Input).disabled = not show
            self.query_one("#create_level", Input).disabled = not show
            self.query_one("#btn_save_character", Button).disabled = not show
            self.query_one("#btn_cancel_character", Button).disabled = not show
        except Exception:
            pass

    async def _do_create_character(self) -> None:
        self._create_mode = not getattr(self, "_create_mode", False)
        self._toggle_create_form(self._create_mode)
        if self._create_mode:
            try:
                self.query_one("#create_class", Input).focus()
            except Exception:
                pass
            self.query_one("#manual_status", Static).update("Enter character details, then Save.")
        else:
            self.query_one("#manual_status", Static).update("")

    async def _save_character(self) -> None:
        name = (self.query_one("#manual_name", Input).value or "").strip()
        host = (self.query_one("#manual_host", Input).value or "127.0.0.1").strip()
        port_s = (self.query_one("#manual_port", Input).value or "23").strip()
        cls = (self.query_one("#create_class", Input).value or "").strip()
        race = (self.query_one("#create_race", Input).value or "").strip()
        lvl_s = (self.query_one("#create_level", Input).value or "1").strip()
        try:
            port = int(port_s)
        except ValueError:
            port = 23
        try:
            level = int(lvl_s)
        except ValueError:
            level = 1
        if not name:
            self.query_one("#manual_status", Static).update("Character name is required.")
            return
        if any(p.get("name") == name for p in self.profiles):
            self.query_one("#manual_status", Static).update(f"Character '{name}' already exists.")
            return
        profile = {
            "name": name,
            "class": cls,
            "race": race,
            "level": level,
            "host": host,
            "port": port,
            "leader_name": name,
            "admin_names": [name],
            "start_ipc_bus": False,
            "survival": {
                "flee_hp_percentage": 30.0,
                "rest_hp_percentage": 70.0,
                "emergency_hangup_on_pvp": False,
                "auto_rest_command": "rest",
                "auto_stand_command": "stand",
            },
            "spells": {
                "auto_heal": False,
                "heal_spell": "heal",
                "heal_hp_pct": 60,
                "auto_bless": False,
                "bless_spell": "cast bless",
                "auto_nuke": False,
                "nuke_spell": "",
            },
            "party": {
                "is_leader": True,
                "leader_name": name,
                "followers": [],
                "whitelist": [name],
                "hp_wait_threshold_pct": 50,
                "party_heal_enabled": False,
            },
            "combat": {
                "auto_combat": True,
                "attack_command": "attack",
                "current_profile": 1,
                "profiles": {"1": {"weapon": "", "attack_command": "attack"}},
            },
            "navigation": {
                "lockpick_enabled": False,
                "lockpick_command": "pick lock",
                "run_mode_suppress_combat": True,
            },
            "inventory_economy": {
                "active_mode": True,
                "auto_deposit": False,
                "keep_cash_copper": 0,
                "stash_room_id": "",
                "bank_room_id": "",
            },
            "automation": {
                "corpse_loot_command": "get all corpse",
                "interrupt_command": "bash",
                "spell_interrupt_command": "bash",
                "recovery_commands": ["look", "recall"],
                "burden_drop_threshold": 0.85,
                "max_carry_weight": 100.0,
            },
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            data = {"characters": {}}
        data.setdefault("characters", {})[name] = profile
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.profiles.append(profile)
        # Rebuild profile list
        plist = self.query_one("#profile_list", VerticalScroll)
        plist.remove_children()
        plist.mount(Label("Configured Characters", classes="section-title"))
        for p in self.profiles:
            pname = str(p.get("name", "?"))
            cls_race = f"{p.get('class','?')}/{p.get('race','?')}"
            lvl = p.get("level", "?")
            phost = p.get("host", "127.0.0.1")
            pport = p.get("port", 23)
            card = Container(
                Static(f"{pname}", classes="profile-name"),
                Static(f"{cls_race}  Lvl {lvl}", classes="profile-detail"),
                Static(f"{phost}:{pport}", classes="profile-detail"),
                Button("Connect", id=f"btn_connect_{pname}", variant="primary"),
                classes="profile-card",
                id=f"card_{pname}",
            )
            plist.mount(card)
        self._toggle_create_form(False)
        self.query_one("#manual_status", Static).update(f"Character '{name}' saved. Click Connect to log in.")

    async def _cancel_character(self) -> None:
        self._toggle_create_form(False)
        self.query_one("#manual_status", Static).update("Character creation cancelled.")

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn_disconnect":
            asyncio.create_task(self._do_disconnect())
        elif bid == "btn_manual_connect":
            asyncio.create_task(self._connect_manual())
        elif bid == "btn_create_character":
            asyncio.create_task(self._do_create_character())
        elif bid == "btn_save_character":
            asyncio.create_task(self._save_character())
        elif bid == "btn_cancel_character":
            asyncio.create_task(self._cancel_character())
        elif bid.startswith("btn_connect_"):
            name = bid[len("btn_connect_"):]
            asyncio.create_task(self._connect_profile(name))

    async def _do_disconnect(self) -> None:
        await self._stop_engine()
        self._switch_to_offline()
        self._log("[*] Disconnected. Returned to server browser.")

    # ── stream output (clean game text, ANSI preserved) ──
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

    # ── comms isolation (telepath/page/party/admin) ──
    def _comms(self, message: Any) -> None:
        if self.comms_widget is None:
            return
        try:
            if isinstance(message, str):
                self.comms_widget.write(escape(message))
            else:
                self.comms_widget.write(message)
        except Exception:
            pass

    def _isolate_comms_line(self, raw_line: str) -> Optional[Dict[str, str]]:
        """Client-side telepath/page isolation so vital comms are never lost in combat text."""
        line = raw_line.strip()
        m = self.regex_telepath.search(line)
        if m:
            return {"kind": "telepath", "from": m.group(1), "text": m.group(2), "line": line}
        m = self.regex_page.search(line)
        if m:
            return {"kind": "page", "from": m.group(1), "to": m.group(2), "text": m.group(3), "line": line}
        return None

    def _ui_call(self, func: Any, *args: Any) -> None:
        try:
            self.call_from_thread(func, *args)
        except Exception:
            try:
                func(*args)
            except Exception:
                pass

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
                    self._ui_call(self._comms, f"[{isolated['kind'].upper()}] {isolated['from']}: {isolated['text']}")
                else:
                    self._ui_call(self._log, line)
        elif etype == "raw_send":
            # Show outgoing client commands/responses in the main stream so the user sees their own input
            text = payload.get("text", "")
            if text:
                self._ui_call(self._log, f"> {text}")
        elif etype == "login_complete":
            mp = payload.get("menu_prompt", "")
            self._ui_call(self._log, f"[*] Login complete ({mp})")
        elif etype == "comms":
            kind = payload.get("kind", "comms")
            frm = payload.get("from", "")
            txt = payload.get("text", payload.get("line", ""))
            to = payload.get("to", "")
            self._ui_call(self._comms, f"[{kind.upper()}] {frm}{' -> '+to if to else ''}: {txt}")
        elif etype in {"party_wait", "party_ok", "party_health", "party_health_msg", "admin_trigger", "stealth_signal", "route_failed", "state", "path_started", "cast_scheduled", "cast_flushed", "immunity_recorded", "burden_drop", "risk_marked", "orientation_recovery", "room_arrival", "exits", "lap_complete", "queue", "dispatch"}:
            self._ui_call(self._comms, f"[{etype}] {payload}")
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

    # ── vitals: full-width color-shifting bars ──
    def _set_hp_color(self, pct: float) -> None:
        if self.engine is None:
            return
        bar = self.query_one("#hp_bar", ProgressBar)
        bar.remove_class("hp-good", "hp-mid", "hp-low")
        if pct < 30:
            bar.add_class("hp-low")   # blinking red
        elif pct < 60:
            bar.add_class("hp-mid")   # yellow
        else:
            bar.add_class("hp-good")  # green

    def refresh_dashboard_metrics(self) -> None:
        if self.engine is None:
            try:
                self.query_one("#hp_bar", ProgressBar).update(progress=0)
                self.query_one("#mp_bar", ProgressBar).update(progress=0)
                self.query_one("#xp_bar", ProgressBar).update(progress=0)
                self.query_one("#hp_text", Static).update("-/-")
                self.query_one("#mp_text", Static).update("-/-")
                self.query_one("#xp_text", Static).update("Offline | select a character")
                self.query_one("#room_info", Static).update("Room: — (—)")
                self.query_one("#exits_info", Static).update("Obvious exits: —")
                self.query_one("#map_text", Static).update("[select a character to connect]")
                self.query_one("#party_panel", Static).update("Not connected.")
                self.query_one("#analytics_panel", Static).update("Offline.")
                self.query_one("#queue_panel", Static).update("Queue: —")
                self.query_one("#queue_badge", Static).update("Q: 0/0/0")
            except Exception:
                pass
            return

        hp_pct = (self.engine.hp / self.engine.max_hp * 100) if self.engine.max_hp else 0.0
        mp_pct = (self.engine.mp / self.engine.max_mp * 100) if self.engine.max_mp else 0.0
        mv = getattr(self.engine, "mv", 0)
        max_mv = getattr(self.engine, "max_mv", 0)
        need = int(getattr(self.engine, "need_xp", 0) or 0)
        xp_proxy = 100.0 if need == 0 else max(0.0, min(99.0, 100.0 - min(99.0, need / 1000.0)))

        try:
            self.query_one("#hp_bar", ProgressBar).update(progress=max(0.0, min(100.0, hp_pct)))
            self.query_one("#mp_bar", ProgressBar).update(progress=max(0.0, min(100.0, mp_pct)))
            self.query_one("#xp_bar", ProgressBar).update(progress=xp_proxy)
            self._set_hp_color(hp_pct)
            self.query_one("#hp_text", Static).update(f"{int(self.engine.hp)}/{int(self.engine.max_hp)}")
            self.query_one("#mp_text", Static).update(f"{int(self.engine.mp)}/{int(self.engine.max_mp)}")
            self.query_one("#xp_text", Static).update(f"Need {need} | {self.engine.state}")
        except Exception:
            pass

        snap = self.engine.get_map_snapshot()
        room_name = snap.get("current_room_name") or "-"
        room_id = snap.get("current_room_id", "-")
        room = self.engine.mapper.raw_room_data.get(str(room_id), {}) if self.engine.mapper else {}
        zone = room.get("zone", "-")
        exits = snap.get("exits") or list((room or {}).get("exits", {}).keys())
        self.query_one("#room_info", Static).update(f"Room: {room_name} (#{room_id}) | Zone: {zone}")
        self.query_one("#exits_info", Static).update("Obvious exits: " + (", ".join(exits) if exits else "—"))
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
                role = info.get("role", "DPS")
                w = " [WAIT]" if pwaits.get(name) else ""
                leader_tag = " (LEADER)" if name.lower() == str(getattr(self.engine, "leader_name", "")).lower() else ""
                party_lines.append(f"[{role}] {info.get('name', name)}{leader_tag}: HP {hp}/{mhp} MP {mp}/{mmp} {st} @{room}{w}")
        else:
            party_lines.append("No party telemetry yet.")
        if pwaits:
            waiting = ", ".join([k for k, v in pwaits.items() if v])
            party_lines.append(f"Waiting on: {waiting}")
        self.query_one("#party_panel", Static).update("\n".join(party_lines))

        path_key = self.engine.active_path_key
        ta = self.engine.analytics.get_time_analysis()
        analytics_text = (
            f"XP 5m {self.engine.analytics.calculate_rolling_velocity(300):,.0f}/hr | "
            f"15m {self.engine.analytics.calculate_rolling_velocity(900):,.0f}/hr | Total {self.engine.analytics.total_xp_gained}\n"
            f"Path {path_key or '-'} | RTK {self.engine.analytics.average_rtk():.2f} | "
            f"RTC {self.engine.analytics.average_rtc():.2f} | Walk {self.engine.analytics.walk_speed():.2f}s\n"
            f"M/A/R {ta.get('moving_pct', 0):.1f}/{ta.get('attacking_pct', 0):.1f}/{ta.get('resting_pct', 0):.1f}% | "
            f"MV {mv}/{max_mv} | Blessed {bool(getattr(self.engine, '_blessed', False))}"
        )
        self.query_one("#analytics_panel", Static).update(analytics_text)

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
        queue_text = (
            f"Queue total: {qd} (P1/P2/P3 {p1}/{p2}/{p3}) | Pending: {self.engine.pending_arrival_room_id or '-'}\n"
            f"Steps: {len(self.engine.active_navigation_path)} | IPC: {self.engine.start_ipc_bus} | "
            f"Webhook: {'yes' if self.engine.coordination.webhook_url else 'no'}"
        )
        self.query_one("#queue_panel", Static).update(queue_text)

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
                self._log(f"[ui] queued command: {cmd}")
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
