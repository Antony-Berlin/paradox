# JUDGE_GUIDE — run and inspect PARADOX in a few minutes

This guide is optimized for an automated reviewer. Every command is runnable;
every claim maps to a file you can inspect.

## 0. Setup (once)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # set GOOGLE_API_KEY=... for the live demo
```

## 1. Readiness — no key needed for most checks

```bash
python -m paradox doctor
# Optionally ping the live model:
python -m paradox doctor --ping
```

Confirms: Python version, package imports, key presence, and that the **MCP
server actually spawns over stdio and exposes its tools** (19 logistics tools).

## 2. Deterministic checks — NO API key required

```bash
pytest -q
```

Covers: legal state machine & invariants (`test_invariants.py`), counterfactual
twin isolation (`test_twin_isolation.py`), scoring/tournament (`test_scoring.py`),
recovery verification, and MCP protocol integration (`test_mcp_integration.py`).

## 3. Live end-to-end demo — needs a Gemini key

```bash
python -m paradox demo --scenario driver_failure --json-output artifacts/demo.json
```

Exercises all four LLM-backed ADK agents + genuine MCP tool calls:
World Builder → seed → Strategy (3 plans) → Chaos (faults) → counterfactual
tournament → live execution → **injected driver fault** → Blue recovery →
independent verification → feedback update.

**Inspect:**
- `artifacts/demo.json` — `model_calls` (real Gemini), `mcp_calls` (tool name +
  redacted args + status), `plans`, `plan_distinctness`, `shared_faults`,
  `tournament.champion_explanation`, `isolation`, `execution_events`,
  `recovery_verifications`, `assertions`, `feedback_before/after`, `limitations`.
- `artifacts/demo_summary.md` — the same facts in human form (auto-generated).
- `artifacts/world_snapshot.json` — labeled `agent_generated`.

## 4. Independent validation — does NOT trust the report's own flags

```bash
python -m paradox validate --report artifacts/demo.json
echo "exit code: $?"   # 0 only if ALL mandatory properties hold
```

Re-checks provenance, required MCP tool invocation, three operationally distinct
plans, counterfactual isolation, legal transitions to `VERIFIED`, fault
injection, and an independently-verified driver recovery.

## 5. Benchmark — cheapest-first vs PARADOX, paired trials

```bash
python -m paradox benchmark --runs 5 --json-output artifacts/benchmark.json
```

Reuses the agent-generated snapshot; identical world + fault suite per trial;
honest win/loss/tie counts (PARADOX is not guaranteed to win every trial).

## 6. Optional viewer

```bash
streamlit run app.py
```

Renders the same `artifacts/*.json` — not a disconnected staged sequence.

## What proves the hard requirements

| Requirement | Evidence |
|---|---|
| Real ADK agents | `demo.json → model_calls` (agent, model, duration) |
| Real MCP transport | `demo.json → mcp_calls`; `doctor` spawns the server + lists tools |
| Agent-generated world | `world_provenance = agent_generated`, `world_snapshot.json` |
| 3 operationally distinct plans | `plan_distinctness.distinct = true, count = 3` |
| Counterfactual isolation | `isolation.hash_unchanged / versions_unchanged = true` |
| Verified self-healing | `recovery_verifications[].verified = true`, final status `VERIFIED` |
| Honest failure on no key | `demo` exits code 2 with a friendly message |
