"""AegisAgentNode and AegisClient SDK for HTTP Bearer JWT Control Plane Interaction."""

from __future__ import annotations

from typing import Any, Optional
from fastapi import FastAPI
import httpx

from aegis_core.server import app as default_fastapi_app


class AegisHTTPException(Exception):
    """Raised when the AEGIS Control Plane returns a non-200 HTTP status code."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.payload = payload or {}


def _build_async_client(
    fastapi_app: Optional[FastAPI] = None,
    base_url: str = "http://aegis-proxy",
) -> httpx.AsyncClient:
    if fastapi_app is not None:
        transport = httpx.ASGITransport(app=fastapi_app)
        return httpx.AsyncClient(transport=transport, base_url=base_url)
    return httpx.AsyncClient(base_url=base_url)


class AegisAgentNode:
    """
    Autonomous Agent SDK Node holding its own Ed25519 JWT passport and communicating
    exclusively through the AEGIS FastAPI Control Plane with HTTP Authorization: Bearer <JWT>.
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        scopes: list[str],
        jwt_token: str,
        branch_id: str = "Root",
        token_budget: int = 2000,
        parent_id: Optional[str] = None,
        jti: Optional[str] = None,
        fastapi_app: Optional[FastAPI] = default_fastapi_app,
        base_url: str = "http://aegis-proxy",
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.scopes = list(scopes)
        self.jwt_token = jwt_token
        self.branch_id = branch_id
        self.token_budget = token_budget
        self.parent_id = parent_id
        self.jti = jti
        self._fastapi_app = fastapi_app
        self._base_url = base_url

    def auth_headers(self) -> dict[str, str]:
        """Return standard HTTP Authorization Bearer header with this agent's Ed25519 JWT."""
        return {"Authorization": f"Bearer {self.jwt_token}"}

    async def spawn_child(
        self,
        child_id: str,
        requested_role: str,
        requested_scopes: list[str],
        token_budget: int = 2000,
        branch_id: Optional[str] = None,
    ) -> "AegisAgentNode":
        """
        Call POST /api/v1/token/mint with parent's Authorization: Bearer <jwt> header
        and return a newly initialized AegisAgentNode with the child's attenuated JWT.
        """
        target_branch = branch_id or self.branch_id
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/token/mint",
                headers=self.auth_headers(),
                json={
                    "child_agent_id": child_id,
                    "requested_role": requested_role,
                    "requested_scopes": requested_scopes,
                    "token_budget": token_budget,
                    "branch_id": target_branch,
                },
            )
        data = resp.json()
        if resp.status_code != 200:
            raise AegisHTTPException(
                status_code=resp.status_code,
                detail=data.get("detail", str(data)),
                payload=data,
            )
        claims = data["claims"]
        return AegisAgentNode(
            agent_id=child_id,
            role=requested_role,
            scopes=claims["scopes"],
            jwt_token=data["jwt_token"],
            branch_id=claims["branch_id"],
            token_budget=claims["token_budget"],
            parent_id=self.agent_id,
            jti=claims["jti"],
            fastapi_app=self._fastapi_app,
            base_url=self._base_url,
        )

    async def checkpoint(
        self,
        state: dict[str, Any],
        checkpoint_id: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Call POST /api/v1/checkpoint with Bearer header to save clean subtree snapshot."""
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/checkpoint",
                headers=self.auth_headers(),
                json={
                    "branch_id": branch_id or self.branch_id,
                    "current_graph_state": state,
                    "checkpoint_id": checkpoint_id,
                },
            )
        data = resp.json()
        if resp.status_code != 200:
            raise AegisHTTPException(
                status_code=resp.status_code,
                detail=data.get("detail", str(data)),
                payload=data,
            )
        return data

    async def call_tool(
        self,
        tool_name: str,
        trace_id: str,
        raise_on_error: bool = True,
        **tool_args: Any,
    ) -> dict[str, Any]:
        """Call POST /api/v1/tool/intercept with Bearer header."""
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/tool/intercept",
                headers=self.auth_headers(),
                json={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "trace_id": trace_id,
                },
            )
        data = resp.json()
        if resp.status_code != 200 and raise_on_error:
            raise AegisHTTPException(
                status_code=resp.status_code,
                detail=data.get("detail", str(data)),
                payload=data,
            )
        return data

    async def send_a2a(
        self,
        receiver_id: str,
        payload: dict[str, Any],
        trace_id: str,
        schema_name: Optional[str] = None,
        optimistic_pipeline: bool = True,
        raise_on_error: bool = True,
    ) -> dict[str, Any]:
        """Call POST /api/v1/a2a/route with Bearer header."""
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/a2a/route",
                headers=self.auth_headers(),
                json={
                    "receiver_id": receiver_id,
                    "payload": payload,
                    "trace_id": trace_id,
                    "schema_name": schema_name,
                    "optimistic_pipeline": optimistic_pipeline,
                },
            )
        data = resp.json()
        if resp.status_code != 200 and raise_on_error:
            raise AegisHTTPException(
                status_code=resp.status_code,
                detail=data.get("detail", str(data)),
                payload=data,
            )
        return data


class AegisClient:
    """Operator & Root Bootstrap SDK Client for interacting with the AEGIS Control Plane."""

    def __init__(
        self,
        fastapi_app: Optional[FastAPI] = default_fastapi_app,
        base_url: str = "http://aegis-proxy",
    ) -> None:
        self._fastapi_app = fastapi_app
        self._base_url = base_url

    async def bootstrap_root_agent(
        self,
        agent_id: str = "Chief_Treasury_Orchestrator",
        role: str = "L0_Orchestrator",
        scopes: Optional[list[str]] = None,
        token_budget: int = 10000,
        branch_id: str = "Root",
    ) -> AegisAgentNode:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/token/root",
                json={
                    "agent_id": agent_id,
                    "role": role,
                    "scopes": scopes or [],
                    "token_budget": token_budget,
                    "branch_id": branch_id,
                },
            )
        data = resp.json()
        claims = data["claims"]
        return AegisAgentNode(
            agent_id=agent_id,
            role=role,
            scopes=claims["scopes"],
            jwt_token=data["jwt_token"],
            branch_id=claims["branch_id"],
            token_budget=claims["token_budget"],
            parent_id=None,
            jti=claims["jti"],
            fastapi_app=self._fastapi_app,
            base_url=self._base_url,
        )

    async def register_tool_dynamic(
        self,
        tool_name: str,
        required_scope: str,
        mutates_state: bool = False,
        provenance_fields: Optional[list[str]] = None,
        parameter_constraints: Optional[dict[str, Any]] = None,
        fallback_tool_name: Optional[str] = None,
        rate_limit_per_min: int = 60,
        token_cost_per_call: int = 100,
        description: str = "",
        default_response: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/tools/register",
                json={
                    "tool_name": tool_name,
                    "required_scope": required_scope,
                    "mutates_state": mutates_state,
                    "provenance_fields": provenance_fields or [],
                    "parameter_constraints": parameter_constraints or {},
                    "fallback_tool_name": fallback_tool_name,
                    "rate_limit_per_min": rate_limit_per_min,
                    "token_cost_per_call": token_cost_per_call,
                    "description": description,
                    "default_response": default_response,
                },
            )
        return resp.json()

    async def inject_chaos_fault(
        self,
        target: str,
        fault_category: str,
        probability: float = 1.0,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={
                    "target": target,
                    "fault_category": fault_category,
                    "probability": probability,
                    "config": config or {},
                },
            )
        return resp.json()

    async def remove_chaos_fault(
        self,
        target_or_id: str = "ALL",
        clear_agent_status: bool = True,
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                f"/api/v1/chaos/remove?target_or_id={target_or_id}&clear_agent_status={str(clear_agent_status).lower()}"
            )
        return resp.json()

    async def isolate_and_rollback(
        self,
        failed_agent_id: str,
        failure_reason: str,
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/recovery/rollback",
                json={
                    "failed_agent_id": failed_agent_id,
                    "failure_reason": failure_reason,
                },
            )
        return resp.json()

    async def trigger_kill_switch(
        self,
        mode: str,
        target_id: str = "ALL",
        operator_override_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                "/api/v1/killswitch",
                json={
                    "mode": mode,
                    "target_id": target_id,
                    "operator_override_state": operator_override_state,
                },
            )
        return resp.json()

    async def get_telemetry_state(self) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.get("/api/v1/telemetry/state")
        return resp.json()

    async def reset_telemetry(
        self, preserve_registered_tools: bool = True
    ) -> dict[str, Any]:
        async with _build_async_client(
            self._fastapi_app, self._base_url
        ) as client:
            resp = await client.post(
                f"/api/v1/telemetry/reset?preserve_registered_tools={str(preserve_registered_tools).lower()}"
            )
        return resp.json()
