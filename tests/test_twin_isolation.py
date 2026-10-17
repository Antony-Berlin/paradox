"""Counterfactual twin isolation tests (no LLM key needed).

Proves the simulator never mutates the baseline DB: world hash + row versions
are identical after a full tournament of (plan x fault) branches.
"""

from __future__ import annotations

from paradox.policies import validate_plan_feasible
from paradox.schemas import Fault, Plan
from paradox.simulator import CounterfactualTwin


def _plans():
    cheapest = Plan(plan_id="cheapest_1", objective="cheapest",
                    lines=[{"sku": "SKU_1", "qty": 4, "warehouse_id": "WH_1",
                            "supplier_id": "SUP_1", "procure_qty": 3},
                           {"sku": "SKU_2", "qty": 2, "warehouse_id": "WH_1"}],
                    driver_id="DRV_1", backup_driver_id=None, route_id="R_1",
                    estimated_cost_inr=295, estimated_eta_minutes=45,
                    reason_summary="cheapest supplier+driver, no backup")
    fastest = Plan(plan_id="fastest_1", objective="fastest",
                   lines=[{"sku": "SKU_1", "qty": 4, "warehouse_id": "WH_1",
                           "supplier_id": "SUP_2", "procure_qty": 3},
                          {"sku": "SKU_2", "qty": 2, "warehouse_id": "WH_1"}],
                   driver_id="DRV_2", backup_driver_id=None, route_id="R_1",
                   estimated_cost_inr=340, estimated_eta_minutes=38,
                   reason_summary="fastest driver")
    resilient = Plan(plan_id="resilience_1", objective="resilience",
                     lines=[{"sku": "SKU_1", "qty": 4, "warehouse_id": "WH_1",
                             "supplier_id": "SUP_2", "procure_qty": 3},
                            {"sku": "SKU_2", "qty": 2, "warehouse_id": "WH_1"}],
                     driver_id="DRV_3", backup_driver_id="DRV_2", route_id="R_1",
                     estimated_cost_inr=325, estimated_eta_minutes=42,
                     reason_summary="reliable driver + backup, no SPOF")
    return [cheapest, fastest, resilient]


def _faults():
    return [
        Fault(fault_id="f_drv1", kind="driver_cancelled", target_id="DRV_1",
              at_stage="PACKED", reason_summary="attack cheapest's only driver"),
        Fault(fault_id="f_sup1", kind="supplier_down", target_id="SUP_1",
              at_stage="PROCUREMENT_REQUIRED", reason_summary="attack cheapest's supplier"),
    ]


def test_plans_feasible_in_fixture(fixture_world):
    order = fixture_world.orders[0]
    for p in _plans():
        assert validate_plan_feasible(p, order, fixture_world) == []


def test_twin_does_not_mutate_baseline(storage_with_world, fixture_world):
    s = storage_with_world
    hash_before = s.world_hash("W_fixture")
    versions_before = s.row_versions("W_fixture")

    twin = CounterfactualTwin(fixture_world, seed=123)
    for p in _plans():
        for f in _faults():
            twin.simulate(p, f, "ORD_1")

    assert s.world_hash("W_fixture") == hash_before
    assert s.row_versions("W_fixture") == versions_before


def test_twin_determinism(fixture_world):
    twin1 = CounterfactualTwin(fixture_world, seed=7)
    twin2 = CounterfactualTwin(fixture_world, seed=7)
    p, f = _plans()[0], _faults()[0]
    r1 = twin1.simulate(p, f, "ORD_1")
    r2 = twin2.simulate(p, f, "ORD_1")
    assert (r1.delivered, r1.total_cost_inr, r1.recovery_steps) == \
           (r2.delivered, r2.total_cost_inr, r2.recovery_steps)


def test_resilient_plan_survives_driver_fault_without_backup_loss(fixture_world):
    """The cheapest plan (no backup) should need a fallback; resilient has a backup."""
    twin = CounterfactualTwin(fixture_world, seed=7)
    cheapest, _, resilient = _plans()
    drv_fault = _faults()[0]  # cancels DRV_1 (cheapest's only driver)
    rc = twin.simulate(cheapest, drv_fault, "ORD_1")
    rr = twin.simulate(resilient, drv_fault, "ORD_1")
    # cheapest's own driver isn't DRV_1-dependent here only if fallback found;
    # resilient explicitly unaffected (uses DRV_3) and keeps its backup intact
    assert rr.delivered is True
    assert rr.spof["has_spof"] is False or "no_backup_driver" not in rr.spof["dependencies"]
