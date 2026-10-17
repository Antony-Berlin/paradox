"""PARADOX package root.

PARADOX: Predictive Autonomous Recovery & Digital Operations eXchange.

A simulated multi-agent logistics system that runs a counterfactual tournament:
ADK/Gemini agents synthesize the world, propose plans, inject faults, and recover
from failures; deterministic code performs all simulation, scoring, and
verification. "Real" here means genuine agent -> MCP -> persistent-state
transitions in a *simulated* logistics domain -- never an actual purchase or
courier dispatch.
"""

__version__ = "1.0.0"
