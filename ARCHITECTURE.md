# ARCHITECTURE — components, trust boundaries, decisions

## Trust boundary: LLM vs code

The central design rule (from the spec) is a hard separation:

- **LLM-backed (ADK/Gemini), may be creative/uncertain:** World Builder,
  Strategy, Red/Chaos, Blue/Recovery. Their outputs are *proposals*.
- **Deterministic code, authoritative:** Mission Controller (control flow),
  Storage/SQLite (state + legal transitions + idempotency + versioning),
  Counterfactual Twin (simulation), Scorer (ranking), Policy gate (feasibility,
  distinctness, fault safety), Recovery Verifier (postconditions).

An LLM is **never** the database, simulator, scoring oracle, safety gate, or
assertion engine. Every LLM proposal passes a code gate before it can affect
state or scoring.

## Components

| Module | Role | LLM? |
|---|---|---|
| `paradox/schemas.py` | Pydantic contracts (domain + agent I/O) | no |
| `paradox/settings.py` | config, model id, policy constants | no |
| `paradox/storage.py` | SQLite (WAL, FKs, tx), state machine, events, feedback, snapshot/clone | no |
| `paradox/mcp_server.py` | MCP server (stdio) exposing 19 logistics tools | no |
| `paradox/mcp_client.py` | stdio MCP client + redacted tool-call trace | no |
| `paradox/adk_agents.py` | real ADK `LlmAgent`s + structured-output runner | **yes** |
| `paradox/policies.py` | feasibility, distinctness, fault safety, SPOF | no |
| `paradox/simulator.py` | counterfactual digital twin (isolated forks) | no |
| `paradox/scoring.py` | normalized weighted scoring + baseline | no |
| `paradox/recovery.py` | independent recovery/postcondition verification | no |
| `paradox/controller.py` | mission state machine + trace assembly | no |
| `paradox/reporting.py` | demo.json + demo_summary.md + tournament.json | no |
| `paradox/evals.py` | cheapest-first vs PARADOX benchmark | no |
| `paradox/__main__.py` | CLI: doctor / demo / benchmark / validate | no |

## Key decisions & tradeoffs

- **Structured output via `LlmAgent(output_schema=...)`.** Each agent returns a
  Pydantic-validated JSON object, eliminating fragile text parsing. One bounded
  content retry on invalid output, then honest failure.
- **Gemini Developer API schema subset.** Key-mode structured output rejects
  `additionalProperties` and `exclusiveMinimum`. So agent output schemas carry
  no free-form `dict` fields, and numeric bounds use `minimum`/`maximum`
  (`ge`/`le`) with strict positivity enforced by validators. See `schemas.py`.
- **Model resilience.** Dated model ids (e.g. `gemini-2.5-flash`) can be retired
  for newer API keys; the default is the stable `gemini-flash-latest` alias, and
  the runner falls back across candidate models and retries transient 503/429
  with backoff.
- **Real MCP transport, no bypass.** The controller drives execution tools
  through the stdio MCP subprocess (`mcp_client`), and the recovery path reads
  candidates and calls `reassign_driver` over MCP. `doctor` proves the server
  spawns and lists tools.
- **Counterfactual isolation.** The twin deep-copies the world document into
  in-memory structures; it never opens the real DB for writes. The controller
  asserts the baseline world hash + row versions are unchanged after the
  tournament. The read-only `simulate_plan` idea is implemented as the twin, not
  a mutating server tool, to make the "never touches execution state" guarantee
  structural.
- **Idempotency + versioning.** Purchases and reservations carry unique
  idempotency keys; order transitions are version-checked (optimistic
  concurrency). Retried tool calls do not double-apply.
- **Provenance.** Live worlds are always agent-generated and stamped with model,
  time, prompt version, and a content hash. Code fixtures live only in `tests/`
  and are labeled `test_fixture` — never presented as generated data.

## Data & state

SQLite with foreign keys and WAL. Tables: worlds (immutable snapshot + hash),
products, warehouses, inventory (versioned), suppliers, drivers, routes, orders
(versioned, status), reservations, purchase_orders, faults, recovery_attempts,
feedback, events (full provenance fields). The original seeded world snapshot is
immutable; mutations happen on live order/inventory/driver rows only.

## Legal state machine

```
CREATED → STOCK_CHECKED → [PROCUREMENT_REQUIRED → PROCUREMENT_COMPLETED]
        → RESERVED → PACKED → DISPATCHED → DELIVERED → VERIFIED
Exceptional: FAILED → RECOVERING → (resume legal step | ESCALATED)
```

Transitions are validated against `settings.LEGAL_TRANSITIONS`; illegal jumps
raise `StateError`.
