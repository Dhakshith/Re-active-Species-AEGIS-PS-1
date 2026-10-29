"""
Live Agent DAG, Vector Clock & Event Log Panels (`ui/dag_panels.py`).
Uses `@st.fragment(run_every=1.0)` to poll `session.snapshot()` read-only
and render the real-time Graphviz causal lineage DAG (including all Chat/Proxy
errors, RBAC 403 blocks, Pre-Commit 409 blocks, and 503 outages) and HTTP proxy event stream.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import streamlit as st

from ui.state import SessionState


def _build_dag_dot(
    telemetry: dict[str, Any],
    transcript: Optional[list[dict[str, Any]]] = None,
    last_error: Optional[str] = None,
) -> str:
    """
    Build Graphviz DOT specification for the multi-agent hierarchy, A2A lineage,
    taint state, AND any errors/blocks encountered in Chat or the Control Plane proxy.
    """
    proc_table = telemetry.get("c3_tasks", {}).get("process_table", {})
    taint_status = telemetry.get("c4_lineage", {}).get("taint_status", {})
    revoked_set = set(telemetry.get("c1_iam", {}).get("revocation_set", []))
    dag_edges = telemetry.get("c4_lineage", {}).get("dag_edges", [])
    budget_ledger = telemetry.get("c2_policy", {}).get("budget_ledger", {})
    freeze_states = telemetry.get("c3_tasks", {}).get("branch_freeze_states", {})
    event_log = telemetry.get("event_log", [])

    # Collect per-agent error/block history from both transcript and event_log
    agent_error_badges: dict[str, str] = {}
    error_nodes_dot: list[str] = []
    error_edges_dot: list[str] = []

    for idx, turn in enumerate(transcript or []):
        t_type = turn.get("type", "")
        if t_type in ("SYSTEM_BLOCK", "ERROR"):
            a_id = turn.get("agent_id", "Chief_Treasury_Orchestrator")
            target_tool = turn.get("target") or turn.get("receipt", {}).get("tool", "tool")
            http_code = turn.get("receipt", {}).get("http_code", 500)
            err_id = f"chat_err_{idx}"
            agent_error_badges[a_id] = f"HTTP {http_code} ({target_tool})"

            if http_code == 403:
                e_fill, e_border, e_font = "#2e1065", "#a855f7", "#f3e8ff"
                e_title = f"🚫 RBAC BLOCKED (403)\\n{target_tool}"
            elif http_code == 409:
                e_fill, e_border, e_font = "#450a0a", "#ef4444", "#fecaca"
                e_title = f"🔴 GATE L4 BLOCKED (409)\\n{target_tool}"
            elif http_code in (503, 504):
                e_fill, e_border, e_font = "#431407", "#f97316", "#ffedd5"
                e_title = f"⚠️ OUTAGE ({http_code})\\n{target_tool}"
            else:
                e_fill, e_border, e_font = "#450a0a", "#ef4444", "#fecaca"
                e_title = f"❌ ERROR ({http_code})\\n{target_tool}"

            error_nodes_dot.append(
                f'  "{err_id}" [label="{e_title}", shape=octagon, fillcolor="{e_fill}", color="{e_border}", fontcolor="{e_font}"];'
            )
            error_edges_dot.append(
                f'  "{a_id}" -> "{err_id}" [label="HTTP {http_code} BLOCK/ERR", color="{e_border}", fontcolor="{e_border}", style="dashed", penwidth=2.2];'
            )

    # Also check kernel event_log for scripted scenario blocks/errors if transcript is empty
    if not error_nodes_dot:
        for idx, ev in enumerate(event_log):
            ev_type = ev.get("event_type", "")
            a_id = ev.get("agent_id", "")
            details = ev.get("details") or {}
            t_name = details.get("tool_name", "")
            if ev_type == "RBAC_BLOCKED_403":
                err_id = f"ev_err_{idx}"
                agent_error_badges[a_id] = f"HTTP 403 ({t_name})"
                error_nodes_dot.append(
                    f'  "{err_id}" [label="🚫 RBAC BLOCKED (403)\\n{t_name}", shape=octagon, fillcolor="#2e1065", color="#a855f7", fontcolor="#f3e8ff"];'
                )
                error_edges_dot.append(
                    f'  "{a_id}" -> "{err_id}" [label="HTTP 403", color="#a855f7", fontcolor="#a855f7", style="dashed", penwidth=2.2];'
                )
            elif ev_type == "GATE_L4_PRE_COMMIT_BLOCK":
                err_id = f"ev_err_{idx}"
                agent_error_badges[a_id] = f"HTTP 409 ({t_name})"
                error_nodes_dot.append(
                    f'  "{err_id}" [label="🔴 GATE L4 BLOCK (409)\\n{t_name}", shape=octagon, fillcolor="#450a0a", color="#ef4444", fontcolor="#fecaca"];'
                )
                error_edges_dot.append(
                    f'  "{a_id}" -> "{err_id}" [label="HTTP 409", color="#ef4444", fontcolor="#ef4444", style="dashed", penwidth=2.2];'
                )
            elif ev_type == "CHAOS_SERVICE_UNAVAILABLE_503":
                err_id = f"ev_err_{idx}"
                agent_error_badges[a_id] = f"HTTP 503 ({t_name})"
                error_nodes_dot.append(
                    f'  "{err_id}" [label="⚠️ HTTP 503 OUTAGE\\n{t_name}", shape=octagon, fillcolor="#431407", color="#f97316", fontcolor="#ffedd5"];'
                )
                error_edges_dot.append(
                    f'  "{a_id}" -> "{err_id}" [label="HTTP 503", color="#f97316", fontcolor="#f97316", style="dashed", penwidth=2.2];'
                )

    lines = [
        "digraph AEGIS_DAG {",
        '  graph [rankdir=TB, bgcolor="#0b0f19", pad="0.25", nodesep="0.45", ranksep="0.65", fontname="Inter,Helvetica"];',
        '  node [shape=box, style="filled,rounded", fontname="Inter,Helvetica", fontsize=10, margin="0.16,0.10", penwidth=2.0];',
        '  edge [fontname="Inter,Helvetica", fontsize=9, penwidth=1.6];',
    ]

    branches: dict[str, list[dict[str, Any]]] = {
        "Root": [],
        "Branch_A": [],
        "Branch_B": [],
    }
    for agent_id, info in proc_table.items():
        b_id = info.get("branch_id", "Root")
        branches.setdefault(b_id, []).append(info)

    cluster_titles = {
        "Root": "L0 Root Orchestration & Human Governor",
        "Branch_A": "Branch A: Liquidity & SWIFT Settlement Bulkhead",
        "Branch_B": "Branch B: Compliance & Audit Bulkhead",
    }

    for b_id, nodes in branches.items():
        if not nodes:
            continue
        is_frozen = not freeze_states.get(b_id, True)
        cluster_border = "#f59e0b" if is_frozen else "#334155"
        cluster_label = cluster_titles.get(b_id, f"Branch: {b_id}") + (
            " [PAUSED: SOFT_FREEZE]" if is_frozen else ""
        )

        lines.append(f"  subgraph cluster_{b_id.replace('-', '_')} {{")
        lines.append(f'    label="{cluster_label}";')
        lines.append('    fontcolor="#cbd5e1";')
        lines.append("    fontsize=11;")
        lines.append('    style="rounded,dashed";')
        lines.append(f'    color="{cluster_border}";')
        lines.append('    bgcolor="#111827";')

        for info in nodes:
            a_id = info["agent_id"]
            status = info.get("status", "RUNNING")
            jti = info.get("jti", "")
            is_tainted = bool(taint_status.get(a_id, False))
            is_revoked = jti in revoked_set
            err_badge = agent_error_badges.get(a_id)

            if status == "BLOCKED_403":
                fill, border, font = "#2e1065", "#a855f7", "#f3e8ff"
                badge = "BLOCKED 403 (RBAC)"
            elif status in ("ERROR_503", "ERROR_504", "BLOCKED_429"):
                fill, border, font = "#431407", "#f97316", "#ffedd5"
                badge = status
            elif status == "ERROR_LLM" or is_tainted or status == "KILLED" or is_revoked:
                fill, border, font = "#450a0a", "#ef4444", "#fecaca"
                badge = (
                    "LLM / AGENT ERROR"
                    if status == "ERROR_LLM"
                    else ("TAINTED & REVOKED" if is_tainted else "KILLED / REVOKED")
                )
            elif status == "FROZEN" or is_frozen:
                fill, border, font = "#451a03", "#f59e0b", "#fef3c7"
                badge = "PAUSED (SOFT_FREEZE)"
            elif err_badge:
                # Agent encountered a block/error during the run (even if it later adapted)
                fill, border, font = "#3b0764", "#f43f5e", "#ffe4e6"
                badge = f"{status} | Last Err: {err_badge}"
            elif status == "IN_FLIGHT_TOOL":
                fill, border, font = "#1e3a8a", "#38bdf8", "#e0f2fe"
                badge = "IN-FLIGHT TOOL"
            else:
                fill, border, font = "#064e3b", "#10b981", "#d1fae5"
                badge = status

            b_info = budget_ledger.get(a_id, {})
            tok_str = f"{b_info.get('tokens_used', 0)}/{b_info.get('max_tokens', 0)}"
            label = f"{a_id}\\n[{badge}]\\nTokens: {tok_str}"
            lines.append(
                f'    "{a_id}" [label="{label}", fillcolor="{fill}", color="{border}", fontcolor="{font}"];'
            )
        lines.append("  }")

    # Structural parent -> child delegation edges
    for a_id, info in proc_table.items():
        parent_id = info.get("parent_id")
        if parent_id and parent_id in proc_table:
            lines.append(
                f'  "{parent_id}" -> "{a_id}" [label="delegates JWT", color="#64748b", fontcolor="#94a3b8", style="dashed"];'
            )

    # Causal A2A message edges
    for edge in dag_edges:
        sender = edge["sender_id"]
        receiver = edge["receiver_id"]
        summary = edge.get("payload_summary", {})
        fx_val = summary.get("fx_rate")
        edge_tainted = (
            taint_status.get(sender, False)
            and taint_status.get(receiver, False)
            and (fx_val is not None and float(fx_val) > 1.5)
        )
        e_color = "#ef4444" if edge_tainted else "#38bdf8"
        e_label = "A2A"
        if fx_val is not None:
            e_label = f"A2A (fx={fx_val})"
        elif "goal" in summary:
            e_label = "Signed Goal"
        lines.append(
            f'  "{sender}" -> "{receiver}" [label="{e_label}", color="{e_color}", fontcolor="{e_color}", penwidth=2.2];'
        )

    # Append Error / Block Nodes & Edges so any Chat error is clearly visible in the DAG
    lines.extend(error_nodes_dot)
    lines.extend(error_edges_dot)

    lines.append("}")
    return "\n".join(lines)


@st.fragment(run_every=1.0)
def render_live_dag_fragment(session: SessionState) -> None:
    """Auto-refreshing 1.0s fragment rendering the Live Agent DAG, Vector Clocks, and Event Stream."""
    snap = session.snapshot()
    telemetry = snap["telemetry"]
    transcript = snap["transcript"]
    step_snapshots = snap.get("step_snapshots", [])
    waiting_for_step = bool(snap.get("waiting_for_step", False))
    last_error = snap["last_error"]

    total_steps = len(transcript)
    if total_steps > 0:
        raw_sel = st.session_state.get("aegis_selected_step")
        if raw_sel is None or int(raw_sel) >= total_steps:
            active_idx = total_steps - 1
            is_live_view = True
        else:
            active_idx = max(0, int(raw_sel))
            is_live_view = False

        with st.container(border=True):
            d1, d2, d3, d4, d5 = st.columns([0.8, 1.0, 2.4, 1.0, 0.8], vertical_alignment="center")
            with d1:
                if st.button(
                    "⏮️",
                    key="btn_dag_step_first",
                    disabled=(active_idx <= 0),
                    use_container_width=True,
                    help="First step",
                ):
                    st.session_state["aegis_selected_step"] = 0
                    st.rerun()
            with d2:
                if st.button(
                    "⏪ Back",
                    key="btn_dag_step_prev",
                    disabled=(active_idx <= 0),
                    use_container_width=True,
                ):
                    st.session_state["aegis_selected_step"] = max(0, active_idx - 1)
                    st.rerun()
            with d3:
                cur_turn = transcript[active_idx]
                cur_label = f"{cur_turn.get('agent_id')} · {cur_turn.get('type')}"
                if is_live_view:
                    st.markdown(
                        f"<div style='text-align:center;'>🔴 <b>LIVE DAG</b> · Step {total_steps}/{total_steps} (<code>{cur_label}</code>)</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='text-align:center;color:#fbbf24;'>⏳ <b>Step {active_idx + 1}</b>/{total_steps} (<code>{cur_label}</code>)</div>",
                        unsafe_allow_html=True,
                    )
            with d4:
                can_advance_history = active_idx < total_steps - 1
                if st.button(
                    "Next ⏩",
                    key="btn_dag_step_next",
                    disabled=(not can_advance_history and not waiting_for_step),
                    use_container_width=True,
                ):
                    if can_advance_history:
                        next_i = active_idx + 1
                        st.session_state["aegis_selected_step"] = (
                            None if next_i >= total_steps - 1 else next_i
                        )
                    elif waiting_for_step:
                        st.session_state["aegis_selected_step"] = None
                        session.step_forward_live()
                    st.rerun()
            with d5:
                if st.button(
                    "⏭️ Live",
                    key="btn_dag_step_latest",
                    disabled=is_live_view,
                    use_container_width=True,
                ):
                    st.session_state["aegis_selected_step"] = None
                    st.rerun()

        if not is_live_view and active_idx < len(step_snapshots):
            telemetry = step_snapshots[active_idx].get("telemetry", telemetry)
            transcript = transcript[: active_idx + 1]

    if last_error:
        st.error(f"**Active Session / LLM Error (Reflected in DAG):** `{last_error}`")

    col_dag, col_events = st.columns([1.25, 1.0])
    with col_dag:
        st.markdown("#### 🕸️ Live Agent Execution Topology, Blocks & Causal Lineage")
        dot_code = _build_dag_dot(
            telemetry=telemetry,
            transcript=transcript,
            last_error=last_error,
        )
        st.graphviz_chart(dot_code, use_container_width=True)

        vc_map = telemetry.get("c4_lineage", {}).get("vector_clocks", {})
        if vc_map:
            st.markdown("##### ⏱️ C4 Vector Clocks by Branch")
            st.json(vc_map)

    with col_events:
        st.markdown("#### 📜 Live Out-of-Band HTTP Proxy Event Log")
        events = list(reversed(telemetry.get("event_log", [])))
        if events:
            rows = []
            for ev in events[:30]:
                rows.append(
                    {
                        "Event ID": ev.get("event_id"),
                        "Type": ev.get("event_type"),
                        "Agent": ev.get("agent_id"),
                        "Branch": ev.get("branch_id"),
                        "Summary": ev.get("summary"),
                    }
                )
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                height=420,
            )
        else:
            st.info("No proxy events recorded yet. Submit a goal in Chat or trigger a Scenario.")

        taint_reasons = telemetry.get("c4_lineage", {}).get("taint_reasons", {})
        if taint_reasons:
            st.error(
                "**Active Causal Taint Reasons ($T_{\\text{fault}}$):**\n\n"
                + "\n".join(f"- `{k}`: {v}" for k, v in taint_reasons.items())
            )
