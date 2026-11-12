import argparse
import asyncio
import codecs
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from analytics import ApexForgeAnalytics
from coordination import ApexForgeCoordinationBus
from navigation import ApexForgeMapper

# --- PRIORITY LEVELS ---
PRIORITY_SURVIVAL = 1
PRIORITY_COMBAT = 2
PRIORITY_UTILITY = 3

STATE_NAVIGATING = "NAVIGATING"
STATE_COMBAT = "COMBAT"
STATE_RESTING = "RESTING"
STATE_RECOVERY = "RECOVERY"  # PvP/control/spin-out recovery; movement locked until verified prompt


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Config] Failed to load {path}: {e}")
        return default


class ApexForgeEngine:
    def __init__(
        self,
        profile_name: Optional[str] = None,
        config_dir: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self.config_dir = config_dir or os.path.dirname(os.path.abspath(__file__))
        self.paths_file = os.path.join(self.config_dir, "paths.json")
        self.profiles_file = os.path.join(self.config_dir, "profiles.json")
        self.database_file = os.path.join(self.config_dir, "database.json")
        self.items_file = os.path.join(self.config_dir, "items.json")

        self.profile_name = profile_name
        self.profile = self._load_profile(profile_name)

        self.host = host or self.profile.get("host", "127.0.0.1")
        self.port = int(port or self.profile.get("port", 23))
        self.is_running = False

        # Core state (deterministic state machine)
        self.state = STATE_NAVIGATING
        self._recovery_reason: str = ""
        self._recovery_until_prompt: bool = False
        self._stream_line_buffer: List[str] = []  # prompt-bounded paragraph/chunk accumulator
        self._last_prompt_ts: float = 0.0
        self.hp = 100
        self.max_hp = 100
        self.mp = 50
        self.max_mp = 50
        self.mv = 0
        self.max_mv = 0
        self.mana_tick_delta = 0
        self.current_room_id = "1"
        self.current_room_name = ""
        self.current_room_exits: List[str] = []
        self.pending_arrival_room_id: Optional[str] = None
        self.pending_step: Optional[Dict[str, Any]] = None
        self.active_navigation_path: List[Dict[str, Any]] = []
        self.active_path_key: Optional[str] = None
        self.active_path_loop: List[str] = []
        self.active_path_index = 0
        self.recent_room_history: List[str] = []

        survival_cfg = self.profile.get("survival", {})
        automation_cfg = self.profile.get("automation", {})
        spells_cfg = self.profile.get("spells", {})
        party_cfg = self.profile.get("party", {})
        combat_cfg = self.profile.get("combat", {})
        nav_cfg = self.profile.get("navigation", {})
        econ_cfg = self.profile.get("inventory_economy", {})

        self.flee_hp_percentage = float(survival_cfg.get("flee_hp_percentage", 30.0))
        self.flee_hp_absolute = int(survival_cfg.get("flee_hp_absolute", 0) or 0)
        self.min_monsters_flee = int(survival_cfg.get("min_monsters_flee", 0) or 0)
        self.rest_hp_percentage = float(survival_cfg.get("rest_hp_percentage", 70.0))
        self.emergency_hangup_on_pvp = bool(survival_cfg.get("emergency_hangup_on_pvp", False))
        self.auto_rest_command = survival_cfg.get("auto_rest_command", "rest")
        self.auto_stand_command = survival_cfg.get("auto_stand_command", "stand")
        self.restock_room_id = str(automation_cfg.get("restock_room_id", "1"))
        self.restock_commands = list(automation_cfg.get("restock_commands", []))
        self.corpse_loot_command = automation_cfg.get("corpse_loot_command", "get all corpse")
        self.corpse_snipe_command = automation_cfg.get("corpse_snipe_command", "get all from corpse")
        self.interrupt_command = automation_cfg.get("interrupt_command", "bash")
        self.spell_interrupt_command = automation_cfg.get("spell_interrupt_command", self.interrupt_command)
        self.recovery_commands = list(automation_cfg.get("recovery_commands", ["look", "recall"]))
        self.burden_drop_threshold = float(automation_cfg.get("burden_drop_threshold", 0.85))
        self.drop_candidates = [str(x) for x in automation_cfg.get("drop_candidates", [])]
        self.auto_recheck_inventory_on_unknown = bool(automation_cfg.get("auto_recheck_inventory_on_unknown", True))

        # Spells / bless / heal / nuke scheduling
        self.spells_cfg = spells_cfg or {}
        self.auto_heal = bool(spells_cfg.get("auto_heal", False))
        self.heal_spell = str(spells_cfg.get("heal_spell", "heal") or "heal")
        self.heal_hp_threshold_abs = int(spells_cfg.get("heal_hp_threshold", 0) or 0)
        self.heal_hp_pct = float(spells_cfg.get("heal_hp_pct", 60) or 60)
        self.heal_target = str(spells_cfg.get("heal_target", "self") or "self")
        self.party_heal_spell = str(spells_cfg.get("party_heal_spell", self.heal_spell) or self.heal_spell)
        self.party_heal_hp_pct = float(spells_cfg.get("party_heal_hp_pct", 50) or 50)
        self.auto_bless = bool(spells_cfg.get("auto_bless", False))
        self.bless_spell = str(spells_cfg.get("bless_spell", "") or "")
        self.bless_target = str(spells_cfg.get("bless_target", "self") or "self")
        self.auto_nuke = bool(spells_cfg.get("auto_nuke", False))
        self.nuke_spell = str(spells_cfg.get("nuke_spell", "") or "")
        self.min_nuke_monsters = int(spells_cfg.get("min_nuke_monsters", 2) or 2)
        self.cast_at_end_of_round = bool(spells_cfg.get("cast_at_end_of_round", False))
        self.vampiric_spells = {str(s).lower() for s in spells_cfg.get("vampiric_spells", [])}

        # Party coordination
        self.party_cfg = party_cfg or {}
        self.is_leader = bool(party_cfg.get("is_leader", True))
        self.is_follower = bool(party_cfg.get("is_follower", not self.is_leader))
        self.leader_name = str(party_cfg.get("leader_name", self.profile.get("leader_name", "Mofo")) or "Mofo")
        self.followers = [str(x) for x in party_cfg.get("followers", [])]
        self.party_whitelist = {str(x).lower() for x in party_cfg.get("whitelist", [self.leader_name] + self.followers)}
        self.rest_healing_applies_to_party = bool(party_cfg.get("rest_healing_applies_to_party", False))
        self.disable_health_spam = bool(party_cfg.get("disable_health_spam", False))
        self.hp_wait_threshold_pct = float(party_cfg.get("hp_wait_threshold_pct", 50) or 50)
        self.party_heal_enabled = bool(party_cfg.get("party_heal_enabled", False))
        self.party_wait_flags: Dict[str, bool] = {}
        self.party_member_status: Dict[str, Dict[str, Any]] = {}
        self._party_client_writer = None
        self._party_client_task = None

        # Combat profiles / weapon swap / immunity
        self.combat_cfg = combat_cfg or {}
        self.auto_combat_enabled = bool(combat_cfg.get("auto_combat", True))
        self.combat_profiles = combat_cfg.get("profiles", {}) or {}
        self.current_combat_profile = str(combat_cfg.get("current_profile", 1))
        self.immunity_db_limit = int(combat_cfg.get("immunity_db_limit", 500) or 500)
        self.run_mode_suppress_combat = False
        self.pvp_mode = str(self.profile.get("pvp_mode", "normal")).lower()  # "do nothing" disables nuke abort on friendlies

        # Navigation behavior
        self.nav_cfg = nav_cfg or {}
        self.lockpick_enabled = bool(nav_cfg.get("lockpick_enabled", False))
        self.lockpick_command = str(nav_cfg.get("lockpick_command", "pick lock") or "pick lock")
        self.reengage_combat_on_lost = bool(nav_cfg.get("reengage_combat_on_lost", True))

        # Inventory/economy
        self.econ_cfg = econ_cfg or {}
        self.active_mode = bool(econ_cfg.get("active_mode", True))
        self.auto_buy = bool(econ_cfg.get("auto_buy", False))
        self.auto_sell = bool(econ_cfg.get("auto_sell", False))
        self.auto_deposit = bool(econ_cfg.get("auto_deposit", False))
        self.auto_withdraw = bool(econ_cfg.get("auto_withdraw", False))
        self.keep_cash_copper = int(econ_cfg.get("keep_cash_copper", 0) or 0)
        self.stash_room_id = str(econ_cfg.get("stash_room_id", "") or "")
        self.bank_room_id = str(econ_cfg.get("bank_room_id", "") or "")
        self.wealth_copper = 0
        self.item_uses: Dict[str, int] = {}

        self.admin_names = {str(n).lower() for n in self.profile.get("admin_names", [self.leader_name])}
        self.tick_cast_command = self.profile.get("tick_cast_command", self.bless_spell or "cast bless")

        # Subsystems (local/off-socket data + processing)
        self.mapper = ApexForgeMapper(self.database_file)
        self.analytics = ApexForgeAnalytics(self.items_file)
        self.analytics.stat_weights = self.profile.get("stat_weights", self.analytics.stat_weights)
        self.analytics.immunity_db_limit = self.immunity_db_limit

        bus_port = int(self.profile.get("ipc_port", 5555))
        webhook_url = self.profile.get("discord_webhook") or None
        self.coordination = ApexForgeCoordinationBus(bus_host="127.0.0.1", bus_port=bus_port, webhook_url=webhook_url)
        self.coordination.command_handler = self.handle_admin_bus_command
        self.start_ipc_bus = bool(self.profile.get("start_ipc_bus", False))

        self.paths_data = _load_json(self.paths_file, {"paths": {}}).get("paths", {})
        self.path_efficiency_thresholds = {k: float(v.get("min_xp_hr", 0.0)) for k, v in self.paths_data.items()}
        self.messages_data = _load_json(os.path.join(self.config_dir, "messages.json"), {"messages": []}).get("messages", [])

        # Networking / queue
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.command_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

        # BBS/login scripting (MegaMUD-style prompt/reply)
        self.bbs_cfg = self.profile.get("bbs", {}) or {}
        self._bbs_login_index = 0
        self._bbs_logged_in = not bool(self.bbs_cfg.get("logon_prompts"))  # if no script, assume ready
        self._bbs_menu_prompt = str(self.bbs_cfg.get("menu_prompt", "") or "")
        self._bbs_ansi_cmd = str(self.bbs_cfg.get("ansi_cmd", "") or "")

        # Deterministic/tunable parsing config (ApexForge-native)
        self.parsing_cfg = self.profile.get("parsing", {}) or {}
        self.stat_line_template = str(self.profile.get("stat_line_template", "") or self.parsing_cfg.get("stat_line_template", "") or "")
        self.prompt_markers = [str(x) for x in (self.parsing_cfg.get("prompt_markers", []) or []) if str(x).strip()]
        if self._bbs_menu_prompt and self._bbs_menu_prompt not in self.prompt_markers:
            self.prompt_markers.append(self._bbs_menu_prompt)
        if "[MAJORMUD]:" not in self.prompt_markers:
            self.prompt_markers.append("[MAJORMUD]:")
        self.regex_statline = self._compile_statline_template(self.stat_line_template)
        # optional game-specific regex overrides/patterns: parsing.patterns.{name: "regex"}
        self._compiled_parse_patterns: Dict[str, re.Pattern] = {}
        for _pname, _pexpr in (self.parsing_cfg.get("patterns", {}) or {}).items():
            if not _pexpr:
                continue
            try:
                self._compiled_parse_patterns[str(_pname)] = re.compile(str(_pexpr), re.IGNORECASE)
            except Exception:
                pass

        # Aliases / gags / trigger classes (zMUD-cMUD/Mudlet-inspired, config-driven)
        _automation_cfg = self.profile.get("automation", {}) or {}
        self.aliases: Dict[str, str] = {str(k).lower(): str(v) for k, v in (_automation_cfg.get("aliases", {}) or {}).items() if str(k).strip()}
        self.gag_patterns: List[re.Pattern] = []
        for pat in (self.parsing_cfg.get("gag_patterns", []) or _automation_cfg.get("gag_patterns", []) or []):
            try:
                self.gag_patterns.append(re.compile(str(pat), re.IGNORECASE))
            except Exception:
                pass
        self.disabled_trigger_classes = {str(x).lower() for x in (self.parsing_cfg.get("disabled_classes", []) or [])}

        # Navigation/orientation/safety tracking
        self.active_navigation_path: List[Dict[str, Any]] = []
        self.active_path_loop: List[str] = []
        self.active_path_index = 0
        self.active_path_key: Optional[str] = None
        self.pending_step: Optional[Dict[str, Any]] = None
        self.pending_arrival_room_id: Optional[str] = None
        self.room_sequence: List[str] = []  # multi-node path re-sync window
        self.risk_rooms: set = set()
        self.inventory: Dict[str, int] = {}  # item_id -> count (best-effort)
        self.carried_weight = 0.0
        self.max_carry_weight = float(automation_cfg.get("max_carry_weight", 100.0))
        self.orientation_recovering = False
        self._last_move_dispatch_ts: Optional[float] = None
        self.global_pre_move_cmd = str(automation_cfg.get("pre_move_command", "") or self.profile.get("pre_move_command", "") or "")
        self.global_post_move_cmd = str(automation_cfg.get("post_move_command", "") or self.profile.get("post_move_command", "") or "")
        self.min_lap_wait_task: Optional[asyncio.Task] = None

        # Combat/room state for MegaMUD-like logic
        self.room_players: List[str] = []
        self.room_monster_count = 0
        self.current_target: Optional[str] = None
        self._pending_end_of_round_casts: List[str] = []
        self._round_in_progress = False

        # Precompiled regex (bare-metal stream parsing)
        self.regex_vitals = re.compile(r"HP:\s*(\d+)/(\d+)\s+MP:\s*(\d+)/(\d+)", re.IGNORECASE)
        self.regex_vitals_full = re.compile(r"^HP:\s*(\d+)/(\d+)\s+MA:\s*(\d+)/(\d+)\s+MV:\s*(\d+)/(\d+)", re.IGNORECASE)
        self.regex_mana_sync = re.compile(r"M:\s*(\d+)\((\d+)\)", re.IGNORECASE)
        self.regex_xp_gain = re.compile(r"You gain\s*(\d+)\s*experience", re.IGNORECASE)
        self.regex_party_attack = re.compile(r"(\w+) moves to attack (.+)", re.IGNORECASE)
        self.regex_loot_item = re.compile(r"You (?:get|take|loot)\s+(.+?)(?:\s+\((\d+)\))?\.", re.IGNORECASE)
        self.regex_pvp = re.compile(r"\b(ambush(?:es)?|attacks you|backstabs you)\b", re.IGNORECASE)
        self.regex_combat_hit = re.compile(r"\b(you hit|hits you|you miss|misses you|slashes|bashes|rakes|glanc(?:e|ing))\b", re.IGNORECASE)
        self.regex_kill = re.compile(r"\b(is dead|you have slain|falls to the ground dead)\b", re.IGNORECASE)
        self.regex_boss_death = re.compile(r"\b(boss|warlord|chieftain|dragon).*(is dead|slain|falls dead)\b", re.IGNORECASE)
        self.regex_corpse = re.compile(r"\b(corpse)\b", re.IGNORECASE)
        self.regex_casting = re.compile(r"\b(begins to cast|starts casting|chants|is casting)\b", re.IGNORECASE)
        self.regex_stealth = re.compile(r"\b(faint rustle|shadow crosses|footsteps|someone arrives|someone leaves|sneak|you sense movement)\b", re.IGNORECASE)
        self.regex_enemy_enter = re.compile(r"^([A-Za-z\s]+) just walked in from the (north|south|east|west|up|down)\.", re.IGNORECASE)
        self.regex_enemy_enter_generic = re.compile(r"\b(arrives|enters the room|walks in|steps in)\b", re.IGNORECASE)
        self.regex_disorient = re.compile(r"\b(it is pitch black|too dark to see|you are blind|you can't see|a trapdoor opens|you fall|you are teleported|you feel disoriented|you have been summoned)\b", re.IGNORECASE)
        self.regex_telepath = re.compile(r"^([A-Za-z]+)\s+(?:telepaths|says|tells you),?\s+\"([^\"]*)\"", re.IGNORECASE)
        self.regex_page = re.compile(r"^(\w+)\s+pages?:?\s*@(\w+)\s+(.+)$", re.IGNORECASE)
        self.regex_party_health = re.compile(r"^(?:([A-Za-z]+) is critically wounded|([A-Za-z]+)\s*\[HP:(\d+)%/(\d+)%\])", re.IGNORECASE)
        self.regex_also_here = re.compile(r"^Also here:\s*([A-Za-z,\s]+)\.", re.IGNORECASE)
        self.regex_locked = re.compile(r"^(The door is locked\.|It's locked\.)$", re.IGNORECASE)
        self.regex_lost = re.compile(r"^(You are lost\.|You can't go that way\.)$", re.IGNORECASE)
        self.regex_inventory_unknown = re.compile(r"^You see nothing like that in your inventory\.$", re.IGNORECASE)
        self.regex_immunity = re.compile(r"^(?:Your attack has no effect on ([A-Za-z\s]+)!|You notice your attack does not harm ([A-Za-z\s]+)\.)$", re.IGNORECASE)
        self.regex_round_end_hint = re.compile(r"\b(round ends|your turn|prompt)\b", re.IGNORECASE)  # best-effort, game-tunable
        self.ansi_cleaner = re.compile(r"(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]")

        # Combat timing / server tick adaptation hooks
        self._last_combat_burst_ts = 0.0
        self.adaptive_combat_delay = 0.0  # seconds, learned from analytics tick tracker
        self.last_parsed_chunk: str = ""

        # Party role matrix / combo profiles (IPC + follower assist)
        self.party_role = str(party_cfg.get("role", self.profile.get("role", "dps")) or "dps").upper()
        self.party_roles_map = party_cfg.get("roles", {}) if isinstance(party_cfg.get("roles", {}), dict) else {}
        self.leader_assist_heal_pct = float(party_cfg.get("leader_assist_heal_pct", 40.0) or 40.0)
        self.party_heal_spell_leader = str(party_cfg.get("leader_heal_spell", spells_cfg.get("party_heal_spell", "")) or "")
        self._last_combo_heal_ts = 0.0

        # GMCP / OOB state (fallback to regex parsing when unavailable)
        self.gmcp_supported = False
        self.gmcp_negotiated = False
        self.gmcp_enabled = bool(self.profile.get("gmcp", True))
        self._telnet_buffer = bytearray()
        enc = str(self.profile.get("encoding", "cp437") or "cp437")
        try:
            self._text_decoder = codecs.getincrementaldecoder(enc)(errors="replace")
        except Exception:
            self._text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._last_efficiency_check = 0.0
        self._last_auto_heal_ts = 0.0
        self._last_party_wait_sent_ts = 0.0
        self._last_party_ok_sent_ts = 0.0
        self._blessed = False
        self._last_bless_ts = 0.0
        self._last_inventory_recheck_ts = 0.0
        self._last_nuke_ts = 0.0
        # MegaMUD message/status tracking
        self.need_xp = 0
        self.status_effects: Dict[str, bool] = {}
        self._msg_start_patterns: List[tuple] = []  # (effect_key, regex)
        self._msg_end_patterns: List[tuple] = []
        self._bless_spell_index = 0
        self.bless_spells = list(self.spells_cfg.get("bless_spells", []) or ([self.bless_spell] if self.bless_spell else []))
        self.poison_cure_cmd = str(self.spells_cfg.get("poison_cure_cmd", "") or "")
        self.disease_cure_cmd = str(self.spells_cfg.get("disease_cure_cmd", "") or "")
        self.blind_cure_cmd = str(self.spells_cfg.get("blind_cure_cmd", "") or "")
        self._compile_message_triggers()

        # lethal/death detection for avoidance registry (best-effort, config-tunable)
        self.regex_player_death = re.compile(r"\b(you (have been slain|are slain|die|have died|are dead)|you have been killed)\b", re.IGNORECASE)

        # Observability hooks
        self.event_listeners: List[Any] = []

    def _load_profile(self, profile_name: Optional[str]) -> Dict[str, Any]:
        data = _load_json(self.profiles_file, {})
        chars = data.get("characters", {})
        if profile_name and profile_name in chars:
            return chars[profile_name]
        if chars:
            first = next(iter(chars.keys()))
            self.profile_name = first
            return chars[first]
        return {}

    def add_event_listener(self, listener: Any) -> None:
        self.event_listeners.append(listener)

    def emit_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        event = {"type": event_type, "payload": payload or {}, "ts": time.time()}
        for listener in list(self.event_listeners):
            try:
                listener(event, self)
            except Exception:
                pass

    def strip_ansi(self, raw_buffer: str) -> str:
        return self.ansi_cleaner.sub("", raw_buffer)

    def _compile_statline_template(self, template: str) -> Optional[re.Pattern]:
        """Compile a MegaMUD-style statline template into a regex with named groups.
        Supported tokens (best-effort): %h/%H hp, %m/%M mana, %v/%V mv, %x/%X need/exp-to-level, %r room/rest.
        Unknown %tokens become non-greedy wildcards.
        """
        if not template:
            return None
        s = str(template).strip()
        if not s:
            return None
        # strip leading mode words (e.g. "full custom ") if template is descriptive, but keep bracketed form
        # attempt to find a bracketed/stat segment containing %
        m = re.search(r"(\[.*%.*\].*$|%.*$)", s)
        if m:
            s = m.group(1)
        token_map = {
            "h": r"(?P<hp>\d+)", "H": r"(?P<max_hp>\d+)",
            "m": r"(?P<mp>\d+)", "M": r"(?P<max_mp>\d+)",
            "v": r"(?P<mv>\d+)", "V": r"(?P<max_mv>\d+)",
            "x": r"(?P<need>\d+)", "X": r"(?P<need>\d+)",
            "e": r"(?P<xp>\d+)", "E": r"(?P<xp>\d+)",
            "g": r"(?P<gold>\d+)", "G": r"(?P<gold>\d+)",
        }
        out = []
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "%":
                if i + 1 < len(s):
                    t = s[i + 1]
                    if t == "%":
                        out.append("%"); i += 2; continue
                    out.append(token_map.get(t, r".*?"))
                    i += 2
                    continue
            out.append(re.escape(ch))
            i += 1
        try:
            return re.compile("".join(out), re.IGNORECASE)
        except Exception:
            return None

    def _is_prompt_line(self, clean: str) -> bool:
        low = (clean or "").lower()
        for marker in self.prompt_markers:
            if marker and str(marker).lower() in low:
                return True
        # common prompt endings
        if re.search(r"\]:\s*$", clean):
            return True
        return False

    # --- Message/status parsing (from imported MegaMUD Messages.md) ---
    def _compile_message_triggers(self) -> None:
        wanted = {
            "bless": "blessed",
            "alertness": "alertness",
            "barkskin": "barkskin",
            "blind": "blind",
            "poison": "poisoned",
            "bee poison": "poisoned",
            "disease": "diseased",
            "afraid": "afraid",
        }
        def to_re(pat: str) -> Optional[re.Pattern]:
            p = (pat or "").strip()
            if not p or p.startswith("."):
                return None
            # replace placeholders
            p = re.escape(p)
            p = p.replace(r"\{target\}", r".+?")
            p = p.replace(r"\{dmg\}", r"\d+")
            try:
                return re.compile(p, re.IGNORECASE)
            except Exception:
                return None
        for entry in (self.messages_data or []):
            name = str(entry.get("name", "")).strip().lower()
            if name not in wanted:
                continue
            key = wanted[name]
            pats = list(entry.get("patterns", []) or [])
            if pats:
                r0 = to_re(pats[0])
                if r0:
                    self._msg_start_patterns.append((key, r0))
            if len(pats) > 1:
                r1 = to_re(pats[1])
                if r1:
                    self._msg_end_patterns.append((key, r1))

    def _update_status_from_line(self, clean: str) -> None:
        for key, rx in self._msg_start_patterns:
            try:
                if rx.search(clean):
                    self.status_effects[key] = True
                    if key == "blessed":
                        self._blessed = True
            except Exception:
                pass
        for key, rx in self._msg_end_patterns:
            try:
                if rx.search(clean):
                    self.status_effects[key] = False
                    if key == "blessed":
                        self._blessed = False
            except Exception:
                pass
        # generic fallbacks
        if "the effects of bless wear off" in clean.lower() or "the effects of greater bless wear off" in clean.lower():
            self.status_effects["blessed"] = False
            self._blessed = False

    # --- Profile / weapon helpers ---
    def _active_combat_profile(self) -> Dict[str, Any]:
        p = self.combat_profiles.get(str(self.current_combat_profile), {})
        return p if isinstance(p, dict) else {}

    async def apply_combat_profile(self, profile_id: str) -> bool:
        pid = str(profile_id)
        if pid not in self.combat_profiles:
            return False
        self.current_combat_profile = pid
        p = self._active_combat_profile()
        weapon = str(p.get("weapon", "") or "")
        if weapon:
            await self.inject_command(PRIORITY_COMBAT, f"wield {weapon}")
        self.emit_event("profile_changed", {"profile": pid})
        return True

    # --- Party coordination helpers ---
    def _party_blocked(self) -> bool:
        if not self.is_leader or not self.rest_healing_applies_to_party:
            return False
        return any(bool(v) for v in self.party_wait_flags.values())

    async def _send_party_telepath(self, text: str) -> None:
        if not text:
            return
        # use tell/telepath style command template; configurable in future via profile
        cmd_tpl = str(self.party_cfg.get("telepath_command", "telepath {leader} {text}"))
        leader = self.leader_name
        try:
            cmd = cmd_tpl.format(leader=leader, text=text)
        except Exception:
            cmd = f"telepath {leader} {text}"
        await self.inject_command(PRIORITY_COMBAT if text.startswith("@") else PRIORITY_UTILITY, cmd)

    async def _emit_party_health_if_needed(self) -> None:
        if self.disable_health_spam or self.is_leader:
            return
        hp_pct = self._hp_pct()
        now = time.time()
        if hp_pct < self.hp_wait_threshold_pct and (now - self._last_party_wait_sent_ts) > 5:
            self._last_party_wait_sent_ts = now
            await self._send_party_telepath("@wait")
        elif hp_pct >= self.rest_hp_percentage and (now - self._last_party_ok_sent_ts) > 5 and self.state != STATE_COMBAT:
            self._last_party_ok_sent_ts = now
            await self._send_party_telepath("@ok")

    def _players_in_room_excluding_party(self) -> List[str]:
        out = []
        for name in self.room_players:
            nl = str(name).strip().lower()
            if nl and nl not in self.party_whitelist:
                out.append(name.strip())
        return out

    # --- Spell/cast helpers ---
    async def _schedule_cast(self, spell_cmd: str, priority: int = PRIORITY_COMBAT, end_of_round: Optional[bool] = None) -> None:
        if not spell_cmd:
            return
        eor = self.cast_at_end_of_round if end_of_round is None else bool(end_of_round)
        if eor and (self.state == STATE_COMBAT or self._round_in_progress):
            self._pending_end_of_round_casts.append(spell_cmd)
            self.emit_event("cast_scheduled", {"spell": spell_cmd, "mode": "end_of_round"})
            return
        await self.inject_command(priority, spell_cmd)

    def _apply_config_parse_patterns(self, clean: str) -> None:
        """Apply tunable parsing.patterns from config (Step 2). Best-effort, event-driven."""
        if not self._compiled_parse_patterns:
            return
        pats = self._compiled_parse_patterns
        # room/monster/player counts
        for key in ("mob_count", "monster_count", "monsters_here"):
            rx = pats.get(key)
            if rx:
                m = rx.search(clean)
                if m:
                    try:
                        self.room_monster_count = int(m.group(1))
                        self.emit_event("mob_count", {"count": self.room_monster_count, "line": clean})
                    except Exception:
                        pass
        for key in ("player_count", "players_here"):
            rx = pats.get(key)
            if rx:
                m = rx.search(clean)
                if m:
                    try:
                        self.emit_event("player_count", {"count": int(m.group(1)), "line": clean})
                    except Exception:
                        pass
        # also-here override/list
        rx = pats.get("also_here")
        if rx:
            m = rx.search(clean)
            if m:
                names = [n.strip() for n in (m.group(1) or "").split(",") if n.strip()]
                if names:
                    self.room_players = names
                    self.emit_event("room_players", {"players": names, "non_party": self._players_in_room_excluding_party(), "source": "pattern"})
        # round end / prompt cues
        for key in ("round_end", "prompt", "combat_end"):
            rx = pats.get(key)
            if rx and rx.search(clean):
                if self._round_in_progress or key == "round_end":
                    self._round_in_progress = False
                    if self._pending_end_of_round_casts:
                        try:
                            asyncio.get_running_loop().create_task(self._flush_end_of_round_casts())
                        except Exception:
                            pass
                self.emit_event("round_cue", {"kind": key, "line": clean})
        # economy/wealth/corpse/lock outcomes
        for key, ev in (("wealth", "wealth"), ("currency", "wealth"), ("corpse", "corpse"), ("shop", "economy"), ("bank", "economy"), ("stash", "economy"), ("uses", "item_uses"), ("lockpick_success", "lockpick"), ("lockpick_fail", "lockpick"), ("door_locked", "door"), ("door_unlocked", "door")):
            rx = pats.get(key)
            if rx:
                m = rx.search(clean)
                if m:
                    self.emit_event(ev, {"kind": key, "line": clean, "match": m.groups()})
        # attack/immunity confirmations
        for key, ev in (("attack_hit", "combat"), ("attack_miss", "combat"), ("immunity", "immunity")):
            rx = pats.get(key)
            if rx and rx.search(clean):
                self.emit_event(ev, {"kind": key, "line": clean})

    async def _flush_end_of_round_casts(self) -> None:
        if not self._pending_end_of_round_casts:
            return
        casts = list(self._pending_end_of_round_casts)
        self._pending_end_of_round_casts.clear()
        for spell in casts:
            await self.inject_command(PRIORITY_COMBAT, spell)
        self.emit_event("cast_flushed", {"count": len(casts)})

    async def _maybe_auto_heal_and_bless(self) -> None:
        hp_pct = self._hp_pct()
        now = time.time()
        # cure afflictions before regular heals (MegaMUD-like priority)
        if self.status_effects.get("poisoned") and self.poison_cure_cmd and (now - self._last_auto_heal_ts) > 1.0:
            self._last_auto_heal_ts = now
            await self._schedule_cast(self.poison_cure_cmd, PRIORITY_SURVIVAL)
            return
        if self.status_effects.get("diseased") and self.disease_cure_cmd and (now - self._last_auto_heal_ts) > 1.0:
            self._last_auto_heal_ts = now
            await self._schedule_cast(self.disease_cure_cmd, PRIORITY_SURVIVAL)
            return
        if self.status_effects.get("blind") and self.blind_cure_cmd and (now - self._last_auto_heal_ts) > 1.0:
            self._last_auto_heal_ts = now
            await self._schedule_cast(self.blind_cure_cmd, PRIORITY_SURVIVAL)
            return
        # auto-heal (self)
        if self.auto_heal and (hp_pct <= self.heal_hp_pct or (self.heal_hp_threshold_abs and self.hp <= self.heal_hp_threshold_abs)):
            if now - self._last_auto_heal_ts > 1.0 and self.heal_spell:
                self._last_auto_heal_ts = now
                cmd = self.heal_spell if self.heal_target == "self" else f"{self.heal_spell} {self.heal_target}"
                await self._schedule_cast(cmd, PRIORITY_SURVIVAL if hp_pct < self.flee_hp_percentage else PRIORITY_COMBAT)
        # auto-bless rotation
        if self.auto_bless and self.bless_spells and (not self._blessed or (now - self._last_bless_ts) > float(self.spells_cfg.get("bless_refresh_seconds", 300) or 300)):
            self._last_bless_ts = now
            spell = self.bless_spells[self._bless_spell_index % len(self.bless_spells)]
            self._bless_spell_index = (self._bless_spell_index + 1) % max(1, len(self.bless_spells))
            self._blessed = True
            await self.inject_command(PRIORITY_UTILITY, spell)

    async def _send_follower_bus_status(self, event: str = "update") -> None:
        """Follower: push status update to leader bus if connected."""
        if not (self.is_follower and self._party_client_writer):
            return
        try:
            payload = self.get_party_status_payload()
            payload["event"] = event
            self._party_client_writer.write((json.dumps(payload) + "\n").encode())
            await self._party_client_writer.drain()
        except Exception:
            self._party_client_writer = None

    # --- Party/follower execution (Step 3) ---
    async def handle_party_state(self, packet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Follower-side handler for leader state broadcasts received via IPC bus."""
        # follower may receive peer status packets; allow HEALER combo reaction to leader telemetry
        if str(packet.get("type", "")).lower() == "status":
            sender = str(packet.get("from") or packet.get("name") or "").lower()
            if sender and sender == str(self.leader_name).lower():
                self.party_member_status[sender] = dict(packet)
                self.party_member_status[sender]["ts"] = time.time()
                hp_pct = packet.get("hp_pct")
                if hp_pct is None and packet.get("hp") is not None and packet.get("max_hp"):
                    try:
                        hp_pct = (float(packet.get("hp")) / float(packet.get("max_hp"))) * 100.0
                    except Exception:
                        hp_pct = None
                await self._leader_combo_heal_if_needed(sender, str(packet.get("state", "")), hp_pct)
            return None

        action = str(packet.get("action", "") or "").upper()
        target = packet.get("target", "")
        room_id = packet.get("room_id", "")
        extra = packet.get("extra", {}) if isinstance(packet.get("extra"), dict) else {}

        if action == "ATTACK" and target:
            self.current_target = str(target)
            cmd_tpl = str(self.party_cfg.get("assist_command", "attack {target}") or "attack {target}")
            try:
                cmd = cmd_tpl.format(target=str(target))
            except Exception:
                cmd = f"attack {target}"
            await self.inject_command(PRIORITY_COMBAT, cmd)
            self.set_state(STATE_COMBAT, "leader_attack")
        elif action == "PARTY_CMD" and target:
            # execute leader-broadcast party command (prevalidated by leader/bus context)
            await self.inject_command(PRIORITY_UTILITY, str(target))
        elif action in ("FLEE", "RETREAT"):
            await self._on_flee_event("leader_flee")
            await self.inject_command(PRIORITY_SURVIVAL, "flee")
            self.set_state(STATE_RESTING, "leader_flee")
        elif action == "STOP":
            self.active_navigation_path = []
            self.pending_step = None
            self.pending_arrival_room_id = None
            self.run_mode_suppress_combat = False
            self.set_state(STATE_NAVIGATING, "leader_stop")
        elif action == "STATE":
            # leader state/vitals broadcast (combo profile sync)
            leader_name = str(packet.get("from") or packet.get("leader") or self.leader_name or "leader")
            leader_state = str(packet.get("state") or extra.get("state") or packet.get("leader_state") or "")
            hp_pct = packet.get("hp_pct", extra.get("hp_pct"))
            if hp_pct is None and packet.get("hp") is not None and packet.get("max_hp"):
                try:
                    hp_pct = (float(packet.get("hp")) / float(packet.get("max_hp"))) * 100.0
                except Exception:
                    hp_pct = None
            await self._leader_combo_heal_if_needed(leader_name, leader_state or ("COMBAT" if extra.get("combat") else ""), hp_pct)

        # report status back to leader after acting on relevant state
        if action in ("ATTACK", "PARTY_CMD", "FLEE", "RETREAT", "STOP", "STATE"):
            st = self.get_party_status_payload()
            st["event"] = action.lower()
            if room_id:
                st["room_id"] = str(room_id)
            return st
        return None

    def get_party_status_payload(self) -> Dict[str, Any]:
        hp_pct = self._hp_pct()
        waiting = bool(self.is_follower and hp_pct < self.hp_wait_threshold_pct and self.state in (STATE_RESTING, STATE_COMBAT))
        return {
            "type": "status",
            "from": (self.profile_name or "follower").lower(),
            "name": self.profile_name or "follower",
            "role": getattr(self, "party_role", "DPS"),
            "hp": self.hp, "max_hp": self.max_hp,
            "mp": self.mp, "max_mp": self.max_mp,
            "mv": self.mv, "max_mv": self.max_mv,
            "hp_pct": round(hp_pct, 2),
            "state": self.state,
            "room_id": self.current_room_id,
            "waiting": waiting,
            "gmcp": bool(getattr(self, "gmcp_supported", False)),
        }

    async def _party_status_heartbeat(self) -> None:
        """Periodic follower status reporting to leader bus (best-effort)."""
        while self.is_running and self.is_follower and self._party_client_writer:
            try:
                payload = self.get_party_status_payload()
                self._party_client_writer.write((json.dumps(payload) + "\n").encode())
                await self._party_client_writer.drain()
            except Exception:
                return
            await asyncio.sleep(max(2.0, float(self.party_cfg.get("status_interval_s", 5.0) or 5.0)))

    async def _run_follower_bus_client(self, bus_host: str, bus_port: int) -> None:
        """Follower: connect to leader IPC bus, execute leader states, report status."""
        from coordination import ApexForgePartyFollowerClient
        client = ApexForgePartyFollowerClient(bus_host=bus_host, bus_port=bus_port, name=(self.profile_name or "follower"))
        # run client loop; it will call back into handle_party_state
        while self.is_running and self.is_follower:
            try:
                reader, writer = await asyncio.open_connection(bus_host, bus_port)
                self._party_client_writer = writer
                hello = self.get_party_status_payload()
                hello["event"] = "connected"
                writer.write((json.dumps(hello) + "\n").encode())
                await writer.drain()
                hb = asyncio.create_task(self._party_status_heartbeat())
                buffer = ""
                while self.is_running and self.is_follower:
                    data = await reader.read(4096)
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            packet = json.loads(line)
                        except Exception:
                            continue
                        if packet.get("type") != "state":
                            continue
                        resp = await self.handle_party_state(packet)
                        if isinstance(resp, dict):
                            writer.write((json.dumps(resp) + "\n").encode())
                            await writer.drain()
                hb.cancel()
            except Exception as e:
                self._party_client_writer = None
                print(f"[Follower] Bus connect/retry ({e})")
                await asyncio.sleep(3)
            finally:
                self._party_client_writer = None

    # --- Navigation helpers ---
    def _path_step_overrides(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Allow per-path/per-transition step overrides in paths.json: paths[key].steps["from->to"] or ["to"]
        if not self.active_path_key:
            return step
        cfg = self.paths_data.get(self.active_path_key, {})
        steps = cfg.get("steps", {}) or {}
        key1 = f"{step.get('from_node')}->{step.get('to_node')}"
        key2 = str(step.get("to_node"))
        ov = steps.get(key1) or steps.get(key2) or {}
        if isinstance(ov, dict):
            merged = dict(step)
            for k in ("direction", "pre_cmd", "post_cmd", "delay", "lockpick", "party_cmd", "min_monsters", "wait_for_party"):
                if k in ov:
                    merged[k] = ov[k]
            return merged
        return step

    async def _handle_lockpick_if_needed(self, line: str) -> bool:
        if not self.regex_locked.search(line):
            return False
        if self.lockpick_enabled and self.lockpick_command:
            await self.inject_command(PRIORITY_UTILITY, self.lockpick_command)
            self.emit_event("lockpick_attempt", {"line": line, "command": self.lockpick_command})
            return True
        return False

    async def _handle_lost_or_blocked(self, line: str) -> None:
        if not self.regex_lost.search(line):
            return
        if self.run_mode_suppress_combat and self.reengage_combat_on_lost:
            self.run_mode_suppress_combat = False
            self.auto_combat_enabled = True
            self.emit_event("run_reengage_combat", {"line": line})
        # force orientation recovery/resync to known safe target
        await self.trigger_orientation_recovery("lost_or_cant_go")

    # --- State helpers --- (original follows)
    def set_state(self, new_state: str, reason: str = "") -> None:
        if new_state not in (STATE_NAVIGATING, STATE_COMBAT, STATE_RESTING, STATE_RECOVERY):
            return
        if self.state != new_state:
            old = self.state
            self.state = new_state
            try:
                self.analytics.set_active_state(new_state)
            except Exception:
                pass
            print(f"[State] {old} -> {new_state}{f' ({reason})' if reason else ''}")
            self.emit_event("state", {"from": old, "to": new_state, "reason": reason})
            # broadcast leader state (with vitals/role) for multi-class combo coordination
            if self.is_leader and getattr(self, "coordination", None):
                try:
                    extra = {"state": new_state, "hp_pct": round(self._hp_pct(), 2), "role": getattr(self, "party_role", "DPS")}
                    asyncio.get_running_loop().create_task(
                        self.coordination.broadcast_leader_state_shift("STATE", target=new_state, room_id=self.current_room_id, extra=extra)
                    )
                except Exception:
                    pass

    def flush_command_queue(self, priorities=None, keep_priorities=None) -> int:
        """Drain queued commands (best-effort). Used for P1 recovery/spin-out to clear P2/P3 backlog."""
        if priorities is None and keep_priorities is None:
            priorities = {PRIORITY_COMBAT, PRIORITY_UTILITY}
        drained = 0
        try:
            kept = []
            while True:
                try:
                    item = self.command_queue.get_nowait()
                except Exception:
                    break
                pr = item[0] if isinstance(item, tuple) and item else None
                drop = False
                if priorities is not None:
                    drop = pr in priorities
                if keep_priorities is not None:
                    drop = pr not in keep_priorities
                if drop:
                    drained += 1
                    try:
                        self.command_queue.task_done()
                    except Exception:
                        pass
                else:
                    kept.append(item)
            for item in kept:
                try:
                    self.command_queue.put_nowait(item)
                except Exception:
                    pass
        except Exception:
            return drained
        if drained:
            self.emit_event("queue_flush", {"drained": drained, "state": self.state, "reason": getattr(self, "_recovery_reason", "")})
        return drained

    def _enter_recovery_state(self, reason: str, lock_until_prompt: bool = True) -> None:
        self._recovery_reason = str(reason or "recovery")
        self._recovery_until_prompt = bool(lock_until_prompt)
        self.pending_step = None
        self.pending_arrival_room_id = None
        self.active_navigation_path = []
        self.state = STATE_RECOVERY
        if hasattr(self.analytics, "set_active_state"):
            self.analytics.set_active_state(STATE_RECOVERY)
        self.flush_command_queue(priorities={PRIORITY_COMBAT, PRIORITY_UTILITY})
        self.emit_event("recovery_enter", {"reason": self._recovery_reason, "lock_until_prompt": self._recovery_until_prompt})

    def _try_unlock_recovery(self, source: str = "prompt") -> bool:
        if self.state != STATE_RECOVERY:
            return False
        if self._recovery_until_prompt and source not in ("prompt", "verified_prompt"):
            return False
        self._recovery_reason = ""
        self._recovery_until_prompt = False
        self.set_state(STATE_NAVIGATING, "recovery_cleared")
        self.emit_event("recovery_exit", {"source": source})
        return True

    async def _handle_p1_control_intercept(self, clean: str, chunk: str = "") -> bool:
        """High-priority interceptor for PvP/control/forced displacement text (best-effort, config-driven).
        Anchored to line boundaries to avoid matching chat/comms; does not suppress chunk parsing."""
        text = (clean or "").strip()
        blob = chunk or text
        pats = getattr(self, "_compiled_parse_patterns", {}) or {}
        if text:
            for key, reason in (("stun", "stunned"), ("blind", "blinded"), ("freeze", "frozen"), ("drag", "dragged"), ("teleport_forced", "forced_teleport"), ("arena", "arena_spinout"), ("pvp_control", "pvp_control")):
                rx = pats.get(key)
                if rx and rx.search(text):
                    self._enter_recovery_state(reason)
                    return True
        fallback = [
            (r"^(?:\[[^\]]+\]\s*)?(you are stunned[!.]?|you are dazed[!.]?|you can't move[!.]?|cannot move[!.]?)$", "stunned"),
            (r"^(?:\[[^\]]+\]\s*)?(you are blinded[!.]?|everything goes dark\.?)$", "blinded"),
            (r"^(?:\[[^\]]+\]\s*)?(frozen solid[!.]?|you are frozen[!.]?)$", "frozen"),
            (r"^(?:\[[^\]]+\]\s*)?.+\bdrags you\b.*$", "dragged"),
            (r"^(?:\[[^\]]+\]\s*)?(?:.+\bteleports you\b.*|you vanish and reappear\.?|you feel a sudden wrench\.?)$", "forced_teleport"),
        ]
        for raw in (blob or "").split("\n"):
            line = raw.strip()
            if not line:
                continue
            for pat, reason in fallback:
                if re.match(pat, line, re.IGNORECASE):
                    self._enter_recovery_state(reason)
                    return True
        return False

    async def _process_stream_chunk(self, chunk: str) -> None:
        """Process a prompt-bounded chunk/paragraph to improve multi-line/packet-safe parsing.

        Stream accumulator behavior: lines are buffered until a verified prompt boundary in
        run_lifecycle, then dispatched as one paragraph chunk here so multi-line/wrapped
        text (room descs, combat bursts, prompts) can be evaluated with full context.
        """
        chunk = (chunk or "").strip()
        if not chunk:
            return
        self.last_parsed_chunk = chunk
        # P1 control intercept: enter recovery on control text but still parse/display chunk
        await self._handle_p1_control_intercept("", chunk)

        # Paragraph-level lethal/death detection -> avoidance (best-effort)
        try:
            if getattr(self, "regex_player_death", None) and self.regex_player_death.search(chunk):
                self._record_hostile_failure("lethal_death")
                self.set_state(STATE_RECOVERY, "player_death")
                self.emit_event("player_death", {"room_id": self.current_room_id})
        except Exception:
            pass

        # Paragraph-level blocked/closed-door handling for conditional/gated exits
        try:
            if re.search(r"\b(door is (closed|locked)|it's locked|the door is locked|you can't open|lock is closed)\b", chunk, re.IGNORECASE):
                step = self.pending_step or {}
                if step:
                    cmd = None
                    if step.get("required_item_id") or step.get("required_key") or step.get("requires_command"):
                        cmd = step.get("use_command") or step.get("requires_command") or step.get("unlock_command")
                        item = step.get("required_item_id") or step.get("required_key")
                        if not cmd and item:
                            cmd = f"use {item}"
                    if not cmd and (step.get("requires_lockpick") or self.lockpick_enabled):
                        cmd = str(step.get("lockpick_cmd") or self.lockpick_command or "")
                    if cmd:
                        await self.inject_command(PRIORITY_UTILITY, cmd)
                        self.emit_event("gate_recovery", {"command": cmd, "step": step})
        except Exception:
            pass

        for raw in chunk.split("\n"):
            await self.parse_stream_line(raw)

    def _hp_pct(self) -> float:
        return (self.hp / self.max_hp * 100.0) if self.max_hp else 0.0

    def get_zone_status_map(self) -> Dict[str, str]:
        statuses: Dict[str, str] = {}
        for key, cfg in self.paths_data.items():
            zone_key = str(cfg.get("zone_color_key") or key)
            statuses[zone_key] = self.analytics.path_status(key, float(cfg.get("min_xp_hr", 0.0)))
        # Ensure known zones have a default
        for rid, attrs in self.mapper.raw_room_data.items():
            z = str(attrs.get("zone", "unknown"))
            statuses.setdefault(z, "neutral")
        return statuses

    def get_map_snapshot(self) -> Dict[str, Any]:
        active_nodes: List[str] = [str(s["from_node"]) for s in self.active_navigation_path if s.get("from_node") is not None]
        if self.active_navigation_path:
            to_node = self.active_navigation_path[-1].get("to_node")
            if to_node is not None:
                active_nodes.append(str(to_node))
        zone_status = self.get_zone_status_map()
        ascii_map = self.mapper.render_ascii_map(self.current_room_id, active_nodes, zone_status, risk_rooms=self.risk_rooms)
        room = self.mapper.raw_room_data.get(str(self.current_room_id), {}) if self.current_room_id is not None else {}
        exits = list((room.get("exits") or {}).keys())
        return {
            "current_room_id": self.current_room_id,
            "current_room_name": self.current_room_name or room.get("name", ""),
            "active_path_key": self.active_path_key,
            "active_nodes": active_nodes,
            "zone_status": zone_status,
            "risk_rooms": sorted(self.risk_rooms),
            "ascii": ascii_map,
            "exits": exits,
        }

    # --- Queue / dispatch ---
    async def inject_command(self, priority: int, command_string: str) -> None:
        if not command_string:
            return
        cmd = str(command_string)
        # simple alias expansion (exact/first-token), configurable
        if not cmd.startswith("@") and self.aliases:
            parts = cmd.split(None, 1)
            if parts:
                key = parts[0].lower()
                if key in self.aliases:
                    repl = self.aliases[key]
                    cmd = repl + ((" " + parts[1]) if len(parts) > 1 else "")
        await self.command_queue.put((priority, time.monotonic(), cmd))
        print(f"[Queue] P{priority} <- {cmd}")
        self.emit_event("queue", {"priority": priority, "command": cmd, "depth": self.command_queue.qsize()})

    async def outbound_scheduler_loop(self) -> None:
        print("[*] Tri-tier outbound dispatcher engaged.")
        while self.is_running:
            priority, _, command = await self.command_queue.get()
            # adaptive pacing: combat respects learned server tick to avoid flood/bottleneck
            tick = self.analytics.get_recommended_cast_delay(default=0.3) if hasattr(self.analytics, "get_recommended_cast_delay") else 0.3
            if priority == PRIORITY_SURVIVAL:
                delay_offset = 0.0
            elif priority == PRIORITY_COMBAT:
                delay_offset = max(0.05, float(tick or 0.0)) if (self.state == STATE_COMBAT or getattr(self, "_round_in_progress", False)) else 0.05
            else:
                delay_offset = 0.25
            if delay_offset > 0:
                await asyncio.sleep(delay_offset)
            print(f"[Dispatcher] P{priority} -> {command}")
            try:
                if self.writer is None:
                    raise RuntimeError("socket writer unavailable")
                payload = str(command)
                # allow raw payloads (e.g. BBS replies with \r) when explicitly marked
                if payload.endswith("\r") or payload.endswith("\n"):
                    data = payload.encode("utf-8", errors="ignore")
                else:
                    data = f"{payload}\n".encode("utf-8", errors="ignore")
                self.writer.write(data)
                await self.writer.drain()
                self.emit_event("dispatch", {"priority": priority, "command": command})
            except Exception as e:
                print(f"[-] Dispatch failure: {e}")
                self.is_running = False
                break
            finally:
                self.command_queue.task_done()

    async def _send_raw(self, text: str) -> None:
        if self.writer is None or text is None:
            return
        try:
            self.writer.write(str(text).encode("utf-8", errors="ignore"))
            await self.writer.drain()
            self.emit_event("raw_send", {"text": text})
        except Exception as e:
            print(f"[-] Raw send failure: {e}")

    async def _process_login_buffer(self, clean_buffer: str) -> None:
        """Best-effort MegaMUD-style prompt/reply login scripting over incoming stream."""
        if self._bbs_logged_in or not self.bbs_cfg:
            return
        prompts = list(self.bbs_cfg.get("logon_prompts", []) or [])
        replies = list(self.bbs_cfg.get("logon_replies", []) or [])
        # env/keyring-friendly overrides (do not store credentials in profiles.json)
        env_user = os.environ.get("APEXFORGE_BBS_USER") or os.environ.get("APEXFORGE_USER")
        env_pass = os.environ.get("APEXFORGE_BBS_PASS") or os.environ.get("APEXFORGE_PASS")
        # advance through prompts in order
        while self._bbs_login_index < len(prompts):
            p = str(prompts[self._bbs_login_index] or "").strip()
            if not p:
                self._bbs_login_index += 1
                continue
            if p.lower() in clean_buffer.lower():
                reply = str(replies[self._bbs_login_index]) if self._bbs_login_index < len(replies) else ""
                # heuristic: substitute env creds for empty placeholder replies on likely user/pass prompts
                pl = p.lower()
                if (not reply.strip()) and env_user and any(k in pl for k in ("name", "user", "login", "account", "handle")):
                    reply = env_user + "\r"
                elif (not reply.strip()) and env_pass and any(k in pl for k in ("pass", "password", "pw")):
                    reply = env_pass + "\r"
                self._bbs_login_index += 1
                if reply:
                    print(f"[Login] Prompt matched -> sending reply #{self._bbs_login_index}")
                    await self._send_raw(reply)
                continue
            break
        # menu/game prompt detection
        if self._bbs_menu_prompt and self._bbs_menu_prompt.lower() in clean_buffer.lower() and not self._bbs_logged_in:
            self._bbs_logged_in = True
            print(f"[Login] Menu prompt detected: {self._bbs_menu_prompt}")
            if self._bbs_ansi_cmd:
                await self._send_raw(self._bbs_ansi_cmd + "\r")
            self.emit_event("login_complete", {"menu_prompt": self._bbs_menu_prompt})

    # --- Navigation / path optimization ---
    async def execute_destination_routing(self, target_room_id: str) -> bool:
        route = self.mapper.compute_dijkstra_route(self.current_room_id, str(target_room_id))
        if not route:
            print(f"[-] Route failed {self.current_room_id} -> {target_room_id}")
            self.emit_event("route_failed", {"from": self.current_room_id, "to": str(target_room_id)})
            return False
        self.active_navigation_path = route
        self.set_state(STATE_NAVIGATING, "route_loaded")
        print(f"[Pathing] {self.current_room_id} -> {target_room_id} ({len(route)} steps)")
        self.emit_event("route_loaded", {"from": self.current_room_id, "to": str(target_room_id), "steps": len(route)})
        self.emit_event("map_update", self.get_map_snapshot())
        await self.trigger_next_path_step()
        return True

    async def trigger_next_path_step(self) -> None:
        if self.state in (STATE_RESTING, STATE_RECOVERY):
            if self.state == STATE_RECOVERY:
                self.emit_event("movement_locked", {"reason": getattr(self, "_recovery_reason", "recovery")})
            return
        # Party coordination: leader pauses when waiting for followers to heal/rest
        if self._party_blocked():
            self.emit_event("party_wait_block", {"waiting_for": [k for k, v in self.party_wait_flags.items() if v]})
            return
        if not self.active_navigation_path:
            self.pending_step = None
            self.pending_arrival_room_id = None
            self.emit_event("route_complete", {"room_id": self.current_room_id})
            if self.active_path_loop:
                await self._advance_path_loop()
            return

        step = self._path_step_overrides(self.active_navigation_path.pop(0))
        # min-monsters / room conditions best-effort: if configured and room seems empty, optionally delay/skip by requeueing step
        min_mon = int(step.get("min_monsters", 0) or 0)
        if min_mon > 0 and self.room_monster_count < min_mon:
            # if not enough monsters in current context, wait briefly and retry same step
            self.active_navigation_path.insert(0, step)
            self.emit_event("min_monsters_wait", {"need": min_mon, "have": self.room_monster_count, "room": self.current_room_id})
            await asyncio.sleep(1.0)
            return

        self.pending_step = step
        self.pending_arrival_room_id = str(step.get("to_node"))
        # conditional edge self-heal hooks (best-effort)
        if step.get("requires_lockpick") and self.lockpick_enabled and self.lockpick_command:
            await self.inject_command(PRIORITY_UTILITY, str(step.get("lockpick_cmd") or self.lockpick_command))
        req_item = step.get("required_item_id") or step.get("required_key")
        if req_item and not (self.inventory.get(str(req_item), 0) > 0 or str(req_item) in self.inventory):
            self.emit_event("edge_condition_blocked", {"step": step, "reason": "missing_required_item", "item": req_item})
        if self.global_pre_move_cmd:
            await self.inject_command(PRIORITY_UTILITY, self.global_pre_move_cmd)
        if step.get("pre_cmd"):
            await self.inject_command(PRIORITY_UTILITY, step["pre_cmd"])
        # path-step party command hook (@party semantics)
        if step.get("party_cmd"):
            party_cmd = str(step.get("party_cmd"))
            await self.coordination.broadcast_leader_state_shift("PARTY_CMD", target=party_cmd, room_id=self.current_room_id)
            await self.inject_command(PRIORITY_UTILITY, party_cmd)
        await self.inject_command(PRIORITY_UTILITY, step["direction"])
        self._last_move_dispatch_ts = time.monotonic()
        self.emit_event("step_dispatched", step)

    async def _advance_path_loop(self) -> None:
        if not self.active_path_loop or len(self.active_path_loop) < 2:
            return
        # lap timing/telemetry and min-lap enforcement
        if self.active_path_key:
            lap_time = self.analytics.lap_end(self.active_path_key)
            if lap_time is not None:
                self.emit_event("lap_complete", {"path": self.active_path_key, "lap_time": lap_time, "stats": self.analytics.lap_stats(self.active_path_key)})
            cfg = self.paths_data.get(self.active_path_key, {})
            min_lap = float(cfg.get("min_lap_time", 0.0) or 0.0)
            if min_lap > 0 and lap_time is not None and lap_time < min_lap:
                wait_s = min_lap - lap_time
                print(f"[Pathing] Min lap wait {wait_s:.2f}s for {self.active_path_key}")
                self.emit_event("lap_wait", {"path": self.active_path_key, "wait": wait_s})
                await asyncio.sleep(wait_s)
            self.analytics.lap_start(self.active_path_key)
        await self._maybe_dynamic_reroute()
        self.active_path_index = (self.active_path_index + 1) % (len(self.active_path_loop) - 1)
        next_idx = self.active_path_index + 1
        target = self.active_path_loop[next_idx]
        await self.execute_destination_routing(target)

    async def start_path(self, path_key: str) -> bool:
        path_cfg = self.paths_data.get(path_key)
        if not path_cfg:
            print(f"[-] Unknown path key: {path_key}")
            return False
        raw_nodes = [str(n) for n in path_cfg.get("loop_nodes", [])]
        # normalize: collapse consecutive duplicates (common in imported .mp traversals)
        loop_nodes = []
        for n in raw_nodes:
            if not loop_nodes or loop_nodes[-1] != n:
                loop_nodes.append(n)
        # if closed loop duplicated final==first and length>2 keep as-is; else ensure minimal form
        if len(loop_nodes) >= 2 and loop_nodes[0] == loop_nodes[-1] and len(loop_nodes) > 2:
            # keep closure node for loop semantics
            pass
        if len(loop_nodes) < 2:
            print(f"[-] Path '{path_key}' has insufficient loop_nodes")
            return False
        # validate graph continuity for loop (Step 1): require routable adjacent pairs
        missing_pairs = []
        for i in range(len(loop_nodes) - 1):
            a, b = str(loop_nodes[i]), str(loop_nodes[i + 1])
            if a == b:
                missing_pairs.append(f"{a}->{b}")
                continue
            if not self.mapper.graph.has_node(a) or not self.mapper.graph.has_node(b) or not self.mapper.compute_dijkstra_route(a, b):
                missing_pairs.append(f"{a}->{b}")
        if missing_pairs:
            msg = f"path '{path_key}' has unroutable segment(s): {', '.join(missing_pairs[:5])}"
            print(f"[-] {msg}")
            self.emit_event("route_failed", {"path": path_key, "reason": "unroutable_loop", "missing": missing_pairs})
            return False
        self.active_path_key = path_key
        self.active_path_loop = loop_nodes
        self.active_path_index = 0
        self.analytics.lap_start(path_key)
        start_target = loop_nodes[1] if loop_nodes[0] == self.current_room_id else loop_nodes[0]
        print(f"[Pathing] Starting path '{path_key}' -> {start_target}")
        self.emit_event("path_started", {"path": path_key, "target": start_target})
        self.emit_event("map_update", self.get_map_snapshot())
        await self.coordination.broadcast_leader_state_shift("PATH", target=path_key, room_id=self.current_room_id)
        return await self.execute_destination_routing(start_target)

    async def _maybe_dynamic_reroute(self) -> None:
        # expire/restore avoidance penalties periodically
        await self._maintain_avoidance_penalties()
        if not self.active_path_key:
            return
        now = time.time()
        if now - self._last_efficiency_check < 15:
            return
        self._last_efficiency_check = now
        cfg = self.paths_data.get(self.active_path_key, {})
        min_xp_hr = float(cfg.get("min_xp_hr", 0.0))
        eff = self.analytics.get_path_efficiency(self.active_path_key)
        fallback = cfg.get("fallback_path")
        if min_xp_hr > 0 and eff > 0 and eff < min_xp_hr and fallback and fallback in self.paths_data and fallback != self.active_path_key:
            # increase edge weights on current loop to penalize in graph recalculations
            self.mapper.adjust_path_weights(self.active_path_loop, multiplier=2.5)
            print(f"[Optimizer] Efficiency {eff:.1f} < {min_xp_hr:.1f}; rerouting {self.active_path_key} -> {fallback}")
            await self.coordination.dispatch_discord_alert("Dynamic Path Reroute", f"{self.active_path_key} {eff:.1f} xp/hr < {min_xp_hr:.1f}; switching to {fallback}", "WARNING")
            await self.start_path(str(fallback))

    async def handle_room_arrival(self, room_id: str, source: str) -> None:
        room_id = str(room_id)
        if room_id == self.current_room_id and not self.pending_step:
            return
        prev = self.current_room_id
        self.current_room_id = room_id
        self.current_room_name = self.mapper.raw_room_data.get(room_id, {}).get("name", "")
        self.recent_room_history = (self.recent_room_history + [room_id])[-10:]
        self.room_sequence = (self.room_sequence + [room_id])[-3:]
        self.analytics.record_room_clear()
        self.orientation_recovering = False
        self.room_players = []
        self.room_monster_count = 0
        self.current_target = None
        print(f"[Mapper] Arrival {room_id} ({self.current_room_name}) via {source} from {prev}")
        self.emit_event("room_arrival", {"room_id": room_id, "room_name": self.current_room_name, "source": source, "seq": list(self.room_sequence)})
        # broadcast leader movement/state to followers; follower reports position
        if self.is_leader:
            await self.coordination.broadcast_leader_state_shift("MOVE", target=str(prev), room_id=room_id)
        await self._send_follower_bus_status("room_arrival")

        # Multi-node re-sync: if observed room is part of active loop, realign loop index (MegaMUD-style resync by last rooms walked)
        resynced_to_loop = False
        if self.active_path_loop and room_id in self.active_path_loop:
            try:
                idx = self.active_path_loop.index(room_id)
                if len(self.active_path_loop) > 1 and idx == len(self.active_path_loop) - 1:
                    idx = 0
                self.active_path_index = idx
                resynced_to_loop = True
            except ValueError:
                pass
        if len(self.room_sequence) == 3 and self.pending_arrival_room_id and room_id != self.pending_arrival_room_id:
            print(f"[Mapper] 3-node window {self.room_sequence} observed at {room_id}; evaluating resync (loop_aligned={resynced_to_loop})")

        # Restock / recovery behavior on configured room
        if room_id == self.restock_room_id and self.state == STATE_RESTING:
            for cmd in self.restock_commands:
                await self.inject_command(PRIORITY_UTILITY, cmd)
        # Inventory/economy active-mode transactions at bank/stash/restock (best-effort hooks)
        if self.active_mode and self.auto_withdraw and room_id and room_id == self.bank_room_id:
            for cmd in self.econ_cfg.get("withdraw_commands", []):
                await self.inject_command(PRIORITY_UTILITY, str(cmd))
        if self.active_mode and self.auto_deposit and room_id and room_id == self.bank_room_id:
            for cmd in self.econ_cfg.get("deposit_commands", []):
                await self.inject_command(PRIORITY_UTILITY, str(cmd))
        if self.active_mode and self.auto_sell and room_id == self.restock_room_id:
            for cmd in self.econ_cfg.get("sell_commands", []):
                await self.inject_command(PRIORITY_UTILITY, str(cmd))
        if self.active_mode and self.auto_buy and room_id == self.restock_room_id:
            for cmd in self.econ_cfg.get("buy_commands", []):
                await self.inject_command(PRIORITY_UTILITY, str(cmd))

        if self.pending_step and str(self.pending_step.get("to_node")) == room_id:
            post_cmd = self.pending_step.get("post_cmd")
            delay = float(self.pending_step.get("delay", 0.0) or 0.0)
            if self._last_move_dispatch_ts is not None:
                move_s = max(0.0, time.monotonic() - self._last_move_dispatch_ts)
                self.analytics.record_move_time(move_s)
                self._last_move_dispatch_ts = None
            self.pending_step = None
            self.pending_arrival_room_id = None
            if post_cmd:
                await self.inject_command(PRIORITY_UTILITY, post_cmd)
            if self.global_post_move_cmd:
                await self.inject_command(PRIORITY_UTILITY, self.global_post_move_cmd)
            if delay > 0:
                await asyncio.sleep(delay)
            self.emit_event("map_update", self.get_map_snapshot())
            await self.trigger_next_path_step()
        elif self.pending_step:
            if self._last_move_dispatch_ts is not None:
                move_s = max(0.0, time.monotonic() - self._last_move_dispatch_ts)
                self.analytics.record_move_time(move_s)
                self._last_move_dispatch_ts = None
            if resynced_to_loop and self.active_path_loop:
                self.pending_step = None
                self.pending_arrival_room_id = None
                self.active_navigation_path = []
                print(f"[Mapper] Resynced to loop room {room_id}; continuing path '{self.active_path_key}'")
                await self._advance_path_loop()
            else:
                target = self.pending_arrival_room_id
                self.pending_step = None
                self.active_navigation_path = []
                if target:
                    print(f"[Mapper] Unexpected room; resyncing to {target}")
                    await self.execute_destination_routing(target)
        else:
            if resynced_to_loop and self.active_path_loop and self.state == STATE_NAVIGATING:
                self.active_navigation_path = []
                await self._advance_path_loop()
            self.emit_event("map_update", self.get_map_snapshot())

    async def trigger_orientation_recovery(self, reason: str) -> None:
        if self.orientation_recovering:
            return
        self.orientation_recovering = True
        print(f"[Recovery] Orientation recovery triggered: {reason}")
        self.emit_event("orientation_recovery", {"reason": reason, "room_id": self.current_room_id, "seq": list(self.room_sequence)})
        # stop current motion and attempt safe recovery
        self.pending_step = None
        self.pending_arrival_room_id = None
        self.active_navigation_path = []
        for cmd in self.recovery_commands:
            await self.inject_command(PRIORITY_UTILITY, cmd)
        # if we have a recent known room, try to reroute to restock/safe point
        safe_target = self.restock_room_id or self.current_room_id
        await self.execute_destination_routing(safe_target)

    def mark_room_risk(self, room_id: str, reason: str) -> None:
        rid = str(room_id)
        self.risk_rooms.add(rid)
        self.mapper.mark_risk(rid, risk=3.0)
        self.emit_event("risk_marked", {"room_id": rid, "reason": reason})
        self.emit_event("map_update", self.get_map_snapshot())

    async def handle_burden_swap(self) -> None:
        if self.max_carry_weight <= 0:
            return
        load_ratio = self.carried_weight / self.max_carry_weight
        if load_ratio < self.burden_drop_threshold:
            return
        # drop lowest-value, highest-weight candidates first
        scored = []
        for iid in self.drop_candidates:
            item = self.analytics.get_item(iid)
            if not item:
                continue
            val = float(item.get("value", 0))
            wt = float(item.get("weight", 0))
            scored.append((val, -wt, iid, str(item.get("name", iid))))
        scored.sort()
        for _, _, iid, name in scored[:3]:
            item = self.analytics.get_item(iid)
            if self.inventory.get(iid, 0) <= 0 and iid not in self.drop_candidates:
                continue
            await self.inject_command(PRIORITY_UTILITY, f"drop {iid}")
            if self.inventory.get(iid, 0) > 0 and item:
                self.inventory[iid] -= 1
                self.carried_weight = max(0.0, self.carried_weight - float(item.get("weight", 0) or 0))
            print(f"[Burden] Dropping low-value/heavy item {name} ({iid}) due to encumbrance risk")
            self.emit_event("burden_drop", {"item_id": iid, "name": name, "load_ratio": round(load_ratio, 3)})

    # --- Admin / remote triggers ---
    async def handle_admin_bus_command(self, msg: Dict[str, Any], writer: asyncio.StreamWriter) -> Optional[str]:
        sender = str(msg.get("from", msg.get("sender", ""))).lower()
        text = str(msg.get("text", msg.get("command", ""))).strip()
        msg_type = str(msg.get("type", "")).lower()
        if not text and msg_type != "status":
            return None
        if sender and sender not in self.admin_names and sender != str(self.leader_name).lower():
            return json.dumps({"ok": False, "error": "unauthorized sender"})
        parts = text.split() if text else []
        cmd = parts[0].lstrip("@").lower() if parts else msg_type
        arg = parts[1] if len(parts) > 1 else ""
        rest = " ".join(parts[1:]).strip()

        # bus/status sync from followers
        if msg_type == "status" or cmd == "status_sync":
            if sender:
                self.party_member_status[sender] = {
                    "name": msg.get("name", sender),
                    "hp": msg.get("hp"), "max_hp": msg.get("max_hp"),
                    "mp": msg.get("mp"), "max_mp": msg.get("max_mp"),
                    "state": msg.get("state"), "room_id": msg.get("room_id"),
                    "ts": time.time(),
                }
                if "waiting" in msg:
                    self.party_wait_flags[sender] = bool(msg.get("waiting"))
            return json.dumps({"ok": True, "ack": "status"})

        if cmd in ("status", "where"):
            ta = self.analytics.get_time_analysis()
            lap = self.analytics.lap_stats(self.active_path_key) if self.active_path_key else {}
            payload = {
                "ok": True,
                "state": self.state,
                "room_id": self.current_room_id,
                "room_name": self.current_room_name or self.mapper.raw_room_data.get(str(self.current_room_id), {}).get("name", ""),
                "hp": self.hp, "max_hp": self.max_hp, "mp": self.mp, "max_mp": self.max_mp, "mv": self.mv, "max_mv": self.max_mv,
                "need_xp": self.need_xp,
                "blessed": bool(self._blessed),
                "status_effects": {k: v for k, v in self.status_effects.items() if v},
                "login_ready": bool(self._bbs_logged_in),
                "path": self.active_path_key,
                "pending_target": self.pending_arrival_room_id,
                "steps_remaining": len(self.active_navigation_path),
                "xp_total": self.analytics.total_xp_gained,
                "xp_hr_5m": self.analytics.calculate_rolling_velocity(300),
                "walk_speed_s": self.analytics.walk_speed(),
                "rtk": self.analytics.average_rtk(),
                "rtc": self.analytics.average_rtc(),
                "lap": lap,
                "time": ta,
                "immunity_count": getattr(self.analytics, "immunity_count", lambda: 0)(),
                "party_status": self.party_member_status,
                "party_waiting": [k for k, v in self.party_wait_flags.items() if v],
            }
            if cmd == "where":
                payload["zone"] = self.mapper.raw_room_data.get(str(self.current_room_id), {}).get("zone", "")
                payload["exits"] = list(self.mapper.raw_room_data.get(str(self.current_room_id), {}).get("exits", {}).keys())
            return json.dumps(payload)

        if cmd == "speed":
            lap = self.analytics.lap_stats(self.active_path_key) if self.active_path_key else {}
            return json.dumps({
                "ok": True,
                "walk_speed_s_per_room": self.analytics.walk_speed(),
                "lap": lap,
                "rtk": self.analytics.average_rtk(),
                "rtc": self.analytics.average_rtc(),
                "xp_hr_5m": self.analytics.calculate_rolling_velocity(300),
                "path": self.active_path_key,
            })

        if cmd == "route" and arg:
            ok = await self.execute_destination_routing(arg)
            return json.dumps({"ok": ok, "target": arg})

        if cmd in ("path", "loop") and arg:
            ok = await self.start_path(arg)
            return json.dumps({"ok": ok, "path": arg, "steps_remaining": len(self.active_navigation_path)})

        if cmd == "run" and arg:
            # run to room; optionally suppress auto-combat while running (MegaMUD-like)
            prev_state = self.state
            self.run_mode_suppress_combat = bool(self.nav_cfg.get("run_mode_suppress_combat", True))
            self.set_state(STATE_NAVIGATING, "run_command")
            ok = await self.execute_destination_routing(arg)
            return json.dumps({"ok": ok, "target": arg, "mode": "run", "suppress_combat": self.run_mode_suppress_combat, "prev_state": prev_state})

        if cmd == "flee":
            await self._on_flee_event("admin_flee")
            await self.inject_command(PRIORITY_SURVIVAL, "flee")
            self.set_state(STATE_RESTING, "admin_flee")
            return json.dumps({"ok": True})

        if cmd == "stop":
            self.active_navigation_path = []
            self.active_path_loop = []
            self.active_path_key = None
            self.pending_step = None
            self.pending_arrival_room_id = None
            self.run_mode_suppress_combat = False
            self.set_state(STATE_NAVIGATING, "stopped")
            return json.dumps({"ok": True, "stopped": True})

        if cmd == "profile" and arg:
            ok = await self.apply_combat_profile(arg)
            return json.dumps({"ok": ok, "profile": arg})

        if cmd == "weapon" and arg:
            p = self._active_combat_profile()
            p["weapon"] = arg
            self.combat_profiles[str(self.current_combat_profile)] = p
            await self.inject_command(PRIORITY_COMBAT, f"wield {arg}")
            return json.dumps({"ok": True, "weapon": arg})

        if cmd == "bsweapon" and arg:
            p = self._active_combat_profile()
            p["bs_weapon"] = arg
            self.combat_profiles[str(self.current_combat_profile)] = p
            return json.dumps({"ok": True, "bs_weapon": arg})

        if cmd == "altweapon" and arg:
            p = self._active_combat_profile()
            p["alt_weapon"] = arg
            self.combat_profiles[str(self.current_combat_profile)] = p
            return json.dumps({"ok": True, "alt_weapon": arg})

        if cmd == "health":
            return json.dumps({"ok": True, "hp": self.hp, "max_hp": self.max_hp, "mp": self.mp, "max_mp": self.max_mp, "mv": self.mv, "max_mv": self.max_mv, "room": self.current_room_id})

        if cmd == "heal" and arg:
            spell = self.party_heal_spell or self.heal_spell
            await self.inject_command(PRIORITY_COMBAT, f"{spell} {arg}")
            return json.dumps({"ok": True, "heal": arg, "spell": spell})

        if cmd == "inventory":
            items = [{"id": iid, "count": cnt, "name": (self.analytics.get_item(iid) or {}).get("name", iid), "uses": self.item_uses.get(iid, (self.analytics.get_item(iid) or {}).get("uses", 0))} for iid, cnt in self.inventory.items() if cnt > 0]
            return json.dumps({"ok": True, "inventory": items})

        if cmd == "uses" and arg:
            iid = str(arg)
            item = self.analytics.get_item(iid) or {}
            uses = self.item_uses.get(iid, item.get("uses", 0))
            return json.dumps({"ok": True, "item": iid, "name": item.get("name", iid), "uses": uses})

        if cmd == "wealth":
            return json.dumps({"ok": True, "wealth_copper": self.wealth_copper, "keep_cash_copper": self.keep_cash_copper})

        if cmd in ("deposit-all", "autocash", "auto-cash"):
            cmds = list(self.econ_cfg.get("deposit_commands", [])) or (["deposit all"] if self.active_mode else [])
            for c in cmds:
                await self.inject_command(PRIORITY_UTILITY, str(c))
            return json.dumps({"ok": True, "commands": cmds})

        if cmd == "reset":
            self.risk_rooms.clear()
            self.orientation_recovering = False
            self.pending_step = None
            self.pending_arrival_room_id = None
            self.recent_room_history.clear()
            self.room_sequence.clear()
            self.set_state(STATE_NAVIGATING, "reset")
            self.emit_event("map_update", self.get_map_snapshot())
            return json.dumps({"ok": True, "reset": True})

        if cmd == "say" and rest:
            await self.inject_command(PRIORITY_COMBAT, rest)
            return json.dumps({"ok": True, "sent": rest})

        if cmd == "wait":
            # follower reports wait state to leader (processed on leader side when received as remote text)
            return json.dumps({"ok": True, "note": "wait handled via party telepath parsing"})

        if cmd == "ok":
            return json.dumps({"ok": True, "note": "ok handled via party telepath parsing"})

        if cmd == "share":
            await self._send_party_telepath("@health")
            return json.dumps({"ok": True, "shared": "health"})

        if cmd == "time":
            return json.dumps({"ok": True, "time": self.analytics.get_time_analysis()})

        if cmd == "lap":
            return json.dumps({"ok": True, "path": self.active_path_key, "lap": self.analytics.lap_stats(self.active_path_key)})

        if cmd == "recalc":
            if self.active_path_key:
                ok = await self.start_path(self.active_path_key)
                return json.dumps({"ok": ok, "recalculated": self.active_path_key})
            if self.pending_arrival_room_id:
                ok = await self.execute_destination_routing(self.pending_arrival_room_id)
                return json.dumps({"ok": ok, "recalculated": self.pending_arrival_room_id})
            return json.dumps({"ok": False, "error": "no active path or target"})

        if cmd == "map":
            snap = self.get_map_snapshot()
            return json.dumps({"ok": True, "room_id": snap.get("current_room_id"), "room_name": snap.get("current_room_name"), "zone_status": snap.get("zone_status"), "risk_rooms": snap.get("risk_rooms")})

        if cmd == "help":
            return json.dumps({"ok": True, "commands": ["@status", "@where", "@speed", "@route <room>", "@run <room>", "@path <key>", "@loop <key>", "@flee", "@stop", "@reset", "@say <cmd>", "@time", "@lap", "@recalc", "@map", "@profile <id>", "@weapon <id>", "@bsweapon <id>", "@altweapon <id>", "@health", "@heal <name>", "@inventory", "@uses <item>", "@wealth", "@deposit-all"]})

        return json.dumps({"ok": False, "error": "unknown admin command"})

    async def handle_remote_trigger_text(self, sender: str, body: str) -> None:
        sender_l = (sender or "").lower().strip()
        body = (body or "").strip()
        if not body:
            return
        # Party coordination hooks (followers -> leader)
        b = body.strip().strip('"').strip()
        if self.is_leader and sender_l and sender_l != (self.profile_name or "").lower():
            if b == "@wait":
                self.party_wait_flags[sender_l] = True
                self.emit_event("party_wait", {"from": sender_l})
                return
            if b == "@ok":
                self.party_wait_flags[sender_l] = False
                self.emit_event("party_ok", {"from": sender_l})
                # if leader was resting for party, resume when all clear
                if not self._party_blocked() and self.active_navigation_path and self.state == STATE_NAVIGATING and self.pending_step is None:
                    await self.trigger_next_path_step()
                return
            if b.startswith("@health") or b.startswith("@heal") or b.startswith("@share"):
                # accept and let leader logic/alerts react; optional: respond with own health if desired
                self.emit_event("party_health_msg", {"from": sender_l, "body": b})
                return

        # Admin/remote command execution (requires authorization)
        if sender_l and sender_l not in self.admin_names:
            return
        if not b.startswith("@"):
            b = "@" + b
        msg = {"from": sender_l or "admin", "text": b}
        resp = await self.handle_admin_bus_command(msg, None)  # type: ignore[arg-type]
        if resp:
            print(f"[AdminTrigger] {sender}: {b} -> {resp}")
            self.emit_event("admin_trigger", {"from": sender, "body": b, "response": resp})

    async def _send_party_health_response(self) -> None:
        if self.disable_health_spam:
            return
        text = f"@health HP:{self.hp}/{self.max_hp} MP:{self.mp}/{self.max_mp}"
        await self._send_party_telepath(text)

    # --- Stream parsing ---
    async def parse_stream_line(self, line: str) -> None:
        raw_line = str(line).rstrip("\r")
        clean = self.strip_ansi(raw_line).strip()
        if not clean:
            return
        # gag/filter for display (parse still runs for deterministic state)
        gagged = any(rx.search(clean) for rx in self.gag_patterns) if self.gag_patterns else False
        if not gagged:
            print(f"[Server] {clean}")
        self.emit_event("stream", {"line": clean, "raw": raw_line, "gagged": gagged})

        # Update status effects from imported MegaMUD message patterns
        self._update_status_from_line(clean)
        # Config-driven parsing patterns (Step 2, best-effort)
        self._apply_config_parse_patterns(clean)

        # Statline template parsing (ApexForge-native/tunable)
        if self.regex_statline:
            sm = self.regex_statline.search(clean)
            if sm:
                gd = sm.groupdict()
                try:
                    if gd.get("hp") is not None: self.hp = int(gd["hp"])
                    if gd.get("max_hp") is not None: self.max_hp = int(gd["max_hp"])
                    if gd.get("mp") is not None: self.mp = int(gd["mp"])
                    if gd.get("max_mp") is not None: self.max_mp = int(gd["max_mp"])
                    if gd.get("mv") is not None: self.mv = int(gd["mv"])
                    if gd.get("max_mv") is not None: self.max_mv = int(gd["max_mv"])
                    if gd.get("need") is not None: self.need_xp = int(gd["need"])
                except Exception:
                    pass
                # treat parsed statline as vitals update + possible prompt boundary
                self.emit_event("vitals", {"hp": self.hp, "max_hp": self.max_hp, "mp": self.mp, "max_mp": self.max_mp, "mv": self.mv, "max_mv": self.max_mv, "need_xp": self.need_xp, "state": self.state, "source": "statline"})
                if self._is_prompt_line(clean):
                    self._bbs_logged_in = True
                    self._try_unlock_recovery("prompt")
                    if self._round_in_progress:
                        self._round_in_progress = False
                        await self._flush_end_of_round_casts()
                # do not return; allow other parsers (room/combat cues) to run on same line if present

        # Need/XP from custom statline-style fragments (best-effort fallback)
        need_m = re.search(r"Need=(\d+)", clean, re.IGNORECASE)
        if need_m:
            try:
                self.need_xp = int(need_m.group(1))
            except Exception:
                pass
        # Prompt/end-of-round hint: menu prompt or configured prompt markers
        if self._is_prompt_line(clean):
            self._bbs_logged_in = True
            self._try_unlock_recovery("prompt")
            if self._round_in_progress:
                self._round_in_progress = False
                await self._flush_end_of_round_casts()

        # Telepaths/pages with sender capture (MegaMUD-style)
        m = self.regex_telepath.search(clean)
        if m:
            sender = m.group(1) or ""
            msg_text = m.group(2) or ""
            mt = msg_text.strip()
            self.emit_event("comms", {"kind": "telepath", "from": sender, "text": mt, "line": clean})
            # respond to @health requests
            if mt == "@health" and sender and sender.lower() != (self.profile_name or "").lower():
                await self._send_party_health_response()
            await self.handle_remote_trigger_text(sender=sender, body=mt)
        m = self.regex_page.search(clean)
        if m:
            self.emit_event("comms", {"kind": "page", "from": m.group(1), "to": m.group(2), "text": m.group(3), "line": clean})
            await self.handle_remote_trigger_text(sender=m.group(1), body=m.group(3))

        # Party health cues
        ph = self.regex_party_health.search(clean)
        if ph:
            name = (ph.group(1) or ph.group(2) or "").strip()
            pct = None
            if ph.group(3):
                try:
                    pct = float(ph.group(3))
                except Exception:
                    pct = None
            if name:
                nl = name.lower()
                self.party_member_status[nl] = {"name": name, "pct": pct, "crit": bool(ph.group(1)), "ts": time.time()}
                self.emit_event("party_health", {"name": name, "pct": pct, "critical": bool(ph.group(1))})
                # Leader: optional party heal assist
                if self.is_leader and self.party_heal_enabled and pct is not None and pct <= self.party_heal_hp_pct and self.party_heal_spell:
                    await self._schedule_cast(f"{self.party_heal_spell} {name}", PRIORITY_COMBAT)
                # If follower is self-reported low in status tracking and leader resting-applies-to-party, mark wait heuristically
                if self.is_leader and self.rest_healing_applies_to_party and pct is not None and pct <= self.hp_wait_threshold_pct:
                    self.party_wait_flags[nl] = True

        # Vitals (MegaMUD style HP/MA/MV and fallback HP/MP)
        vitf = self.regex_vitals_full.search(clean)
        vit = self.regex_vitals.search(clean)
        if vitf:
            self.hp = int(vitf.group(1)); self.max_hp = int(vitf.group(2))
            self.mp = int(vitf.group(3)); self.max_mp = int(vitf.group(4))
            self.mv = int(vitf.group(5)); self.max_mv = int(vitf.group(6))
            hp_pct = self._hp_pct()
            # Auto-heal/bless scheduling hooks
            await self._maybe_auto_heal_and_bless()
            # Follower party coordination: send @wait/@ok based on own health (rate-limited)
            await self._emit_party_health_if_needed()

            should_flee_hp = hp_pct < self.flee_hp_percentage or (self.flee_hp_absolute > 0 and self.hp <= self.flee_hp_absolute)
            if should_flee_hp:
                await self._on_flee_event("low_hp")
                await self.inject_command(PRIORITY_SURVIVAL, "flee")
                self.set_state(STATE_RESTING, "low_hp")
                await self.coordination.dispatch_discord_alert("Low HP Flee", f"HP {self.hp}/{self.max_hp} room {self.current_room_id}", "CRITICAL")
                await self.coordination.broadcast_leader_state_shift("FLEE", room_id=self.current_room_id)
                if self.current_room_id != self.restock_room_id:
                    await self.execute_destination_routing(self.restock_room_id)
            else:
                # Resting/recovery transitions with party-aware waiting
                recovered_self = hp_pct >= self.rest_hp_percentage and self.mp >= max(1, int(self.max_mp * 0.5))
                if self.state == STATE_RESTING and recovered_self:
                    if self.is_leader and self.rest_healing_applies_to_party and self._party_blocked():
                        # remain resting/waiting for @ok from followers
                        self.emit_event("rest_wait_party", {"waiting_for": [k for k, v in self.party_wait_flags.items() if v]})
                    else:
                        if self.auto_stand_command:
                            await self.inject_command(PRIORITY_UTILITY, self.auto_stand_command)
                        self.set_state(STATE_NAVIGATING, "recovered")
                        # if path pending and not dispatched, resume
                        if self.active_navigation_path and self.pending_step is None:
                            await self.trigger_next_path_step()
            self.emit_event("vitals", {"hp": self.hp, "max_hp": self.max_hp, "mp": self.mp, "max_mp": self.max_mp, "mv": self.mv, "max_mv": self.max_mv, "state": self.state})
            await self._send_follower_bus_status("vitals")
            return
        elif vit:
            self.hp = int(vit.group(1)); self.max_hp = int(vit.group(2))
            self.mp = int(vit.group(3)); self.max_mp = int(vit.group(4))

            hp_pct = self._hp_pct()
            # Auto-heal/bless scheduling hooks
            await self._maybe_auto_heal_and_bless()
            # Follower party coordination: send @wait/@ok based on own health (rate-limited)
            await self._emit_party_health_if_needed()

            should_flee_hp = hp_pct < self.flee_hp_percentage or (self.flee_hp_absolute > 0 and self.hp <= self.flee_hp_absolute)
            if should_flee_hp:
                await self._on_flee_event("low_hp")
                await self.inject_command(PRIORITY_SURVIVAL, "flee")
                self.set_state(STATE_RESTING, "low_hp")
                await self.coordination.dispatch_discord_alert("Low HP Flee", f"HP {self.hp}/{self.max_hp} room {self.current_room_id}", "CRITICAL")
                await self.coordination.broadcast_leader_state_shift("FLEE", room_id=self.current_room_id)
                if self.current_room_id != self.restock_room_id:
                    await self.execute_destination_routing(self.restock_room_id)
            else:
                # Resting/recovery transitions with party-aware waiting
                recovered_self = hp_pct >= self.rest_hp_percentage and self.mp >= max(1, int(self.max_mp * 0.5))
                if self.state == STATE_RESTING and recovered_self:
                    if self.is_leader and self.rest_healing_applies_to_party and self._party_blocked():
                        # remain resting/waiting for @ok from followers
                        self.emit_event("rest_wait_party", {"waiting_for": [k for k, v in self.party_wait_flags.items() if v]})
                    else:
                        if self.auto_stand_command:
                            await self.inject_command(PRIORITY_UTILITY, self.auto_stand_command)
                        self.set_state(STATE_NAVIGATING, "recovered")
                        # if path pending and not dispatched, resume
                        if self.active_navigation_path and self.pending_step is None:
                            await self.trigger_next_path_step()
            self.emit_event("vitals", {"hp": self.hp, "max_hp": self.max_hp, "mp": self.mp, "max_mp": self.max_mp, "mv": self.mv, "max_mv": self.max_mv, "state": self.state})
            await self._send_follower_bus_status("vitals")
            return

        # Mana tick / auto-buff rotation
        tick = self.regex_mana_sync.search(clean)
        if tick:
            self.mp = int(tick.group(1)); self.mana_tick_delta = int(tick.group(2))
            if self.mana_tick_delta == 19 and self.tick_cast_command and self.state != STATE_RESTING and self.auto_bless:
                # treat tick_cast_command as bless/cast rotation hook
                await self.inject_command(PRIORITY_COMBAT, self.tick_cast_command)
            self.emit_event("mana_tick", {"mp": self.mp, "delta": self.mana_tick_delta})
            return

        # XP gain / velocity + dynamic path efficiency
        xp_m = self.regex_xp_gain.search(clean)
        if xp_m:
            gained = int(xp_m.group(1))
            self.analytics.total_xp_gained += gained
            self.analytics.append_xp_data_point(self.analytics.total_xp_gained)
            path_eff = self.analytics.record_path_xp(self.active_path_key, gained)
            self.emit_event("xp", {"gained": gained, "total": self.analytics.total_xp_gained, "xp_hr_5m": self.analytics.calculate_rolling_velocity(300), "path_eff": path_eff})
            if self.state == STATE_COMBAT:
                self.analytics.record_kill()
                self.set_state(STATE_NAVIGATING, "kill_resolved")
                if self.corpse_loot_command:
                    await self.inject_command(PRIORITY_UTILITY, self.corpse_loot_command)
            self.emit_event("map_update", self.get_map_snapshot())
            return

        # Room players/presence (auto-nuke safety)
        ah = self.regex_also_here.search(clean)
        if ah:
            names = [n.strip() for n in ah.group(1).split(",") if n.strip()]
            self.room_players = names
            self.emit_event("room_players", {"players": names, "non_party": self._players_in_room_excluding_party()})

        # Lockpick intervention and lost/can't-go handling
        if await self._handle_lockpick_if_needed(clean):
            # after lockpick attempt, do not also treat as generic block in same line
            pass
        await self._handle_lost_or_blocked(clean)

        # Self-healing orientation machine (darkness/blindness/teleport traps)
        if self.regex_disorient.search(clean):
            await self.trigger_orientation_recovery(clean)

        # Inventory validation re-check
        if self.regex_inventory_unknown.search(clean) and self.auto_recheck_inventory_on_unknown:
            now = time.time()
            if now - self._last_inventory_recheck_ts > 2.0:
                self._last_inventory_recheck_ts = now
                await self.inject_command(PRIORITY_UTILITY, "inventory")
                self.emit_event("inventory_recheck", {"reason": clean})

        # Immunity detection -> memory DB
        im = self.regex_immunity.search(clean)
        if im:
            mob = (im.group(1) or im.group(2) or "").strip()
            if mob:
                self.analytics.record_immunity(mob, immune_type="physical", source=clean)
                self.emit_event("immunity_recorded", {"mob": mob})

        # Blocked movement / path friction cues (MegaMUD-style resync/retry behavior hooks)
        if re.search(r"\b(blocked|door is closed|lock is closed|the door is locked|it's locked|you can't open|you are too heavy|too encumbered|can't move)\b", clean, re.IGNORECASE):
            self.emit_event("movement_blocked", {"line": clean, "room_id": self.current_room_id, "pending": self.pending_arrival_room_id})
            # conditional gate validation: try key/use/lockpick command from pending edge before reroute
            step = self.pending_step or {}
            gate_cmd = None
            if step:
                req_item = step.get("required_item_id") or step.get("required_key")
                if step.get("requires_command") or step.get("unlock_command") or step.get("use_command"):
                    gate_cmd = str(step.get("requires_command") or step.get("unlock_command") or step.get("use_command"))
                elif req_item:
                    gate_cmd = f"use {req_item}"
                elif (step.get("requires_lockpick") or self.lockpick_enabled) and (step.get("lockpick_cmd") or self.lockpick_command):
                    gate_cmd = str(step.get("lockpick_cmd") or self.lockpick_command)
            if gate_cmd:
                await self.inject_command(PRIORITY_UTILITY, gate_cmd)
                self.emit_event("gate_recovery", {"command": gate_cmd, "step": step})
            if self.pending_arrival_room_id and self.state == STATE_NAVIGATING:
                target = self.pending_arrival_room_id
                self.pending_step = None
                self.active_navigation_path = []
                await asyncio.sleep(0.5)
                await self.execute_destination_routing(target)

        # Stealth/invisible tracker -> mark high-risk node
        if self.regex_stealth.search(clean):
            self.mark_room_risk(self.current_room_id, reason=clean)
            self.emit_event("stealth_signal", {"line": clean, "room_id": self.current_room_id})

        # Targeted spell interruption hook (immediate combat interrupt)
        if self.regex_casting.search(clean) and self.spell_interrupt_command and self.state in (STATE_COMBAT, STATE_NAVIGATING):
            await self.inject_command(PRIORITY_SURVIVAL, self.spell_interrupt_command)
            self.emit_event("spell_interrupt", {"line": clean, "command": self.spell_interrupt_command})

        # Corpse-snipe interceptor on boss/kill cues (instant P1)
        if self.regex_boss_death.search(clean) or ("corpse" in clean.lower() and "falls" in clean.lower()):
            cmd = self.corpse_snipe_command or self.corpse_loot_command
            if cmd:
                await self.inject_command(PRIORITY_SURVIVAL, cmd)
                self.emit_event("corpse_snipe", {"line": clean, "command": cmd})

        # Dynamic inventory burden swapper on hostile entry
        # Min-monsters flee guard: do NOT flee from a monster that walks in while resting
        if self.regex_enemy_enter.search(clean) and "you" not in clean.lower():
            self.room_monster_count = max(self.room_monster_count, 1) + 0  # heuristic bump when unknown
            if self.state != STATE_RESTING:
                await self.handle_burden_swap()
                # min-monsters flee rule (best-effort): if configured threshold exceeded, flee unless resting
                path_cfg = self.paths_data.get(self.active_path_key or "", {}) if self.active_path_key else {}
                min_mon_flee = int(path_cfg.get("min_monsters_flee", self.min_monsters_flee) or self.min_monsters_flee or 0)
                if min_mon_flee > 0 and self.room_monster_count >= min_mon_flee:
                    await self._on_flee_event("min_monsters_flee")
                    await self.inject_command(PRIORITY_SURVIVAL, "flee")
                    self.set_state(STATE_RESTING, "min_monsters_flee")
            self.emit_event("enemy_enter", {"line": clean, "room_id": self.current_room_id})
        elif self.regex_enemy_enter_generic.search(clean) and "you" not in clean.lower():
            # generic arrivals: best-effort monster count tracking
            self.room_monster_count += 1

        # Combat / deterministic state transitions
        if self.regex_combat_hit.search(clean):
            if self.auto_combat_enabled and not (self.run_mode_suppress_combat and self.state == STATE_NAVIGATING):
                self.set_state(STATE_COMBAT, "combat_hit")
            self.analytics.record_round()
            self._round_in_progress = True
            # end-of-round cast flush heuristic when prompt/round hints appear nearby
            if self.regex_round_end_hint.search(clean):
                await self._flush_end_of_round_casts()
        if self.regex_kill.search(clean):
            self.analytics.record_kill()
            self._round_in_progress = False
            self._pending_end_of_round_casts.clear()
            self.set_state(STATE_NAVIGATING, "kill_text")
            if self.corpse_loot_command:
                await self.inject_command(PRIORITY_UTILITY, self.corpse_loot_command)
            self.room_monster_count = max(0, self.room_monster_count - 1)
        if self.regex_corpse.search(clean) and self.corpse_loot_command and self.state != STATE_RESTING:
            await self.inject_command(PRIORITY_UTILITY, self.corpse_loot_command)

        # Party sync
        pm = self.regex_party_attack.search(clean)
        if pm:
            leader, target = pm.group(1), pm.group(2).strip()
            if leader.lower() == str(self.leader_name).lower():
                await self.coordination.broadcast_leader_state_shift("ATTACK", target=target, room_id=self.current_room_id)
                self.current_target = target
                await self.inject_command(PRIORITY_COMBAT, f"attack {target}")
                self.set_state(STATE_COMBAT, "party_sync")

        # PvP / ambush
        if self.regex_pvp.search(clean):
            await self.coordination.dispatch_discord_alert("PvP/Ambush", clean, "CRITICAL")
            await self.handle_burden_swap()
            if self.emergency_hangup_on_pvp:
                await self.inject_command(PRIORITY_SURVIVAL, "quit")

        # Auto-nuke logic (best-effort, configurable): abort if non-party players present unless pvp mode is "do nothing"
        if self.auto_nuke and self.nuke_spell and self.state == STATE_COMBAT and self.room_monster_count >= self.min_nuke_monsters:
            non_party = self._players_in_room_excluding_party()
            allow_nuke = True
            if non_party and self.pvp_mode != "do nothing":
                allow_nuke = False
            if allow_nuke:
                now = time.time()
                if now - self._last_nuke_ts > 2.0:
                    self._last_nuke_ts = now
                    await self._schedule_cast(self.nuke_spell, PRIORITY_COMBAT)

        # Loot / BiS local lookup + inventory tracking
        lm = self.regex_loot_item.search(clean)
        if lm:
            item_name = lm.group(1).strip()
            item_id = lm.group(2)
            if item_id:
                item = self.analytics.get_item(str(item_id))
                self.inventory[str(item_id)] = self.inventory.get(str(item_id), 0) + 1
                if item:
                    self.carried_weight += float(item.get("weight", 0.0))
                if self.analytics.evaluate_loot_drop_upgrade(str(item_id)):
                    slot = self.analytics.item_db.get(str(item_id), {}).get("slot")
                    if slot:
                        await self.inject_command(PRIORITY_UTILITY, f"equip {item_id}")
                        self.analytics.update_equipped_state(slot, str(item_id))
            self.emit_event("loot", {"name": item_name, "id": item_id, "carried_weight": round(self.carried_weight, 2)})

        # Obvious exits capture (MajorMUD/BBS variants) for mapper/UX context
        exits_m = re.search(r"(?:obvious|visible|apparent)\s+exits?\s*[:\-]\s*(.+)$", clean, re.IGNORECASE)
        if exits_m:
            raw_exits = exits_m.group(1)
            parts = re.split(r",|\band\b", raw_exits, flags=re.IGNORECASE)
            exits = [p.strip().strip(".").lower() for p in parts if p and p.strip() and p.strip().lower() not in {"none"}]
            self.current_room_exits = exits
            self.emit_event("exits", {"room_id": self.current_room_id, "exits": exits, "line": clean})

        # Room arrival by runtime resolution: (name + obvious exits) -> deterministic hash -> code/id -> name
        resolved = self.mapper.resolve_room(room_name=clean, obvious_exits=self.current_room_exits or None)
        if resolved and resolved != str(self.current_room_id):
            await self.handle_room_arrival(resolved, source="room_resolve")
            return
        # fallback: normalized/substring name match (legacy)
        if not resolved:
            clean_norm = " ".join(clean.lower().split())
            for room_id, attrs in self.mapper.raw_room_data.items():
                room_name = (attrs.get("name") or "").strip()
                if not room_name:
                    continue
                rn = " ".join(room_name.lower().split())
                if rn and (rn == clean_norm or (len(rn) >= 5 and rn in clean_norm)):
                    await self.handle_room_arrival(room_id, source="room_name")
                    return

    # --- Lifecycle ---
    def _record_hostile_failure(self, reason: str = "flee") -> None:
        """Anti-loop: log flee/lethal failure for current room and apply avoidance penalty if threshold hit."""
        try:
            rid = str(self.current_room_id or "")
            monsters = []
            if getattr(self, "current_target", None):
                monsters.append(str(self.current_target))
            # best-effort: include recent room player/mob context not tracked as names; keep minimal
            res = self.analytics.record_room_failure(rid, monsters=monsters, reason=reason)
            if res and res.get("avoided"):
                changed = self.mapper.apply_avoidance_penalty(rid, penalty=9999.0)
                self.risk_rooms.add(rid)
                self.emit_event("room_avoided", {"room_id": rid, "count": res.get("count"), "until_ts": res.get("until_ts"), "edges": changed, "reason": reason})
                self.emit_event("map_update", self.get_map_snapshot())
            else:
                self.emit_event("room_failure", res or {"room_id": rid, "reason": reason})
        except Exception:
            pass

    async def _maintain_avoidance_penalties(self) -> None:
        """Clear expired avoidance penalties so Dijkstra can reuse rooms after timer."""
        try:
            expired = self.analytics.expired_avoidances()
            for rid in expired:
                self.mapper.clear_avoidance_penalty(str(rid))
                if str(rid) in self.risk_rooms:
                    # keep generic risk marking separate; only drop if we added purely for avoidance? best-effort clear
                    pass
                self.emit_event("room_avoidance_expired", {"room_id": rid})
            if expired:
                self.emit_event("map_update", self.get_map_snapshot())
        except Exception:
            pass

    async def _on_flee_event(self, reason: str) -> None:
        self._record_hostile_failure(reason)

    async def _leader_combo_heal_if_needed(self, leader_name: str, leader_state: str, leader_hp_pct: Optional[float]) -> None:
        """Follower HEALER: P1 targeted heal when leader in COMBAT and HP < threshold."""
        if not self.is_follower:
            return
        role = str(getattr(self, "party_role", "DPS") or "DPS").upper()
        if role != "HEALER":
            return
        if str(leader_state or "").upper() != "COMBAT":
            return
        if leader_hp_pct is None:
            return
        try:
            if float(leader_hp_pct) >= float(getattr(self, "leader_assist_heal_pct", 40.0) or 40.0):
                return
        except Exception:
            return
        now = time.time()
        if now - float(getattr(self, "_last_combo_heal_ts", 0.0) or 0.0) < 2.0:
            return
        spell = str(getattr(self, "party_heal_spell_leader", "") or self.party_heal_spell or self.heal_spell or "").strip()
        if not spell:
            return
        target = leader_name or self.leader_name or ""
        cmd = f"{spell} {target}".strip()
        self._last_combo_heal_ts = now
        await self.inject_command(PRIORITY_SURVIVAL, cmd)
        self.emit_event("combo_heal", {"role": role, "leader": target, "hp_pct": leader_hp_pct, "cmd": cmd})

    # --- Telnet / GMCP (OOB) negotiation (best-effort, fallback to regex) ---
    async def _send_telnet_gmcp_will(self) -> None:
        """Offer GMCP; also be ready to accept server-initiated GMCP and refuse other options."""
        if not self.writer or not getattr(self, "gmcp_enabled", True):
            return
        try:
            IAC, WILL = 255, 251
            GMCP = 201
            # IAC WILL GMCP (offer/advertise support)
            self.writer.write(bytes([IAC, WILL, GMCP]))
            await self.writer.drain()
            self.gmcp_negotiated = False
            self.emit_event("gmcp", {"event": "will_sent"})
        except Exception:
            pass

    def _process_telnet_and_extract_text(self, data: bytes) -> str:
        """Handle basic telnet negotiation and GMCP subnegotiations; return printable text chunk."""
        if not data:
            return ""
        IAC, WILL, WONT, DO, DONT, SB, SE = 255, 251, 252, 253, 254, 250, 240
        GMCP = 201
        buf = getattr(self, "_telnet_buffer", bytearray()) + bytearray(data)
        out = bytearray()
        i = 0
        gmcp_msgs = []
        replies = bytearray()
        while i < len(buf):
            b = buf[i]
            if b == IAC:
                if i + 1 >= len(buf):
                    break
                cmd = buf[i + 1]
                if cmd == IAC:
                    out.append(IAC); i += 2; continue
                if cmd in (WILL, WONT, DO, DONT):
                    if i + 2 >= len(buf):
                        break
                    opt = buf[i + 2]
                    if opt == GMCP and getattr(self, "gmcp_enabled", True):
                        # GMCP: accept either direction
                        if cmd == DO:
                            # server asks us to enable GMCP -> WILL
                            replies += bytes([IAC, WILL, GMCP])
                            self.gmcp_supported = True
                            self.gmcp_negotiated = True
                            gmcp_msgs.append(("negotiated", {"supported": True}))
                        elif cmd == WILL:
                            # server offers GMCP -> DO
                            replies += bytes([IAC, DO, GMCP])
                            self.gmcp_supported = True
                            self.gmcp_negotiated = True
                            gmcp_msgs.append(("negotiated", {"supported": True}))
                        elif cmd in (WONT, DONT):
                            self.gmcp_negotiated = False
                    else:
                        # refuse other options to avoid negotiation stalls
                        if cmd == WILL:
                            replies += bytes([IAC, DONT, opt])
                        elif cmd == DO:
                            replies += bytes([IAC, WONT, opt])
                    i += 3; continue
                if cmd == SB:
                    # find IAC SE
                    j = i + 2
                    found = -1
                    while j + 1 < len(buf):
                        if buf[j] == IAC and buf[j + 1] == SE:
                            found = j; break
                        j += 1
                    if found == -1:
                        break  # incomplete subnegotiation
                    if i + 2 < len(buf) and buf[i + 2] == GMCP:
                        payload = bytes(buf[i + 3:found])
                        gmcp_msgs.append(("subneg", payload))
                    i = found + 2; continue
                # other telnet command, skip
                i += 2; continue
            else:
                out.append(b); i += 1
        self._telnet_buffer = bytearray(buf[i:])
        # send telnet replies (best-effort)
        if replies and self.writer:
            try:
                self.writer.write(bytes(replies))
            except Exception:
                pass
        # handle gmcp messages (best-effort)
        for kind, val in gmcp_msgs:
            if kind == "negotiated":
                self.emit_event("gmcp", {"event": "negotiated", "supported": True})
            elif kind == "subneg":
                try:
                    text = val.decode("utf-8", errors="ignore").strip()
                    self._handle_gmcp_message(text)
                except Exception:
                    pass
        try:
            return self._text_decoder.decode(bytes(out), final=False)
        except Exception:
            try:
                return bytes(out).decode("cp437", errors="replace")
            except Exception:
                return bytes(out).decode("utf-8", errors="ignore")

    def _handle_gmcp_message(self, text: str) -> None:
        if not text:
            return
        # GMCP format: "Package.Subpackage {json}" or "Package {json}"
        pkg = text
        data = {}
        if " " in text:
            head, rest = text.split(" ", 1)
            pkg = head.strip()
            rest = rest.strip()
            if rest.startswith("{") or rest.startswith("["):
                try:
                    data = json.loads(rest)
                except Exception:
                    data = {"raw": rest}
            else:
                data = {"raw": rest}
        self.emit_event("gmcp", {"package": pkg, "data": data})
        pl = pkg.lower()
        # best-effort vitals mapping (Core/Char.Vitals or similar)
        if "vital" in pl or "char.vitals" in pl or pl.endswith("vitals"):
            try:
                hp = data.get("hp") or data.get("health")
                mhp = data.get("maxhp") or data.get("max_hp") or data.get("health_max")
                mp = data.get("mana") or data.get("mp")
                mmp = data.get("maxmana") or data.get("max_mp") or data.get("mana_max")
                mv = data.get("mv") or data.get("moves")
                mmv = data.get("maxmv") or data.get("max_mv") or data.get("moves_max")
                if hp is not None: self.hp = int(hp)
                if mhp is not None: self.max_hp = int(mhp)
                if mp is not None: self.mp = int(mp)
                if mmp is not None: self.max_mp = int(mmp)
                if mv is not None: self.mv = int(mv)
                if mmv is not None: self.max_mv = int(mmv)
            except Exception:
                pass
        if "room" in pl or pl.endswith("room.info") or pl.endswith("room"):
            try:
                rid = data.get("num") or data.get("id") or data.get("vnum")
                name = data.get("name")
                exits = data.get("exits") or data.get("exit") or {}
                exit_names = None
                if isinstance(exits, dict):
                    exit_names = list(exits.keys())
                elif isinstance(exits, list):
                    exit_names = [str(x) for x in exits]
                resolved = self.mapper.resolve_room(room_name=str(name or ""), obvious_exits=exit_names, megamud_code=str(rid or ""), room_id=str(rid or ""))
                if resolved:
                    self.current_room_id = resolved
                elif rid is not None and self.mapper.graph.has_node(str(rid)):
                    self.current_room_id = str(rid)
                elif rid is not None:
                    self.current_room_id = str(rid)
                if name:
                    self.current_room_name = str(name)
                if exit_names:
                    self.current_room_exits = [str(e).lower() for e in exit_names]
            except Exception:
                pass


    async def _start_optional_ipc_bus(self) -> Optional[asyncio.Task]:
        if not self.start_ipc_bus:
            return None
        try:
            server = await asyncio.start_server(self.coordination.handle_bus_subscriber, self.coordination.bus_host, self.coordination.bus_port)
            print(f"[Sync Bus] IPC bus active on {self.coordination.bus_host}:{self.coordination.bus_port}")
            return asyncio.create_task(server.serve_forever())
        except Exception as e:
            print(f"[-] Failed to start IPC bus: {e}")
            return None

    async def run_lifecycle(self, auto_path: Optional[str] = None, auto_target: Optional[str] = None, connect: bool = True) -> None:
        self.is_running = True
        bus_task = await self._start_optional_ipc_bus()
        dispatch_task = None
        follower_task = None
        try:
            self.emit_event("map_update", self.get_map_snapshot())
            # follower mode: connect to leader IPC bus for state/commands (Step 3)
            if self.is_follower and not self.is_leader:
                bhost = str(self.party_cfg.get("bus_host", self.coordination.bus_host) or self.coordination.bus_host)
                bport = int(self.party_cfg.get("bus_port", self.coordination.bus_port) or self.coordination.bus_port)
                follower_task = asyncio.create_task(self._run_follower_bus_client(bhost, bport))
                print(f"[Follower] Party bus client -> {bhost}:{bport}")
            if connect:
                print(f"[*] Connecting to {self.host}:{self.port}...")
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                print("[+] Socket connected.")
                await self._send_telnet_gmcp_will()
                dispatch_task = asyncio.create_task(self.outbound_scheduler_loop())
            else:
                print("[*] Offline/no-connect mode (local subsystems active).")

            if auto_path:
                await self.start_path(auto_path)
            elif auto_target:
                await self.execute_destination_routing(str(auto_target))

            if not connect:
                while self.is_running:
                    await asyncio.sleep(1)
                return

            buffer = ""
            assert self.reader is not None
            while self.is_running:
                data = await self.reader.read(4096)
                if not data:
                    print("[!] Remote stream closed.")
                    break
                decoded = self._process_telnet_and_extract_text(data)
                text_chunk = self.strip_ansi(decoded)
                buffer += text_chunk
                # process login prompts against rolling buffer (handles prompts without newline)
                await self._process_login_buffer(buffer)

                # line-buffer accumulator: assemble prompt-bounded chunks/paragraphs for multi-line parsing
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    self._stream_line_buffer.append(line)
                    if self._is_prompt_line(line.strip()):
                        chunk = "\n".join(self._stream_line_buffer)
                        self._stream_line_buffer = []
                        await self._process_stream_chunk(chunk)

                # prompt-only fragments without newline (common BBS/MajorMUD prompts)
                if buffer and ("\n" not in buffer):
                    bstrip = buffer.strip()
                    if bstrip and self._is_prompt_line(bstrip):
                        self._stream_line_buffer.append(buffer)
                        chunk = "\n".join(self._stream_line_buffer)
                        self._stream_line_buffer = []
                        buffer = ""
                        await self._process_stream_chunk(chunk)
                    elif self._bbs_menu_prompt and self._bbs_menu_prompt.lower() in buffer.lower():
                        await self.parse_stream_line(buffer)
                        buffer = ""
        except Exception as e:
            print(f"[-] Runtime loop error: {e}")
        finally:
            self.is_running = False
            if dispatch_task:
                dispatch_task.cancel()
            if bus_task:
                bus_task.cancel()
            if follower_task:
                follower_task.cancel()
            if self.writer:
                try:
                    self.writer.close()
                    await self.writer.wait_closed()
                except Exception:
                    pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ApexForge integrated MUD engine")
    p.add_argument("--profile", help="Character/profile name from profiles.json")
    p.add_argument("--host", help="MUD host override")
    p.add_argument("--port", type=int, help="MUD port override")
    p.add_argument("--path", dest="path_key", help="Grinding path key from paths.json to start")
    p.add_argument("--target", help="Destination room id to route to on start")
    p.add_argument("--no-connect", action="store_true", help="Initialize subsystems without opening a socket")
    p.add_argument("--ipc-bus", action="store_true", help="Start local IPC coordination bus")
    p.add_argument("--follower", action="store_true", help="Run as follower and connect to leader IPC bus")
    p.add_argument("--leader-host", help="Leader IPC bus host for follower mode (overrides profile party.bus_host)")
    p.add_argument("--leader-port", type=int, help="Leader IPC bus port for follower mode (overrides profile party.bus_port)")
    return p.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    engine = ApexForgeEngine(profile_name=args.profile, host=args.host, port=args.port)
    if args.ipc_bus:
        engine.start_ipc_bus = True
    if args.follower:
        engine.is_follower = True
        engine.is_leader = False
        if args.leader_host:
            engine.party_cfg["bus_host"] = args.leader_host
        if args.leader_port:
            engine.party_cfg["bus_port"] = int(args.leader_port)
    await engine.run_lifecycle(auto_path=args.path_key, auto_target=args.target, connect=not args.no_connect)


if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("\n[*] ApexForge engine stopped.")
