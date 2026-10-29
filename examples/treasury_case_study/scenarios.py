"""TreasuryScenarioRunner executing Scenarios 1–5 with empirical metric calculation."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from uuid import uuid4

from fastapi import FastAPI
import httpx

from aegis_core.kernel import ControlPlaneKernel, default_kernel
from aegis_core.server import create_aegis_app
from aegis_sdk.client import AegisAgentNode, AegisClient, AegisHTTPException
from examples.treasury_case_study.agents import (
    TreasuryAgentFleet,
    build_treasury_agent_fleet,
)
from examples.treasury_case_study.tools_and_gates import register_treasury_plugin


class TreasuryScenarioRunner:
    """
    Executes all 5 Corporate Treasury & Cross-Border SWIFT Settlement Chaos Scenarios
    against the domain-agnostic AEGIS Control Plane using AegisAgentNode HTTP Bearer tokens.
    """

    def __init__(
        self,
        kernel: Optional[ControlPlaneKernel] = None,
        fastapi_app: Optional[FastAPI] = None,
    ) -> None:
        self.kernel = kernel or default_kernel
        register_treasury_plugin(self.kernel)
        self.app = fastapi_app or create_aegis_app(self.kernel)
        self.client = AegisClient(fastapi_app=self.app)

    def _mark_completed(self, *agent_ids: str) -> None:
        for a_id in agent_ids:
            if a_id in self.kernel.c3.process_table:
                if self.kernel.c3.process_table[a_id]["status"] not in (
                    "KILLED",
                    "BLOCKED_403",
                ):
                    self.kernel.c3.process_table[a_id]["status"] = "COMPLETED"

    async def _execute_branch_b(
        self, fleet: TreasuryAgentFleet
    ) -> dict[str, Any]:
        """Run Branch B (Compliance & Audit Bulkhead) concurrently."""
        self.kernel.c3.process_table["Compliance_Supervisor"][
            "status"
        ] = "RUNNING"
        await fleet.sup_b.checkpoint(
            state={
                "branch_id": "Branch_B",
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "stage": "PRE_KYC",
            },
            checkpoint_id="chk_Branch_B_1",
        )

        trace_b = f"trace_b_{uuid4().hex[:8]}"
        kyc_res = await fleet.sanctions_screener.call_tool(
            "kyc:check",
            trace_id=trace_b,
            supplier="Siemens Energy AG",
        )
        kyc_data = kyc_res["data"]

        await fleet.sanctions_screener.send_a2a(
            receiver_id="Compliance_Supervisor",
            payload=kyc_data,
            trace_id=trace_b,
        )
        await fleet.sup_b.send_a2a(
            receiver_id="Audit_Logger",
            payload=kyc_data,
            trace_id=trace_b,
        )
        audit_res = await fleet.audit_logger.call_tool(
            "audit:log",
            trace_id=trace_b,
            invoice_id="INV-2026-9981",
            supplier="Siemens Energy AG",
            sanctions_status=kyc_data["sanctions_status"],
        )

        self._mark_completed(
            "Sanctions_Screener", "Audit_Logger", "Compliance_Supervisor"
        )
        return {
            "branch_id": "Branch_B",
            "kyc_data": kyc_data,
            "audit_data": audit_res["data"],
            "status": "COMPLETED",
        }

    # =========================================================================
    # Scenario 1: Normal Execution & Cryptographic Delegation
    # =========================================================================
    async def run_scenario_1_normal(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)

        async def _run_branch_a() -> dict[str, Any]:
            self.kernel.c3.process_table["FX_Settlement_Supervisor"][
                "status"
            ] = "RUNNING"
            chk = await fleet.sup_a.checkpoint(
                state={
                    "branch_id": "Branch_A",
                    "invoice_id": "INV-2026-9981",
                    "supplier": "Siemens Energy AG",
                    "iban": "DE89370400440532013000",
                    "amount_usd": 2000000.0,
                    "stage": "PRE_FX_QUOTE",
                },
                checkpoint_id="chk_Branch_A_1",
            )

            trace_a = f"trace_a_{uuid4().hex[:8]}"
            erp_res = await fleet.fx_reader.call_tool(
                "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
            )
            inv = erp_res["data"]

            fx_res = await fleet.fx_reader.call_tool(
                "fx:quote", trace_id=trace_a, amount_usd=inv["amount_usd"]
            )
            quote = fx_res["data"]

            a2a_payload = {
                "invoice_id": inv["invoice_id"],
                "supplier": inv["supplier"],
                "iban": inv["iban"],
                "amount_usd": inv["amount_usd"],
                "fx_rate": quote["fx_rate"],
                "settlement_eur": quote["settlement_eur"],
                "provider": quote["provider"],
            }
            await fleet.fx_reader.send_a2a(
                receiver_id="FX_Settlement_Supervisor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )
            await fleet.sup_a.send_a2a(
                receiver_id="SWIFT_Wire_Executor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )

            wire_res = await fleet.swift_executor.call_tool(
                "swift:wire",
                trace_id=trace_a,
                **a2a_payload,
            )
            self._mark_completed(
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
                "FX_Settlement_Supervisor",
            )
            return {
                "branch_id": "Branch_A",
                "checkpoint_id": chk["checkpoint_id"],
                "wire_data": wire_res["data"],
                "settlement_eur": wire_res["data"]["settlement_eur"],
                "fx_rate": wire_res["data"]["fx_rate"],
                "provider": wire_res["data"]["provider"],
            }

        res_a, res_b = await asyncio.gather(
            _run_branch_a(), self._execute_branch_b(fleet)
        )
        self._mark_completed("Chief_Treasury_Orchestrator")
        return {
            "scenario_id": 1,
            "scenario_name": "Scenario 1: Normal Execution & Cryptographic Delegation",
            "settlement_eur": res_a["settlement_eur"],
            "fx_rate": res_a["fx_rate"],
            "provider": res_a["provider"],
            "branch_a": res_a,
            "branch_b": res_b,
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 2: Confused Deputy / RBAC Privilege Escalation Block
    # =========================================================================
    async def run_scenario_2_confused_deputy_rbac(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)

        # Inject PRIVILEGE_ESCALATION fault on FX_Quote_Reader forcing unauthorized swift:wire call
        await self.client.inject_chaos_fault(
            target="FX_Quote_Reader",
            fault_category="PRIVILEGE_ESCALATION",
            config={
                "unauthorized_tool": "swift:wire",
                "forced_args": {
                    "invoice_id": "INV-2026-9981",
                    "amount_usd": 2000000.0,
                    "fx_rate": 0.9200,
                    "settlement_eur": 1840000.0,
                },
            },
        )

        async def _run_branch_a() -> dict[str, Any]:
            t_start = time.perf_counter()
            chk = await fleet.sup_a.checkpoint(
                state={
                    "branch_id": "Branch_A",
                    "invoice_id": "INV-2026-9981",
                    "amount_usd": 2000000.0,
                    "stage": "PRE_FX_QUOTE",
                },
                checkpoint_id="chk_Branch_A_1",
            )
            trace_a = f"trace_a_{uuid4().hex[:8]}"

            blocked_status_code = 200
            blocked_detail = ""
            try:
                await fleet.fx_reader.call_tool(
                    "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
                )
            except AegisHTTPException as exc:
                blocked_status_code = exc.status_code
                blocked_detail = exc.detail

            # Trigger SURGICAL kill-switch on compromised FX_Quote_Reader
            kill_receipt = await self.client.trigger_kill_switch(
                mode="SURGICAL",
                target_id="FX_Quote_Reader",
            )

            # Spawn replacement FX_Quote_Reader_v2 from FX_Settlement_Supervisor
            fx_reader_v2 = await fleet.sup_a.spawn_child(
                child_id="FX_Quote_Reader_v2",
                requested_role="L2_FX_Quote_Reader_v2",
                requested_scopes=["erp:read", "fx:quote"],
                token_budget=1800,
                branch_id="Branch_A",
            )

            erp_res = await fx_reader_v2.call_tool(
                "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
            )
            inv = erp_res["data"]
            fx_res = await fx_reader_v2.call_tool(
                "fx:quote", trace_id=trace_a, amount_usd=inv["amount_usd"]
            )
            quote = fx_res["data"]

            a2a_payload = {
                "invoice_id": inv["invoice_id"],
                "supplier": inv["supplier"],
                "iban": inv["iban"],
                "amount_usd": inv["amount_usd"],
                "fx_rate": quote["fx_rate"],
                "settlement_eur": quote["settlement_eur"],
                "provider": quote["provider"],
            }
            await fx_reader_v2.send_a2a(
                receiver_id="FX_Settlement_Supervisor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )
            await fleet.sup_a.send_a2a(
                receiver_id="SWIFT_Wire_Executor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )
            wire_res = await fleet.swift_executor.call_tool(
                "swift:wire", trace_id=trace_a, **a2a_payload
            )
            self.kernel.last_mttr_seconds = round(
                time.perf_counter() - t_start, 4
            )
            self._mark_completed(
                "FX_Quote_Reader_v2",
                "SWIFT_Wire_Executor",
                "FX_Settlement_Supervisor",
            )
            return {
                "branch_id": "Branch_A",
                "checkpoint_id": chk["checkpoint_id"],
                "blocked_http_code": blocked_status_code,
                "blocked_detail": blocked_detail,
                "surgical_kill_receipt": kill_receipt,
                "respawned_agent_id": fx_reader_v2.agent_id,
                "wire_data": wire_res["data"],
                "settlement_eur": wire_res["data"]["settlement_eur"],
                "fx_rate": wire_res["data"]["fx_rate"],
                "provider": wire_res["data"]["provider"],
            }

        res_a, res_b = await asyncio.gather(
            _run_branch_a(), self._execute_branch_b(fleet)
        )
        self._mark_completed("Chief_Treasury_Orchestrator")
        return {
            "scenario_id": 2,
            "scenario_name": "Scenario 2: Confused Deputy / RBAC Privilege Escalation Block",
            "blocked_http_code": res_a["blocked_http_code"],
            "rbac_block_rate": self.kernel.c2.rbac_block_rate,
            "settlement_eur": res_a["settlement_eur"],
            "fx_rate": res_a["fx_rate"],
            "provider": res_a["provider"],
            "branch_a": res_a,
            "branch_b": res_b,
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 3: Corrupted FX Output, Causal BFS Taint & Surgical Rollback
    # =========================================================================
    async def run_scenario_3_corrupted_output_rollback(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)

        # Inject 10x CORRUPTED_OUTPUT on FX_Quote_Reader (0.9200 -> 9.200, 1.84M -> 18.4M)
        await self.client.inject_chaos_fault(
            target="FX_Quote_Reader",
            fault_category="CORRUPTED_OUTPUT",
            config={
                "mutations": {
                    "fx_rate": {"multiply": 10.0},
                    "settlement_eur": {"multiply": 10.0},
                }
            },
        )

        async def _run_branch_a() -> dict[str, Any]:
            t_start = time.perf_counter()
            chk = await fleet.sup_a.checkpoint(
                state={
                    "branch_id": "Branch_A",
                    "invoice_id": "INV-2026-9981",
                    "supplier": "Siemens Energy AG",
                    "iban": "DE89370400440532013000",
                    "amount_usd": 2000000.0,
                    "stage": "PRE_FX_QUOTE",
                },
                checkpoint_id="chk_Branch_A_1",
            )
            trace_a = f"trace_a_{uuid4().hex[:8]}"

            erp_res = await fleet.fx_reader.call_tool(
                "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
            )
            inv = erp_res["data"]

            fx_res = await fleet.fx_reader.call_tool(
                "fx:quote", trace_id=trace_a, amount_usd=inv["amount_usd"]
            )
            quote = fx_res["data"]

            # Route A2A from FX_Quote_Reader -> FX_Settlement_Supervisor (corrupted in-flight to fx_rate=9.200)
            r1 = await fleet.fx_reader.send_a2a(
                receiver_id="FX_Settlement_Supervisor",
                payload={
                    "invoice_id": inv["invoice_id"],
                    "supplier": inv["supplier"],
                    "iban": inv["iban"],
                    "amount_usd": inv["amount_usd"],
                    "fx_rate": quote["fx_rate"],
                    "settlement_eur": quote["settlement_eur"],
                    "provider": quote["provider"],
                },
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
                optimistic_pipeline=True,
            )

            # Optimistically pipeline tainted payload from FX_Settlement_Supervisor -> SWIFT_Wire_Executor
            r2 = await fleet.sup_a.send_a2a(
                receiver_id="SWIFT_Wire_Executor",
                payload=r1["payload"],
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
                optimistic_pipeline=True,
            )

            # Attempt state-mutating swift:wire -> Gate L4 Pre-Commit Gate blocks it!
            gate_l4_code = 200
            gate_l4_error = ""
            try:
                await fleet.swift_executor.call_tool(
                    "swift:wire",
                    trace_id=trace_a,
                    **r2["payload"],
                )
            except AegisHTTPException as exc:
                gate_l4_code = exc.status_code
                gate_l4_error = exc.detail

            # Invoke isolate_and_rollback("FX_Quote_Reader")
            rollback_report = await self.client.isolate_and_rollback(
                failed_agent_id="FX_Quote_Reader",
                failure_reason=gate_l4_error,
            )
            restored_state = rollback_report["restored_state"]
            fallback_fx_tool = rollback_report["fallback_tool_map"].get(
                "fx:quote", "fx:quote_reuters"
            )

            # Re-spawn clean Branch_A subtree from root with fresh Ed25519 JWTs
            sup_a_clean = await fleet.root.spawn_child(
                child_id="FX_Settlement_Supervisor",
                requested_role="L1_FX_Settlement_Supervisor_Restored",
                requested_scopes=["erp:read", "fx:quote", "swift:wire"],
                token_budget=4000,
                branch_id="Branch_A",
            )
            fx_reader_clean = await sup_a_clean.spawn_child(
                child_id="FX_Quote_Reader",
                requested_role="L2_FX_Quote_Reader_Restored",
                requested_scopes=["erp:read", "fx:quote"],
                token_budget=1600,
                branch_id="Branch_A",
            )
            swift_exec_clean = await sup_a_clean.spawn_child(
                child_id="SWIFT_Wire_Executor",
                requested_role="L2_SWIFT_Wire_Executor_Restored",
                requested_scopes=["swift:wire"],
                token_budget=1600,
                branch_id="Branch_A",
            )

            # Retry using fallback tool fx:quote_reuters
            trace_retry = f"trace_a_retry_{uuid4().hex[:8]}"
            fx_retry = await fx_reader_clean.call_tool(
                fallback_fx_tool,
                trace_id=trace_retry,
                amount_usd=restored_state["amount_usd"],
            )
            clean_quote = fx_retry["data"]

            clean_payload = {
                "invoice_id": restored_state["invoice_id"],
                "supplier": restored_state["supplier"],
                "iban": restored_state["iban"],
                "amount_usd": restored_state["amount_usd"],
                "fx_rate": clean_quote["fx_rate"],
                "settlement_eur": clean_quote["settlement_eur"],
                "provider": clean_quote["provider"],
            }
            await fx_reader_clean.send_a2a(
                receiver_id="FX_Settlement_Supervisor",
                payload=clean_payload,
                trace_id=trace_retry,
                schema_name="TreasurySettlementSchema",
            )
            await sup_a_clean.send_a2a(
                receiver_id="SWIFT_Wire_Executor",
                payload=clean_payload,
                trace_id=trace_retry,
                schema_name="TreasurySettlementSchema",
            )
            wire_res = await swift_exec_clean.call_tool(
                "swift:wire",
                trace_id=trace_retry,
                **clean_payload,
            )

            total_mttr = round(time.perf_counter() - t_start, 4)
            rollback_report["mttr_seconds"] = total_mttr
            self.kernel.last_mttr_seconds = total_mttr
            self._mark_completed(
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
                "FX_Settlement_Supervisor",
            )
            return {
                "branch_id": "Branch_A",
                "checkpoint_id": chk["checkpoint_id"],
                "gate_l4_http_code": gate_l4_code,
                "gate_l4_error": gate_l4_error,
                "rollback_report": rollback_report,
                "wire_data": wire_res["data"],
                "settlement_eur": wire_res["data"]["settlement_eur"],
                "fx_rate": wire_res["data"]["fx_rate"],
                "provider": wire_res["data"]["provider"],
            }

        res_a, res_b = await asyncio.gather(
            _run_branch_a(), self._execute_branch_b(fleet)
        )
        self._mark_completed("Chief_Treasury_Orchestrator")
        rb = res_a["rollback_report"]
        return {
            "scenario_id": 3,
            "scenario_name": "Scenario 3: Corrupted FX Output, Causal BFS Taint & Surgical Rollback",
            "tainted_bfs_nodes": rb["tainted_bfs_nodes"],
            "blast_radius_ratio": rb["blast_radius_ratio"],
            "mttr_seconds": rb["mttr_seconds"],
            "restored_checkpoint_id": rb["restored_checkpoint_id"],
            "settlement_eur": res_a["settlement_eur"],
            "fx_rate": res_a["fx_rate"],
            "provider": res_a["provider"],
            "branch_a": res_a,
            "branch_b": res_b,
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 4: Infrastructure Outage (HTTP 503) & Automatic Failover
    # =========================================================================
    async def run_scenario_4_service_unavailable_failover(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)

        # Inject SERVICE_UNAVAILABLE (HTTP 503) on fx:quote (Bloomberg Primary)
        await self.client.inject_chaos_fault(
            target="fx:quote",
            fault_category="SERVICE_UNAVAILABLE",
        )

        async def _run_branch_a() -> dict[str, Any]:
            t_start = time.perf_counter()
            await fleet.sup_a.checkpoint(
                state={
                    "branch_id": "Branch_A",
                    "invoice_id": "INV-2026-9981",
                    "amount_usd": 2000000.0,
                    "stage": "PRE_FX_QUOTE",
                },
                checkpoint_id="chk_Branch_A_1",
            )
            trace_a = f"trace_a_{uuid4().hex[:8]}"

            erp_res = await fleet.fx_reader.call_tool(
                "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
            )
            inv = erp_res["data"]

            primary_status_code = 200
            fallback_tool = "fx:quote_reuters"
            try:
                fx_res = await fleet.fx_reader.call_tool(
                    "fx:quote", trace_id=trace_a, amount_usd=inv["amount_usd"]
                )
            except AegisHTTPException as exc:
                primary_status_code = exc.status_code
                fallback_tool = (
                    exc.payload.get("details", {}).get("fallback_tool_name")
                    or "fx:quote_reuters"
                )
                fx_res = await fleet.fx_reader.call_tool(
                    fallback_tool,
                    trace_id=trace_a,
                    amount_usd=inv["amount_usd"],
                )

            quote = fx_res["data"]
            a2a_payload = {
                "invoice_id": inv["invoice_id"],
                "supplier": inv["supplier"],
                "iban": inv["iban"],
                "amount_usd": inv["amount_usd"],
                "fx_rate": quote["fx_rate"],
                "settlement_eur": quote["settlement_eur"],
                "provider": quote["provider"],
            }
            await fleet.fx_reader.send_a2a(
                receiver_id="FX_Settlement_Supervisor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )
            await fleet.sup_a.send_a2a(
                receiver_id="SWIFT_Wire_Executor",
                payload=a2a_payload,
                trace_id=trace_a,
                schema_name="TreasurySettlementSchema",
            )
            wire_res = await fleet.swift_executor.call_tool(
                "swift:wire", trace_id=trace_a, **a2a_payload
            )
            self.kernel.last_mttr_seconds = round(
                time.perf_counter() - t_start, 4
            )
            self._mark_completed(
                "FX_Quote_Reader",
                "SWIFT_Wire_Executor",
                "FX_Settlement_Supervisor",
            )
            return {
                "branch_id": "Branch_A",
                "primary_http_code": primary_status_code,
                "failover_tool_used": fallback_tool,
                "wire_data": wire_res["data"],
                "settlement_eur": wire_res["data"]["settlement_eur"],
                "fx_rate": wire_res["data"]["fx_rate"],
                "provider": wire_res["data"]["provider"],
            }

        res_a, res_b = await asyncio.gather(
            _run_branch_a(), self._execute_branch_b(fleet)
        )
        self._mark_completed("Chief_Treasury_Orchestrator")
        return {
            "scenario_id": 4,
            "scenario_name": "Scenario 4: Infrastructure Outage (HTTP 503) & Automatic Failover",
            "primary_http_code": res_a["primary_http_code"],
            "mttr_seconds": self.kernel.last_mttr_seconds,
            "settlement_eur": res_a["settlement_eur"],
            "fx_rate": res_a["fx_rate"],
            "provider": res_a["provider"],
            "branch_a": res_a,
            "branch_b": res_b,
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 5A: SOFT_FREEZE Human Override
    # =========================================================================
    async def run_scenario_5a_soft_freeze_override(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)
        await fleet.sup_a.checkpoint(
            state={
                "branch_id": "Branch_A",
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            checkpoint_id="chk_Branch_A_1",
        )

        trace_a = f"trace_a_{uuid4().hex[:8]}"
        erp_res = await fleet.fx_reader.call_tool(
            "erp:read", trace_id=trace_a, invoice_id="INV-2026-9981"
        )
        inv = erp_res["data"]
        fx_res = await fleet.fx_reader.call_tool(
            "fx:quote", trace_id=trace_a, amount_usd=inv["amount_usd"]
        )
        quote = fx_res["data"]

        a2a_payload = {
            "invoice_id": inv["invoice_id"],
            "supplier": inv["supplier"],
            "iban": inv["iban"],
            "amount_usd": inv["amount_usd"],
            "fx_rate": quote["fx_rate"],
            "settlement_eur": quote["settlement_eur"],
            "provider": quote["provider"],
        }
        await fleet.fx_reader.send_a2a(
            receiver_id="FX_Settlement_Supervisor",
            payload=a2a_payload,
            trace_id=trace_a,
            schema_name="TreasurySettlementSchema",
        )
        await fleet.sup_a.send_a2a(
            receiver_id="SWIFT_Wire_Executor",
            payload=a2a_payload,
            trace_id=trace_a,
            schema_name="TreasurySettlementSchema",
        )

        # Pause Branch_A via SOFT_FREEZE right before swift:wire
        pause_receipt = await self.client.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id="Branch_A",
            operator_override_state=None,
        )

        wire_task = asyncio.create_task(
            fleet.swift_executor.call_tool(
                "swift:wire", trace_id=trace_a, **a2a_payload
            )
        )
        branch_b_task = asyncio.create_task(self._execute_branch_b(fleet))
        await asyncio.sleep(0.01)

        # Inject human operator override {"fx_rate": 0.9195, "settlement_eur": 1839000.0} and resume
        override_state = {"fx_rate": 0.9195, "settlement_eur": 1839000.0}
        resume_receipt = await self.client.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id="Branch_A",
            operator_override_state=override_state,
        )

        wire_res, res_b = await asyncio.gather(wire_task, branch_b_task)
        self._mark_completed(
            "FX_Quote_Reader",
            "SWIFT_Wire_Executor",
            "FX_Settlement_Supervisor",
            "Chief_Treasury_Orchestrator",
        )
        return {
            "scenario_id": "5A",
            "scenario_name": "Scenario 5A: Soft-Freeze Human Operator Override",
            "pause_receipt": pause_receipt,
            "resume_receipt": resume_receipt,
            "settlement_eur": wire_res["data"]["settlement_eur"],
            "fx_rate": wire_res["data"]["fx_rate"],
            "wire_data": wire_res["data"],
            "branch_b": res_b,
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 5B: GLOBAL_HARD Kill-Switch (< 50ms)
    # =========================================================================
    async def run_scenario_5b_global_hard_kill(self) -> dict[str, Any]:
        self.kernel.reset_state(preserve_registered_tools=True)
        register_treasury_plugin(self.kernel)

        fleet = await build_treasury_agent_fleet(self.app)

        # Launch long-running async agent tasks with active httpx.AsyncClient sockets registered in C3
        async def _long_running_agent_work(
            node: AegisAgentNode, tool_name: str
        ) -> None:
            sock = httpx.AsyncClient()
            proc = self.kernel.c3.process_table[node.agent_id]
            proc["task_handle"] = asyncio.current_task()
            proc["http_client"] = sock
            proc["status"] = "IN_FLIGHT_TOOL"
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                raise
            finally:
                if not sock.is_closed:
                    await sock.aclose()

        active_tasks = [
            asyncio.create_task(
                _long_running_agent_work(fleet.root, "erp:read")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.sup_a, "erp:read")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.fx_reader, "fx:quote")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.swift_executor, "swift:wire")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.sup_b, "kyc:check")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.sanctions_screener, "kyc:check")
            ),
            asyncio.create_task(
                _long_running_agent_work(fleet.audit_logger, "audit:log")
            ),
        ]
        await asyncio.sleep(0.01)

        kill_receipt = await self.client.trigger_kill_switch(
            mode="GLOBAL_HARD",
            target_id="ALL",
        )
        await asyncio.gather(*active_tasks, return_exceptions=True)

        return {
            "scenario_id": "5B",
            "scenario_name": "Scenario 5B: Emergency Global Hard Kill-Switch (< 50ms)",
            "kill_switch_receipt": kill_receipt,
            "termination_latency_ms": kill_receipt["termination_latency_ms"],
            "t_revoke_ms": kill_receipt["t_revoke_ms"],
            "t_cancel_ms": kill_receipt["t_cancel_ms"],
            "t_socket_close_ms": kill_receipt["t_socket_close_ms"],
            "telemetry": self.kernel.get_telemetry_state(),
        }

    # =========================================================================
    # Scenario 5 Combined (Part A + Part B)
    # =========================================================================
    async def run_scenario_5_kill_switch_and_override(self) -> dict[str, Any]:
        part_a = await self.run_scenario_5a_soft_freeze_override()
        part_b = await self.run_scenario_5b_global_hard_kill()
        return {
            "scenario_id": 5,
            "scenario_name": "Scenario 5: Soft-Freeze Override & Global Hard Kill-Switch",
            "part_a": part_a,
            "part_b": part_b,
            "settlement_eur": part_a["settlement_eur"],
            "termination_latency_ms": part_b["termination_latency_ms"],
            "telemetry": part_b["telemetry"],
        }

    async def run_all_scenarios(self) -> dict[str, Any]:
        s1 = await self.run_scenario_1_normal()
        s2 = await self.run_scenario_2_confused_deputy_rbac()
        s3 = await self.run_scenario_3_corrupted_output_rollback()
        s4 = await self.run_scenario_4_service_unavailable_failover()
        s5 = await self.run_scenario_5_kill_switch_and_override()
        return {
            "scenario_1": s1,
            "scenario_2": s2,
            "scenario_3": s3,
            "scenario_4": s4,
            "scenario_5": s5,
            "summary_metrics": {
                "scenario_1_settlement_eur": s1["settlement_eur"],
                "scenario_2_rbac_block_rate_pct": s2["rbac_block_rate"],
                "scenario_3_blast_radius_pct": s3["blast_radius_ratio"] * 100.0,
                "scenario_3_mttr_seconds": s3["mttr_seconds"],
                "scenario_4_failover_provider": s4["provider"],
                "scenario_5a_override_eur": s5["part_a"]["settlement_eur"],
                "scenario_5b_kill_latency_ms": s5["part_b"][
                    "termination_latency_ms"
                ],
            },
        }
