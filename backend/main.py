"""FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import get_settings
from db import init_db
from rate_limit import limiter
from routers import alerts, bus, health, hospitals, incidents, layers, plan, reports, routes, runs, scenario, signals, sim
from services import citizen_db
from services.resync import resync_loop
from services.citizen_sync import poll_loop
from services.reports import report_store
from services.signals import ingest as signal_ingest
from services.signals.store import signal_store
from services.store import run_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    try:
        if init_db():
            run_store.load_from_db()
            report_store.load_from_db()
            signal_store.load_from_db()
    except Exception as exc:  # unreachable database must not stop the API: carry on in memory
        logging.getLogger(__name__).error(
            "Database unreachable at startup (%s). Continuing WITHOUT persistence: runs, incidents and reports are "
            "kept in memory only. Check DATABASE_URL and your internet/DNS connection, then restart.",
            type(exc).__name__,
        )
    poller = None
    sachet_poller = None
    if settings.sachet_enabled and settings.sachet_poll_seconds > 0 and settings.sachet_feeds:
        sachet_poller = asyncio.create_task(signal_ingest.poll_loop())
    evidence_pollers = []
    if settings.open_meteo_enabled and settings.open_meteo_poll_seconds > 0:
        evidence_pollers.append(asyncio.create_task(signal_ingest.open_meteo_poll_loop()))
    if settings.gdelt_enabled and settings.gdelt_poll_seconds > 0:
        evidence_pollers.append(asyncio.create_task(signal_ingest.gdelt_poll_loop()))
    if citizen_db.configured() and settings.citizen_poll_seconds > 0:
        poller = asyncio.create_task(poll_loop())
    if settings.db_resync_seconds > 0:  # keeps memory (and so every dashboard) in line with the database
        evidence_pollers.append(asyncio.create_task(resync_loop()))
    yield
    if poller:
        poller.cancel()
    if sachet_poller:
        sachet_poller.cancel()
    for task in evidence_pollers:
        task.cancel()


app = FastAPI(
    title="Disaster Relief Coordinator API",
    description="AI multi-agent decision-support for disaster response planning",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_origin_regex=settings.allowed_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(scenario.router)
app.include_router(incidents.router)
app.include_router(runs.router)
app.include_router(plan.router)
app.include_router(alerts.router)
app.include_router(bus.router)
app.include_router(sim.router)
app.include_router(reports.router)
app.include_router(layers.router)
app.include_router(hospitals.router)
app.include_router(signals.router)
app.include_router(routes.router)


class MockHeaderMiddleware:
    """Pure-ASGI middleware (BaseHTTPMiddleware interferes with long-lived SSE streams)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and get_settings().effective_mock_mode:
                headers = list(message.get("headers", []))
                headers.append((b"x-mock-mode", b"true"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(MockHeaderMiddleware)


if __name__ == "__main__":
    # ONE worker: the SSE bus and all state are in-process memory.
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8742")), workers=1)  # 8742 = frontend default
