"""Central configuration for PARADOX.

Loads environment (from .env when present), exposes the model id, API-key
presence, paths, prompt versions, and tournament policy constants. No LLM or
network calls happen here.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()  # load .env from CWD if present; no-op otherwise
except Exception:  # pragma: no cover - dotenv always installed, defensive only
    pass


# --- Paths -------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
DEFAULT_DB_PATH = os.environ.get("PARADOX_DB", str(REPO_ROOT / "paradox_state.db"))
WORLD_SNAPSHOT_PATH = ARTIFACTS_DIR / "world_snapshot.json"


# --- Model / credentials -----------------------------------------------------
# Default to the stable "flash latest" alias: specific dated flash ids (e.g.
# gemini-2.5-flash) are periodically retired for new API keys, and the alias
# resolves to the current flash model. Override with PARADOX_MODEL.
MODEL_ID = os.environ.get("PARADOX_MODEL", "gemini-flash-latest")
# ADK reads GOOGLE_API_KEY / GOOGLE_GENAI_USE_VERTEXAI directly from env.
USE_VERTEX = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"


def api_key() -> str | None:
    """Return the Gemini API key if configured, else None.

    Checks GOOGLE_API_KEY first, then GEMINI_API_KEY as a common alias.
    """
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    return key.strip() if key and key.strip() else None


def has_api_key() -> bool:
    return api_key() is not None


# --- Prompt versioning (recorded in provenance) ------------------------------
PROMPT_VERSION = "pv-1"


# --- Domain fixed constants (allowed per spec: enums, IDs, bounds) -----------
CURRENCY = "INR"
CITY = "Novapur"  # fictional metro, not a real place

FAULT_KINDS = ("supplier_down", "driver_cancelled", "stock_discrepancy")

# Legal execution state machine (section 7 of the spec).
ORDER_STATES = (
    "CREATED",
    "STOCK_CHECKED",
    "PROCUREMENT_REQUIRED",
    "PROCUREMENT_COMPLETED",
    "RESERVED",
    "PACKED",
    "DISPATCHED",
    "DELIVERED",
    "VERIFIED",
    "FAILED",
    "RECOVERING",
    "ESCALATED",
)

# Legal forward transitions; recovery transitions handled explicitly in storage.
LEGAL_TRANSITIONS = {
    "CREATED": {"STOCK_CHECKED"},
    "STOCK_CHECKED": {"PROCUREMENT_REQUIRED", "RESERVED"},
    "PROCUREMENT_REQUIRED": {"PROCUREMENT_COMPLETED"},
    "PROCUREMENT_COMPLETED": {"RESERVED"},
    "RESERVED": {"PACKED"},
    "PACKED": {"DISPATCHED", "FAILED"},
    "DISPATCHED": {"DELIVERED", "FAILED"},
    "DELIVERED": {"VERIFIED"},
    "VERIFIED": set(),
    "FAILED": {"RECOVERING", "ESCALATED"},
    "RECOVERING": {"PACKED", "DISPATCHED", "RESERVED", "ESCALATED"},
    "ESCALATED": set(),
}

# --- Tournament scoring policy (demo policy, not a proven optimum) -----------
SCORE_WEIGHTS = {
    "resilience": 0.40,
    "deadline": 0.30,
    "cost": 0.20,
    "verification": 0.10,
}

# Feedback: smoothed reliability estimator (successes + ALPHA) / (total + BETA).
RELIABILITY_ALPHA = 2
RELIABILITY_BETA = 4

# Recovery is bounded.
MAX_RECOVERY_ATTEMPTS = 2

# Deterministic RNG seed controlling ONLY simulator fault trials.
DEFAULT_SIM_SEED = 1729


def ensure_artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR
