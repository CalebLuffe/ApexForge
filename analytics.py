import json
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


class ApexForgeAnalytics:
    def __init__(self, items_database_path: str = "items.json"):
        self.items_path = items_database_path
        self.item_db: Dict[str, Any] = {}
        self.immunity_db: Dict[str, Dict[str, Any]] = {}
        self.immunity_db_limit = 500

        # Experience / combat telemetry
        self.xp_history: Deque[tuple] = deque(maxlen=20000)  # (ts, total_xp)
        self.total_xp_gained = 0
        self.session_start = time.time()

        self.path_efficiency: Dict[str, Dict[str, Any]] = {}
        self.rtk_samples: Deque[int] = deque(maxlen=200)
        self.rtc_samples: Deque[int] = deque(maxlen=200)
        self.current_rounds_in_fight = 0
        self.current_rounds_in_room = 0

        # Time analysis / movement telemetry (MegaMUD-inspired)
        self.state_durations: Dict[str, float] = {
            "NAVIGATING": 0.0,
            "COMBAT": 0.0,
            "RESTING": 0.0,
            "IDLE": 0.0,
        }
        self._active_state = "NAVIGATING"
        self._state_enter_time = time.time()
        self.move_time_samples: Deque[float] = deque(maxlen=50)  # seconds per room move
        self.path_lap_times: Dict[str, Deque[float]] = {}
        self._lap_start: Dict[str, float] = {}
        self.last_lap_time: Dict[str, float] = {}

        # Class/stat weights (overridable by profile)
        self.stat_weights = {"ac": 1.0, "strength": 0.8, "intellect": 0.5}

        # Character/equipment state
        self.char_level = 12
        self.equipped_gear = {
            "body": {"id": "101", "name": "Tattered Cloth Robe", "score": 2.5},
            "weapon": {"id": "103", "name": "Serrated Steel Dagger", "score": 0.8},
        }

        self.load_item_matrix()

        # Avoidance / anti-loop registry (volatile, bounded): failures per room in rolling window
        self.room_failures = {}
        self.room_avoidance = {}  # room_id -> {until_ts, count, reason, monsters}
        self.avoidance_window_s = 900.0  # 15 minutes
        self.avoidance_threshold = 3
        self.avoidance_penalty_s = 900.0
        self.avoidance_max_rooms = 200

        # Combat timing / adaptive tick telemetry (used by record_combat_burst/record_round)
        self._last_combat_burst_ts = 0.0
        self._last_round_ts = 0.0
        self._last_round_marker_ts = 0.0
        self._combat_round_intervals = deque(maxlen=50)
        self._combat_burst_times = deque(maxlen=50)
        self.combat_tick_seconds_ema = 0.0
        self.cast_delay_seconds_ema = 0.0

    def load_item_matrix(self) -> None:
        if not os.path.exists(self.items_path):
            print(f"[Analytics] Item schema '{self.items_path}' missing.")
            self.item_db = {}
            self.immunity_db = {}
            return
        try:
            with open(self.items_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.item_db = data.get("items", {})
                self.immunity_db = data.get("immunities", {}) or {}
            print(f"[Analytics] Best-in-Slot matrix initialized with {len(self.item_db)} item profiles.")
        except Exception as e:
            print(f"[-] Item profile load error: {e}")
            self.item_db = {}
            self.immunity_db = {}

    # --- Immunity memory DB (bounded, session/runtime persisted in-memory) ---
    def record_immunity(self, mob_name: str, immune_type: str = "physical", source: str = "") -> bool:
        if not mob_name:
            return False
        key = mob_name.strip().lower()
        if not key:
            return False
        # enforce capacity (evict oldest by ts if over limit)
        if key not in self.immunity_db and len(self.immunity_db) >= max(1, int(self.immunity_db_limit)):
            oldest_key = None
            oldest_ts = None
            for k, v in self.immunity_db.items():
                ts = float(v.get("ts", 0) or 0)
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                    oldest_key = k
            if oldest_key is not None:
                self.immunity_db.pop(oldest_key, None)
        rec = self.immunity_db.get(key, {"name": mob_name.strip(), "types": [], "count": 0, "ts": time.time(), "sources": []})
        types = set(rec.get("types", []))
        types.add(str(immune_type))
        rec["types"] = sorted(types)
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["ts"] = time.time()
        if source:
            srcs = rec.get("sources", [])
            if source not in srcs:
                srcs = (srcs + [source])[-5:]
            rec["sources"] = srcs
        rec["name"] = mob_name.strip()
        self.immunity_db[key] = rec
        return True

    def is_immune(self, mob_name: str, immune_type: Optional[str] = None) -> bool:
        if not mob_name:
            return False
        rec = self.immunity_db.get(mob_name.strip().lower())
        if not rec:
            return False
        if immune_type is None:
            return True
        return immune_type in set(rec.get("types", []))

    # --- Session / state time analysis ---
    def set_active_state(self, state: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        if self._state_enter_time:
            elapsed = max(0.0, now - self._state_enter_time)
            key = self._active_state if self._active_state in self.state_durations else "IDLE"
            self.state_durations[key] = self.state_durations.get(key, 0.0) + elapsed
        self._active_state = state
        self._state_enter_time = now

    def _flush_state_time(self, now: Optional[float] = None) -> None:
        self.set_active_state(self._active_state, now)

    def session_duration(self) -> float:
        return max(0.0, time.time() - self.session_start)

    def get_time_analysis(self) -> Dict[str, Any]:
        self._flush_state_time()
        total = sum(self.state_durations.values()) or 0.0
        moving = self.state_durations.get("NAVIGATING", 0.0)
        attacking = self.state_durations.get("COMBAT", 0.0)
        resting = self.state_durations.get("RESTING", 0.0)
        idle = self.state_durations.get("IDLE", 0.0)
        pct = lambda v: round((v / total * 100.0), 2) if total > 0 else 0.0
        return {
            "session_seconds": round(self.session_duration(), 2),
            "tracked_seconds": round(total, 2),
            "moving_seconds": round(moving, 2),
            "attacking_seconds": round(attacking, 2),
            "resting_seconds": round(resting, 2),
            "idle_seconds": round(idle, 2),
            "moving_pct": pct(moving),
            "attacking_pct": pct(attacking),
            "resting_pct": pct(resting),
            "idle_pct": pct(idle),
        }

    # --- Movement / lap analysis ---
    def record_move_time(self, seconds: float) -> None:
        if seconds is None:
            return
        s = float(seconds)
        if 0.0 < s < 120.0:
            self.move_time_samples.append(s)

    def walk_speed(self) -> float:
        """Average seconds per room over recent moves."""
        if not self.move_time_samples:
            return 0.0
        return round(sum(self.move_time_samples) / len(self.move_time_samples), 3)

    def lap_start(self, path_key: Optional[str], now: Optional[float] = None) -> None:
        if not path_key:
            return
        self._lap_start[str(path_key)] = now if now is not None else time.time()

    def lap_end(self, path_key: Optional[str], now: Optional[float] = None) -> Optional[float]:
        if not path_key:
            return None
        key = str(path_key)
        start = self._lap_start.get(key)
        if start is None:
            return None
        now = now if now is not None else time.time()
        dur = max(0.0, now - start)
        dq = self.path_lap_times.setdefault(key, deque(maxlen=200))
        if dur > 0:
            dq.append(dur)
            self.last_lap_time[key] = dur
        self._lap_start[key] = now  # next lap starts immediately
        return round(dur, 3)

    def lap_stats(self, path_key: Optional[str]) -> Dict[str, float]:
        key = str(path_key) if path_key else ""
        dq = self.path_lap_times.get(key)
        if not dq:
            return {"current": round(self.last_lap_time.get(key, 0.0), 3), "min": 0.0, "avg": 0.0, "max": 0.0, "count": 0}
        vals = list(dq)
        return {
            "current": round(self.last_lap_time.get(key, vals[-1]), 3),
            "min": round(min(vals), 3),
            "avg": round(sum(vals) / len(vals), 3),
            "max": round(max(vals), 3),
            "count": len(vals),
        }

    # --- XP / velocity ---
    def append_xp_data_point(self, current_total_xp: int) -> None:
        now = time.time()
        self.total_xp_gained = int(current_total_xp)
        self.xp_history.append((now, self.total_xp_gained))
        self._prune(now)

    def _prune(self, current_time: float) -> None:
        cutoff = current_time - 3600
        while self.xp_history and self.xp_history[0][0] < cutoff:
            self.xp_history.popleft()

    def calculate_rolling_velocity(self, lookback_seconds: int = 300) -> float:
        if len(self.xp_history) < 2:
            return 0.0
        now = time.time()
        target_time = now - lookback_seconds
        anchor = self.xp_history[0]
        for pt in self.xp_history:
            if pt[0] <= target_time:
                anchor = pt
            else:
                break
        latest = self.xp_history[-1]
        dt = latest[0] - anchor[0]
        dx = latest[1] - anchor[1]
        if dt <= 0 or dx <= 0:
            return 0.0
        return round((dx / dt) * 3600.0, 2)

    # --- Path efficiency / dynamic optimization ---
    def record_path_xp(self, path_key: Optional[str], gained_xp: int) -> Optional[float]:
        if not path_key or gained_xp <= 0:
            return None
        now = time.time()
        rec = self.path_efficiency.setdefault(path_key, {"samples": deque(maxlen=500), "ema_xp_hr": 0.0, "last_update": now})
        samples = rec["samples"]
        samples.append((now, int(gained_xp)))
        cutoff = now - 600  # 10-minute rolling window per path
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        total = sum(x for _, x in samples)
        span = max(1.0, (samples[-1][0] - samples[0][0]) if len(samples) > 1 else 1.0)
        inst_xp_hr = (total / span) * 3600.0
        ema = rec.get("ema_xp_hr", 0.0)
        rec["ema_xp_hr"] = inst_xp_hr if ema <= 0 else (ema * 0.7 + inst_xp_hr * 0.3)
        rec["last_update"] = now
        return round(rec["ema_xp_hr"], 2)

    def get_path_efficiency(self, path_key: Optional[str]) -> float:
        if not path_key:
            return 0.0
        rec = self.path_efficiency.get(path_key)
        if not rec:
            return 0.0
        return float(rec.get("ema_xp_hr", 0.0))

    def path_status(self, path_key: Optional[str], min_xp_hr: float = 0.0) -> str:
        if not path_key:
            return "neutral"
        eff = self.get_path_efficiency(path_key)
        if eff <= 0:
            return "neutral"
        if min_xp_hr > 0 and eff < min_xp_hr:
            return "bad"
        if min_xp_hr > 0 and eff >= min_xp_hr:
            return "good"
        return "neutral"

    # --- Combat/room telemetry ---
    def record_combat_burst(self, now: Optional[float] = None) -> Optional[float]:
        """Record incoming damage/combat text burst; returns observed interval (s) when plausible."""
        now = float(now if now is not None else time.time())
        interval = None
        if self._last_combat_burst_ts > 0:
            dt = now - self._last_combat_burst_ts
            # ignore implausible gaps/jitter; typical MUD rounds ~1-4s but modded servers vary
            if 0.25 <= dt <= 8.0:
                self._combat_round_intervals.append(dt)
                ema = self.combat_tick_seconds_ema
                self.combat_tick_seconds_ema = dt if ema <= 0 else (ema * 0.7 + dt * 0.3)
                # conservative cast/action pacing: slightly under observed tick
                self.cast_delay_seconds_ema = max(0.15, self.combat_tick_seconds_ema * 0.85)
                interval = dt
        self._last_combat_burst_ts = now
        self._combat_burst_times.append(now)
        return interval

    def get_combat_tick_delay(self, default: float = 2.0) -> float:
        if self.combat_tick_seconds_ema > 0:
            return round(self.combat_tick_seconds_ema, 3)
        if self._combat_round_intervals:
            vals = list(self._combat_round_intervals)[-10:]
            return round(sum(vals) / len(vals), 3)
        return float(default)

    def get_recommended_cast_delay(self, default: float = 0.3) -> float:
        if self.cast_delay_seconds_ema > 0:
            return round(self.cast_delay_seconds_ema, 3)
        tick = self.get_combat_tick_delay(default=2.0)
        return round(max(0.15, min(default, tick * 0.85)), 3)

    def record_round(self, now: Optional[float] = None) -> None:
        self.current_rounds_in_fight += 1
        self.current_rounds_in_room += 1
        now = float(now if now is not None else time.time())
        if self._last_round_marker_ts > 0:
            dt = now - self._last_round_marker_ts
            if 0.25 <= dt <= 8.0:
                self._combat_round_intervals.append(dt)
                ema = self.combat_tick_seconds_ema
                self.combat_tick_seconds_ema = dt if ema <= 0 else (ema * 0.7 + dt * 0.3)
                self.cast_delay_seconds_ema = max(0.15, self.combat_tick_seconds_ema * 0.85)
        self._last_round_marker_ts = now
        self._last_combat_burst_ts = now

    def record_kill(self) -> None:
        if self.current_rounds_in_fight > 0:
            self.rtk_samples.append(self.current_rounds_in_fight)
        self.current_rounds_in_fight = 0

    def record_room_clear(self) -> None:
        if self.current_rounds_in_room > 0:
            self.rtc_samples.append(self.current_rounds_in_room)
        self.current_rounds_in_room = 0

    def average_rtk(self) -> float:
        return round(sum(self.rtk_samples) / len(self.rtk_samples), 2) if self.rtk_samples else 0.0

    def average_rtc(self) -> float:
        return round(sum(self.rtc_samples) / len(self.rtc_samples), 2) if self.rtc_samples else 0.0

    # --- BiS / local item lookups ---
    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self.item_db.get(str(item_id))

    def calculate_item_score(self, item_properties: Dict[str, Any]) -> float:
        ac_score = item_properties.get("ac", 0) * self.stat_weights.get("ac", 0.0)
        str_score = item_properties.get("strength", 0) * self.stat_weights.get("strength", 0.0)
        int_score = item_properties.get("intellect", 0) * self.stat_weights.get("intellect", 0.0)
        hp_score = item_properties.get("hp", 0) * self.stat_weights.get("hp", 0.0)
        return round(ac_score + str_score + int_score + hp_score, 2)

    def evaluate_loot_drop_upgrade(self, item_id: str) -> bool:
        item = self.item_db.get(str(item_id))
        if not item:
            return False
        if self.char_level < item.get("min_level", 1):
            print(f"[BiS] Level gate: char {self.char_level} < item {item.get('min_level', 1)}")
            return False
        slot = item.get("slot")
        item_score = self.calculate_item_score(item)
        current_equipment_score = self.equipped_gear.get(slot, {}).get("score", 0.0)
        print(f"[BiS] '{item.get('name')}' score {item_score} vs current {current_equipment_score}")
        if item_score > current_equipment_score:
            print(f"[BiS] Upgrade for slot [{slot}] (+{round(item_score - current_equipment_score, 2)})")
            return True
        return False

    def update_equipped_state(self, slot: str, item_id: str) -> None:
        item = self.item_db.get(str(item_id))
        if item:
            self.equipped_gear[slot] = {"id": str(item_id), "name": item.get("name", str(item_id)), "score": self.calculate_item_score(item)}
            print(f"[Inventory] Equipped {slot}: {item.get('name')}")


    def immunity_count(self) -> int:
        return len(self.immunity_db)

    # --- Avoidance / anti-loop registry (self-healing safety) ---
    def record_room_failure(self, room_id=None, monsters=None, reason="flee", now=None):
        """Log a hostile failure for a room (flee/lethal/death-block). Returns avoidance decision."""
        now = float(now if now is not None else time.time())
        rid = str(room_id or "").strip() or "unknown"
        rec = {"ts": now, "reason": str(reason or "failure"), "monsters": list(monsters or [])[:10]}
        dq = self.room_failures.setdefault(rid, deque(maxlen=50))
        dq.append(rec)
        cutoff = now - float(self.avoidance_window_s)
        while dq and float(dq[0].get("ts", 0)) < cutoff:
            dq.popleft()
        count = len(dq)
        avoided = False
        until_ts = 0.0
        if count >= int(self.avoidance_threshold):
            until_ts = now + float(self.avoidance_penalty_s)
            self.room_avoidance[rid] = {"until_ts": until_ts, "count": count, "reason": rec["reason"], "monsters": rec["monsters"]}
            avoided = True
        if len(self.room_failures) > int(self.avoidance_max_rooms):
            for k in list(self.room_failures.keys())[: max(0, len(self.room_failures) - int(self.avoidance_max_rooms))]:
                if k != rid and not self.room_failures.get(k):
                    self.room_failures.pop(k, None)
        return {"room_id": rid, "count": count, "avoided": avoided, "until_ts": until_ts, "monsters": rec["monsters"]}

    def failure_count_recent(self, room_id=None, window_s=None, now=None):
        rid = str(room_id or "").strip()
        if not rid or rid not in self.room_failures:
            return 0
        now = float(now if now is not None else time.time())
        cutoff = now - float(window_s if window_s is not None else self.avoidance_window_s)
        dq = self.room_failures[rid]
        while dq and float(dq[0].get("ts", 0)) < cutoff:
            dq.popleft()
        return len(dq)

    def is_room_avoided(self, room_id=None, now=None):
        rid = str(room_id or "").strip()
        if not rid:
            return False
        now = float(now if now is not None else time.time())
        av = self.room_avoidance.get(rid)
        if not av:
            return False
        if float(av.get("until_ts", 0)) <= now:
            self.room_avoidance.pop(rid, None)
            return False
        return True

    def expired_avoidances(self, now=None):
        now = float(now if now is not None else time.time())
        expired = [rid for rid, av in list(self.room_avoidance.items()) if float(av.get("until_ts", 0)) <= now]
        for rid in expired:
            self.room_avoidance.pop(rid, None)
        return expired

    def active_avoidances(self, now=None):
        self.expired_avoidances(now)
        return dict(self.room_avoidance)


    def get_summary(self, active_path_key: Optional[str] = None) -> Dict[str, Any]:
        ta = self.get_time_analysis()
        lap = self.lap_stats(active_path_key) if active_path_key else {"current": 0.0, "min": 0.0, "avg": 0.0, "max": 0.0, "count": 0}
        try:
            avoidance_active = len(self.active_avoidances())
        except Exception:
            avoidance_active = 0
        return {
            "total_xp": self.total_xp_gained,
            "xp_hr_5m": self.calculate_rolling_velocity(300),
            "xp_hr_15m": self.calculate_rolling_velocity(900),
            "xp_hr_60m": self.calculate_rolling_velocity(3600),
            "walk_speed_s_per_room": self.walk_speed(),
            "rtk_avg": self.average_rtk(),
            "rtc_avg": self.average_rtc(),
            "lap": lap,
            "time": ta,
            "immunity_count": len(self.immunity_db),
            "avoidance_active": avoidance_active,
        }


if __name__ == "__main__":
    analytics = ApexForgeAnalytics("items.json")
    analytics.append_xp_data_point(50000)
    analytics.xp_history[0] = (time.time() - 300, 50000)
    analytics.append_xp_data_point(62500)
    print("xp/hr(5m)", analytics.calculate_rolling_velocity(300))
    analytics.evaluate_loot_drop_upgrade("102")
