"""Refresh external evidence (SACHET, Open-Meteo, GDELT) into the common store. Never raises."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import get_settings
from services.signals import common, gdelt, open_meteo, sachet
from services.signals.models import SignalSource, SignalStatus
from services.signals.store import signal_store

log = logging.getLogger("signals.ingest")

_lock = asyncio.Lock()
_open_meteo_lock = asyncio.Lock()
_gdelt_lock = asyncio.Lock()
_gdelt_cooldown_until = 0.0  # time.monotonic() before which GDELT is not asked again (it answered 429)
# most recent summary per source
last_refresh: Dict[str, Optional[Dict[str, Any]]] = {"sachet": None, "open_meteo": None, "gdelt": None}


async def refresh_sachet(client=None) -> Dict[str, Any]:
    """Fetch new SACHET alerts into the store. Overlapping calls (poller + manual) are serialized."""
    settings = get_settings()
    if not settings.sachet_enabled:
        return {"ok": False, "disabled": True}
    async with _lock:
        started = datetime.utcnow()
        result = await sachet.fetch_sachet(signal_store.known_urls(SignalSource.SACHET), client=client)
        added, updated = signal_store.upsert_many(result.signals)
        pruned = signal_store.prune()
        summary = {**result.summary(), "added": added, "updated": updated, "pruned": pruned,
                   "at": started.isoformat(timespec="seconds") + "Z"}
        last_refresh["sachet"] = summary
        log.info("SACHET refresh: %s", {k: v for k, v in summary.items() if k != "errors"})
        return summary


async def refresh_open_meteo(client=None, city: Optional[str] = None) -> Dict[str, Any]:
    """Pull rain / flood / wind / storm / heat conditions for the configured cities (or just `city`, a cached city
    name or slug) into the store. A hazard that a successful fetch no longer shows is expired early; a failed fetch
    leaves what is stored alone. Overlapping calls are serialized."""
    settings = get_settings()
    if not settings.open_meteo_enabled:
        return {"ok": False, "disabled": True}
    async with _open_meteo_lock:
        started = datetime.utcnow()
        cities, city_errors = common.resolve_cities([city] if city else settings.open_meteo_city_names)
        result = await open_meteo.fetch_open_meteo(cities, client=client, now=started)
        for e in city_errors:
            result.err(e)
        added, updated = signal_store.upsert_many(result.signals)
        fresh = {s.id for s in result.signals}
        stale = [
            s.id for s in signal_store.list(source=SignalSource.OPEN_METEO)
            if s.status == SignalStatus.ACTIVE and s.id not in fresh
            and (s.metadata.get("city"), s.metadata.get("endpoint")) in result.scopes_ok
        ]
        expired = signal_store.expire(stale, started)
        pruned = signal_store.prune()
        summary = {**result.summary(), "added": added, "updated": updated, "expired": expired, "pruned": pruned,
                   "at": started.isoformat(timespec="seconds") + "Z"}
        last_refresh["open_meteo"] = summary
        log.info("Open-Meteo refresh: %s", {k: v for k, v in summary.items() if k != "errors"})
        return summary


async def refresh_gdelt(client=None, city: Optional[str] = None) -> Dict[str, Any]:
    """Pull news about hazards in the configured cities (or just `city`) into the store. Articles are idempotent by
    link; they expire after GDELT_TTL_HOURS. Overlapping calls are serialized."""
    settings = get_settings()
    if not settings.gdelt_enabled:
        return {"ok": False, "disabled": True}
    global _gdelt_cooldown_until
    async with _gdelt_lock:
        wait = _gdelt_cooldown_until - time.monotonic()
        if wait > 0:  # GDELT told us to slow down a moment ago: do not poke it again (poller and manual calls alike)
            return {"ok": False, "rate_limited": True, "cooldown_seconds": int(wait) + 1}
        started = datetime.utcnow()
        cities, city_errors = common.resolve_cities([city] if city else settings.gdelt_city_names)
        result = await gdelt.fetch_gdelt(cities, client=client, now=started)
        if result.rate_limited:
            _gdelt_cooldown_until = time.monotonic() + max(0, settings.gdelt_cooldown_seconds)
        for e in city_errors:
            result.err(e)
        added, updated = signal_store.upsert_many(result.signals)
        pruned = signal_store.prune()
        summary = {**result.summary(), "added": added, "updated": updated, "pruned": pruned,
                   "at": started.isoformat(timespec="seconds") + "Z"}
        last_refresh["gdelt"] = summary
        log.info("GDELT refresh: %s", {k: v for k, v in summary.items() if k != "errors"})
        return summary


async def _loop(name: str, refresh: Callable[[], Awaitable[Any]], seconds: Callable[[], int],
                first_delay: Optional[Callable[[], float]] = None) -> None:
    if first_delay is not None:
        await asyncio.sleep(max(0.0, first_delay()))
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s poll failed", name)
        await asyncio.sleep(max(30, seconds()))


async def poll_loop() -> None:
    """Refresh SACHET every SACHET_POLL_SECONDS (first one right away). Cancelled on shutdown."""
    await _loop("SACHET", refresh_sachet, lambda: get_settings().sachet_poll_seconds)


async def open_meteo_poll_loop() -> None:
    """Refresh Open-Meteo every OPEN_METEO_POLL_SECONDS (first one right away). Cancelled on shutdown."""
    await _loop("Open-Meteo", refresh_open_meteo, lambda: get_settings().open_meteo_poll_seconds)


async def gdelt_poll_loop() -> None:
    """Refresh GDELT every GDELT_POLL_SECONDS (first one right away). Cancelled on shutdown."""
    await _loop("GDELT", refresh_gdelt, lambda: get_settings().gdelt_poll_seconds, first_delay=_gdelt_first_delay)


def _gdelt_first_delay() -> float:
    """Wait before the first poll after startup, and longer still when news was fetched recently (it was loaded back
    from the database): a restart must not count as a fresh request against GDELT's rate limit."""
    s = get_settings()
    delay = float(max(0, s.gdelt_startup_delay_seconds))
    newest = max((x.fetched_at for x in signal_store.list(source=SignalSource.GDELT)), default=None)
    if newest is not None:
        delay = max(delay, s.gdelt_poll_seconds - (datetime.utcnow() - newest).total_seconds())
    return delay
