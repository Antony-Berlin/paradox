"""Independent recovery verification.

Deterministic postcondition checks over persisted state -- NOT the LLM's
self-report. After a recovery action, the verifier confirms the repair actually
took effect and the order can legitimately proceed. Each verified recovery
yields a verifier_id that the report cites as evidence.
"""

from __future__ import annotations

from .storage import Storage, new_id


def verify_driver_recovery(storage: Storage, order_id: str, failed_driver_id: str,
                           attempt_id: str) -> dict:
    """Confirm a driver reassignment is valid and the order can advance.

    Checks (all must hold):
      * the order's assigned driver changed and is NOT the failed driver,
      * the new driver is in the correct zone with sufficient capacity,
      * the order is in a dispatchable state (PACKED after RECOVERING).
    """
    order = storage.get_order(order_id)
    world_id = order["world_id"]
    new_driver = order["assigned_driver_id"]
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("driver_changed", new_driver and new_driver != failed_driver_id,
          f"{failed_driver_id} -> {new_driver}")
    check("failed_driver_not_reselected", new_driver != failed_driver_id)

    with storage.tx() as conn:
        drv = conn.execute(
            "SELECT * FROM drivers WHERE world_id=? AND driver_id=?",
            (world_id, new_driver),
        ).fetchone()
        check("new_driver_exists", drv is not None, str(new_driver))
        if drv:
            check("zone_valid", drv["zone"] == order["delivery_zone"],
                  f"driver zone {drv['zone']} vs order {order['delivery_zone']}")
            check("capacity_valid", order["packed_weight_kg"] <= drv["max_weight_kg"],
                  f"packed {order['packed_weight_kg']} <= max {drv['max_weight_kg']}")

    check("order_dispatchable", order["status"] in ("PACKED", "DISPATCHED",
                                                     "DELIVERED", "VERIFIED"),
          order["status"])

    verified = all(c["ok"] for c in checks)
    verifier_id = new_id("ver")
    storage.mark_recovery_verified(attempt_id, verifier_id, verified)
    return {"verifier_id": verifier_id, "verified": verified, "checks": checks,
            "attempt_id": attempt_id}


def verify_final_delivery(storage: Storage, order_id: str) -> dict:
    """Confirm the order reached VERIFIED through a legal, evented path."""
    order = storage.get_order(order_id)
    events = storage.get_events(order_id=order_id)
    transitions = [e["event_type"] for e in events
                   if e["event_type"].startswith("transition:")]
    verifier_id = new_id("ver")
    checks = [
        {"check": "final_status_verified", "ok": order["status"] == "VERIFIED",
         "detail": order["status"]},
        {"check": "reached_delivered", "ok": any("->DELIVERED" in t for t in transitions)},
        {"check": "reached_verified", "ok": any("->VERIFIED" in t for t in transitions)},
        {"check": "has_recovery_event",
         "ok": any(e["event_type"] == "recovery_action" for e in events)},
    ]
    return {"verifier_id": verifier_id,
            "verified": all(c["ok"] for c in checks),
            "checks": checks, "transition_path": transitions}
