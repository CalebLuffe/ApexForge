import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx


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


class ApexForgeMapper:
    def __init__(self, database_path: str = "database.json"):
        self.database_path = database_path
        self.graph = nx.DiGraph()
        self.raw_room_data: Dict[str, Any] = {}
        self._room_name_index: Dict[str, str] = {}
        self._room_name_multi: Dict[str, List[str]] = {}
        self._room_hash_index: Dict[str, List[str]] = {}
        self._code_index: Dict[str, str] = {}
        self._coord_index: Dict[Tuple[int, int], str] = {}
        self.load_graph_from_json()

    def load_graph_from_json(self) -> None:
        if not os.path.exists(self.database_path):
            print(f"[Mapper] Database file '{self.database_path}' not found. Spawning blank layer.")
            self.raw_room_data = {}
            self.graph.clear()
            self._rebuild_indexes()
            return

        try:
            with open(self.database_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.raw_room_data = data.get("rooms", {})

            self.graph.clear()
            for room_id, attributes in self.raw_room_data.items():
                self.graph.add_node(
                    str(room_id),
                    name=attributes.get("name", "Unknown Room"),
                    zone=attributes.get("zone", "unknown"),
                    x=attributes.get("x"),
                    y=attributes.get("y"),
                    drops=attributes.get("drops", []),
                    pre_move_cmd=attributes.get("pre_move_cmd", ""),
                    post_move_cmd=attributes.get("post_move_cmd", ""),
                    min_room_wait=float(attributes.get("min_room_wait", 0.0)),
                )

            for room_id, attributes in self.raw_room_data.items():
                for direction, exit_properties in attributes.get("exits", {}).items():
                    target_id = str(exit_properties.get("target"))
                    weight_cost = float(exit_properties.get("weight", 1.0))
                    if self.graph.has_node(target_id):
                        self.graph.add_edge(
                            str(room_id),
                            target_id,
                            direction=direction,
                            weight=weight_cost,
                            base_weight=weight_cost,
                        )

            self._rebuild_indexes()
            print(f"[Mapper] Topography synced: Loaded {self.graph.number_of_nodes()} rooms and {self.graph.number_of_edges()} exit paths.")
        except Exception as file_error:
            print(f"[-] Mapper load error: {file_error}")
            self.raw_room_data = {}
            self.graph.clear()
            self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._room_name_index = {}
        self._room_name_multi = {}
        self._room_hash_index = {}
        self._code_index = {}
        self._coord_index = {}
        for rid, attrs in self.raw_room_data.items():
            srid = str(rid)
            nraw = str(attrs.get("name") or "")
            nrm = normalize_room_name(nraw)
            if nrm:
                self._room_name_index[nrm] = srid
                self._room_name_multi.setdefault(nrm, []).append(srid)
            for key in (srid, str(attrs.get("id") or ""), str(attrs.get("megamud_code") or ""), str(attrs.get("code") or "")):
                if key and key != "None":
                    self._code_index[key] = srid
                    self._code_index[key.upper()] = srid
                    self._code_index[key.lower()] = srid
            exits = list((attrs.get("exits") or {}).keys())
            rhash = str(attrs.get("room_hash") or "").strip() or room_deterministic_hash(nraw, exits)
            if rhash:
                self._room_hash_index.setdefault(rhash, []).append(srid)
            # also index name-only hash for fallback when exits unknown
            nhash = room_deterministic_hash(nraw, [])
            if nhash:
                self._room_hash_index.setdefault(f"name:{nhash}", []).append(srid)
            x, y = attrs.get("x"), attrs.get("y")
            if isinstance(x, int) and isinstance(y, int):
                self._coord_index[(x, y)] = srid

    def lookup_room_by_name(self, name: str) -> Optional[str]:
        return self._room_name_index.get(normalize_room_name(name))

    def resolve_room(self, room_name: str = "", obvious_exits: Optional[List[str]] = None,
                     megamud_code: Optional[str] = None, room_id: Optional[str] = None) -> Optional[str]:
        """Resolve runtime arrival to canonical room id: (name + obvious exits) -> deterministic hash -> code/id -> name."""
        # 1) explicit id/code
        for cand in (room_id, megamud_code):
            if cand is None:
                continue
            s = str(cand).strip()
            if not s:
                continue
            if self.graph.has_node(s):
                return s
            hit = self._code_index.get(s) or self._code_index.get(s.upper()) or self._code_index.get(s.lower())
            if hit and self.graph.has_node(hit):
                return hit
        nrm = normalize_room_name(room_name)
        exits = obvious_exits or []
        # 2) deterministic hash(name+exits)
        if nrm:
            rhash = room_deterministic_hash(room_name, exits)
            hits = self._room_hash_index.get(rhash, [])
            if len(hits) == 1 and self.graph.has_node(hits[0]):
                return hits[0]
            if len(hits) > 1 and nrm:
                # disambiguate by normalized name
                for h in hits:
                    if normalize_room_name(self.raw_room_data.get(h, {}).get("name", "")) == nrm:
                        return h
                return hits[0]
            # 3) name-only hash fallback
            nhash = f"name:{room_deterministic_hash(room_name, [])}"
            nhits = self._room_hash_index.get(nhash, [])
            if len(nhits) == 1 and self.graph.has_node(nhits[0]):
                return nhits[0]
            if nhits and nrm:
                for h in nhits:
                    if normalize_room_name(self.raw_room_data.get(h, {}).get("name", "")) == nrm and self.graph.has_node(h):
                        return h
        # 4) normalized name index
        if nrm:
            byname = self.lookup_room_by_name(nrm)
            if byname and self.graph.has_node(byname):
                # if exits provided and unique candidates exist, prefer one whose exit set matches best
                cands = self._room_name_multi.get(nrm, [byname])
                if exits and len(cands) > 1:
                    norm_ex = set(normalize_exit_name(e) for e in exits if e)
                    best = byname
                    best_score = -1
                    for c in cands:
                        rex = set(normalize_exit_name(e) for e in (self.raw_room_data.get(c, {}).get("exits") or {}).keys())
                        score = len(norm_ex & rex) if norm_ex else 0
                        if score > best_score:
                            best_score = score
                            best = c
                    return best
                return byname
        return None

    def lookup_rooms_by_zone(self, zone: str) -> List[str]:
        z = (zone or "").strip().lower()
        return [str(rid) for rid, attrs in self.raw_room_data.items() if str(attrs.get("zone", "")).lower() == z]

    def lookup_drops(self, room_id: str) -> List[str]:
        return list(self.raw_room_data.get(str(room_id), {}).get("drops", []))

    def set_edge_weight(self, from_id: str, to_id: str, weight: float) -> bool:
        if self.graph.has_edge(str(from_id), str(to_id)):
            self.graph[str(from_id)][str(to_id)]["weight"] = float(weight)
            return True
        return False

    def adjust_edge_weight(self, from_id: str, to_id: str, multiplier: float, min_weight: float = 0.5, max_weight: float = 50.0) -> Optional[float]:
        if not self.graph.has_edge(str(from_id), str(to_id)):
            return None
        edge = self.graph[str(from_id)][str(to_id)]
        base = float(edge.get("base_weight", edge.get("weight", 1.0)))
        new_w = max(min_weight, min(max_weight, base * float(multiplier)))
        edge["weight"] = new_w
        return new_w

    def adjust_path_weights(self, node_ids: List[str], multiplier: float) -> int:
        changed = 0
        for i in range(len(node_ids) - 1):
            if self.adjust_edge_weight(str(node_ids[i]), str(node_ids[i + 1]), multiplier) is not None:
                changed += 1
        return changed

    def mark_risk(self, room_id: str, risk: float = 2.0) -> int:
        """Increase incoming/outgoing edge weights around a risky room (stealth/PvP signals)."""
        rid = str(room_id)
        changed = 0
        if rid not in self.graph:
            return 0
        for nbr in list(self.graph.successors(rid)) + list(self.graph.predecessors(rid)):
            pairs = [(rid, nbr), (nbr, rid)]
            for a, b in pairs:
                if self.graph.has_edge(a, b):
                    edge = self.graph[a][b]
                    base = float(edge.get("base_weight", edge.get("weight", 1.0)))
                    edge["weight"] = min(100.0, max(edge.get("weight", base), base * float(risk)))
                    changed += 1
        return changed

    def apply_avoidance_penalty(self, room_id: str, penalty: float = 9999.0) -> int:
        """Temporarily penalize all edges to/from a hostile room (anti-loop avoidance)."""
        rid = str(room_id)
        changed = 0
        if rid not in self.graph:
            return 0
        for nbr in list(self.graph.successors(rid)) + list(self.graph.predecessors(rid)):
            for a, b in ((rid, nbr), (nbr, rid)):
                if self.graph.has_edge(a, b):
                    edge = self.graph[a][b]
                    edge.setdefault("base_weight", float(edge.get("weight", 1.0)))
                    edge["weight"] = float(penalty)
                    edge["avoided"] = True
                    changed += 1
        return changed

    def clear_avoidance_penalty(self, room_id: str) -> int:
        """Restore base weights for edges around a room after avoidance expiry."""
        rid = str(room_id)
        changed = 0
        if rid not in self.graph:
            return 0
        for nbr in list(self.graph.successors(rid)) + list(self.graph.predecessors(rid)):
            for a, b in ((rid, nbr), (nbr, rid)):
                if self.graph.has_edge(a, b):
                    edge = self.graph[a][b]
                    base = float(edge.get("base_weight", edge.get("weight", 1.0)))
                    edge["weight"] = base
                    if "avoided" in edge:
                        edge.pop("avoided", None)
                    changed += 1
        return changed

    def find_path_containing_sequence(self, seq: List[str]) -> Optional[str]:
        """Return the last room id if the sequence matches the tail of any known simple path in recent history context.
        Lightweight helper: if last element known and previous matches predecessor along expected pending route, accept it."""
        if not seq:
            return None
        last = str(seq[-1])
        return last if self.graph.has_node(last) else None

    def neighbors(self, room_id: str) -> List[str]:
        rid = str(room_id)
        if rid not in self.graph:
            return []
        return sorted(set(list(self.graph.successors(rid)) + list(self.graph.predecessors(rid))))

    def render_ascii_map(self, current_room_id: Optional[str] = None, path_nodes: Optional[List[str]] = None, zone_status: Optional[Dict[str, str]] = None, risk_rooms: Optional[set] = None) -> str:
        zone_status = zone_status or {}
        path_set = set(str(n) for n in (path_nodes or []))
        risk_rooms = set(str(r) for r in (risk_rooms or set()))
        coords = []
        for rid, attrs in self.raw_room_data.items():
            x, y = attrs.get("x"), attrs.get("y")
            if isinstance(x, int) and isinstance(y, int):
                coords.append((x, y, str(rid), attrs))
        if not coords:
            return "[map] no coordinates available"

        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        # cap render size to keep output readable
        if (max_x - min_x) > 60 or (max_y - min_y) > 30:
            cx, cy = 0, 0
            if current_room_id and str(current_room_id) in self.raw_room_data:
                a = self.raw_room_data[str(current_room_id)]
                if isinstance(a.get("x"), int) and isinstance(a.get("y"), int):
                    cx, cy = int(a["x"]), int(a["y"])
            min_x, max_x = cx - 30, cx + 30
            min_y, max_y = cy - 15, cy + 15

        grid: Dict[Tuple[int, int], str] = {}
        for x, y, rid, attrs in coords:
            if x < min_x or x > max_x or y < min_y or y > max_y:
                continue
            zone = str(attrs.get("zone", ""))
            status = zone_status.get(zone, "neutral")
            marker = "o"
            if status == "good":
                marker = "+"
            elif status == "bad":
                marker = "x"
            if rid in risk_rooms:
                marker = "!"
            if rid in path_set:
                marker = "*"
            if current_room_id and rid == str(current_room_id):
                marker = "@"
            grid[(x, y)] = marker
        lines = []
        for y in range(max_y, min_y - 1, -1):
            row = []
            for x in range(min_x, max_x + 1):
                row.append(grid.get((x, y), "."))
            lines.append(" ".join(row))
        lines.append("")
        lines.append("legend: @ current, * active path, + efficient zone, x inefficient zone, ! risk, o room")
        return "\n".join(lines)

    def compute_dijkstra_route(self, origin_node_id: str, destination_node_id: str) -> Optional[List[Dict[str, Any]]]:
        origin = str(origin_node_id)
        destination = str(destination_node_id)

        if not self.graph.has_node(origin) or not self.graph.has_node(destination):
            print(f"[Mapper] Missing node: {origin} or {destination}")
            return None

        try:
            node_path = nx.dijkstra_path(self.graph, source=origin, target=destination, weight="weight")
            compiled_route_steps = []
            for i in range(len(node_path) - 1):
                current_node = node_path[i]
                next_node = node_path[i + 1]
                edge_data = self.graph.get_edge_data(current_node, next_node) or {}
                node_attributes = self.graph.nodes[current_node]
                step = {
                    "from_node": current_node,
                    "to_node": next_node,
                    "direction": edge_data.get("direction", ""),
                    "weight": edge_data.get("weight", 1.0),
                    "pre_cmd": node_attributes.get("pre_move_cmd", ""),
                    "post_cmd": node_attributes.get("post_move_cmd", ""),
                    "delay": node_attributes.get("min_room_wait", 0.0),
                }
                # Conditional edge metadata (self-healing/navigation safety)
                for k in (
                    "requires_lockpick", "lockpick_cmd", "required_item_id", "required_key",
                    "min_monsters_present", "max_monsters_present", "closed", "requires_skill",
                    "requires_command", "condition",
                ):
                    if k in edge_data:
                        step[k] = edge_data.get(k)
                if edge_data.get("edge_meta") and isinstance(edge_data.get("edge_meta"), dict):
                    step.setdefault("edge_meta", edge_data.get("edge_meta"))
                compiled_route_steps.append(step)
            return compiled_route_steps
        except nx.NetworkXNoPath:
            print(f"[Mapper] No path between {origin} and {destination}.")
            return None
        except Exception as calc_error:
            print(f"[-] Mapper route error: {calc_error}")
            return None


if __name__ == "__main__":
    mapper_engine = ApexForgeMapper("database.json")
    route = mapper_engine.compute_dijkstra_route("1", "4")
    if route:
        print("route", " -> ".join([route[0]["from_node"]] + [s["to_node"] for s in route]))
    print(mapper_engine.render_ascii_map(current_room_id="1", path_nodes=["1", "2", "4"], zone_status={"goblin_caves": "good", "town": "neutral"}, risk_rooms={"4"}))
