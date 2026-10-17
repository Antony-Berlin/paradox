"""Report assembly: machine-readable JSON + human-readable Markdown.

Serializes a MissionTrace into artifacts/demo.json (the full evidence envelope)
and generates artifacts/demo_summary.md from the SAME facts -- the Markdown is
derived, never hand-authored claims. Also writes tournament.json and the
benchmark report. `validate` (in __main__) re-reads these independently.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from . import settings


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def build_demo_report(trace) -> dict:
    t = trace
    all_assertions_ok = all(a["ok"] for a in t.assertions) if t.assertions else False
    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "run_id": t.run_id,
        "scenario": t.scenario,
        "world_id": t.world_id,
        "world_hash": t.world_hash,
        "world_provenance": t.world_provenance,
        "model": settings.MODEL_ID,
        "model_calls": t.model_calls,
        "mcp_calls": t.mcp_calls,
        "plans": t.plans,
        "plan_feasibility": t.plan_feasibility,
        "plan_distinctness": t.plan_distinctness,
        "shared_faults": t.shared_faults,
        "tournament": t.tournament,
        "baseline": t.baseline,
        "isolation": t.isolation,
        "execution_events": t.execution_events,
        "recovery_verifications": t.recovery_verifications,
        "assertions": t.assertions,
        "feedback_before": t.feedback_before,
        "feedback_after": t.feedback_after,
        "final_order_status": t.final_order_status,
        "all_assertions_passed": all_assertions_ok,
        "limitations": t.limitations + [
            "Simulated logistics only: no real purchase or courier dispatch.",
            "Scoring weights are a demo policy, not a proven optimum.",
            "'Reliability' is smoothed operational memory, not model training.",
        ],
        "errors": t.errors,
    }


def write_report(report: dict, path: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str))
    return p


def write_demo_summary(report: dict, json_path: str) -> Path:
    """Generate demo_summary.md from report facts."""
    jp = Path(json_path)
    md_path = jp.with_name(jp.stem + "_summary.md")
    champ = report.get("tournament", {}).get("champion_plan_id")
    champ_expl = report.get("tournament", {}).get("champion_explanation", "")
    baseline = report.get("baseline", {})
    n_models = len(report.get("model_calls", []))
    n_mcp = len(report.get("mcp_calls", []))
    asserts = report.get("assertions", [])
    passed = sum(1 for a in asserts if a["ok"])

    lines = [
        f"# PARADOX demo summary — run `{report['run_id']}`",
        "",
        f"- **Scenario:** {report['scenario']}",
        f"- **World:** `{report['world_id']}` (provenance: {report['world_provenance']}, "
        f"hash `{(report.get('world_hash') or '')[:16]}`)",
        f"- **Model:** {report['model']}",
        f"- **LLM agent calls:** {n_models}  •  **MCP tool calls:** {n_mcp}",
        f"- **Final order status:** **{report['final_order_status']}**",
        f"- **Assertions:** {passed}/{len(asserts)} passed "
        f"({'ALL PASS' if report['all_assertions_passed'] else 'SOME FAILED'})",
        "",
        "## Counterfactual tournament",
        f"- **Champion:** `{champ}`",
        f"- {champ_expl}",
        f"- **Cheapest-first baseline:** `{baseline.get('selected_plan_id')}` "
        f"(score {baseline.get('score', {}).get('score')})",
        "",
        "### Plans scored (shared fault suite)",
        "| plan | objective | score | delivered | mean cost | mean ETA |",
        "|---|---|---|---|---|---|",
    ]
    for s in report.get("tournament", {}).get("ranked", []):
        rm = s["raw_metrics"]
        lines.append(
            f"| {s['plan_id']} | {s['objective']} | {s['score']} | "
            f"{rm['delivered_cases']}/{rm['fault_cases']} | "
            f"{rm['mean_cost_inr']} | {rm['mean_delivery_minutes']} |")

    lines += ["", "## Isolation (counterfactual twin)",
              f"- world hash unchanged: **{report['isolation'].get('hash_unchanged')}**",
              f"- row versions unchanged: **{report['isolation'].get('versions_unchanged')}**",
              "", "## Recovery verifications"]
    for rv in report.get("recovery_verifications", []):
        lines.append(f"- {rv.get('type')}: verified=**{rv.get('verified')}** "
                     f"(verifier `{rv.get('verifier_id')}`)")

    lines += ["", "## Assertions"]
    for a in asserts:
        mark = "✅" if a["ok"] else "❌"
        lines.append(f"- {mark} `{a['assertion']}` — {a.get('detail','')}")

    lines += ["", "## Limitations"]
    for lim in report.get("limitations", []):
        lines.append(f"- {lim}")
    lines.append("")

    md_path.write_text("\n".join(lines))
    return md_path


def write_tournament(report: dict) -> Path:
    settings.ensure_artifacts_dir()
    p = settings.ARTIFACTS_DIR / "tournament.json"
    p.write_text(json.dumps({
        "run_id": report["run_id"], "world_id": report["world_id"],
        "plans": report["plans"], "shared_faults": report["shared_faults"],
        "tournament": report["tournament"], "baseline": report["baseline"],
    }, indent=2, default=str))
    return p
