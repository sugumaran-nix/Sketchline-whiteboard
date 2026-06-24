"""
Integration test against a REAL running server (not TestClient — avoids
test-transport buffering quirks and exercises the actual production code
path over real sockets).

Usage:
    python3 -m uvicorn main:app --host 127.0.0.1 --port 8125 &
    python3 test_live.py
"""
import asyncio
import json
import uuid

import websockets

URL = "ws://127.0.0.1:8125/ws"


async def recv(ws, timeout=3):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def drain_until(ws, msg_type, timeout=3):
    """Read messages until one of the given type is found; return it."""
    for _ in range(10):
        msg = await recv(ws, timeout)
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"never saw message type {msg_type}")


async def main():
    room = f"test-{uuid.uuid4().hex[:8]}"

    async with websockets.connect(f"{URL}/{room}?name=Alice") as a:
        init_a = await recv(a)
        assert init_a["type"] == "init" and init_a["strokes"] == []
        a_id = init_a["clientId"]
        await drain_until(a, "user_count")  # A's own count echo (1)
        print("✓ A connected, got empty init")

        async with websockets.connect(f"{URL}/{room}?name=Bob") as b:
            init_b = await recv(b)
            assert init_b["type"] == "init"
            assert len(init_b["users"]) == 1 and init_b["users"][0]["name"] == "Alice"
            b_id = init_b["clientId"]
            await drain_until(b, "user_count")  # B's own count echo (2)
            print("✓ B connected, sees Alice in roster")

            joined = await drain_until(a, "user_joined")
            assert joined["user"]["id"] == b_id
            count = await drain_until(a, "user_count")
            assert count["count"] == 2
            print("✓ A notified of B joining, count=2")

            # Draw a 3-point stroke from A
            stroke_id = "stroke-1"
            await a.send(json.dumps({"type": "stroke_start", "strokeId": stroke_id, "color": "#ff0000", "width": 6, "tool": "pen", "point": {"x": 10, "y": 10}}))
            await a.send(json.dumps({"type": "stroke_point", "strokeId": stroke_id, "point": {"x": 20, "y": 20}}))
            await a.send(json.dumps({"type": "stroke_point", "strokeId": stroke_id, "point": {"x": 30, "y": 15}}))
            await a.send(json.dumps({"type": "stroke_end", "strokeId": stroke_id}))

            e1 = await recv(b)
            e2 = await recv(b)
            e3 = await recv(b)
            e4 = await recv(b)
            assert e1["type"] == "stroke_start" and e1["id"] == a_id and e1["point"] == {"x": 10, "y": 10}
            assert e2["type"] == "stroke_point" and e2["point"] == {"x": 20, "y": 20}
            assert e3["type"] == "stroke_point" and e3["point"] == {"x": 30, "y": 15}
            assert e4["type"] == "stroke_end"
            print("✓ B received full stroke in real time (start, 2 points, end)")

            # Cursor broadcast (throttled client-side; here we just send one)
            await a.send(json.dumps({"type": "cursor", "x": 99, "y": 42}))
            cursor_evt = await recv(b)
            assert cursor_evt["type"] == "cursor" and cursor_evt["x"] == 99 and cursor_evt["name"] == "Alice"
            print("✓ B received A's cursor position + name")

        # B disconnected — A should learn about it
        left = await drain_until(a, "user_left")
        assert left["id"] == b_id
        count2 = await drain_until(a, "user_count")
        assert count2["count"] == 1
        print("✓ A notified B left, count=1")

        # Carol joins late — must receive the completed stroke as a snapshot
        async with websockets.connect(f"{URL}/{room}?name=Carol") as c:
            init_c = await recv(c)
            assert init_c["type"] == "init"
            assert len(init_c["strokes"]) == 1
            assert init_c["strokes"][0]["strokeId"] == stroke_id
            assert len(init_c["strokes"][0]["points"]) == 3
            await drain_until(c, "user_count")  # Carol's own count echo
            print("✓ Carol (late joiner) replayed 1 stroke with 3 points from snapshot")

            # Clear the board
            await c.send(json.dumps({"type": "clear"}))
            clear_evt = await drain_until(a, "clear")
            assert clear_evt["type"] == "clear"
            print("✓ Clear event broadcast to A")

        # Dave joins after the clear -> must see an empty board
        async with websockets.connect(f"{URL}/{room}?name=Dave") as d:
            init_d = await recv(d)
            assert init_d["strokes"] == []
            print("✓ Dave sees empty board after clear")

    print("\nALL LIVE PROTOCOL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
