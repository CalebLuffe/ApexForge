import asyncio, json, tempfile
from pathlib import Path

from import_megamud import parse_mp
from apexforge import ApexForgeEngine, STATE_RECOVERY, STATE_NAVIGATING
from navigation import ApexForgeMapper


def test_parse_mp_keeps_plain_directions():
    tmp = Path(tempfile.mkdtemp())
    mp = tmp / "sample.mp"
    # MegaMUD .mp step: "<room>:...:<action>" means perform action at <room> to reach next room in sequence.
    mp.write_text(
        "[x]\n[ACCD:Arlysia:Arlysia, City Docks]\n[ARFL:Arlysia:White Forest, Landing]\n"
        "AAAA:0000:w\nBBBB:0000:nw\nCCCC:0000:@party buy skiff\n",
        encoding="utf-8",
    )
    k, cfg = parse_mp(str(mp))
    assert k == "sample"
    moves = [(m["from"], m["to"], m["direction"], m["conditional"]) for m in cfg.get("move_edges", [])]
    assert ("AAAA", "BBBB", "w", False) in moves, moves
    assert ("BBBB", "CCCC", "nw", False) in moves, moves
    assert any(m.get("conditional") and m.get("from") == "CCCC" for m in cfg.get("move_edges", [])), moves


def test_recovery_unlock_on_prompt_and_p1_not_dropping_chunk():
    eng = ApexForgeEngine(profile_name=None)
    eng._bbs_menu_prompt = "[MAJORMUD]:"
    eng._enter_recovery_state("stunned", lock_until_prompt=True)
    assert eng.state == STATE_RECOVERY

    async def run():
        await eng.parse_stream_line("[MAJORMUD]:")
        assert eng.state == STATE_NAVIGATING, eng.state

        eng2 = ApexForgeEngine(profile_name=None)
        ev = []
        eng2.add_event_listener(lambda e, _: ev.append(e))
        await eng2._process_stream_chunk("You are stunned!\nThe goblin hits you.\n")
        assert eng2.state == STATE_RECOVERY, eng2.state
        assert any(e.get("type") == "stream" and "goblin hits you" in ((e.get("payload") or {}).get("line", "")) for e in ev), ev

    asyncio.run(run())


def test_resolver_and_fake_server_replay():
    # Minimal, self-contained database so the test does not depend on
    # D:\ApexForge\database.json existing at an absolute path.
    tmpdb = Path(tempfile.mkdtemp()) / "database.json"
    tmpdb.write_text(
        json.dumps({"rooms": {
            "1": {
                "id": "1", "name": "The Grand Town Square", "zone": "town",
                "exits": {"north": {"target": "2", "weight": 1.0}, "east": {"target": "3", "weight": 1.0}},
            },
            "2": {"id": "2", "name": "North Road", "zone": "town", "exits": {}},
            "3": {"id": "3", "name": "East Road", "zone": "town", "exits": {}},
        }}),
        encoding="utf-8",
    )
    m = ApexForgeMapper(str(tmpdb))
    assert m.resolve_room("The Grand Town Square", ["north", "east"]) == "1"

    async def run():
        events = []

        async def handler(reader, writer):
            writer.write(b"[MAJORMUD]:\r\n")
            writer.write(b"The Grand Town Square\r\nObvious exits: north, east\r\n")
            writer.write(b"[MAJORMUD]:\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()
        async with server:
            eng = ApexForgeEngine(profile_name=None, host=host, port=port)
            eng.add_event_listener(lambda e, _: events.append(e))
            task = asyncio.create_task(eng.run_lifecycle(connect=True))
            await asyncio.sleep(0.6)
            eng.is_running = False
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except Exception:
                task.cancel()
        assert any(e.get("type") == "stream" for e in events), [e.get("type") for e in events]

    asyncio.run(run())


if __name__ == "__main__":
    test_parse_mp_keeps_plain_directions()
    test_recovery_unlock_on_prompt_and_p1_not_dropping_chunk()
    test_resolver_and_fake_server_replay()
    print("smoke_ok")
