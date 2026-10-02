# AI Multi-Agent Disaster Relief Coordinator

Decision-support web app where a **Supervisor Agent** orchestrates specialist agents to turn fragmented disaster information into a prioritized, human-reviewable **Draft Response Plan**.

> **Simulated data only.** This prototype does not dispatch teams or send real alerts.

## Stack

- **Backend:** Python 3.10+, FastAPI, Pydantic v2, async hand-written supervisor
- **Frontend:** React + Vite + TypeScript + Tailwind CSS + Leaflet
- **Realtime:** Server-Sent Events (SSE)
- **Weather:** Open-Meteo (no key)
- **Routing:** Public OSRM with straight-line fallback

## Quick Start

### 1. Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # edit LLM_API_KEY for live LLM; leave empty for mock mode
uvicorn main:app --reload --host 127.0.0.1 --port 8742
```

Health check: http://127.0.0.1:8742/health

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env       # optional; defaults proxy to backend
npm run dev
```

Open http://127.0.0.1:5173

### 3. Demo

1. Click **Start Scenario** (try 5x or 10x replay speed).
2. Watch the **Agent Activity Timeline** fill via SSE.
3. Review incidents (low-credibility items flagged).
4. Inspect the **Draft Plan** and **Live Map** (routes, flood zones).
5. **Approve** plan items in the Review panel — plan locks only when all are approved.
6. Copy draft alerts (nothing is sent).

## Mock Mode

Runs automatically when `LLM_API_KEY` is missing or `MOCK_MODE=true`. The UI shows a **MOCK MODE** badge.

## Tests

```bash
cd backend
source venv/bin/activate
pytest tests/ -v
```

## Project Structure

```
backend/          FastAPI app, agents, supervisor, mock data
frontend/         React dashboard
SECURITY.md       Secrets and input-handling notes
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /health | Health + mock mode status |
| POST | /scenario/start | Start scenario replay |
| GET | /incidents | List incidents |
| GET | /runs/{id} | Run state |
| GET | /runs/{id}/stream | SSE activity stream |
| GET | /plan/{id} | Draft plan |
| POST | /plan/{id}/review | Approve/edit/reject items |
| GET | /alerts/{id} | Draft alerts |
