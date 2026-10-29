"""Streamlit Live Observability, Causal Lineage Visualizer, Simultaneous Multi-Agent/Multi-Tool Chaos Injector & Kill-Switch UI."""

from __future__ import annotations

import asyncio
import html
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st

from agents.treasury_graph import TreasuryWorkflowRunner
from control_plane.methods import ControlPlaneKernel, kernel_singleton
from control_plane.models import VALID_FAULT_CATEGORIES


st.set_page_config(
    page_title="AEGIS PS01 — Zero-Trust Agent Control Plane",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _render_html(raw_html: str) -> None:
    """Render HTML cleanly in Streamlit by stripping leading line indentation to avoid Markdown code blocks."""
    cleaned = "\n".join(line.strip() for line in raw_html.splitlines() if line.strip())
    st.markdown(cleaned, unsafe_allow_html=True)


def _inject_custom_css() -> None:
    _render_html(
        """
        <style>
        .stApp {
            background-color: #070b14;
            color: #e2e8f0;
        }
        section[data-testid="stSidebar"] {
            background-color: #0b1120;
            border-right: 1px solid #1e293b;
        }
        .aegis-banner {
            background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 55%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 18px 24px;
            margin-bottom: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
        }
        .aegis-banner-title {
            font-size: 1.45rem;
            font-weight: 800;
            color: #f8fafc;
            letter-spacing: -0.02em;
            margin: 0 0 4px 0;
        }
        .aegis-banner-sub {
            font-size: 0.86rem;
            color: #94a3b8;
            margin: 0;
        }
        .aegis-posture-pill {
            padding: 8px 16px;
            border-radius: 999px;
            font-weight: 700;
            font-size: 0.82rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            border: 1px solid rgba(255,255,255,0.15);
        }
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 14px;
            margin-bottom: 18px;
        }
        .kpi-card {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 14px 16px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }
        .kpi-label {
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #94a3b8;
            font-weight: 600;
            margin-bottom: 6px;
        }
        .kpi-value {
            font-size: 1.55rem;
            font-weight: 800;
            color: #f8fafc;
            margin-bottom: 6px;
        }
        .kpi-sla {
            font-size: 0.74rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 4px;
            display: inline-block;
        }
        .sla-pass {
            background: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .sla-warn {
            background: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.35);
        }
        .section-box {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 16px 18px;
            margin-bottom: 16px;
        }
        .section-header {
            font-size: 1.02rem;
            font-weight: 700;
            color: #f1f5f9;
            margin-bottom: 12px;
            border-bottom: 1px solid #1e293b;
            padding-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .agent-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
            gap: 12px;
        }
        .agent-card {
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 8px;
            padding: 12px 14px;
            position: relative;
        }
        .agent-name {
            font-size: 0.9rem;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 4px;
            word-break: break-word;
        }
        .agent-meta {
            font-size: 0.75rem;
            color: #94a3b8;
            margin-bottom: 8px;
        }
        .status-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 999px;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.03em;
        }
        .progress-track {
            width: 100%;
            height: 6px;
            background: #1e293b;
            border-radius: 3px;
            overflow: hidden;
            margin-top: 6px;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #38bdf8, #6366f1);
        }
        .aegis-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.82rem;
            color: #e2e8f0;
        }
        .aegis-table th {
            background: #1e293b;
            color: #94a3b8;
            text-transform: uppercase;
            font-size: 0.72rem;
            letter-spacing: 0.04em;
            padding: 9px 12px;
            text-align: left;
            border-bottom: 1px solid #334155;
        }
        .aegis-table td {
            padding: 9px 12px;
            border-bottom: 1px solid #1e293b;
            vertical-align: middle;
        }
        .aegis-table tr:hover td {
            background: rgba(30, 41, 59, 0.45);
        }
        .code-chip {
            font-family: monospace;
            font-size: 0.76rem;
            background: #1e293b;
            color: #38bdf8;
            padding: 2px 6px;
            border-radius: 4px;
        }
        </style>
        """
    )


def _get_kernel_and_runner() -> tuple[ControlPlaneKernel, TreasuryWorkflowRunner]:
    if "aegis_kernel" not in st.session_state:
        st.session_state["aegis_kernel"] = kernel_singleton
        st.session_state["aegis_runner"] = TreasuryWorkflowRunner(
            kernel=st.session_state["aegis_kernel"]
        )
        st.session_state["last_run"] = asyncio.run(
            st.session_state["aegis_runner"].run_scenario_1(reset_kernel=True)
        )
    return st.session_state["aegis_kernel"], st.session_state["aegis_runner"]


def _status_style(status: str, is_tainted: bool) -> tuple[str, str, str]:
    """Return (hex_bg, hex_text, border_hex) for an agent or tool status."""
    if is_tainted or status in ("TAINTED", "BLOCKED_403", "OUTAGE_DETECTED"):
        return ("rgba(239, 68, 68, 0.18)", "#f87171", "#ef4444")
    if status in (
        "RESTORED",
        "PAUSED",
        "IN_FLIGHT_TOOL",
        "FAILOVER_ACTIVE",
        "FAULT_ARMED",
    ):
        return ("rgba(245, 158, 11, 0.18)", "#fbbf24", "#f59e0b")
    if status == "KILLED":
        return ("rgba(107, 114, 128, 0.22)", "#9ca3af", "#6b7280")
    return ("rgba(16, 185, 129, 0.18)", "#34d399", "#10b981")


def _build_graphviz_dot(state: dict[str, Any]) -> str:
    proc_table = state["C3_task_registry"]["process_table"]
    taint_map = state["C4_lineage_graph"]["taint_status"]
    edges = state["C4_lineage_graph"]["dag_edges"]

    lines = [
        "digraph AEGIS_Hierarchy {",
        '  graph [bgcolor="#0f172a", rankdir=TB, pad="0.25", nodesep="0.45", ranksep="0.65"];',
        '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=10, fontcolor="#f8fafc", penwidth=2];',
        '  edge [fontname="Helvetica", fontsize=9, color="#64748b"];',
    ]

    for agent_id, info in proc_table.items():
        status = info.get("status", "READY")
        is_tainted = bool(taint_map.get(agent_id, False))
        _, _, border_hex = _status_style(status, is_tainted)
        fill_map = {
            "#ef4444": "#7f1d1d",
            "#f59e0b": "#78350f",
            "#6b7280": "#374151",
            "#10b981": "#064e3b",
        }
        fill = fill_map.get(border_hex, "#064e3b")
        branch = info.get("branch_id", "Root")
        jti_short = str(info.get("jti", ""))[-8:]
        label = f"{agent_id}\\n[{branch} | {status}]\\nJTI: ..{jti_short}"
        lines.append(
            f'  "{agent_id}" [label="{label}", fillcolor="{fill}", color="{border_hex}"];'
        )
        parent_id = info.get("parent_id")
        if parent_id:
            lines.append(
                f'  "{parent_id}" -> "{agent_id}" [label="delegates", color="#475569", style="dashed"];'
            )

    for edge in edges[-12:]:
        s_id = edge["sender_id"]
        r_id = edge["receiver_id"]
        summary = edge.get("payload_summary", {})
        if "fx_rate" in summary and summary["fx_rate"] is not None:
            edge_label = f"A2A fx={summary['fx_rate']}"
        elif "kyc_status" in summary and summary["kyc_status"] is not None:
            edge_label = f"A2A kyc={summary['kyc_status']}"
        else:
            edge_label = "A2A"
        lines.append(
            f'  "{s_id}" -> "{r_id}" [label="{edge_label}", color="#38bdf8", penwidth=1.6];'
        )

    lines.append("}")
    return "\n".join(lines)


def _render_banner_and_kpis(state: dict[str, Any], last_run: dict[str, Any]) -> None:
    metrics = state["metrics"]
    proc_table = state["C3_task_registry"]["process_table"]
    statuses = {info.get("status") for info in proc_table.values()}

    if "KILLED" in statuses and len(statuses) == 1:
        posture_text = "GLOBAL HARD KILL ENGAGED"
        posture_bg = "background: rgba(239, 68, 68, 0.25); color: #fca5a5; border-color: #ef4444;"
    elif "PAUSED" in statuses:
        posture_text = "SOFT-FREEZE OPERATOR GATE"
        posture_bg = "background: rgba(245, 158, 11, 0.25); color: #fcd34d; border-color: #f59e0b;"
    elif (
        metrics["total_failover_events"] > 0
        or metrics["total_agent_respawns"] > 0
        or metrics["total_subtree_rollbacks"] > 0
    ):
        posture_text = "AUTONOMOUS MULTI-FAULT RECOVERY"
        posture_bg = "background: rgba(56, 189, 248, 0.2); color: #7dd3fc; border-color: #38bdf8;"
    else:
        posture_text = "NOMINAL ZERO-TRUST ENFORCEMENT"
        posture_bg = "background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border-color: #10b981;"

    scen_title = html.escape(
        f"Scenario {last_run.get('scenario_id', 1)}: {last_run.get('scenario_name', 'Normal Execution')}"
    )
    eur_val = last_run.get("settlement_eur")
    eur_str = f"€{eur_val:,.2f} EUR" if isinstance(eur_val, (int, float)) else "HALTED / KILLED"
    fx_val = last_run.get("fx_rate")
    fx_str = f"{fx_val:.4f}" if isinstance(fx_val, (int, float)) else "N/A"
    prov_str = html.escape(str(last_run.get("provider") or "N/A"))

    _render_html(
        f"""
        <div class="aegis-banner">
            <div>
                <div class="aegis-banner-title">🛡️ AEGIS 2026 — Zero-Trust Agent Identity, Recovery &amp; Kill-Switch Control Plane</div>
                <div class="aegis-banner-sub">
                    Active Run: <strong style="color:#e2e8f0;">{scen_title}</strong> &nbsp;|&nbsp;
                    Invoice: <span class="code-chip">INV-2026-9981 ($2,000,000 USD)</span> &nbsp;|&nbsp;
                    Settlement: <strong style="color:#34d399;">{eur_str}</strong> (FX: <span class="code-chip">{fx_str}</span> via <span class="code-chip">{prov_str}</span>)
                </div>
            </div>
            <div class="aegis-posture-pill" style="{posture_bg}">
                {posture_text}
            </div>
        </div>
        """
    )

    t_kill = float(metrics["kill_switch_latency_ms"])
    r_blast = float(metrics["blast_radius_pct"])
    rbac_rate = float(metrics["rbac_block_rate_pct"])
    mttr = float(metrics["mttr_seconds"])
    wasted_pct = float(metrics["wasted_token_overhead_pct"])

    _render_html(
        f"""
        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-label">Kill-Switch Latency (T_kill)</div>
                <div class="kpi-value">{t_kill:.3f} ms</div>
                <span class="kpi-sla {'sla-pass' if t_kill < 50.0 else 'sla-warn'}">SLA Target: &lt; 50.0 ms</span>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Blast Radius Containment</div>
                <div class="kpi-value">{r_blast:.1f}%</div>
                <span class="kpi-sla {'sla-pass' if r_blast > 90.0 else 'sla-warn'}">SLA Target: &gt; 90.0%</span>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">RBAC Escalation Block Rate</div>
                <div class="kpi-value">{rbac_rate:.1f}%</div>
                <span class="kpi-sla sla-pass">100% Zero-Trust Gate</span>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Recovery Latency (MTTR)</div>
                <div class="kpi-value">{mttr:.4f} s</div>
                <span class="kpi-sla {'sla-pass' if mttr < 5.0 else 'sla-warn'}">SLA Target: &lt; 5.0 s</span>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Resilience Counters</div>
                <div class="kpi-value" style="font-size:1.25rem;">
                    {metrics['total_agent_respawns']} Respawns / {metrics['total_failover_events']} Failovers
                </div>
                <span class="kpi-sla sla-pass">Wasted Tokens: {wasted_pct:.1f}% | Rollbacks: {metrics['total_subtree_rollbacks']}</span>
            </div>
        </div>
        """
    )


def _render_agent_cards_html(state: dict[str, Any]) -> None:
    proc_table = state["C3_task_registry"]["process_table"]
    taint_map = state["C4_lineage_graph"]["taint_status"]
    budgets = state["C2_policy_store"]["budget_ledger"]
    active_tokens = state["C1_iam_engine"]["active_tokens"]

    cards_html: list[str] = []
    for agent_id, info in proc_table.items():
        status = info.get("status", "READY")
        is_tainted = bool(taint_map.get(agent_id, False))
        bg_rgba, txt_hex, border_hex = _status_style(status, is_tainted)
        jti = info.get("jti", "")
        scopes = active_tokens.get(jti, {}).get("scopes", [])
        scopes_str = ", ".join(scopes) if scopes else "none"

        b_info = budgets.get(agent_id, {"tokens_used": 0, "max_tokens": 2000, "calls_made": 0})
        used = int(b_info.get("tokens_used", 0))
        max_t = max(int(b_info.get("max_tokens", 2000)), 1)
        pct = min(round((used / max_t) * 100, 1), 100)

        cards_html.append(
            f"""
            <div class="agent-card" style="border-left: 4px solid {border_hex};">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <div class="agent-name">{html.escape(agent_id)}</div>
                    <span class="status-badge" style="background:{bg_rgba}; color:{txt_hex}; border:1px solid {border_hex};">
                        {html.escape(status)}
                    </span>
                </div>
                <div class="agent-meta">
                    Branch: <strong>{html.escape(str(info.get('branch_id')))}</strong> &nbsp;|&nbsp;
                    Role: {html.escape(str(info.get('role')))}
                </div>
                <div style="font-size:0.74rem; color:#cbd5e1; margin-bottom:4px;">
                    Passport JTI: <span class="code-chip">{html.escape(jti[-14:])}</span>
                </div>
                <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:6px;">
                    Scopes: <span style="color:#38bdf8;">{html.escape(scopes_str)}</span>
                </div>
                <div style="font-size:0.72rem; color:#94a3b8; display:flex; justify-content:space-between;">
                    <span>Tokens: {used} / {max_t}</span>
                    <span>Calls: {b_info.get('calls_made', 0)}</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill" style="width:{pct}%;"></div>
                </div>
            </div>
            """
        )

    _render_html(
        f"""
        <div class="section-box">
            <div class="section-header">
                <span>🤖 L0 → L2 Multi-Agent Fleet Status &amp; Cryptographic Passports</span>
                <span style="font-size:0.78rem; color:#94a3b8;">7 Autonomous Agents Across Dual Bulkheads</span>
            </div>
            <div class="agent-grid">
                {''.join(cards_html)}
            </div>
        </div>
        """
    )


def _render_tool_health_matrix_html(state: dict[str, Any]) -> None:
    tool_reg = state["C6_chaos_monkey"].get("tool_health_registry", {})
    rbac_mat = state["C2_policy_store"].get("rbac_matrix", {})

    rows_html: list[str] = []
    for tool_name, info in tool_reg.items():
        status = info.get("status", "HEALTHY")
        bg_rgba, txt_hex, border_hex = _status_style(status, False)
        mutates = rbac_mat.get(tool_name, {}).get("mutates_state", False)
        gate_label = "Gate L4 Pre-Commit Write" if mutates else "HMAC Provenance Read"
        fallbacks = " → ".join(info.get("fallback_providers", []))
        last_err = html.escape(str(info.get("last_error") or "None"))

        rows_html.append(
            f"""
            <tr>
                <td><span class="code-chip">{html.escape(tool_name)}</span></td>
                <td>{gate_label}</td>
                <td><span class="code-chip">{html.escape(str(info.get('primary_provider')))}</span></td>
                <td style="color:#94a3b8;">{html.escape(fallbacks)}</td>
                <td><strong style="color:#38bdf8;">{html.escape(str(info.get('active_provider')))}</strong></td>
                <td>
                    <span class="status-badge" style="background:{bg_rgba}; color:{txt_hex}; border:1px solid {border_hex};">
                        {html.escape(status)}
                    </span>
                </td>
                <td>{info.get('outage_events', 0)} outages / {info.get('failover_switches', 0)} failovers</td>
                <td style="font-size:0.75rem; color:#f87171;">{last_err}</td>
            </tr>
            """
        )

    _render_html(
        f"""
        <div class="section-box">
            <div class="section-header">
                <span>🛠️ Multi-Tool Resilience &amp; Provider Failover Matrix</span>
                <span style="font-size:0.78rem; color:#94a3b8;">Automatic Primary → Secondary → Tertiary Failover</span>
            </div>
            <table class="aegis-table">
                <thead>
                    <tr>
                        <th>Tool Scope</th>
                        <th>Verification Gate</th>
                        <th>Primary Provider</th>
                        <th>Fallback Chain</th>
                        <th>Active Provider</th>
                        <th>Health Status</th>
                        <th>Outages / Failovers</th>
                        <th>Last Recorded Fault</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows_html)}
                </tbody>
            </table>
        </div>
        """
    )


def main() -> None:
    _inject_custom_css()
    kernel, runner = _get_kernel_and_runner()

    # =========================================================================
    # Sidebar: Scenarios 1–6, Simultaneous Multi-Agent/Tool Matrix & Kill-Switch
    # =========================================================================
    with st.sidebar:
        st.markdown("### ⚡ One-Click Scenario Suite")
        col_s1, col_s2 = st.columns(2)
        if col_s1.button("▶️ Scenario 1\n(Normal)", use_container_width=True):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_1(reset_kernel=True)
            )
        if col_s2.button("🚫 Scenario 2\n(403 RBAC)", use_container_width=True):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_2(reset_kernel=True)
            )

        col_s3, col_s4 = st.columns(2)
        if col_s3.button("🧬 Scenario 3\n(BFS Rollback)", use_container_width=True):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_3(reset_kernel=True)
            )
        if col_s4.button("🔌 Scenario 4\n(503 Failover)", use_container_width=True):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_4(reset_kernel=True)
            )

        if st.button(
            "🛑 Scenario 5 (Soft-Freeze & <50ms Hard Kill)",
            use_container_width=True,
        ):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_5(reset_kernel=True)
            )

        if st.button(
            "🌪️ Scenario 6: Simultaneous Multi-Agent & Multi-Tool Storm",
            use_container_width=True,
            type="primary",
        ):
            st.session_state["last_run"] = asyncio.run(
                runner.run_scenario_6(reset_kernel=True)
            )

        st.divider()
        st.markdown("### 🧪 Simultaneous Multi-Failure Injector")
        selected_agents = st.multiselect(
            "Simultaneous Target Agents",
            options=[
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
                "Sanctions_Screener",
                "Audit_Logger",
                "FX_Settlement_Supervisor",
                "Compliance_Supervisor",
            ],
            default=["FX_Quote_Reader", "Sanctions_Screener"],
        )
        agent_fault_cat = st.selectbox(
            "Agent Fault Type",
            options=["PRIVILEGE_ESCALATION", "CORRUPTED_OUTPUT", "RUNAWAY_LOOP"],
        )

        selected_tools = st.multiselect(
            "Simultaneous Target Tools",
            options=["erp:read", "fx:quote", "kyc:check", "audit:log", "swift:wire"],
            default=["erp:read", "fx:quote", "kyc:check"],
        )
        tool_fault_cat = st.selectbox(
            "Tool Fault Type",
            options=["SERVICE_UNAVAILABLE", "NETWORK_OUTAGE"],
        )

        if st.button(
            "⚡ Inject Batch & Execute Resilient Run",
            use_container_width=True,
            type="primary",
        ):
            kernel.reset()
            batch_faults: list[dict[str, Any]] = []
            for ag in selected_agents:
                batch_faults.append(
                    {
                        "target": ag,
                        "fault_category": agent_fault_cat,
                        "fault_config": {"multiplier": 10.0},
                    }
                )
            for tl in selected_tools:
                batch_faults.append(
                    {
                        "target": tl,
                        "fault_category": tool_fault_cat,
                        "fault_config": {"fast_failover": True},
                    }
                )
            kernel.inject_multi_chaos_faults(batch_faults)
            st.session_state["last_run"] = asyncio.run(
                runner.run_custom_live_workflow()
            )

        st.divider()
        st.markdown("### 🚨 Multi-Granular Kill-Switch")
        if st.button(
            "🔴 GLOBAL HARD KILL-SWITCH (ALL)",
            use_container_width=True,
            type="primary",
        ):
            receipt = asyncio.run(
                kernel.trigger_kill_switch(mode="GLOBAL_HARD", target_id="ALL")
            )
            st.session_state["last_kill_receipt"] = receipt

        surgical_targets = st.multiselect(
            "Surgical Target Agent(s)",
            options=[
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
                "FX_Settlement_Supervisor",
                "Sanctions_Screener",
                "Audit_Logger",
                "Compliance_Supervisor",
            ],
            default=["FX_Quote_Reader", "Sanctions_Screener"],
        )
        if st.button("✂️ SURGICAL KILL SELECTED AGENTS", use_container_width=True):
            target_str = ",".join(surgical_targets) if surgical_targets else "FX_Quote_Reader"
            receipt = asyncio.run(
                kernel.trigger_kill_switch(mode="SURGICAL", target_id=target_str)
            )
            st.session_state["last_kill_receipt"] = receipt

        override_fx = st.number_input(
            "Soft-Freeze Human Override FX Rate",
            value=0.9195,
            step=0.0005,
            format="%.4f",
        )
        col_frz1, col_frz2 = st.columns(2)
        if col_frz1.button("⏸️ Pause Branch A", use_container_width=True):
            receipt = asyncio.run(
                kernel.trigger_kill_switch(
                    mode="SOFT_FREEZE",
                    target_id="Branch_A",
                    operator_override_state=None,
                )
            )
            st.session_state["last_kill_receipt"] = receipt
        if col_frz2.button("⏯️ Resume + Override", use_container_width=True):
            receipt = asyncio.run(
                kernel.trigger_kill_switch(
                    mode="SOFT_FREEZE",
                    target_id="Branch_A",
                    operator_override_state={
                        "fx_rate": float(override_fx),
                        "settlement_eur": round(2_000_000.0 * float(override_fx), 2),
                    },
                )
            )
            st.session_state["last_kill_receipt"] = receipt

    # =========================================================================
    # Main Observability Content
    # =========================================================================
    state = kernel.get_full_state_and_metrics()
    last_run = st.session_state.get("last_run", {})

    # 1. Top Executive Banner & 5 KPI Cards
    _render_banner_and_kpis(state, last_run)

    # 2. Live Agent Fleet Cards & Multi-Tool Failover Matrix
    _render_agent_cards_html(state)
    _render_tool_health_matrix_html(state)

    # 3. Causal Lineage Graph & Latest Recovery / Respawn / Failover Telemetry
    col_graph, col_telemetry = st.columns([3, 2])
    with col_graph:
        _render_html(
            """
            <div class="section-box" style="margin-bottom:4px;">
                <div class="section-header" style="margin-bottom:4px; border-bottom:none; padding-bottom:0;">
                    <span>🌐 Live L0 → L2 Dual-Topology &amp; Causal Lineage DAG</span>
                    <span style="font-size:0.75rem; color:#94a3b8;">🟢 Healthy | 🟠 Restored/Paused | 🔴 Tainted/Blocked | ⚫ Killed</span>
                </div>
            </div>
            """
        )
        st.graphviz_chart(_build_graphviz_dot(state), use_container_width=True)

    with col_telemetry:
        respawns = state.get("agent_respawn_events", [])
        failovers = state.get("failover_events", [])
        rollbacks = state.get("recovery_history", [])

        respawn_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(r['agent_id'])}</span></td>"
            f"<td>{html.escape(r['branch_id'])}</td>"
            f"<td style='color:#f87171;'>{html.escape(str(r.get('old_revoked_jti', ''))[-10:])}</td>"
            f"<td style='color:#34d399;'>{html.escape(str(r.get('new_active_jti', ''))[-10:])}</td>"
            f"<td style='font-size:0.74rem;'>{html.escape(r['reason'])}</td></tr>"
            for r in respawns[-6:]
        ) or "<tr><td colspan='5' style='color:#64748b; text-align:center;'>No agent respawns triggered in this run</td></tr>"

        failover_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(f['tool_name'])}</span></td>"
            f"<td>{html.escape(f['agent_id'])}</td>"
            f"<td style='color:#f87171;'>{html.escape(f['failed_provider'])} ({f['http_code']})</td>"
            f"<td style='color:#38bdf8;'>→ {html.escape(f['recommended_fallback'])}</td></tr>"
            for f in failovers[-6:]
        ) or "<tr><td colspan='4' style='color:#64748b; text-align:center;'>No tool failovers triggered in this run</td></tr>"

        _render_html(
            f"""
            <div class="section-box">
                <div class="section-header">
                    <span>🔄 Autonomous Agent Respawn Log ({len(respawns)})</span>
                </div>
                <table class="aegis-table">
                    <thead>
                        <tr><th>Agent</th><th>Branch</th><th>Revoked JTI</th><th>New JTI</th><th>Trigger</th></tr>
                    </thead>
                    <tbody>{respawn_rows}</tbody>
                </table>
            </div>
            <div class="section-box">
                <div class="section-header">
                    <span>🔌 Multi-Tool Automatic Failover Log ({len(failovers)})</span>
                </div>
                <table class="aegis-table">
                    <thead>
                        <tr><th>Tool</th><th>Caller</th><th>Failed Provider</th><th>Failover Target</th></tr>
                    </thead>
                    <tbody>{failover_rows}</tbody>
                </table>
            </div>
            """
        )

        if rollbacks:
            latest_rb = rollbacks[-1]
            _render_html(
                f"""
                <div class="section-box">
                    <div class="section-header">
                        <span>⏪ Latest Subtree Rollback ({len(rollbacks)} Total)</span>
                        <span class="status-badge sla-pass">R_blast = {latest_rb['blast_radius_ratio']*100:.0f}%</span>
                    </div>
                    <div style="font-size:0.8rem; color:#cbd5e1; line-height:1.5;">
                        <strong>Branch Rolled Back:</strong> <span class="code-chip">{html.escape(latest_rb['branch_rolled_back'])}</span> &nbsp;|&nbsp;
                        <strong>Checkpoint:</strong> <span class="code-chip">{html.escape(latest_rb['restored_checkpoint_id'])}</span><br/>
                        <strong>Quarantined BFS Cone (T_fault):</strong> {html.escape(', '.join(latest_rb['tainted_bfs_nodes']))}<br/>
                        <strong>Gate L2/L3/L4 Reason:</strong> <span style="color:#fca5a5;">{html.escape(latest_rb['failure_reason'])}</span>
                    </div>
                </div>
                """
            )

    # 4. Deep-Dive Component State Tabs (C1 - C6)
    tab_c1, tab_c4, tab_c5, tab_c2_c6 = st.tabs(
        [
            "🔑 C1: Ed25519 JWT Passports & Revocations",
            "🔗 C4: Causal DAG Edges & HMAC Receipts",
            "💾 C5: Versioned Checkpoints (RAM + SQLite)",
            "⚙️ C2 & C6: RBAC Matrix & Chaos Fault Queues",
        ]
    )

    with tab_c1:
        tok_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(jti)}</span></td>"
            f"<td><strong>{html.escape(str(cl.get('sub')))}</strong></td>"
            f"<td>{html.escape(str(cl.get('parent') or 'ROOT'))}</td>"
            f"<td>{html.escape(str(cl.get('branch')))}</td>"
            f"<td>{html.escape(', '.join(cl.get('scopes', [])))}</td>"
            f"<td>{'<span style=\"color:#f87171;font-weight:700;\">REVOKED</span>' if jti in state['C1_iam_engine']['revocation_set'] else '<span style=\"color:#34d399;font-weight:700;\">ACTIVE</span>'}</td></tr>"
            for jti, cl in state["C1_iam_engine"]["active_tokens"].items()
        )
        _render_html(
            f"""
            <div class="section-box">
                <div class="section-header">
                    <span>C1: Issued Ed25519 Cryptographic Passports &amp; O(1) Revocation Status</span>
                    <span style="font-size:0.78rem; color:#94a3b8;">Revoked Count: {len(state['C1_iam_engine']['revocation_set'])}</span>
                </div>
                <table class="aegis-table">
                    <thead>
                        <tr><th>Token JTI</th><th>Subject Agent</th><th>Parent</th><th>Branch</th><th>Attenuated Scopes</th><th>Status</th></tr>
                    </thead>
                    <tbody>{tok_rows}</tbody>
                </table>
            </div>
            """
        )

    with tab_c4:
        edge_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(e['edge_id'])}</span></td>"
            f"<td>{html.escape(e['sender_id'])}</td>"
            f"<td>→ {html.escape(e['receiver_id'])}</td>"
            f"<td><span class='code-chip'>{html.escape(e['trace_id'])}</span></td>"
            f"<td style='font-family:monospace;font-size:0.74rem;'>{html.escape(json.dumps(e['vector_clock']))}</td></tr>"
            for e in state["C4_lineage_graph"]["dag_edges"]
        )
        receipt_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(tr_id)}</span></td>"
            f"<td><span class='code-chip'>{html.escape(rc['tool_name'])}</span></td>"
            f"<td style='font-family:monospace;font-size:0.74rem;color:#34d399;'>{html.escape(rc['hmac_sha256'][:24])}...</td>"
            f"<td style='font-size:0.75rem;'>{html.escape(json.dumps(rc['raw_output']))}</td></tr>"
            for tr_id, rc in state["C4_lineage_graph"]["provenance_receipts"].items()
        )
        _render_html(
            f"""
            <div class="section-box">
                <div class="section-header"><span>C4: Causal Lineage DAG Edges &amp; Lamport Vector Clocks</span></div>
                <table class="aegis-table">
                    <thead><tr><th>Edge ID</th><th>Sender</th><th>Receiver</th><th>Trace ID</th><th>Vector Clock</th></tr></thead>
                    <tbody>{edge_rows}</tbody>
                </table>
            </div>
            <div class="section-box">
                <div class="section-header"><span>C4: Ground-Truth Tool Provenance Receipts (HMAC-SHA256)</span></div>
                <table class="aegis-table">
                    <thead><tr><th>Trace ID</th><th>Tool</th><th>HMAC-SHA256 Digest</th><th>Canonical Output</th></tr></thead>
                    <tbody>{receipt_rows}</tbody>
                </table>
            </div>
            """
        )

    with tab_c5:
        snap_rows = "".join(
            f"<tr><td><span class='code-chip'>{html.escape(s['checkpoint_id'])}</span></td>"
            f"<td><strong>{html.escape(s['branch_id'])}</strong></td>"
            f"<td>{html.escape(s['created_by'])}</td>"
            f"<td style='font-family:monospace;font-size:0.75rem;'>{html.escape(json.dumps(s['state_preview']))}</td></tr>"
            for s in state["C5_snapshot_db"]["snapshots"]
        )
        _render_html(
            f"""
            <div class="section-box">
                <div class="section-header">
                    <span>C5: Versioned Subtree Checkpoints (Dual In-Memory + SQLite Persistence)</span>
                    <span style="font-size:0.78rem; color:#34d399;">SQLite Rows Verified: {state['C5_snapshot_db']['sql_snapshot_count']}</span>
                </div>
                <table class="aegis-table">
                    <thead><tr><th>Checkpoint ID</th><th>Branch</th><th>Created By</th><th>Clean Serialized State Preview</th></tr></thead>
                    <tbody>{snap_rows}</tbody>
                </table>
            </div>
            """
        )

    with tab_c2_c6:
        col_b, col_c = st.columns(2)
        with col_b:
            st.markdown("**C2: Tokenomics Budget Ledger**")
            st.json(state["C2_policy_store"]["budget_ledger"])
        with col_c:
            st.markdown("**C6: Multi-Agent & Multi-Tool Fault Queues**")
            st.json(state["C6_chaos_monkey"]["fault_queues"])


if __name__ == "__main__":
    main()
