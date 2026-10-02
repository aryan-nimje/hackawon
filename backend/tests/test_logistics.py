"""Unit tests for logistics allocation."""

from agents.logistics import allocate_supplies
from state import Severity, Zone


def test_allocate_supplies_flags_shortage():
    zones = [
        Zone(
            id="zone-001",
            name="Critical Zone",
            center_lat=29.76,
            center_lng=-95.37,
            severity=Severity.CRITICAL,
            incident_ids=["a", "b", "c"],
            summary="test",
        )
    ]
    warehouses = [
        {
            "id": "w004",
            "name": "Small Community Center",
            "lat": 29.8011,
            "lng": -95.3967,
            "inventory": {"food_meals": 100, "water_bottles": 200, "medicine_kits": 5},
        }
    ]
    result = allocate_supplies(zones, warehouses)
    assert len(result) == 1
    assert result[0].zone_id == "zone-001"
    assert len(result[0].shortage_flags) > 0


def test_nearest_warehouse_selected():
    zones = [
        Zone(
            id="zone-002",
            name="Heights Zone",
            center_lat=29.80,
            center_lng=-95.40,
            severity=Severity.MEDIUM,
            incident_ids=["x"],
            summary="test",
        )
    ]
    warehouses = [
        {"id": "w001", "name": "Far Warehouse", "lat": 29.60, "lng": -95.15, "inventory": {"food_meals": 5000, "water_bottles": 8000, "medicine_kits": 100}},
        {"id": "w004", "name": "Near Warehouse", "lat": 29.8011, "lng": -95.3967, "inventory": {"food_meals": 800, "water_bottles": 1500, "medicine_kits": 50}},
    ]
    result = allocate_supplies(zones, warehouses)
    assert result[0].warehouse_id == "w004"
