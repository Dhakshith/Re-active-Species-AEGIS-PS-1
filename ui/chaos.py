"""
Sidebar Session Controls, Kill-Switch Triggers, Chaos Fault Console (with Fault Removal),
and Scripted Scenarios (`ui/chaos.py`).
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ui.state import SessionState


def render_sidebar_controls(session: SessionState, snap: dict[str, Any] | None = None) -> None:
    """Render the Streamlit sidebar with Session Boot/Reset, Kill-Switches, Chaos Injector + Remover, and A/B Scripts."""
    snap = snap or session.snapshot()
    telemetry = snap["telemetry"]
    proc_table = telemetry.get("c3_tasks", {}).get("process_table", {})
    reg_tools = telemetry.get("c2_policy", {}).get("registered_tools", {})

    with st.sidebar:
        st.markdown("## 🛡️ AEGIS-CONTROL")
        st.caption(
            f"**Session:** `{session.session_id[:12]}` &nbsp;|&nbsp; "
            f"**DB:** `snapshots.db`"
        )

        c_reset, c_stop = st.columns(2)
        with c_reset:
            if st.button("🔄 Reset", use_container_width=True, help="Reset session state, kernel, and fleet."):
                session.reset_session()
                st.toast("Session kernel & fleet reset cleanly.", icon="🔄")
                st.rerun()
        with c_stop:
            if st.button("⏹️ Stop", use_container_width=True, help="Cancel active background goal."):
                if session._current_goal_future and not session._current_goal_future.done():
                    session._current_goal_future.cancel()
                st.toast("Active goal cancelled.", icon="⏹️")
                st.rerun()

        st.divider()

        # =====================================================================
        # 1. Emergency Multi-Tier Kill-Switch Controls
        # =====================================================================
        with st.expander("🚨 Emergency Kill-Switch", expanded=False):
            if st.button(
                "🛑 GLOBAL HARD KILL (ALL AGENTS)",
                type="primary",
                use_container_width=True,
                help="Revoke all agent JWTs and terminate all live fleet sockets.",
            ):
                receipt = session.trigger_kill_switch(mode="GLOBAL_HARD", target_id="ALL")
                st.toast(
                    f"GLOBAL_HARD executed in {receipt['termination_latency_ms']:.2f} ms!",
                    icon="🛑",
                )
                st.rerun()

            st.markdown("---")
            agent_targets = list(proc_table.keys()) or [
                "FX_Quote_Reader",
                "FX_Settlement_Supervisor",
                "SWIFT_Wire_Executor",
            ]
            surg_target = st.selectbox(
                "Surgical Target",
                options=agent_targets,
                key="sb_surgical_target",
            )
            if st.button("✂️ Trigger Surgical Kill", use_container_width=True):
                receipt = session.trigger_kill_switch(
                    mode="SURGICAL", target_id=surg_target
                )
                st.toast(
                    f"SURGICAL kill on {surg_target} ({receipt['termination_latency_ms']:.2f} ms)",
                    icon="✂️",
                )
                st.rerun()

        # =====================================================================
        # 2. Live Chaos Fault Console (Inject & Remove Faults)
        # =====================================================================
        active_rules = telemetry.get("c6_chaos", {}).get("active_fault_rules", {})
        chaos_label = f"💥 Chaos Fault Console ({len(active_rules)} Armed)" if active_rules else "💥 Chaos Fault Console"
        with st.expander(chaos_label, expanded=bool(active_rules)):
            all_targets = list(proc_table.keys()) + list(reg_tools.keys())
            if not all_targets:
                all_targets = ["FX_Quote_Reader", "fx:quote", "swift:wire"]

            chaos_target = st.selectbox(
                "Fault Target",
                options=all_targets,
                key="sb_chaos_target",
            )
            chaos_cat = st.selectbox(
                "Fault Category",
                options=[
                    "CORRUPTED_OUTPUT",
                    "SERVICE_UNAVAILABLE",
                    "PRIVILEGE_ESCALATION",
                    "NETWORK_OUTAGE",
                ],
                key="sb_chaos_cat",
            )
            default_cfg = (
                '{"mutations": {"fx_rate": {"multiply": 10.0}, "settlement_eur": {"multiply": 10.0}}}'
                if chaos_cat == "CORRUPTED_OUTPUT"
                else (
                    '{"unauthorized_tool": "swift:wire"}'
                    if chaos_cat == "PRIVILEGE_ESCALATION"
                    else "{}"
                )
            )
            chaos_cfg_str = st.text_area(
                "Config (JSON)",
                value=default_cfg,
                height=65,
                key="sb_chaos_cfg",
            )
            if st.button("⚡ Arm Fault Rule", use_container_width=True):
                try:
                    parsed_cfg: dict[str, Any] = json.loads(chaos_cfg_str or "{}")
                    session.inject_fault(
                        target=chaos_target,
                        fault_category=chaos_cat,
                        probability=1.0,
                        config=parsed_cfg,
                    )
                    st.toast(f"Armed {chaos_cat} on {chaos_target}", icon="⚡")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Invalid JSON config: {exc}")

            # Active Fault List + Individual & Bulk Fault Removal
            if active_rules:
                st.markdown(f"**⚠️ Armed Fault Rules:**")
                for rule_key, rule_info in list(active_rules.items()):
                    r_col1, r_col2 = st.columns([2.0, 1.0])
                    with r_col1:
                        st.caption(f"🔸 `{rule_key}`")
                    with r_col2:
                        if st.button(
                            "🗑️",
                            key=f"rm_fault_{rule_key}",
                            use_container_width=True,
                            help=f"Remove fault {rule_key}",
                        ):
                            session.remove_fault(
                                target_or_id=rule_key,
                                clear_agent_status=True,
                            )
                            st.toast(f"Removed fault {rule_key}", icon="🗑️")
                            st.rerun()

                if st.button(
                    "🧹 Clear All Faults & Errors",
                    key="btn_clear_all_faults",
                    use_container_width=True,
                ):
                    session.remove_fault(target_or_id="ALL", clear_agent_status=True)
                    st.toast("Cleared all armed faults and reset agent error/taint states.", icon="🧹")
                    st.rerun()

        # =====================================================================
        # 3. Scripted Scenarios (A/B Comparison)
        # =====================================================================
        with st.expander("🧪 Benchmark Scenarios (A/B)", expanded=False):
            st.caption("Deterministic scenarios for A/B comparison against live NIM.")
            if st.button("▶️ Scenario 1: Normal Baseline", use_container_width=True):
                session.run_scripted_scenario("1")
                st.rerun()
            if st.button("🚫 Scenario 2: Confused Deputy (403)", use_container_width=True):
                session.run_scripted_scenario("2")
                st.rerun()
            if st.button("☣️ Scenario 3: 10x FX & BFS Rollback", use_container_width=True):
                session.run_scripted_scenario("3")
                st.rerun()
            if st.button("🔌 Scenario 4: HTTP 503 Failover", use_container_width=True):
                session.run_scripted_scenario("4")
                st.rerun()
            if st.button("⏸️ Scenario 5A: Soft-Freeze Override", use_container_width=True):
                session.run_scripted_scenario("5A")
                st.rerun()
            if st.button("⚡ Scenario 5B: Global Hard Kill (<50ms)", use_container_width=True):
                session.run_scripted_scenario("5B")
                st.rerun()
            if st.button("🚀 Run All 5 Scenarios", use_container_width=True):
                session.run_scripted_scenario("ALL")
                st.rerun()
                session.run_scripted_scenario("ALL")
                st.rerun()
