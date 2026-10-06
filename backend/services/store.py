"""In-memory run storage."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from services import persistence
from state import Incident, RunState


class RunStore:
    def __init__(self) -> None:
        self._runs: Dict[str, RunState] = {}
        self._all_incidents: Dict[str, Incident] = {}
        # The run every client follows. Starting a scenario makes its run active; older runs stay
        # stored (hidden from default listings, never deleted).
        self.active_run_id: Optional[str] = None
        # One lock per run: the full pipeline and every incremental plan update take it, so they never interleave.
        self._locks: Dict[str, asyncio.Lock] = {}
        # Runs removed by "Reset simulation". A pipeline that was still running for one of them must not bring it back.
        self._removed: Set[str] = set()

    def load_from_db(self) -> int:
        """Restore runs, incidents and the active run after a restart. Returns the number of runs loaded."""
        runs, active_id = persistence.load_runs()
        for data in runs:
            run = RunState.model_validate(data)
            self._runs[run.id] = run
        for data in persistence.load_incidents():
            inc = Incident.model_validate(data)
            self._all_incidents[inc.id] = inc
        if active_id in self._runs:
            self.active_run_id = active_id
        return len(runs)

    def sync_from_db(self, grace_s: float = 0.0) -> Optional[Dict[str, Any]]:
        """Make memory match the database: forget runs / incidents that are no longer there, pick up ones that are.

        Memory is otherwise only filled at startup, so rows deleted from the database by hand would live on here and
        be replayed to every client. Objects present in both are kept as they are (a pipeline may be holding them).
        A run dropped here can never be written back by a pipeline still working on it. Runs younger than `grace_s`
        seconds are kept even when the database does not have them yet (a save may still be on its way). Returns None,
        touching nothing, when the database is not configured or cannot be read.
        """
        loaded = persistence.load_runs_strict()
        incidents = persistence.load_incidents_strict()
        if loaded is None or incidents is None:
            return None
        rows, active_id = loaded
        db_runs: Dict[str, RunState] = {}
        for data in rows:
            try:
                run = RunState.model_validate(data)
            except Exception:
                continue
            db_runs[run.id] = run
        now = datetime.utcnow()
        dropped = [
            rid for rid, run in self._runs.items()
            if rid not in db_runs and (now - run.created_at).total_seconds() >= grace_s
        ]
        for rid in dropped:
            self._removed.add(rid)
            self._runs.pop(rid, None)
            self._locks.pop(rid, None)
        added = 0
        for rid, run in db_runs.items():
            if rid not in self._runs and rid not in self._removed:
                self._runs[rid] = run
                added += 1
        if self.active_run_id not in self._runs:
            self.active_run_id = active_id if active_id in self._runs else None

        db_incidents: Dict[str, Incident] = {}
        for data in incidents:
            try:
                inc = Incident.model_validate(data)
            except Exception:
                continue
            db_incidents[inc.id] = inc
        gone = [i for i in self._all_incidents if i not in db_incidents]
        for i in gone:
            del self._all_incidents[i]
        for i, inc in db_incidents.items():
            self._all_incidents.setdefault(i, inc)
        return {"runs_dropped": len(dropped), "runs_added": added, "incidents_dropped": len(gone), "dropped_run_ids": dropped}

    def create_run(self, run: RunState) -> RunState:
        self._runs[run.id] = run
        persistence.save_run(run)
        return run

    def get_run(self, run_id: str) -> Optional[RunState]:
        return self._runs.get(run_id)

    def run_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = self._locks[run_id] = asyncio.Lock()
        return lock

    def update_run(self, run: RunState) -> RunState:
        if run.id in self._removed:  # removed by a simulation reset: late writes from a still-running pipeline are dropped
            return run
        self._runs[run.id] = run
        persistence.save_run(run)
        return run

    # -- simulation reset -------------------------------------------------------------------------
    def all_runs(self) -> List[RunState]:
        return list(self._runs.values())

    def is_removed(self, run_id: str) -> bool:
        return run_id in self._removed

    def remove_run(self, run_id: str) -> None:
        """Forget a run (memory and database). Only the simulation reset calls this."""
        self._removed.add(run_id)
        self._runs.pop(run_id, None)
        self._locks.pop(run_id, None)
        persistence.delete_run(run_id)
        if self.active_run_id == run_id:
            self.active_run_id = None

    def delete_incidents(self, ids: Iterable[str]) -> None:
        gone = [i for i in ids if i in self._all_incidents]
        for i in gone:
            del self._all_incidents[i]
        persistence.delete_incidents(gone)

    def clear_active(self) -> None:
        self.active_run_id = None
        persistence.clear_active_run()

    def list_incidents(self) -> List[Incident]:
        return list(self._all_incidents.values())

    def upsert_incident(self, incident: Incident) -> None:
        self._all_incidents[incident.id] = incident
        persistence.save_incident(incident)

    def set_active(self, run_id: str) -> None:
        if run_id in self._removed:
            return
        self.active_run_id = run_id
        persistence.set_active_run(run_id)

    def get_active_run(self) -> Optional[RunState]:
        return self._runs.get(self.active_run_id) if self.active_run_id else None

    def get_latest_run(self) -> Optional[RunState]:
        if not self._runs:
            return None
        return max(self._runs.values(), key=lambda r: r.created_at)


run_store = RunStore()
