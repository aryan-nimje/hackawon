import pytest


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SACHET_POLL_SECONDS", "0")  # tests never poll the real SACHET feed
    monkeypatch.setenv("OPEN_METEO_POLL_SECONDS", "0")  # ... nor Open-Meteo
    monkeypatch.setenv("GDELT_POLL_SECONDS", "0")  # ... nor GDELT
    monkeypatch.setenv("GDELT_MIN_INTERVAL_SECONDS", "0")  # and never wait between GDELT queries
    from config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_signal_store():
    from services.signals import ingest
    from services.signals.store import signal_store

    signal_store.clear()
    ingest._gdelt_cooldown_until = 0.0  # a 429 in one test must not silence GDELT in the next
    for key in ingest.last_refresh:
        ingest.last_refresh[key] = None
    yield
    signal_store.clear()
