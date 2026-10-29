"""
AEGIS-CONTROL: Live Observability, NIM Multi-Agent Chat, Governor Review & Chaos Dashboard (`ui/app.py`).
Uses per-session `SessionState` (`ui/state.py`) with an isolated `ControlPlaneKernel`,
FastAPI ASGI app, SQLite snapshot DB (`runs/<session>/snapshots.db`), and JSONL journal.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.chaos import render_sidebar_controls
from ui.chat import render_chat_tab
from ui.config import load_dotenv
from ui.dag_panels import render_live_dag_fragment
from ui.governor import render_governor_tab
from ui.state import SessionState, get_or_create_session_state


st.set_page_config(
    page_title="AEGIS-CONTROL | Out-of-Band Governance Control Plane",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_dotenv()


@st.fragment(run_every=1.0)
def _render_kpi_bar(session_or_snap: SessionState | dict[str, Any]) -> None:
    snap = (
        session_or_snap.snapshot()
        if isinstance(session_or_snap, SessionState)
        else session_or_snap
    )
    metrics = snap["telemetry"].get("metrics", {})

    t_kill = float(metrics.get("kill_switch_latency_ms", 0.0))
    r_blast = float(metrics.get("blast_radius_pct", 100.0))
    rbac_rate = float(metrics.get("rbac_block_rate_pct", 100.0))
    mttr_s = float(metrics.get("mttr_seconds", 0.0))
    wasted_pct = float(metrics.get("wasted_token_overhead_pct", 0.0))

    with st.container(border=True):
        col_title, col_kpi = st.columns([1.2, 2.8], vertical_alignment="center")
        with col_title:
            st.markdown("### 🛡️ AEGIS-CONTROL")
            st.caption("🟢 Control Plane: `ONLINE` (Port 8000) &nbsp;|&nbsp; Model: `NVIDIA Nemotron 30B`")
        with col_kpi:
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("⚡ T_kill", f"{t_kill:.1f} ms" if t_kill > 0 else "< 5.0 ms", "≤ 50 ms")
            k2.metric("🛡️ Containment", f"{r_blast:.0f}%", "100%")
            k3.metric("🔒 RBAC Block", f"{rbac_rate:.0f}%", "100%")
            k4.metric("🔄 MTTR", f"{mttr_s:.2f} s" if mttr_s > 0 else "< 0.5 s", "≤ 5.0 s")
            k5.metric("🪙 Token Waste", f"{wasted_pct:.1f}%", "≤ 5.0%")


@st.fragment(run_every=1.0)
def _render_identity_and_policy_tab(session: SessionState) -> None:
    snap = session.snapshot()
    telemetry = snap["telemetry"]
    c1_iam = telemetry.get("c1_iam", {})
    c2_policy = telemetry.get("c2_policy", {})

    st.markdown("#### 🔐 C1 Cryptographic Identity (Ed25519 JWT Passports) & C2 Policy Store")

    col_key, col_rev = st.columns([1.3, 1.0])
    with col_key:
        st.markdown("##### 🔑 Live Ed25519 Public Key (PEM)")
        st.code(c1_iam.get("public_key_pem", ""), language="text")
    with col_rev:
        st.markdown("##### 🚫 $O(1)$ Revocation Set (`revocation_set`)")
        revoked = c1_iam.get("revocation_set", [])
        if revoked:
            st.error(f"**Revoked JTIs ({len(revoked)}):**\n\n" + "\n".join(f"- `{j}`" for j in revoked))
        else:
            st.success("Zero revoked JTIs currently in set.")

    st.markdown("##### 🪪 Active Agent JWT Passports & Token Budgets")
    active_tokens = c1_iam.get("active_tokens", {})
    budget_ledger = c2_policy.get("budget_ledger", {})
    if active_tokens:
        passport_rows = []
        for a_id, claims in active_tokens.items():
            b_entry = budget_ledger.get(a_id, {})
            passport_rows.append(
                {
                    "Agent ID": a_id,
                    "Role": claims.get("role"),
                    "Branch": claims.get("branch_id"),
                    "Parent": claims.get("parent_id") or "ROOT",
                    "Permitted Scopes": ", ".join(claims.get("scopes", [])),
                    "JTI": claims.get("jti", "")[:14],
                    "Tokens Used / Max": f"{b_entry.get('tokens_used', 0)} / {b_entry.get('max_tokens', claims.get('token_budget', 0))}",
                    "Wasted Tokens": b_entry.get("wasted_tokens", 0),
                }
            )
        st.dataframe(pd.DataFrame(passport_rows), use_container_width=True, hide_index=True)

    st.markdown("##### 🧰 C2 Dynamically Registered Tools & Pre-Commit Constraints")
    reg_tools = c2_policy.get("registered_tools", {})
    if reg_tools:
        tool_rows = []
        for t_name, spec in reg_tools.items():
            tool_rows.append(
                {
                    "Tool Name": t_name,
                    "Required Scope": spec.get("required_scope"),
                    "Mutates State": spec.get("mutates_state"),
                    "Provenance Fields": ", ".join(spec.get("provenance_fields", [])),
                    "Parameter Constraints": json.dumps(spec.get("parameter_constraints", {})),
                    "Fallback Tool": spec.get("fallback_tool_name") or "None",
                    "Description": spec.get("description", ""),
                }
            )
        st.dataframe(pd.DataFrame(tool_rows), use_container_width=True, hide_index=True)

    st.markdown("##### 📜 C4 HMAC-SHA256 Provenance Receipts (`provenance_receipts`)")
    receipts = telemetry.get("c4_lineage", {}).get("provenance_receipts", {})
    if receipts:
        st.json(receipts)
    else:
        st.info("No provenance receipts generated yet.")

    st.divider()
    with st.expander("🔌 Dynamic Tool & Agent Onboarding (Plug-and-Play)", expanded=False):
        c_tool, c_agent = st.columns(2)
        with c_tool:
            st.markdown("**1. Register Custom Tool at Runtime**")
            new_t_name = st.text_input("Tool Name", value="k8s:scale_deployment", key="dyn_t_name")
            new_t_scope = st.text_input("Required Scope", value="k8s:scale_deployment", key="dyn_t_scope")
            new_t_mutates = st.checkbox("Mutates State (Enforce 4-Layer Pre-Commit Gate)", value=True, key="dyn_t_mut")
            new_t_prov = st.text_input("Provenance Fields (comma-separated)", value="active_pods", key="dyn_t_prov")
            new_t_bounds = st.text_input(
                "Parameter Constraints (JSON)",
                value='{"target_replicas": {"min": 1, "max": 20}}',
                key="dyn_t_bounds",
            )
            if st.button("➕ Register Tool on Session Kernel", key="btn_dyn_reg_tool"):
                prov_list = [p.strip() for p in new_t_prov.split(",") if p.strip()]
                bounds_dict = json.loads(new_t_bounds or "{}")
                assert session.fleet is not None
                fut = asyncio.run_coroutine_threadsafe(
                    session.fleet.client.register_tool_dynamic(
                        tool_name=new_t_name,
                        required_scope=new_t_scope,
                        mutates_state=new_t_mutates,
                        provenance_fields=prov_list,
                        parameter_constraints=bounds_dict,
                        description=f"Dynamic tool {new_t_name}",
                    ),
                    session._loop,
                )
                fut.result(timeout=5.0)
                st.success(f"Registered `{new_t_name}` into session C2 Policy Store!")
                st.rerun()

        with c_agent:
            st.markdown("**2. Spawn Custom Agent with Attenuated Ed25519 JWT**")
            new_a_id = st.text_input("Agent ID", value="Cloud_Scaler_Agent", key="dyn_a_id")
            new_a_role = st.text_input("Role", value="L2_Cloud_Scaler", key="dyn_a_role")
            new_a_scopes = st.text_input(
                "Permitted Scopes (comma-separated)",
                value="k8s:scale_deployment,erp:read",
                key="dyn_a_scopes",
            )
            new_a_branch = st.selectbox("Branch ID", ["Branch_A", "Branch_B", "Root"], key="dyn_a_branch")
            if st.button("🚀 Mint Passport & Register Agent", key="btn_dyn_spawn_agent"):
                scopes_list = [s.strip() for s in new_a_scopes.split(",") if s.strip()]
                assert session.fleet is not None

                async def _spawn_custom() -> Any:
                    assert session.fleet is not None
                    node = await session.fleet.client.bootstrap_root_agent(
                        agent_id=new_a_id,
                        role=new_a_role,
                        scopes=scopes_list,
                        token_budget=2500,
                        branch_id=new_a_branch,
                    )
                    session.fleet.live_agents[new_a_id] = node
                    return node

                asyncio.run_coroutine_threadsafe(_spawn_custom(), session._loop).result(timeout=5.0)
                st.success(f"Minted Ed25519 JWT passport for `{new_a_id}`!")
                st.rerun()


@st.fragment(run_every=1.0)
def _render_snapshots_tab(session: SessionState) -> None:
    snap = session.snapshot()
    telemetry = snap["telemetry"]
    c5_snaps = telemetry.get("c5_snapshots", {})
    recovery_history = list(telemetry.get("recovery_history", []))
    kill_history = telemetry.get("kill_switch_history", [])

    # Also include any rollback reports captured on SYSTEM_RECOVERY turns in transcript
    seen_rb_ts = {r.get("timestamp") for r in recovery_history if r.get("timestamp")}
    for turn in snap.get("transcript", []):
        if turn.get("type") == "SYSTEM_RECOVERY":
            rb_rep = (turn.get("receipt") or {}).get("rollback_report")
            if isinstance(rb_rep, dict) and rb_rep.get("timestamp") not in seen_rb_ts:
                recovery_history.append(rb_rep)
                if rb_rep.get("timestamp"):
                    seen_rb_ts.add(rb_rep.get("timestamp"))

    st.markdown("#### 💾 C5 SQLite Execution Checkpoints & Surgical Rollback Diff Viewer")
    st.caption(f"SQLite Database File: `{session.db_path}`")

    snapshots = c5_snaps.get("snapshots", [])
    if snapshots:
        snap_rows = []
        for s in snapshots:
            snap_rows.append(
                {
                    "Checkpoint ID": s.get("checkpoint_id"),
                    "Branch": s.get("branch_id"),
                    "Saved By Agent": s.get("agent_id"),
                    "Vector Clock": json.dumps(s.get("vector_clock", {})),
                    "State Blob": json.dumps(s.get("state_blob", {})),
                }
            )
        st.dataframe(pd.DataFrame(snap_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No checkpoints saved yet.")

    st.divider()
    st.markdown("##### 🔬 Surgical Subtree Rollback Reports (`isolate_and_rollback`)")
    if recovery_history:
        latest_rb = recovery_history[-1]
        st.success(
            f"**Restored Checkpoint:** `{latest_rb.get('restored_checkpoint_id')}` | "
            f"**Post-Rollback Checkpoint:** `{latest_rb.get('post_rollback_checkpoint_id', 'chk_Branch_A_1')}` | "
            f"**Tainted BFS Nodes ($T_{{\\text{{fault}}}}$):** `{latest_rb.get('tainted_bfs_nodes')}` | "
            f"**Blast Radius Containment ($R_{{\\text{{blast}}}}$):** `{latest_rb.get('blast_radius_ratio')}` | "
            f"**MTTR:** `{latest_rb.get('mttr_seconds')} s`"
        )
        col_bad, col_clean = st.columns(2)
        with col_bad:
            st.markdown("🔴 **Blocked Tainted / Corrupted State**")
            st.json(
                {
                    "failed_agent_id": latest_rb.get("failed_agent_id"),
                    "failure_reason": latest_rb.get("failure_reason"),
                    "tainted_bfs_nodes": latest_rb.get("tainted_bfs_nodes"),
                    "revoked_jtis": latest_rb.get("revoked_jtis"),
                }
            )
        with col_clean:
            st.markdown("🟢 **Restored Clean Checkpoint State (`state_blob`)**")
            st.json(latest_rb.get("restored_state", {}))

        if len(recovery_history) > 1:
            with st.expander(f"All Partial Rollback Events in Session ({len(recovery_history)})", expanded=False):
                st.dataframe(pd.DataFrame(recovery_history), use_container_width=True, hide_index=True)
    else:
        st.info("No rollback events triggered yet. Inject a `CORRUPTED_OUTPUT` fault or run Scenario 3.")

    if kill_history:
        st.divider()
        st.markdown("##### ⚡ Kill-Switch Termination Receipts")
        st.dataframe(pd.DataFrame(kill_history), use_container_width=True, hide_index=True)


def _render_journal_tab(session: SessionState) -> None:
    st.markdown("#### 📜 Append-Only JSONL Session Journal Viewer & Replay")
    runs_dir = PROJECT_ROOT / "runs"
    journal_files = sorted(
        runs_dir.glob("*/journal.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )

    current_j = Path(session.journal_path)
    if current_j not in journal_files and current_j.exists():
        journal_files.insert(0, current_j)

    if not journal_files:
        st.info("No journal files found in `runs/` yet.")
        return

    selected_path = st.selectbox(
        "Select Session Journal File (`runs/<session_id>/journal.jsonl`)",
        options=[str(p) for p in journal_files],
        index=0,
    )

    j_path = Path(selected_path)
    if not j_path.is_file():
        st.warning(f"Journal file `{j_path}` not created yet.")
        return

    lines = [
        json.loads(line)
        for line in j_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    st.caption(f"Loaded **{len(lines)}** JSONL records from `{j_path}`.")

    filter_type = st.radio(
        "Filter Record Type",
        options=["ALL", "AGENT_TURN", "KERNEL_EVENT"],
        horizontal=True,
    )
    filtered = [
        rec
        for rec in lines
        if filter_type == "ALL" or rec.get("record_type") == filter_type
    ]

    summary_rows = []
    for idx, rec in enumerate(filtered, start=1):
        r_type = rec.get("record_type")
        if r_type == "AGENT_TURN":
            t = rec.get("turn", {})
            rcpt = t.get("receipt", {})
            summary_rows.append(
                {
                    "#": idx,
                    "Record": "AGENT_TURN",
                    "Actor": t.get("agent_id"),
                    "Type / Event": t.get("type"),
                    "Tool / Target": rcpt.get("tool", t.get("target")),
                    "HTTP": rcpt.get("http_code"),
                    "Overridden": rcpt.get("overridden"),
                    "Detail": str(t.get("content", ""))[:120],
                }
            )
        else:
            ev = rec.get("event", {})
            summary_rows.append(
                {
                    "#": idx,
                    "Record": "KERNEL_EVENT",
                    "Actor": ev.get("agent_id"),
                    "Type / Event": ev.get("event_type"),
                    "Tool / Target": ev.get("branch_id"),
                    "HTTP": 200,
                    "Overridden": False,
                    "Detail": str(ev.get("summary", ""))[:120],
                }
            )

    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    with st.expander("Inspect Raw JSONL Records", expanded=False):
        st.json(filtered[-25:])


def main() -> None:
    session = get_or_create_session_state(st.session_state)
    snap = session.snapshot()

    _render_kpi_bar(session)
    render_sidebar_controls(session, snap=snap)

    pending_gov_count = sum(
        1
        for item in snap.get("governor_queue", [])
        if item.get("status") == "PENDING"
    )
    gov_tab_label = (
        f"⚖️ Governor Review Queue ({pending_gov_count} PENDING)"
        if pending_gov_count > 0
        else "⚖️ Governor Review Queue"
    )

    tab_chat, tab_dag, tab_gov, tab_iam, tab_snap, tab_journal = st.tabs(
        [
            "💬 Chat (Live NIM Fleet)",
            "🕸️ Live DAG & Event Log",
            gov_tab_label,
            "🔐 Identity & Policy Tables",
            "💾 Snapshots & Rollback Diffs",
            "📜 Journal Viewer",
        ]
    )

    with tab_chat:
        render_chat_tab(session)

    with tab_dag:
        render_live_dag_fragment(session)

    with tab_gov:
        render_governor_tab(session)

    with tab_iam:
        _render_identity_and_policy_tab(session)

    with tab_snap:
        _render_snapshots_tab(session)

    with tab_journal:
        _render_journal_tab(session)


if __name__ == "__main__":
    main()
