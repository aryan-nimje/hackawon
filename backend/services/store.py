"""In-memory run storage."""

from __future__ import annotations

from typing import Dict, List, Optional

from state import Incident, RunState


class RunStore:
    def __init__(self) -> None:
        self._runs: Dict[str, RunState] = {}
        self._all_incidents: Dict[str, Incident] = {}

    def create_run(self, run: RunState) -> RunState:
        self._runs[run.id] = run
        return run

    def get_run(self, run_id: str) -> Optional[RunState]:
        return self._runs.get(run_id)

    def update_run(self, run: RunState) -> RunState:
        self._runs[run.id] = run
        return run

    def list_incidents(self) -> List[Incident]:
        return list(self._all_incidents.values())

    def upsert_incident(self, incident: Incident) -> None:
        self._all_incidents[incident.id] = incident

    def get_latest_run(self) -> Optional[RunState]:
        if not self._runs:
            return None
        return max(self._runs.values(), key=lambda r: r.created_at)


run_store = RunStore()
