"""FastAPI REST & WebSocket Server exposing the AEGIS Control Plane on Port 8000."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from agents.treasury_graph import TreasuryWorkflowRunner
from control_plane.methods import ControlPlaneKernel, kernel_singleton
from control_plane.models import (
    A2ARouteRequest,
    ChaosInjectRequest,
    CheckpointRequest,
    KillSwitchRequest,
    MultiChaosInjectRequest,
    RollbackRequest,
    TokenMintRequest,
    ToolInterceptRequest,
)

app = FastAPI(
    title="AEGIS 2026 PS01 — Agent Identity, Failure Recovery & Kill-Switch Control Plane",
    version="1.0.0",
    description="Zero-Trust Control Plane for Autonomous Corporate Treasury & SWIFT Wire Settlement",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

kernel: ControlPlaneKernel = kernel_singleton
runner = TreasuryWorkflowRunner(kernel=kernel)
HTML_DASHBOARD_PATH = (
    Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
)


@app.get("/", response_class=HTMLResponse)
async def serve_html_dashboard() -> HTMLResponse:
    """Serve the interactive HTML Observability & Multi-Failure Lab."""
    if HTML_DASHBOARD_PATH.exists():
        return HTMLResponse(content=HTML_DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>AEGIS Control Plane Running</h1>")


@app.post("/api/v1/token/bootstrap")
async def api_bootstrap_root_token(
    root_agent_id: str = "Chief_Treasury_Orchestrator",
    token_budget: int = 10000,
) -> dict[str, Any]:
    """Bootstrap L0 root orchestrator token."""
    token = kernel.bootstrap_root_token(
        root_agent_id=root_agent_id,
        token_budget=token_budget,
    )
    return {"status": "SUCCESS", "root_agent_id": root_agent_id, "jwt": token}


@app.post("/api/v1/token/mint")
async def api_mint_delegated_token(req: TokenMintRequest) -> dict[str, Any]:
    """Method 1: Mint attenuated child JWT token."""
    try:
        child_jwt = kernel.mint_delegated_token(
            parent_jwt=req.parent_jwt,
            child_agent_id=req.child_agent_id,
            requested_role=req.requested_role,
            requested_scopes=req.requested_scopes,
            token_budget=req.token_budget,
            branch_id=req.branch_id,
        )
        return {
            "status": "SUCCESS",
            "child_agent_id": req.child_agent_id,
            "jwt": child_jwt,
            "jti": kernel.C1.agent_current_jti.get(req.child_agent_id),
        }
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/checkpoint")
async def api_create_checkpoint(req: CheckpointRequest) -> dict[str, Any]:
    """Method 2: Create versioned subtree checkpoint in C5."""
    try:
        chk_id = kernel.create_checkpoint(
            agent_jwt=req.agent_jwt,
            branch_id=req.branch_id,
            current_graph_state=req.current_graph_state,
        )
        return {
            "status": "SUCCESS",
            "checkpoint_id": chk_id,
            "branch_id": req.branch_id,
        }
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/tool/intercept")
async def api_intercept_tool_call(req: ToolInterceptRequest) -> dict[str, Any]:
    """Method 3: Intercept tool call through Zero-Trust Proxy & Gate L4."""
    try:
        result = await kernel.intercept_tool_call(
            agent_jwt=req.agent_jwt,
            tool_name=req.tool_name,
            tool_args=req.tool_args,
            trace_id=req.trace_id,
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/v1/a2a/route")
async def api_route_a2a_message(req: A2ARouteRequest) -> dict[str, Any]:
    """Method 4: Route Agent-to-Agent message through Gates L1–L3 and record causal DAG edge."""
    try:
        result = await kernel.route_a2a_message(
            sender_jwt=req.sender_jwt,
            receiver_id=req.receiver_id,
            payload=req.payload,
            trace_id=req.trace_id,
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/v1/chaos/inject")
async def api_inject_chaos_fault(req: ChaosInjectRequest) -> dict[str, Any]:
    """Method 5: Inject deterministic or probabilistic chaos fault into C6."""
    try:
        rule = kernel.inject_chaos_fault(
            target=req.target,
            fault_category=req.fault_category,
            fault_config=req.fault_config,
        )
        return {"status": "INJECTED", "rule": rule}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/chaos/inject-batch")
async def api_inject_batch_chaos_faults(
    req: MultiChaosInjectRequest,
) -> dict[str, Any]:
    """Inject simultaneous faults across multiple agents and multiple tools."""
    try:
        rules = kernel.inject_multi_chaos_faults(
            [f.model_dump() for f in req.faults]
        )
        return {"status": "BATCH_INJECTED", "count": len(rules), "rules": rules}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/recovery/rollback")
async def api_isolate_and_rollback(req: RollbackRequest) -> dict[str, Any]:
    """Method 6: Isolate tainted BFS cone T_fault and rollback branch to clean C5 snapshot."""
    try:
        report = await kernel.isolate_and_rollback(
            failed_agent_id=req.failed_agent_id,
            failure_reason=req.failure_reason,
        )
        return report
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/killswitch")
async def api_trigger_kill_switch(req: KillSwitchRequest) -> dict[str, Any]:
    """Method 7: Trigger SURGICAL, SOFT_FREEZE, or GLOBAL_HARD kill-switch (<50ms)."""
    try:
        receipt = await kernel.trigger_kill_switch(
            mode=req.mode,
            target_id=req.target_id,
            operator_override_state=req.operator_override_state,
        )
        return receipt
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/workflow/run/{scenario_id}")
async def api_run_workflow_scenario(
    scenario_id: int,
    reset_kernel: bool = True,
) -> dict[str, Any]:
    """Execute Scenario 1, 2, 3, 4, 5, or 6 end-to-end and return full telemetry & state snapshots."""
    try:
        return await runner.run_scenario(
            scenario_id=scenario_id,
            reset_kernel=reset_kernel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/workflow/run-custom-batch")
async def api_run_custom_batch_workflow(
    req: MultiChaosInjectRequest,
) -> dict[str, Any]:
    """Reset kernel, inject simultaneous multi-agent & multi-tool faults, and run resilient workflow."""
    try:
        kernel.reset()
        kernel.inject_multi_chaos_faults([f.model_dump() for f in req.faults])
        return await runner.run_custom_live_workflow()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/state")
async def api_get_state() -> dict[str, Any]:
    """Return live state of C1–C6 and the 5 evaluation metrics."""
    return kernel.get_full_state_and_metrics()


@app.websocket("/ws/telemetry")
async def websocket_telemetry(ws: WebSocket) -> None:
    """Stream live Control Plane C1–C6 telemetry over WebSocket."""
    await ws.accept()
    try:
        while True:
            await ws.send_json(kernel.get_full_state_and_metrics())
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
