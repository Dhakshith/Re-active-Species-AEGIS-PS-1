"""FastAPI Server for the Domain-Agnostic AEGIS Control Plane with HTTP Bearer JWT Authentication."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from aegis_core.kernel import ControlPlaneKernel, default_kernel
from aegis_core.models import (
    A2ARouteRequest,
    AegisControlPlaneError,
    ChaosInjectRequest,
    CheckpointCreateRequest,
    DelegatedTokenRequest,
    KillSwitchRequest,
    RollbackRequest,
    RootTokenRequest,
    TokenRevokedOrInvalidError,
    ToolInterceptRequest,
    ToolRegistrationSpec,
)


def create_aegis_app(kernel: Optional[ControlPlaneKernel] = None) -> FastAPI:
    """Create a FastAPI Control Plane application bound to a ControlPlaneKernel instance."""
    active_kernel = kernel or default_kernel

    app = FastAPI(
        title="AEGIS 2026 — Out-of-Band Agent Governance Control Plane",
        version="2.0.0",
        description="Domain-Agnostic Zero-Trust Identity, Causal Taint Lineage, Checkpointing & Sub-50ms Kill-Switch",
    )
    app.state.kernel = active_kernel

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AegisControlPlaneError)
    async def handle_aegis_error(
        request: Request, exc: AegisControlPlaneError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_code,
            content={
                "status": "ERROR",
                "http_code": exc.http_code,
                "error_code": exc.error_code,
                "detail": exc.message,
                "details": exc.details,
            },
        )

    def verify_bearer_token(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        """Extract Bearer <JWT> from HTTP Authorization header and verify via C1_IAM_Engine."""
        if not authorization or not authorization.startswith("Bearer "):
            raise TokenRevokedOrInvalidError(
                "Missing or malformed 'Authorization: Bearer <JWT>' header"
            )
        token_str = authorization.removeprefix("Bearer ").strip()
        if not token_str:
            raise TokenRevokedOrInvalidError("Empty Bearer JWT token")
        return active_kernel.c1.verify_jwt(token_str)

    @app.get("/", response_class=HTMLResponse)
    async def index_page() -> HTMLResponse:
        html_path = (
            Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
        )
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
        return HTMLResponse(
            content="<h2>AEGIS Out-of-Band Control Plane Active on Port 8000</h2>"
        )

    @app.post("/api/v1/tools/register")
    async def api_register_tool(spec: ToolRegistrationSpec) -> dict[str, Any]:
        """Dynamically register a new tool specification into C2_Policy_Store."""
        registered = active_kernel.register_tool(spec)
        return {
            "status": "REGISTERED",
            "tool": registered.model_dump(),
        }

    @app.post("/api/v1/token/root")
    async def api_mint_root_token(req: RootTokenRequest) -> dict[str, Any]:
        """Bootstrap an L0 root orchestrator token."""
        return active_kernel.mint_root_token(
            agent_id=req.agent_id,
            role=req.role,
            scopes=req.scopes,
            token_budget=req.token_budget,
            branch_id=req.branch_id,
        )

    @app.post("/api/v1/token/mint")
    async def api_mint_delegated_token(
        req: DelegatedTokenRequest,
        parent_claims: dict[str, Any] = Depends(verify_bearer_token),
    ) -> dict[str, Any]:
        """Mint an attenuated child JWT using caller's verified parent Bearer JWT."""
        return active_kernel.mint_delegated_token(
            parent_claims=parent_claims,
            child_agent_id=req.child_agent_id,
            requested_role=req.requested_role,
            requested_scopes=req.requested_scopes,
            token_budget=req.token_budget,
            branch_id=req.branch_id,
        )

    @app.post("/api/v1/checkpoint")
    async def api_create_checkpoint(
        req: CheckpointCreateRequest,
        agent_claims: dict[str, Any] = Depends(verify_bearer_token),
    ) -> dict[str, Any]:
        """Save a clean versioned subtree checkpoint into C5_Snapshot_DB."""
        try:
            return active_kernel.create_checkpoint(
                agent_claims=agent_claims,
                branch_id=req.branch_id,
                current_graph_state=req.current_graph_state,
                checkpoint_id=req.checkpoint_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/tool/intercept")
    async def api_intercept_tool_call(
        req: ToolInterceptRequest,
        agent_claims: dict[str, Any] = Depends(verify_bearer_token),
    ) -> dict[str, Any]:
        """Intercept and validate an external tool call through C1–C6 and Gate L4."""
        return await active_kernel.intercept_tool_call(
            agent_claims=agent_claims,
            tool_name=req.tool_name,
            tool_args=req.tool_args,
            trace_id=req.trace_id,
        )

    @app.post("/api/v1/a2a/route")
    async def api_route_a2a_message(
        req: A2ARouteRequest,
        sender_claims: dict[str, Any] = Depends(verify_bearer_token),
    ) -> dict[str, Any]:
        """Route an Agent-to-Agent message through Gates L1–L3 and record causal DAG edge."""
        return await active_kernel.route_a2a_message(
            sender_claims=sender_claims,
            receiver_id=req.receiver_id,
            payload=req.payload,
            trace_id=req.trace_id,
            schema_name=req.schema_name,
            optimistic_pipeline=req.optimistic_pipeline,
        )

    @app.post("/api/v1/chaos/inject")
    async def api_inject_chaos_fault(req: ChaosInjectRequest) -> dict[str, Any]:
        """Operator endpoint: inject a fault rule into C6_Chaos_Monkey."""
        rule = active_kernel.inject_chaos_fault(
            target=req.target,
            fault_category=req.fault_category,
            probability=req.probability,
            config=req.config,
        )
        return {"status": "INJECTED", "rule": rule.model_dump()}

    @app.post("/api/v1/recovery/rollback")
    async def api_isolate_and_rollback(req: RollbackRequest) -> dict[str, Any]:
        """Operator / Supervisor endpoint: compute causal BFS cone T_fault and roll back branch."""
        return await active_kernel.isolate_and_rollback(
            failed_agent_id=req.failed_agent_id,
            failure_reason=req.failure_reason,
        )

    @app.post("/api/v1/killswitch")
    async def api_trigger_kill_switch(req: KillSwitchRequest) -> dict[str, Any]:
        """Operator endpoint: trigger SURGICAL, SOFT_FREEZE, or GLOBAL_HARD kill-switch."""
        receipt = await active_kernel.trigger_kill_switch(
            mode=req.mode,
            target_id=req.target_id,
            operator_override_state=req.operator_override_state,
        )
        return receipt.model_dump()

    @app.post("/api/v1/chaos/remove")
    async def api_remove_chaos_fault(
        target_or_id: str = "ALL",
        clear_agent_status: bool = True,
    ) -> dict[str, Any]:
        """Remove active chaos fault rule(s) from C6_Chaos_Monkey."""
        return active_kernel.remove_chaos_fault(
            target_or_id=target_or_id,
            clear_agent_status=clear_agent_status,
        )

    @app.get("/api/v1/telemetry/state")
    async def api_get_telemetry_state() -> dict[str, Any]:
        """Return full live state of C1–C6, event logs, and empirical KPIs."""
        return active_kernel.get_telemetry_state()

    @app.post("/api/v1/telemetry/reset")
    async def api_reset_telemetry_state(
        preserve_registered_tools: bool = True,
    ) -> dict[str, Any]:
        """Reset kernel state cleanly between scenario runs."""
        active_kernel.reset_state(
            preserve_registered_tools=preserve_registered_tools
        )
        return {"status": "RESET_COMPLETE"}

    return app


app = create_aegis_app(default_kernel)
