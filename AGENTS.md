# AGENTS.md — notes for coding/review agents

Short orientation for an AI agent working on this repo.

## Golden rule

Keep the **LLM vs code** boundary intact (see `ARCHITECTURE.md`). LLMs propose;
code decides. Never move database writes, simulation, scoring, safety checks, or
assertions into an LLM. Never fabricate run results, passing logs, or evidence —
only actual executed output counts as proof.

## Where things live

- Agent definitions + runner: `paradox/adk_agents.py` (real ADK `LlmAgent`).
- MCP tools: `paradox/mcp_server.py` (mcp 2.3 `MCPServer`, stdio).
- State + state machine: `paradox/storage.py`.
- Orchestration: `paradox/controller.py`.
- CLI: `paradox/__main__.py`.

## Gotchas (already handled — don't regress)

- Gemini Developer API structured output rejects `additionalProperties` and
  `exclusiveMinimum`. Agent **output** schemas must avoid free-form `dict`
  fields and `Field(gt=...)`; use `ge`/`le` + validators (see `schemas.py`).
- `mcp` 2.x renamed `FastMCP` → `MCPServer` (`mcp.server.mcpserver`).
- Dated model ids can be retired for new keys; default to `gemini-flash-latest`.
- `InMemoryRunner.create_session` is async.

## Running

```bash
source .venv/bin/activate
python -m paradox doctor
pytest -q                       # no key needed
python -m paradox demo --json-output artifacts/demo.json   # needs GOOGLE_API_KEY
python -m paradox validate --report artifacts/demo.json
```

## Integrity checklist before claiming "done"

- [ ] All modules import and byte-compile.
- [ ] `doctor` spawns the MCP server and lists tools.
- [ ] Any "it works" claim is backed by real command output, not description.
- [ ] No secrets committed; `.env` is gitignored.
