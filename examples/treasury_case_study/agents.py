"""3-Tier Hierarchical Treasury Fleet (L0 -> L2) built strictly using AegisAgentNode SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from fastapi import FastAPI

from aegis_core.server import app as default_fastapi_app
from aegis_sdk.client import AegisAgentNode, AegisClient
from examples.treasury_case_study.tools_and_gates import MASTER_TREASURY_SCOPES


@dataclass
class TreasuryAgentFleet:
    """Holds all 7 AegisAgentNode instances across L0 Root, Branch A, and Branch B."""

    root: AegisAgentNode
    sup_a: AegisAgentNode
    fx_reader: AegisAgentNode
    swift_executor: AegisAgentNode
    sup_b: AegisAgentNode
    sanctions_screener: AegisAgentNode
    audit_logger: AegisAgentNode

    def as_dict(self) -> dict[str, AegisAgentNode]:
        return {
            self.root.agent_id: self.root,
            self.sup_a.agent_id: self.sup_a,
            self.fx_reader.agent_id: self.fx_reader,
            self.swift_executor.agent_id: self.swift_executor,
            self.sup_b.agent_id: self.sup_b,
            self.sanctions_screener.agent_id: self.sanctions_screener,
            self.audit_logger.agent_id: self.audit_logger,
        }


async def build_treasury_agent_fleet(
    fastapi_app: Optional[FastAPI] = default_fastapi_app,
) -> TreasuryAgentFleet:
    """
    Bootstrap L0 Chief_Treasury_Orchestrator and delegate attenuated Ed25519 JWTs
    via HTTP Authorization: Bearer <JWT> headers to spawn L1 supervisors and L2 workers.
    """
    client = AegisClient(fastapi_app=fastapi_app)

    # L0 Root Orchestrator
    root = await client.bootstrap_root_agent(
        agent_id="Chief_Treasury_Orchestrator",
        role="L0_Chief_Treasury_Orchestrator",
        scopes=list(MASTER_TREASURY_SCOPES),
        token_budget=10000,
        branch_id="Root",
    )

    # Branch A (Liquidity & Settlement Bulkhead)
    sup_a = await root.spawn_child(
        child_id="FX_Settlement_Supervisor",
        requested_role="L1_FX_Settlement_Supervisor",
        requested_scopes=["erp:read", "fx:quote", "swift:wire"],
        token_budget=4500,
        branch_id="Branch_A",
    )
    fx_reader = await sup_a.spawn_child(
        child_id="FX_Quote_Reader",
        requested_role="L2_FX_Quote_Reader",
        requested_scopes=["erp:read", "fx:quote"],
        token_budget=1800,
        branch_id="Branch_A",
    )
    swift_executor = await sup_a.spawn_child(
        child_id="SWIFT_Wire_Executor",
        requested_role="L2_SWIFT_Wire_Executor",
        requested_scopes=["swift:wire"],
        token_budget=1800,
        branch_id="Branch_A",
    )

    # Branch B (Compliance & Audit Bulkhead)
    sup_b = await root.spawn_child(
        child_id="Compliance_Supervisor",
        requested_role="L1_Compliance_Supervisor",
        requested_scopes=["kyc:check", "audit:log"],
        token_budget=4000,
        branch_id="Branch_B",
    )
    sanctions_screener = await sup_b.spawn_child(
        child_id="Sanctions_Screener",
        requested_role="L2_Sanctions_Screener",
        requested_scopes=["kyc:check"],
        token_budget=1500,
        branch_id="Branch_B",
    )
    audit_logger = await sup_b.spawn_child(
        child_id="Audit_Logger",
        requested_role="L2_Audit_Logger",
        requested_scopes=["audit:log"],
        token_budget=1500,
        branch_id="Branch_B",
    )

    return TreasuryAgentFleet(
        root=root,
        sup_a=sup_a,
        fx_reader=fx_reader,
        swift_executor=swift_executor,
        sup_b=sup_b,
        sanctions_screener=sanctions_screener,
        audit_logger=audit_logger,
    )
