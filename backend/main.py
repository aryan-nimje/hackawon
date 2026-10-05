"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import get_settings
from rate_limit import limiter
from routers import alerts, bus, health, incidents, plan, reports, runs, scenario, sim


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    yield


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

    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1)
