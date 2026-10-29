"""
Corporate Treasury & Cross-Border SWIFT Settlement End-to-End Test Suite.
Verifies all 5 Mandatory Chaos & Kill-Switch Scenarios and target benchmarks:
- Scenario 1: Normal Baseline Execution ($2M USD -> 1,840,000 EUR settled cleanly)
- Scenario 2: Confused Deputy / Privilege Escalation (100% RBAC block rate + surgical respawn)
- Scenario 3: Corrupted Tool Output (10x FX rate = 9.200 caught by Pre-Commit Gate,
  BFS taint set T_fault, surgical rollback to chk_Branch_A_1, R_blast = 1.0, MTTR < 5.0s)
- Scenario 4: Tool Service Unavailable (HTTP 503 failover to Reuters_Fallback)
- Scenario 5: Multi-Tier Kill-Switch & Human Override (SOFT_FREEZE override + GLOBAL_HARD < 50ms)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis_core.kernel import ControlPlaneKernel
from examples.treasury_case_study.scenarios import TreasuryScenarioRunner


@pytest.mark.asyncio
async def test_scenario_1_normal_baseline(tmp_path: Path) -> None:
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    runner = TreasuryScenarioRunner(kernel=kernel)
    res = await runner.run_scenario_1_normal()

    assert res["settlement_eur"] == 1840000.0
    assert res["fx_rate"] == 0.92
    assert res["provider"] == "Bloomberg_Primary"
    assert res["branch_b"]["status"] == "COMPLETED"
    assert res["telemetry"]["metrics"]["blast_radius_ratio"] == 1.0


@pytest.mark.asyncio
async def test_scenario_2_confused_deputy_rbac_and_respawn(tmp_path: Path) -> None:
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    runner = TreasuryScenarioRunner(kernel=kernel)
    res = await runner.run_scenario_2_confused_deputy_rbac()

    assert res["blocked_http_code"] == 403
    assert res["rbac_block_rate"] == 100.0
    assert res["branch_a"]["respawned_agent_id"] == "FX_Quote_Reader_v2"
    assert res["settlement_eur"] == 1840000.0
    assert res["branch_b"]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_scenario_3_corrupted_output_bfs_rollback(tmp_path: Path) -> None:
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    runner = TreasuryScenarioRunner(kernel=kernel)
    res = await runner.run_scenario_3_corrupted_output_rollback()

    assert res["branch_a"]["gate_l4_http_code"] == 409
    assert res["restored_checkpoint_id"] == "chk_Branch_A_1"
    assert set(res["tainted_bfs_nodes"]) == {
        "FX_Quote_Reader",
        "FX_Settlement_Supervisor",
        "SWIFT_Wire_Executor",
    }
    assert res["blast_radius_ratio"] == 1.0
    assert res["mttr_seconds"] < 5.0
    assert res["settlement_eur"] == 1840000.0
    assert res["provider"] == "Reuters_Fallback"
    assert res["branch_b"]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_scenario_4_service_unavailable_failover(tmp_path: Path) -> None:
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    runner = TreasuryScenarioRunner(kernel=kernel)
    res = await runner.run_scenario_4_service_unavailable_failover()

    assert res["primary_http_code"] == 503
    assert res["provider"] == "Reuters_Fallback"
    assert res["settlement_eur"] == 1840000.0
    assert res["mttr_seconds"] < 5.0
    assert res["branch_b"]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_scenario_5_kill_switch_and_human_override(tmp_path: Path) -> None:
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    runner = TreasuryScenarioRunner(kernel=kernel)
    res = await runner.run_scenario_5_kill_switch_and_override()

    part_a = res["part_a"]
    part_b = res["part_b"]

    assert part_a["fx_rate"] == 0.9195
    assert part_a["settlement_eur"] == 1839000.0
    assert part_a["branch_b"]["status"] == "COMPLETED"

    assert part_b["termination_latency_ms"] < 50.0
    assert len(part_b["kill_switch_receipt"]["revoked_jtis"]) >= 7
