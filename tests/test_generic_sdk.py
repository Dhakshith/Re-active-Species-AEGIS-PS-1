"""
Domain-Agnostic Out-of-Band Control Plane & Plug-and-Play SDK Verification Suite.
Verifies:
1. Zero hardcoded Treasury/financial domain symbols inside `aegis_core/`.
2. Real HTTP `Authorization: Bearer <JWT>` validation, missing/invalid token rejection (401),
   and cryptographic parent->child scope attenuation (403).
3. Plug-and-play onboarding of a completely non-financial domain (Cloud Kubernetes DevOps:
   `k8s:read_pods` and `k8s:scale_deployment`) with generic `provenance_fields`,
   `parameter_constraints`, `@aegis_gate_l1`, `@aegis_gate_l3`, and A2A provenance verification.
4. Dynamic runtime tool registration via `POST /api/v1/tools/register`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from pydantic import BaseModel, Field

from aegis_core.kernel import ControlPlaneKernel
from aegis_core.server import create_aegis_app
from aegis_sdk.client import AegisAgentNode, AegisClient, AegisHTTPException
from aegis_sdk.decorators import aegis_gate_l1, aegis_gate_l3, aegis_tool


def test_aegis_core_is_100_percent_domain_agnostic() -> None:
    """Ensure `aegis_core/` has zero hardcoded references to Treasury/FX/SWIFT domain terms."""
    core_dir = Path(__file__).resolve().parent.parent / "aegis_core"
    forbidden_terms = ["fx_rate", "swift:wire", "erp:read", "Bloomberg", "Reuters"]
    for py_file in core_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in content, (
                f"Domain leak detected: '{term}' found in {py_file.name}. "
                "aegis_core must remain 100% domain-agnostic!"
            )


@pytest.mark.asyncio
async def test_http_bearer_jwt_auth_and_privilege_attenuation() -> None:
    """Verify HTTP Bearer token enforcement, 401 rejection on missing/tampered JWT, and 403 scope attenuation."""
    kernel = ControlPlaneKernel()
    kernel.reset_state(preserve_registered_tools=False)
    app = create_aegis_app(kernel)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://aegis-proxy") as http:
        # 1. Calling a protected endpoint without Authorization header -> 401
        unauth_resp = await http.post(
            "/api/v1/tool/intercept",
            json={"tool_name": "k8s:read_pods", "tool_args": {}, "trace_id": "t1"},
        )
        assert unauth_resp.status_code == 401

        # 2. Calling a protected endpoint with tampered Bearer JWT -> 401
        bad_resp = await http.post(
            "/api/v1/tool/intercept",
            json={"tool_name": "k8s:read_pods", "tool_args": {}, "trace_id": "t2"},
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert bad_resp.status_code == 401

    # 3. Mint root DevOps orchestrator and test child scope escalation -> 403
    client = AegisClient(fastapi_app=app)
    root_node = await client.bootstrap_root_agent(
        agent_id="DevOps_Root_Orchestrator",
        role="L0_DevOps_Orchestrator",
        scopes=["k8s:read_pods", "k8s:scale_deployment"],
        token_budget=5000,
        branch_id="Cluster_Prod",
    )

    # Child requests a scope (`db:drop_production`) not in parent's scopes -> 403
    with pytest.raises(AegisHTTPException) as exc_info:
        await root_node.spawn_child(
            child_id="Rogue_Pod_Scaler",
            requested_role="L2_Pod_Scaler",
            requested_scopes=["k8s:read_pods", "db:drop_production"],
            token_budget=2000,
            branch_id="Cluster_Prod",
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_generic_cloud_devops_domain_plug_and_play() -> None:
    """
    Onboard a non-financial Cloud Kubernetes DevOps domain using `@aegis_tool`,
    `@aegis_gate_l1`, `@aegis_gate_l3`, `provenance_fields`, and `parameter_constraints`.
    """
    kernel = ControlPlaneKernel()
    kernel.reset_state(preserve_registered_tools=False)
    app = create_aegis_app(kernel)

    @aegis_tool(
        tool_name="k8s:read_pods",
        required_scope="k8s:read_pods",
        mutates_state=False,
        provenance_fields=["active_pods", "cpu_load"],
        rate_limit_per_min=60,
        kernel=kernel,
    )
    def read_k8s_pods(namespace: str = "production") -> dict[str, Any]:
        return {
            "namespace": namespace,
            "active_pods": 6,
            "cpu_load": 0.78,
        }

    @aegis_gate_l1(schema_name="ScaleDeploymentSchema", kernel=kernel)
    class ScaleDeploymentSchema(BaseModel):
        namespace: str
        active_pods: int = Field(ge=1, le=50)
        cpu_load: float = Field(ge=0.0, le=1.0)

    @aegis_gate_l3(invariant_name="k8s_autoscaling_invariant", kernel=kernel)
    def verify_autoscaling_invariant(tool_args: dict[str, Any]) -> tuple[bool, str]:
        if "cpu_load" not in tool_args or "target_replicas" not in tool_args:
            return True, "Not applicable"
        cpu_load = float(tool_args["cpu_load"])
        active_pods = int(tool_args["active_pods"])
        target_replicas = int(tool_args["target_replicas"])
        if cpu_load > 0.70 and target_replicas < active_pods:
            return (
                False,
                f"Autoscaling invariant violated: cannot scale down ({target_replicas} < {active_pods}) "
                f"when cpu_load={cpu_load:.2f} > 0.70",
            )
        return True, "Autoscaling invariant satisfied"

    @aegis_tool(
        tool_name="k8s:scale_deployment",
        required_scope="k8s:scale_deployment",
        mutates_state=True,
        provenance_fields=["active_pods", "cpu_load"],
        parameter_constraints={"target_replicas": {"min": 1, "max": 20}},
        rate_limit_per_min=30,
        kernel=kernel,
    )
    def scale_k8s_deployment(
        namespace: str,
        active_pods: int,
        cpu_load: float,
        target_replicas: int,
    ) -> dict[str, Any]:
        return {
            "namespace": namespace,
            "previous_pods": active_pods,
            "scaled_to_replicas": target_replicas,
            "cpu_load": cpu_load,
            "status": "SCALED_SUCCESSFULLY",
        }

    client = AegisClient(fastapi_app=app)
    root = await client.bootstrap_root_agent(
        agent_id="SRE_Orchestrator",
        role="L0_SRE_Orchestrator",
        scopes=["k8s:read_pods", "k8s:scale_deployment"],
        token_budget=8000,
        branch_id="Cluster_EU",
    )
    metrics_reader = await root.spawn_child(
        child_id="Pod_Metrics_Reader",
        requested_role="L2_Metrics_Reader",
        requested_scopes=["k8s:read_pods"],
        token_budget=2000,
        branch_id="Cluster_EU",
    )
    autoscaler = await root.spawn_child(
        child_id="Deployment_Autoscaler",
        requested_role="L2_Autoscaler",
        requested_scopes=["k8s:scale_deployment"],
        token_budget=3000,
        branch_id="Cluster_EU",
    )
    assert isinstance(metrics_reader, AegisAgentNode)
    assert isinstance(autoscaler, AegisAgentNode)

    # 1. Read cluster metrics -> records provenance hashes on `active_pods` and `cpu_load`
    metrics_resp = await metrics_reader.call_tool(
        "k8s:read_pods",
        trace_id="trace_k8s_1",
        namespace="payments-prod",
    )
    pod_data = metrics_resp["data"]
    assert pod_data["active_pods"] == 6
    assert pod_data["cpu_load"] == 0.78

    # 2. Route metrics via A2A to Autoscaler with Gate L1 schema validation
    a2a_resp = await metrics_reader.send_a2a(
        receiver_id="Deployment_Autoscaler",
        payload=pod_data,
        trace_id="trace_k8s_1",
        schema_name="ScaleDeploymentSchema",
    )
    assert a2a_resp["status"] == "DELIVERED"

    # 3. Valid scale operation passes all 4 Pre-Commit Gates (L1-L4)
    valid_scale = await autoscaler.call_tool(
        "k8s:scale_deployment",
        trace_id="trace_k8s_1",
        namespace="payments-prod",
        active_pods=6,
        cpu_load=0.78,
        target_replicas=10,
    )
    assert valid_scale["status"] == "SUCCESS"
    assert valid_scale["data"]["scaled_to_replicas"] == 10

    # 4. Test Gate L2 Provenance Verification: tamper with `cpu_load` -> blocked (409 Pre-Commit Gate)
    with pytest.raises(AegisHTTPException) as exc_l2:
        await autoscaler.call_tool(
            "k8s:scale_deployment",
            trace_id="trace_k8s_1",
            namespace="payments-prod",
            active_pods=6,
            cpu_load=0.99,  # Tampered!
            target_replicas=12,
        )
    assert exc_l2.value.status_code == 409
    assert "Provenance" in exc_l2.value.detail or "mismatch" in exc_l2.value.detail

    # Clear taint to test Gate L3 invariant in isolation
    kernel.c4.taint_status.clear()
    kernel.c4.taint_reasons.clear()

    # 5. Test Gate L3 Domain Invariant: scale down during high load -> blocked (409 Pre-Commit Gate)
    with pytest.raises(AegisHTTPException) as exc_l3:
        await autoscaler.call_tool(
            "k8s:scale_deployment",
            trace_id="trace_k8s_1",
            namespace="payments-prod",
            active_pods=6,
            cpu_load=0.78,
            target_replicas=2,  # Violates L3 invariant!
        )
    assert exc_l3.value.status_code == 409
    assert "Autoscaling invariant violated" in exc_l3.value.detail

    # Clear taint to test Parameter Constraints in isolation
    kernel.c4.taint_status.clear()
    kernel.c4.taint_reasons.clear()

    # 6. Test Parameter Constraints: target_replicas=35 > max(20) -> blocked (409 Pre-Commit Gate)
    with pytest.raises(AegisHTTPException) as exc_bounds:
        await autoscaler.call_tool(
            "k8s:scale_deployment",
            trace_id="trace_k8s_1",
            namespace="payments-prod",
            active_pods=6,
            cpu_load=0.78,
            target_replicas=35,
        )
    assert exc_bounds.value.status_code == 409
    assert "target_replicas" in exc_bounds.value.detail


@pytest.mark.asyncio
async def test_dynamic_http_tool_registration_and_agent_execution() -> None:
    """Verify dynamic onboarding via `POST /api/v1/tools/register` and `POST /api/v1/token/root`."""
    kernel = ControlPlaneKernel()
    kernel.reset_state(preserve_registered_tools=False)
    app = create_aegis_app(kernel)
    client = AegisClient(fastapi_app=app)

    reg_resp = await client.register_tool_dynamic(
        tool_name="secops:quarantine_host",
        required_scope="secops:quarantine_host",
        mutates_state=True,
        provenance_fields=["host_ip"],
        parameter_constraints={"severity_score": {"min": 1.0, "max": 10.0}},
        rate_limit_per_min=20,
    )
    assert reg_resp["status"] == "REGISTERED"

    secops_agent = await client.bootstrap_root_agent(
        agent_id="SecOps_Responder_01",
        role="L1_SecOps_Responder",
        scopes=["secops:quarantine_host"],
        token_budget=3000,
        branch_id="SecOps_Branch",
    )

    # Record provenance receipt for `host_ip` in trace_secops_1
    kernel.c4.record_provenance_receipt(
        trace_id="trace_secops_1",
        tool_name="secops:scan_host",
        tool_output={"host_ip": "10.24.0.88"},
        provenance_fields=["host_ip"],
    )

    exec_resp = await secops_agent.call_tool(
        "secops:quarantine_host",
        trace_id="trace_secops_1",
        host_ip="10.24.0.88",
        severity_score=8.5,
    )
    assert exec_resp["status"] == "SUCCESS"
    assert exec_resp["tool_name"] == "secops:quarantine_host"
