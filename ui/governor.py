"""
Human-in-the-Loop Governor Review Queue & Soft-Freeze Override Panel (`ui/governor.py`).
Uses `@st.fragment(run_every=1.0)` so queued reviews appear live without page reloads.
Allows human operators to:
1. Toggle auto-interception of every `swift:wire` for Human Governor approval.
2. Pause a branch via `SOFT_FREEZE` and immediately enqueue a review card in `governor_queue`.
3. Compare the agent's proposed settlement payload against the clean C5 SQLite checkpoint.
4. Edit parameters (e.g., `settlement_eur` / `fx_rate`) and resume execution with
   `operator_override_state`, setting `overridden=True` in C4 provenance receipts.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ui.state import SessionState


@st.fragment(run_every=1.0)
def _render_governor_queue_fragment(session: SessionState) -> None:
    snap = session.snapshot()
    telemetry = snap["telemetry"]
    gov_queue = snap["governor_queue"]
    freeze_states = telemetry.get("c3_tasks", {}).get("branch_freeze_states", {})
    branch_overrides = telemetry.get("c3_tasks", {}).get("branch_overrides", {})

    pending_items = [item for item in gov_queue if item.get("status") == "PENDING"]
    resolved_items = [item for item in gov_queue if item.get("status") != "PENDING"]

    # 1. Direct Branch Soft-Freeze & Queue Review Controls
    with st.expander("⏸️ Branch Soft-Freeze & Governor Gate Controls", expanded=True):
        auto_gov = st.checkbox(
            "🔒 Auto-Queue Every `swift:wire` Call for Human Governor Approval",
            value=snap.get("require_governor_on_wire", False),
            key="chk_auto_gov_wire",
            help="When enabled, any agent attempting to execute `swift:wire` automatically pauses Branch_A and enqueues a review item below.",
        )
        if auto_gov != snap.get("require_governor_on_wire", False):
            session.set_require_governor_on_wire(auto_gov)

        c1, c2, c3 = st.columns([1.1, 1.2, 1.2])
        with c1:
            target_branch = st.selectbox(
                "Target Branch",
                ["Branch_A", "Branch_B"],
                key="gov_manual_branch",
            )
            is_running_branch = freeze_states.get(target_branch, True)
            st.markdown(
                f"**Branch Status:** {'🟢 RUNNING' if is_running_branch else '🟠 PAUSED (SOFT_FREEZE)'}"
            )
            if st.button(
                "⏸️ Pause Branch & Queue Review",
                key="btn_gov_pause",
                use_container_width=True,
            ):
                item = session.pause_and_queue_governor_review(branch_id=target_branch)
                st.toast(
                    f"Paused {target_branch} & queued review {item['item_id']}",
                    icon="⏸️",
                )
                st.rerun()

        with c2:
            manual_eur = st.number_input(
                "Direct Override Settlement EUR",
                min_value=1.0,
                max_value=2000000.0,
                value=1839000.0,
                step=1000.0,
                key="gov_manual_eur",
            )
            manual_fx = round(float(manual_eur) / 2000000.0, 6)
            st.caption(f"Implied Conservation FX Rate (for $2M USD): `{manual_fx:.6f}`")

        with c3:
            st.write("")
            st.write("")
            if st.button(
                "▶️ Apply Override & Resume Branch",
                key="btn_gov_manual_resume",
                type="primary",
                use_container_width=True,
            ):
                override_payload = {
                    "fx_rate": manual_fx,
                    "settlement_eur": float(manual_eur),
                }
                if pending_items:
                    for p_item in pending_items:
                        if p_item["branch_id"] == target_branch:
                            session.resolve_governor_item(
                                item_id=p_item["item_id"],
                                operator_override_state=override_payload,
                                approved=True,
                            )
                else:
                    session.trigger_kill_switch(
                        mode="SOFT_FREEZE",
                        target_id=target_branch,
                        operator_override_state=override_payload,
                    )
                st.toast(
                    f"Resumed {target_branch} with override {override_payload}",
                    icon="✅",
                )
                st.rerun()

        if branch_overrides:
            st.info(f"**Active Branch Overrides:** `{json.dumps(branch_overrides)}`")

    st.divider()

    # 2. Queued REQUEST_HUMAN / Pre-Wire Review Items
    st.markdown(f"##### 📋 Pending Governor Review Items ({len(pending_items)})")
    if not pending_items:
        st.info(
            "No pending review items in queue. Click **'⚖️ Request Human Governor Approval'** in Chat, "
            "enable **'🔒 Auto-Queue Every `swift:wire`'** above, or click **'⏸️ Pause Branch & Queue Review'**."
        )
    else:
        for item in pending_items:
            item_id = item["item_id"]
            branch_id = item["branch_id"]
            proposed = item.get("proposed_payload") or {}
            chk = (item.get("checkpoint_snapshot") or {}).get("state_blob", {})

            with st.container(border=True):
                st.markdown(
                    f"### ⚖️ Review Item `{item_id}` — Agent: `{item['agent_id']}` · "
                    f"Branch: `{branch_id}` · Trace: `{item['trace_id']}`"
                )
                item_msg = item.get("message", "")
                if any(
                    kw in item_msg.lower()
                    for kw in (
                        "fault detected",
                        "rbac",
                        "violation",
                        "blocked",
                        "outage",
                        "corrupt",
                        "human verification required",
                    )
                ):
                    st.error(f"🚨 **Action Required — Detected Fault Verification:** {item_msg}")
                else:
                    st.markdown(f"*{item_msg}*")

                col_prop, col_chk = st.columns(2)
                with col_prop:
                    st.markdown("**Agent Proposed Payload**")
                    st.json(proposed)
                with col_chk:
                    st.markdown("**C5 Latest Clean Checkpoint (`state_blob`)**")
                    st.json(chk)

                default_usd = float(
                    proposed.get("amount_usd", chk.get("amount_usd", 2000000.0))
                )
                default_eur = float(
                    proposed.get("settlement_eur", chk.get("settlement_eur", 1840000.0))
                )

                e1, e2, e3 = st.columns([1.2, 1.2, 1.0])
                with e1:
                    edited_eur = st.number_input(
                        f"Approved Settlement EUR ({item_id})",
                        min_value=1.0,
                        max_value=2000000.0,
                        value=default_eur,
                        step=500.0,
                        key=f"edit_eur_{item_id}",
                    )
                with e2:
                    implied_fx = (
                        round(edited_eur / default_usd, 6)
                        if default_usd > 0
                        else 0.9200
                    )
                    edited_fx = st.number_input(
                        f"Approved FX Rate ({item_id})",
                        min_value=0.5000,
                        max_value=1.5000,
                        value=float(implied_fx),
                        step=0.0005,
                        format="%.6f",
                        key=f"edit_fx_{item_id}",
                    )
                with e3:
                    st.write("")
                    b_col1, b_col2 = st.columns(2)
                    with b_col1:
                        if st.button(
                            "✅ Approve & Resume with Override",
                            key=f"approve_btn_{item_id}",
                            type="primary",
                            use_container_width=True,
                        ):
                            final_eur = (
                                round(default_usd * float(edited_fx), 2)
                                if abs(edited_eur - default_eur) < 1e-4
                                else float(edited_eur)
                            )
                            override_state = {
                                "amount_usd": default_usd,
                                "fx_rate": round(final_eur / default_usd, 6),
                                "settlement_eur": final_eur,
                            }
                            session.resolve_governor_item(
                                item_id=item_id,
                                operator_override_state=override_state,
                                approved=True,
                            )
                            st.toast(
                                f"Approved & resumed {item_id} (`overridden=True`)",
                                icon="✅",
                            )
                            st.rerun()
                    with b_col2:
                        if st.button(
                            "⛔ Reject & Abort",
                            key=f"reject_btn_{item_id}",
                            use_container_width=True,
                        ):
                            session.resolve_governor_item(
                                item_id=item_id,
                                operator_override_state=None,
                                approved=False,
                            )
                            st.toast(
                                f"Rejected & aborted {item_id} (`status=REJECTED`)",
                                icon="⛔",
                            )
                            st.rerun()

    if resolved_items:
        with st.expander(
            f"🗂️ Resolved Governor Items ({len(resolved_items)})", expanded=True
        ):
            for item in reversed(resolved_items):
                st.json(
                    {
                        "item_id": item["item_id"],
                        "branch_id": item["branch_id"],
                        "agent_id": item.get("agent_id"),
                        "status": item["status"],
                        "operator_override_state": item.get("operator_override_state"),
                    }
                )


def render_governor_tab(session: SessionState) -> None:
    """Render the Governor Review Queue and manual Soft-Freeze Override controls."""
    st.markdown("#### ⚖️ Human Governor Review Queue (`SOFT_FREEZE` & State Override)")
    st.caption(
        "When an agent invokes `REQUEST_HUMAN` (or attempts `swift:wire` while Governor Review is active), "
        "execution pauses on an `asyncio.Event`. Review the proposed payload vs. the C5 checkpoint, "
        "edit settlement parameters if needed, and resume to stamp `overridden=True` into the HMAC receipt."
    )
    _render_governor_queue_fragment(session)
