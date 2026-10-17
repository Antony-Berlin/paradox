"""Optional Streamlit viewer for PARADOX artifacts.

Renders the SAME artifacts/*.json produced by `python -m paradox demo` and
`... benchmark` -- it does not stage a disconnected sequence. Run:

    streamlit run app.py

If no artifacts exist yet, it tells you which command to run first.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

ART = Path(__file__).parent / "artifacts"


def _load(name: str):
    p = ART / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


st.set_page_config(page_title="PARADOX", layout="wide")
st.title("PARADOX — counterfactual logistics tournament")
st.caption("Simulated logistics. 'Real' = agent → MCP → persistent simulation, "
           "not a real purchase or courier dispatch.")

demo = _load("demo.json")
bench = _load("benchmark.json")

if not demo:
    st.warning("No artifacts/demo.json yet. Run:\n\n"
               "`python -m paradox demo --json-output artifacts/demo.json`")
    st.stop()

# --- headline metrics --------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Final order status", demo.get("final_order_status", "?"))
asserts = demo.get("assertions", [])
c2.metric("Assertions passed", f"{sum(a['ok'] for a in asserts)}/{len(asserts)}")
c3.metric("LLM agent calls", len(demo.get("model_calls", [])))
c4.metric("MCP tool calls", len(demo.get("mcp_calls", [])))

st.subheader("World provenance")
st.json({"world_id": demo.get("world_id"),
         "provenance": demo.get("world_provenance"),
         "world_hash": demo.get("world_hash"),
         "model": demo.get("model")})

# --- tournament --------------------------------------------------------------
st.subheader("Counterfactual tournament")
tour = demo.get("tournament", {})
st.write(f"**Champion:** `{tour.get('champion_plan_id')}` — "
         f"{tour.get('champion_explanation', '')}")
ranked = tour.get("ranked", [])
if ranked:
    st.dataframe([
        {"plan": s["plan_id"], "objective": s["objective"], "score": s["score"],
         "delivered": f"{s['raw_metrics']['delivered_cases']}/{s['raw_metrics']['fault_cases']}",
         "mean_cost": s["raw_metrics"]["mean_cost_inr"],
         "mean_eta": s["raw_metrics"]["mean_delivery_minutes"]}
        for s in ranked], use_container_width=True)
base = demo.get("baseline", {})
st.caption(f"Cheapest-first baseline: {base.get('selected_plan_id')} "
           f"(score {base.get('score', {}).get('score')})")

# --- isolation + recovery ----------------------------------------------------
col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Counterfactual isolation")
    st.json(demo.get("isolation", {}))
with col_b:
    st.subheader("Recovery verifications")
    for rv in demo.get("recovery_verifications", []):
        st.write(f"- **{rv.get('type')}** verified=`{rv.get('verified')}` "
                 f"(verifier `{rv.get('verifier_id')}`)")

# --- assertions --------------------------------------------------------------
st.subheader("Assertions")
for a in asserts:
    (st.success if a["ok"] else st.error)(
        f"{a['assertion']} — {a.get('detail','')}")

# --- MCP trace ---------------------------------------------------------------
with st.expander("MCP tool-call trace (redacted)"):
    st.dataframe(demo.get("mcp_calls", []), use_container_width=True)

# --- feedback ----------------------------------------------------------------
with st.expander("Reliability feedback (before / after)"):
    st.write("Before"); st.json(demo.get("feedback_before", {}))
    st.write("After"); st.json(demo.get("feedback_after", {}))

# --- benchmark ---------------------------------------------------------------
if bench:
    st.subheader("Benchmark — cheapest-first vs PARADOX")
    b1, b2, b3 = st.columns(3)
    b1.metric("PARADOX wins", bench.get("paradox_wins"))
    b2.metric("Baseline wins", bench.get("baseline_wins"))
    b3.metric("Ties", bench.get("ties"))
    st.dataframe(bench.get("trials", []), use_container_width=True)

st.subheader("Limitations")
for lim in demo.get("limitations", []):
    st.write(f"- {lim}")
