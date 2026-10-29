"""L0 -> L2 Hierarchical Supervisor-Worker Treasury Workflow & Scenarios 1–6 (Including Simultaneous Multi-Agent & Multi-Tool Chaos Storm)."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional
from uuid import uuid4

import httpx

from control_plane.methods import ControlPlaneKernel, kernel_singleton
from control_plane.models import TOOL_PROVIDER_CHAINS


AGENT_METADATA: dict[str, dict[str, Any]] = {
    "FX_Settlement_Supervisor": {
        "parent": "Chief_Treasury_Orchestrator",
        "role": "L1_Branch_A_Supervisor",
        "scopes": ["erp:read", "fx:quote", "swift:wire"],
        "budget": 4000,
        "branch": "Branch_A",
    },
    "FX_Quote_Reader": {
        "parent": "FX_Settlement_Supervisor",
        "role": "L2_FX_Quote_Worker",
        "scopes": ["erp:read", "fx:quote"],
        "budget": 1500,
        "branch": "Branch_A",
    },
    "SWIFT_Wire_Executor": {
        "parent": "FX_Settlement_Supervisor",
        "role": "L2_SWIFT_Wire_Worker",
        "scopes": ["swift:wire"],
        "budget": 1500,
        "branch": "Branch_A",
    },
    "Compliance_Supervisor": {
        "parent": "Chief_Treasury_Orchestrator",
        "role": "L1_Branch_B_Supervisor",
        "scopes": ["kyc:check", "audit:log"],
        "budget": 4000,
        "branch": "Branch_B",
    },
    "Sanctions_Screener": {
        "parent": "Compliance_Supervisor",
        "role": "L2_KYC_Worker",
        "scopes": ["kyc:check"],
        "budget": 1500,
        "branch": "Branch_B",
    },
    "Audit_Logger": {
        "parent": "Compliance_Supervisor",
        "role": "L2_Audit_Worker",
        "scopes": ["audit:log"],
        "budget": 1500,
        "branch": "Branch_B",
    },
}


class TreasuryWorkflowRunner:
    """
    Multi-Agent Corporate Treasury Orchestrator sharing the ControlPlaneKernel's
    in-process asyncio event loop for deterministic sub-50ms task cancellation
    and simultaneous multi-agent & multi-tool failure recovery.
    """

    def __init__(self, kernel: Optional[ControlPlaneKernel] = None) -> None:
        self.kernel: ControlPlaneKernel = kernel or kernel_singleton
        self.llm_api_key: Optional[str] = os.environ.get(
            "NVIDIA_API_KEY"
        ) or os.environ.get("OPENAI_API_KEY")
        self.execution_mode: str = (
            "LLM_ASSISTED" if self.llm_api_key else "DETERMINISTIC_STATE_MACHINE"
        )

    def _bootstrap_and_delegate_hierarchy(self) -> dict[str, str]:
        """
        Bootstrap L0 Chief_Treasury_Orchestrator and mint attenuated tokens for
        Branch A (FX_Settlement_Supervisor -> FX_Quote_Reader, SWIFT_Wire_Executor)
        and Branch B (Compliance_Supervisor -> Sanctions_Screener, Audit_Logger).
        """
        root_jwt = self.kernel.bootstrap_root_token(
            root_agent_id="Chief_Treasury_Orchestrator",
            token_budget=25000,
        )
        tokens: dict[str, str] = {"Chief_Treasury_Orchestrator": root_jwt}

        for agent_id in (
            "FX_Settlement_Supervisor",
            "Compliance_Supervisor",
            "FX_Quote_Reader",
            "SWIFT_Wire_Executor",
            "Sanctions_Screener",
            "Audit_Logger",
        ):
            meta = AGENT_METADATA[agent_id]
            parent_jwt = tokens[meta["parent"]]
            tokens[agent_id] = self.kernel.mint_delegated_token(
                parent_jwt=parent_jwt,
                child_agent_id=agent_id,
                requested_role=meta["role"],
                requested_scopes=meta["scopes"],
                token_budget=meta["budget"],
                branch_id=meta["branch"],
            )

        return tokens

    def _respawn_agent_with_ancestors(
        self, agent_id: str, tokens: dict[str, str], reason: str
    ) -> str:
        """
        Surgically respawn a failed/killed agent by verifying its parent's JWT
        (and recursively respawning the parent supervisor if also killed/revoked)
        and minting a fresh attenuated Ed25519 token.
        """
        meta = AGENT_METADATA[agent_id]
        parent_id = meta["parent"]
        parent_jwt = tokens[parent_id]

        # Ensure parent token is still valid; if parent was also killed, respawn parent first
        try:
            self.kernel.C1.verify_jwt(parent_jwt)
        except PermissionError:
            if parent_id == "Chief_Treasury_Orchestrator":
                parent_jwt = self.kernel.bootstrap_root_token(
                    root_agent_id="Chief_Treasury_Orchestrator",
                    token_budget=25000,
                )
                tokens["Chief_Treasury_Orchestrator"] = parent_jwt
            else:
                parent_jwt = self._respawn_agent_with_ancestors(
                    parent_id, tokens, reason=f"PARENT_RECOVERY_FOR_{agent_id}"
                )

        old_jti = self.kernel.C3.process_table.get(agent_id, {}).get("jti")
        new_jwt = self.kernel.mint_delegated_token(
            parent_jwt=parent_jwt,
            child_agent_id=agent_id,
            requested_role=f"{meta['role']}_Respawned",
            requested_scopes=meta["scopes"],
            token_budget=meta["budget"],
            branch_id=meta["branch"],
        )
        tokens[agent_id] = new_jwt
        new_jti = self.kernel.C3.process_table.get(agent_id, {}).get("jti")

        self.kernel.agent_respawn_events.append(
            {
                "agent_id": agent_id,
                "branch_id": meta["branch"],
                "old_revoked_jti": old_jti,
                "new_active_jti": new_jti,
                "reason": reason,
                "timestamp": time.time(),
            }
        )
        return new_jwt

    async def _call_tool_resilient(
        self,
        agent_id: str,
        tokens: dict[str, str],
        tool_name: str,
        tool_args: dict[str, Any],
        trace_prefix: str,
        events: list[dict[str, Any]],
        step_label: str,
        exact_trace_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        """
        Universal Resilient Tool Executor:
          - Automatically recovers from agent faults (HTTP 403 Confused Deputy, HTTP 429 Runaway Loop,
            or revoked JTI) by respawning the agent with a fresh cryptographic identity.
          - Automatically fails over across multi-tier tool providers (HTTP 503 Service Unavailable,
            HTTP 504 Network Timeout) for any tool.
          - Returns (final_response_dict, final_trace_id) or passes through HTTP 409 (Gate L4 Pre-Commit Block).
        """
        provider_chain = list(
            TOOL_PROVIDER_CHAINS.get(tool_name, ["primary", "reuters_fallback"])
        )
        provider_key = "wire_network" if tool_name == "swift:wire" else "provider"
        initial_provider = str(tool_args.get(provider_key, provider_chain[0]))
        if initial_provider in provider_chain:
            start_idx = provider_chain.index(initial_provider)
            candidates = provider_chain[start_idx:]
        else:
            candidates = [initial_provider] + provider_chain

        current_args = dict(tool_args)
        current_args.setdefault(provider_key, candidates[0])
        provider_idx = 0
        max_attempts = 8
        t_start = time.perf_counter()

        for attempt in range(max_attempts):
            if exact_trace_id is not None:
                trace_id = exact_trace_id
            else:
                trace_id = (
                    f"{trace_prefix}_{uuid4().hex[:6]}"
                    if attempt > 0
                    else f"{trace_prefix}_{uuid4().hex[:8]}"
                )
            try:
                resp = await self.kernel.intercept_tool_call(
                    agent_jwt=tokens[agent_id],
                    tool_name=tool_name,
                    tool_args=current_args,
                    trace_id=trace_id,
                )
            except PermissionError as exc:
                # Token was revoked by a concurrent fault/kill; respawn agent and retry
                self._respawn_agent_with_ancestors(
                    agent_id, tokens, reason=f"TOKEN_REVOKED ({exc})"
                )
                continue

            ev_name = step_label if attempt == 0 else f"{step_label}_retry_{attempt}"
            events.append({"step": ev_name, "agent_id": agent_id, "response": resp})

            http_code = resp.get("http_code", 200)
            if http_code == 200 or http_code == 409:
                if attempt > 0 and http_code == 200:
                    self.kernel.last_mttr_seconds = round(
                        time.perf_counter() - t_start, 4
                    )
                return resp, trace_id

            if http_code in (403, 429):
                # Agent-level failure (Confused Deputy 403 or Runaway Loop 429) -> respawn clean worker
                old_jti = self.kernel.C1.agent_current_jti.get(agent_id)
                self._respawn_agent_with_ancestors(
                    agent_id,
                    tokens,
                    reason=f"HTTP_{http_code}_{resp.get('status')}",
                )
                events[-1]["old_revoked_jti"] = old_jti
                continue

            if http_code in (503, 504):
                # Tool-level outage or network timeout -> advance to next fallback provider in chain
                provider_idx = min(provider_idx + 1, len(candidates) - 1)
                next_provider = resp.get(
                    "recommended_fallback", candidates[provider_idx]
                )
                current_args[provider_key] = next_provider
                continue

        return resp, trace_id

    async def _route_a2a_resilient(
        self,
        sender_id: str,
        receiver_id: str,
        tokens: dict[str, str],
        payload: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        """
        Route A2A message, automatically handling supervisor/worker faults or
        revoked tokens on either sender_id or receiver_id.
        """
        # Check if sender or receiver has an active PRIVILEGE_ESCALATION or RUNAWAY_LOOP fault rule
        for participant_id in (sender_id, receiver_id):
            for fault_cat in ("PRIVILEGE_ESCALATION", "RUNAWAY_LOOP"):
                rule = self.kernel.C6.get_active_rule(participant_id, fault_cat)
                if rule is not None:
                    self.kernel.C6.consume_rule(rule)
                    if fault_cat == "PRIVILEGE_ESCALATION":
                        self.kernel.C2.rbac_attempts_unauthorized += 1
                        self.kernel.C2.rbac_blocks_enforced += 1
                    await self.kernel.trigger_kill_switch(
                        mode="SURGICAL", target_id=participant_id
                    )
                    self._respawn_agent_with_ancestors(
                        participant_id,
                        tokens,
                        reason=f"SUPERVISOR_A2A_{fault_cat}_RECOVERED",
                    )

        # Ensure receiver token is active
        try:
            self.kernel.C1.verify_jwt(tokens[receiver_id])
        except PermissionError:
            self._respawn_agent_with_ancestors(
                receiver_id, tokens, reason="A2A_RECEIVER_TOKEN_REVOKED"
            )

        try:
            return await self.kernel.route_a2a_message(
                sender_jwt=tokens[sender_id],
                receiver_id=receiver_id,
                payload=payload,
                trace_id=trace_id,
            )
        except PermissionError:
            self._respawn_agent_with_ancestors(
                sender_id, tokens, reason="A2A_SENDER_TOKEN_REVOKED"
            )
            return await self.kernel.route_a2a_message(
                sender_jwt=tokens[sender_id],
                receiver_id=receiver_id,
                payload=payload,
                trace_id=trace_id,
            )

    async def _run_branch_a(
        self,
        tokens: dict[str, str],
        simulate_latency_sec: float = 0.002,
    ) -> dict[str, Any]:
        """
        Execute Subtree Branch A (Liquidity & Settlement Bulkhead):
          FX_Settlement_Supervisor -> FX_Quote_Reader -> SWIFT_Wire_Executor
        Resilient to simultaneous multi-agent (403/429/taint) and multi-tool (503/504) failures.
        """
        self.kernel.C3.process_table["FX_Settlement_Supervisor"]["status"] = "RUNNING"
        self.kernel.C3.process_table["FX_Quote_Reader"]["status"] = "RUNNING"
        self.kernel.C3.process_table["SWIFT_Wire_Executor"]["status"] = "READY"

        # Save initial clean checkpoint chk_Branch_A_1
        clean_branch_a_state = {
            "branch_id": "Branch_A",
            "invoice_id": "INV-2026-9981",
            "beneficiary": "SIEMENS_ENERGY_DE",
            "iban": "DE89370400440532013000",
            "amount_usd": 2_000_000.0,
            "provider": "bloomberg_primary",
            "stage": "PRE_FX_QUOTE",
            "message_history": [],
        }
        chk_a_id = self.kernel.create_checkpoint(
            agent_jwt=tokens["FX_Settlement_Supervisor"],
            branch_id="Branch_A",
            current_graph_state=clean_branch_a_state,
        )

        events: list[dict[str, Any]] = []

        # Step 1: FX_Quote_Reader reads ERP invoice (resilient to 403/429/503/504)
        erp_resp, _ = await self._call_tool_resilient(
            agent_id="FX_Quote_Reader",
            tokens=tokens,
            tool_name="erp:read",
            tool_args={
                "invoice_id": "INV-2026-9981",
                "provider": "sap_s4_primary",
                "simulate_latency_sec": simulate_latency_sec,
            },
            trace_prefix="tr_erp",
            events=events,
            step_label="erp_read_attempt",
        )
        invoice_data = erp_resp["data"]
        amount_usd = float(invoice_data["amount_usd"])

        # Step 2: FX_Quote_Reader fetches FX Quote (resilient to 403/429/503/504)
        fx_resp, trace_fx = await self._call_tool_resilient(
            agent_id="FX_Quote_Reader",
            tokens=tokens,
            tool_name="fx:quote",
            tool_args={
                "provider": "bloomberg_primary",
                "amount_usd": amount_usd,
                "simulate_latency_sec": simulate_latency_sec,
            },
            trace_prefix="tr_fx",
            events=events,
            step_label="fx_quote_primary",
        )
        quote_data = fx_resp["data"]

        # Step 3: Route A2A message FX_Quote_Reader -> FX_Settlement_Supervisor -> SWIFT_Wire_Executor
        a2a_payload_1 = {
            "invoice_id": invoice_data["invoice_id"],
            "beneficiary": invoice_data["beneficiary"],
            "iban": invoice_data["iban"],
            "amount_usd": amount_usd,
            "fx_rate": quote_data["fx_rate"],
            "settlement_eur": quote_data["settlement_eur"],
            "provider": quote_data["provider"],
            "step": "QUOTE_TO_SUPERVISOR",
        }
        route_1 = await self._route_a2a_resilient(
            sender_id="FX_Quote_Reader",
            receiver_id="FX_Settlement_Supervisor",
            tokens=tokens,
            payload=a2a_payload_1,
            trace_id=trace_fx,
        )
        events.append({"step": "a2a_w1_to_supA", "route": route_1})

        forwarded_payload = dict(route_1["payload"])
        forwarded_payload["step"] = "SUPERVISOR_TO_EXECUTOR"
        route_2 = await self._route_a2a_resilient(
            sender_id="FX_Settlement_Supervisor",
            receiver_id="SWIFT_Wire_Executor",
            tokens=tokens,
            payload=forwarded_payload,
            trace_id=trace_fx,
        )
        events.append({"step": "a2a_supA_to_w2", "route": route_2})

        # Step 4: SWIFT_Wire_Executor calls swift:wire (Gate L4 Pre-Commit Check + 403/429/503/504 resilience)
        exec_payload = route_2["payload"]
        wire_resp, _ = await self._call_tool_resilient(
            agent_id="SWIFT_Wire_Executor",
            tokens=tokens,
            tool_name="swift:wire",
            tool_args={
                "invoice_id": exec_payload["invoice_id"],
                "beneficiary": exec_payload["beneficiary"],
                "iban": exec_payload["iban"],
                "amount_usd": exec_payload["amount_usd"],
                "fx_rate": exec_payload["fx_rate"],
                "settlement_eur": exec_payload["settlement_eur"],
                "provider": exec_payload["provider"],
                "simulate_latency_sec": simulate_latency_sec,
            },
            trace_prefix=trace_fx,
            events=events,
            step_label="swift_wire_attempt",
            exact_trace_id=trace_fx,
        )
        # Ensure exact trace_id provenance binding when not retried
        if wire_resp["http_code"] == 409:
            t_rollback_retry_start = time.perf_counter()
            recovery_report = wire_resp["recovery_report"]
            restored_state = recovery_report["restored_state"]
            fallback_provider = recovery_report.get(
                "fallback_provider", "reuters_fallback"
            )

            # Re-mint fresh cryptographic identities for the restored Branch A subtree
            for a_id in (
                "FX_Settlement_Supervisor",
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
            ):
                self._respawn_agent_with_ancestors(
                    a_id, tokens, reason="SUBTREE_ROLLBACK_RESTORE"
                )

            fx_retry_resp, trace_retry = await self._call_tool_resilient(
                agent_id="FX_Quote_Reader",
                tokens=tokens,
                tool_name="fx:quote",
                tool_args={
                    "provider": fallback_provider,
                    "amount_usd": restored_state["amount_usd"],
                    "simulate_latency_sec": simulate_latency_sec,
                },
                trace_prefix="tr_fx_rollback_retry",
                events=events,
                step_label="fx_quote_rollback_retry",
            )
            retry_quote = fx_retry_resp["data"]

            r1_retry = await self._route_a2a_resilient(
                sender_id="FX_Quote_Reader",
                receiver_id="FX_Settlement_Supervisor",
                tokens=tokens,
                payload={
                    "invoice_id": restored_state["invoice_id"],
                    "beneficiary": restored_state["beneficiary"],
                    "iban": restored_state["iban"],
                    "amount_usd": restored_state["amount_usd"],
                    "fx_rate": retry_quote["fx_rate"],
                    "settlement_eur": retry_quote["settlement_eur"],
                    "provider": retry_quote["provider"],
                    "step": "ROLLBACK_RETRY_QUOTE_TO_SUPERVISOR",
                },
                trace_id=trace_retry,
            )
            r2_retry = await self._route_a2a_resilient(
                sender_id="FX_Settlement_Supervisor",
                receiver_id="SWIFT_Wire_Executor",
                tokens=tokens,
                payload=r1_retry["payload"],
                trace_id=trace_retry,
            )
            wire_resp, _ = await self._call_tool_resilient(
                agent_id="SWIFT_Wire_Executor",
                tokens=tokens,
                tool_name="swift:wire",
                tool_args={
                    "invoice_id": restored_state["invoice_id"],
                    "beneficiary": restored_state["beneficiary"],
                    "iban": restored_state["iban"],
                    "amount_usd": restored_state["amount_usd"],
                    "fx_rate": r2_retry["payload"]["fx_rate"],
                    "settlement_eur": r2_retry["payload"]["settlement_eur"],
                    "provider": r2_retry["payload"]["provider"],
                    "simulate_latency_sec": simulate_latency_sec,
                },
                trace_prefix=trace_retry,
                events=events,
                step_label="swift_wire_rollback_retry_success",
                exact_trace_id=trace_retry,
            )
            total_mttr = round(
                recovery_report["mttr_seconds"]
                + (time.perf_counter() - t_rollback_retry_start),
                4,
            )
            recovery_report["mttr_seconds"] = total_mttr
            self.kernel.last_mttr_seconds = total_mttr

        for a_id in (
            "FX_Quote_Reader",
            "SWIFT_Wire_Executor",
            "FX_Settlement_Supervisor",
        ):
            if a_id in self.kernel.C3.process_table:
                self.kernel.C3.process_table[a_id]["status"] = "COMPLETED"

        return {
            "branch_id": "Branch_A",
            "checkpoint_id": chk_a_id,
            "final_wire_receipt": wire_resp.get("data", {}),
            "settlement_eur": wire_resp.get("data", {}).get("settlement_eur"),
            "fx_rate": wire_resp.get("data", {}).get("fx_rate"),
            "provider": wire_resp.get("data", {}).get("provider"),
            "events": events,
        }

    async def _run_branch_b(
        self,
        tokens: dict[str, str],
        simulate_latency_sec: float = 0.002,
    ) -> dict[str, Any]:
        """
        Execute Subtree Branch B (Compliance & Audit Bulkhead):
          Compliance_Supervisor -> Sanctions_Screener -> Audit_Logger
        Resilient to simultaneous multi-agent (403/429/taint rollback) and multi-tool (503/504) failures.
        """
        self.kernel.C3.process_table["Compliance_Supervisor"]["status"] = "RUNNING"
        self.kernel.C3.process_table["Sanctions_Screener"]["status"] = "RUNNING"
        self.kernel.C3.process_table["Audit_Logger"]["status"] = "READY"

        clean_branch_b_state = {
            "branch_id": "Branch_B",
            "invoice_id": "INV-2026-9981",
            "beneficiary": "SIEMENS_ENERGY_DE",
            "iban": "DE89370400440532013000",
            "stage": "PRE_SANCTIONS_SCREEN",
        }
        chk_b_id = self.kernel.create_checkpoint(
            agent_jwt=tokens["Compliance_Supervisor"],
            branch_id="Branch_B",
            current_graph_state=clean_branch_b_state,
        )

        events: list[dict[str, Any]] = []
        kyc_resp, trace_kyc = await self._call_tool_resilient(
            agent_id="Sanctions_Screener",
            tokens=tokens,
            tool_name="kyc:check",
            tool_args={
                "beneficiary": "SIEMENS_ENERGY_DE",
                "iban": "DE89370400440532013000",
                "provider": "ofac_eu_primary",
                "simulate_latency_sec": simulate_latency_sec,
            },
            trace_prefix="tr_kyc",
            events=events,
            step_label="kyc_check_attempt",
        )
        kyc_data = kyc_resp["data"]

        route_b1 = await self._route_a2a_resilient(
            sender_id="Sanctions_Screener",
            receiver_id="Compliance_Supervisor",
            tokens=tokens,
            payload={
                "invoice_id": "INV-2026-9981",
                "beneficiary": "SIEMENS_ENERGY_DE",
                "iban": "DE89370400440532013000",
                "kyc_status": kyc_data["kyc_status"],
                "step": "KYC_TO_SUPERVISOR",
            },
            trace_id=trace_kyc,
        )

        route_b2 = await self._route_a2a_resilient(
            sender_id="Compliance_Supervisor",
            receiver_id="Audit_Logger",
            tokens=tokens,
            payload={
                **route_b1["payload"],
                "step": "SUPERVISOR_TO_AUDIT",
            },
            trace_id=trace_kyc,
        )

        audit_resp, _ = await self._call_tool_resilient(
            agent_id="Audit_Logger",
            tokens=tokens,
            tool_name="audit:log",
            tool_args={
                "invoice_id": "INV-2026-9981",
                "beneficiary": "SIEMENS_ENERGY_DE",
                "kyc_status": route_b2["payload"]["kyc_status"],
                "provider": "worm_ledger_primary",
                "simulate_latency_sec": simulate_latency_sec,
            },
            trace_prefix=trace_kyc,
            events=events,
            step_label="audit_log_attempt",
            exact_trace_id=trace_kyc,
        )

        # Handle Branch B Pre-Commit Block & Subtree Rollback (HTTP 409) if Sanctions_Screener was corrupted
        if audit_resp["http_code"] == 409:
            recovery_report = audit_resp["recovery_report"]
            fallback_provider = recovery_report.get(
                "fallback_provider", "worldcheck_fallback"
            )
            for a_id in (
                "Compliance_Supervisor",
                "Sanctions_Screener",
                "Audit_Logger",
            ):
                self._respawn_agent_with_ancestors(
                    a_id, tokens, reason="BRANCH_B_SUBTREE_ROLLBACK_RESTORE"
                )

            kyc_retry_resp, trace_kyc_retry = await self._call_tool_resilient(
                agent_id="Sanctions_Screener",
                tokens=tokens,
                tool_name="kyc:check",
                tool_args={
                    "beneficiary": "SIEMENS_ENERGY_DE",
                    "iban": "DE89370400440532013000",
                    "provider": fallback_provider,
                    "simulate_latency_sec": simulate_latency_sec,
                },
                trace_prefix="tr_kyc_rollback_retry",
                events=events,
                step_label="kyc_check_rollback_retry",
            )
            kyc_data = kyc_retry_resp["data"]

            rb1_retry = await self._route_a2a_resilient(
                sender_id="Sanctions_Screener",
                receiver_id="Compliance_Supervisor",
                tokens=tokens,
                payload={
                    "invoice_id": "INV-2026-9981",
                    "beneficiary": "SIEMENS_ENERGY_DE",
                    "iban": "DE89370400440532013000",
                    "kyc_status": kyc_data["kyc_status"],
                    "step": "ROLLBACK_RETRY_KYC_TO_SUPERVISOR",
                },
                trace_id=trace_kyc_retry,
            )
            rb2_retry = await self._route_a2a_resilient(
                sender_id="Compliance_Supervisor",
                receiver_id="Audit_Logger",
                tokens=tokens,
                payload=rb1_retry["payload"],
                trace_id=trace_kyc_retry,
            )
            audit_resp, _ = await self._call_tool_resilient(
                agent_id="Audit_Logger",
                tokens=tokens,
                tool_name="audit:log",
                tool_args={
                    "invoice_id": "INV-2026-9981",
                    "beneficiary": "SIEMENS_ENERGY_DE",
                    "kyc_status": rb2_retry["payload"]["kyc_status"],
                    "provider": "dr_vault_fallback",
                    "simulate_latency_sec": simulate_latency_sec,
                },
                trace_prefix=trace_kyc_retry,
                events=events,
                step_label="audit_log_rollback_retry_success",
                exact_trace_id=trace_kyc_retry,
            )

        for a_id in ("Sanctions_Screener", "Audit_Logger", "Compliance_Supervisor"):
            if a_id in self.kernel.C3.process_table:
                self.kernel.C3.process_table[a_id]["status"] = "COMPLETED"

        return {
            "branch_id": "Branch_B",
            "checkpoint_id": chk_b_id,
            "kyc_status": kyc_data["kyc_status"],
            "audit_receipt": audit_resp.get("data", {}),
            "events": events,
        }

    # =========================================================================
    # Scenario 1: Normal Execution & Cryptographic Delegation
    # =========================================================================
    async def run_scenario_1(self, reset_kernel: bool = True) -> dict[str, Any]:
        """Execute Scenario 1: Normal Execution & Cryptographic Delegation."""
        if reset_kernel:
            self.kernel.reset()

        tokens = self._bootstrap_and_delegate_hierarchy()
        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"

        return {
            "scenario_id": 1,
            "scenario_name": "Normal Execution & Cryptographic Delegation",
            "execution_mode": self.execution_mode,
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    # =========================================================================
    # Scenario 2: Confused Deputy / RBAC Privilege Escalation Block
    # =========================================================================
    async def run_scenario_2(self, reset_kernel: bool = True) -> dict[str, Any]:
        """Execute Scenario 2: Confused Deputy / RBAC Privilege Escalation Block."""
        if reset_kernel:
            self.kernel.reset()

        tokens = self._bootstrap_and_delegate_hierarchy()
        initial_w1_jti = self.kernel.C3.process_table["FX_Quote_Reader"]["jti"]

        fault_receipt = self.kernel.inject_chaos_fault(
            target="FX_Quote_Reader",
            fault_category="PRIVILEGE_ESCALATION",
        )

        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"

        first_attempt = branch_a_res["events"][0]["response"]
        return {
            "scenario_id": 2,
            "scenario_name": "Confused Deputy / RBAC Privilege Escalation Block",
            "fault_injected": fault_receipt,
            "blocked_http_code": first_attempt["http_code"],
            "initial_fx_reader_jti": initial_w1_jti,
            "initial_jti_revoked": initial_w1_jti in self.kernel.C1.revocation_set,
            "rbac_block_rate": self.kernel.C2.rbac_block_rate,
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    # =========================================================================
    # Scenario 3: Corrupted FX Output, Causal BFS Taint & Surgical Subtree Rollback
    # =========================================================================
    async def run_scenario_3(self, reset_kernel: bool = True) -> dict[str, Any]:
        """Execute Scenario 3: Corrupted FX Output, Causal BFS Taint & Surgical Subtree Rollback."""
        if reset_kernel:
            self.kernel.reset()

        tokens = self._bootstrap_and_delegate_hierarchy()
        fault_receipt = self.kernel.inject_chaos_fault(
            target="FX_Quote_Reader",
            fault_category="CORRUPTED_OUTPUT",
            fault_config={"multiplier": 10.0},
        )

        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"

        recovery_report = self.kernel.recovery_history[-1]
        return {
            "scenario_id": 3,
            "scenario_name": "Corrupted FX Output, Causal BFS Taint & Surgical Subtree Rollback",
            "fault_injected": fault_receipt,
            "recovery_report": recovery_report,
            "tainted_bfs_nodes": recovery_report["tainted_bfs_nodes"],
            "blast_radius_ratio": recovery_report["blast_radius_ratio"],
            "mttr_seconds": recovery_report["mttr_seconds"],
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    # =========================================================================
    # Scenario 4: Infrastructure Outage (HTTP 503) & Automatic Failover
    # =========================================================================
    async def run_scenario_4(self, reset_kernel: bool = True) -> dict[str, Any]:
        """Execute Scenario 4: Infrastructure Outage (HTTP 503) & Automatic Failover."""
        if reset_kernel:
            self.kernel.reset()

        tokens = self._bootstrap_and_delegate_hierarchy()
        fault_receipt = self.kernel.inject_chaos_fault(
            target="fx:quote",
            fault_category="SERVICE_UNAVAILABLE",
        )

        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"

        return {
            "scenario_id": 4,
            "scenario_name": "Infrastructure Outage (HTTP 503) & Automatic Failover",
            "fault_injected": fault_receipt,
            "mttr_seconds": self.kernel.last_mttr_seconds,
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    # =========================================================================
    # Scenario 5: Emergency Hard Kill-Switch (<50ms) & Soft-Freeze Human Override
    # =========================================================================
    async def run_scenario_5(self, reset_kernel: bool = True) -> dict[str, Any]:
        """
        Execute Scenario 5:
          Part A: SOFT_FREEZE on Branch_A, inspect snapshot, and resume with human operator override.
          Part B: GLOBAL_HARD kill-switch across active in-flight multi-agent tasks & sockets (<50ms).
        """
        if reset_kernel:
            self.kernel.reset()

        tokens_a = self._bootstrap_and_delegate_hierarchy()
        self.kernel.create_checkpoint(
            agent_jwt=tokens_a["FX_Settlement_Supervisor"],
            branch_id="Branch_A",
            current_graph_state={
                "branch_id": "Branch_A",
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2_000_000.0,
                "stage": "READY_FOR_OPERATOR_INSPECTION",
            },
        )

        freeze_receipt = await self.kernel.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id="Branch_A",
            operator_override_state=None,
        )

        branch_a_task = asyncio.create_task(self._run_branch_a(tokens_a))
        branch_b_task = asyncio.create_task(self._run_branch_b(tokens_a))
        await asyncio.sleep(0.005)

        override_params = {
            "fx_rate": 0.9195,
            "settlement_eur": 1_839_000.0,
        }
        resume_receipt = await self.kernel.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id="Branch_A",
            operator_override_state=override_params,
        )

        part_a_branch_a, part_a_branch_b = await asyncio.gather(
            branch_a_task, branch_b_task
        )

        # Part B: GLOBAL_HARD Kill-Switch (<50ms Termination Latency)
        self.kernel.reset()
        tokens_b = self._bootstrap_and_delegate_hierarchy()

        async def _in_flight_agent_worker(
            agent_id: str, agent_jwt: str, tool_name: str
        ) -> None:
            client = httpx.AsyncClient()
            proc = self.kernel.C3.process_table[agent_id]
            proc["task_handle"] = asyncio.current_task()
            proc["http_client"] = client
            proc["status"] = "IN_FLIGHT_TOOL"
            try:
                await self.kernel.intercept_tool_call(
                    agent_jwt=agent_jwt,
                    tool_name=tool_name,
                    tool_args={"simulate_latency_sec": 10.0},
                    trace_id=f"tr_inflight_{agent_id}",
                )
            except asyncio.CancelledError:
                raise
            finally:
                if not client.is_closed:
                    await client.aclose()

        in_flight_tasks = [
            asyncio.create_task(
                _in_flight_agent_worker(
                    "Chief_Treasury_Orchestrator",
                    tokens_b["Chief_Treasury_Orchestrator"],
                    "erp:read",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "FX_Settlement_Supervisor",
                    tokens_b["FX_Settlement_Supervisor"],
                    "erp:read",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "FX_Quote_Reader",
                    tokens_b["FX_Quote_Reader"],
                    "fx:quote",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "SWIFT_Wire_Executor",
                    tokens_b["SWIFT_Wire_Executor"],
                    "swift:wire",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "Compliance_Supervisor",
                    tokens_b["Compliance_Supervisor"],
                    "kyc:check",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "Sanctions_Screener",
                    tokens_b["Sanctions_Screener"],
                    "kyc:check",
                )
            ),
            asyncio.create_task(
                _in_flight_agent_worker(
                    "Audit_Logger",
                    tokens_b["Audit_Logger"],
                    "audit:log",
                )
            ),
        ]

        await asyncio.sleep(0.01)

        hard_kill_receipt = await self.kernel.trigger_kill_switch(
            mode="GLOBAL_HARD",
            target_id="ALL",
        )

        await asyncio.gather(*in_flight_tasks, return_exceptions=True)

        return {
            "scenario_id": 5,
            "scenario_name": "Emergency Hard Kill-Switch (<50ms) & Soft-Freeze Human Override",
            "part_a_soft_freeze": {
                "freeze_receipt": freeze_receipt,
                "resume_receipt": resume_receipt,
                "settlement_eur": part_a_branch_a["settlement_eur"],
                "fx_rate": part_a_branch_a["fx_rate"],
                "branch_a": part_a_branch_a,
                "branch_b": part_a_branch_b,
            },
            "part_b_global_hard": {
                "kill_switch_receipt": hard_kill_receipt,
                "termination_latency_ms": hard_kill_receipt["termination_latency_ms"],
                "all_jtis_revoked": all(
                    jti in self.kernel.C1.revocation_set
                    for jti in self.kernel.C1.active_tokens.keys()
                ),
                "all_agents_killed": all(
                    info["status"] == "KILLED"
                    for info in self.kernel.C3.process_table.values()
                ),
            },
            "termination_latency_ms": hard_kill_receipt["termination_latency_ms"],
            "state": self.kernel.get_full_state_and_metrics(),
        }

    # =========================================================================
    # Scenario 6: Simultaneous Multi-Agent & Multi-Tool Chaos Storm
    # =========================================================================
    async def run_scenario_6(self, reset_kernel: bool = True) -> dict[str, Any]:
        """
        Execute Scenario 6: Simultaneous Multi-Agent & Multi-Tool Chaos Storm.
        Simultaneously injects:
          - Agent Fault 1: FX_Quote_Reader -> PRIVILEGE_ESCALATION (HTTP 403 + Surgical Respawn)
          - Agent Fault 2: FX_Quote_Reader -> CORRUPTED_OUTPUT (Gate L2/L3 Taint + Gate L4 Rollback)
          - Agent Fault 3: SWIFT_Wire_Executor -> RUNAWAY_LOOP (HTTP 429 + Surgical Respawn)
          - Agent Fault 4: Sanctions_Screener -> PRIVILEGE_ESCALATION (HTTP 403 + Surgical Respawn)
          - Agent Fault 5: Sanctions_Screener -> CORRUPTED_OUTPUT (Branch B Taint + Gate L4 Rollback)
          - Agent Fault 6: Audit_Logger -> RUNAWAY_LOOP (HTTP 429 + Surgical Respawn)
          - Tool Fault 1: erp:read -> SERVICE_UNAVAILABLE (HTTP 503 -> oracle_erp_fallback)
          - Tool Fault 2: fx:quote -> SERVICE_UNAVAILABLE (HTTP 503 -> reuters_fallback)
          - Tool Fault 3: kyc:check -> NETWORK_OUTAGE (HTTP 504 -> worldcheck_fallback)
          - Tool Fault 4: audit:log -> SERVICE_UNAVAILABLE (HTTP 503 -> dr_vault_fallback)
        Verifies both branches survive concurrent multi-agent & multi-tool failures cleanly!
        """
        if reset_kernel:
            self.kernel.reset()

        t_storm_start = time.perf_counter()
        tokens = self._bootstrap_and_delegate_hierarchy()

        simultaneous_faults = [
            # Multi-Agent Faults across Branch A & Branch B
            {"target": "FX_Quote_Reader", "fault_category": "PRIVILEGE_ESCALATION"},
            {
                "target": "FX_Quote_Reader",
                "fault_category": "CORRUPTED_OUTPUT",
                "fault_config": {"multiplier": 10.0},
            },
            {"target": "SWIFT_Wire_Executor", "fault_category": "RUNAWAY_LOOP"},
            {"target": "Sanctions_Screener", "fault_category": "PRIVILEGE_ESCALATION"},
            {"target": "Sanctions_Screener", "fault_category": "CORRUPTED_OUTPUT"},
            {"target": "Audit_Logger", "fault_category": "RUNAWAY_LOOP"},
            # Multi-Tool Faults across ERP, FX, KYC, and Audit
            {"target": "erp:read", "fault_category": "SERVICE_UNAVAILABLE"},
            {"target": "fx:quote", "fault_category": "SERVICE_UNAVAILABLE"},
            {
                "target": "kyc:check",
                "fault_category": "NETWORK_OUTAGE",
                "fault_config": {"fast_failover": True},
            },
            {"target": "audit:log", "fault_category": "SERVICE_UNAVAILABLE"},
        ]
        injected_rules = self.kernel.inject_multi_chaos_faults(simultaneous_faults)

        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"
        total_storm_mttr = round(time.perf_counter() - t_storm_start, 4)
        self.kernel.last_mttr_seconds = total_storm_mttr

        return {
            "scenario_id": 6,
            "scenario_name": "Simultaneous Multi-Agent & Multi-Tool Chaos Storm",
            "faults_injected_count": len(injected_rules),
            "injected_rules": injected_rules,
            "agent_respawns_count": len(self.kernel.agent_respawn_events),
            "tool_failovers_count": len(self.kernel.failover_events),
            "subtree_rollbacks_count": len(self.kernel.recovery_history),
            "mttr_seconds": total_storm_mttr,
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    async def run_custom_live_workflow(self) -> dict[str, Any]:
        """
        Run the dual-branch workflow on top of whatever custom multi-agent & multi-tool
        faults have been armed in C6 without wiping existing fault queues.
        """
        t_start = time.perf_counter()
        # Preserve armed fault queues in C6 while re-bootstrapping clean agent hierarchy
        saved_queues = self.kernel.C6.fault_queues
        saved_active = self.kernel.C6.active_fault_rules
        saved_tool_health = self.kernel.C6.tool_health_registry
        self.kernel.reset()
        self.kernel.C6.fault_queues = saved_queues
        self.kernel.C6.active_fault_rules = saved_active
        self.kernel.C6.tool_health_registry = saved_tool_health

        tokens = self._bootstrap_and_delegate_hierarchy()
        branch_a_res, branch_b_res = await asyncio.gather(
            self._run_branch_a(tokens),
            self._run_branch_b(tokens),
        )
        self.kernel.C3.process_table["Chief_Treasury_Orchestrator"][
            "status"
        ] = "COMPLETED"
        self.kernel.last_mttr_seconds = round(time.perf_counter() - t_start, 4)

        return {
            "scenario_id": 0,
            "scenario_name": "Custom Multi-Agent & Multi-Tool Fault Run",
            "settlement_eur": branch_a_res["settlement_eur"],
            "fx_rate": branch_a_res["fx_rate"],
            "provider": branch_a_res["provider"],
            "branch_a": branch_a_res,
            "branch_b": branch_b_res,
            "state": self.kernel.get_full_state_and_metrics(),
        }

    async def run_scenario(
        self, scenario_id: int, reset_kernel: bool = True
    ) -> dict[str, Any]:
        """Dispatch and execute Scenario 1, 2, 3, 4, 5, or 6 by ID."""
        dispatch = {
            1: self.run_scenario_1,
            2: self.run_scenario_2,
            3: self.run_scenario_3,
            4: self.run_scenario_4,
            5: self.run_scenario_5,
            6: self.run_scenario_6,
        }
        if scenario_id not in dispatch:
            raise ValueError(f"Unsupported scenario_id: {scenario_id}. Must be 1..6")
        return await dispatch[scenario_id](reset_kernel=reset_kernel)
