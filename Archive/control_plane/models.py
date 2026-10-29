"""Pydantic schemas, enums, and domain invariant validators for the AEGIS Control Plane."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


FaultCategory = Literal[
    "NETWORK_OUTAGE",
    "SERVICE_UNAVAILABLE",
    "CORRUPTED_OUTPUT",
    "PRIVILEGE_ESCALATION",
    "RUNAWAY_LOOP",
]

KillSwitchMode = Literal[
    "SURGICAL",
    "SOFT_FREEZE",
    "GLOBAL_HARD",
]

AgentStatus = Literal[
    "READY",
    "RUNNING",
    "IN_FLIGHT_TOOL",
    "PAUSED",
    "BLOCKED_403",
    "TAINTED",
    "RESTORED",
    "KILLED",
    "COMPLETED",
]

BranchID = Literal["Root", "Branch_A", "Branch_B", "ALL"]

VALID_FAULT_CATEGORIES: set[str] = {
    "NETWORK_OUTAGE",
    "SERVICE_UNAVAILABLE",
    "CORRUPTED_OUTPUT",
    "PRIVILEGE_ESCALATION",
    "RUNAWAY_LOOP",
}

MASTER_ROOT_SCOPES: list[str] = [
    "erp:read",
    "fx:quote",
    "swift:wire",
    "kyc:check",
    "audit:log",
]

# Multi-tier provider failover chains for all 5 enterprise tools
TOOL_PROVIDER_CHAINS: dict[str, list[str]] = {
    "erp:read": ["sap_s4_primary", "oracle_erp_fallback", "local_ledger_replica"],
    "fx:quote": ["bloomberg_primary", "reuters_fallback", "ecb_reference_backup"],
    "swift:wire": ["swift_gpi_primary", "sepa_target2_fallback", "fedwire_backup"],
    "kyc:check": ["ofac_eu_primary", "worldcheck_fallback", "un_sanctions_backup"],
    "audit:log": ["worm_ledger_primary", "dr_vault_fallback", "cold_archive_backup"],
}


class A2AMessagePayload(BaseModel):
    """Gate L1 syntactic schema contract for Agent-to-Agent (A2A) messages."""

    model_config = ConfigDict(extra="allow")

    invoice_id: Optional[str] = Field(default="INV-2026-9981")
    beneficiary: Optional[str] = Field(default="SIEMENS_ENERGY_DE")
    iban: Optional[str] = Field(default="DE89370400440532013000")
    amount_usd: Optional[float] = Field(default=None)
    fx_rate: Optional[float] = Field(default=None)
    settlement_eur: Optional[float] = Field(default=None)
    provider: Optional[str] = Field(default=None)
    kyc_status: Optional[str] = Field(default=None)
    audit_status: Optional[str] = Field(default=None)
    step: Optional[str] = Field(default=None)

    def verify_gate_l3_invariants(
        self, max_settlement_eur: float = 2_500_000.0
    ) -> tuple[bool, Optional[str]]:
        """
        Gate L3: Deterministic Domain Accounting & Compliance Conservation Invariant.
        Checks:
          1. |amount_usd * fx_rate - settlement_eur| < 0.01
          2. 0 < fx_rate <= 2.0
          3. 0 < settlement_eur <= max_settlement_eur (default 2,500,000.0 for A2A)
          4. If kyc_status is present, it must equal 'CLEAR'
        """
        reasons: list[str] = []

        if self.kyc_status is not None and self.kyc_status != "CLEAR":
            reasons.append(
                f"GATE_L3_COMPLIANCE_INVARIANT_VIOLATION: kyc_status='{self.kyc_status}' != 'CLEAR'"
            )

        if (
            self.amount_usd is not None
            and self.fx_rate is not None
            and self.settlement_eur is not None
        ):
            if not (0.0 < self.fx_rate <= 2.0):
                reasons.append(
                    f"GATE_L3_FX_RATE_BOUNDS_VIOLATION: fx_rate={self.fx_rate} outside (0, 2.0]"
                )

            if not (0.0 < self.settlement_eur <= max_settlement_eur):
                reasons.append(
                    f"GATE_L3_SETTLEMENT_CEILING_VIOLATION: settlement_eur={self.settlement_eur} exceeds ceiling {max_settlement_eur}"
                )

            expected_eur = self.amount_usd * self.fx_rate
            conservation_diff = abs(expected_eur - self.settlement_eur)
            if conservation_diff >= 0.01:
                reasons.append(
                    f"GATE_L3_CONSERVATION_VIOLATION: |{self.amount_usd} * {self.fx_rate} - {self.settlement_eur}| = {conservation_diff:.4f} >= 0.01"
                )

        if reasons:
            return False, "; ".join(reasons)
        return True, None


def validate_tool_args_gate_l3(
    tool_name: str, tool_args: dict[str, Any]
) -> tuple[bool, Optional[str]]:
    """Validate state-mutating tool arguments against Gate L3 domain invariants."""
    if tool_name == "swift:wire":
        amount_usd = float(tool_args.get("amount_usd", 2_000_000.0))
        fx_rate = float(tool_args.get("fx_rate", 0.0))
        settlement_eur = float(
            tool_args.get("settlement_eur", tool_args.get("amount_eur", 0.0))
        )
        payload = A2AMessagePayload(
            amount_usd=amount_usd,
            fx_rate=fx_rate,
            settlement_eur=settlement_eur,
            iban=tool_args.get("iban", "DE89370400440532013000"),
        )
        return payload.verify_gate_l3_invariants(max_settlement_eur=2_500_000.0)

    if tool_name == "audit:log":
        kyc_status = str(tool_args.get("kyc_status", "CLEAR"))
        payload = A2AMessagePayload(kyc_status=kyc_status)
        return payload.verify_gate_l3_invariants()

    return True, None


class TokenMintRequest(BaseModel):
    parent_jwt: str
    child_agent_id: str
    requested_role: str
    requested_scopes: list[str]
    token_budget: int = Field(default=2000, gt=0)
    branch_id: str = Field(default="Branch_A")


class CheckpointRequest(BaseModel):
    agent_jwt: str
    branch_id: str
    current_graph_state: dict[str, Any]


class ToolInterceptRequest(BaseModel):
    agent_jwt: str
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class A2ARouteRequest(BaseModel):
    sender_jwt: str
    receiver_id: str
    payload: dict[str, Any]
    trace_id: str


class ChaosInjectRequest(BaseModel):
    target: str
    fault_category: str
    fault_config: Optional[dict[str, Any]] = None


class MultiChaosInjectRequest(BaseModel):
    faults: list[ChaosInjectRequest]


class RollbackRequest(BaseModel):
    failed_agent_id: str
    failure_reason: str


class KillSwitchRequest(BaseModel):
    mode: str
    target_id: str
    operator_override_state: Optional[dict[str, Any]] = None


class RecoveryReport(BaseModel):
    failed_agent_id: str
    failure_reason: str
    branch_rolled_back: str
    restored_checkpoint_id: str
    tainted_bfs_nodes: list[str]
    revoked_jtis: list[str]
    restored_state: dict[str, Any]
    fallback_provider: str = "reuters_fallback"
    blast_radius_ratio: float = 1.0
    mttr_seconds: float


class KillSwitchReceipt(BaseModel):
    mode: str
    target_id: str
    killed_agents: list[str] = Field(default_factory=list)
    paused_agents: list[str] = Field(default_factory=list)
    resumed_agents: list[str] = Field(default_factory=list)
    revoked_jtis: list[str] = Field(default_factory=list)
    closed_sockets_count: int = 0
    termination_latency_ms: float
    inspect_snapshot: Optional[dict[str, Any]] = None
    operator_override_applied: Optional[dict[str, Any]] = None
