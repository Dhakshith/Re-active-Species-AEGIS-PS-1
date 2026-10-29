"""Domain-agnostic Pydantic models and contracts for the AEGIS Out-of-Band Control Plane."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


FaultCategory = Literal[
    "NETWORK_OUTAGE",
    "SERVICE_UNAVAILABLE",
    "CORRUPTED_OUTPUT",
    "PRIVILEGE_ESCALATION",
]

KillSwitchMode = Literal[
    "SURGICAL",
    "SOFT_FREEZE",
    "GLOBAL_HARD",
]

AgentLifecycleStatus = Literal[
    "READY",
    "RUNNING",
    "IN_FLIGHT_TOOL",
    "PAUSED_FREEZE",
    "BLOCKED_403",
    "TAINTED",
    "RESTORED",
    "COMPLETED",
    "KILLED",
]


class AegisControlPlaneError(Exception):
    """Base exception for AEGIS Control Plane governance errors."""

    def __init__(
        self,
        message: str,
        http_code: int = 400,
        error_code: str = "AEGIS_ERROR",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_code = http_code
        self.error_code = error_code
        self.details = details or {}


class TokenRevokedOrInvalidError(AegisControlPlaneError):
    """Raised when a JWT signature is invalid, expired, or its JTI is in C1.revocation_set."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=401,
            error_code="HTTP_401_UNAUTHORIZED_TOKEN",
            details=details,
        )


class PrivilegeEscalationError(AegisControlPlaneError):
    """Raised when an agent requests scopes or tools outside its attenuated JWT scope set."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=403,
            error_code="HTTP_403_PRIVILEGE_ESCALATION",
            details=details,
        )


class RateLimitExceededError(AegisControlPlaneError):
    """Raised when an agent exceeds its token budget or per-minute call limit."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=429,
            error_code="HTTP_429_BUDGET_OR_RATE_EXCEEDED",
            details=details,
        )


class PreCommitGateError(AegisControlPlaneError):
    """Raised by Gate L4 prior to executing a state-mutating tool when taint, provenance, or invariants fail."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=409,
            error_code="HTTP_409_PRE_COMMIT_GATE_BLOCKED",
            details=details,
        )


class ServiceUnavailableFaultError(AegisControlPlaneError):
    """Raised when C6 Chaos Monkey triggers a simulated HTTP 503 Service Unavailable."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=503,
            error_code="HTTP_503_SERVICE_UNAVAILABLE",
            details=details,
        )


class NetworkOutageFaultError(AegisControlPlaneError):
    """Raised when C6 Chaos Monkey triggers a simulated connection drop or timeout."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            http_code=504,
            error_code="HTTP_504_NETWORK_OUTAGE",
            details=details,
        )


class AgentJWTClaims(BaseModel):
    """Cryptographic JWT passport claims issued by C1_IAM_Engine."""

    model_config = ConfigDict(extra="allow")

    jti: str
    sub: str
    parent_id: Optional[str] = None
    branch_id: str = "Root"
    role: str
    scopes: list[str]
    token_budget: int = Field(default=5000, ge=0)
    iat: int
    exp: int


class ToolRegistrationSpec(BaseModel):
    """Declarative specification for dynamically registering any domain tool into C2_Policy_Store."""

    model_config = ConfigDict(extra="allow")

    tool_name: str
    required_scope: str
    mutates_state: bool = False
    provenance_fields: list[str] = Field(default_factory=list)
    parameter_constraints: dict[str, Any] = Field(default_factory=dict)
    fallback_tool_name: Optional[str] = None
    rate_limit_per_min: int = 60
    token_cost_per_call: int = 100
    description: str = ""
    default_response: Optional[dict[str, Any]] = None


class A2AMessageEnvelope(BaseModel):
    """100% domain-agnostic Agent-to-Agent message envelope."""

    model_config = ConfigDict(extra="allow")

    sender_id: str
    receiver_id: str
    trace_id: str
    branch_id: str = "Root"
    schema_name: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ChaosFaultRule(BaseModel):
    """Declarative fault injection rule stored in C6_Chaos_Monkey."""

    model_config = ConfigDict(extra="allow")

    rule_id: str
    target: str
    fault_category: FaultCategory
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    config: dict[str, Any] = Field(default_factory=dict)
    active: bool = True
    triggered_count: int = 0


class KillSwitchRequest(BaseModel):
    """Request contract for multi-granular kill-switch invocation."""

    mode: KillSwitchMode
    target_id: str = "ALL"
    operator_override_state: Optional[dict[str, Any]] = None


class KillSwitchReceipt(BaseModel):
    """High-precision receipt returned by trigger_kill_switch."""

    mode: KillSwitchMode
    target_id: str
    revoked_jtis: list[str] = Field(default_factory=list)
    cancelled_tasks: list[str] = Field(default_factory=list)
    closed_sockets_count: int = 0
    paused_branch: Optional[str] = None
    resumed_branch: Optional[str] = None
    applied_override: Optional[dict[str, Any]] = None
    t_revoke_ms: float = 0.0
    t_cancel_ms: float = 0.0
    t_socket_close_ms: float = 0.0
    termination_latency_ms: float = 0.0


class RootTokenRequest(BaseModel):
    agent_id: str = "Root_Orchestrator"
    role: str = "L0_Orchestrator"
    scopes: list[str]
    token_budget: int = 10000
    branch_id: str = "Root"


class DelegatedTokenRequest(BaseModel):
    child_agent_id: str
    requested_role: str
    requested_scopes: list[str]
    token_budget: int = 2000
    branch_id: Optional[str] = None


class CheckpointCreateRequest(BaseModel):
    branch_id: str
    current_graph_state: dict[str, Any]
    checkpoint_id: Optional[str] = None


class ToolInterceptRequest(BaseModel):
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class A2ARouteRequest(BaseModel):
    receiver_id: str
    payload: dict[str, Any]
    trace_id: str
    schema_name: Optional[str] = None
    optimistic_pipeline: bool = True


class ChaosInjectRequest(BaseModel):
    target: str
    fault_category: FaultCategory
    probability: float = 1.0
    config: dict[str, Any] = Field(default_factory=dict)


class RollbackRequest(BaseModel):
    failed_agent_id: str
    failure_reason: str
