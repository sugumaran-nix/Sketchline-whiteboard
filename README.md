# Sketchline — Real-time Collaborative Whiteboard

A multiplayer drawing canvas where every stroke syncs across all connected browsers in real time, built from scratch with FastAPI WebSockets on the backend and Next.js + HTML5 Canvas on the frontend. No external whiteboard library — the drawing engine and sync logic are written directly.

```
project-root/
├── README.md
├── backend/
│   ├── main.py           ← FastAPI app + WebSocket room logic
│   ├── requirements.txt
│   ├── Dockerfile        ← reads $PORT dynamically (Railway-compatible)
│   ├── README.md         ← HF Spaces metadata header
│   └── test_live.py      ← integration tests against a live server
└── frontend/
    ├── app/
    │   ├── layout.tsx
    │   ├── globals.css
    │   ├── page.tsx                    ← landing / create-or-join
    │   └── board/[roomId]/page.tsx     ← the live whiteboard
    ├── components/
    │   ├── Canvas.tsx      ← drawing engine + imperative replay API
    │   ├── Toolbar.tsx     ← pen / eraser / color / brush / clear
    │   ├── PresenceBar.tsx ← header with room code, avatars, status
    │   ├── CursorLayer.tsx ← floating cursor dots for remote users
    │   ├── ThemeToggle.tsx ← light / dark toggle (no-flash)
    │   └── NameModal.tsx   ← display-name prompt before joining
    ├── lib/
    │   ├── types.ts        ← wire-protocol TypeScript types
    │   └── config.ts       ← WS URL helper + room-id generator
    ├── .env.local.example
    ├── next.config.mjs
    ├── tailwind.config.ts
    └── package.json
```

---

## Features

| Feature | Details |
|---|---|
| Room-based sessions | Every board has a shareable URL (`/board/<room-id>`); anyone with the link joins the same live canvas |
| Real-time stroke sync | Strokes broadcast to all room members via WebSocket as they are drawn; typical latency is well under 100 ms on the same continent |
| Live cursor presence | Each user's cursor appears for every other user, with a name label; broadcast throttled to ~20 updates/sec client-side |
| Toolset | Pen, eraser, color picker (6 swatches + freeform), brush-size slider, clear-board (two-click confirmation) |
| Reconnection / state replay | On every (re)connect the server sends a full stroke history snapshot so late-joining or reconnecting clients never see a blank canvas |
| Active user count | Avatar strip + online count shown in the top bar; updates live as people join and leave |
| Dark mode | CSS-variable design token system; preference saved to `localStorage`, no flash on load |

---

## Architecture

### Backend (`backend/main.py`)

```
Browser A ──ws──┐
Browser B ──ws──┤  FastAPI /ws/{room_id}  ──→  Room dict (in-memory)
Browser C ──ws──┘                                  ├── clients: [WebSocket, ...]
                                                   └── strokes: [Stroke, ...]
```

One FastAPI process holds all state in a plain Python dict:

```python
rooms: Dict[str, Room]   # room_id → Room
```

Each `Room` holds:
- **`clients`** — currently connected `ConnectedClient` objects (id, name, color, websocket)
- **`strokes`** — completed strokes, used to replay the full board to any client that joins or rejoins
- **`active_strokes`** — strokes currently mid-draw, kept so a resize or late joiner during a long drag doesn't lose the in-flight stroke

**WebSocket endpoint:** `GET /ws/{room_id}?name=<display-name>`

On connect the server unicasts `init` (full snapshot) to the new client, then broadcasts `user_joined` + `user_count` to everyone else. Every subsequent message the client sends is relayed to all _other_ clients in the room (the sender already has local/optimistic state). On disconnect the server cleans up and broadcasts `user_left` + `user_count`.

**Wire protocol** (JSON, one message per send):

| Direction | Type | Payload |
|---|---|---|
| C→S | `stroke_start` | strokeId, color, width, tool, point |
| C→S | `stroke_point` | strokeId, point |
| C→S | `stroke_end` | strokeId |
| C→S | `cursor` | x, y (normalized 0–1) |
| C→S | `clear` | — |
| C→S | `set_name` | name |
| S→C | `init` | clientId, color, name, strokes[], users[], userCount |
| S→C | `user_joined` | user {id, name, color} |
| S→C | `user_left` | id |
| S→C | `user_count` | count |
| S→C | `stroke_start/point/end` | id (sender) + stroke data |
| S→C | `clear` | id (sender) |
| S→C | `cursor` | id, x, y, name, color |

### Frontend (`frontend/`)

```
page.tsx (BoardPage)
 ├── PresenceBar    ← header: room code, share button, avatars, status
 ├── Toolbar        ← docked to left side
 ├── Canvas         ← HTML5 Canvas, pointer events → stroke events
 │    └── (imperative ref API: redrawAll / applyRemote* / clearCanvas)
 └── CursorLayer    ← absolutely-positioned overlay, normalized coords
```

**Coordinate normalization:** All x/y positions and brush widths that cross the WebSocket are normalized to 0–1 fractions of the canvas dimensions. This means two collaborators with different window sizes still see strokes land in the same proportional position. The canvas is redrawn from the full stroke history on every resize, so nothing is lost when a window is resized.

**Reconnect logic:** The frontend uses a simple capped-exponential backoff (500 ms → 1 s → 2 s → 4 s → 8 s, then stays at 8 s). On every reconnect the server replays the full board history via `init`, so the user's canvas automatically catches up.

**Cursor staleness:** Remote cursors that haven't sent an update in 4 seconds are silently removed from the overlay — this catches tabs killed without a clean WebSocket close.

---

## Deployment

### 1. Backend → Railway

Railway runs the Dockerfile directly and exposes it at a stable HTTPS/WSS domain — free tier available, and unlike some serverless platforms, processes stay warm rather than restarting on every idle period, which matters for a stateful in-memory WebSocket server like this one.

**Step-by-step:**

1. Create a free account at [railway.com](https://railway.com).

2. Push the contents of the `backend/` folder (`main.py`, `requirements.txt`, `Dockerfile`, `railway.json`) to a GitHub repo.

3. In Railway: **New Project → Deploy from GitHub repo** → select the repo. Railway auto-detects the `Dockerfile` and `railway.json` and deploys without further configuration.

4. Go to **Settings → Networking → Generate Domain** to get a public HTTPS/WSS domain.

5. Test it:
   ```bash
   curl https://your-domain.up.railway.app/
   # → {"status":"ok","service":"whiteboard-backend","rooms":0}
   ```

> **Note on WebSockets and reverse proxies in general:** most hosting platforms route WebSocket traffic through a reverse proxy with an idle-connection timeout (often 30-60s without traffic). This backend's ping/pong keepalive (see `main.py`) sends a heartbeat every 20 seconds specifically to keep connections alive through this kind of proxy, and the frontend's reconnect logic handles a drop gracefully regardless of the exact cause.

---

### 2. Frontend → Vercel

1. Push the entire `frontend/` folder to a GitHub/GitLab repo (or a monorepo with the root pointing to `frontend/` as the project root).

2. Go to [vercel.com/new](https://vercel.com/new), import the repo.

3. In **Project Settings → Environment Variables**, add:
   ```
   NEXT_PUBLIC_WS_URL = wss://your-domain.up.railway.app
   ```
   Use `wss://` (WebSocket Secure), not `https://`.

4. Set the **Framework Preset** to **Next.js** and the **Root Directory** to `frontend/` if you're using a monorepo layout.

5. Click **Deploy**. Vercel will run `npm run build` and serve the result on a `*.vercel.app` domain.

6. Open `https://your-app.vercel.app` — click "Create a new board", share the URL with someone else, and draw.

---

### Running locally (quick test)

```bash
# Terminal 1 — backend
cd backend
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 7860 --reload

# Terminal 2 — frontend
cd frontend
cp .env.local.example .env.local
# .env.local already has: NEXT_PUBLIC_WS_URL=ws://127.0.0.1:7860
npm install
npm run dev
```

Open `http://localhost:3000` in two browser tabs and draw in one — you'll see strokes appear in the other.

**Running the integration tests** (backend must be running on port 8125 or edit the URL in `test_live.py`):
```bash
cd backend
python3 test_live.py
```

---

## Limitations

These are honest tradeoffs made deliberately for a demo. A production system would need to address them.

### Persistence
Board history lives only in the FastAPI process's memory. If the backend process restarts for any reason, all room history is permanently lost. A production system would use a database (Postgres, Redis, etc.) to persist stroke history.

### Scalability
The in-memory `rooms` dict is local to one process. Two backend processes (e.g. multiple Gunicorn workers or multiple Spaces replicas) would each have their own separate dict, so clients would be silently split across different state islands. Fixing this requires a shared message bus (Redis Pub/Sub is the conventional choice) so every process can broadcast to every client regardless of which process they connected to.

### Conflict resolution
Concurrent strokes from multiple users are simply appended in the order the server receives them. There is no operational transform (OT) or CRDT system — if two users draw simultaneously, both strokes are kept, but their relative order in the history may differ between what each user sees locally (optimistic) versus what a third user's replay sees. For drawing this is usually imperceptible. For a text editor or structured data it would be a significant problem.

### Stroke memory
There is no stroke limit per room. A room with many users drawing for a long time will accumulate an unbounded list of stroke events in memory, and every new joiner receives the full list. In practice, add a `MAX_STROKES_PER_ROOM` cap and a periodic GC in production.

### Security
There is no authentication, no rate limiting, and no input validation beyond JSON parsing. Room IDs are the only access control — anyone who knows (or guesses) a room ID can join and draw on that board. The backend's CORS policy is fully open (`*`). Tighten all of this before exposing to untrusted users.

### WebSocket idle timeouts
Most hosting platforms route WebSocket traffic through a reverse proxy that closes idle connections after some timeout (commonly 30-60s without traffic). The backend's ping/pong keepalive (every 20s) is designed to prevent this in normal operation, but a client that was already disconnected for another reason (e.g. a mobile network drop) may briefly see "Disconnected" in the status indicator before auto-reconnecting and reloading board state.

### Aspect-ratio drift in coordinates
Coordinates are normalized as fractions of canvas width and height independently. On two clients with very different aspect ratios (e.g. one landscape, one portrait), a diagonal stroke will look slightly different in angle. True aspect-ratio–correct normalization would require agreeing on a fixed logical canvas size and letter-boxing, which adds frontend complexity for minimal real-world gain in a drawing app.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI 0.115, Uvicorn, websockets |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS |
| Drawing | HTML5 Canvas, Pointer Events API — no library |
| Real-time | Native FastAPI WebSocket support — no Socket.io, no Node.js |
| Deployment | Hugging Face Spaces (Docker SDK) + Vercel |
