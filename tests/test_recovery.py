"""Recovery verification tests (no LLM key needed).

Drives the storage layer through a real injected driver fault and a
`reassign_driver` recovery, then checks the independent verifier confirms it.
"""

from __future__ import annotations

from paradox.recovery import verify_driver_recovery, verify_final_delivery


def _to_packed(s):
    s.create_order("ORD_1")
    s.check_stock("ORD_1", "WH_1")
    s.purchase_stock("ORD_1", "SUP_1", "SKU_1", 3, "WH_1", idempotency_key="b1")
    s.reserve_inventory("ORD_1", "WH_1", idempotency_key="r1")
    s.pack_order("ORD_1")


def test_driver_recovery_verified(storage_with_world):
    s = storage_with_world
    _to_packed(s)
    s.assign_driver("ORD_1", "DRV_1", "R_1")
    fault = s.inject_execution_fault("driver_cancelled", "DRV_1", "ORD_1")
    # dispatch fails -> order FAILED
    try:
        s.dispatch_order("ORD_1")
    except Exception:
        pass
    attempt = s.reassign_driver("ORD_1", "DRV_2", fault_id=fault["fault_id"])
    ver = verify_driver_recovery(s, "ORD_1", "DRV_1", attempt["attempt_id"])
    assert ver["verified"] is True
    # the failed driver must not have been reselected
    assert s.get_order("ORD_1")["assigned_driver_id"] == "DRV_2"


def test_cannot_reassign_to_failed_driver(storage_with_world):
    s = storage_with_world
    _to_packed(s)
    s.assign_driver("ORD_1", "DRV_1", "R_1")
    s.inject_execution_fault("driver_cancelled", "DRV_1", "ORD_1")
    try:
        s.dispatch_order("ORD_1")
    except Exception:
        pass
    import pytest
    with pytest.raises(Exception):
        s.reassign_driver("ORD_1", "DRV_1")  # same (failed) driver -> rejected


def test_final_delivery_verified_after_recovery(storage_with_world):
    s = storage_with_world
    _to_packed(s)
    s.assign_driver("ORD_1", "DRV_1", "R_1")
    f = s.inject_execution_fault("driver_cancelled", "DRV_1", "ORD_1")
    try:
        s.dispatch_order("ORD_1")
    except Exception:
        pass
    s.reassign_driver("ORD_1", "DRV_2", fault_id=f["fault_id"])
    s.dispatch_order("ORD_1")
    s.complete_delivery("ORD_1")
    s.verify_order("ORD_1")
    fv = verify_final_delivery(s, "ORD_1")
    assert fv["verified"] is True
    assert any("->VERIFIED" in t for t in fv["transition_path"])


def test_feedback_reliability_shifts(storage_with_world):
    s = storage_with_world
    before = s.get_feedback("W_fixture")["driver:DRV_1"]["reliability"]
    s.record_feedback("W_fixture", "driver", "DRV_1", "failure")
    after = s.get_feedback("W_fixture")["driver:DRV_1"]["reliability"]
    assert after < before  # a failure lowers smoothed reliability
