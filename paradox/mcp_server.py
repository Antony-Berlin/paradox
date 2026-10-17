"""PARADOX MCP server (mcp 2.3 `MCPServer`, stdio transport).

Exposes the logistics domain as genuine MCP tools over stdio. ADK's `McpToolset`
and the controller's `mcp_client` both reach these tools via the Model Context
Protocol -- there is no direct-function bypass on the claimed MCP path.

Every tool is a thin wrapper over `Storage`, which owns all validation, the
legal state machine, idempotency, versioning, and event logging. The server
does NOT invent world data: `seed_world` validates and persists agent-produced
JSON only.

Run standalone:  python -m paradox.mcp_server  [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from mcp.server.mcpserver import MCPServer

from .schemas import World
from .storage import Storage, StateError


def build_server(db_path: Optional[str] = None) -> MCPServer:
    storage = Storage(db_path)
    server = MCPServer(
        name="paradox-logistics",
        version="1.0.0",
        instructions="Simulated logistics domain tools for the PARADOX tournament.",
    )

    def _ok(data: dict) -> str:
        return json.dumps({"status": "ok", **data})

    def _err(msg: str) -> str:
        return json.dumps({"status": "error", "error": msg})

    # --- world ---------------------------------------------------------------
    @server.tool()
    def seed_world(world_json: str) -> str:
        """Validate agent-generated world JSON and persist it immutably.

        Args:
            world_json: JSON string of the full World document.
        """
        try:
            world = World.model_validate_json(world_json)
            return _ok(storage.seed_world(world))
        except Exception as e:  # validation or persistence failure
            return _err(f"seed_world failed: {e}")

    @server.tool()
    def get_world_snapshot(world_id: str) -> str:
        """Return the immutable persisted world document and its hash."""
        try:
            return _ok({"snapshot": storage.get_world_snapshot(world_id)})
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def get_order(order_id: str) -> str:
        """Return the current order row (status, version, lines, driver)."""
        try:
            return _ok({"order": storage.get_order(order_id)})
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def list_available_suppliers(world_id: str, sku: str) -> str:
        """List suppliers that are available and support the given SKU."""
        return _ok({"suppliers": storage.list_available_suppliers(world_id, sku)})

    @server.tool()
    def list_available_drivers(world_id: str, zone: str) -> str:
        """List drivers currently available in the given delivery zone."""
        return _ok({"drivers": storage.list_available_drivers(world_id, zone)})

    # --- execution lifecycle -------------------------------------------------
    @server.tool()
    def create_order(order_id: str, run_id: str = "") -> str:
        """Open an existing (seeded) order for execution."""
        try:
            return _ok(storage.create_order(order_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def check_stock(order_id: str, warehouse_id: str, run_id: str = "") -> str:
        """Check stock for an order's lines at a warehouse; flag shortfalls."""
        try:
            return _ok(storage.check_stock(order_id, warehouse_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def purchase_stock(order_id: str, supplier_id: str, sku: str, qty: int,
                       warehouse_id: str, idempotency_key: str = "",
                       run_id: str = "") -> str:
        """Procure stock from a supplier into a warehouse (idempotent)."""
        try:
            return _ok(storage.purchase_stock(order_id, supplier_id, sku, qty,
                                              warehouse_id,
                                              idempotency_key=idempotency_key,
                                              run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def reserve_inventory(order_id: str, warehouse_id: str,
                          idempotency_key: str = "", run_id: str = "") -> str:
        """Reserve all order lines at a warehouse (idempotent, no overselling)."""
        try:
            return _ok(storage.reserve_inventory(order_id, warehouse_id,
                                                idempotency_key=idempotency_key,
                                                run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def pack_order(order_id: str, run_id: str = "") -> str:
        """Pack a reserved order; computes packed weight."""
        try:
            return _ok(storage.pack_order(order_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def assign_driver(order_id: str, driver_id: str, route_id: str,
                      run_id: str = "") -> str:
        """Assign a driver + route to a packed order (zone/capacity checked)."""
        try:
            return _ok(storage.assign_driver(order_id, driver_id, route_id,
                                             run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def dispatch_order(order_id: str, run_id: str = "") -> str:
        """Dispatch an assigned order. Fails structurally if driver is offline."""
        try:
            return _ok(storage.dispatch_order(order_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def complete_delivery(order_id: str, run_id: str = "") -> str:
        """Mark a dispatched order delivered."""
        try:
            return _ok(storage.complete_delivery(order_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def verify_order(order_id: str, run_id: str = "") -> str:
        """Independently verify a delivered order -> VERIFIED."""
        try:
            return _ok(storage.verify_order(order_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    # --- fault injection (real execution-state change) -----------------------
    @server.tool()
    def inject_execution_fault(kind: str, target_id: str, order_id: str,
                               reason: str = "", run_id: str = "") -> str:
        """Inject an enumerated safe fault into the LIVE execution state.

        kind in {supplier_down, driver_cancelled, stock_discrepancy}.
        For stock_discrepancy, target_id is 'warehouse_id:sku'.
        """
        try:
            return _ok(storage.inject_execution_fault(kind, target_id, order_id,
                                                     run_id=run_id, reason=reason))
        except StateError as e:
            return _err(str(e))

    # --- recovery tools ------------------------------------------------------
    @server.tool()
    def reassign_driver(order_id: str, new_driver_id: str, fault_id: str = "",
                        run_id: str = "") -> str:
        """Reassign a failed order to a different, valid driver and re-enable dispatch."""
        try:
            return _ok(storage.reassign_driver(order_id, new_driver_id,
                                              fault_id=fault_id, run_id=run_id))
        except StateError as e:
            return _err(str(e))

    @server.tool()
    def switch_supplier(order_id: str, sku: str, new_supplier_id: str,
                        warehouse_id: str, qty: int, fault_id: str = "",
                        run_id: str = "") -> str:
        """Procure from an alternate supplier after a supplier failure."""
        try:
            return _ok(storage.switch_supplier(order_id, sku, new_supplier_id,
                                              warehouse_id, qty, fault_id=fault_id,
                                              run_id=run_id))
        except StateError as e:
            return _err(str(e))

    # --- observability / feedback -------------------------------------------
    @server.tool()
    def get_events(order_id: str = "", world_id: str = "") -> str:
        """Return the ordered event log for an order and/or world."""
        return _ok({"events": storage.get_events(order_id or None,
                                                 world_id or None)})

    @server.tool()
    def record_feedback(world_id: str, entity_type: str, entity_id: str,
                        outcome: str, event_id: str = "") -> str:
        """Update smoothed reliability for a supplier/driver from an outcome."""
        return _ok(storage.record_feedback(world_id, entity_type, entity_id,
                                           outcome, event_id=event_id))

    # Note: simulate_plan (read-only counterfactual branch) is handled by the
    # deterministic simulator module, which clones state and never touches the
    # real order; it is intentionally NOT a mutating server tool.

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="PARADOX MCP logistics server")
    parser.add_argument("--db", default=os.environ.get("PARADOX_DB"))
    args = parser.parse_args()
    server = build_server(args.db)
    # stdio transport (default for `run`); logs go to stderr so stdout stays clean.
    print("paradox-logistics MCP server starting (stdio)", file=sys.stderr)
    server.run()


if __name__ == "__main__":
    main()
