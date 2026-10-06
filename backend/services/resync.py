"""Re-read the database into the in-memory stores.

The stores are write-through caches filled at startup, so anything deleted from the database afterwards (by hand, by
another tool, by a wipe) would keep living in memory and keep being served to every client. This brings memory back in
line: once on every simulation reset / scenario start, and then every DB_RESYNC_SECONDS in the background.

It is a no-op, reported as `synced: false`, when no database is configured or it cannot be read: memory is never
emptied just because the database is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from config import get_settings
from services.bus import bus
from services.city import effective_city_slug
from services.reports import report_store
from services.signals.store import signal_store
from services.store import run_store

log = logging.getLogger("resync")

BACKGROUND_GRACE_S = 30.0  # a run this young may simply not have reached the database yet


def resync_from_db(publish: bool = False, grace_s: float = 0.0) -> Dict[str, Any]:
    """Make memory match the database. With `publish`, clients are told when a run they may be showing disappeared
    (the same `sim.reset` event the dashboards already understand: forget those runs, follow `run_id`)."""
    try:
        runs = run_store.sync_from_db(grace_s=grace_s)
        reports = report_store.sync_from_db()
        signals = signal_store.sync_from_db()
    except Exception:
        log.exception("could not re-sync memory with the database")
        return {"synced": False}
    if runs is None or reports is None or signals is None:
        return {"synced": False}
    dropped_ids = runs.pop("dropped_run_ids", [])
    out = {"synced": True, **runs, "reports_dropped": reports, "signals_dropped": signals}
    if any(v for k, v in out.items() if k != "synced"):
        log.info("memory re-synced with the database: %s", out)
    if publish and dropped_ids:
        active = run_store.get_active_run()
        bus.publish("sim.reset", {
            "removed_run_ids": dropped_ids,
            "run_id": active.id if active else None,
            "city": effective_city_slug(active.city) if active else None,
        })
    return out


async def resync_loop() -> None:
    """Background keeper: memory follows the database. Cancelled on shutdown."""
    while True:
        seconds = get_settings().db_resync_seconds
        await asyncio.sleep(max(5, seconds))
        try:
            resync_from_db(publish=True, grace_s=BACKGROUND_GRACE_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background re-sync failed")
