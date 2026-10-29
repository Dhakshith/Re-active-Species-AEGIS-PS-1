"""
Automated Verification Suite for `LLMFleet`, `NimBrain`, and `SessionState` (`tests/test_llm_fleet.py`).
Uses `httpx.MockTransport` for deterministic, scriptable NIM responses per test while
executing all actions against a real per-test `ControlPlaneKernel` + `create_aegis_app(kernel)`.

Covers all 6 mandatory requirements:
1. Goal -> SPAWN_CHILD / CALL_TOOL / SEND_A2A / COMPLETE chain executes end-to-end.
2. Out-of-scope action -> real HTTP 403 from C2 -> model receives system-block in context
   and records its next-turn adaptation.
3. Chaos CORRUPTED_OUTPUT on FX feed -> L2/L3/L4 Pre-Commit Gate fails (409) -> taint BFS
   -> isolate_and_rollback -> fallback quote (`fx:quote_reuters`) -> clean settlement.
4. REQUEST_HUMAN -> branch frozen (`SOFT_FREEZE`) -> operator override resume -> `overridden=True`
   in receipt.
5. GLOBAL_HARD kills 5 live fleet tasks in < 50ms.
6. Journal JSONL lines written per turn and kernel event.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegis_core.kernel import ControlPlaneKernel
from aegis_core.server import create_aegis_app
from examples.treasury_case_study.llm_brains import NimBrain
from examples.treasury_case_study.llm_fleet import LLMFleet


def make_scripted_nim_brain(
    scripted_actions: list[dict[str, Any]],
    captured_requests: list[dict[str, Any]] | None = None,
) -> NimBrain:
    """Build a `NimBrain` backed by an `httpx.MockTransport` that yields `scripted_actions` in order."""
    iterator = iter(scripted_actions)

    async def _handler(request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content.decode("utf-8"))
        if captured_requests is not None:
            captured_requests.append(req_body)
        try:
            next_action = next(iterator)
        except StopIteration:
            next_action = {"action": "COMPLETE", "message": "Scripted sequence exhausted."}

        response_payload = {
            "id": "chatcmpl-mock",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(next_action),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(_handler)
    return NimBrain(
        api_key="nvapi-test-mock-key",
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_1_goal_to_spawn_call_message_chain(tmp_path: Path) -> None:
    """(1) Goal -> SPAWN_CHILD -> CALL_TOOL -> SEND_A2A -> CALL_TOOL -> COMPLETE chain executes."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    journal_file = tmp_path / "journal.jsonl"

    script = [
        # Turn 1: Chief_Treasury_Orchestrator spawns a custom L2 reader agent
        {
            "action": "SPAWN_CHILD",
            "target": "Custom_ERP_Inspector",
            "args": {
                "requested_role": "L2_ERP_Inspector",
                "requested_scopes": ["erp:read", "fx:quote"],
                "token_budget": 1500,
                "branch_id": "Branch_A",
            },
            "message": "Spawning dedicated ERP & FX inspector.",
        },
        # Turn 2: Custom_ERP_Inspector calls erp:read
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Reading supplier invoice.",
        },
        # Turn 3: Custom_ERP_Inspector calls fx:quote
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching Bloomberg USD/EUR quote.",
        },
        # Turn 4: Custom_ERP_Inspector sends A2A payload to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Handing off verified quote to SWIFT_Wire_Executor.",
        },
        # Turn 5: SWIFT_Wire_Executor executes swift:wire
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Executing SWIFT MT103 settlement.",
        },
        # Turn 6: Complete
        {
            "action": "COMPLETE",
            "message": "Settled INV-2026-9981 cleanly via spawned Custom_ERP_Inspector and SWIFT_Wire_Executor.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=journal_file,
        feed_mode="deterministic",
    )
    res = await fleet.run_goal("Settle invoice INV-2026-9981 via a custom inspector.")

    assert res["turns_taken"] == 6
    assert "Custom_ERP_Inspector" in fleet.live_agents
    # Verify Human_Operator signed event is in C4 lineage DAG
    assert any(
        edge["sender_id"] == "Human_Operator" for edge in kernel.c4.dag_edges
    )
    # Verify swift:wire turn succeeded with HTTP 200 and non-empty HMAC prefix
    wire_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "swift:wire"
    ]
    assert len(wire_turns) == 1
    assert wire_turns[0]["receipt"]["http_code"] == 200
    assert wire_turns[0]["receipt"]["hmac_prefix"] not in ("NONE", "BLOCKED", "")


@pytest.mark.asyncio
async def test_2_out_of_scope_403_and_model_adaptation(tmp_path: Path) -> None:
    """(2) Out-of-scope action -> real 403 from C2 -> model's next-turn adaptation recorded."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    captured_requests: list[dict[str, Any]] = []

    script = [
        # Turn 1: FX_Quote_Reader (scopes=['erp:read', 'fx:quote']) attempts unauthorized swift:wire -> 403!
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Attempting direct wire from FX_Quote_Reader.",
        },
        # Turn 2: Model sees [SYSTEM_BLOCK HTTP 403] in transcript and adapts by calling authorized erp:read
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Adapting after HTTP 403 RBAC block: reading ERP invoice within permitted scope.",
        },
        # Turn 3: Complete
        {
            "action": "COMPLETE",
            "message": "Adapted to C2 RBAC 403 block and executed authorized erp:read.",
        },
    ]

    brain = make_scripted_nim_brain(script, captured_requests=captured_requests)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )
    res = await fleet.run_goal(
        "Try calling swift:wire from FX_Quote_Reader and adapt if blocked.",
        start_agent_id="FX_Quote_Reader",
    )

    # Verify Turn 1 produced a genuine HTTP 403 block from C2
    block_turns = [t for t in res["transcript"] if t.get("type") == "SYSTEM_BLOCK"]
    assert len(block_turns) == 1
    assert block_turns[0]["receipt"]["http_code"] == 403
    assert kernel.c2.rbac_block_rate == 100.0

    # Verify the model's Turn 2 prompt context included the [SYSTEM_BLOCK HTTP 403] text
    turn_2_user_msg = captured_requests[1]["messages"][1]["content"]
    assert "[SYSTEM_BLOCK HTTP 403]" in turn_2_user_msg

    # Verify Turn 2 adaptation succeeded with HTTP 200
    adapted_turn = res["transcript"][2]
    assert adapted_turn["type"] == "CALL_TOOL"
    assert adapted_turn["target"] == "erp:read"
    assert adapted_turn["receipt"]["http_code"] == 200


@pytest.mark.asyncio
async def test_3_chaos_corrupted_output_taint_rollback_and_fallback(
    tmp_path: Path,
) -> None:
    """
    (3) Chaos CORRUPTED_OUTPUT on FX feed -> Gate L2/L3/L4 fails (409) -> taint BFS
    -> isolate_and_rollback -> fallback quote (`fx:quote_reuters`) succeeds.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Turn 1: FX_Quote_Reader reads ERP
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Reading ERP invoice.",
        },
        # Turn 2: FX_Quote_Reader fetches primary fx:quote
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching primary FX quote.",
        },
        # Turn 3: FX_Quote_Reader sends A2A to FX_Settlement_Supervisor (corrupted in-flight 10x by C6!)
        {
            "action": "SEND_A2A",
            "to": "FX_Settlement_Supervisor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Routing quote to FX_Settlement_Supervisor.",
        },
        # Turn 4: FX_Settlement_Supervisor forwards tainted payload to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 9.2000,
                "settlement_eur": 18400000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Forwarding payload to SWIFT_Wire_Executor.",
        },
        # Turn 5: SWIFT_Wire_Executor attempts swift:wire -> Gate L4 blocks (409) & auto-triggers isolate_and_rollback!
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 9.2000,
                "settlement_eur": 18400000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Attempting SWIFT wire.",
        },
        # Turn 6: Respawned FX_Quote_Reader calls fallback fx:quote_reuters
        {
            "action": "CALL_TOOL",
            "target": "fx:quote_reuters",
            "args": {"amount_usd": 2000000.0},
            "message": "Using Reuters fallback after rollback.",
        },
        # Turn 7: Route clean quote to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Reuters_Fallback",
            },
            "message": "Sending clean Reuters quote to SWIFT_Wire_Executor.",
        },
        # Turn 8: SWIFT_Wire_Executor settles cleanly
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Reuters_Fallback",
            },
            "message": "Settling clean wire via Reuters_Fallback.",
        },
        # Turn 9: Complete
        {
            "action": "COMPLETE",
            "message": "Recovered from corrupted FX output via BFS rollback and settled on Reuters_Fallback.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )
    await fleet.initialize_fleet()

    # Inject 10x CORRUPTED_OUTPUT on FX_Quote_Reader
    await fleet.client.inject_chaos_fault(
        target="FX_Quote_Reader",
        fault_category="CORRUPTED_OUTPUT",
        config={
            "mutations": {
                "fx_rate": {"multiply": 10.0},
                "settlement_eur": {"multiply": 10.0},
            }
        },
    )

    res = await fleet.run_goal(
        "Settle INV-2026-9981 and recover if FX output is corrupted.",
        start_agent_id="FX_Quote_Reader",
    )

    recovery_turns = [
        t for t in res["transcript"] if t.get("type") == "SYSTEM_RECOVERY"
    ]
    assert len(recovery_turns) == 1
    rb = recovery_turns[0]["receipt"]["rollback_report"]
    assert rb["restored_checkpoint_id"] == "chk_Branch_A_1"
    assert set(rb["tainted_bfs_nodes"]) == {
        "FX_Quote_Reader",
        "FX_Settlement_Supervisor",
        "SWIFT_Wire_Executor",
    }
    assert rb["blast_radius_ratio"] == 1.0

    # Verify fallback quote and final wire succeeded
    wire_ok = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL"
        and t.get("target") == "swift:wire"
        and t["receipt"]["http_code"] == 200
    ]
    assert len(wire_ok) == 1
    assert wire_ok[0]["receipt"]["output"]["provider"] == "Reuters_Fallback"


@pytest.mark.asyncio
async def test_4_request_human_freeze_override_resume(tmp_path: Path) -> None:
    """
    (4) REQUEST_HUMAN -> branch frozen (`SOFT_FREEZE`) -> operator override resume
    -> `overridden=True` in receipt.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Turn 1: FX_Quote_Reader fetches quote
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching initial quote.",
        },
        # Turn 2: Request human governor review before wire
        {
            "action": "REQUEST_HUMAN",
            "target": "Branch_A",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Requesting human governor approval for $2M wire.",
        },
        # Turn 3: Route to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9195,
                "settlement_eur": 1839000.0,
            },
            "message": "Forwarding governor-approved parameters.",
        },
        # Turn 4: Execute swift:wire (should receive overridden=True receipt)
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Executing wire with governor override.",
        },
        # Turn 5: Complete
        {
            "action": "COMPLETE",
            "message": "Settled wire with human governor override.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    goal_task = asyncio.create_task(
        fleet.run_goal(
            "Request human approval and execute SWIFT wire.",
            start_agent_id="FX_Quote_Reader",
        )
    )

    # Wait until the REQUEST_HUMAN turn enqueues a pending item and freezes Branch_A
    for _ in range(50):
        if fleet.governor_queue and fleet.governor_queue[0]["status"] == "PENDING":
            break
        await asyncio.sleep(0.01)

    assert len(fleet.governor_queue) == 1
    pending_item = fleet.governor_queue[0]
    assert not kernel.c3.ensure_branch_event("Branch_A").is_set()

    # Operator edits EUR settlement to 1,839,000.0 (fx_rate=0.9195) and resumes Branch_A
    await fleet.resolve_governor_item(
        item_id=pending_item["item_id"],
        operator_override_state={"fx_rate": 0.9195, "settlement_eur": 1839000.0},
    )

    res = await goal_task
    wire_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "swift:wire"
    ]
    assert len(wire_turns) == 1
    assert wire_turns[0]["receipt"]["overridden"] is True
    assert wire_turns[0]["receipt"]["output"]["settlement_eur"] == 1839000.0


@pytest.mark.asyncio
async def test_5_global_hard_kills_five_live_fleet_tasks_under_50ms(
    tmp_path: Path,
) -> None:
    """(5) GLOBAL_HARD kills 5 live fleet tasks in < 50ms."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    brain = make_scripted_nim_brain([])
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
    )

    tasks = await fleet.spawn_background_fleet_tasks(count=5)
    assert len(tasks) == 5
    assert all(not t.done() for t in tasks)

    kill_receipt = await fleet.client.trigger_kill_switch(
        mode="GLOBAL_HARD",
        target_id="ALL",
    )
    await asyncio.gather(*tasks, return_exceptions=True)

    assert kill_receipt["termination_latency_ms"] < 50.0
    assert len(kill_receipt["cancelled_tasks"]) >= 5
    assert all(t.cancelled() or t.done() for t in tasks)


@pytest.mark.asyncio
async def test_6_journal_jsonl_lines_written_per_turn(tmp_path: Path) -> None:
    """(6) Journal JSONL lines written per turn and kernel event."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    journal_file = tmp_path / "journal.jsonl"

    script = [
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Reading invoice.",
        },
        {
            "action": "COMPLETE",
            "message": "Done reading invoice.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=journal_file,
        feed_mode="deterministic",
    )
    await fleet.run_goal(
        "Read invoice INV-2026-9981.", start_agent_id="FX_Quote_Reader"
    )

    assert journal_file.is_file()
    entries = fleet.journal.read_all()
    turn_records = [e for e in entries if e.get("record_type") == "AGENT_TURN"]
    kernel_records = [e for e in entries if e.get("record_type") == "KERNEL_EVENT"]

    # 3 turn records: HUMAN_GOAL (turn 0), CALL_TOOL (turn 1), COMPLETE (turn 2)
    assert len(turn_records) == 3
    assert len(kernel_records) >= 5


@pytest.mark.asyncio
async def test_7_remove_chaos_faults_and_dag_error_rendering(
    tmp_path: Path,
) -> None:
    """(7) Verify chaos faults can be removed individually or in bulk, and Chat errors appear in DAG DOT."""
    from ui.dag_panels import _build_dag_dot

    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    brain = make_scripted_nim_brain(
        [
            {
                "action": "CALL_TOOL",
                "target": "swift:wire",
                "args": {"amount_usd": 2000000.0},
                "message": "Attempting unauthorized wire.",
            },
            {
                "action": "COMPLETE",
                "message": "Finished after block.",
            },
        ]
    )
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
    )
    await fleet.initialize_fleet()

    # Inject two faults and verify both are listed
    await fleet.client.inject_chaos_fault(
        target="fx:quote",
        fault_category="SERVICE_UNAVAILABLE",
    )
    await fleet.client.inject_chaos_fault(
        target="FX_Quote_Reader",
        fault_category="CORRUPTED_OUTPUT",
        config={"mutations": {"fx_rate": {"multiply": 10.0}}},
    )
    assert len(kernel.c6.list_active_rules()) == 2

    # Remove one fault
    await fleet.client.remove_chaos_fault("fx:quote:SERVICE_UNAVAILABLE")
    assert len(kernel.c6.list_active_rules()) == 1

    # Clear all faults
    await fleet.client.remove_chaos_fault("ALL")
    assert len(kernel.c6.list_active_rules()) == 0

    # Run a goal that triggers a 403 block in Chat and verify _build_dag_dot renders the error node
    res = await fleet.run_goal(
        "Test RBAC block visibility in DAG.",
        start_agent_id="FX_Quote_Reader",
    )
    dot_out = _build_dag_dot(
        telemetry=kernel.get_telemetry_state(),
        transcript=res["transcript"],
    )
    assert "RBAC BLOCKED (403)" in dot_out
    assert "chat_err_" in dot_out


@pytest.mark.asyncio
async def test_8_auto_governor_queue_on_wire_and_manual_pause(
    tmp_path: Path,
) -> None:
    """(8) Verify governor queue auto-populates when goal requests approval or when branch is paused."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    # Even if LLM calls swift:wire directly without REQUEST_HUMAN, require_governor_on_wire queues it!
    script = [
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching quote.",
        },
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Forwarding to executor.",
        },
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Calling swift:wire.",
        },
        {
            "action": "COMPLETE",
            "message": "Completed wire after governor override.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
        require_governor_on_wire=True,
    )

    goal_task = asyncio.create_task(
        fleet.run_goal(
            "Settle invoice INV-2026-9981.",
            start_agent_id="FX_Quote_Reader",
        )
    )
    for _ in range(50):
        if fleet.governor_queue and fleet.governor_queue[0]["status"] == "PENDING":
            break
        await asyncio.sleep(0.01)

    assert len(fleet.governor_queue) == 1
    item = fleet.governor_queue[0]
    assert item["proposed_payload"]["settlement_eur"] == 1840000.0

    await fleet.resolve_governor_item(
        item_id=item["item_id"],
        operator_override_state={"settlement_eur": 1835000.0, "fx_rate": 0.9175},
    )
    res = await goal_task
    wire_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "swift:wire"
    ]
    assert len(wire_turns) == 1
    assert wire_turns[0]["receipt"]["overridden"] is True
    assert wire_turns[0]["receipt"]["output"]["settlement_eur"] == 1835000.0


@pytest.mark.asyncio
async def test_9_governor_reject_aborts_goal_with_no_wire_no_hang(
    tmp_path: Path,
) -> None:
    """(a) Governor Reject -> goal aborts with no wire, no hang."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching quote.",
        },
        {
            "action": "REQUEST_HUMAN",
            "target": "Branch_A",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Requesting human governor review.",
        },
        # Should NEVER reach this turn when rejected
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Attempting wire.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    goal_task = asyncio.create_task(
        fleet.run_goal(
            "Request human approval and settle.",
            start_agent_id="FX_Quote_Reader",
        )
    )
    for _ in range(50):
        if fleet.governor_queue and fleet.governor_queue[0]["status"] == "PENDING":
            break
        await asyncio.sleep(0.01)

    assert len(fleet.governor_queue) == 1
    item = fleet.governor_queue[0]

    reject_res = await fleet.resolve_governor_item(
        item_id=item["item_id"],
        operator_override_state=None,
        approved=False,
    )
    assert reject_res["status"] == "REJECTED"

    res = await asyncio.wait_for(goal_task, timeout=3.0)
    assert "[GOVERNOR_REJECTED]" in res["final_summary"]
    wire_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "swift:wire"
    ]
    assert len(wire_turns) == 0


@pytest.mark.asyncio
async def test_10_erp_read_turn_shows_hmac_prefix_none(tmp_path: Path) -> None:
    """(b) erp:read turn shows hmac_prefix == 'NONE' even after fx:quote in the same trace."""
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching quote first.",
        },
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Reading ERP invoice after quote.",
        },
        {
            "action": "COMPLETE",
            "message": "Completed.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    res = await fleet.run_goal(
        "Fetch quote then read ERP.",
        start_agent_id="FX_Quote_Reader",
    )
    fx_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "fx:quote"
    ]
    erp_turns = [
        t
        for t in res["transcript"]
        if t.get("type") == "CALL_TOOL" and t.get("target") == "erp:read"
    ]
    assert len(fx_turns) == 1
    assert fx_turns[0]["receipt"]["hmac_prefix"] != "NONE"
    assert len(erp_turns) == 1
    assert erp_turns[0]["receipt"]["hmac_prefix"] == "NONE"


@pytest.mark.asyncio
async def test_11_second_goal_reports_only_its_own_turns_and_canonical_rbac_preserved(
    tmp_path: Path,
) -> None:
    """
    (c) Second goal in same session reports only its own turns_taken;
    also verifies SPAWN_CHILD targeting FX_Quote_Reader cannot escalate its scopes to swift:wire.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Goal 1 (2 turns): CALL_TOOL erp:read -> COMPLETE
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Goal 1 ERP read.",
        },
        {
            "action": "COMPLETE",
            "message": "Goal 1 complete.",
        },
        # Goal 2 (3 turns): Orchestrator tries SPAWN_CHILD on FX_Quote_Reader with swift:wire,
        # then FX_Quote_Reader tries swift:wire -> MUST be blocked with HTTP 403 -> COMPLETE
        {
            "action": "SPAWN_CHILD",
            "target": "FX_Quote_Reader",
            "args": {
                "requested_role": "FX_Quote_Reader",
                "requested_scopes": ["fx:quote", "swift:wire"],
                "branch_id": "Branch_A",
            },
            "message": "Attempting to spawn FX_Quote_Reader with swift:wire.",
        },
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "FX_Quote_Reader attempting swift:wire.",
        },
        {
            "action": "COMPLETE",
            "message": "Goal 2 complete after 403 block.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    res1 = await fleet.run_goal(
        "Read invoice INV-2026-9981.",
        start_agent_id="FX_Quote_Reader",
    )
    assert res1["turns_taken"] == 2

    res2 = await fleet.run_goal(
        "Test second goal turn count and canonical FX_Quote_Reader scope protection.",
        start_agent_id="Chief_Treasury_Orchestrator",
    )
    # Goal 2 took exactly 3 agent turns (not 2 + 3 + 1 = 6!)
    assert res2["turns_taken"] == 3

    # Verify FX_Quote_Reader was blocked with HTTP 403 on swift:wire despite SPAWN_CHILD attempt
    blocks = [
        t
        for t in res2["transcript"]
        if t.get("type") == "SYSTEM_BLOCK" and t.get("target") == "swift:wire"
    ]
    assert len(blocks) == 1
    assert blocks[0]["receipt"]["http_code"] == 403
    assert fleet.live_agents["FX_Quote_Reader"].scopes == ["erp:read", "fx:quote"]


@pytest.mark.asyncio
async def test_12_swift_wire_executor_self_loop_repaired_and_rollback_checkpoint_saved(
    tmp_path: Path,
) -> None:
    """
    Verifies:
    - SWIFT_Wire_Executor outputting SEND_A2A to itself (self-loop recursion bug) is repaired to
      CALL_TOOL swift:wire instead of spinning on [A2A_SELF_LOOP_IGNORED].
    - After a 409 Pre-Commit Gate block, isolate_and_rollback records a restored C5 checkpoint
      (`chk_Branch_A_restored_1`) and populates `recovery_history`.
    - Post-settlement SEND_A2A is converted to COMPLETE so the run finishes cleanly.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Turn 1: FX_Quote_Reader calls fx:quote
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching primary FX quote.",
        },
        # Turn 2: FX_Quote_Reader routes A2A to SWIFT_Wire_Executor (corrupted 10x in-flight by C6!)
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Routing quote to SWIFT_Wire_Executor.",
        },
        # Turn 3: SWIFT_Wire_Executor tries to SEND_A2A to itself!
        # Must be repaired to CALL_TOOL swift:wire -> triggers Gate L4 409 + isolate_and_rollback!
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 9.2000,
                "settlement_eur": 18400000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Self-loop attempt on SWIFT_Wire_Executor.",
        },
        # Turn 4: After rollback, FX_Quote_Reader fetches clean fallback quote
        {
            "action": "CALL_TOOL",
            "target": "fx:quote_reuters",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching Reuters fallback quote.",
        },
        # Turn 5: FX_Quote_Reader routes clean quote to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "provider": "Reuters_Fallback",
            },
            "message": "Routing clean quote to SWIFT_Wire_Executor.",
        },
        # Turn 6: SWIFT_Wire_Executor again tries SEND_A2A to itself -> repaired to CALL_TOOL swift:wire (200 SETTLED)
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {},
            "message": "Another self-loop attempt on SWIFT_Wire_Executor.",
        },
        # Turn 7: SWIFT_Wire_Executor tries SEND_A2A back to FX_Quote_Reader after settlement -> repaired to COMPLETE!
        {
            "action": "SEND_A2A",
            "to": "FX_Quote_Reader",
            "payload": {},
            "message": "Unnecessary A2A after wire settled.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )
    await fleet.initialize_fleet()
    await fleet.client.inject_chaos_fault(
        target="FX_Quote_Reader",
        fault_category="CORRUPTED_OUTPUT",
        config={
            "mutations": {
                "fx_rate": {"multiply": 10.0},
                "settlement_eur": {"multiply": 10.0},
            }
        },
    )

    res = await fleet.run_goal(
        "Settle INV-2026-9981 and recover from any corruption.",
        start_agent_id="FX_Quote_Reader",
    )

    assert "SWIFT wire settlement completed cleanly" in res["final_summary"]
    assert len(kernel.recovery_history) == 1
    snap_ids = [
        s["checkpoint_id"]
        for s in kernel.get_telemetry_state()["c5_snapshots"]["snapshots"]
    ]
    assert "chk_Branch_A_restored_1" in snap_ids


@pytest.mark.asyncio
async def test_13_interactive_step_snapshots_and_live_step_by_step_mode(
    tmp_path: Path,
) -> None:
    """
    Verifies per-turn `step_snapshots` capture telemetry at each turn for forward/backward
    stepping and that `step_by_step_mode` pauses before each turn until `step_forward()` is called.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Step 1 ERP read.",
        },
        {
            "action": "COMPLETE",
            "message": "Step 2 complete.",
        },
    ]
    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )
    fleet.set_step_by_step_mode(True)

    async def _driver() -> None:
        # Advance Turn 1 and Turn 2 as soon as the fleet pauses at each step gate
        for _ in range(2):
            while not fleet.waiting_for_step:
                await asyncio.sleep(0.01)
            fleet.step_forward()
            await asyncio.sleep(0.02)

    driver_task = asyncio.create_task(_driver())
    res = await fleet.run_goal(
        "Step-by-step test.",
        start_agent_id="FX_Quote_Reader",
    )
    await driver_task

    assert res["turns_taken"] == 2
    # HUMAN_GOAL (step 0) + CALL_TOOL (step 1) + COMPLETE (step 2) = 3 snapshots
    assert len(fleet.step_snapshots) == 3
    assert [s["step_idx"] for s in fleet.step_snapshots] == [0, 1, 2]
    assert "c4_lineage" in fleet.step_snapshots[1]["telemetry"]


@pytest.mark.asyncio
async def test_14_goal_settle_amount_delegates_to_specialist_and_preserves_custom_amount(
    tmp_path: Path,
) -> None:
    """
    Verifies that:
    1. A custom amount ($500,000) specified in the goal is parsed and populated into `latest_context_data`.
    2. When starting at Chief_Treasury_Orchestrator without naming a specialist, the orchestrator
       delegates via SEND_A2A rather than executing swift:wire directly.
    3. The custom amount is preserved in swift:wire execution and conservation holds.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Turn 1: Chief_Treasury_Orchestrator delegates to FX_Quote_Reader
        {
            "action": "SEND_A2A",
            "to": "FX_Quote_Reader",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 500000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 460000.0,
            },
            "message": "Delegating settlement of $500,000 to FX_Quote_Reader.",
        },
        # Turn 2: FX_Quote_Reader fetches quote for $500,000
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 500000.0},
            "message": "Fetching FX quote for $500,000.",
        },
        # Turn 3: FX_Quote_Reader delegates to SWIFT_Wire_Executor
        {
            "action": "SEND_A2A",
            "to": "SWIFT_Wire_Executor",
            "payload": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 500000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 460000.0,
                "provider": "Bloomberg_Primary",
            },
            "message": "Routing $500k payload to SWIFT_Wire_Executor.",
        },
        # Turn 4: SWIFT_Wire_Executor calls swift:wire for $500,000
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 500000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 460000.0,
            },
            "message": "Executing SWIFT MT103 wire for $500,000.",
        },
        # Turn 5: Complete
        {
            "action": "COMPLETE",
            "message": "Settled $500,000 USD cleanly via FX_Quote_Reader and SWIFT_Wire_Executor.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    res = await fleet.run_goal(
        "Settle $500,000 USD to Siemens Energy AG for invoice INV-2026-9981."
    )

    assert fleet.latest_context_data["amount_usd"] == 500000.0
    wire_turns = [
        t for t in res["transcript"] if t.get("type") == "CALL_TOOL" and t.get("target") == "swift:wire"
    ]
    assert len(wire_turns) == 1
    assert wire_turns[0]["receipt"]["output"]["amount_usd"] == 500000.0
    assert wire_turns[0]["receipt"]["output"]["settlement_eur"] == 460000.0


@pytest.mark.asyncio
async def test_15_goal_explicit_worker_mention_routes_directly(
    tmp_path: Path,
) -> None:
    """
    Verifies that mentioning a specialist worker (e.g., 'use fx_quote_reader', 'call the fx_quote_reader')
    automatically routes start_agent_id directly to FX_Quote_Reader.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        {
            "action": "CALL_TOOL",
            "target": "fx:quote",
            "args": {"amount_usd": 2000000.0},
            "message": "Fetching quote directly from FX_Quote_Reader.",
        },
        {
            "action": "COMPLETE",
            "message": "Quote fetched.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )

    res = await fleet.run_goal("use fx_quote_reader and find the fx quote")
    # First agent turn must be FX_Quote_Reader, not Chief_Treasury_Orchestrator
    first_agent_turn = [t for t in res["transcript"] if t.get("turn") == 1][0]
    assert first_agent_turn["agent_id"] == "FX_Quote_Reader"


@pytest.mark.asyncio
async def test_16_surgical_kill_switch_revokes_and_restarts_from_snapshot(
    tmp_path: Path,
) -> None:
    """
    (16) Surgical kill switch mechanism:
    When an agent node fails or is surgically killed:
    - Its token is revoked in C1.
    - Rolled back to the latest snapshot in C5.
    - Rollback report is recorded in kernel.recovery_history.
    - Clean replacement agent is respawned from supervisor with canonical scopes.
    - Status in C3 is set to RESTORED.
    - SYSTEM_RECOVERY turn is recorded in transcript.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)
    brain = make_scripted_nim_brain([])
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
    )
    await fleet.initialize_fleet()

    old_fx_reader = fleet.live_agents["FX_Quote_Reader"]
    old_jti = old_fx_reader.jti

    # Trigger surgical recovery and restart on FX_Quote_Reader
    res = await fleet.recover_and_restart_node(
        failed_agent_id="FX_Quote_Reader",
        reason="Simulated node failure and surgical recovery test",
    )

    # 1. Old token revoked in C1
    assert old_jti in kernel.c1.revocation_set

    # 2. Rollback recorded in recovery_history
    assert len(kernel.recovery_history) >= 1
    latest_rb = kernel.recovery_history[-1]
    assert latest_rb["failed_agent_id"] == "FX_Quote_Reader"
    assert latest_rb["restored_checkpoint_id"] == "chk_Branch_A_1"
    assert latest_rb["blast_radius_ratio"] == 1.0

    # 3. Clean replacement agent respawned in live_agents with new token and canonical scopes
    new_fx_reader = fleet.live_agents["FX_Quote_Reader"]
    assert new_fx_reader.jti != old_jti
    assert new_fx_reader.jti not in kernel.c1.revocation_set
    assert new_fx_reader.scopes == ["erp:read", "fx:quote"]

    # 4. Status in C3 is RESTORED
    assert kernel.c3.process_table["FX_Quote_Reader"]["status"] == "RESTORED"

    # 5. Transcript includes SYSTEM_RECOVERY turn
    recovery_turns = [
        t for t in fleet.transcript if t.get("type") == "SYSTEM_RECOVERY"
    ]
    assert len(recovery_turns) == 1
    assert recovery_turns[0]["receipt"]["rollback_report"]["failed_agent_id"] == "FX_Quote_Reader"


@pytest.mark.asyncio
async def test_17_fault_detected_enqueues_governor_and_resumes_after_user_verification(
    tmp_path: Path,
) -> None:
    """
    (17) Fault detection & human verification:
    When a fault is detected while doing the work and require_governor_on_fault=True:
    - Execution pauses via SOFT_FREEZE.
    - An item is enqueued in the Governor Review Queue with status PENDING.
    - Chat transcript records a REQUEST_HUMAN turn.
    - When the human approves via resolve_governor_item, execution resumes,
      the node is recovered from the snapshot, and completes cleanly.
    """
    kernel = ControlPlaneKernel(db_path=str(tmp_path / "snapshots.db"))
    app = create_aegis_app(kernel)

    script = [
        # Turn 1: FX_Quote_Reader attempts unauthorized swift:wire -> gets blocked by 403
        {
            "action": "CALL_TOOL",
            "target": "swift:wire",
            "args": {
                "invoice_id": "INV-2026-9981",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
            },
            "message": "Attempting unauthorized wire.",
        },
        # Turn 2: After governor verification & surgical recovery, reads ERP
        {
            "action": "CALL_TOOL",
            "target": "erp:read",
            "args": {"invoice_id": "INV-2026-9981"},
            "message": "Reading ERP after clean recovery.",
        },
        # Turn 3: Complete
        {
            "action": "COMPLETE",
            "message": "Workflow completed after operator verification.",
        },
    ]

    brain = make_scripted_nim_brain(script)
    fleet = LLMFleet(
        kernel=kernel,
        app=app,
        brain=brain,
        journal_path=tmp_path / "journal.jsonl",
        feed_mode="deterministic",
        require_governor_on_fault=True,
    )

    goal_task = asyncio.create_task(
        fleet.run_goal(
            "Attempt wire from FX_Quote_Reader",
            start_agent_id="FX_Quote_Reader",
        )
    )

    # Wait until fault is detected and enqueued in governor queue
    for _ in range(50):
        if fleet.governor_queue and fleet.governor_queue[0]["status"] == "PENDING":
            break
        await asyncio.sleep(0.02)

    assert len(fleet.governor_queue) == 1
    pending_item = fleet.governor_queue[0]
    assert pending_item["status"] == "PENDING"
    assert "RBAC Unauthorized Tool Block" in pending_item["message"]

    # Verify Chat transcript has REQUEST_HUMAN turn
    req_human_turns = [
        t for t in fleet.transcript if t.get("type") == "REQUEST_HUMAN"
    ]
    assert len(req_human_turns) == 1
    assert "Fault detected" in req_human_turns[0]["content"]

    # Verify Branch_A is frozen
    assert not kernel.c3.ensure_branch_event("Branch_A").is_set()

    # Operator approves recovery
    await fleet.resolve_governor_item(
        item_id=pending_item["item_id"],
        approved=True,
    )

    res = await goal_task
    # Verify execution resumed, node was recovered from snapshot, and goal completed
    assert res.get("final_summary") or len(res.get("transcript", [])) > 0
    assert any(t.get("type") == "GOVERNOR_RESUMED" for t in res["transcript"])
    assert any(t.get("type") == "SYSTEM_RECOVERY" for t in res["transcript"])
    assert len(kernel.recovery_history) >= 1

