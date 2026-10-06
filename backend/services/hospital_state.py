"""Live hospital capacity, as published by the simulator.

The simulator owns the clock and the patient ledger: every patient (walk-in or incident) has a length
of stay, keeps a bed until it ends, and the bed is freed again at discharge. It publishes the resulting
census inside every `POST /sim/world` as `hospital_load` (plus `admitted_incident_ids`).

This store keeps the latest census so the Medical Agent plans against the same occupancy the maps show,
instead of the static `beds_available` of the city layer. When no census applies (no simulator running,
or it belongs to another run) every helper falls back to the static behaviour, so the agents still work
on their own.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Set

# Simulator status -> the status vocabulary the Medical Agent uses.
_STATUS = {"open": "operational", "full": "full", "offline": "offline"}


def _num(v: Any, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


class HospitalCapacityStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._load: Dict[str, Dict[str, Any]] = {}
        self._admitted: Set[str] = set()
        self.run_id: Optional[str] = None
        self.sim_time_s: float = 0.0

    # -- ingest ------------------------------------------------------------
    def update_from_world(self, world: Dict[str, Any]) -> None:
        """Take the census out of a world snapshot. A snapshot without one clears the live view."""
        raw = world.get("hospital_load")
        if not isinstance(raw, dict) or not raw:
            self.reset()
            return
        load: Dict[str, Dict[str, Any]] = {}
        for hid, h in raw.items():
            if not isinstance(h, dict):
                continue
            cap = max(0, int(_num(h.get("capacity"))))
            occupied = min(cap, max(0, int(_num(h.get("occupied")))))
            status = h.get("status") if h.get("status") in _STATUS else "open"
            load[str(hid)] = {**h, "capacity": cap, "occupied": occupied, "free": cap - occupied, "status": status}
        self._load = load
        ids = world.get("admitted_incident_ids")
        self._admitted = {str(i) for i in ids} if isinstance(ids, list) else set()
        self.run_id = world.get("run_id")
        self.sim_time_s = _num(world.get("sim_time_s"))

    # -- queries -----------------------------------------------------------
    def is_live(self, run_id: Optional[str] = None) -> bool:
        """A census applies to `run_id` when there is one and it is not tagged with a different run."""
        if not self._load:
            return False
        return run_id is None or self.run_id is None or self.run_id == run_id

    def get(self, hospital_id: str) -> Optional[Dict[str, Any]]:
        return self._load.get(hospital_id)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._load.items()}

    def overlay(self, hospitals: List[Dict[str, Any]], run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Hospitals in the agents' shape, with live free beds and status when a census applies.

        `beds_available` becomes the number of beds free *now*; the agent still subtracts the beds it has
        reserved for patients who are assigned but not admitted yet.
        """
        if not self.is_live(run_id):
            return hospitals
        out: List[Dict[str, Any]] = []
        for h in hospitals:
            live = self._load.get(h["id"])
            if live is None:
                out.append(h)
                continue
            out.append({
                **h,
                "beds_available": live["free"],
                "status": _STATUS[live["status"]],
                "occupied": live["occupied"],
                "capacity": live["capacity"],
            })
        return out

    def is_admitted(self, incident_id: str) -> bool:
        return incident_id in self._admitted

    def unadmitted(self, run_id: Optional[str], assignments: Iterable[Any], released: Optional[Set[str]] = None) -> List[Any]:
        """Assignments that still hold a reservation: the patient has not been admitted yet.

        Once admitted, the patient is part of the census occupancy (and later discharged), so counting the
        assignment as well would take the bed twice. `released` ids (e.g. rejected plan items) hold nothing.
        Without a live census every assignment counts, as before.
        """
        released = released or set()
        live = self.is_live(run_id)
        return [
            a for a in assignments
            if a.incident_id not in released and not (live and a.incident_id in self._admitted)
        ]

    def reserved(self, run_id: Optional[str], assignments: Iterable[Any], released: Optional[Set[str]] = None) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for a in self.unadmitted(run_id, assignments, released):
            counts[a.hospital_id] = counts.get(a.hospital_id, 0) + 1
        return counts


hospital_store = HospitalCapacityStore()
