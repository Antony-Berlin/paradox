"""Scoring & tournament tests (no LLM key needed)."""

from __future__ import annotations

from paradox import scoring
from paradox.schemas import Fault, Plan
from paradox.simulator import CounterfactualTwin
from tests.test_twin_isolation import _faults, _plans


def _results(fixture_world, seed=7):
    twin = CounterfactualTwin(fixture_world, seed=seed)
    plans = _plans()
    faults = _faults()
    return plans, {p.plan_id: [twin.simulate(p, f, "ORD_1") for f in faults]
                   for p in plans}


def test_rank_produces_eligible_champion(fixture_world):
    plans, rbp = _results(fixture_world)
    order = fixture_world.orders[0]
    outcome = scoring.rank_tournament(plans, order, rbp)
    assert outcome["champion_plan_id"] is not None
    assert outcome["ranked"][0]["score"] >= outcome["ranked"][-1]["score"]


def test_weights_sum_to_one():
    from paradox import settings
    assert abs(sum(settings.SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_score_breakdown_has_components(fixture_world):
    plans, rbp = _results(fixture_world)
    order = fixture_world.orders[0]
    outcome = scoring.rank_tournament(plans, order, rbp)
    s = outcome["ranked"][0]
    for k in ("resilience", "deadline", "cost", "verification"):
        assert 0.0 <= s["normalized"][k] <= 1.0


def test_cheapest_first_baseline_selects_lowest_estimate(fixture_world):
    plans, rbp = _results(fixture_world)
    order = fixture_world.orders[0]
    baseline = scoring.cheapest_first_baseline(plans, order, rbp)
    # cheapest_1 has the lowest estimated_cost_inr (295)
    assert baseline["selected_plan_id"] == "cheapest_1"


def test_disqualified_plan_cannot_win(fixture_world):
    """A plan disqualified on every fault case must not be champion."""
    order = fixture_world.orders[0]
    twin = CounterfactualTwin(fixture_world, seed=7)
    # a plan pointing every line at an impossible supplier qty -> disqualified
    broken = Plan(plan_id="broken", objective="cheapest",
                  lines=[{"sku": "SKU_1", "qty": 4, "warehouse_id": "WH_2",
                          "supplier_id": "SUP_1", "procure_qty": 999999}],
                  driver_id="DRV_2", route_id="R_1", estimated_cost_inr=1,
                  estimated_eta_minutes=1, reason_summary="broken")
    plans = _plans() + [broken]
    faults = _faults()
    rbp = {p.plan_id: [twin.simulate(p, f, "ORD_1") for f in faults] for p in plans}
    outcome = scoring.rank_tournament(plans, order, rbp)
    assert outcome["champion_plan_id"] != "broken"
