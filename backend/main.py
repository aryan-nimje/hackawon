"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import get_settings
from rate_limit import limiter
from routers import alerts, health, incidents, plan, runs, scenario


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


@app.middleware("http")
async def add_mock_header(request: Request, call_next):
    response = await call_next(request)
    if get_settings().effective_mock_mode:
        response.headers["X-Mock-Mode"] = "true"
    return response
