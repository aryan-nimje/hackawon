"""Health check endpoint."""

from fastapi import APIRouter

from config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    settings = get_settings()
    return {
        "status": "ok",
        "mock_mode": settings.effective_mock_mode,
        "llm_provider": settings.llm_provider if not settings.effective_mock_mode else "mock",
    }
