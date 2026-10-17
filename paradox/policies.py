"""Safety gate, invariants, and plan feasibility/distinctness checks.

Pure deterministic code -- never an LLM. These are the security/validation
boundary the spec requires: the LLM may *propose* plans and faults, but code
decides what is legal to run.
"""

from __future__ import annotations

from typing import Optional

from .schemas import Fault, Plan, World


class PolicyViolation(Exception):
    pass


def index_world(world: World) -> dict:
    return {
        "skus": {p.sku: p for p in world.products},
        "warehouses": {w.warehouse_id: w for w in world.warehouses},
        "inventory": {(i.warehouse_id, i.sku): i for i in world.inventory},
        "suppliers": {s.supplier_id: s for s in world.suppliers},
        "drivers": {d.driver_id: d for d in world.drivers},
        "routes": {r.route_id: r for r in world.routes},
        "orders": {o.order_id: o for o in world.orders},
    }


def validate_plan_feasible(plan: Plan, order, world: World) -> list[str]:
    """Return a list of feasibility errors (empty => feasible)."""
    idx = index_world(world)
    errors: list[str] = []

    # driver + route must exist
    if plan.driver_id not in idx["drivers"]:
        errors.append(f"unknown driver {plan.driver_id}")
    if plan.backup_driver_id and plan.backup_driver_id not in idx["drivers"]:
        errors.append(f"unknown backup driver {plan.backup_driver_id}")
    if plan.route_id not in idx["routes"]:
        errors.append(f"unknown route {plan.route_id}")

    # driver zone must match order delivery zone
    drv = idx["drivers"].get(plan.driver_id)
    if drv and drv.zone != order.delivery_zone:
        errors.append(
            f"driver {plan.driver_id} zone {drv.zone} != order zone {order.delivery_zone}")

    # each plan line references valid sku/warehouse and (if procuring) a supplier
    order_qty = {ln.sku: ln.qty for ln in order.lines}
    covered: dict[str, int] = {}
    for ln in plan.lines:
        if ln.sku not in idx["skus"]:
            errors.append(f"plan line unknown sku {ln.sku}")
        if ln.warehouse_id not in idx["warehouses"]:
            errors.append(f"plan line unknown warehouse {ln.warehouse_id}")
        if ln.sku not in order_qty:
            errors.append(f"plan line sku {ln.sku} not in order")
        covered[ln.sku] = covered.get(ln.sku, 0) + ln.qty
        if ln.procure_qty > 0:
            if not ln.supplier_id:
                errors.append(f"line {ln.sku} procures but has no supplier")
            else:
                sup = idx["suppliers"].get(ln.supplier_id)
                if not sup:
                    errors.append(f"unknown supplier {ln.supplier_id}")
                elif ln.sku not in sup.supported_skus:
                    errors.append(f"supplier {ln.supplier_id} does not supply {ln.sku}")
                elif ln.procure_qty > sup.stock_capacity:
                    errors.append(
                        f"supplier {ln.supplier_id} capacity < procure_qty {ln.procure_qty}")

    # plan must cover the full order quantity per sku
    for sku, qty in order_qty.items():
        if covered.get(sku, 0) < qty:
            errors.append(f"plan under-covers {sku}: {covered.get(sku,0)} < {qty}")

    # weight must fit the assigned driver
    if drv:
        total_weight = sum(
            idx["skus"][ln.sku].unit_weight_kg * ln.qty
            for ln in plan.lines if ln.sku in idx["skus"])
        if total_weight > drv.max_weight_kg:
            errors.append(
                f"driver {plan.driver_id} max_weight {drv.max_weight_kg} < load {total_weight:.1f}")

    return errors


def plans_distinct(plans: list[Plan]) -> tuple[bool, str]:
    """Distinctness compares OPERATIONAL signatures, not strategy labels."""
    sigs = [p.operational_signature() for p in plans]
    if len(set(sigs)) == len(sigs):
        return True, "all plans operationally distinct"
    return False, "two or more plans share an operational signature (not distinct)"


def validate_fault(fault: Fault, world: World) -> list[str]:
    """Safety gate: fault must target a real entity and be bounded."""
    idx = index_world(world)
    errors: list[str] = []
    if fault.kind == "driver_cancelled":
        if fault.target_id not in idx["drivers"]:
            errors.append(f"fault targets unknown driver {fault.target_id}")
    elif fault.kind == "supplier_down":
        if fault.target_id not in idx["suppliers"]:
            errors.append(f"fault targets unknown supplier {fault.target_id}")
    elif fault.kind == "stock_discrepancy":
        if ":" not in fault.target_id:
            errors.append("stock_discrepancy target must be 'warehouse_id:sku'")
        else:
            wh, sku = fault.target_id.split(":", 1)
            if (wh, sku) not in idx["inventory"]:
                errors.append(f"fault targets unknown inventory {fault.target_id}")
    if not (0.0 <= fault.magnitude <= 1.0):
        errors.append("fault magnitude out of bounds")
    return errors


def single_point_of_failure(plan: Plan) -> dict:
    """Identify the plan's single-point-of-failure dependency (novelty hook)."""
    spofs = []
    if not plan.backup_driver_id:
        spofs.append("no_backup_driver")
    suppliers = {ln.supplier_id for ln in plan.lines if ln.supplier_id}
    if len(suppliers) == 1:
        spofs.append(f"single_supplier:{next(iter(suppliers))}")
    warehouses = {ln.warehouse_id for ln in plan.lines}
    if len(warehouses) == 1:
        spofs.append(f"single_warehouse:{next(iter(warehouses))}")
    return {"has_spof": bool(spofs), "dependencies": spofs}
