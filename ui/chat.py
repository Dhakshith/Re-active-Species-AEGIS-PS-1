"""
Interactive Multi-Agent Chat Panel (`ui/chat.py`).
Provides:
- Novel goal input via `st.chat_input` and quick-prompt buttons (including conversational greeting).
- Live turn-by-turn agent transcript with expandable cryptographic receipts
  (`trace_id`, `tool`, `http_code`, `hmac_prefix`, `overridden` badge).
- Live notification banner when an item is waiting in the Governor Review Queue.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.state import SessionState


SAMPLE_GOALS: list[tuple[str, str]] = [
    (
        "👋 Say Hi",
        "Hi",
    ),
    (
        "💼 Settle $2,000,000 (INV-2026-9981)",
        "Read invoice INV-2026-9981 from ERP, fetch the live USD/EUR quote, route to SWIFT_Wire_Executor, and settle the wire.",
    ),
    (
        "💰 Settle $500,000 (Custom Amount)",
        "Settle $500,000 USD to Siemens Energy AG for invoice INV-2026-9981.",
    ),
    (
        "🚫 Test RBAC 403 Block & Adapt",
        "Have FX_Quote_Reader attempt to call swift:wire directly first; when blocked by C2 RBAC (403), adapt by reading ERP and delegating properly.",
    ),
    (
        "⚖️ Request Human Governor Approval",
        "Read invoice INV-2026-9981 and fx:quote, then call REQUEST_HUMAN on Branch_A for governor approval before SWIFT wire execution.",
    ),
    (
        "🛡️ Full KYC + FX Settlement",
        "Run kyc:check and audit:log for Siemens Energy AG, fetch fx:quote for INV-2026-9981, and complete the settlement.",
    ),
]


def _render_turn_card(turn: dict[str, Any]) -> None:
    """Render a single agent turn and its cryptographic receipt cleanly."""
    t_type = turn.get("type", "TURN")
    agent_id = turn.get("agent_id", "Agent")
    role = turn.get("role", "")
    content = turn.get("content", "")
    receipt = turn.get("receipt") or {}

    avatar = (
        "👤"
        if agent_id == "Human_Operator"
        else ("🛡️" if "Control_Plane" in agent_id else "🤖")
    )
    chat_role = "user" if agent_id == "Human_Operator" else "assistant"

    with st.chat_message(chat_role, avatar=avatar):
        http_code = receipt.get("http_code", 200)
        tool_name = receipt.get("tool", turn.get("target", ""))
        trace_id = receipt.get("trace_id", "n/a")
        hmac_prefix = receipt.get("hmac_prefix", "NONE")
        hmac_display = (
            "NONE (not provenance-tracked)"
            if hmac_prefix == "NONE"
            else hmac_prefix
        )
        overridden = bool(receipt.get("overridden", False))

        if http_code == 200:
            code_badge = f"🟢 HTTP {http_code}"
        elif http_code in (401, 403):
            code_badge = f"🟣 HTTP {http_code} RBAC BLOCK"
        elif http_code == 409:
            code_badge = f"🔴 HTTP {http_code} PRE-COMMIT BLOCK"
        else:
            code_badge = f"🟠 HTTP {http_code}"

        override_badge = " · ⚖️ `overridden=True`" if overridden else ""
        st.markdown(
            f"**`{agent_id}`** *({role})* &nbsp;·&nbsp; `{t_type}` &nbsp;·&nbsp; {code_badge}{override_badge}"
        )
        st.markdown(content)

        if receipt and (
            receipt.get("output")
            or receipt.get("rollback_report")
            or receipt.get("error")
            or receipt.get("operator_override_state")
            or hmac_prefix != "NONE"
        ):
            with st.expander(
                f"🔐 Receipt · `{tool_name or trace_id[:12]}` · HMAC `{hmac_display[:14]}`",
                expanded=False,
            ):
                st.caption(
                    f"**trace_id:** `{trace_id}` &nbsp;|&nbsp; **tool:** `{tool_name}` &nbsp;|&nbsp; "
                    f"**HTTP:** `{http_code}` &nbsp;|&nbsp; **HMAC:** `{hmac_display}` &nbsp;|&nbsp; **overridden:** `{overridden}`"
                )
                if (
                    receipt.get("output")
                    or receipt.get("rollback_report")
                    or receipt.get("error")
                    or receipt.get("operator_override_state")
                ):
                    st.json(receipt)


@st.fragment(run_every=1.0)
def _render_live_transcript_fragment(session: SessionState) -> None:
    snap = session.snapshot()
    transcript = snap["transcript"]
    step_snapshots = snap.get("step_snapshots", [])
    is_running = snap["is_running"]
    last_error = snap["last_error"]
    step_by_step_mode = bool(snap.get("step_by_step_mode", False))
    waiting_for_step = bool(snap.get("waiting_for_step", False))
    pending_gov = [
        item
        for item in snap.get("governor_queue", [])
        if item.get("status") == "PENDING"
    ]

    total_steps = len(transcript)
    if total_steps > 0:
        raw_sel = st.session_state.get("aegis_selected_step")
        if raw_sel is None or int(raw_sel) >= total_steps:
            active_idx = total_steps - 1
            is_live_view = True
        else:
            active_idx = max(0, int(raw_sel))
            is_live_view = False

        # Unified Time-Travel & Stepper Toolbar (Clean & Compact)
        with st.container(border=True):
            nav1, nav2, nav3, nav4, nav5, nav6 = st.columns([0.7, 1.0, 2.2, 1.0, 0.8, 1.3], vertical_alignment="center")
            with nav1:
                if st.button("⏮️", key="btn_chat_step_first", disabled=(active_idx <= 0), use_container_width=True, help="Jump to Step 1"):
                    st.session_state["aegis_selected_step"] = 0
                    st.rerun()
            with nav2:
                if st.button("⏪ Back", key="btn_chat_step_prev", disabled=(active_idx <= 0), use_container_width=True):
                    st.session_state["aegis_selected_step"] = max(0, active_idx - 1)
                    st.rerun()
            with nav3:
                cur_turn = transcript[active_idx]
                cur_label = f"{cur_turn.get('agent_id')} · {cur_turn.get('type')}"
                if is_live_view:
                    st.markdown(
                        f"<div style='text-align:center;'>🔴 <b>LIVE</b> · Step {total_steps}/{total_steps} (<code>{cur_label}</code>)</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='text-align:center;color:#fbbf24;'>⏳ <b>Step {active_idx + 1}</b>/{total_steps} (<code>{cur_label}</code>)</div>",
                        unsafe_allow_html=True,
                    )
            with nav4:
                can_advance_history = active_idx < total_steps - 1
                if st.button(
                    "Next ⏩",
                    key="btn_chat_step_next",
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
            with nav5:
                if st.button("⏭️ Live", key="btn_chat_step_latest", disabled=is_live_view, use_container_width=True):
                    st.session_state["aegis_selected_step"] = None
                    st.rerun()
            with nav6:
                new_step_mode = st.checkbox(
                    "Pause per turn",
                    value=step_by_step_mode,
                    key="chk_step_by_step_live",
                    help="Pause live execution before each turn.",
                )
                if new_step_mode != step_by_step_mode:
                    session.set_step_by_step_mode(new_step_mode)
                    st.rerun()

    if waiting_for_step:
        c_w1, c_w2 = st.columns([2.0, 1.0], vertical_alignment="center")
        with c_w1:
            st.warning("⏸️ **Paused Before Next Live Turn**")
        with c_w2:
            if st.button("⏩ Execute Next Turn", key="btn_chat_exec_turn_p", type="primary", use_container_width=True):
                st.session_state["aegis_selected_step"] = None
                session.step_forward_live()
                st.rerun()

    if pending_gov:
        st.warning(
            f"⚖️ **Action Required:** Branch `{pending_gov[0]['branch_id']}` paused (`SOFT_FREEZE`) on review `{pending_gov[0]['item_id']}`. "
            f"Switch to the **'⚖️ Governor Review Queue'** tab to approve with override or reject."
        )
    elif is_running and not waiting_for_step:
        st.info("⚡ **Live NIM Fleet Active** — Agents are reasoning and executing...")

    if last_error:
        st.error(f"**NIM / Agent Error (Reflected in DAG):** `{last_error}`")

    if not transcript:
        st.markdown(
            "> 💡 **No chat turns yet.** Type a treasury goal below or select an example above to start."
        )
    else:
        raw_sel = st.session_state.get("aegis_selected_step")
        if raw_sel is not None and 0 <= int(raw_sel) < len(transcript):
            vis_idx = int(raw_sel)
            visible_turns = transcript[: vis_idx + 1]
            st.caption(
                f"⏳ **Inspecting Step {vis_idx + 1} of {len(transcript)}** · Showing turns 1..{vis_idx + 1}. Click 'Live' to return to latest."
            )
        else:
            visible_turns = transcript

        for turn in visible_turns:
            _render_turn_card(turn)


def render_chat_tab(session: SessionState) -> None:
    """Render the interactive Chat tab with quick-goal triggers, live transcript fragment, and chat input."""
    # Top quick action chips
    c_p1, c_p2, c_p3, c_p4 = st.columns(4)
    with c_p1:
        if st.button("👋 Say Hi", key="btn_quick_hi", use_container_width=True):
            st.session_state["aegis_selected_step"] = None
            session.submit_goal("Hi")
            st.rerun()
    with c_p2:
        if st.button("💼 Settle $2,000,000", key="btn_quick_settle_2m", use_container_width=True):
            st.session_state["aegis_selected_step"] = None
            session.submit_goal("Read invoice INV-2026-9981 from ERP, fetch the live USD/EUR quote, route to SWIFT_Wire_Executor, and settle the wire.")
            st.rerun()
    with c_p3:
        if st.button("💰 Settle $500,000", key="btn_quick_settle_500k", use_container_width=True):
            st.session_state["aegis_selected_step"] = None
            session.submit_goal("Settle $500,000 USD to Siemens Energy AG for invoice INV-2026-9981.")
            st.rerun()
    with c_p4:
        if st.button("🚫 Test RBAC 403", key="btn_quick_rbac_403", use_container_width=True):
            st.session_state["aegis_selected_step"] = None
            session.submit_goal("Have FX_Quote_Reader attempt to call swift:wire directly first; when blocked by C2 RBAC (403), adapt by reading ERP and delegating properly.")
            st.rerun()

    with st.expander("⚡ More Example Treasury Prompts", expanded=False):
        for idx, (label, prompt_text) in enumerate(SAMPLE_GOALS[4:], start=4):
            if st.button(label, key=f"sample_goal_more_{idx}", use_container_width=True):
                st.session_state["aegis_selected_step"] = None
                session.submit_goal(prompt_text)
                st.rerun()

    st.divider()
    _render_live_transcript_fragment(session)

    user_goal = st.chat_input(
        "Enter treasury goal (e.g., 'Settle $500,000 for invoice INV-2026-9981' or 'use fx_quote_reader to fetch quote')..."
    )
    if user_goal:
        st.session_state["aegis_selected_step"] = None
        session.submit_goal(user_goal)
        st.rerun()
