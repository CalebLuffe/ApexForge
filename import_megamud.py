import argparse
import configparser
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -------------------- basic helpers --------------------

def load_ini(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    p = Path(path)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(p, "r", encoding=enc, errors="ignore") as f:
                cp.read_file(f)
            return cp
        except Exception:
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.optionxform = str
    return cp


def get(cp, sec, key, default=None):
    if cp.has_option(sec, key):
        return cp.get(sec, key)
    if cp.has_section(sec):
        for k, v in cp.items(sec):
            if str(k).lower() == str(key).lower():
                return v
    return default


def ffloat(v, d=0.0):
    try:
        s = str(v).strip()
        if s.endswith('%'):
            s = s[:-1]
        return float(s)
    except Exception:
        return d


def fint(v, d=0):
    try:
        s = str(v).strip()
        if s.endswith('%'):
            s = s[:-1]
        return int(float(s))
    except Exception:
        return d


def load_json(path, default):
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


# -------------------- MegaMUD room helpers --------------------

JET_SIGNATURES = (b"\x00\x01\x00\x00Standard Jet DB", b"Standard Jet DB", b"Standard ACE DB")
DIRECTION_TOKENS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "u", "d", "north", "south", "east", "west", "up", "down"}
ACTION_TOKENS = {"go", "open", "pick", "unlock", "close", "enter", "climb", "push", "pull", "gate", "portal", "use"}


def is_jet_db(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(32)
        if any(sig in head for sig in JET_SIGNATURES):
            return True
        if head.startswith(b"MDB2"):
            return True
        sample = head
        if not sample:
            return False
        textish = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
        return textish < 8
    except Exception:
        return False


def normalize_room_name(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def normalize_exit_name(ex: str) -> str:
    e = str(ex or "").strip().lower()
    mapping = {
        "north": "n", "south": "s", "east": "e", "west": "w",
        "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
        "up": "u", "down": "d",
    }
    return mapping.get(e, e)


def room_deterministic_hash(room_name: str, exits: Optional[List[str]] = None) -> str:
    norm_name = normalize_room_name(room_name)
    norm_exits = sorted({normalize_exit_name(x) for x in (exits or []) if str(x).strip() and normalize_exit_name(x) != "none"})
    key = f"{norm_name}|{','.join(norm_exits)}"
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:12]


# -------------------- BBS / Char import --------------------

def parse_bbs(bbs_ini_path):
    cp = load_ini(bbs_ini_path)
    sec = cp.sections()[0] if cp.sections() else "BBS"
    prompts = []
    replies = []
    for i in range(20):
        p = get(cp, sec, f"LogonPrompt{i}", "") or ""
        r = get(cp, sec, f"LogonReply{i}", "") or ""
        if str(p).strip() == "--Unused--" and str(r).strip() == "":
            continue
        if p or r:
            prompts.append("" if str(p).strip() == "--Unused--" else str(p))
            replies.append(str(r).replace("^M", "\r").replace("^m", "\r"))
    return {
        "name": sec, "telnet_addr": get(cp, sec, "TelnetAddr", "127.0.0.1"),
        "telnet_port": fint(get(cp, sec, "TelnetPort", 23), 23),
        "menu_prompt": get(cp, sec, "MenuPrompt", "[MAJORMUD]:"),
        "ansi_cmd": get(cp, sec, "AnsiCmd", "==a"), "relog_cmd": get(cp, sec, "RelogCmd", ";relog"),
        "logon_prompts": prompts, "logon_replies": replies,
        "speech_prefix": get(cp, sec, "SpeechPrefix", "."), "broadcast_prefix": get(cp, sec, "BroadcastPrefix", "'"),
        "runic_name": get(cp, sec, "RunicName", ""), "source_ini": str(bbs_ini_path),
    }


def parse_char(char_ini_path, bbs_info=None):
    cp = load_ini(char_ini_path)
    file_stem = Path(char_ini_path).stem
    pname = (get(cp, "Player", "Name", file_stem) or file_stem).strip() or file_stem
    bbs_name = (get(cp, "BBS", "BbsName", "") or (bbs_info.get("name") if bbs_info else "") or "").strip()
    host = bbs_info.get("telnet_addr") if bbs_info else "127.0.0.1"
    port = bbs_info.get("telnet_port") if bbs_info else 23
    rest_hp = ffloat(get(cp, "Health", "HpRest%", 70), 70)
    heal_pct = ffloat(get(cp, "Health", "HpHeal%", 60), 60)
    flee_pct = ffloat(get(cp, "Health", "HpRun%", 30), 30)
    hp_full = ffloat(get(cp, "Health", "HpFull%", 99), 99)
    bless_cmds = [str(get(cp, "Spells", f"BlessCmd{i}", "") or "").strip() for i in range(1, 11)]
    bless_cmds = [b for b in bless_cmds if b]
    heal_cmd = (get(cp, "Spells", "HealCmd", "") or "").strip()
    party_heal1 = (get(cp, "Party", "PartyHeal1", "") or "").strip()
    party_heal2 = (get(cp, "Party", "PartyHeal2", "") or "").strip()
    profile = {
        "class": "", "race": "", "level": fint(get(cp, "Player", "Level", 1), 1),
        "host": host, "port": port, "bbs_name": bbs_name, "discord_webhook": "",
        "leader_name": pname, "admin_names": [pname],
        "tick_cast_command": bless_cmds[0] if bless_cmds else "cast bless",
        "start_ipc_bus": False, "pre_move_command": "", "post_move_command": "",
        "stat_line_template": (get(cp, "MegaMud", "StatLine", "") or "").strip(),
        "survival": {"flee_hp_percentage": flee_pct, "rest_hp_percentage": rest_hp, "emergency_hangup_on_pvp": False,
                     "auto_rest_command": "rest", "auto_stand_command": "stand", "flee_hp_absolute": 0, "min_monsters_flee": 0, "hp_full_pct": hp_full},
        "spells": {"auto_heal": bool(heal_cmd), "heal_spell": heal_cmd or "heal", "heal_hp_threshold": 0, "heal_hp_pct": heal_pct,
                   "heal_target": "self", "party_heal_spell": party_heal1 or heal_cmd or "heal",
                   "party_heal_hp_pct": ffloat(get(cp, "Party", "PartyHeal1%", 50), 50),
                   "party_heal2_spell": party_heal2, "party_heal2_hp_pct": ffloat(get(cp, "Party", "PartyHeal2%", 20), 20),
                   "auto_bless": bool(bless_cmds), "bless_spell": bless_cmds[0] if bless_cmds else "", "bless_spells": bless_cmds,
                   "bless_target": "self", "auto_nuke": fint(get(cp, "MegaMud", "AutoNuke", 0), 0) == 1,
                   "nuke_spell": (get(cp, "Combat", "AttackSpl", "") or "").strip(), "min_nuke_monsters": 2,
                   "cast_at_end_of_round": False, "vampiric_spells": [],
                   "poison_cure_cmd": (get(cp, "Spells", "PoisonCmd", "") or "").strip(),
                   "disease_cure_cmd": (get(cp, "Spells", "DiseaseCmd", "") or "").strip(),
                   "blind_cure_cmd": (get(cp, "Spells", "BlindCmd", "") or "").strip()},
        "party": {"is_leader": False, "leader_name": pname, "followers": [], "whitelist": [pname],
                  "rest_healing_applies_to_party": False, "disable_health_spam": fint(get(cp, "Party", "AskHealth", 1), 1) != 1,
                  "hp_wait_threshold_pct": ffloat(get(cp, "Party", "PartyWait%", 50), 50),
                  "party_heal_enabled": bool(party_heal1 or party_heal2),
                  "wait_cmd": (get(cp, "Party", "PartyWaitCmd", "@wait") or "@wait").strip(),
                  "ok_cmd": (get(cp, "Party", "PartyResumeCmd", "@ok") or "@ok").strip(),
                  "telepath_command": "telepath {leader} {text}"},
        "combat": {"auto_combat": fint(get(cp, "MegaMud", "AutoCombat", 1), 1) == 1,
                   "attack_cmd": (get(cp, "Combat", "AttackCmd", "a") or "a").strip(),
                   "pre_attack_cmd": (get(cp, "Combat", "PreAttack", "") or "").strip(),
                   "multi_attack_cmd": (get(cp, "Combat", "MultAttack", "") or "").strip(),
                   "attack_spell": (get(cp, "Combat", "AttackSpl", "") or "").strip(),
                   "current_profile": 1,
                   "profiles": {"1": {"weapon": (get(cp, "Combat", "NrmWeapon", "") or "").strip(),
                                      "bs_weapon": (get(cp, "Combat", "BsWeapon", "") or "").strip(),
                                      "alt_weapon": (get(cp, "Combat", "AltWeapon", "") or "").strip(),
                                      "attack_command": (get(cp, "Combat", "AttackCmd", "a") or "a").strip(),
                                      "nuke_spell": (get(cp, "Combat", "AttackSpl", "") or "").strip(),
                                      "heal_spell": heal_cmd or "heal"}},
                   "immunity_db_limit": 500, "max_monsters": fint(get(cp, "Combat", "MaxMstrs", 2), 2),
                   "run_rooms": fint(get(cp, "Combat", "RunRooms", 4), 4)},
        "navigation": {"lockpick_enabled": fint(get(cp, "Other", "CanPickLocks", 0), 0) == 1, "lockpick_command": "pick lock",
                       "pick_max": fint(get(cp, "Other", "PickMax", 3), 3), "reengage_combat_on_lost": True,
                       "run_mode_suppress_combat": True, "run_backwards": fint(get(cp, "Other", "RunBackwards", 1), 1) == 1},
        "inventory_economy": {"active_mode": True, "auto_buy": False, "auto_sell": False,
                              "auto_deposit": fint(get(cp, "MegaMud", "AutoCash", 1), 1) == 1, "auto_withdraw": False,
                              "keep_cash_copper": 0, "max_coins": fint(get(cp, "Cash", "MaxCoins", 1000), 1000),
                              "dont_be_heavy": fint(get(cp, "Cash", "DontBeHeavy", 0), 0) == 1,
                              "stash_room_id": "", "bank_room_id": "", "bank_name": (get(cp, "Cash", "Bank", "") or "").strip(),
                              "buy_commands": [], "sell_commands": [], "deposit_commands": [], "withdraw_commands": []},
        "automation": {"restock_room_id": "1", "restock_commands": [], "corpse_loot_command": "get all corpse",
                       "corpse_snipe_command": "get all from corpse", "interrupt_command": "bash",
                       "spell_interrupt_command": "bash", "recovery_commands": ["look", "recall"],
                       "burden_drop_threshold": 0.85 if fint(get(cp, "Cash", "DontBeHeavy", 0), 0) == 1 else 0.95,
                       "max_carry_weight": 100.0, "drop_candidates": [], "auto_recheck_inventory_on_unknown": True,
                       "cmd_split_char": (get(cp, "Other", "CmdSplitChar", ";") or ";")},
        "stat_weights": {"ac": 1.0, "strength": 0.8, "hp": 0.5, "intellect": 0.3},
        "imported_from": str(char_ini_path),
    }
    if bbs_info:
        profile["bbs"] = bbs_info
    return pname, profile


# -------------------- Rooms / Messages / Paths parsing --------------------

def parse_rooms(rooms_md_path, limit=None):
    rooms = {}
    try:
        with open(rooms_md_path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if limit and i >= limit:
                    break
                line = line.strip()
                if not line or line.startswith("MDB2"):
                    continue
                parts = line.split(":")
                if len(parts) < 7:
                    continue
                rid = parts[0].strip()
                exit_mask = parts[1].strip() if len(parts) > 1 else ""
                f2 = parts[2].strip() if len(parts) > 2 else ""
                f3 = parts[3].strip() if len(parts) > 3 else ""
                f4 = parts[4].strip() if len(parts) > 4 else ""
                code = parts[5].strip() if len(parts) > 5 else ""
                zone = parts[6].strip() if len(parts) > 6 else ""
                name = ":".join(parts[7:]).strip() if len(parts) > 7 else zone
                if not rid and not code:
                    continue
                if code and re.fullmatch(r"[A-Za-z0-9]{4}", code):
                    key = code.upper()
                else:
                    key = rid or code
                exits: Dict[str, Any] = {}
                rhash = room_deterministic_hash(name or code or rid, list(exits.keys()))
                rooms[key] = {
                    "id": key,
                    "megamud_id": rid or "",
                    "megamud_code": code.upper() if code else "",
                    "name": name or code or rid or key,
                    "zone": zone or "unknown",
                    "code": code.upper() if code else "",
                    "exit_mask": exit_mask,
                    "room_hash": rhash,
                    "x": None, "y": None,
                    "exits": exits,
                    "drops": [],
                    "source": {"rooms_md_fields": [rid, exit_mask, f2, f3, f4, code, zone]},
                }
    except Exception as e:
        print("rooms parse error", e)
    return rooms


def parse_messages(messages_md_path, max_entries=5000):
    entries = []
    try:
        with open(messages_md_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.rstrip("\r\n") for l in f]
    except Exception:
        return entries
    header_re = re.compile(r"^[A-Za-z0-9 _'\-/()]+:\w*:\w*:?")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if ":" in s and header_re.match(s):
            parts = s.split(":")
            name = parts[0].strip()
            flags = parts[1].strip() if len(parts) > 1 else ""
            i += 1
            pats = []
            while i < len(lines):
                cur = lines[i]
                cs = cur.strip()
                if cs == "":
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == "":
                        j += 1
                    if j < len(lines) and header_re.match(lines[j].strip()) and lines[j].strip().count(":") >= 2:
                        i = j
                        break
                    i += 1
                    continue
                if header_re.match(cs) and cs.count(":") >= 2 and cs.split(":")[0].strip() != name:
                    break
                pats.append(cs)
                i += 1
            entries.append({"name": name, "flags": flags, "patterns": pats})
            if len(entries) >= max_entries:
                break
            continue
        i += 1
    return entries


def _classify_step_action(action: str) -> Tuple[str, bool]:
    al = str(action or "").strip().lower()
    if not al:
        return "move", False
    first = al.split()[0]
    if al in DIRECTION_TOKENS or first in DIRECTION_TOKENS:
        return "move", False
    if first in ACTION_TOKENS or any(tok in al for tok in ("go ", "open ", "pick ", "unlock ", "close ", "gate", "portal", "enter ", "climb", "push ", "pull ")):
        return "action", True
    return "action", True


def parse_mp(path_file):
    try:
        raw = Path(path_file).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None, None
    lines = raw.splitlines()
    if not lines:
        return None, None
    from_meta = to_meta = None
    idx = 0
    if lines and lines[0].startswith("["):
        idx = 1
    if idx < len(lines) and lines[idx].startswith("["):
        from_meta = lines[idx].strip().strip("[]")
        idx += 1
    if idx < len(lines) and lines[idx].startswith("["):
        to_meta = lines[idx].strip().strip("[]")
        idx += 1

    seq = []
    for ln in lines[idx:]:
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split(":")
        if len(parts) < 2:
            continue
        rid = parts[0].strip()
        action = parts[-1].strip() if parts else ""
        # meta/header rows (e.g. 42D00140:44E00004:12:-1:0:::)
        if len(parts) >= 6:
            p2 = parts[2].strip().lstrip("-")
            p3 = parts[3].strip().lstrip("-")
            if p2.isdigit() and p3.isdigit():
                seq.append(("meta", rid, action, parts))
                continue
        seq.append(("step", rid, action, parts))

    loop_nodes = []
    for kind, rid, act, _ in seq:
        if kind == "step" and rid:
            if not loop_nodes or loop_nodes[-1] != rid:
                loop_nodes.append(rid)
    if not loop_nodes:
        for kind, rid, _, _ in seq:
            if rid and (not loop_nodes or loop_nodes[-1] != rid):
                loop_nodes.append(rid)

    key = Path(path_file).stem
    zone = ""
    name = key
    if from_meta:
        fm = from_meta.split(":")
        to_last = to_meta.split(":")[-1] if to_meta else ""
        if len(fm) >= 3:
            zone = fm[1].strip()
            from_room = fm[2].strip()
            name = f"{zone}: {from_room} -> {to_last}".strip(" ->")
    if len(loop_nodes) >= 2 and loop_nodes[0] != loop_nodes[-1]:
        loop_nodes.append(loop_nodes[0])
    if len(loop_nodes) < 2:
        return key, None

    step_overrides = {}
    move_edges = []  # edges inferred as (current_room --action--> next_room)
    step_rows = [(rid, act) for kind, rid, act, _ in seq if kind == "step" and rid]
    for i in range(len(step_rows) - 1):
        a, act = step_rows[i]
        b, _nact = step_rows[i + 1]
        if not act or a == b:
            continue
        action_type, conditional = _classify_step_action(act)
        dir_norm = normalize_exit_name(act)
        is_cardinal_move = (not conditional) and (dir_norm in DIRECTION_TOKENS or action_type == "move")
        if conditional or action_type == "action" or not is_cardinal_move:
            step_overrides[f"{a}->{b}"] = {
                "direction": act,
                "party_cmd": act if "@" in act else "",
                "pre_cmd": act if "@" not in act else "",
                "conditional_action": bool(conditional or action_type == "action"),
                "action_type": action_type,
            }
        if is_cardinal_move:
            move_edges.append({"from": a, "to": b, "direction": dir_norm, "action": act, "conditional": False})
        elif conditional or action_type == "action":
            move_edges.append({"from": a, "to": b, "direction": act, "action": act, "conditional": True})

    cfg = {
        "name": name, "loop_nodes": loop_nodes, "zone_color_key": zone or "imported",
        "min_level": 1, "max_level": 99, "min_xp_hr": 0, "min_lap_time": 0.0,
        "fallback_path": None, "min_monsters": 0, "steps": step_overrides,
        "move_edges": move_edges,
        "source_mp": str(path_file), "from_meta": from_meta, "to_meta": to_meta,
    }
    return key, cfg


# -------------------- MDB2 / Jet extraction (best-effort) --------------------

def _extract_printable_strings(data: bytes, min_len: int = 4) -> List[str]:
    out = []
    cur = bytearray()
    for b in data:
        if 32 <= b <= 126:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                s = cur.decode("latin-1", errors="ignore").strip()
                if s:
                    out.append(s)
            cur.clear()
    if len(cur) >= min_len:
        s = cur.decode("latin-1", errors="ignore").strip()
        if s:
            out.append(s)
    return out


def _try_mdb_tools_export(path: Path) -> Optional[Dict[str, Any]]:
    if not shutil.which("mdb-tables") or not shutil.which("mdb-export"):
        return None
    try:
        tables_raw = subprocess.check_output(["mdb-tables", "-1", str(path)], text=True, encoding="utf-8", errors="ignore")
        tables = [t.strip() for t in tables_raw.splitlines() if t.strip()]
        exported = {}
        for t in tables[:20]:
            try:
                csv = subprocess.check_output(["mdb-export", str(path), t], text=True, encoding="utf-8", errors="ignore")
                exported[t] = csv[:200000]
            except Exception:
                continue
        return {"tables": tables, "exported_csv": exported}
    except Exception:
        return None


def extract_mdb2_generic(path: Path, kind: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"file": str(path), "kind": kind, "is_jet": is_jet_db(path), "size": path.stat().st_size if path.exists() else 0}
    try:
        data = path.read_bytes()
    except Exception as e:
        info["error"] = str(e)
        return info
    info["header_hex"] = data[:32].hex()
    info["strings_sample"] = _extract_printable_strings(data[:262144], min_len=5)[:200]
    nums = []
    for off in range(0, min(len(data) - 4, 65536), 4):
        try:
            v = struct.unpack_from("<I", data, off)[0]
            if 0 < v < 10_000_000:
                nums.append(v)
        except Exception:
            pass
    info["u32_sample"] = nums[:64]
    mdb = _try_mdb_tools_export(path)
    if mdb:
        info["mdb_tools"] = mdb
    return info


def merge_items_from_mdb(extracted: Dict[str, Any], items_json: Dict[str, Any]) -> int:
    mdb = extracted.get("mdb_tools") or {}
    if mdb.get("exported_csv"):
        items_json.setdefault("mdb_exports", {})[Path(extracted.get("file", "items")).stem] = mdb.get("exported_csv")
        return 0
    names = extracted.get("strings_sample") or []
    items = items_json.setdefault("items", {})
    added = 0
    for s in names:
        if len(s) < 3 or len(s) > 40 or not re.search(r"[A-Za-z]", s):
            continue
        key = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or hashlib.sha1(s.encode()).hexdigest()[:8]
        if key in items:
            continue
        items[key] = {"name": s, "source": "mdb2_carved", "stats": {}, "uses": None, "immunities": []}
        added += 1
        if added >= 500:
            break
    return added


def merge_table_from_mdb(extracted: Dict[str, Any], db_json: Dict[str, Any], table_key: str) -> int:
    mdb = extracted.get("mdb_tools") or {}
    if mdb.get("exported_csv"):
        db_json.setdefault("mdb_exports", {})[table_key] = mdb.get("exported_csv")
        return 0
    names = extracted.get("strings_sample") or []
    table = db_json.setdefault(table_key, {})
    added = 0
    for s in names:
        if len(s) < 3 or len(s) > 50 or not re.search(r"[A-Za-z]", s):
            continue
        key = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or hashlib.sha1(s.encode()).hexdigest()[:8]
        if key in table:
            continue
        table[key] = {"name": s, "source": "mdb2_carved"}
        added += 1
        if added >= 500:
            break
    return added


# -------------------- Path validation --------------------

def _resolve_room_key(db_rooms: Dict[str, Any], rid: str, code_index: Dict[str, str], id_index: Dict[str, str]) -> Optional[str]:
    s = str(rid)
    for cand in (s, s.upper(), s.lower()):
        if cand in db_rooms:
            return cand
        if cand in code_index:
            return code_index[cand]
        if cand in id_index:
            return id_index[cand]
    su = s.upper()
    for k, v in code_index.items():
        if str(k).upper() == su:
            return v
    for k, v in id_index.items():
        if str(k).upper() == su:
            return v
    return None


def _build_graph(db_rooms: Dict[str, Any]):
    try:
        import networkx as nx
    except Exception:
        nx = None
    if nx is None:
        adj = {}
        for rid, attrs in db_rooms.items():
            outs = set()
            for ex in (attrs.get("exits") or {}).values():
                tgt = str((ex or {}).get("target") or "")
                if tgt:
                    outs.add(tgt)
            adj[str(rid)] = outs
        return ("adj", adj)
    g = nx.DiGraph()
    for rid in db_rooms.keys():
        g.add_node(str(rid))
    for rid, attrs in db_rooms.items():
        for ex in (attrs.get("exits") or {}).values():
            tgt = str((ex or {}).get("target") or "")
            if tgt and g.has_node(tgt):
                g.add_edge(str(rid), tgt)
    return ("nx", g)


def _has_path(graph, a: str, b: str) -> bool:
    kind, g = graph
    if kind == "nx":
        try:
            import networkx as nx
            return nx.has_path(g, a, b)
        except Exception:
            return False
    if a not in g:
        return False
    seen = {a}
    stack = [a]
    while stack:
        cur = stack.pop()
        if cur == b:
            return True
        for nb in g.get(cur, []):
            if nb not in seen and nb in g:
                seen.add(nb)
                stack.append(nb)
    return False


def validate_paths(paths_json_path: Path, db_json_path: Path, annotate_db: bool = True) -> Dict[str, Any]:
    paths_data = load_json(paths_json_path, {"paths": {}})
    db_data = load_json(db_json_path, {"rooms": {}})
    paths = paths_data.get("paths", {})
    rooms = db_data.get("rooms", {})

    code_index: Dict[str, str] = {}
    id_index: Dict[str, str] = {}
    for k, r in rooms.items():
        code = str(r.get("megamud_code") or r.get("code") or "")
        mid = str(r.get("megamud_id") or r.get("id") or "")
        if code:
            code_index[code] = k
            code_index[code.upper()] = k
        if mid:
            id_index[mid] = k
            id_index[mid.upper()] = k
        code_index[k] = k
        code_index[str(k).upper()] = k

    graph = _build_graph(rooms)
    report = {"total": len(paths), "valid": 0, "invalid": 0, "details": {}}

    for pkey, pcfg in paths.items():
        nodes = list(pcfg.get("loop_nodes") or [])
        steps_meta = pcfg.get("steps") or {}
        missing_nodes = []
        unconnected = []
        conditional = []
        resolved_nodes = [_resolve_room_key(rooms, str(n), code_index, id_index) for n in nodes]

        for n, rk in zip(nodes, resolved_nodes):
            if not rk:
                missing_nodes.append(n)

        for i in range(len(nodes) - 1):
            a_raw, b_raw = str(nodes[i]), str(nodes[i + 1])
            a, b = resolved_nodes[i], resolved_nodes[i + 1]
            sm = steps_meta.get(f"{a_raw}->{b_raw}") or steps_meta.get(f"{a}->{b}") or {}
            act = sm.get("direction") or sm.get("pre_cmd") or sm.get("party_cmd") or ""
            is_cond = bool(sm.get("conditional_action")) or _classify_step_action(act)[1]
            if not a or not b:
                if is_cond:
                    conditional.append({"from": a_raw, "to": b_raw, "action": act, "reason": "missing_node_but_conditional"})
                else:
                    unconnected.append({"from": a_raw, "to": b_raw, "reason": "missing_node"})
                continue
            if is_cond:
                conditional.append({"from": a_raw, "to": b_raw, "resolved_from": a, "resolved_to": b, "action": act})
                if annotate_db and a in rooms and b in rooms:
                    ex = rooms[a].setdefault("exits", {})
                    found = None
                    for _dname, edata in ex.items():
                        if str((edata or {}).get("target")) == b:
                            found = edata
                            break
                    if found is None:
                        found = {"target": b, "weight": 1.0}
                        ex[f"cond_{b}"] = found
                    found["conditional"] = True
                    found.setdefault("actions", [])
                    if act and act not in found["actions"]:
                        found["actions"].append(act)
                    if sm.get("requires_lockpick") or "pick" in str(act).lower():
                        found["requires_lockpick"] = True
                    if sm.get("pre_cmd"):
                        found.setdefault("pre_cmd", sm.get("pre_cmd"))
                continue
            direct = False
            if a in rooms:
                for ex in (rooms[a].get("exits") or {}).values():
                    if str((ex or {}).get("target")) == b:
                        direct = True
                        break
            if direct or _has_path(graph, a, b):
                continue
            unconnected.append({"from": a_raw, "to": b_raw, "reason": "no_direct_or_routed_edge"})

        ok = (len(missing_nodes) == 0 and len(unconnected) == 0)
        pcfg["graph_validated"] = bool(ok)
        pcfg["missing_node_count"] = len(missing_nodes)
        pcfg["missing_nodes"] = missing_nodes
        pcfg["missing_edge_count"] = len(unconnected)
        pcfg["unconnected_edges"] = unconnected
        pcfg["conditional_edge_count"] = len(conditional)
        pcfg["conditional_edges"] = conditional
        report["details"][pkey] = {"valid": ok, "missing_nodes": missing_nodes, "unconnected_edges": unconnected, "conditional_edges": conditional}
        if ok:
            report["valid"] += 1
        else:
            report["invalid"] += 1

    paths_data["paths"] = paths
    write_json(paths_json_path, paths_data)
    if annotate_db:
        db_data["rooms"] = rooms
        write_json(db_json_path, db_data)
    return report




# -------------------- merge-only / staging helpers --------------------

def _merge_room_safe(existing: dict, incoming: dict, force: bool = False) -> bool:
    changed = False
    if force:
        for k, v in incoming.items():
            if k == "exits":
                ex = existing.setdefault("exits", {})
                for d, ed in (v or {}).items():
                    if d not in ex:
                        ex[d] = ed
                        changed = True
                continue
            if k == "drops":
                existing.setdefault("drops", v or [])
                continue
            if existing.get(k) != v and v not in (None, "", []):
                existing[k] = v
                changed = True
        return changed
    for k in ("name", "zone", "code", "megamud_code", "megamud_id", "exit_mask", "room_hash", "id", "lookup_hash"):
        if (existing.get(k) in (None, "", [])) and incoming.get(k) not in (None, "", []):
            existing[k] = incoming[k]
            changed = True
    if (existing.get("lookup_hash") in (None, "")) and existing.get("room_hash"):
        existing["lookup_hash"] = existing.get("room_hash")
        changed = True
    if incoming.get("source"):
        src = existing.setdefault("source", {})
        for sk, sv in incoming.get("source", {}).items():
            if sk not in src and sv not in (None, "", []):
                src[sk] = sv
                changed = True
    existing.setdefault("exits", {})
    existing.setdefault("drops", [])
    return changed


def _merge_path_safe(existing: dict, incoming: dict, force: bool = False) -> bool:
    if (not force) and bool(existing.get("graph_validated")):
        return False
    if force:
        existing.update(incoming)
        return True
    changed = False
    protected = {"graph_validated", "validated_edges", "missing_edge_count", "missing_edges", "missing_node_count",
                 "missing_nodes", "unconnected_edges", "conditional_edge_count", "conditional_edges", "manual_review",
                 "repair_status", "repair_notes"}
    for k, v in incoming.items():
        if k in protected and k in existing:
            continue
        if k == "steps":
            steps = existing.setdefault("steps", {})
            for sk, sv in (v or {}).items():
                if sk not in steps:
                    steps[sk] = sv
                    changed = True
            continue
        if k == "loop_nodes":
            if not existing.get("loop_nodes"):
                existing["loop_nodes"] = v
                changed = True
            continue
        if existing.get(k) in (None, "", [], {}):
            existing[k] = v
            changed = True
    return changed


# -------------------- main --------------------

def main():
    ap = argparse.ArgumentParser(description="Import MegaMUD data into ApexForge configs")
    ap.add_argument("--megamud", default=r"E:\megamud")
    ap.add_argument("--char", default="sysop")
    ap.add_argument("--bbs", default=None)
    ap.add_argument("--out", default=r"D:\ApexForge")
    ap.add_argument("--import-paths", action="store_true", help="Import all .mp path files")
    ap.add_argument("--paths-limit", type=int, default=0)
    ap.add_argument("--rooms-limit", type=int, default=0, help="0 = all")
    ap.add_argument("--messages-limit", type=int, default=5000)
    ap.add_argument("--extract-mdb2", action="store_true", help="Best-effort extract Jet/MDB2 .md tables")
    ap.add_argument("--validate-paths", action="store_true", help="Validate paths.json against database.json graph and annotate results")
    ap.add_argument("--annotate-db", action="store_true", default=True)
    ap.add_argument("--no-annotate-db", dest="annotate_db", action="store_false")
    ap.add_argument("--merge-only", dest="merge_only", action="store_true", default=True,
                    help="Safe default: non-destructive merges/staging")
    ap.add_argument("--no-merge-only", dest="merge_only", action="store_false",
                    help="Disable safe merge-only behavior (still respects graph_validated unless --force-overwrite)")
    ap.add_argument("--force-overwrite", dest="force_overwrite", action="store_true", default=False,
                    help="Allow destructive updates/overwrite validated paths")
    ap.add_argument("--init-env", dest="init_env", action="store_true", default=False,
                    help="Write requirements.txt")
    args = ap.parse_args()

    mm = Path(args.megamud)
    out = Path(args.out)
    if not mm.exists():
        print("MegaMUD path not found", mm)
        return 1

    bbs_info = None
    chosen_bbs = None
    if args.bbs:
        cand = mm / "BBS" / args.bbs / "BBS.INI"
        if cand.exists():
            chosen_bbs = cand
    if not chosen_bbs:
        for cand in [mm / "BBS" / f"mudserver01{args.char}" / "BBS.INI", mm / "BBS" / "mudserver01" / "BBS.INI"]:
            if cand.exists():
                chosen_bbs = cand
                break
    if not chosen_bbs:
        for d in sorted([p for p in (mm / "BBS").glob("*") if p.is_dir()]):
            cand = d / "BBS.INI"
            if cand.exists():
                chosen_bbs = cand
                break
    if chosen_bbs and chosen_bbs.exists():
        bbs_info = parse_bbs(str(chosen_bbs))
        print(f"BBS: {bbs_info['name']} {bbs_info['telnet_addr']}:{bbs_info['telnet_port']} ({chosen_bbs})")

    char_ini = mm / "Chars" / f"{args.char}.INI"
    if not char_ini.exists():
        for p in (mm / "Chars").glob("*.INI"):
            if p.stem.lower() == args.char.lower():
                char_ini = p
                break
    merge_only = bool(getattr(args, "merge_only", True)) and not bool(getattr(args, "force_overwrite", False))
    force_overwrite = bool(getattr(args, "force_overwrite", False))
    profiles_target = out / ("profiles.imported.json" if merge_only else "profiles.json")
    profiles = load_json(profiles_target, {"characters": {}})
    if char_ini.exists():
        pname, prof = parse_char(str(char_ini), bbs_info)
        chars = profiles.setdefault("characters", {})
        if merge_only or force_overwrite or pname not in chars:
            chars[pname] = prof
            write_json(profiles_target, profiles)
            print(f"Imported char profile: {pname} <- {char_ini} ({profiles_target.name})")
        else:
            print(f"Skipped char profile {pname} (exists)")
    else:
        print("Char INI not found", char_ini)

    rooms_md = mm / "Default" / "Rooms.md"
    if rooms_md.exists() and not is_jet_db(rooms_md):
        limit = args.rooms_limit if args.rooms_limit and args.rooms_limit > 0 else None
        rooms = parse_rooms(str(rooms_md), limit=limit)
        db = load_json(out / "database.json", {"rooms": {}})
        exrooms = db.setdefault("rooms", {})
        added = 0
        updated = 0
        for rid, rv in rooms.items():
            if rv.get("room_hash") and not rv.get("lookup_hash"):
                rv["lookup_hash"] = rv.get("room_hash")
            if rid not in exrooms:
                exrooms[rid] = rv
                added += 1
            else:
                if _merge_room_safe(exrooms[rid], rv, force=force_overwrite):
                    updated += 1
        db["rooms"] = exrooms
        write_json(out / "database.json", db)
        print(f"Rooms: parsed {len(rooms)}, added {added}, updated {updated}, total {len(exrooms)} -> {out/'database.json'} (merge_only={merge_only})")

    msgs_md = mm / "Default" / "Messages.md"
    if msgs_md.exists() and not is_jet_db(msgs_md):
        messages = parse_messages(str(msgs_md), max_entries=args.messages_limit)
        msgs_target = out / ("messages.imported.json" if merge_only else "messages.json")
        write_json(msgs_target, {"messages": messages, "source": str(msgs_md)})
        print(f"Messages: parsed {len(messages)} -> {msgs_target.name} (merge_only={merge_only})")

    if args.extract_mdb2:
        db = load_json(out / "database.json", {"rooms": {}})
        items = load_json(out / "items.json", {"items": {}, "immunities": {}})
        for name, kind in [("Items.md", "items"), ("Monsters.md", "monsters"), ("Spells.md", "spells"), ("Paths.md", "paths_mdb"), ("Classes.md", "classes"), ("Races.md", "races"), ("Macros.md", "macros")]:
            p = mm / "Default" / name
            if not p.exists() or not is_jet_db(p):
                continue
            extracted = extract_mdb2_generic(p, kind)
            if kind == "items":
                n = merge_items_from_mdb(extracted, items)
                print(f"MDB2 {name}: strings={len(extracted.get('strings_sample', []))} merged_items={n}")
            else:
                n = merge_table_from_mdb(extracted, db, kind)
                print(f"MDB2 {name}: strings={len(extracted.get('strings_sample', []))} merged_{kind}={n}")
            db.setdefault("mdb2_inspection", {})[name] = {"header_hex": extracted.get("header_hex"), "size": extracted.get("size"), "mdb_tools": bool(extracted.get("mdb_tools"))}
        write_json(out / "items.json", items)
        write_json(out / "database.json", db)
        print(f"MDB2 extraction pass complete -> {out/'items.json'}, {out/'database.json'}")

    if args.import_paths:
        mp_files = sorted((mm / "Default").glob("*.mp"))
        if args.paths_limit and args.paths_limit > 0:
            mp_files = mp_files[: args.paths_limit]
        pdata = load_json(out / "paths.json", {"paths": {}})
        pmap = pdata.setdefault("paths", {})
        db = load_json(out / "database.json", {"rooms": {}})
        exrooms = db.setdefault("rooms", {})
        imported = 0
        updated = 0
        skipped_valid = 0
        edges_added = 0
        for mp in mp_files:
            k, cfg = parse_mp(str(mp))
            if not (k and cfg):
                continue
            # apply inferred exits from plain directional moves (merge-only: add missing exits only)
            for mv in (cfg.get("move_edges") or []):
                a, b, d = str(mv.get("from")), str(mv.get("to")), str(mv.get("direction") or "")
                if not a or not b or a == b or not d:
                    continue
                ra = exrooms.get(a)
                rb = exrooms.get(b)
                if ra is None or rb is None:
                    continue
                exits = ra.setdefault("exits", {})
                if not mv.get("conditional") and normalize_exit_name(d) in DIRECTION_TOKENS:
                    nd = normalize_exit_name(d)
                    if nd not in exits:
                        exits[nd] = {"target": b, "weight": 1.0, "source": "mp_inferred"}
                        edges_added += 1
                    elif force_overwrite and isinstance(exits.get(nd), dict) and not exits[nd].get("target"):
                        exits[nd]["target"] = b
                elif mv.get("conditional"):
                    # annotate conditional on existing matching exit if found, else leave to validator annotation
                    pass
            if k not in pmap:
                pmap[k] = cfg
                imported += 1
            else:
                if (not force_overwrite) and bool(pmap[k].get("graph_validated")):
                    skipped_valid += 1
                    continue
                if _merge_path_safe(pmap[k], cfg, force=force_overwrite):
                    updated += 1
        pdata["paths"] = pmap
        write_json(out / "paths.json", pdata)
        db["rooms"] = exrooms
        write_json(out / "database.json", db)
        print(f"Paths: imported {imported}, updated {updated}, skipped_validated {skipped_valid}, inferred_edges_added {edges_added} from {len(mp_files)} .mp files -> {out/'paths.json'} (merge_only={merge_only})")

    if args.validate_paths:
        report = validate_paths(out / "paths.json", out / "database.json", annotate_db=args.annotate_db)
        write_json(out / "path_validation_report.json", report)
        print(f"Path validation: {report['valid']}/{report['total']} valid, {report['invalid']} invalid -> {out/'path_validation_report.json'}")

    if bool(getattr(args, "init_env", False)):
        req = out / "requirements.txt"
        req.write_text("networkx\ntextual\n", encoding="utf-8")
        print(f"Wrote {req}")
    else:
        print("Skipped requirements.txt (use --init-env to write)")
    print(f"DONE merge_only={merge_only} force_overwrite={force_overwrite}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
