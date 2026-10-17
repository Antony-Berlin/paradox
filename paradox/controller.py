"""Deterministic Mission Controller.

Orchestrates the full PARADOX mission as a state machine. The controller itself
is NOT an LLM: it decides control flow, routes execution tool calls through the
genuine MCP server, runs the deterministic counterfactual tournament, injects a
real fault, invokes the Blue/Recovery agent, and independently verifies the
repair. LLM-backed ADK agents are called only for world synthesis, strategy,
chaos, and recovery choice.

Produces a structured trace (model calls, MCP calls, plans, faults, tournament,
execution events, recovery verifications, assertions) that `reporting.py`
serializes into artifacts/demo.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

from . import adk_agents, policies, scoring, settings
from .mcp_client import ParadoxMcpClient
from .recovery import verify_driver_recovery, verify_final_delivery
from .schemas import Fault, Plan, World
from .simulator import CounterfactualTwin
from .storage import Storage, new_id


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass
class MissionTrace:
    run_id: str
    scenario: str
    world_id: Optional[str] = None
    world_hash: Optional[str] = None
    world_provenance: str = "agent_generated"
    model_calls: list[dict] = field(default_factory=list)
    mcp_calls: list[dict] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)
    plan_feasibility: list[dict] = field(default_factory=list)
    plan_distinctness: dict = field(default_factory=dict)
    shared_faults: list[dict] = field(default_factory=list)
    tournament: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    isolation: dict = field(default_factory=dict)
    execution_events: list[dict] = field(default_factory=list)
    recovery_verifications: list[dict] = field(default_factory=list)
    assertions: list[dict] = field(default_factory=list)
    feedback_before: dict = field(default_factory=dict)
    feedback_after: dict = field(default_factory=dict)
    final_order_status: str = ""
    limitations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def model_call(self, agent: str, model: str, status: str, ms: int,
                   summary: str) -> None:
        self.model_calls.append({"agent": agent, "model": model, "status": status,
                                 "duration_ms": ms, "reason_summary": summary,
                                 "timestamp": _now_iso()})

    def assert_that(self, name: str, ok: bool, detail: str = "") -> bool:
        self.assertions.append({"assertion": name, "ok": bool(ok), "detail": detail})
        return bool(ok)


class MissionController:
    def __init__(self, db_path: str, scenario: str = "driver_failure",
                 seed: int = settings.DEFAULT_SIM_SEED):
        self.db_path = db_path
        self.scenario = scenario
        self.seed = seed
        self.storage = Storage(db_path)
        self.trace = MissionTrace(run_id=new_id("run"), scenario=scenario)

    # --- phase 1: agent-generated world + MCP seed -------------------------
    async def build_and_seed_world(self, mcp: ParadoxMcpClient,
                                   world_id: Optional[str] = None) -> World:
        t0 = time.time()
        ctx = (f"Scenario: {self.scenario}. Generate the world now. The order's "
               "delivery_zone MUST have at least two available drivers so a "
               "driver failure can be recovered. At least one ordered SKU must "
               "require procurement (available < ordered qty).")
        wb = await adk_agents.run_world_builder(ctx)
        ms = int((time.time() - t0) * 1000)
        self.trace.model_call("world_builder", settings.MODEL_ID, "ok", ms,
                              "synthesized relationally-consistent world")
        world = World(
            world_id=world_id or new_id("W"), seed_hint=str(self.seed),
            generated_at=_now_iso(), generator_model=settings.MODEL_ID,
            city=settings.CITY, simulation_clock="0", provenance="agent_generated",
            generation_constraints={"scenario": self.scenario,
                                    "prompt_version": settings.PROMPT_VERSION},
            products=wb.products, warehouses=wb.warehouses, inventory=wb.inventory,
            suppliers=wb.suppliers, drivers=wb.drivers, routes=wb.routes,
            orders=wb.orders)
        res = await mcp.call("seed_world", {"world_json": world.model_dump_json()})
        self.trace.assert_that("world_seeded_via_mcp", res.get("status") == "ok",
                               json.dumps(res))
        self.trace.world_id = world.world_id
        self.trace.world_hash = res.get("world_hash") or self.storage.world_hash(world.world_id)
        return world

    async def load_world(self, world_id: str) -> World:
        snap = self.storage.get_world_snapshot(world_id)
        snap.pop("_world_hash", None)
        self.trace.world_id = world_id
        self.trace.world_hash = self.storage.world_hash(world_id)
        self.trace.world_provenance = "replay_snapshot"
        return World.model_validate(snap)

    # --- phase 2: strategy -> 3 distinct feasible plans --------------------
    async def propose_plans(self, world: World) -> list[Plan]:
        order = world.orders[0]
        feedback = self.storage.get_feedback(world.world_id)
        t0 = time.time()
        ctx = self._strategy_context(world, order, feedback)
        out = await adk_agents.run_strategy(ctx)
        ms = int((time.time() - t0) * 1000)
        self.trace.model_call("strategy", settings.MODEL_ID, "ok", ms,
                              "proposed 3 candidate plans")
        plans = out.plans
        # feasibility gate
        feasible: list[Plan] = []
        for p in plans:
            errs = policies.validate_plan_feasible(p, order, world)
            self.trace.plan_feasibility.append(
                {"plan_id": p.plan_id, "objective": p.objective,
                 "feasible": not errs, "errors": errs,
                 "spof": policies.single_point_of_failure(p)})
            if not errs:
                feasible.append(p)
        # distinctness gate (operational, not labels)
        ok, why = policies.plans_distinct(feasible)
        self.trace.plan_distinctness = {"distinct": ok, "detail": why,
                                        "count": len(feasible)}
        self.trace.plans = [p.model_dump() for p in plans]
        self.trace.assert_that("three_operationally_distinct_plans",
                               ok and len(feasible) == 3, why)
        return feasible

    # --- phase 3: chaos -> shared fault suite ------------------------------
    async def propose_faults(self, world: World, plans: list[Plan]) -> list[Fault]:
        t0 = time.time()
        ctx = self._chaos_context(world, plans)
        out = await adk_agents.run_chaos(ctx)
        ms = int((time.time() - t0) * 1000)
        self.trace.model_call("chaos", settings.MODEL_ID, "ok", ms,
                              "proposed targeted fault suite")
        safe: list[Fault] = []
        for f in out.faults:
            errs = policies.validate_fault(f, world)
            if not errs:
                safe.append(f)
            self.trace.shared_faults.append(
                {**f.model_dump(), "accepted": not errs, "safety_errors": errs})
        self.trace.assert_that("faults_target_real_entities", len(safe) >= 2,
                               f"{len(safe)} safe faults")
        return safe

    # --- phase 4: counterfactual tournament (deterministic) ----------------
    def run_tournament(self, world: World, plans: list[Plan],
                       faults: list[Fault]) -> Plan:
        order = world.orders[0]
        hash_before = self.storage.world_hash(world.world_id)
        versions_before = self.storage.row_versions(world.world_id)

        twin = CounterfactualTwin(world, seed=self.seed)
        results_by_plan: dict[str, list] = {}
        for p in plans:
            results_by_plan[p.plan_id] = [
                twin.simulate(p, f, order.order_id) for f in faults]

        outcome = scoring.rank_tournament(plans, order, results_by_plan)
        baseline = scoring.cheapest_first_baseline(plans, order, results_by_plan)
        self.trace.tournament = outcome
        self.trace.baseline = baseline

        # isolation proof: baseline DB untouched by the counterfactual twin
        hash_after = self.storage.world_hash(world.world_id)
        versions_after = self.storage.row_versions(world.world_id)
        self.trace.isolation = {
            "world_hash_before": hash_before, "world_hash_after": hash_after,
            "hash_unchanged": hash_before == hash_after,
            "versions_unchanged": versions_before == versions_after}
        self.trace.assert_that("counterfactual_isolation",
                               hash_before == hash_after
                               and versions_before == versions_after,
                               "baseline world hash + versions unchanged")

        champ_id = outcome["champion_plan_id"]
        champion = next((p for p in plans if p.plan_id == champ_id), None)
        if champion is None:
            raise RuntimeError("tournament produced no eligible champion")
        self.trace.assert_that("champion_beats_or_matches_baseline",
                               outcome["ranked"][0]["score"]
                               >= baseline["score"]["score"],
                               f"champion {outcome['ranked'][0]['score']} "
                               f"vs cheapest-first {baseline['score']['score']}")
        return champion

    # --- phase 5: live execution + injected fault + recovery ---------------
    async def execute_with_recovery(self, mcp: ParadoxMcpClient, world: World,
                                    champion: Plan) -> None:
        order = world.orders[0]
        oid = order.order_id
        rid = self.trace.run_id
        self.trace.feedback_before = self.storage.get_feedback(world.world_id)

        wh = champion.lines[0].warehouse_id
        await mcp.call("create_order", {"order_id": oid, "run_id": rid})
        await mcp.call("check_stock", {"order_id": oid, "warehouse_id": wh, "run_id": rid})
        # procure where needed
        for ln in champion.lines:
            if ln.procure_qty > 0 and ln.supplier_id:
                await mcp.call("purchase_stock", {
                    "order_id": oid, "supplier_id": ln.supplier_id, "sku": ln.sku,
                    "qty": ln.procure_qty, "warehouse_id": ln.warehouse_id,
                    "idempotency_key": f"{rid}:buy:{ln.sku}", "run_id": rid})
        await mcp.call("reserve_inventory", {
            "order_id": oid, "warehouse_id": wh,
            "idempotency_key": f"{rid}:reserve", "run_id": rid})
        await mcp.call("pack_order", {"order_id": oid, "run_id": rid})
        await mcp.call("assign_driver", {
            "order_id": oid, "driver_id": champion.driver_id,
            "route_id": champion.route_id, "run_id": rid})

        # ---- inject the REAL driver fault after PACKED ----
        fault_res = await mcp.call("inject_execution_fault", {
            "kind": "driver_cancelled", "target_id": champion.driver_id,
            "order_id": oid, "reason": "primary driver cancelled before dispatch",
            "run_id": rid})
        fault_id = fault_res.get("fault_id", "")
        self.trace.assert_that("driver_fault_applied",
                               fault_res.get("applied") is True, json.dumps(fault_res))

        # dispatch must fail structurally
        disp = await mcp.call("dispatch_order", {"order_id": oid, "run_id": rid})
        self.trace.assert_that("dispatch_fails_on_fault",
                               disp.get("status") == "error", json.dumps(disp))

        # ---- Blue/Recovery agent chooses an alternative over MCP-read candidates ----
        recovered = await self._recover_driver(mcp, world, champion, oid, fault_id, rid)

        if recovered:
            await mcp.call("dispatch_order", {"order_id": oid, "run_id": rid})
            await mcp.call("complete_delivery", {"order_id": oid, "run_id": rid})
            await mcp.call("verify_order", {"order_id": oid, "run_id": rid})

        # feedback: failed driver down, recovery driver credited
        failed_evt = None
        for e in self.storage.get_events(order_id=oid):
            if e["event_type"] == "fault_injected":
                failed_evt = e["event_id"]
        self.storage.record_feedback(world.world_id, "driver", champion.driver_id,
                                     "failure", event_id=failed_evt or "")
        final = self.storage.get_order(oid)
        self.trace.final_order_status = final["status"]
        self.trace.feedback_after = self.storage.get_feedback(world.world_id)

        fv = verify_final_delivery(self.storage, oid)
        self.trace.recovery_verifications.append({"type": "final_delivery", **fv})
        self.trace.assert_that("order_reaches_verified_after_recovery",
                               final["status"] == "VERIFIED", final["status"])
        self.trace.execution_events = self.storage.get_events(order_id=oid)

    async def _recover_driver(self, mcp: ParadoxMcpClient, world: World,
                              champion: Plan, oid: str, fault_id: str,
                              rid: str) -> bool:
        order = world.orders[0]
        for attempt in range(settings.MAX_RECOVERY_ATTEMPTS):
            cand = await mcp.call("list_available_drivers", {
                "world_id": world.world_id, "zone": order.delivery_zone})
            candidates = cand.get("drivers", [])
            cand_ids = [d["driver_id"] for d in candidates
                        if d["driver_id"] != champion.driver_id]
            if not cand_ids:
                break
            t0 = time.time()
            ctx = self._recovery_context(world, champion, candidates, fault_id)
            choice = await adk_agents.run_recovery(ctx)
            ms = int((time.time() - t0) * 1000)
            self.trace.model_call("recovery", settings.MODEL_ID, "ok", ms,
                                  f"chose {choice.action}")
            if choice.action != "reassign_driver" or not choice.new_driver_id:
                continue
            if choice.new_driver_id not in cand_ids:
                continue  # agent must pick from candidate list
            res = await mcp.call("reassign_driver", {
                "order_id": oid, "new_driver_id": choice.new_driver_id,
                "fault_id": fault_id, "run_id": rid})
            if res.get("status") != "ok":
                continue
            attempt_id = res.get("attempt_id", "")
            ver = verify_driver_recovery(self.storage, oid, champion.driver_id,
                                         attempt_id)
            self.trace.recovery_verifications.append({"type": "driver", **ver})
            if ver["verified"]:
                self.storage.record_feedback(world.world_id, "driver",
                                            choice.new_driver_id, "recovery")
                return True
        # escalate
        self.trace.limitations.append("driver recovery exhausted attempts -> ESCALATED")
        return False

    # --- context builders (compact, evidence-only, no secrets) --------------
    @staticmethod
    def _strategy_context(world: World, order, feedback: dict) -> str:
        return json.dumps({
            "order": order.model_dump(),
            "products": [p.model_dump() for p in world.products],
            "warehouses": [w.model_dump() for w in world.warehouses],
            "inventory": [i.model_dump() for i in world.inventory],
            "suppliers": [s.model_dump() for s in world.suppliers],
            "drivers": [d.model_dump() for d in world.drivers],
            "routes": [r.model_dump() for r in world.routes],
            "reliability_feedback": {k: v.get("reliability") for k, v in feedback.items()},
        }, default=str)

    @staticmethod
    def _chaos_context(world: World, plans: list[Plan]) -> str:
        return json.dumps({
            "plans": [p.model_dump() for p in plans],
            "drivers": [{"driver_id": d.driver_id, "zone": d.zone} for d in world.drivers],
            "suppliers": [{"supplier_id": s.supplier_id, "supported_skus": s.supported_skus}
                          for s in world.suppliers],
            "inventory": [{"warehouse_id": i.warehouse_id, "sku": i.sku,
                           "available": i.available} for i in world.inventory],
            "spof_per_plan": {p.plan_id: policies.single_point_of_failure(p) for p in plans},
        }, default=str)

    @staticmethod
    def _recovery_context(world: World, champion: Plan, candidates: list[dict],
                          fault_id: str) -> str:
        order = world.orders[0]
        return json.dumps({
            "failure": {"kind": "driver_cancelled",
                        "failed_driver_id": champion.driver_id,
                        "fault_id": fault_id, "stage": "DISPATCHED"},
            "order": {"order_id": order.order_id, "delivery_zone": order.delivery_zone},
            "candidate_drivers": [
                {"driver_id": d["driver_id"], "zone": d["zone"],
                 "max_weight_kg": d["max_weight_kg"], "status": d["status"]}
                for d in candidates],
            "constraints": "choose an available driver in the order zone with "
                           "capacity, not the failed driver",
        }, default=str)
