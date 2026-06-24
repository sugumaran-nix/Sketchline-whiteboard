"""
Realtime Collaborative Whiteboard — FastAPI backend.

Key reliability fixes vs v1
----------------------------
1. Ping/pong keepalive:  The server sends a {"type":"ping"} every 20 seconds.
   The frontend responds with {"type":"pong"}.  This prevents Hugging Face
   Spaces' nginx reverse-proxy (and most other proxies) from closing idle
   WebSocket connections due to their ~30-60 s read timeout.

2. Delayed room teardown: When the last client leaves, the room is kept in
   memory for ROOM_LINGER_SECS (60 s) before being deleted.  This means a
   client that briefly disconnects and reconnects (e.g. page refresh, mobile
   network blip, or HF proxy timeout) gets its full stroke history replayed
   rather than seeing a blank canvas.

3. Concurrent receive + ping: The message-receive loop and the ping timer run
   as two concurrent asyncio tasks.  When the receive task finishes (disconnect)
   the ping task is cancelled, and vice-versa.  This avoids the original design
   where a blocking receive_text() prevented the server from ever sending pings.

Protocol additions
------------------
  S→C  {"type":"ping"}   — sent every PING_INTERVAL_SECS, expect a pong back
  C→S  {"type":"pong"}   — the client's reply; server resets its watchdog timer
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whiteboard")

app = FastAPI(title="Realtime Whiteboard Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Tuning constants ──────────────────────────────────────────────────────────
PING_INTERVAL_SECS = 20   # how often the server pings each client
PONG_TIMEOUT_SECS  = 15   # how long to wait for a pong before dropping
ROOM_LINGER_SECS   = 60   # how long to keep an empty room (stroke history) alive

USER_COLORS = [
    "#F97316", "#22C55E", "#3B82F6", "#EC4899",
    "#A855F7", "#EAB308", "#14B8A6", "#EF4444",
]
ADJECTIVES = ["Swift", "Calm", "Bold", "Quiet", "Bright", "Lucky", "Sharp", "Witty"]
ANIMALS    = ["Otter", "Falcon", "Panda", "Lynx", "Heron", "Fox", "Wren", "Tiger"]


def random_name() -> str:
    return f"{random.choice(ADJECTIVES)} {random.choice(ANIMALS)}"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ConnectedClient:
    id: str
    websocket: WebSocket
    name: str
    color: str


@dataclass
class Room:
    id: str
    clients: Dict[str, ConnectedClient] = field(default_factory=dict)
    strokes: List[Dict[str, Any]]       = field(default_factory=list)
    active_strokes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # asyncio task that will delete this room after ROOM_LINGER_SECS if no
    # new client joins; cancelled when a client connects.
    _linger_task: Optional[asyncio.Task] = field(default=None, repr=False)

    async def broadcast(
        self, message: Dict[str, Any], exclude: Optional[str] = None
    ) -> None:
        dead: List[str] = []
        payload = json.dumps(message)
        for cid, client in list(self.clients.items()):
            if cid == exclude:
                continue
            try:
                await client.websocket.send_text(payload)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)

    def user_list(self) -> List[Dict[str, str]]:
        return [
            {"id": c.id, "name": c.name, "color": c.color}
            for c in self.clients.values()
        ]

    def cancel_linger(self) -> None:
        if self._linger_task and not self._linger_task.done():
            self._linger_task.cancel()
        self._linger_task = None

    def schedule_linger(self, room_id: str) -> None:
        """Start the countdown to delete this room when it's been empty for a while."""
        self.cancel_linger()
        async def _linger():
            try:
                await asyncio.sleep(ROOM_LINGER_SECS)
                rooms.pop(room_id, None)
                logger.info("room %s expired after linger period", room_id)
            except asyncio.CancelledError:
                pass
        self._linger_task = asyncio.create_task(_linger())


rooms: Dict[str, Room] = {}


def get_or_create_room(room_id: str) -> Room:
    room = rooms.get(room_id)
    if room is None:
        room = Room(id=room_id)
        rooms[room_id] = room
    return room


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "whiteboard-backend", "rooms": len(rooms)}


@app.get("/rooms/{room_id}/stats")
async def room_stats(room_id: str) -> Dict[str, Any]:
    room = rooms.get(room_id)
    if room is None:
        return {"exists": False, "userCount": 0, "strokeCount": 0}
    return {
        "exists": True,
        "userCount": len(room.clients),
        "strokeCount": len(room.strokes),
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    room_id: str,
    name: Optional[str] = None,
) -> None:
    await websocket.accept()

    room = get_or_create_room(room_id)
    room.cancel_linger()  # a client is here — cancel any pending room expiry

    client_id    = str(uuid.uuid4())
    color        = USER_COLORS[len(room.clients) % len(USER_COLORS)]
    display_name = (name or random_name()).strip()[:24] or random_name()
    client       = ConnectedClient(id=client_id, websocket=websocket, name=display_name, color=color)

    existing_users = room.user_list()
    room.clients[client_id] = client
    logger.info("client %s (%s) joined room %s  (%d total)", client_id, display_name, room_id, len(room.clients))

    # ── Initial snapshot ──
    await websocket.send_text(json.dumps({
        "type":      "init",
        "clientId":  client_id,
        "color":     color,
        "name":      display_name,
        "strokes":   room.strokes,
        "users":     existing_users,
        "userCount": len(room.clients),
    }))

    await room.broadcast(
        {"type": "user_joined", "user": {"id": client_id, "name": display_name, "color": color}},
        exclude=client_id,
    )
    await room.broadcast({"type": "user_count", "count": len(room.clients)})

    # ── Concurrent tasks ──────────────────────────────────────────────────────
    # Task A: receive messages from this client
    # Task B: periodically send a ping and wait for pong
    # Either task finishing causes the other to be cancelled.

    stop_event = asyncio.Event()

    pong_flag = {"v": False}  # mutable cell shared between both inner functions

    async def receive_loop() -> None:
        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=PING_INTERVAL_SECS + PONG_TIMEOUT_SECS + 5,
                    )
                except asyncio.TimeoutError:
                    logger.warning("receive timeout for client %s — dropping", client_id)
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Pong is handled here (updates shared flag) and not forwarded
                if msg.get("type") == "pong":
                    pong_flag["v"] = True
                    continue

                await handle_message(msg, client, room, client_id, room_id)
        except (WebSocketDisconnect, Exception) as exc:
            if not isinstance(exc, WebSocketDisconnect):
                logger.debug("receive error for %s: %s", client_id, exc)
        finally:
            stop_event.set()

    async def ping_loop() -> None:
        try:
            while not stop_event.is_set():
                await asyncio.sleep(PING_INTERVAL_SECS)
                if stop_event.is_set():
                    break
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    logger.debug("ping send failed for %s", client_id)
                    break
                # Wait up to PONG_TIMEOUT_SECS in 0.5 s increments
                pong_flag["v"] = False
                for _ in range(int(PONG_TIMEOUT_SECS / 0.5)):
                    await asyncio.sleep(0.5)
                    if pong_flag["v"]:
                        break
                else:
                    logger.warning("pong timeout for client %s — dropping", client_id)
                    try:
                        await websocket.close(1001)
                    except Exception:
                        pass
                    break
        except asyncio.CancelledError:
            pass
        finally:
            stop_event.set()

    recv_task = asyncio.create_task(receive_loop())
    ping_task = asyncio.create_task(ping_loop())

    try:
        done, pending = await asyncio.wait(
            [recv_task, ping_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        room.clients.pop(client_id, None)
        logger.info("client %s left room %s  (%d remaining)", client_id, room_id, len(room.clients))

        if room.clients:
            await room.broadcast({"type": "user_left",  "id": client_id})
            await room.broadcast({"type": "user_count", "count": len(room.clients)})
        else:
            # Keep room alive for ROOM_LINGER_SECS so a brief reconnect gap
            # (page refresh, proxy timeout, mobile network blip) doesn't wipe history.
            logger.info("room %s is empty — starting %ds linger timer", room_id, ROOM_LINGER_SECS)
            room.schedule_linger(room_id)


async def handle_message(
    msg: Dict[str, Any],
    client: ConnectedClient,
    room: Room,
    client_id: str,
    room_id: str,
) -> None:
    """Dispatch a parsed client→server message."""
    msg_type = msg.get("type")

    if msg_type == "pong":
        # Handled inline by ping_loop via the shared dict; nothing else to do.
        # We import pong_received via closure so just return here.
        return  # ping_loop polls the dict directly

    if msg_type == "stroke_start":
        stroke_id = str(msg.get("strokeId", ""))
        stroke: Dict[str, Any] = {
            "strokeId": stroke_id,
            "color":    msg.get("color", "#000000"),
            "width":    msg.get("width", 4),
            "tool":     msg.get("tool", "pen"),
            "points":   [msg["point"]] if msg.get("point") else [],
            "authorId": client_id,
        }
        room.active_strokes[stroke_id] = stroke
        await room.broadcast({
            "type":     "stroke_start",
            "id":       client_id,
            "strokeId": stroke_id,
            "color":    stroke["color"],
            "width":    stroke["width"],
            "tool":     stroke["tool"],
            "point":    msg.get("point"),
        }, exclude=client_id)

    elif msg_type == "stroke_point":
        stroke_id = str(msg.get("strokeId", ""))
        point     = msg.get("point")
        stroke    = room.active_strokes.get(stroke_id)
        if stroke is not None and point is not None:
            stroke["points"].append(point)
        await room.broadcast({
            "type": "stroke_point", "id": client_id,
            "strokeId": stroke_id, "point": point,
        }, exclude=client_id)

    elif msg_type == "stroke_end":
        stroke_id = str(msg.get("strokeId", ""))
        stroke    = room.active_strokes.pop(stroke_id, None)
        if stroke and stroke["points"]:
            room.strokes.append(stroke)
        await room.broadcast(
            {"type": "stroke_end", "id": client_id, "strokeId": stroke_id},
            exclude=client_id,
        )

    elif msg_type == "cursor":
        await room.broadcast({
            "type":  "cursor",
            "id":    client_id,
            "x":     msg.get("x"),
            "y":     msg.get("y"),
            "name":  client.name,
            "color": client.color,
        }, exclude=client_id)

    elif msg_type == "clear":
        room.strokes.clear()
        room.active_strokes.clear()
        await room.broadcast({"type": "clear", "id": client_id})

    elif msg_type == "undo":
        stroke_id    = str(msg.get("strokeId", ""))
        room.strokes = [s for s in room.strokes if s["strokeId"] != stroke_id]
        room.active_strokes.pop(stroke_id, None)
        await room.broadcast(
            {"type": "undo", "id": client_id, "strokeId": stroke_id},
            exclude=client_id,
        )

    elif msg_type == "redo":
        stroke = msg.get("stroke")
        if stroke and isinstance(stroke, dict):
            room.strokes.append(stroke)
            await room.broadcast(
                {"type": "redo", "id": client_id, "stroke": stroke},
                exclude=client_id,
            )

    elif msg_type == "set_name":
        new_name = str(msg.get("name", "")).strip()[:24]
        if new_name:
            client.name = new_name
            await room.broadcast(
                {"type": "user_renamed", "id": client_id, "name": new_name},
                exclude=client_id,
            )

    # Unknown types are silently ignored so older/newer clients degrade gracefully.
