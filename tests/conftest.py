"""Shared pytest fixtures.

The fixture world here is a CODE-BASED test_fixture, explicitly labeled and
confined to tests -- it is NEVER presented as agent-generated demo data (per the
spec's integrity rule). It gives the deterministic layers a stable world to
exercise without any LLM/API key.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from paradox.schemas import World
from paradox.storage import Storage


def make_fixture_world(world_id: str = "W_fixture") -> World:
    return World(
        world_id=world_id, seed_hint="fixture", generated_at="2026-01-01T00:00:00Z",
        generator_model="test_fixture", city="Novapur", simulation_clock="0",
        provenance="test_fixture",
        generation_constraints={"note": "code fixture, not agent-generated"},
        products=[
            {"sku": "SKU_1", "name": "Widget", "category": "general",
             "unit_weight_kg": 2.0, "unit_volume_l": 1.0, "unit_price_inr": 100,
             "reorder_point": 5},
            {"sku": "SKU_2", "name": "Gadget", "category": "general",
             "unit_weight_kg": 1.0, "unit_volume_l": 0.5, "unit_price_inr": 60,
             "reorder_point": 3},
        ],
        warehouses=[
            {"warehouse_id": "WH_1", "zone": "Z1", "capacity_units": 500,
             "max_weight_kg": 2000},
            {"warehouse_id": "WH_2", "zone": "Z2", "capacity_units": 300,
             "max_weight_kg": 1500},
        ],
        inventory=[
            # SKU_1 deliberately short at WH_1 (order wants 4, available 1 -> procure 3)
            {"warehouse_id": "WH_1", "sku": "SKU_1", "on_hand": 1, "reserved": 0,
             "reorder_point": 5},
            {"warehouse_id": "WH_1", "sku": "SKU_2", "on_hand": 20, "reserved": 0,
             "reorder_point": 3},
            {"warehouse_id": "WH_2", "sku": "SKU_1", "on_hand": 10, "reserved": 0,
             "reorder_point": 5},
        ],
        suppliers=[
            {"supplier_id": "SUP_1", "supported_skus": ["SKU_1"], "stock_capacity": 100,
             "unit_cost_inr": 80, "lead_time_minutes": 30, "on_time_probability": 0.9,
             "failure_probability": 0.1, "reliability_prior": 0.8, "service_zone": "Z1"},
            {"supplier_id": "SUP_2", "supported_skus": ["SKU_1", "SKU_2"],
             "stock_capacity": 100, "unit_cost_inr": 90, "lead_time_minutes": 20,
             "on_time_probability": 0.95, "failure_probability": 0.05,
             "reliability_prior": 0.9, "service_zone": "Z1"},
        ],
        drivers=[
            {"driver_id": "DRV_1", "zone": "Z1", "status": "available",
             "max_weight_kg": 100, "vehicle_type": "bike", "cost_inr": 40,
             "travel_time_minutes": 25, "failure_probability": 0.2,
             "reliability_prior": 0.7},
            {"driver_id": "DRV_2", "zone": "Z1", "status": "available",
             "max_weight_kg": 200, "vehicle_type": "van", "cost_inr": 70,
             "travel_time_minutes": 18, "failure_probability": 0.05,
             "reliability_prior": 0.92},
            {"driver_id": "DRV_3", "zone": "Z1", "status": "available",
             "max_weight_kg": 150, "vehicle_type": "car", "cost_inr": 55,
             "travel_time_minutes": 22, "failure_probability": 0.1,
             "reliability_prior": 0.85},
        ],
        routes=[
            {"route_id": "R_1", "origin_warehouse_id": "WH_1", "delivery_zone": "Z1",
             "distance_km": 6.0, "eta_minutes": 20, "risk_factor": 0.1, "cost_inr": 15},
        ],
        orders=[
            {"order_id": "ORD_1", "customer_id": "cust_abc",
             "lines": [{"sku": "SKU_1", "qty": 4}, {"sku": "SKU_2", "qty": 2}],
             "delivery_zone": "Z1", "deadline_minutes": 60, "budget_inr": 2000,
             "priority": "high"},
        ],
    )


@pytest.fixture
def fixture_world() -> World:
    return make_fixture_world()


@pytest.fixture
def storage_with_world(fixture_world) -> Storage:
    db = tempfile.mktemp(suffix=".db")
    s = Storage(db)
    s.seed_world(fixture_world)
    yield s
    Path(db).unlink(missing_ok=True)
    Path(db + "-wal").unlink(missing_ok=True)
    Path(db + "-shm").unlink(missing_ok=True)
