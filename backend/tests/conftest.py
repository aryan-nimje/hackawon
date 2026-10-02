import pytest


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("LLM_API_KEY", "")
    from config import get_settings

    get_settings.cache_clear()
