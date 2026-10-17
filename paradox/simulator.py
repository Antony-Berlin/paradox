"""Counterfactual digital twin.

For each (plan x fault_case) the simulator forks a DEEP COPY of the frozen
baseline world into an isolated in-memory SQLite DB and runs the plan's
execution deterministically, applying the fault at its stage. It computes
factual outcomes (feasible stock movements, purchases, cost, delivery time,
recovery steps, final state) -- the LLM never self-reports success.

Hard guarantee: the simulator operates ONLY on cloned state. The real
`Storage` / DB passed as the baseline is read once (its snapshot) and never
mutated. `test_twin_isolation.py` asserts the baseline hash/versions are
unchanged after a full tournament.

Determinism: a fixed RNG seed governs only fault-trial randomness; the same
seed + fault definitions are used across all plans for a fair comparison.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Optional

from . import settings
from .policies import index_world, single_point_of_failure
from .schemas import Fault, Plan, World


@dataclass
class SimResult:
    plan_id: str
    objective: str
    fault_id: Optional[str]
    feasible: bool
    delivered: bool
    recovered: bool
    final_state: str
    total_cost_inr: float
    delivery_time_minutes: int
    recovery_steps: int
    disqualified: bool
    disqualification_reason: str
    spof: dict
    invariant_ok: bool
    trace: list[str] = field(default_factory=list)


class CounterfactualTwin:
    """Pure in-memory simulation over a cloned world. No external DB writes."""

    def __init__(self, world: World, seed: int = settings.DEFAULT_SIM_SEED):
        # deep, independent copy of the world document
        self.baseline = World.model_validate(world.model_dump())
        self.seed = seed

    def _fresh_state(self) -> dict:
        """A mutable clone of inventory/driver/supplier state for one branch."""
        idx = index_world(self.baseline)
        return {
            "inventory": {k: {"on_hand": v.on_hand, "reserved": v.reserved}
                          for k, v in idx["inventory"].items()},
            "suppliers": {sid: {"available": True,
                                "capacity": s.stock_capacity,
                                "unit_cost": s.unit_cost_inr,
                                "supported": set(s.supported_skus)}
                          for sid, s in idx["suppliers"].items()},
            "drivers": {did: {"status": "available", "zone": d.zone,
                              "cost": d.cost_inr, "max_weight": d.max_weight_kg,
                              "travel": d.travel_time_minutes}
                        for did, d in idx["drivers"].items()},
            "skus": idx["skus"], "routes": idx["routes"], "orders": idx["orders"],
        }

    def simulate(self, plan: Plan, fault: Optional[Fault], order_id: str) -> SimResult:
        rng = random.Random(f"{self.seed}:{plan.plan_id}:{fault.fault_id if fault else 'none'}")
        st = self._fresh_state()
        order = st["orders"][order_id]
        trace: list[str] = []
        cost = 0.0
        recovery_steps = 0
        spof = single_point_of_failure(plan)

        def apply_fault_if(stage: str) -> None:
            if fault and fault.at_stage == stage:
                if fault.kind == "driver_cancelled":
                    if fault.target_id in st["drivers"]:
                        st["drivers"][fault.target_id]["status"] = "offline"
                        trace.append(f"FAULT driver_cancelled {fault.target_id} @ {stage}")
                elif fault.kind == "supplier_down":
                    if fault.target_id in st["suppliers"]:
                        st["suppliers"][fault.target_id]["available"] = False
                        trace.append(f"FAULT supplier_down {fault.target_id} @ {stage}")
                elif fault.kind == "stock_discrepancy":
                    wh, sku = fault.target_id.split(":", 1)
                    if (wh, sku) in st["inventory"]:
                        st["inventory"][(wh, sku)]["on_hand"] = max(
                            0, st["inventory"][(wh, sku)]["on_hand"] - 1)
                        trace.append(f"FAULT stock_discrepancy {fault.target_id} @ {stage}")

        # ---- STOCK_CHECKED ----
        apply_fault_if("STOCK_CHECKED")

        # ---- PROCUREMENT (if any line needs it) ----
        apply_fault_if("PROCUREMENT_REQUIRED")
        for ln in plan.lines:
            if ln.procure_qty > 0:
                sup = st["suppliers"].get(ln.supplier_id)
                if not sup or not sup["available"]:
                    # try supplier switch: any available supplier for this sku
                    alt = next((sid for sid, s in st["suppliers"].items()
                                if s["available"] and ln.sku in s["supported"]
                                and sid != ln.supplier_id), None)
                    if alt:
                        recovery_steps += 1
                        sup = st["suppliers"][alt]
                        trace.append(f"RECOVER switch_supplier {ln.supplier_id}->{alt}")
                    else:
                        return self._dq(plan, fault, "supplier_unavailable_no_alt",
                                        spof, trace, cost)
                if ln.procure_qty > sup["capacity"]:
                    return self._dq(plan, fault, "supplier_capacity", spof, trace, cost)
                cost += sup["unit_cost"] * ln.procure_qty
                st["inventory"].setdefault((ln.warehouse_id, ln.sku),
                                           {"on_hand": 0, "reserved": 0})
                st["inventory"][(ln.warehouse_id, ln.sku)]["on_hand"] += ln.procure_qty

        # ---- RESERVE ---- (check availability, no overselling)
        for ln in plan.lines:
            inv = st["inventory"].get((ln.warehouse_id, ln.sku), {"on_hand": 0, "reserved": 0})
            if inv["on_hand"] - inv["reserved"] < ln.qty:
                return self._dq(plan, fault, "insufficient_stock_at_reserve",
                                spof, trace, cost)
            inv["reserved"] += ln.qty

        # ---- PACK ----
        total_weight = sum(st["skus"][ln.sku].unit_weight_kg * ln.qty
                           for ln in plan.lines if ln.sku in st["skus"])

        # ---- ASSIGN + DISPATCH ----
        apply_fault_if("PACKED")
        apply_fault_if("DISPATCHED")
        driver_id = plan.driver_id
        drv = st["drivers"].get(driver_id)
        if not drv or drv["status"] != "available" or drv["zone"] != order.delivery_zone \
                or total_weight > drv["max_weight"]:
            # recover with backup driver if valid
            bk = plan.backup_driver_id
            bdrv = st["drivers"].get(bk) if bk else None
            if bdrv and bdrv["status"] == "available" and bdrv["zone"] == order.delivery_zone \
                    and total_weight <= bdrv["max_weight"] and bk != driver_id:
                recovery_steps += 1
                driver_id = bk
                drv = bdrv
                trace.append(f"RECOVER reassign_driver {plan.driver_id}->{bk}")
            else:
                # last resort: any available in-zone driver
                alt = next((did for did, d in st["drivers"].items()
                            if d["status"] == "available" and d["zone"] == order.delivery_zone
                            and total_weight <= d["max_weight"] and did != plan.driver_id), None)
                if alt:
                    recovery_steps += 1
                    driver_id = alt
                    drv = st["drivers"][alt]
                    trace.append(f"RECOVER fallback_driver ->{alt}")
                else:
                    return self._dq(plan, fault, "no_feasible_driver", spof, trace, cost)

        # ---- DELIVER ----
        cost += drv["cost"]
        route = st["routes"].get(plan.route_id)
        delivery_time = (route.eta_minutes if route else 0) + drv["travel"]
        trace.append(f"DELIVERED via {driver_id} in {delivery_time}min cost {cost:.0f}")

        invariant_ok = all(
            v["on_hand"] - v["reserved"] >= 0 for v in st["inventory"].values())

        return SimResult(
            plan_id=plan.plan_id, objective=plan.objective,
            fault_id=fault.fault_id if fault else None,
            feasible=True, delivered=True, recovered=recovery_steps > 0,
            final_state="VERIFIED", total_cost_inr=round(cost, 2),
            delivery_time_minutes=delivery_time, recovery_steps=recovery_steps,
            disqualified=False, disqualification_reason="", spof=spof,
            invariant_ok=invariant_ok, trace=trace)

    @staticmethod
    def _dq(plan: Plan, fault: Optional[Fault], reason: str, spof: dict,
            trace: list[str], cost: float) -> SimResult:
        trace.append(f"DISQUALIFIED {reason}")
        return SimResult(
            plan_id=plan.plan_id, objective=plan.objective,
            fault_id=fault.fault_id if fault else None, feasible=False,
            delivered=False, recovered=False, final_state="FAILED",
            total_cost_inr=round(cost, 2), delivery_time_minutes=10**6,
            recovery_steps=0, disqualified=True, disqualification_reason=reason,
            spof=spof, invariant_ok=True, trace=trace)
