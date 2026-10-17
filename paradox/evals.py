"""Benchmark harness: cheapest-first baseline vs PARADOX policy.

Reuses a legitimately agent-generated world snapshot and runs N paired trials.
Each trial re-runs the deterministic counterfactual tournament against the
IDENTICAL world + fault suite but with a different simulator seed (so fault
trials vary) -- cheapest-first and PARADOX see the SAME conditions each trial.
No new LLM world generation here; faults can be reused from a prior demo trace
or regenerated once. Honest: PARADOX is not guaranteed to win every trial.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import scoring, settings
from .schemas import Fault, Plan, World
from .simulator import CounterfactualTwin
from .storage import Storage


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def run_benchmark(db_path: str, world_id: str, plans: list[Plan],
                  faults: list[Fault], runs: int = 5) -> dict:
    storage = Storage(db_path)
    snap = storage.get_world_snapshot(world_id)
    snap.pop("_world_hash", None)
    world = World.model_validate(snap)
    order = world.orders[0]

    hash_before = storage.world_hash(world_id)
    trials = []
    paradox_wins = 0
    baseline_wins = 0
    ties = 0

    for i in range(runs):
        seed = settings.DEFAULT_SIM_SEED + i * 101
        twin = CounterfactualTwin(world, seed=seed)
        results_by_plan = {
            p.plan_id: [twin.simulate(p, f, order.order_id) for f in faults]
            for p in plans}
        outcome = scoring.rank_tournament(plans, order, results_by_plan)
        baseline = scoring.cheapest_first_baseline(plans, order, results_by_plan)
        p_score = outcome["ranked"][0]["score"] if outcome["ranked"] else 0.0
        b_score = baseline["score"]["score"]
        if p_score > b_score:
            paradox_wins += 1
            winner = "paradox"
        elif b_score > p_score:
            baseline_wins += 1
            winner = "baseline"
        else:
            ties += 1
            winner = "tie"
        trials.append({
            "trial": i, "seed": seed,
            "paradox_champion": outcome["champion_plan_id"],
            "paradox_score": p_score,
            "baseline_plan": baseline["selected_plan_id"],
            "baseline_score": b_score,
            "winner": winner,
        })

    hash_after = storage.world_hash(world_id)
    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "world_id": world_id,
        "world_hash": hash_before,
        "runs": runs,
        "method_a": "cheapest_first",
        "method_b": "paradox_counterfactual",
        "paradox_wins": paradox_wins,
        "baseline_wins": baseline_wins,
        "ties": ties,
        "paired_identical_conditions": True,
        "isolation_hash_unchanged": hash_before == hash_after,
        "trials": trials,
        "limitations": [
            "Same agent-generated world snapshot reused across trials (replay).",
            "Only simulator fault-trial seed varies per trial; faults identical.",
            "No guarantee PARADOX wins every trial; results are honest counts.",
        ],
    }


def write_benchmark(report: dict, path: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str))
    # markdown summary
    md = p.with_name(p.stem + "_summary.md")
    lines = [
        f"# PARADOX benchmark — {report['runs']} paired trials",
        "",
        f"- World: `{report['world_id']}` (hash `{report['world_hash'][:16]}`)",
        f"- Paired identical conditions: {report['paired_identical_conditions']}",
        f"- Isolation hash unchanged: {report['isolation_hash_unchanged']}",
        "",
        f"**PARADOX wins: {report['paradox_wins']}  •  "
        f"Cheapest-first wins: {report['baseline_wins']}  •  "
        f"Ties: {report['ties']}**",
        "",
        "| trial | seed | paradox champ | paradox | baseline | baseline score | winner |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in report["trials"]:
        lines.append(
            f"| {t['trial']} | {t['seed']} | {t['paradox_champion']} | "
            f"{t['paradox_score']} | {t['baseline_plan']} | {t['baseline_score']} | "
            f"{t['winner']} |")
    lines += ["", "## Limitations"] + [f"- {x}" for x in report["limitations"]] + [""]
    md.write_text("\n".join(lines))
    return p
