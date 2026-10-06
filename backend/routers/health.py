"""Health check endpoint."""

from fastapi import APIRouter

from config import get_settings
from db import ping
from services import citizen_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    settings = get_settings()
    return {
        "status": "ok",
        "mock_mode": settings.effective_mock_mode,
        "llm_provider": settings.llm_provider if not settings.effective_mock_mode else "mock",
        # None = not configured (in-memory), True = reachable, False = configured but unreachable
        "database": ping(),
        "citizen_database": citizen_db.ping(),
    }
