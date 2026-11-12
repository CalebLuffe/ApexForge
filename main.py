import argparse
import asyncio
import json
from typing import Any, Dict, Optional

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal
from textual.widgets import Footer, Header, Input, Label, ProgressBar, RichLog, Static

from apexforge import ApexForgeEngine


class ApexForgeDashboard(App):
    TITLE = "ApexForge MUD Client Engine"
    SUB_TITLE = "MegaMUD/zMUD-inspired split view"
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
    #root_grid {
        layout: grid;
        grid-size: 2 4;
        grid-columns: 2.2fr 1fr;
        grid-rows: 3fr 1.2fr auto auto;
        height: 1fr;
    }
    #stream_box { row-span: 1; column-span: 1; }
    #mapper_box { row-span: 1; column-span: 1; }
    #comms_box { row-span: 1; column-span: 1; }
    #party_box { row-span: 1; column-span: 1; }
    #vitals_box { row-span: 1; column-span: 2; }
    #input_box { row-span: 1; column-span: 2; }
    Container { background: #141b24; border: solid #2b3a4d; padding: 0 1; }
    .section-title { color: #e0af68; text-style: bold; }
    #server_stream_log { height: 1fr; background: #080c12; border: tall #202938; }
    #comms_log { height: 1fr; background: #080c12; border: tall #22311f; }
    #map_panel { height: 1fr; }
    #room_info { color: #7dcfff; }
    #exits_info { color: #9ece6a; }
    .vlabel { width: 18; color: #a9b1d6; }
    #hp_bar.hp-good .bar--bar { color: #9ece6a; }
    #hp_bar.hp-mid .bar--bar { color: #e0af68; }
    #hp_bar.hp-low .bar--bar { color: #f7768e; }
    #mp_bar .bar--bar { color: #7aa2f7; }
    #xp_bar .bar--bar { color: #bb9af7; }
    #cmd_input { width: 1fr; }
    #queue_badge { color: #ff9e64; text-style: bold; width: 12; content-align: right middle; }
    #cmd_hint { color: #7aa2f7; }
    """

    def __init__(self, engine: ApexForgeEngine, connect: bool = False, auto_path: Optional[str] = None, auto_target: Optional[str] = None) -> None:
        super().__init__()
        self.engine = engine
        self.connect = connect
        self.auto_path = auto_path
        self.auto_target = auto_target
        self.engine_task: Optional[asyncio.Task[Any]] = None
        self.log_widget: Optional[RichLog] = None
        self.comms_widget: Optional[RichLog] = None
        self._queue_depths = [0, 0, 0]  # P1,P2,P3

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Grid(
            Container(
                Label("[1] PRIMARY SERVER STREAM", classes="section-title"),
                RichLog(id="server_stream_log", highlight=False, markup=False, wrap=False),
                id="stream_box",
            ),
            Container(
                Label("[3] VISUAL AUTOMAPPER", classes="section-title"),
                Static("", id="room_info"),
                Static("Obvious exits: -", id="exits_info"),
                Static("[map unavailable]", id="map_panel"),
                id="mapper_box",
            ),
            Container(
                Label("[2] COMMS, ALERTS, & EVENTS", classes="section-title"),
                RichLog(id="comms_log", highlight=False, markup=False, wrap=False),
                id="comms_box",
            ),
            Container(
                Label("[4] PARTY / IPC TELEMETRY", classes="section-title"),
                Static("No party telemetry yet.", id="party_panel"),
                Label("[5] ANALYTICS & MONITOR", classes="section-title"),
                Static("", id="analytics_panel"),
                Static("", id="queue_panel"),
                id="party_box",
            ),
            Container(
                Horizontal(Static("HP", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="hp_bar"), Static("-", id="hp_text", classes="vlabel")),
                Horizontal(Static("MANA", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="mp_bar"), Static("-", id="mp_text", classes="vlabel")),
                Horizontal(Static("XP / STATE", classes="vlabel"), ProgressBar(total=100, show_eta=False, id="xp_bar"), Static("-", id="xp_text", classes="vlabel")),
                id="vitals_box",
            ),
            Container(
                Horizontal(
                    Label("[7] COMMAND INPUT: >"),
                    Input(placeholder="Type commands or @admin hooks...", id="cmd_input"),
                    Static("Q: 0/0/0", id="queue_badge"),
                ),
                Static("F1 Flee  F2 Stop  F3 Map  F4 Status  F5 Recalc  F6 Where  F7 @health  F8 Look  F9 Rest  F10 Stand  F11 Loot  F12 Inv", id="cmd_hint"),
                id="input_box",
            ),
            id="root_grid",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one("#server_stream_log", RichLog)
        self.comms_widget = self.query_one("#comms_log", RichLog)
        self._log("[*] Dashboard mounted. Initializing engine subsystems...")
        self._comms("[*] Comms/events log ready (telepaths, pages, party, alerts, admin).")
        self.engine.add_event_listener(self._on_engine_event)
        self.refresh_dashboard_metrics()
        self.engine_task = asyncio.create_task(self.engine.run_lifecycle(auto_path=self.auto_path, auto_target=self.auto_target, connect=self.connect))
        self.set_interval(1.0, self.refresh_dashboard_metrics)

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

    def _ui_call(self, func: Any, *args: Any) -> None:
        try:
            self.call_from_thread(func, *args)
        except Exception:
            try:
                func(*args)
            except Exception:
                pass

    def _on_engine_event(self, event: Dict[str, Any], engine: ApexForgeEngine) -> None:
        etype = str(event.get("type", ""))
        payload = event.get("payload", {}) or {}
        if etype == "stream":
            if not payload.get("gagged"):
                # prefer raw (ANSI-capable) for display; fall back to stripped line
                self._ui_call(self._log, payload.get("raw") or payload.get("line", ""))
        elif etype == "comms":
            kind = payload.get("kind", "comms")
            frm = payload.get("from", "")
            txt = payload.get("text", payload.get("line", ""))
            self._ui_call(self._comms, f"[{kind}] {frm+': ' if frm else ''}{txt}")
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
        refresh_types = {"stream","dispatch","queue","route_loaded","route_failed","route_complete","room_arrival","xp","vitals","mana_tick","path_started","map_update","state","stealth_signal","admin_trigger","comms","party_wait","party_ok","party_health","party_health_msg","lap_complete","cast_scheduled","cast_flushed","exits"}
        if etype in refresh_types:
            self._ui_call(self.refresh_dashboard_metrics)

    def _set_hp_color(self, pct: float) -> None:
        bar = self.query_one("#hp_bar", ProgressBar)
        bar.remove_class("hp-good", "hp-mid", "hp-low")
        if pct < 30:
            bar.add_class("hp-low")
        elif pct < 60:
            bar.add_class("hp-mid")
        else:
            bar.add_class("hp-good")

    def refresh_dashboard_metrics(self) -> None:
        hp_pct = (self.engine.hp / self.engine.max_hp * 100) if self.engine.max_hp else 0.0
        mp_pct = (self.engine.mp / self.engine.max_mp * 100) if self.engine.max_mp else 0.0
        mv = getattr(self.engine, "mv", 0)
        max_mv = getattr(self.engine, "max_mv", 0)
        # level/need is not exact XP bar in MajorMUD; show a coarse progress proxy from need_xp if known
        need = int(getattr(self.engine, "need_xp", 0) or 0)
        xp_proxy = 100.0 if need == 0 else max(0.0, min(99.0, 100.0 - min(99.0, need / 1000.0)))

        try:
            self.query_one("#hp_bar", ProgressBar).update(progress=max(0.0, min(100.0, hp_pct)))
            self.query_one("#mp_bar", ProgressBar).update(progress=max(0.0, min(100.0, mp_pct)))
            self.query_one("#xp_bar", ProgressBar).update(progress=xp_proxy)
            self._set_hp_color(hp_pct)
            self.query_one("#hp_text", Static).update(f"{self.engine.hp}/{self.engine.max_hp}")
            self.query_one("#mp_text", Static).update(f"{self.engine.mp}/{self.engine.max_mp}")
            self.query_one("#xp_text", Static).update(f"Need {need} | {self.engine.state}")
        except Exception:
            pass

        snap = self.engine.get_map_snapshot()
        room_name = snap.get("current_room_name") or ""
        room_id = snap.get("current_room_id", "-")
        exits = snap.get("exits") or list((self.engine.mapper.raw_room_data.get(str(room_id), {}) or {}).get("exits", {}).keys())
        self.query_one("#room_info", Static).update(f"Room: {room_name or '-'} (#{room_id}) | Zone: {(self.engine.mapper.raw_room_data.get(str(room_id), {}) or {}).get('zone','-')}")
        self.query_one("#exits_info", Static).update("Obvious exits: " + (", ".join(exits) if exits else "unknown"))
        self.query_one("#map_panel", Static).update(str(snap.get("ascii", "[map unavailable]")))

        pstatus = getattr(self.engine, "party_member_status", {}) or {}
        pwaits = getattr(self.engine, "party_wait_flags", {}) or {}
        is_leader = bool(getattr(self.engine, "is_leader", True))
        party_lines = [f"Role: {'Leader' if is_leader else 'Follower'} | Leader: {getattr(self.engine, 'leader_name', '')}"]
        if pstatus:
            for name, info in sorted(pstatus.items()):
                hp = info.get("hp", "?"); mhp = info.get("max_hp", "?")
                mp = info.get("mp", "?"); mmp = info.get("max_mp", "?")
                st = info.get("state", "-"); room = info.get("room_id", "-")
                w = " WAIT" if pwaits.get(name) else ""
                party_lines.append(f"{info.get('name',name)}: HP {hp}/{mhp} MP {mp}/{mmp} {st} @{room}{w}")
        else:
            party_lines.append("No party telemetry yet.")
        if pwaits:
            party_lines.append("Waiting: " + ", ".join([k for k, v in pwaits.items() if v]))
        self.query_one("#party_panel", Static).update("\n".join(party_lines))

        path_key = self.engine.active_path_key
        ta = self.engine.analytics.get_time_analysis()
        analytics_text = (
            f"XP 5m {self.engine.analytics.calculate_rolling_velocity(300):,.0f}/hr | 15m {self.engine.analytics.calculate_rolling_velocity(900):,.0f}/hr | Total {self.engine.analytics.total_xp_gained}\n"
            f"Path {path_key or '-'} | RTK {self.engine.analytics.average_rtk():.2f} | RTC {self.engine.analytics.average_rtc():.2f} | Walk {self.engine.analytics.walk_speed():.2f}s\n"
            f"M/A/R {ta.get('moving_pct',0):.1f}/{ta.get('attacking_pct',0):.1f}/{ta.get('resting_pct',0):.1f}% | MV {mv}/{max_mv} | Blessed {bool(getattr(self.engine,'_blessed',False))}"
        )
        self.query_one("#analytics_panel", Static).update(analytics_text)

        qd = self.engine.command_queue.qsize()
        # best-effort per-priority estimate (tracked from events, clamped to total)
        p1,p2,p3 = self._queue_depths
        total_tracked = p1+p2+p3
        if total_tracked != qd:
            # resync conservatively
            if total_tracked > qd:
                overflow = total_tracked - qd
                for i in (2,1,0):
                    take = min(overflow, self._queue_depths[i])
                    self._queue_depths[i] -= take
                    overflow -= take
            p1,p2,p3 = self._queue_depths
        self.query_one("#queue_badge", Static).update(f"Q: {p1}/{p2}/{p3}")
        queue_text = (
            f"Queue total: {qd} (P1/P2/P3 {p1}/{p2}/{p3}) | Pending: {self.engine.pending_arrival_room_id or '-'}\n"
            f"Steps: {len(self.engine.active_navigation_path)} | IPC: {self.engine.start_ipc_bus} | Webhook: {'yes' if self.engine.coordination.webhook_url else 'no'}"
        )
        self.query_one("#queue_panel", Static).update(queue_text)

    async def _execute_ui_command(self, text: str) -> None:
        cmd = (text or "").strip()
        if not cmd:
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
        await self.engine.inject_command(1, "flee")
        self._log("[ui] Injected P1 flee command")
        self.refresh_dashboard_metrics()

    async def action_ui_stop(self) -> None:
        await self._execute_ui_command("@stop")

    async def action_ui_status(self) -> None:
        await self._execute_ui_command("@status")

    async def action_ui_recalc(self) -> None:
        await self._execute_ui_command("@recalc")

    async def action_ui_where(self) -> None:
        await self._execute_ui_command("@where")

    async def action_ui_health(self) -> None:
        await self._execute_ui_command("@health")

    async def action_ui_look(self) -> None:
        await self._execute_ui_command("look")

    async def action_ui_rest(self) -> None:
        await self._execute_ui_command("rest")

    async def action_ui_stand(self) -> None:
        await self._execute_ui_command("stand")

    async def action_ui_loot(self) -> None:
        cmd = getattr(self.engine, "corpse_loot_command", None) or "get all corpse"
        await self._execute_ui_command(str(cmd))

    async def action_ui_inventory(self) -> None:
        await self._execute_ui_command("@inventory")

    async def action_refresh_map(self) -> None:
        self.engine.emit_event("map_update", self.engine.get_map_snapshot())
        self.refresh_dashboard_metrics()

    async def action_app_quit(self) -> None:
        self.exit()

    async def on_unmount(self) -> None:
        self.engine.is_running = False
        if self.engine_task is not None:
            self.engine_task.cancel()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ApexForge dashboard")
    p.add_argument("--profile", help="Profile name from profiles.json")
    p.add_argument("--connect", action="store_true", help="Open live MUD socket (default: offline/local)")
    p.add_argument("--path", dest="path_key", help="Start configured path from paths.json")
    p.add_argument("--target", help="Route to target room id on start")
    p.add_argument("--ipc-bus", action="store_true", help="Start local IPC bus")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    engine = ApexForgeEngine(profile_name=args.profile)
    if args.ipc_bus:
        engine.start_ipc_bus = True
    app = ApexForgeDashboard(engine, connect=bool(args.connect), auto_path=args.path_key, auto_target=args.target)
    app.run()
