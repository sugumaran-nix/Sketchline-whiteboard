# Whiteboard Backend (FastAPI)

Real-time collaborative whiteboard backend — FastAPI + WebSockets.

## Deploy to Railway (recommended)

1. Push this folder's contents (`main.py`, `requirements.txt`, `Dockerfile`, `railway.json`) to a GitHub repo
2. Go to [railway.com](https://railway.com) → New Project → Deploy from GitHub repo → select the repo
3. Railway auto-detects the Dockerfile and deploys automatically
4. Go to **Settings → Networking → Generate Domain**
5. Test: `curl https://your-domain.up.railway.app/` → `{"status":"ok"}`
6. Set your Vercel frontend env var: `NEXT_PUBLIC_WS_URL=wss://your-domain.up.railway.app`

## Endpoints

- `GET /` — health check
- `GET /rooms/{room_id}/stats` — room info
- `WS /ws/{room_id}?name=YourName` — WebSocket connection

## Local development

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8080 --reload
```
