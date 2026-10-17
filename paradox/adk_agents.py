"""Actual Google ADK agent definitions and the structured-output runner.

Five LLM-backed ADK agents (Gemini): World Builder, Strategy, Chaos/Red,
Recovery/Blue, and a thin Execution Coordinator. Each non-execution agent uses
a Pydantic `output_schema` so the model emits validated JSON (no fragile text
parsing). The Execution agent is given the live MCP toolset so its logistics
actions are genuine protocol tool calls.

If no API key is configured, `run_structured_agent` raises a friendly
`MissingApiKeyError` -- the deterministic tests do not import or need this path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, Type

from pydantic import BaseModel

from . import settings
from .schemas import (ChaosOutput, RecoveryChoice, StrategyOutput,
                      WorldBuilderOutput)


class MissingApiKeyError(RuntimeError):
    """Raised when a live agent run is attempted without GOOGLE_API_KEY."""


FRIENDLY_KEY_MSG = (
    "No GOOGLE_API_KEY found. PARADOX's live agent demo needs a Gemini key.\n"
    "  1. Get a free key: https://aistudio.google.com/apikey\n"
    "  2. cp .env.example .env  and set GOOGLE_API_KEY=...\n"
    "Deterministic tests (pytest) and `doctor` run WITHOUT a key."
)


# --- Agent instructions (evidence summaries, never private chain of thought) -
WORLD_BUILDER_INSTRUCTION = """\
You are the World Builder for a SIMULATED logistics system in a fictional metro.
Generate a coherent, relationally-consistent, production-shaped synthetic world.
Constraints:
- 8-12 products (varied categories; a few cold_chain_required), 2 warehouses,
  3-4 suppliers, 4-6 drivers, 2-3 zones, exactly 1 order.
- Inventory MUST be deliberately tight for the order: at least one SKU in the
  order should require procurement (available < ordered qty) so the pipeline
  exercises purchasing. available = on_hand - reserved must never be negative.
- Every order line SKU must exist; suppliers must list real SKUs; driver and
  warehouse zones must cover the order's delivery_zone; routes originate at a
  real warehouse. Probabilities in [0,1]. Costs/ETAs plausible relative to each
  other. Currency INR. No real names, phone numbers, or places.
- Provide at least TWO drivers in the order's delivery zone (so recovery has a
  valid alternative) and at least TWO suppliers for the procured SKU.
Return ONLY the structured fields. Do not invent fields beyond the schema.
"""

STRATEGY_INSTRUCTION = """\
You are the Strategy agent. Given the world snapshot and one order, produce
EXACTLY three OPERATIONALLY DISTINCT fulfillment plans with objectives
'cheapest', 'fastest', and 'resilience'. Distinctness is judged on operational
choices (supplier/warehouse/driver/backup/route), NOT on labels, so the three
plans must differ in those IDs:
- cheapest: minimize total cost (cheapest supplier + driver), may omit a backup.
- fastest: minimize ETA (fastest driver/route), may omit a backup.
- resilience: prefer reliable supplier/driver AND include a backup_driver_id
  distinct from the primary; remove single points of failure where possible.
Every plan must fully cover the order quantity, use only IDs present in the
world, fit driver capacity, and set procure_qty where on-hand is insufficient.
Give a one-line reason_summary per plan (evidence, not inner monologue).
"""

CHAOS_INSTRUCTION = """\
You are the Red/Chaos agent. Given the three candidate plans and the world,
propose 2-3 TARGETED, plausible fault cases drawn ONLY from:
supplier_down, driver_cancelled, stock_discrepancy. Aim each fault at the
single-point-of-failure dependency of a plan (e.g. the primary driver of a plan
with no backup, or the only supplier for a procured SKU). For each fault set:
- kind, target_id (driver_id / supplier_id / 'warehouse_id:sku'),
- at_stage: a legal order state where it fires (PACKED or DISPATCHED for driver,
  PROCUREMENT_REQUIRED for supplier, STOCK_CHECKED for stock),
- a one-line reason_summary naming which plan dependency it attacks.
Faults must target real IDs present in the world. Keep them bounded.
"""

RECOVERY_INSTRUCTION = """\
You are the Blue/Recovery agent. A real execution fault has occurred. You are
given the failure, a read-only snapshot of valid alternatives (available drivers
in-zone with sufficient capacity, or alternate suppliers for the SKU), and the
constraints. Choose ONE recovery action:
- reassign_driver with a new_driver_id that is available, in the correct zone,
  has capacity, and is NOT the failed driver;
- switch_supplier with a new_supplier_id that supplies the SKU and is available;
- escalate only if no valid alternative exists.
Return the action and a one-line reason_summary citing the chosen candidate.
Never choose an ID that is not in the provided candidate list.
"""


def _require_key() -> None:
    if not settings.has_api_key():
        raise MissingApiKeyError(FRIENDLY_KEY_MSG)


# Model fallback order: configured model first, then stable aliases. If the
# primary is overloaded (503) or retired (404) we transparently try the next.
def _model_candidates() -> list[str]:
    cands = [settings.MODEL_ID]
    for alt in ("gemini-flash-latest", "gemini-3.8-flash", "gemini-2.5-flash"):
        if alt not in cands:
            cands.append(alt)
    return cands


_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                      "high demand", "overloaded")
_RETIRED_MARKERS = ("404", "NOT_FOUND", "no longer available")


def build_llm_agent(name: str, instruction: str,
                    output_schema: Optional[Type[BaseModel]] = None,
                    tools: Optional[list] = None, model: Optional[str] = None):
    """Construct a real ADK LlmAgent. Imported lazily so non-LLM paths stay light."""
    from google.adk.agents import LlmAgent

    kwargs = dict(name=name, model=model or settings.MODEL_ID, instruction=instruction)
    if output_schema is not None:
        kwargs["output_schema"] = output_schema
        # structured-output agents cannot also transfer/use tools in ADK
        kwargs["disallow_transfer_to_parent"] = True
        kwargs["disallow_transfer_to_peers"] = True
    if tools:
        kwargs["tools"] = tools
    return LlmAgent(**kwargs)


async def _run_once(agent_name: str, instruction: str,
                    output_schema: Type[BaseModel], user_message: str,
                    model: str) -> BaseModel:
    """A single structured agent run against one model (one bounded content retry)."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = build_llm_agent(agent_name, instruction, output_schema=output_schema,
                            model=model)
    runner = InMemoryRunner(agent=agent, app_name="paradox")
    session = await runner.session_service.create_session(
        app_name="paradox", user_id="paradox")

    last_err: Optional[str] = None
    for attempt in range(2):
        msg = user_message if attempt == 0 else (
            user_message + "\n\nYour previous output was invalid: "
            + (last_err or "") + "\nReturn ONLY valid JSON matching the schema.")
        content = types.Content(role="user", parts=[types.Part(text=msg)])
        final_text = None
        async for event in runner.run_async(
                user_id="paradox", session_id=session.id, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts)
        if not final_text:
            last_err = "empty response"
            continue
        try:
            return output_schema.model_validate_json(final_text)
        except Exception as e:
            last_err = str(e)[:200]
            continue
    raise ValueError(f"invalid structured output: {last_err}")


async def run_structured_agent(agent_name: str, instruction: str,
                               output_schema: Type[BaseModel],
                               user_message: str) -> BaseModel:
    """Run a one-shot ADK agent and return its validated structured output.

    Resilience: transient server errors (503/429) are retried with exponential
    backoff; a retired-model error (404) or sustained overload advances to the
    next model in the fallback list. Invalid JSON gets one bounded content
    retry inside `_run_once`.
    """
    _require_key()
    candidates = _model_candidates()
    last_err: Optional[str] = None
    for model in candidates:
        for backoff in (0.0, 2.0, 5.0, 10.0):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                return await _run_once(agent_name, instruction, output_schema,
                                       user_message, model)
            except Exception as e:  # noqa: BLE001 - classify and route
                msg = str(e)
                last_err = f"[{model}] {msg[:200]}"
                if any(m in msg for m in _RETIRED_MARKERS):
                    break  # try next model immediately
                if any(m in msg for m in _TRANSIENT_MARKERS):
                    continue  # back off and retry same model
                # validation/other error: one more model may behave, but don't spin
                break
    raise ValueError(
        f"{agent_name} failed to produce valid structured output. "
        f"Last error: {last_err}")


# Convenience wrappers naming each agent's schema/instruction -----------------
async def run_world_builder(constraints_text: str) -> WorldBuilderOutput:
    return await run_structured_agent(
        "world_builder", WORLD_BUILDER_INSTRUCTION, WorldBuilderOutput,
        constraints_text)


async def run_strategy(context_text: str) -> StrategyOutput:
    return await run_structured_agent(
        "strategy", STRATEGY_INSTRUCTION, StrategyOutput, context_text)


async def run_chaos(context_text: str) -> ChaosOutput:
    return await run_structured_agent(
        "chaos", CHAOS_INSTRUCTION, ChaosOutput, context_text)


async def run_recovery(context_text: str) -> RecoveryChoice:
    return await run_structured_agent(
        "recovery", RECOVERY_INSTRUCTION, RecoveryChoice, context_text)
