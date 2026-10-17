"""MCP protocol integration tests (no LLM key needed).

Spawns the PARADOX MCP server as a stdio subprocess and drives the full order
lifecycle over the Model Context Protocol -- proving genuine tool discovery and
invocation (not a direct-function bypass).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from paradox.mcp_client import ParadoxMcpClient
from tests.conftest import make_fixture_world


def _run(coro):
    return asyncio.run(coro)


def test_mcp_tool_discovery():
    async def go():
        db = tempfile.mktemp(suffix=".db")
        try:
            async with ParadoxMcpClient(db) as c:
                tools = await c.list_tools()
            return tools
        finally:
            Path(db).unlink(missing_ok=True)
    tools = _run(go())
    for required in ("seed_world", "reserve_inventory", "pack_order",
                     "assign_driver", "inject_execution_fault",
                     "reassign_driver", "verify_order"):
        assert required in tools


def test_mcp_full_lifecycle_over_protocol():
    world = make_fixture_world("W_mcp")

    async def go():
        db = tempfile.mktemp(suffix=".db")
        try:
            async with ParadoxMcpClient(db) as c:
                seed = await c.call("seed_world",
                        {"world_json": world.model_dump_json()})
                assert seed["status"] in ("ok", "seeded", "already_seeded")
                assert (await c.call("create_order",
                        {"order_id": "ORD_1"}))["status"] != "error"
                assert (await c.call("check_stock",
                        {"order_id": "ORD_1", "warehouse_id": "WH_1"}))["status"] != "error"
                await c.call("purchase_stock", {
                    "order_id": "ORD_1", "supplier_id": "SUP_1", "sku": "SKU_1",
                    "qty": 3, "warehouse_id": "WH_1", "idempotency_key": "b1"})
                await c.call("reserve_inventory", {
                    "order_id": "ORD_1", "warehouse_id": "WH_1", "idempotency_key": "r1"})
                await c.call("pack_order", {"order_id": "ORD_1"})
                await c.call("assign_driver", {
                    "order_id": "ORD_1", "driver_id": "DRV_1", "route_id": "R_1"})
                await c.call("inject_execution_fault", {
                    "kind": "driver_cancelled", "target_id": "DRV_1", "order_id": "ORD_1"})
                disp = await c.call("dispatch_order", {"order_id": "ORD_1"})
                assert disp["status"] == "error"  # structured failure
                await c.call("reassign_driver", {
                    "order_id": "ORD_1", "new_driver_id": "DRV_2"})
                await c.call("dispatch_order", {"order_id": "ORD_1"})
                await c.call("complete_delivery", {"order_id": "ORD_1"})
                await c.call("verify_order", {"order_id": "ORD_1"})
                order = await c.call("get_order", {"order_id": "ORD_1"})
                trace = list(c.trace)
            return order, trace
        finally:
            Path(db).unlink(missing_ok=True)
            Path(db + "-wal").unlink(missing_ok=True)
            Path(db + "-shm").unlink(missing_ok=True)

    order, trace = _run(go())
    assert order["order"]["status"] == "VERIFIED"
    # trace proves genuine MCP invocations and secret redaction
    assert len(trace) >= 12
    seed_entry = next(t for t in trace if t["tool"] == "seed_world")
    assert "redacted" in str(seed_entry["args"]["world_json"])
