"""Logistics Agent — allocate supplies from mock inventory to zones."""

from __future__ import annotations

import math
from typing import Dict, List

from config import get_settings
from services.data_loader import load_inventory
from services.llm import llm_service
from state import LogisticsOutput, RunState, Severity, SupplyAllocation, Zone

NEEDS_BY_SEVERITY = {
    Severity.CRITICAL: {"food_meals": 500, "water_bottles": 1000, "medicine_kits": 50},
    Severity.HIGH: {"food_meals": 300, "water_bottles": 600, "medicine_kits": 30},
    Severity.MEDIUM: {"food_meals": 150, "water_bottles": 300, "medicine_kits": 15},
    Severity.LOW: {"food_meals": 50, "water_bottles": 100, "medicine_kits": 5},
}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def allocate_supplies(zones: List[Zone], warehouses: List[Dict]) -> List[SupplyAllocation]:
    """Match zone needs against inventory; flag shortages."""
    remaining: Dict[str, Dict[str, int]] = {
        w["id"]: dict(w.get("inventory", {})) for w in warehouses
    }
    allocations: List[SupplyAllocation] = []

    for zone in sorted(zones, key=lambda z: list(Severity).index(z.severity), reverse=True):
        needs = dict(NEEDS_BY_SEVERITY.get(zone.severity, NEEDS_BY_SEVERITY[Severity.LOW]))
        best_wh = min(
            warehouses,
            key=lambda w: _haversine_km(zone.center_lat, zone.center_lng, w["lat"], w["lng"]),
        )
        wh_id = best_wh["id"]
        inv = remaining[wh_id]
        allocated: Dict[str, int] = {}
        shortages: List[str] = []

        for item, qty in needs.items():
            available = inv.get(item, 0)
            take = min(qty, available)
            allocated[item] = take
            inv[item] = available - take
            if take < qty:
                shortages.append(f"Shortage: {item} need {qty}, allocated {take}")

        allocations.append(
            SupplyAllocation(
                zone_id=zone.id,
                warehouse_id=wh_id,
                warehouse_name=best_wh["name"],
                items=allocated,
                shortage_flags=shortages,
                explanation=(
                    f"Allocated from nearest warehouse for {zone.severity.value} zone "
                    f"({len(zone.incident_ids)} incidents)."
                ),
            )
        )
    return allocations


def _remaining_inventory(warehouses: List[Dict], existing: List[SupplyAllocation]) -> List[Dict]:
    """Warehouses with the stock already promised in `existing` allocations taken out."""
    used: Dict[str, Dict[str, int]] = {}
    for a in existing:
        wh = used.setdefault(a.warehouse_id, {})
        for item, qty in a.items.items():
            wh[item] = wh.get(item, 0) + qty
    return [
        {**w, "inventory": {k: max(0, v - used.get(w["id"], {}).get(k, 0)) for k, v in w.get("inventory", {}).items()}}
        for w in warehouses
    ]


async def run_logistics(state: RunState, zone_ids: set[str] | None = None) -> LogisticsOutput:
    """Build supply allocation plan from zone needs and mock inventory.

    `zone_ids`: when given (a new zone joins a live plan), only those zones are allocated, from what is
    left after the existing allocations. The output then holds just the new allocations.
    """
    warehouses = load_inventory()
    if zone_ids is None:
        allocations = allocate_supplies(state.zones, warehouses)
    else:
        remaining = _remaining_inventory(warehouses, state.supply_allocations)
        allocations = allocate_supplies([z for z in state.zones if z.id in zone_ids], remaining) if remaining else []

    if not get_settings().effective_mock_mode:
        for alloc in allocations:
            try:
                alloc.explanation = await llm_service.complete(
                    "Summarize supply allocation in one sentence.",
                    f"Zone {alloc.zone_id}: {alloc.items}, shortages: {alloc.shortage_flags}",
                    max_tokens=60,
                )
            except Exception:
                pass

    return LogisticsOutput(allocations=allocations)
