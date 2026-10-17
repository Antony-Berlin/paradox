"""Invariant & legal-state-machine tests (no LLM key needed)."""

from __future__ import annotations

import pytest

from paradox.storage import Storage, StateError


def _run_to_packed(s: Storage) -> None:
    s.create_order("ORD_1")
    s.check_stock("ORD_1", "WH_1")
    # SKU_1 short by 3 at WH_1 -> procure
    s.purchase_stock("ORD_1", "SUP_1", "SKU_1", 3, "WH_1", idempotency_key="b1")
    s.reserve_inventory("ORD_1", "WH_1", idempotency_key="r1")
    s.pack_order("ORD_1")


def test_full_lifecycle_reaches_verified(storage_with_world):
    s = storage_with_world
    _run_to_packed(s)
    s.assign_driver("ORD_1", "DRV_2", "R_1")
    s.dispatch_order("ORD_1")
    s.complete_delivery("ORD_1")
    s.verify_order("ORD_1")
    assert s.get_order("ORD_1")["status"] == "VERIFIED"


def test_available_never_negative_on_reserve(storage_with_world):
    s = storage_with_world
    s.create_order("ORD_1")
    s.check_stock("ORD_1", "WH_1")
    # try to reserve without procuring the shortfall -> must fail, not oversell
    with pytest.raises(StateError):
        s.reserve_inventory("ORD_1", "WH_1", idempotency_key="bad")


def test_illegal_transition_rejected(storage_with_world):
    s = storage_with_world
    s.create_order("ORD_1")
    # cannot jump straight to DELIVERED
    with pytest.raises(StateError):
        s.complete_delivery("ORD_1")


def test_idempotent_purchase_and_reservation(storage_with_world):
    s = storage_with_world
    s.create_order("ORD_1")
    s.check_stock("ORD_1", "WH_1")
    r1 = s.purchase_stock("ORD_1", "SUP_1", "SKU_1", 3, "WH_1", idempotency_key="k")
    r2 = s.purchase_stock("ORD_1", "SUP_1", "SKU_1", 3, "WH_1", idempotency_key="k")
    assert r1["status"] == "purchased" and r2["status"] == "already_applied"
    res1 = s.reserve_inventory("ORD_1", "WH_1", idempotency_key="rk")
    res2 = s.reserve_inventory("ORD_1", "WH_1", idempotency_key="rk")
    assert res1["status"] == "reserved" and res2["status"] == "already_applied"
    # on_hand after single purchase = 1 + 3 = 4, reserved = 4 -> available 0, nonneg
    with s.tx() as conn:
        inv = conn.execute(
            "SELECT on_hand, reserved FROM inventory WHERE world_id='W_fixture' "
            "AND warehouse_id='WH_1' AND sku='SKU_1'").fetchone()
    assert inv["on_hand"] - inv["reserved"] >= 0


def test_dispatch_fails_when_driver_offline(storage_with_world):
    s = storage_with_world
    _run_to_packed(s)
    s.assign_driver("ORD_1", "DRV_1", "R_1")
    s.inject_execution_fault("driver_cancelled", "DRV_1", "ORD_1")
    with pytest.raises(StateError) as ei:
        s.dispatch_order("ORD_1")
    assert "driver_unavailable" in str(ei.value)
    assert s.get_order("ORD_1")["status"] == "FAILED"


def test_version_increments_on_each_transition(storage_with_world):
    s = storage_with_world
    v0 = s.get_order("ORD_1")["version"]
    s.create_order("ORD_1")
    s.check_stock("ORD_1", "WH_1")
    assert s.get_order("ORD_1")["version"] > v0
