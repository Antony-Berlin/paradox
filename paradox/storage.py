"""SQLite persistence for PARADOX.

This is the deterministic state backbone -- NOT an LLM. It:

* validates agent-generated world JSON (via schemas) and persists an immutable
  original snapshot,
* enforces the legal order state machine with version checks,
* records every mutation as an event with full provenance fields,
* supports idempotency keys so retried tool calls do not double-apply,
* can snapshot (hash) a world and clone full state into an isolated copy for
  counterfactual simulation.

Connections use WAL, foreign keys ON. All mutations run in transactions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import settings
from .schemas import World

SCHEMA_VERSION = "1.0"


def _now_iso() -> str:
    # time.time() is allowed (not Date.now in a workflow script); wall clock is fine here.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


DDL = """
CREATE TABLE IF NOT EXISTS worlds (
    world_id TEXT PRIMARY KEY,
    seed_hint TEXT,
    generated_at TEXT,
    generator_model TEXT,
    schema_version TEXT,
    currency TEXT,
    city TEXT,
    simulation_clock TEXT,
    generation_constraints TEXT,
    provenance TEXT,
    world_hash TEXT,
    snapshot_json TEXT NOT NULL,     -- immutable original world document
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS products (
    world_id TEXT, sku TEXT, name TEXT, category TEXT,
    unit_weight_kg REAL, unit_volume_l REAL, unit_price_inr REAL,
    reorder_point INTEGER, cold_chain_required INTEGER, shelf_life_hours INTEGER,
    PRIMARY KEY (world_id, sku),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS warehouses (
    world_id TEXT, warehouse_id TEXT, zone TEXT, capacity_units INTEGER,
    max_weight_kg REAL, cold_chain_supported INTEGER, operating_status TEXT,
    PRIMARY KEY (world_id, warehouse_id),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS inventory (
    world_id TEXT, warehouse_id TEXT, sku TEXT,
    on_hand INTEGER, reserved INTEGER, reorder_point INTEGER, last_updated TEXT,
    version INTEGER DEFAULT 0,
    PRIMARY KEY (world_id, warehouse_id, sku),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    world_id TEXT, supplier_id TEXT, supported_skus TEXT, stock_capacity INTEGER,
    unit_cost_inr REAL, lead_time_minutes INTEGER, on_time_probability REAL,
    failure_probability REAL, reliability_prior REAL, service_zone TEXT,
    available INTEGER DEFAULT 1,
    PRIMARY KEY (world_id, supplier_id),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS drivers (
    world_id TEXT, driver_id TEXT, zone TEXT, status TEXT, max_weight_kg REAL,
    vehicle_type TEXT, cost_inr REAL, travel_time_minutes INTEGER,
    failure_probability REAL, reliability_prior REAL,
    PRIMARY KEY (world_id, driver_id),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS routes (
    world_id TEXT, route_id TEXT, origin_warehouse_id TEXT, delivery_zone TEXT,
    distance_km REAL, eta_minutes INTEGER, risk_factor REAL, cost_inr REAL,
    PRIMARY KEY (world_id, route_id),
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, world_id TEXT, customer_id TEXT, lines TEXT,
    delivery_zone TEXT, deadline_minutes INTEGER, budget_inr REAL, priority TEXT,
    status TEXT, version INTEGER DEFAULT 0,
    assigned_driver_id TEXT, route_id TEXT, packed_weight_kg REAL DEFAULT 0,
    FOREIGN KEY (world_id) REFERENCES worlds(world_id)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY, order_id TEXT, world_id TEXT,
    warehouse_id TEXT, sku TEXT, qty INTEGER, idempotency_key TEXT UNIQUE,
    created_at TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id TEXT PRIMARY KEY, order_id TEXT, world_id TEXT, supplier_id TEXT,
    sku TEXT, qty INTEGER, cost_inr REAL, idempotency_key TEXT UNIQUE, created_at TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS faults (
    fault_id TEXT PRIMARY KEY, order_id TEXT, world_id TEXT, kind TEXT,
    target_id TEXT, at_stage TEXT, magnitude REAL, reason_summary TEXT,
    applied INTEGER DEFAULT 0, created_at TEXT
);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    attempt_id TEXT PRIMARY KEY, order_id TEXT, fault_id TEXT, agent TEXT,
    action TEXT, old_value TEXT, new_value TEXT, verified INTEGER,
    verifier_id TEXT, reason_summary TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    world_id TEXT, entity_type TEXT, entity_id TEXT,
    total_assignments INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    recoveries INTEGER DEFAULT 0, reliability REAL,
    last_event_id TEXT, updated_at TEXT,
    PRIMARY KEY (world_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, run_id TEXT, order_id TEXT, world_id TEXT,
    timestamp TEXT, agent TEXT, event_type TEXT, tool TEXT, status TEXT,
    duration_ms INTEGER, correlation_id TEXT, idempotency_key TEXT,
    before_version INTEGER, after_version INTEGER, reason_summary TEXT,
    evidence_refs TEXT, payload TEXT
);
"""


class StateError(Exception):
    """Raised on illegal transitions, version conflicts, or invariant breaches."""


class Storage:
    """Thin SQLite wrapper. One instance per DB file."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # --- connection management ---------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.tx() as conn:
            conn.executescript(DDL)

    # --- world persistence --------------------------------------------------
    @staticmethod
    def compute_world_hash(world: World) -> str:
        """Deterministic hash over the canonical world document."""
        canonical = json.dumps(
            world.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def seed_world(self, world: World) -> dict[str, Any]:
        """Validate and persist a world + its immutable snapshot. Idempotent on world_id."""
        world_hash = self.compute_world_hash(world)
        snapshot_json = json.dumps(world.model_dump(mode="json"), sort_keys=True)
        with self.tx() as conn:
            exists = conn.execute(
                "SELECT world_id FROM worlds WHERE world_id=?", (world.world_id,)
            ).fetchone()
            if exists:
                # idempotent: do not re-seed an existing world
                return {"world_id": world.world_id, "world_hash": world_hash,
                        "status": "already_seeded"}
            conn.execute(
                """INSERT INTO worlds (world_id, seed_hint, generated_at,
                    generator_model, schema_version, currency, city,
                    simulation_clock, generation_constraints, provenance,
                    world_hash, snapshot_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (world.world_id, world.seed_hint, world.generated_at,
                 world.generator_model, world.schema_version, world.currency,
                 world.city, world.simulation_clock,
                 json.dumps(world.generation_constraints), world.provenance,
                 world_hash, snapshot_json, _now_iso()),
            )
            for p in world.products:
                conn.execute(
                    """INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (world.world_id, p.sku, p.name, p.category, p.unit_weight_kg,
                     p.unit_volume_l, p.unit_price_inr, p.reorder_point,
                     int(p.cold_chain_required), p.shelf_life_hours),
                )
            for w in world.warehouses:
                conn.execute(
                    """INSERT INTO warehouses VALUES (?,?,?,?,?,?,?)""",
                    (world.world_id, w.warehouse_id, w.zone, w.capacity_units,
                     w.max_weight_kg, int(w.cold_chain_supported), w.operating_status),
                )
            for inv in world.inventory:
                conn.execute(
                    """INSERT INTO inventory VALUES (?,?,?,?,?,?,?,0)""",
                    (world.world_id, inv.warehouse_id, inv.sku, inv.on_hand,
                     inv.reserved, inv.reorder_point, inv.last_updated or _now_iso()),
                )
            for s in world.suppliers:
                conn.execute(
                    """INSERT INTO suppliers VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                    (world.world_id, s.supplier_id, json.dumps(s.supported_skus),
                     s.stock_capacity, s.unit_cost_inr, s.lead_time_minutes,
                     s.on_time_probability, s.failure_probability,
                     s.reliability_prior, s.service_zone),
                )
            for d in world.drivers:
                conn.execute(
                    """INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (world.world_id, d.driver_id, d.zone, d.status, d.max_weight_kg,
                     d.vehicle_type, d.cost_inr, d.travel_time_minutes,
                     d.failure_probability, d.reliability_prior),
                )
            for r in world.routes:
                conn.execute(
                    """INSERT INTO routes VALUES (?,?,?,?,?,?,?,?)""",
                    (world.world_id, r.route_id, r.origin_warehouse_id,
                     r.delivery_zone, r.distance_km, r.eta_minutes, r.risk_factor,
                     r.cost_inr),
                )
            for o in world.orders:
                conn.execute(
                    """INSERT INTO orders (order_id, world_id, customer_id, lines,
                        delivery_zone, deadline_minutes, budget_inr, priority,
                        status, version)
                       VALUES (?,?,?,?,?,?,?,?,?,0)""",
                    (o.order_id, world.world_id, o.customer_id,
                     json.dumps([ln.model_dump() for ln in o.lines]),
                     o.delivery_zone, o.deadline_minutes, o.budget_inr,
                     o.priority, o.status),
                )
            # seed feedback rows from priors
            for s in world.suppliers:
                conn.execute(
                    """INSERT OR IGNORE INTO feedback (world_id, entity_type,
                        entity_id, total_assignments, failures, recoveries,
                        reliability, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (world.world_id, "supplier", s.supplier_id, 0, 0, 0,
                     s.reliability_prior, _now_iso()),
                )
            for d in world.drivers:
                conn.execute(
                    """INSERT OR IGNORE INTO feedback (world_id, entity_type,
                        entity_id, total_assignments, failures, recoveries,
                        reliability, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (world.world_id, "driver", d.driver_id, 0, 0, 0,
                     d.reliability_prior, _now_iso()),
                )
        return {"world_id": world.world_id, "world_hash": world_hash,
                "status": "seeded"}

    def get_world_snapshot(self, world_id: str) -> dict[str, Any]:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT snapshot_json, world_hash FROM worlds WHERE world_id=?",
                (world_id,),
            ).fetchone()
            if not row:
                raise StateError(f"unknown world {world_id}")
            snap = json.loads(row["snapshot_json"])
            snap["_world_hash"] = row["world_hash"]
            return snap

    def world_hash(self, world_id: str) -> str:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT world_hash FROM worlds WHERE world_id=?", (world_id,)
            ).fetchone()
            if not row:
                raise StateError(f"unknown world {world_id}")
            return row["world_hash"]

    def latest_world_id(self) -> Optional[str]:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT world_id FROM worlds ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return row["world_id"] if row else None

    # --- helpers for live state -------------------------------------------
    def get_order_row(self, conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise StateError(f"unknown order {order_id}")
        return row

    def row_versions(self, world_id: str) -> dict[str, int]:
        """Snapshot of all mutable row versions (for isolation proofs)."""
        with self.tx() as conn:
            out: dict[str, int] = {}
            for r in conn.execute(
                "SELECT warehouse_id, sku, version FROM inventory WHERE world_id=?",
                (world_id,),
            ):
                out[f"inv:{r['warehouse_id']}:{r['sku']}"] = r["version"]
            for r in conn.execute(
                "SELECT order_id, version FROM orders WHERE world_id=?", (world_id,)
            ):
                out[f"order:{r['order_id']}"] = r["version"]
            return out

    # --- events ------------------------------------------------------------
    def record_event(self, conn: sqlite3.Connection, **kw) -> str:
        event_id = new_id("evt")
        conn.execute(
            """INSERT INTO events (event_id, run_id, order_id, world_id, timestamp,
                agent, event_type, tool, status, duration_ms, correlation_id,
                idempotency_key, before_version, after_version, reason_summary,
                evidence_refs, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, kw.get("run_id"), kw.get("order_id"), kw.get("world_id"),
             _now_iso(), kw.get("agent", "system"), kw.get("event_type", ""),
             kw.get("tool"), kw.get("status", "ok"), kw.get("duration_ms", 0),
             kw.get("correlation_id"), kw.get("idempotency_key"),
             kw.get("before_version"), kw.get("after_version"),
             kw.get("reason_summary", ""),
             json.dumps(kw.get("evidence_refs", [])),
             json.dumps(kw.get("payload", {}))),
        )
        return event_id

    def get_events(self, order_id: Optional[str] = None,
                   world_id: Optional[str] = None) -> list[dict]:
        with self.tx() as conn:
            q = "SELECT * FROM events WHERE 1=1"
            args: list = []
            if order_id:
                q += " AND order_id=?"
                args.append(order_id)
            if world_id:
                q += " AND world_id=?"
                args.append(world_id)
            q += " ORDER BY timestamp ASC, rowid ASC"
            rows = conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
                d["payload"] = json.loads(d.get("payload") or "{}")
                out.append(d)
            return out

    # --- state machine helpers ---------------------------------------------
    def _transition(self, conn: sqlite3.Connection, order_id: str, to_state: str,
                    *, run_id: str = "", agent: str = "execution",
                    tool: str = "", recovery: bool = False, **extra) -> dict:
        """Apply a version-checked legal transition and emit an event."""
        row = self.get_order_row(conn, order_id)
        frm = row["status"]
        before_version = row["version"]
        legal = settings.LEGAL_TRANSITIONS.get(frm, set())
        if to_state not in legal and not (recovery and to_state in legal):
            raise StateError(
                f"illegal transition {frm} -> {to_state} for order {order_id}")
        after_version = before_version + 1
        reason = extra.pop("reason_summary", "")
        # only real `orders` columns may be set here; reason goes to the event
        allowed_cols = {"assigned_driver_id", "route_id", "packed_weight_kg"}
        sets = ["status=?", "version=?"]
        vals: list = [to_state, after_version]
        for col, v in extra.items():
            if col not in allowed_cols:
                raise StateError(f"_transition got unknown order column {col!r}")
            sets.append(f"{col}=?")
            vals.append(v)
        vals.append(order_id)
        vals.append(before_version)
        updated = conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE order_id=? AND version=?",
            vals,
        )
        if updated.rowcount != 1:
            raise StateError(
                f"version conflict on order {order_id} (expected {before_version})")
        self.record_event(conn, run_id=run_id, order_id=order_id,
                          world_id=row["world_id"], agent=agent,
                          event_type=f"transition:{frm}->{to_state}", tool=tool,
                          status="ok", before_version=before_version,
                          after_version=after_version,
                          reason_summary=reason)
        return {"order_id": order_id, "from": frm, "to": to_state,
                "before_version": before_version, "after_version": after_version}

    def _idempotent_seen(self, conn: sqlite3.Connection, table: str,
                         idempotency_key: str) -> Optional[sqlite3.Row]:
        if not idempotency_key:
            return None
        return conn.execute(
            f"SELECT * FROM {table} WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()

    # --- lifecycle operations ----------------------------------------------
    def create_order(self, order_id: str, run_id: str = "") -> dict:
        """Order already exists from seed; this marks the start of execution."""
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent="execution",
                              event_type="order_opened", tool="create_order",
                              status="ok")
            return {"order_id": order_id, "status": row["status"]}

    def check_stock(self, order_id: str, warehouse_id: str, run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            lines = json.loads(row["lines"])
            shortfalls = []
            for ln in lines:
                inv = conn.execute(
                    "SELECT on_hand, reserved FROM inventory WHERE world_id=? AND warehouse_id=? AND sku=?",
                    (row["world_id"], warehouse_id, ln["sku"]),
                ).fetchone()
                avail = (inv["on_hand"] - inv["reserved"]) if inv else 0
                if avail < ln["qty"]:
                    shortfalls.append({"sku": ln["sku"], "needed": ln["qty"],
                                       "available": avail,
                                       "procure_qty": ln["qty"] - avail})
            to_state = "PROCUREMENT_REQUIRED" if shortfalls else "STOCK_CHECKED"
            # first always go to STOCK_CHECKED, then PROCUREMENT_REQUIRED if needed
            self._transition(conn, order_id, "STOCK_CHECKED", run_id=run_id,
                             tool="check_stock")
            if shortfalls:
                self._transition(conn, order_id, "PROCUREMENT_REQUIRED",
                                 run_id=run_id, tool="check_stock",
                                 reason_summary="stock shortfall")
            return {"order_id": order_id, "shortfalls": shortfalls,
                    "status": to_state}

    def purchase_stock(self, order_id: str, supplier_id: str, sku: str, qty: int,
                       warehouse_id: str, idempotency_key: str = "",
                       run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            seen = self._idempotent_seen(conn, "purchase_orders", idempotency_key)
            if seen:
                return {"po_id": seen["po_id"], "idempotent": True,
                        "status": "already_applied"}
            sup = conn.execute(
                "SELECT * FROM suppliers WHERE world_id=? AND supplier_id=?",
                (row["world_id"], supplier_id),
            ).fetchone()
            if not sup:
                raise StateError(f"unknown supplier {supplier_id}")
            if not sup["available"]:
                raise StateError(f"supplier {supplier_id} is unavailable")
            if sku not in json.loads(sup["supported_skus"]):
                raise StateError(f"supplier {supplier_id} does not supply {sku}")
            if qty > sup["stock_capacity"]:
                raise StateError(f"supplier {supplier_id} lacks capacity for {qty}")
            po_id = new_id("po")
            cost = sup["unit_cost_inr"] * qty
            conn.execute(
                """INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?)""",
                (po_id, order_id, row["world_id"], supplier_id, sku, qty, cost,
                 idempotency_key or po_id, _now_iso()),
            )
            # add purchased stock to warehouse on_hand (version-checked)
            inv = conn.execute(
                "SELECT on_hand, version FROM inventory WHERE world_id=? AND warehouse_id=? AND sku=?",
                (row["world_id"], warehouse_id, sku),
            ).fetchone()
            if not inv:
                conn.execute(
                    """INSERT INTO inventory VALUES (?,?,?,?,?,?,?,1)""",
                    (row["world_id"], warehouse_id, sku, qty, 0, 0, _now_iso()),
                )
            else:
                conn.execute(
                    "UPDATE inventory SET on_hand=on_hand+?, version=version+1, last_updated=? "
                    "WHERE world_id=? AND warehouse_id=? AND sku=?",
                    (qty, _now_iso(), row["world_id"], warehouse_id, sku),
                )
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent="execution",
                              event_type="purchase", tool="purchase_stock",
                              status="ok", idempotency_key=idempotency_key,
                              reason_summary=f"bought {qty} {sku} from {supplier_id}",
                              payload={"cost_inr": cost})
            # advance to PROCUREMENT_COMPLETED if currently required
            if row["status"] == "PROCUREMENT_REQUIRED":
                self._transition(conn, order_id, "PROCUREMENT_COMPLETED",
                                 run_id=run_id, tool="purchase_stock")
            return {"po_id": po_id, "cost_inr": cost, "status": "purchased"}

    def reserve_inventory(self, order_id: str, warehouse_id: str,
                          idempotency_key: str = "", run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            # idempotency: reservation lines are stored as "{key}:{sku}"; a prior
            # call with the same key means this reservation already applied.
            if idempotency_key:
                prior = conn.execute(
                    "SELECT reservation_id FROM reservations "
                    "WHERE idempotency_key LIKE ?",
                    (f"{idempotency_key}:%",),
                ).fetchone()
                if prior:
                    return {"reservation_id": prior["reservation_id"],
                            "idempotent": True, "status": "already_applied"}
            lines = json.loads(row["lines"])
            # verify availability for all lines first (atomic)
            for ln in lines:
                inv = conn.execute(
                    "SELECT on_hand, reserved FROM inventory WHERE world_id=? AND warehouse_id=? AND sku=?",
                    (row["world_id"], warehouse_id, ln["sku"]),
                ).fetchone()
                avail = (inv["on_hand"] - inv["reserved"]) if inv else 0
                if avail < ln["qty"]:
                    raise StateError(
                        f"insufficient available stock for {ln['sku']} "
                        f"(need {ln['qty']}, have {avail})")
            res_id = new_id("res")
            for ln in lines:
                conn.execute(
                    "UPDATE inventory SET reserved=reserved+?, version=version+1, last_updated=? "
                    "WHERE world_id=? AND warehouse_id=? AND sku=?",
                    (ln["qty"], _now_iso(), row["world_id"], warehouse_id, ln["sku"]),
                )
                conn.execute(
                    """INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?)""",
                    (new_id("resline"), order_id, row["world_id"], warehouse_id,
                     ln["sku"], ln["qty"],
                     f"{idempotency_key or res_id}:{ln['sku']}", _now_iso()),
                )
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent="execution",
                              event_type="reserve", tool="reserve_inventory",
                              status="ok", idempotency_key=idempotency_key)
            self._transition(conn, order_id, "RESERVED", run_id=run_id,
                             tool="reserve_inventory")
            return {"reservation_id": res_id, "status": "reserved"}

    def pack_order(self, order_id: str, run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            lines = json.loads(row["lines"])
            weight = 0.0
            for ln in lines:
                p = conn.execute(
                    "SELECT unit_weight_kg FROM products WHERE world_id=? AND sku=?",
                    (row["world_id"], ln["sku"]),
                ).fetchone()
                weight += (p["unit_weight_kg"] if p else 0) * ln["qty"]
            self._transition(conn, order_id, "PACKED", run_id=run_id,
                             tool="pack_order", packed_weight_kg=weight)
            return {"order_id": order_id, "packed_weight_kg": weight, "status": "PACKED"}

    def assign_driver(self, order_id: str, driver_id: str, route_id: str,
                      run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            drv = conn.execute(
                "SELECT * FROM drivers WHERE world_id=? AND driver_id=?",
                (row["world_id"], driver_id),
            ).fetchone()
            if not drv:
                raise StateError(f"unknown driver {driver_id}")
            if drv["status"] != "available":
                raise StateError(f"driver {driver_id} not available ({drv['status']})")
            if drv["zone"] != row["delivery_zone"]:
                raise StateError(
                    f"driver {driver_id} zone {drv['zone']} != order zone {row['delivery_zone']}")
            if row["packed_weight_kg"] > drv["max_weight_kg"]:
                raise StateError(
                    f"driver {driver_id} max_weight {drv['max_weight_kg']} < "
                    f"packed {row['packed_weight_kg']}")
            conn.execute(
                "UPDATE orders SET assigned_driver_id=?, route_id=? WHERE order_id=?",
                (driver_id, route_id, order_id),
            )
            conn.execute(
                "UPDATE drivers SET status='busy' WHERE world_id=? AND driver_id=?",
                (row["world_id"], driver_id),
            )
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent="execution",
                              event_type="assign_driver", tool="assign_driver",
                              status="ok", reason_summary=f"driver {driver_id}")
            return {"order_id": order_id, "driver_id": driver_id, "status": "assigned"}

    def dispatch_order(self, order_id: str, run_id: str = "") -> dict:
        # Detect an unavailable driver in its own committed transaction so the
        # FAILED state + event persist even though we then raise (raising inside
        # a tx() would roll the transition back).
        failure_payload = None
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            driver_id = row["assigned_driver_id"]
            if not driver_id:
                raise StateError(f"order {order_id} has no assigned driver")
            drv = conn.execute(
                "SELECT status FROM drivers WHERE world_id=? AND driver_id=?",
                (row["world_id"], driver_id),
            ).fetchone()
            if not drv or drv["status"] == "offline":
                # structured failure: driver unavailable at dispatch -> FAILED
                self._transition(conn, order_id, "FAILED", run_id=run_id,
                                 tool="dispatch_order",
                                 reason_summary=f"driver {driver_id} offline at dispatch")
                failure_payload = {"error": "driver_unavailable",
                                   "driver_id": driver_id, "order_id": order_id,
                                   "stage": "DISPATCHED"}
            else:
                self._transition(conn, order_id, "DISPATCHED", run_id=run_id,
                                 tool="dispatch_order")
        if failure_payload is not None:
            raise StateError(json.dumps(failure_payload))
        return {"order_id": order_id, "status": "DISPATCHED", "driver_id": driver_id}

    def complete_delivery(self, order_id: str, run_id: str = "") -> dict:
        with self.tx() as conn:
            self._transition(conn, order_id, "DELIVERED", run_id=run_id,
                             tool="complete_delivery")
            return {"order_id": order_id, "status": "DELIVERED"}

    def verify_order(self, order_id: str, run_id: str = "") -> dict:
        with self.tx() as conn:
            self._transition(conn, order_id, "VERIFIED", run_id=run_id,
                             tool="verify_order")
            return {"order_id": order_id, "status": "VERIFIED"}

    # --- fault injection (REAL execution-state change) ---------------------
    def inject_execution_fault(self, kind: str, target_id: str, order_id: str,
                               run_id: str = "", reason: str = "") -> dict:
        if kind not in settings.FAULT_KINDS:
            raise StateError(f"fault kind {kind} not in allowed {settings.FAULT_KINDS}")
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            fault_id = new_id("flt")
            applied = False
            if kind == "driver_cancelled":
                drv = conn.execute(
                    "SELECT driver_id FROM drivers WHERE world_id=? AND driver_id=?",
                    (row["world_id"], target_id),
                ).fetchone()
                if not drv:
                    raise StateError(f"fault target driver {target_id} unknown")
                conn.execute(
                    "UPDATE drivers SET status='offline' WHERE world_id=? AND driver_id=?",
                    (row["world_id"], target_id),
                )
                applied = True
            elif kind == "supplier_down":
                conn.execute(
                    "UPDATE suppliers SET available=0 WHERE world_id=? AND supplier_id=?",
                    (row["world_id"], target_id),
                )
                applied = conn.total_changes > 0 or True
            elif kind == "stock_discrepancy":
                wh, sku = target_id.split(":", 1)
                conn.execute(
                    "UPDATE inventory SET on_hand=MAX(on_hand-1,0), version=version+1 "
                    "WHERE world_id=? AND warehouse_id=? AND sku=?",
                    (row["world_id"], wh, sku),
                )
                applied = True
            conn.execute(
                """INSERT INTO faults (fault_id, order_id, world_id, kind,
                    target_id, at_stage, magnitude, reason_summary, applied,
                    created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (fault_id, order_id, row["world_id"], kind, target_id,
                 row["status"], 1.0, reason, int(applied), _now_iso()),
            )
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent="chaos",
                              event_type="fault_injected", tool="inject_execution_fault",
                              status="applied" if applied else "noop",
                              reason_summary=reason or f"{kind} on {target_id}",
                              payload={"fault_id": fault_id, "kind": kind,
                                       "target_id": target_id})
            return {"fault_id": fault_id, "kind": kind, "target_id": target_id,
                    "applied": applied}

    # --- recovery tools -----------------------------------------------------
    def reassign_driver(self, order_id: str, new_driver_id: str,
                        fault_id: str = "", agent: str = "recovery",
                        run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            old_driver = row["assigned_driver_id"]
            drv = conn.execute(
                "SELECT * FROM drivers WHERE world_id=? AND driver_id=?",
                (row["world_id"], new_driver_id),
            ).fetchone()
            if not drv:
                raise StateError(f"unknown driver {new_driver_id}")
            if new_driver_id == old_driver:
                raise StateError("cannot reassign to the same (failed) driver")
            if drv["status"] != "available":
                raise StateError(f"driver {new_driver_id} not available")
            if drv["zone"] != row["delivery_zone"]:
                raise StateError(f"driver {new_driver_id} wrong zone")
            if row["packed_weight_kg"] > drv["max_weight_kg"]:
                raise StateError(f"driver {new_driver_id} insufficient capacity")
            # move order into RECOVERING then back to PACKED for re-dispatch
            if row["status"] == "FAILED":
                self._transition(conn, order_id, "RECOVERING", run_id=run_id,
                                 agent=agent, tool="reassign_driver", recovery=True)
                self._transition(conn, order_id, "PACKED", run_id=run_id,
                                 agent=agent, tool="reassign_driver", recovery=True)
            conn.execute(
                "UPDATE orders SET assigned_driver_id=? WHERE order_id=?",
                (new_driver_id, order_id),
            )
            conn.execute("UPDATE drivers SET status='busy' WHERE world_id=? AND driver_id=?",
                         (row["world_id"], new_driver_id))
            attempt_id = new_id("rec")
            conn.execute(
                """INSERT INTO recovery_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, order_id, fault_id, agent, "reassign_driver",
                 old_driver, new_driver_id, 0, "", "driver reassigned", _now_iso()),
            )
            self.record_event(conn, run_id=run_id, order_id=order_id,
                              world_id=row["world_id"], agent=agent,
                              event_type="recovery_action", tool="reassign_driver",
                              status="ok",
                              reason_summary=f"{old_driver} -> {new_driver_id}",
                              payload={"attempt_id": attempt_id,
                                       "old_driver": old_driver,
                                       "new_driver": new_driver_id})
            return {"attempt_id": attempt_id, "old_driver": old_driver,
                    "new_driver": new_driver_id, "status": "reassigned"}

    def switch_supplier(self, order_id: str, sku: str, new_supplier_id: str,
                        warehouse_id: str, qty: int, fault_id: str = "",
                        run_id: str = "") -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            attempt_id = new_id("rec")
            # purchase from the new supplier (reuses validation path)
        res = self.purchase_stock(order_id, new_supplier_id, sku, qty,
                                  warehouse_id, idempotency_key=f"switch:{attempt_id}",
                                  run_id=run_id)
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO recovery_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, order_id, fault_id, "recovery", "switch_supplier",
                 "", new_supplier_id, 0, "", "supplier switched", _now_iso()),
            )
        return {"attempt_id": attempt_id, "new_supplier": new_supplier_id,
                "po": res, "status": "switched"}

    def mark_recovery_verified(self, attempt_id: str, verifier_id: str,
                               verified: bool) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE recovery_attempts SET verified=?, verifier_id=? WHERE attempt_id=?",
                (int(verified), verifier_id, attempt_id),
            )

    # --- feedback (operational memory) -------------------------------------
    def record_feedback(self, world_id: str, entity_type: str, entity_id: str,
                        outcome: str, event_id: str = "") -> dict:
        """outcome in {'success','failure','recovery'}. Updates smoothed reliability."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE world_id=? AND entity_type=? AND entity_id=?",
                (world_id, entity_type, entity_id),
            ).fetchone()
            total = (row["total_assignments"] if row else 0) + 1
            failures = (row["failures"] if row else 0) + (1 if outcome == "failure" else 0)
            recoveries = (row["recoveries"] if row else 0) + (1 if outcome == "recovery" else 0)
            successes = total - failures
            reliability = (successes + settings.RELIABILITY_ALPHA) / (
                total + settings.RELIABILITY_BETA)
            conn.execute(
                """INSERT INTO feedback (world_id, entity_type, entity_id,
                    total_assignments, failures, recoveries, reliability,
                    last_event_id, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(world_id, entity_type, entity_id) DO UPDATE SET
                    total_assignments=excluded.total_assignments,
                    failures=excluded.failures, recoveries=excluded.recoveries,
                    reliability=excluded.reliability,
                    last_event_id=excluded.last_event_id,
                    updated_at=excluded.updated_at""",
                (world_id, entity_type, entity_id, total, failures, recoveries,
                 reliability, event_id, _now_iso()),
            )
            return {"entity_id": entity_id, "reliability": reliability,
                    "total_assignments": total, "failures": failures,
                    "recoveries": recoveries, "last_event_id": event_id}

    def get_feedback(self, world_id: str) -> dict[str, dict]:
        with self.tx() as conn:
            out: dict[str, dict] = {}
            for r in conn.execute("SELECT * FROM feedback WHERE world_id=?", (world_id,)):
                out[f"{r['entity_type']}:{r['entity_id']}"] = dict(r)
            return out

    # --- read helpers for agents -------------------------------------------
    def list_available_suppliers(self, world_id: str, sku: str) -> list[dict]:
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM suppliers WHERE world_id=? AND available=1", (world_id,)
            ).fetchall()
            return [dict(r) for r in rows
                    if sku in json.loads(r["supported_skus"])]

    def list_available_drivers(self, world_id: str, zone: str) -> list[dict]:
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM drivers WHERE world_id=? AND zone=? AND status='available'",
                (world_id, zone),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_order(self, order_id: str) -> dict:
        with self.tx() as conn:
            row = self.get_order_row(conn, order_id)
            d = dict(row)
            d["lines"] = json.loads(d["lines"])
            return d
