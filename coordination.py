import asyncio
import json
import time
import urllib.request
from typing import Any, Awaitable, Callable, Dict, Optional, Set


class ApexForgeCoordinationBus:
    def __init__(self, bus_host: str = "127.0.0.1", bus_port: int = 5555, webhook_url: Optional[str] = None):
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.webhook_url = webhook_url
        self.follower_clients: Set[asyncio.StreamWriter] = set()
        self.leader_state: Dict[str, Any] = {"action": "IDLE", "target": "", "room_id": "1"}
        self.command_handler: Optional[Callable[[Dict[str, Any], asyncio.StreamWriter], Awaitable[Optional[str]]]] = None

    async def dispatch_discord_alert(self, event_title: str, description: str, severity: str = "INFO") -> None:
        if not self.webhook_url:
            print(f"[Webhook] [{severity}] {event_title}: {description}")
            return
        color_map = {"CRITICAL": 16515843, "WARNING": 16753920, "INFO": 3066993}
        payload = {
            "username": "ApexForge Alert System",
            "embeds": [
                {
                    "title": f"[{severity}] {event_title}",
                    "description": description[:3900],
                    "color": color_map.get(severity, 3066993),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ],
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
            print(f"[Webhook] Alert dispatched: {event_title}")
        except Exception as e:
            print(f"[-] Webhook delivery failure: {e}")

    async def handle_bus_subscriber(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.follower_clients.add(writer)
        print(f"[Sync Bus] Peer connected: {peer}")
        try:
            writer.write((json.dumps({"type": "state", **self.leader_state}) + "\n").encode())
            await writer.drain()

            buffer = ""
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    response = await self._handle_bus_message(line, writer)
                    if response:
                        writer.write((response.rstrip() + "\n").encode())
                        await writer.drain()
        except Exception:
            pass
        finally:
            self.follower_clients.discard(writer)
            print(f"[Sync Bus] Peer disconnected: {peer}")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_bus_message(self, line: str, writer: asyncio.StreamWriter) -> Optional[str]:
        # Accept JSON or plain text admin/status messages
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                msg = {"type": "text", "text": line}
        except Exception:
            msg = {"type": "text", "text": line}

        if msg.get("type") in ("status", "pong"):
            print(f"[Sync Bus] Peer status: {msg}")
            return None

        if self.command_handler:
            try:
                return await self.command_handler(msg, writer)
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
        return None

    async def broadcast_leader_state_shift(self, action: str, target: str = "", room_id: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {"type": "state", "action": action, "target": target, "room_id": room_id, "timestamp": time.time()}
        if extra:
            payload.update(extra)
        self.leader_state = payload
        line = json.dumps(payload) + "\n"
        print(f"[Sync Bus] Broadcast state: {action} target='{target}' room='{room_id}'")
        dead = []
        for writer in list(self.follower_clients):
            try:
                writer.write(line.encode())
                await writer.drain()
            except Exception:
                dead.append(writer)
        for w in dead:
            self.follower_clients.discard(w)

    async def start_coordination_bus(self) -> None:
        server = await asyncio.start_server(self.handle_bus_subscriber, self.bus_host, self.bus_port)
        print(f"[Sync Bus] Party Communication Bus active on {self.bus_host}:{self.bus_port}")
        async with server:
            await server.serve_forever()


class ApexForgeFollowerNode:
    def __init__(self, bus_host: str = "127.0.0.1", bus_port: int = 5555):
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.is_running = True

    async def run_follower_sync_loop(self, engine_command_callback):
        """Backward-compatible follower loop: forwards state packets to callback."""
        while self.is_running:
            try:
                reader, writer = await asyncio.open_connection(self.bus_host, self.bus_port)
                print("[Follower Sync] Connected to Master Party Bus.")
                buffer = ""
                while self.is_running:
                    data = await reader.read(4096)
                    if not data:
                        print("[Follower Sync] Master severed synchronization bus.")
                        break
                    buffer += data.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if not line.strip():
                            continue
                        try:
                            state_packet = json.loads(line)
                        except Exception:
                            continue
                        if state_packet.get("type") == "state":
                            await engine_command_callback(state_packet)
            except Exception as e:
                print(f"[Follower Sync] Bus unavailable, retrying in 5s ({e})")
                await asyncio.sleep(5)


class ApexForgePartyFollowerClient:
    """Follower bus client that executes leader-coordinated actions via an engine callback.

    The callback signature is flexible:
      - async/sync callable taking (packet) and returning optional dict/str response
      - or an object exposing `handle_party_state(packet)` / `handle_bus_packet(packet)`
    Responses (if any) are sent back to the leader as bus messages.
    """

    def __init__(self, bus_host: str = "127.0.0.1", bus_port: int = 5555, name: str = "follower"):
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.name = name
        self.is_running = True

    async def _dispatch(self, handler, packet: Dict[str, Any]):
        fn = None
        if callable(handler):
            fn = handler
        elif hasattr(handler, "handle_party_state"):
            fn = getattr(handler, "handle_party_state")
        elif hasattr(handler, "handle_bus_packet"):
            fn = getattr(handler, "handle_bus_packet")
        if fn is None:
            return None
        res = fn(packet)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    async def run(self, handler):
        while self.is_running:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(self.bus_host, self.bus_port)
                hello = {"type": "status", "from": self.name, "event": "connected"}
                writer.write((json.dumps(hello) + "\n").encode())
                await writer.drain()
                buffer = ""
                while self.is_running:
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
                        resp = await self._dispatch(handler, packet)
                        if resp is None:
                            continue
                        if isinstance(resp, (dict, list)):
                            out = json.dumps(resp)
                        else:
                            out = str(resp)
                        writer.write((out.rstrip() + "\n").encode())
                        await writer.drain()
            except Exception:
                await asyncio.sleep(3)
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
