"""Common signals store: in-memory dict with write-through persistence (same pattern as RunStore).

Every external evidence source writes here through `upsert_many`; the verification step reads from here later.
`DATABASE_URL` unset = memory only. A database error is logged, never raised (see persistence.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from config import get_settings
from services import persistence
from services.signals import geo
from services.signals.models import Signal, SignalKind, SignalSource, SignalStatus

log = logging.getLogger("signals.store")

_WITHDRAWN_BY = {"cancel": SignalStatus.CANCELLED, "update": SignalStatus.SUPERSEDED}


class SignalStore:
    def __init__(self) -> None:
        self._signals: Dict[str, Signal] = {}

    # -- lifecycle ------------------------------------------------------------------------------------
    def load_from_db(self) -> int:
        n = 0
        for data in persistence.load_signals():
            try:
                sig = Signal.model_validate(data)
            except Exception:
                log.warning("skipping unreadable signal row", exc_info=True)
                continue
            self._signals[sig.id] = sig
            n += 1
        return n

    def sync_from_db(self) -> Optional[int]:
        """Make memory match the database (see RunStore.sync_from_db). Returns how many signals were dropped, or None
        when the database is not configured or cannot be read."""
        rows = persistence.load_signals_strict()
        if rows is None:
            return None
        db: Dict[str, Signal] = {}
        for data in rows:
            try:
                sig = Signal.model_validate(data)
            except Exception:
                continue
            db[sig.id] = sig
        gone = [i for i in self._signals if i not in db]
        for i in gone:
            del self._signals[i]
        for i, sig in db.items():
            self._signals.setdefault(i, sig)
        return len(gone)

    def clear(self) -> None:
        self._signals.clear()

    # -- writes ---------------------------------------------------------------------------------------
    def upsert_many(self, signals: Iterable[Signal]) -> Tuple[int, int]:
        """Insert or update by id. Returns (added, updated).

        Lifecycle: a cancel / update withdraws the signals it references (also when it arrives before them), and
        re-ingesting a withdrawn signal never brings it back to active.
        """
        changed: Dict[str, Signal] = {}
        added = updated = 0
        for sig in signals:
            old = self._signals.get(sig.id)
            if old is not None and old.status != SignalStatus.ACTIVE and sig.status == SignalStatus.ACTIVE:
                sig = sig.model_copy(update={"status": old.status})
            self._signals[sig.id] = sig
            changed[sig.id] = sig
            if old is None:
                added += 1
            else:
                updated += 1
        for sig in list(changed.values()):
            for target in self._apply_references(sig):
                changed[target.id] = target
        persistence.save_signals(list(changed.values()))
        return added, updated

    def _apply_references(self, sig: Signal) -> List[Signal]:
        """Withdraw what `sig` (cancel / update) points at; and withdraw `sig` itself if a stored cancel / update
        already points at it."""
        touched: List[Signal] = []
        status = _WITHDRAWN_BY.get(sig.msg_type)
        if status is not None:
            for ref in sig.references:
                target = self._signals.get(ref)
                if target is not None and target.id != sig.id and target.status == SignalStatus.ACTIVE:
                    target = target.model_copy(update={"status": status})
                    self._signals[target.id] = target
                    touched.append(target)
        elif sig.status == SignalStatus.ACTIVE:
            for other in self._signals.values():
                if other.id != sig.id and sig.id in other.references and other.msg_type in _WITHDRAWN_BY:
                    self._signals[sig.id] = sig = sig.model_copy(update={"status": _WITHDRAWN_BY[other.msg_type]})
                    touched.append(sig)
                    break
        return touched

    def expire(self, ids: Iterable[str], at: Optional[datetime] = None) -> int:
        """End the lifetime of signals that are no longer observed (e.g. a weather hazard that has passed) without
        withdrawing them: expires_at becomes `at`, status stays active, so the same id can be written again later.
        Returns how many changed."""
        at = at or datetime.utcnow()
        changed: List[Signal] = []
        for sid in ids:
            sig = self._signals.get(sid)
            if sig is not None and sig.status == SignalStatus.ACTIVE and (sig.expires_at is None or sig.expires_at > at):
                sig = sig.model_copy(update={"expires_at": at})
                self._signals[sid] = sig
                changed.append(sig)
        persistence.save_signals(changed)
        return len(changed)

    def remove_simulated(self) -> List[str]:
        """Drop every signal made by the Simulation app (metadata.simulated). Real signals are never touched."""
        gone = [i for i, s in self._signals.items() if (s.metadata or {}).get("simulated")]
        for i in gone:
            del self._signals[i]
        persistence.delete_signals(gone)
        return gone

    def prune(self, now: Optional[datetime] = None) -> int:
        """Drop signals that expired more than SIGNALS_RETENTION_HOURS ago."""
        now = now or datetime.utcnow()
        cutoff = now - timedelta(hours=get_settings().signals_retention_hours)
        gone = [i for i, s in self._signals.items() if (s.expires_at or s.fetched_at) < cutoff]
        for i in gone:
            del self._signals[i]
        persistence.delete_signals(gone)
        return len(gone)

    # -- reads ----------------------------------------------------------------------------------------
    def get(self, signal_id: str) -> Optional[Signal]:
        return self._signals.get(signal_id)

    def known_urls(self, source: SignalSource) -> set:
        return {s.source_url for s in self._signals.values() if s.source == source and s.source_url}

    def list(
        self,
        *,
        source: Optional[SignalSource] = None,
        kind: Optional[SignalKind] = None,
        bbox: Optional[List[float]] = None,
        include: Optional[callable] = None,
    ) -> List[Signal]:
        """Newest first. `bbox` [s, w, n, e] keeps signals whose area overlaps it (signals with no geometry are
        left out). `include` is an extra predicate."""
        out = []
        for s in self._signals.values():
            if source and s.source != source:
                continue
            if kind and s.kind != kind:
                continue
            if bbox is not None and not (s.bbox and geo.bbox_overlaps(s.bbox, bbox)):
                continue
            if include and not include(s):
                continue
            out.append(s)
        out.sort(key=lambda s: s.issued_at or s.fetched_at, reverse=True)
        return out

    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for s in self._signals.values():
            c[s.source.value] = c.get(s.source.value, 0) + 1
        return c


signal_store = SignalStore()
