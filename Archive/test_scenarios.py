"""Automated pytest verification suite for AEGIS 2026 Problem Statement 01 (Scenarios 1–5)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import pytest

from agents.treasury_graph import TreasuryWorkflowRunner
from control_plane.methods import ControlPlaneKernel
from control_plane.server import app


@pytest.mark.asyncio
async def test_scenario_1_normal_execution_and_delegation() -> None:
    """
    Scenario 1: Normal Execution & Cryptographic Delegation.
    Verifies L0 -> L2 token delegation, privilege attenuation invariant,
    clean checkpoints chk_Branch_A_1 and chk_Branch_B_1, and $1,840,000 EUR settlement.
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_1()

    assert result["settlement_eur"] == 1_840_000.0
    assert result["fx_rate"] == 0.9200
    assert result["provider"] == "bloomberg_primary"
    assert result["branch_a"]["checkpoint_id"] == "chk_Branch_A_1"
    assert result["branch_b"]["checkpoint_id"] == "chk_Branch_B_1"
    assert kernel.C5.get_sql_snapshot_count() >= 2

    # All 7 agents must have status == "COMPLETED" and zero taint
    for agent_id, info in kernel.C3.process_table.items():
        assert info["status"] == "COMPLETED", f"Agent {agent_id} not COMPLETED"
        assert kernel.C4.taint_status[agent_id] is False

    # Verify Privilege Attenuation Invariant (S_child <= S_parent)
    root_jwt = kernel.bootstrap_root_token()
    sup_b_jwt = kernel.mint_delegated_token(
        parent_jwt=root_jwt,
        child_agent_id="Compliance_Supervisor",
        requested_role="L1_Compliance",
        requested_scopes=["kyc:check", "audit:log"],
        token_budget=2000,
        branch_id="Branch_B",
    )
    with pytest.raises(PermissionError, match="PRIVILEGE_ESCALATION_BLOCKED"):
        kernel.mint_delegated_token(
            parent_jwt=sup_b_jwt,
            child_agent_id="Rogue_Child",
            requested_role="Rogue",
            requested_scopes=["swift:wire"],
            token_budget=500,
            branch_id="Branch_B",
        )


@pytest.mark.asyncio
async def test_scenario_2_confused_deputy_rbac_escalation_block() -> None:
    """
    Scenario 2: Confused Deputy / RBAC Privilege Escalation Block.
    Verifies HTTP 403 block, 100% RBAC block rate, surgical kill & JTI revocation
    of compromised FX_Quote_Reader, clean worker respawn, and 0% Branch B disruption.
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_2()

    assert result["blocked_http_code"] == 403
    assert result["rbac_block_rate"] == 100.0
    assert result["initial_jti_revoked"] is True
    assert result["initial_fx_reader_jti"] in kernel.C1.revocation_set
    assert result["settlement_eur"] == 1_840_000.0

    # Branch B must have completed with 0% disruption
    for b_agent in ("Compliance_Supervisor", "Sanctions_Screener", "Audit_Logger"):
        assert kernel.C3.process_table[b_agent]["status"] == "COMPLETED"
        assert kernel.C4.taint_status[b_agent] is False


@pytest.mark.asyncio
async def test_scenario_3_corrupted_fx_bfs_taint_and_subtree_rollback() -> None:
    """
    Scenario 3: Corrupted FX Output, Causal BFS Taint & Surgical Subtree Rollback.
    Verifies Gates L2/L3/L4, exact causal BFS cone T_fault across Branch A,
    R_blast == 1.0 (100% > 90% target), MTTR < 5.0s, and Reuters fallback settlement (1,841,000 EUR).
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_3()
    report = result["recovery_report"]

    expected_t_fault = {
        "FX_Quote_Reader",
        "FX_Settlement_Supervisor",
        "SWIFT_Wire_Executor",
    }
    assert set(report["tainted_bfs_nodes"]) == expected_t_fault

    # Zero Branch B nodes in T_fault
    branch_b_nodes = {"Compliance_Supervisor", "Sanctions_Screener", "Audit_Logger"}
    assert set(report["tainted_bfs_nodes"]).isdisjoint(branch_b_nodes)

    assert report["blast_radius_ratio"] == 1.0
    assert report["mttr_seconds"] < 5.0
    assert report["restored_checkpoint_id"] == "chk_Branch_A_1"

    # All 3 tainted JTIs must be in C1.revocation_set
    assert len(report["revoked_jtis"]) == 3
    for jti in report["revoked_jtis"]:
        assert jti in kernel.C1.revocation_set

    # Final settlement uses reuters_fallback (fx_rate = 0.9205, settlement_eur = 1841000.0)
    assert result["fx_rate"] == 0.9205
    assert result["settlement_eur"] == 1_841_000.0
    assert result["provider"] == "reuters_fallback"


@pytest.mark.asyncio
async def test_scenario_4_infrastructure_outage_503_and_automatic_failover() -> None:
    """
    Scenario 4: Infrastructure Outage (HTTP 503) & Automatic Failover.
    Verifies HTTP 503 on Bloomberg primary and automatic failover to Reuters fallback with MTTR < 5.0s.
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_4()

    first_fx_step = result["branch_a"]["events"][1]["response"]
    assert first_fx_step["http_code"] == 503
    assert result["mttr_seconds"] < 5.0
    assert result["fx_rate"] == 0.9205
    assert result["settlement_eur"] == 1_841_000.0
    assert result["provider"] == "reuters_fallback"


@pytest.mark.asyncio
async def test_scenario_5_kill_switch_sub_50ms_and_soft_freeze_override() -> None:
    """
    Scenario 5: Emergency Hard Kill-Switch (<50ms) & Soft-Freeze Human Override.
    Verifies Part A (SOFT_FREEZE pause, C5 snapshot inspection, human override application)
    and Part B (GLOBAL_HARD termination_latency_ms < 50.0, all JTIs revoked, all tasks KILLED).
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_5()

    # Part A assertions: SOFT_FREEZE & Human Operator Override
    part_a = result["part_a_soft_freeze"]
    assert part_a["freeze_receipt"]["inspect_snapshot"] is not None
    assert part_a["fx_rate"] == 0.9195
    assert part_a["settlement_eur"] == 1_839_000.0

    # Part B assertions: GLOBAL_HARD Kill-Switch < 50ms
    part_b = result["part_b_global_hard"]
    assert part_b["termination_latency_ms"] < 50.0
    assert part_b["all_jtis_revoked"] is True
    assert part_b["all_agents_killed"] is True

    for agent_id, info in kernel.C3.process_table.items():
        assert info["status"] == "KILLED", f"Agent {agent_id} was not KILLED"
        assert info["jti"] in kernel.C1.revocation_set


@pytest.mark.asyncio
async def test_tainted_checkpoint_rejection_and_runaway_loop_429() -> None:
    """Verify tainted agents cannot overwrite clean checkpoints and RUNAWAY_LOOP triggers HTTP 429."""
    kernel = ControlPlaneKernel()
    root_jwt = kernel.bootstrap_root_token()
    w1_jwt = kernel.mint_delegated_token(
        parent_jwt=root_jwt,
        child_agent_id="FX_Quote_Reader",
        requested_role="Worker",
        requested_scopes=["erp:read", "fx:quote"],
        token_budget=1000,
        branch_id="Branch_A",
    )

    # Mark FX_Quote_Reader tainted and verify create_checkpoint refuses
    kernel.C4.taint_status["FX_Quote_Reader"] = True
    with pytest.raises(RuntimeError, match="CANNOT_CHECKPOINT_TAINTED_STATE"):
        kernel.create_checkpoint(w1_jwt, "Branch_A", {"state": "poisoned"})

    # Reset taint and test RUNAWAY_LOOP fault rule -> HTTP 429 + surgical kill
    kernel.C4.taint_status["FX_Quote_Reader"] = False
    kernel.inject_chaos_fault("FX_Quote_Reader", "RUNAWAY_LOOP")
    resp = await kernel.intercept_tool_call(
        agent_jwt=w1_jwt,
        tool_name="erp:read",
        tool_args={},
        trace_id="tr_loop_test",
    )
    assert resp["http_code"] == 429
    assert resp["status"] == "BUDGET_EXCEEDED_429"
    assert kernel.C3.process_table["FX_Quote_Reader"]["status"] == "KILLED"


@pytest.mark.asyncio
async def test_scenario_6_simultaneous_multi_agent_and_multi_tool_chaos_storm() -> None:
    """
    Scenario 6: Simultaneous Multi-Agent & Multi-Tool Chaos Storm.
    Verifies the system survives simultaneous failures across multiple agents in both
    Branch A and Branch B (PRIVILEGE_ESCALATION, CORRUPTED_OUTPUT, RUNAWAY_LOOP) AND
    simultaneous outages across multiple tools (erp:read, fx:quote, kyc:check, audit:log).
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    result = await runner.run_scenario_6()

    assert result["faults_injected_count"] == 10
    assert result["agent_respawns_count"] >= 6
    assert result["tool_failovers_count"] >= 4
    assert result["subtree_rollbacks_count"] == 2
    assert result["mttr_seconds"] < 5.0
    assert result["fx_rate"] == 0.9205
    assert result["settlement_eur"] == 1_841_000.0
    assert result["provider"] == "reuters_fallback"
    assert kernel.C2.rbac_block_rate == 100.0

    # Both Branch A and Branch B rollbacks must maintain 100% cross-branch blast radius containment
    for report in kernel.recovery_history:
        assert report["blast_radius_ratio"] == 1.0

    # All 7 agents must finish COMPLETED with zero residual taint
    for agent_id, info in kernel.C3.process_table.items():
        assert info["status"] == "COMPLETED", f"Agent {agent_id} not COMPLETED"
        assert kernel.C4.taint_status[agent_id] is False


@pytest.mark.asyncio
async def test_multi_target_surgical_kill_switch() -> None:
    """Verify SURGICAL kill-switch can terminate multiple agents across branches simultaneously."""
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)
    runner._bootstrap_and_delegate_hierarchy()

    receipt = await kernel.trigger_kill_switch(
        mode="SURGICAL",
        target_id="FX_Quote_Reader,Sanctions_Screener",
    )
    assert receipt["termination_latency_ms"] < 50.0
    assert set(receipt["killed_agents"]) == {"FX_Quote_Reader", "Sanctions_Screener"}
    assert kernel.C3.process_table["FX_Quote_Reader"]["status"] == "KILLED"
    assert kernel.C3.process_table["Sanctions_Screener"]["status"] == "KILLED"
    assert kernel.C3.process_table["SWIFT_Wire_Executor"]["status"] == "READY"


@pytest.mark.asyncio
async def test_multi_hop_tertiary_failover_and_supervisor_recovery() -> None:
    """
    Verify 3-tier tool failover (Primary -> Secondary -> Tertiary when both Primary & Secondary fail)
    alongside simultaneous L1 Supervisor + L2 Worker + All-5-Tools failures.
    """
    kernel = ControlPlaneKernel()
    runner = TreasuryWorkflowRunner(kernel=kernel)

    kernel.inject_multi_chaos_faults(
        [
            {"target": "FX_Settlement_Supervisor", "fault_category": "PRIVILEGE_ESCALATION"},
            {"target": "Compliance_Supervisor", "fault_category": "RUNAWAY_LOOP"},
            {
                "target": "fx:quote",
                "fault_category": "SERVICE_UNAVAILABLE",
                "fault_config": {"max_triggers": 2},
            },
            {"target": "swift:wire", "fault_category": "SERVICE_UNAVAILABLE"},
            {"target": "erp:read", "fault_category": "SERVICE_UNAVAILABLE"},
            {"target": "kyc:check", "fault_category": "SERVICE_UNAVAILABLE"},
            {"target": "audit:log", "fault_category": "SERVICE_UNAVAILABLE"},
        ]
    )

    result = await runner.run_custom_live_workflow()

    # Both primary (bloomberg_primary) and secondary (reuters_fallback) failed, so tertiary (ecb_reference_backup @ 0.9202) settled
    assert result["provider"] == "ecb_reference_backup"
    assert result["fx_rate"] == 0.9202
    assert result["settlement_eur"] == 1_840_400.0
    assert result["branch_a"]["final_wire_receipt"]["wire_network"] == "sepa_target2_fallback"
    assert len(kernel.failover_events) >= 6
    assert len(kernel.agent_respawn_events) >= 2


@pytest.mark.asyncio
async def test_fastapi_endpoints_end_to_end() -> None:
    """Verify FastAPI REST server endpoints for Scenarios 1–6, batch chaos injection, and state telemetry."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for scenario_id in (1, 2, 3, 4, 5, 6):
            r = await client.post(f"/api/v1/workflow/run/{scenario_id}")
            assert r.status_code == 200
            payload = r.json()
            assert payload["scenario_id"] == scenario_id

        batch_resp = await client.post(
            "/api/v1/chaos/inject-batch",
            json={
                "faults": [
                    {"target": "FX_Quote_Reader", "fault_category": "PRIVILEGE_ESCALATION"},
                    {"target": "erp:read", "fault_category": "SERVICE_UNAVAILABLE"},
                ]
            },
        )
        assert batch_resp.status_code == 200
        assert batch_resp.json()["count"] == 2

        state_resp = await client.get("/api/v1/state")
        assert state_resp.status_code == 200
        state_data = state_resp.json()
        assert "metrics" in state_data
        assert "C1_iam_engine" in state_data
        assert "C6_chaos_monkey" in state_data
