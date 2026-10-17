"""Deterministic independent scorer for the counterfactual tournament.

Ranks plans across the shared fault suite with normalized components and the
demo policy weights (40% resilience, 30% deadline, 20% cost, 10% verification).
Shows raw metrics, normalization rules, score breakdown, and hard
disqualifications. This is NOT an LLM and NOT a self-report -- it scores only
verified simulator traces.
"""

from __future__ import annotations

from dataclasses import asdict
from statistics import mean
from typing import Optional

from . import settings
from .simulator import SimResult


def _normalize_lower_better(value: float, lo: float, hi: float) -> float:
    """Map [lo,hi] -> [1,0] (lower value => higher score)."""
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (hi - value) / (hi - lo)))


def score_plan(plan_id: str, objective: str, results: list[SimResult],
               order, all_results: list[SimResult]) -> dict:
    """Score one plan across its fault-suite results, normalized against peers."""
    # resilience: fraction of fault cases delivered (and whether recovery worked)
    n = len(results)
    delivered = [r for r in results if r.delivered and not r.disqualified]
    resilience = len(delivered) / n if n else 0.0

    # deadline: fraction of delivered cases within the order deadline
    if delivered:
        on_time = [r for r in delivered if r.delivery_time_minutes <= order.deadline_minutes]
        deadline = len(on_time) / len(delivered)
    else:
        deadline = 0.0

    # cost: normalized vs the global cost range of all feasible branches
    feasible_costs = [r.total_cost_inr for r in all_results
                      if r.delivered and not r.disqualified]
    if delivered and feasible_costs:
        lo, hi = min(feasible_costs), max(feasible_costs)
        cost_component = mean(
            _normalize_lower_better(r.total_cost_inr, lo, hi) for r in delivered)
    else:
        cost_component = 0.0

    # verification: fraction of branches that pass invariant checks
    verification = (sum(1 for r in results if r.invariant_ok) / n) if n else 0.0

    w = settings.SCORE_WEIGHTS
    total = (w["resilience"] * resilience + w["deadline"] * deadline
             + w["cost"] * cost_component + w["verification"] * verification)

    hard_disqualifications = [
        {"fault_id": r.fault_id, "reason": r.disqualification_reason}
        for r in results if r.disqualified]

    return {
        "plan_id": plan_id,
        "objective": objective,
        "raw_metrics": {
            "fault_cases": n,
            "delivered_cases": len(delivered),
            "mean_cost_inr": round(mean([r.total_cost_inr for r in delivered]), 2) if delivered else None,
            "mean_delivery_minutes": round(mean([r.delivery_time_minutes for r in delivered]), 1) if delivered else None,
            "total_recovery_steps": sum(r.recovery_steps for r in results),
        },
        "normalized": {
            "resilience": round(resilience, 4),
            "deadline": round(deadline, 4),
            "cost": round(cost_component, 4),
            "verification": round(verification, 4),
        },
        "weights": w,
        "score": round(total, 4),
        "hard_disqualifications": hard_disqualifications,
        "per_fault": [asdict(r) for r in results],
    }


def rank_tournament(plans, order, results_by_plan: dict[str, list[SimResult]]) -> dict:
    """Score every plan and return the ranked tournament outcome + champion."""
    all_results = [r for rs in results_by_plan.values() for r in rs]
    scored = []
    for p in plans:
        rs = results_by_plan.get(p.plan_id, [])
        scored.append(score_plan(p.plan_id, p.objective, rs, order, all_results))

    # a plan disqualified on EVERY fault case cannot win
    def eligible(s: dict) -> bool:
        return s["raw_metrics"]["delivered_cases"] > 0

    ranked = sorted(scored, key=lambda s: (eligible(s), s["score"]), reverse=True)
    champion = ranked[0] if ranked and eligible(ranked[0]) else None
    return {
        "normalization_rules": {
            "resilience": "delivered_cases / fault_cases",
            "deadline": "on_time_delivered / delivered_cases",
            "cost": "mean over delivered of (hi - cost)/(hi - lo) across all feasible branches",
            "verification": "invariant_ok_cases / fault_cases",
            "weights": settings.SCORE_WEIGHTS,
            "policy_note": "weights are a demo policy, not a proven optimum",
        },
        "ranked": ranked,
        "champion_plan_id": champion["plan_id"] if champion else None,
        "champion_explanation": _explain(champion) if champion else "no eligible plan",
    }


def _explain(s: dict) -> str:
    nm = s["normalized"]
    return (f"Plan {s['plan_id']} ({s['objective']}) wins with score {s['score']}: "
            f"resilience {nm['resilience']}, deadline {nm['deadline']}, "
            f"cost {nm['cost']}, verification {nm['verification']} "
            f"across {s['raw_metrics']['fault_cases']} shared fault cases.")


def cheapest_first_baseline(plans, order, results_by_plan: dict[str, list[SimResult]]) -> dict:
    """Baseline: pick the plan with the lowest estimated initial cost, then
    report how it fared on the identical fault suite."""
    baseline_plan = min(plans, key=lambda p: p.estimated_cost_inr)
    rs = results_by_plan.get(baseline_plan.plan_id, [])
    all_results = [r for plist in results_by_plan.values() for r in plist]
    return {
        "method": "cheapest_first",
        "selected_plan_id": baseline_plan.plan_id,
        "selection_reason": f"lowest estimated_cost_inr ({baseline_plan.estimated_cost_inr})",
        "score": score_plan(baseline_plan.plan_id, baseline_plan.objective, rs,
                            order, all_results),
    }
