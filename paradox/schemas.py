"""Pydantic contracts for PARADOX.

Two families of models:

1. Domain models (World, Product, Warehouse, Inventory, Supplier, Driver, Route,
   Order, Plan, Fault, ...): the production-shaped synthetic world and the plan
   contract. These validate agent-generated data and enforce relational sanity.

2. Agent I/O schemas: the exact structured output each ADK agent must return
   (used as `output_schema` on the LlmAgent so the model emits validated JSON).

Validation bounds, enums, and field names are fixed by code (allowed by the
spec); the *values* for a live world originate from the World Builder agent.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Provenance = Literal["agent_generated", "test_fixture", "replay_snapshot"]


def _positive(cls_name: str, **named_values: float) -> None:
    """Raise if any named numeric value is not strictly positive.

    Used instead of Field(gt=0) so the emitted JSON Schema uses only
    `minimum`/`maximum` (supported by the Gemini Developer API) rather than
    `exclusiveMinimum` (which its structured-output schema subset rejects).
    """
    for name, val in named_values.items():
        if val is not None and val <= 0:
            raise ValueError(f"{cls_name}.{name} must be > 0 (got {val})")


# ============================================================================
# Domain models
# ============================================================================
class Product(BaseModel):
    sku: str
    name: str
    category: str
    unit_weight_kg: float = Field(ge=0)
    unit_volume_l: float = Field(ge=0)
    unit_price_inr: float = Field(ge=0)
    reorder_point: int = Field(ge=0)
    cold_chain_required: bool = False
    shelf_life_hours: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _pos(self) -> "Product":
        _positive("Product", unit_weight_kg=self.unit_weight_kg,
                  unit_volume_l=self.unit_volume_l, unit_price_inr=self.unit_price_inr)
        return self


class Warehouse(BaseModel):
    warehouse_id: str
    zone: str
    capacity_units: int = Field(ge=0)
    max_weight_kg: float = Field(ge=0)
    cold_chain_supported: bool = False
    operating_status: Literal["open", "closed"] = "open"

    @model_validator(mode="after")
    def _pos(self) -> "Warehouse":
        _positive("Warehouse", capacity_units=self.capacity_units,
                  max_weight_kg=self.max_weight_kg)
        return self


class Inventory(BaseModel):
    warehouse_id: str
    sku: str
    on_hand: int = Field(ge=0)
    reserved: int = Field(ge=0)
    reorder_point: int = Field(ge=0)
    last_updated: str = ""

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    @model_validator(mode="after")
    def _available_nonnegative(self) -> "Inventory":
        if self.on_hand - self.reserved < 0:
            raise ValueError(
                f"available cannot be negative for ({self.warehouse_id},{self.sku}): "
                f"on_hand={self.on_hand} reserved={self.reserved}"
            )
        return self


class Supplier(BaseModel):
    supplier_id: str
    supported_skus: list[str] = Field(min_length=1)
    stock_capacity: int = Field(ge=0)
    unit_cost_inr: float = Field(ge=0)
    lead_time_minutes: int = Field(ge=0)
    on_time_probability: float = Field(ge=0.0, le=1.0)
    failure_probability: float = Field(ge=0.0, le=1.0)
    reliability_prior: float = Field(ge=0.0, le=1.0)
    service_zone: str

    @model_validator(mode="after")
    def _pos(self) -> "Supplier":
        _positive("Supplier", stock_capacity=self.stock_capacity,
                  unit_cost_inr=self.unit_cost_inr)
        return self


class Driver(BaseModel):
    driver_id: str
    zone: str
    status: Literal["available", "busy", "offline"] = "available"
    max_weight_kg: float = Field(ge=0)
    vehicle_type: str
    cost_inr: float = Field(ge=0)
    travel_time_minutes: int = Field(ge=0)
    failure_probability: float = Field(ge=0.0, le=1.0)
    reliability_prior: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _pos(self) -> "Driver":
        _positive("Driver", max_weight_kg=self.max_weight_kg, cost_inr=self.cost_inr)
        return self


class Route(BaseModel):
    route_id: str
    origin_warehouse_id: str
    delivery_zone: str
    distance_km: float = Field(ge=0)
    eta_minutes: int = Field(ge=0)
    risk_factor: float = Field(ge=0.0, le=1.0)
    cost_inr: float = Field(ge=0)

    @model_validator(mode="after")
    def _pos(self) -> "Route":
        _positive("Route", distance_km=self.distance_km)
        return self


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class Order(BaseModel):
    order_id: str
    customer_id: str  # synthetic pseudonym
    lines: list[OrderLine] = Field(min_length=1)
    delivery_zone: str
    deadline_minutes: int = Field(ge=1)
    budget_inr: float = Field(ge=0)
    priority: Literal["low", "normal", "high"] = "normal"
    status: str = "CREATED"
    version: int = 0

    @model_validator(mode="after")
    def _pos(self) -> "Order":
        _positive("Order", budget_inr=self.budget_inr)
        return self


class World(BaseModel):
    world_id: str
    seed_hint: str
    generated_at: str
    generator_model: str
    schema_version: str = "1.0"
    currency: str = "INR"
    city: str
    simulation_clock: str
    generation_constraints: dict = Field(default_factory=dict)
    provenance: Provenance = "agent_generated"

    products: list[Product] = Field(min_length=1)
    warehouses: list[Warehouse] = Field(min_length=1)
    inventory: list[Inventory] = Field(min_length=1)
    suppliers: list[Supplier] = Field(min_length=1)
    drivers: list[Driver] = Field(min_length=1)
    routes: list[Route] = Field(min_length=1)
    orders: list[Order] = Field(min_length=1)

    # --- relational consistency ---------------------------------------------
    @model_validator(mode="after")
    def _relational_consistency(self) -> "World":
        skus = {p.sku for p in self.products}
        wh_ids = {w.warehouse_id for w in self.warehouses}
        zones = {w.zone for w in self.warehouses} | {d.zone for d in self.drivers}

        for inv in self.inventory:
            if inv.sku not in skus:
                raise ValueError(f"inventory references unknown sku {inv.sku}")
            if inv.warehouse_id not in wh_ids:
                raise ValueError(
                    f"inventory references unknown warehouse {inv.warehouse_id}"
                )
        for sup in self.suppliers:
            for s in sup.supported_skus:
                if s not in skus:
                    raise ValueError(
                        f"supplier {sup.supplier_id} supports unknown sku {s}"
                    )
        for r in self.routes:
            if r.origin_warehouse_id not in wh_ids:
                raise ValueError(
                    f"route {r.route_id} has unknown origin {r.origin_warehouse_id}"
                )
        for o in self.orders:
            for line in o.lines:
                if line.sku not in skus:
                    raise ValueError(
                        f"order {o.order_id} line references unknown sku {line.sku}"
                    )
            if o.delivery_zone not in zones:
                # delivery zone must be serviceable by at least one driver/warehouse
                raise ValueError(
                    f"order {o.order_id} delivery_zone {o.delivery_zone} "
                    "has no matching warehouse/driver zone"
                )
        return self


# ============================================================================
# Plan contract (section 5)
# ============================================================================
class PlanLine(BaseModel):
    sku: str
    qty: int = Field(ge=1)
    warehouse_id: str
    supplier_id: Optional[str] = None  # only if procurement needed
    procure_qty: int = Field(default=0, ge=0)


class Plan(BaseModel):
    plan_id: str
    objective: Literal["cheapest", "fastest", "resilience"]
    lines: list[PlanLine] = Field(min_length=1)
    driver_id: str
    backup_driver_id: Optional[str] = None
    route_id: str
    estimated_cost_inr: float = Field(ge=0)
    estimated_eta_minutes: int = Field(ge=0)
    reason_summary: str

    def operational_signature(self) -> tuple:
        """Operational fingerprint used for distinctness checks (NOT the label)."""
        return (
            tuple(sorted((ln.sku, ln.warehouse_id, ln.supplier_id, ln.procure_qty)
                         for ln in self.lines)),
            self.driver_id,
            self.backup_driver_id,
            self.route_id,
        )


# ============================================================================
# Faults (section 6)
# ============================================================================
class Fault(BaseModel):
    fault_id: str
    kind: Literal["supplier_down", "driver_cancelled", "stock_discrepancy"]
    target_id: str  # supplier_id / driver_id / "warehouse_id:sku"
    at_stage: str  # legal order state at which the fault fires
    magnitude: float = Field(default=1.0, ge=0.0, le=1.0)
    reason_summary: str = ""


# ============================================================================
# Agent I/O schemas (used as ADK output_schema)
# ============================================================================
class WorldBuilderOutput(BaseModel):
    """What the World Builder agent must return (world minus server-stamped meta).

    No free-form dict fields: the Gemini Developer API rejects
    `additionalProperties` in response schemas, so `generation_constraints` is
    stamped by the controller, not emitted by the model.
    """

    products: list[Product] = Field(min_length=1)
    warehouses: list[Warehouse] = Field(min_length=1)
    inventory: list[Inventory] = Field(min_length=1)
    suppliers: list[Supplier] = Field(min_length=1)
    drivers: list[Driver] = Field(min_length=1)
    routes: list[Route] = Field(min_length=1)
    orders: list[Order] = Field(min_length=1)


class StrategyOutput(BaseModel):
    """Exactly three operationally distinct candidate plans."""

    plans: list[Plan] = Field(min_length=3, max_length=3)


class ChaosOutput(BaseModel):
    """Two or three targeted, bounded fault cases."""

    faults: list[Fault] = Field(min_length=2, max_length=3)


class RecoveryChoice(BaseModel):
    """Recovery agent's chosen action over a failure."""

    action: Literal["reassign_driver", "switch_supplier", "escalate"]
    new_driver_id: Optional[str] = None
    new_supplier_id: Optional[str] = None
    reason_summary: str


# ============================================================================
# Trace / evidence envelopes (section 9)
# ============================================================================
class EventRecord(BaseModel):
    run_id: str
    order_id: Optional[str] = None
    world_id: Optional[str] = None
    timestamp: str
    agent: str
    event_type: str
    tool: Optional[str] = None
    status: str
    duration_ms: int = 0
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    before_version: Optional[int] = None
    after_version: Optional[int] = None
    reason_summary: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
