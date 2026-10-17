"""PARADOX command-line entry point.

    python -m paradox doctor
    python -m paradox demo --scenario driver_failure --json-output artifacts/demo.json
    python -m paradox benchmark --runs 5 --json-output artifacts/benchmark.json
    python -m paradox validate --report artifacts/demo.json

`doctor` and `validate` and `pytest` run WITHOUT a Gemini key. `demo` and
`benchmark` are the live, LLM-backed paths. Exit code is nonzero when a mandatory
acceptance property fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import settings


# ============================================================================
# doctor
# ============================================================================
def cmd_doctor(args) -> int:
    print("PARADOX doctor\n" + "=" * 40)
    ok = True

    # python version
    v = sys.version_info
    pyok = v >= (3, 10)
    print(f"[{'OK' if pyok else 'XX'}] Python {v.major}.{v.minor}.{v.micro} (need >=3.10)")
    ok &= pyok

    # package imports
    for mod in ("google.adk", "mcp", "pydantic"):
        try:
            __import__(mod)
            print(f"[OK] import {mod}")
        except Exception as e:  # noqa: BLE001
            print(f"[XX] import {mod}: {e}")
            ok = False

    # key presence
    if settings.has_api_key():
        print(f"[OK] GOOGLE_API_KEY present; model = {settings.MODEL_ID}")
    else:
        print("[--] GOOGLE_API_KEY NOT set — deterministic tests & doctor still run; "
              "`demo`/`benchmark` will exit with a friendly error.")

    # MCP server spawn + tool discovery (no key needed)
    async def _mcp_check() -> bool:
        from .mcp_client import ParadoxMcpClient
        import tempfile
        db = tempfile.mktemp(suffix=".db")
        try:
            async with ParadoxMcpClient(db) as c:
                tools = await c.list_tools()
            print(f"[OK] MCP server spawned over stdio; {len(tools)} tools discovered")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[XX] MCP server check failed: {e}")
            return False
        finally:
            Path(db).unlink(missing_ok=True)

    ok &= asyncio.run(_mcp_check())

    # optional: live model ping
    if args.ping and settings.has_api_key():
        try:
            from google import genai
            client = genai.Client(api_key=settings.api_key())
            r = client.models.generate_content(
                model=settings.MODEL_ID, contents="Reply with the single word OK")
            print(f"[OK] model ping: {(r.text or '').strip()[:20]}")
        except Exception as e:  # noqa: BLE001
            print(f"[--] model ping failed (non-fatal): {str(e)[:120]}")

    print("=" * 40)
    print("doctor:", "ALL GOOD" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


# ============================================================================
# demo
# ============================================================================
def cmd_demo(args) -> int:
    from .adk_agents import MissingApiKeyError
    if not settings.has_api_key():
        from .adk_agents import FRIENDLY_KEY_MSG
        print(FRIENDLY_KEY_MSG, file=sys.stderr)
        return 2

    from .controller import MissionController
    from .mcp_client import ParadoxMcpClient
    from . import reporting

    settings.ensure_artifacts_dir()
    db_path = args.db or settings.DEFAULT_DB_PATH
    # fresh DB for a clean live demo unless replaying
    if not args.world_snapshot and Path(db_path).exists():
        Path(db_path).unlink()

    async def _run() -> dict:
        ctrl = MissionController(db_path, scenario=args.scenario, seed=args.seed)
        async with ParadoxMcpClient(db_path) as mcp:
            if args.world_snapshot:
                import json as _json
                snap = _json.loads(Path(args.world_snapshot).read_text())
                from .schemas import World
                w = World.model_validate(snap)
                await mcp.call("seed_world", {"world_json": w.model_dump_json()})
                world = await ctrl.load_world(w.world_id)
            else:
                world = await ctrl.build_and_seed_world(mcp)
                # persist the agent-generated snapshot for replay/benchmark
                snap_path = settings.WORLD_SNAPSHOT_PATH
                snap_path.write_text(world.model_dump_json(indent=2))
            plans = await ctrl.propose_plans(world)
            faults = await ctrl.propose_faults(world, plans)
            champion = ctrl.run_tournament(world, plans, faults)
            await ctrl.execute_with_recovery(mcp, world, champion)
            ctrl.trace.mcp_calls = mcp.trace
        # stash plans+faults for benchmark reuse
        (settings.ARTIFACTS_DIR / "last_tournament_inputs.json").write_text(
            json.dumps({"world_id": world.world_id,
                        "plans": [p.model_dump() for p in plans],
                        "faults": [f.model_dump() for f in faults]}, default=str))
        return reporting.build_demo_report(ctrl.trace)

    try:
        report = asyncio.run(_run())
    except MissingApiKeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - surface honestly
        print(f"demo failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3

    out = args.json_output or str(settings.ARTIFACTS_DIR / "demo.json")
    reporting.write_report(report, out)
    reporting.write_demo_summary(report, out)
    reporting.write_tournament(report)
    print(f"demo report -> {out}")
    print(f"final order status: {report['final_order_status']}")
    print(f"assertions: {sum(a['ok'] for a in report['assertions'])}/"
          f"{len(report['assertions'])} passed")
    return 0 if report["all_assertions_passed"] \
        and report["final_order_status"] == "VERIFIED" else 4


# ============================================================================
# benchmark
# ============================================================================
def cmd_benchmark(args) -> int:
    from . import evals
    from .schemas import Fault, Plan

    db_path = args.db or settings.DEFAULT_DB_PATH
    inputs_path = settings.ARTIFACTS_DIR / "last_tournament_inputs.json"
    if not inputs_path.exists():
        print("No tournament inputs found. Run `demo` first (it saves an "
              "agent-generated world + plans + faults for replay).", file=sys.stderr)
        return 2
    data = json.loads(inputs_path.read_text())
    plans = [Plan.model_validate(p) for p in data["plans"]]
    faults = [Fault.model_validate(f) for f in data["faults"]]
    report = evals.run_benchmark(db_path, data["world_id"], plans, faults,
                                 runs=args.runs)
    out = args.json_output or str(settings.ARTIFACTS_DIR / "benchmark.json")
    evals.write_benchmark(report, out)
    print(f"benchmark report -> {out}")
    print(f"PARADOX wins {report['paradox_wins']} / baseline {report['baseline_wins']} "
          f"/ ties {report['ties']} over {report['runs']} paired trials")
    return 0


# ============================================================================
# validate  (independent re-check; does NOT trust success flags)
# ============================================================================
def cmd_validate(args) -> int:
    path = Path(args.report)
    if not path.exists():
        print(f"report not found: {path}", file=sys.stderr)
        return 2
    report = json.loads(path.read_text())
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # 1. provenance
    chk("world_provenance_present",
        report.get("world_provenance") in ("agent_generated", "replay_snapshot"),
        report.get("world_provenance", ""))
    chk("world_hash_present", bool(report.get("world_hash")))

    # 2. real LLM + MCP activity
    chk("llm_agent_calls_present", len(report.get("model_calls", [])) >= 3,
        f"{len(report.get('model_calls', []))} model calls")
    mcp_calls = report.get("mcp_calls", [])
    tool_names = {c["tool"] for c in mcp_calls}
    required_tools = {"seed_world", "reserve_inventory", "pack_order",
                      "assign_driver", "inject_execution_fault",
                      "reassign_driver", "verify_order"}
    chk("required_mcp_tools_invoked", required_tools.issubset(tool_names),
        f"missing: {sorted(required_tools - tool_names)}")

    # 3. three operationally distinct plans
    chk("three_distinct_plans",
        report.get("plan_distinctness", {}).get("distinct") is True
        and report.get("plan_distinctness", {}).get("count") == 3,
        json.dumps(report.get("plan_distinctness", {})))

    # 4. counterfactual isolation
    iso = report.get("isolation", {})
    chk("counterfactual_isolation",
        iso.get("hash_unchanged") is True and iso.get("versions_unchanged") is True)

    # 5. legal state transitions to VERIFIED
    events = report.get("execution_events", [])
    transitions = [e["event_type"] for e in events
                   if str(e.get("event_type", "")).startswith("transition:")]
    chk("reached_delivered", any("->DELIVERED" in t for t in transitions))
    chk("reached_verified", any("->VERIFIED" in t for t in transitions))
    chk("final_status_verified", report.get("final_order_status") == "VERIFIED",
        report.get("final_order_status", ""))

    # 6. fault injected AND recovery verified independently
    chk("fault_injected",
        any(e.get("event_type") == "fault_injected" for e in events))
    rvs = report.get("recovery_verifications", [])
    chk("driver_recovery_verified",
        any(rv.get("type") == "driver" and rv.get("verified") is True for rv in rvs))

    # 7. recovery action persisted
    chk("recovery_action_persisted",
        any(e.get("event_type") == "recovery_action" for e in events))

    print("PARADOX validate — independent re-check of", path.name)
    print("=" * 50)
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  — {detail}" if detail and not ok else ""))
    print("=" * 50)
    print("validate:", "ALL MANDATORY PROPERTIES HOLD" if all_ok else "VALIDATION FAILED")
    return 0 if all_ok else 1


# ============================================================================
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="paradox", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="environment & MCP readiness checks")
    p_doc.add_argument("--ping", action="store_true", help="also ping the live model")
    p_doc.set_defaults(func=cmd_doctor)

    p_demo = sub.add_parser("demo", help="run the full live tournament + recovery")
    p_demo.add_argument("--scenario", default="driver_failure")
    p_demo.add_argument("--json-output", default=None)
    p_demo.add_argument("--db", default=None)
    p_demo.add_argument("--world-snapshot", default=None,
                        help="replay from a previously agent-generated snapshot")
    p_demo.add_argument("--seed", type=int, default=settings.DEFAULT_SIM_SEED)
    p_demo.set_defaults(func=cmd_demo)

    p_bench = sub.add_parser("benchmark", help="cheapest-first vs PARADOX, N trials")
    p_bench.add_argument("--runs", type=int, default=5)
    p_bench.add_argument("--json-output", default=None)
    p_bench.add_argument("--db", default=None)
    p_bench.set_defaults(func=cmd_benchmark)

    p_val = sub.add_parser("validate", help="independently re-check a demo report")
    p_val.add_argument("--report", required=True)
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
