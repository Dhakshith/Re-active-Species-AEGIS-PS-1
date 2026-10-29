"""
Live Multi-Agent LLM Fleet Runner (`LLMFleet`) for AEGIS-CONTROL.
Drives hierarchical agents turn-by-turn using `NimBrain`, routes all actions
(`CALL_TOOL`, `SPAWN_CHILD`, `SEND_A2A`, `REQUEST_HUMAN`, `COMPLETE`) through
the real FastAPI Control Plane (`Authorization: Bearer <Ed25519 JWT>`), handles
`AegisHTTPException` blocks (`403` RBAC, `409` Pre-Commit Gate + automatic
`isolate_and_rollback`, `503` Service Unavailable), manages the `SOFT_FREEZE`
Human Governor review queue, and writes a JSONL audit journal per turn.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from fastapi import FastAPI
import httpx

from aegis_core.kernel import ControlPlaneKernel
from aegis_core.server import create_aegis_app
from aegis_sdk.client import AegisAgentNode, AegisClient, AegisHTTPException
from examples.treasury_case_study.agents import (
    TreasuryAgentFleet,
    build_treasury_agent_fleet,
)
from examples.treasury_case_study.llm_brains import (
    NimBrain,
    is_conversational_message,
)
from examples.treasury_case_study.tools_and_gates import (
    MASTER_TREASURY_SCOPES,
    register_treasury_plugin,
)


_GOVERNOR_GOAL_KEYWORDS = (
    "governor",
    "human",
    "approval",
    "approve",
    "review",
    "override",
    "request_human",
    "soft_freeze",
    "freeze",
)

CANONICAL_AGENT_SCOPES: dict[str, list[str]] = {
    "Chief_Treasury_Orchestrator": [
        "erp:read",
        "fx:quote",
        "swift:wire",
        "kyc:check",
        "audit:log",
    ],
    "FX_Settlement_Supervisor": ["erp:read", "fx:quote", "swift:wire"],
    "FX_Quote_Reader": ["erp:read", "fx:quote"],
    "SWIFT_Wire_Executor": ["swift:wire"],
    "Compliance_Supervisor": ["kyc:check", "audit:log"],
    "Sanctions_Screener": ["kyc:check"],
    "Audit_Logger": ["audit:log"],
}


def _goal_requests_governor(goal: str) -> bool:
    g_low = (goal or "").lower()
    return any(kw in g_low for kw in _GOVERNOR_GOAL_KEYWORDS)


def extract_amount_from_goal(goal: str) -> Optional[float]:
    """
    Extract requested USD settlement amount if specified in user goal.
    Supports formats:
      - $500,000 | $500000 | 500,000 USD | 500000 usd
      - $1.5M | 1.5M USD | 1.5 million USD
      - $500k | 500k USD | 500 thousand
      - amount of 750000 | amount: 750000
    """
    if not goal:
        return None
    g = goal.replace(",", "")
    # Match $1.5M, 1.5M USD, 1.5 million
    m_mil = re.search(r"(?:\$|\b)(\d+(?:\.\d+)?)\s*(?:m|million)\b", g, re.I)
    if m_mil:
        return float(m_mil.group(1)) * 1_000_000.0
    # Match $500k, 500k USD
    m_k = re.search(r"(?:\$|\b)(\d+(?:\.\d+)?)\s*(?:k|thousand)\b", g, re.I)
    if m_k:
        return float(m_k.group(1)) * 1_000.0
    # Match $500000, 500000 USD
    m_val = re.search(r"(?:\$|\bamount\s*(?:of|:)?\s*)(\d+(?:\.\d+)?)\b", g, re.I)
    if m_val:
        return float(m_val.group(1))
    m_usd = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:usd|dollars|eur)\b", g, re.I)
    if m_usd:
        return float(m_usd.group(1))
    return None


def resolve_start_agent(goal: str, default_agent_id: str = "Chief_Treasury_Orchestrator") -> str:
    """
    Check if the user goal specifically directs an existing specialist worker
    (e.g., FX_Quote_Reader, SWIFT_Wire_Executor, Sanctions_Screener, Audit_Logger) to act directly.
    Multi-step goals or end-to-end treasury workflows always start at Chief_Treasury_Orchestrator.
    """
    if not goal:
        return default_agent_id
    goal_lower = goal.lower().strip()

    # Multi-step orchestration workflows with routing must start at Chief_Treasury_Orchestrator
    has_routing_or_workflow = "route to" in goal_lower or ("read" in goal_lower and "settle" in goal_lower)
    if has_routing_or_workflow:
        return default_agent_id

    for candidate_id in (
        "FX_Quote_Reader",
        "SWIFT_Wire_Executor",
        "Sanctions_Screener",
        "Audit_Logger",
        "FX_Settlement_Supervisor",
        "Compliance_Supervisor",
    ):
        cand_lower = candidate_id.lower()
        cand_spaced = cand_lower.replace("_", " ")
        pattern = rf"\b(?:use|using|call|ask|tell|let|have|from|as|with|via|contact)\s+(?:the\s+)?(?:agent\s+)?({re.escape(cand_lower)}|{re.escape(cand_spaced)})\b"
        if re.search(pattern, goal_lower):
            return candidate_id
    return default_agent_id


class JournalWriter:
    """Append-only JSONL journal writer recording every agent turn and kernel event."""

    def __init__(self, journal_path: Path | str) -> None:
        self.path = Path(journal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_entry(self, entry: dict[str, Any]) -> None:
        record = {
            "ts": time.time(),
            **entry,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries


class LLMFleet:
    """
    Live NVIDIA NIM Multi-Agent Fleet governed out-of-band by `ControlPlaneKernel`.
    """

    def __init__(
        self,
        kernel: ControlPlaneKernel,
        app: Optional[FastAPI] = None,
        brain: Optional[NimBrain] = None,
        journal_path: Optional[Path | str] = None,
        feed_mode: str = "live",
        on_turn_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        require_governor_on_wire: bool = False,
        governor_timeout_sec: float = 600.0,
        require_governor_on_fault: Optional[bool] = None,
    ) -> None:
        self.kernel = kernel
        register_treasury_plugin(self.kernel)
        self.app = app or create_aegis_app(self.kernel)
        self.client = AegisClient(fastapi_app=self.app)
        self.brain = brain or NimBrain()
        self.feed_mode = feed_mode
        self.on_turn_callback = on_turn_callback
        self.require_governor_on_wire = require_governor_on_wire
        self.governor_timeout_sec = float(governor_timeout_sec)
        # Default require_governor_on_fault to True when running in live mode (no MockTransport), False for unit tests
        if require_governor_on_fault is None:
            self.require_governor_on_fault = getattr(self.brain, "transport", None) is None
        else:
            self.require_governor_on_fault = bool(require_governor_on_fault)

        default_journal = Path("runs") / f"session_{uuid4().hex[:8]}" / "journal.jsonl"
        self.journal = JournalWriter(journal_path or default_journal)

        self.fleet: Optional[TreasuryAgentFleet] = None
        self.human_operator: Optional[AegisAgentNode] = None
        self.live_agents: dict[str, AegisAgentNode] = {}
        self.transcript: list[dict[str, Any]] = []
        self.step_snapshots: list[dict[str, Any]] = []
        self.step_by_step_mode: bool = False
        self.waiting_for_step: bool = False
        self._step_event: asyncio.Event = asyncio.Event()
        self._step_event.set()
        self.governor_queue: list[dict[str, Any]] = []
        self._governor_events: dict[str, asyncio.Event] = {}
        self._active_tasks: list[asyncio.Task[Any]] = []
        self._last_logged_event_idx: int = 0
        self.latest_context_data: dict[str, Any] = {
            "invoice_id": "INV-2026-9981",
            "supplier": "Siemens Energy AG",
            "iban": "DE89370400440532013000",
            "amount_usd": 2000000.0,
            "fx_rate": 0.9200,
            "settlement_eur": 1840000.0,
            "provider": "Bloomberg_Primary",
        }
        self.is_running: bool = False

    def set_step_by_step_mode(self, enabled: bool) -> None:
        """Toggle interactive live step-by-step execution mode."""
        self.step_by_step_mode = bool(enabled)
        if not self.step_by_step_mode:
            self._step_event.set()

    def step_forward(self) -> None:
        """Advance one turn when paused in live step-by-step execution mode."""
        self._step_event.set()

    async def initialize_fleet(self) -> TreasuryAgentFleet:
        """Bootstrap `Human_Operator` and the 7-agent hierarchical Treasury fleet."""
        if self.fleet is not None and self.live_agents:
            return self.fleet

        # Mint signed Human_Operator passport so user goals appear as signed lineage events
        self.human_operator = await self.client.bootstrap_root_agent(
            agent_id="Human_Operator",
            role="Human_Governor",
            scopes=MASTER_TREASURY_SCOPES,
            token_budget=50000,
            branch_id="Root",
        )
        self.fleet = await build_treasury_agent_fleet(self.app)
        self.live_agents = dict(self.fleet.as_dict())

        # Take initial clean branch checkpoints so C5 has baseline state for rollbacks & diffs
        await self.fleet.sup_a.checkpoint(
            state={
                "branch_id": "Branch_A",
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "iban": "DE89370400440532013000",
                "amount_usd": 2000000.0,
                "fx_rate": 0.9200,
                "settlement_eur": 1840000.0,
                "stage": "INITIAL_CLEAN_CHECKPOINT",
            },
            checkpoint_id="chk_Branch_A_1",
        )
        await self.fleet.sup_b.checkpoint(
            state={
                "branch_id": "Branch_B",
                "invoice_id": "INV-2026-9981",
                "supplier": "Siemens Energy AG",
                "stage": "INITIAL_CLEAN_CHECKPOINT",
            },
            checkpoint_id="chk_Branch_B_1",
        )
        self._flush_kernel_events_to_journal()
        return self.fleet

    def _flush_kernel_events_to_journal(self) -> None:
        events = self.kernel.event_log
        while self._last_logged_event_idx < len(events):
            ev = events[self._last_logged_event_idx]
            self._last_logged_event_idx += 1
            self.journal.write_entry({"record_type": "KERNEL_EVENT", "event": ev})

    def _record_turn(self, turn_entry: dict[str, Any]) -> None:
        step_idx = len(self.transcript)
        turn_entry["step_idx"] = step_idx
        self.transcript.append(turn_entry)
        self.step_snapshots.append(
            {
                "step_idx": step_idx,
                "turn": copy.deepcopy(turn_entry),
                "telemetry": copy.deepcopy(self.kernel.get_telemetry_state()),
            }
        )
        self.journal.write_entry({"record_type": "AGENT_TURN", "turn": turn_entry})
        self._flush_kernel_events_to_journal()
        if self.on_turn_callback is not None:
            self.on_turn_callback(turn_entry)

    async def spawn_background_fleet_tasks(self, count: int = 5) -> list[asyncio.Task[Any]]:
        """
        Launch `count` live asyncio tasks holding open `httpx.AsyncClient` sockets
        registered in `C3_Task_Registry` (used during live execution or kill-switch drills).
        """
        await self.initialize_fleet()
        agents_list = list(self.live_agents.values())[:count]

        async def _worker_loop(node: AegisAgentNode) -> None:
            sock = httpx.AsyncClient()
            if node.agent_id in self.kernel.c3.process_table:
                proc = self.kernel.c3.process_table[node.agent_id]
                proc["task_handle"] = asyncio.current_task()
                proc["http_client"] = sock
                proc["status"] = "IN_FLIGHT_TOOL"
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                raise
            finally:
                if not sock.is_closed:
                    await sock.aclose()

        tasks = [asyncio.create_task(_worker_loop(a)) for a in agents_list]
        self._active_tasks.extend(tasks)
        await asyncio.sleep(0.01)
        return tasks

    async def enqueue_governor_review(
        self,
        branch_id: str = "Branch_A",
        agent_id: str = "Chief_Treasury_Orchestrator",
        trace_id: Optional[str] = None,
        proposed_payload: Optional[dict[str, Any]] = None,
        message: str = "Human governor review requested before settlement commit.",
    ) -> tuple[dict[str, Any], asyncio.Event]:
        """
        Pause `branch_id` via `SOFT_FREEZE` and enqueue (or return existing pending)
        review item in `self.governor_queue`.
        """
        for existing in self.governor_queue:
            if existing.get("status") == "PENDING" and existing.get("branch_id") == branch_id:
                if proposed_payload:
                    existing["proposed_payload"] = {
                        **self.latest_context_data,
                        **existing.get("proposed_payload", {}),
                        **proposed_payload,
                    }
                ev = self._governor_events.get(existing["item_id"])
                if ev is None:
                    ev = asyncio.Event()
                    self._governor_events[existing["item_id"]] = ev
                return existing, ev

        eff_trace = trace_id or f"trace_gov_{uuid4().hex[:8]}"
        merged_payload = {
            **self.latest_context_data,
            **(proposed_payload or {}),
        }
        # Ensure conservation holds in the proposed display
        if "amount_usd" in merged_payload and "fx_rate" in merged_payload and "settlement_eur" not in (proposed_payload or {}):
            merged_payload["settlement_eur"] = round(
                float(merged_payload["amount_usd"]) * float(merged_payload["fx_rate"]),
                2,
            )

        freeze_receipt = await self.client.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id=branch_id,
            operator_override_state=None,
        )
        chk_snap = self.kernel.c5.get_latest_checkpoint(branch_id)
        item_id = f"gov_{uuid4().hex[:8]}"
        resume_ev = asyncio.Event()
        self._governor_events[item_id] = resume_ev

        gov_item = {
            "item_id": item_id,
            "branch_id": branch_id,
            "agent_id": agent_id,
            "trace_id": eff_trace,
            "proposed_payload": merged_payload,
            "checkpoint_snapshot": chk_snap,
            "message": message,
            "status": "PENDING",
            "created_at": time.time(),
            "freeze_receipt": freeze_receipt,
        }
        self.governor_queue.append(gov_item)
        self._flush_kernel_events_to_journal()
        return gov_item, resume_ev

    async def resolve_governor_item(
        self,
        item_id: str,
        operator_override_state: Optional[dict[str, Any]] = None,
        approved: bool = True,
    ) -> dict[str, Any]:
        """
        Called by the Human Governor UI (or test) to resume a `SOFT_FREEZE` branch
        with optional `operator_override_state` parameter edits.
        """
        target_item: Optional[dict[str, Any]] = None
        for item in self.governor_queue:
            if item["item_id"] == item_id:
                target_item = item
                break
        if target_item is None:
            raise ValueError(f"Governor queue item '{item_id}' not found")

        branch_id = target_item["branch_id"]
        trace_id = target_item["trace_id"]

        if not approved:
            receipt = await self.client.trigger_kill_switch(
                mode="SOFT_FREEZE",
                target_id=branch_id,
                operator_override_state=None,
            )
            target_item["status"] = "REJECTED"
            target_item["operator_override_state"] = None
            target_item["resolved_at"] = time.time()
            target_item["kill_switch_receipt"] = receipt

            ev = self._governor_events.get(item_id)
            if ev is not None:
                ev.set()

            self._flush_kernel_events_to_journal()
            return target_item

        override = dict(operator_override_state or {})

        proposed = dict(target_item.get("proposed_payload") or {})
        amount_usd = float(override.get("amount_usd", proposed.get("amount_usd", 2000000.0)))
        if "settlement_eur" in override and "fx_rate" not in override and amount_usd > 0:
            override["fx_rate"] = round(float(override["settlement_eur"]) / amount_usd, 6)
        elif "fx_rate" in override and "settlement_eur" not in override:
            override["settlement_eur"] = round(amount_usd * float(override["fx_rate"]), 2)

        if override:
            self.latest_context_data.update(override)
            self.kernel.c4.update_provenance_on_override(override, trace_id=trace_id)
            self.kernel.c4.update_provenance_on_override(override)

        resume_override = dict(override) if override else {"resumed_by": "Human_Governor"}
        receipt = await self.client.trigger_kill_switch(
            mode="SOFT_FREEZE",
            target_id=branch_id,
            operator_override_state=resume_override,
        )

        target_item["status"] = "APPROVED_OVERRIDDEN" if override else "APPROVED"
        target_item["operator_override_state"] = override
        target_item["resolved_at"] = time.time()
        target_item["kill_switch_receipt"] = receipt

        ev = self._governor_events.get(item_id)
        if ev is not None:
            ev.set()

        self._flush_kernel_events_to_journal()
        return target_item

    async def _recover_from_precommit_gate_block(
        self,
        current_node: AegisAgentNode,
        trace_id: str,
        exc: AegisHTTPException,
    ) -> dict[str, Any]:
        """
        Execute automatic `isolate_and_rollback` when Gate L2/L3/L4 blocks a state-mutating tool,
        and respawn clean replacement nodes for the tainted subtree so the fleet continues live.
        """
        failed_origin = "FX_Quote_Reader"
        for edge in self.kernel.c4.dag_edges:
            if (
                edge.get("trace_id") == trace_id
                and edge.get("sender_id") not in ("Human_Operator", "Chief_Treasury_Orchestrator")
            ):
                failed_origin = edge["sender_id"]
                break
        if failed_origin not in self.kernel.c3.process_table:
            failed_origin = current_node.agent_id

        rollback_report = await self.client.isolate_and_rollback(
            failed_agent_id=failed_origin,
            failure_reason=exc.detail,
        )

        assert self.fleet is not None
        root_node = self.live_agents.get("Chief_Treasury_Orchestrator", self.fleet.root)
        tainted_set = set(rollback_report.get("tainted_bfs_nodes", []))

        if "FX_Settlement_Supervisor" in tainted_set:
            sup_a_clean = await root_node.spawn_child(
                child_id="FX_Settlement_Supervisor",
                requested_role="L1_FX_Settlement_Supervisor_Restored",
                requested_scopes=["erp:read", "fx:quote", "swift:wire"],
                token_budget=4000,
                branch_id="Branch_A",
            )
            self.live_agents["FX_Settlement_Supervisor"] = sup_a_clean
        else:
            sup_a_clean = self.live_agents["FX_Settlement_Supervisor"]

        if "FX_Quote_Reader" in tainted_set:
            fx_clean = await sup_a_clean.spawn_child(
                child_id="FX_Quote_Reader",
                requested_role="L2_FX_Quote_Reader_Restored",
                requested_scopes=["erp:read", "fx:quote"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            self.live_agents["FX_Quote_Reader"] = fx_clean

        if "SWIFT_Wire_Executor" in tainted_set:
            swift_clean = await sup_a_clean.spawn_child(
                child_id="SWIFT_Wire_Executor",
                requested_role="L2_SWIFT_Wire_Executor_Restored",
                requested_scopes=["swift:wire"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            self.live_agents["SWIFT_Wire_Executor"] = swift_clean

        # Record a post-rollback restored checkpoint in C5 so the Snapshots tab displays
        # both the baseline checkpoint and the newly restored post-recovery state.
        restored_state = dict(rollback_report.get("restored_state") or {})
        restored_state.update(
            {
                "branch_id": "Branch_A",
                "stage": "POST_ROLLBACK_RESTORED",
                "restored_from": rollback_report.get("restored_checkpoint_id", "chk_Branch_A_1"),
                "isolated_nodes": list(rollback_report.get("tainted_bfs_nodes", [])),
                "blast_radius_ratio": rollback_report.get("blast_radius_ratio", 1.0),
            }
        )
        restored_chk_id = f"chk_Branch_A_restored_{len(self.kernel.recovery_history)}"
        await sup_a_clean.checkpoint(
            state=restored_state,
            checkpoint_id=restored_chk_id,
        )
        rollback_report["post_rollback_checkpoint_id"] = restored_chk_id

        return rollback_report

    async def recover_and_restart_node(
        self,
        failed_agent_id: str,
        reason: str = "Surgical kill-switch invoked",
    ) -> dict[str, Any]:
        """
        Surgically revoke a failed/compromised node in C1, roll back its branch
        to the latest clean C5 snapshot, record the rollback report in C4/C5/kernel recovery history,
        and restart/respawn a clean replacement agent with canonical scopes from the snapshot.
        """
        await self.initialize_fleet()
        t0 = time.perf_counter()

        # 1. Execute surgical kill in control plane
        kill_receipt = await self.client.trigger_kill_switch(
            mode="SURGICAL",
            target_id=failed_agent_id,
        )

        # 2. Determine branch and latest snapshot
        proc = self.kernel.c3.process_table.get(failed_agent_id, {})
        branch_id = proc.get("branch_id", "Branch_A")
        latest_chk = self.kernel.c5.get_latest_checkpoint(branch_id)
        if not latest_chk:
            latest_chk = self.kernel.c5.get_latest_checkpoint("Branch_A") or {
                "checkpoint_id": "chk_Branch_A_1",
                "state_blob": {
                    "branch_id": "Branch_A",
                    "invoice_id": "INV-2026-9981",
                    "supplier": "Siemens Energy AG",
                    "iban": "DE89370400440532013000",
                    "amount_usd": 2000000.0,
                    "fx_rate": 0.9200,
                    "settlement_eur": 1840000.0,
                },
            }

        # 3. Build rollback report and mention it in recovery_history
        revoked_jtis = list(kill_receipt.get("revoked_jtis", []))
        rb_report = {
            "failed_agent_id": failed_agent_id,
            "failure_reason": reason,
            "branch_id": branch_id,
            "tainted_bfs_nodes": [failed_agent_id],
            "revoked_jtis": revoked_jtis,
            "restored_checkpoint_id": (
                latest_chk["checkpoint_id"] if latest_chk else "chk_Branch_A_1"
            ),
            "restored_state": dict(
                latest_chk.get("state_blob", {}) if latest_chk else {}
            ),
            "fallback_tool_map": {
                spec.tool_name: spec.fallback_tool_name
                for spec in self.kernel.c2.registered_tools.values()
                if spec.fallback_tool_name
            },
            "blast_radius_ratio": 1.0,
            "healthy_branch_disrupted_count": 0,
            "mttr_seconds": round(time.perf_counter() - t0, 4),
            "timestamp": time.time(),
        }
        self.kernel.recovery_history.append(rb_report)
        self.kernel.last_blast_radius_ratio = 1.0
        self.kernel.last_mttr_seconds = rb_report["mttr_seconds"]

        # 4. Clear taint in C4 and deactivate any C6 target rules
        self.kernel.c4.taint_status[failed_agent_id] = False
        self.kernel.c4.taint_reasons.pop(failed_agent_id, None)
        self.kernel.c6.deactivate_target_rules(failed_agent_id)

        # 5. Respawn clean replacement agent with canonical scopes from its supervisor
        assert self.fleet is not None
        root_node = self.live_agents.get("Chief_Treasury_Orchestrator", self.fleet.root)
        clean_node = None

        if failed_agent_id == "FX_Quote_Reader":
            sup_a = self.live_agents.get("FX_Settlement_Supervisor", self.fleet.sup_a)
            clean_node = await sup_a.spawn_child(
                child_id="FX_Quote_Reader",
                requested_role="L2_FX_Quote_Reader_Restored",
                requested_scopes=["erp:read", "fx:quote"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            self.live_agents["FX_Quote_Reader"] = clean_node
        elif failed_agent_id == "SWIFT_Wire_Executor":
            sup_a = self.live_agents.get("FX_Settlement_Supervisor", self.fleet.sup_a)
            clean_node = await sup_a.spawn_child(
                child_id="SWIFT_Wire_Executor",
                requested_role="L2_SWIFT_Wire_Executor_Restored",
                requested_scopes=["swift:wire"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            self.live_agents["SWIFT_Wire_Executor"] = clean_node
        elif failed_agent_id == "FX_Settlement_Supervisor":
            clean_node = await root_node.spawn_child(
                child_id="FX_Settlement_Supervisor",
                requested_role="L1_FX_Settlement_Supervisor_Restored",
                requested_scopes=["erp:read", "fx:quote", "swift:wire"],
                token_budget=4000,
                branch_id="Branch_A",
            )
            self.live_agents["FX_Settlement_Supervisor"] = clean_node
            fx_c = await clean_node.spawn_child(
                child_id="FX_Quote_Reader",
                requested_role="L2_FX_Quote_Reader_Restored",
                requested_scopes=["erp:read", "fx:quote"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            sw_c = await clean_node.spawn_child(
                child_id="SWIFT_Wire_Executor",
                requested_role="L2_SWIFT_Wire_Executor_Restored",
                requested_scopes=["swift:wire"],
                token_budget=1800,
                branch_id="Branch_A",
            )
            self.live_agents["FX_Quote_Reader"] = fx_c
            self.live_agents["SWIFT_Wire_Executor"] = sw_c
        elif failed_agent_id == "Sanctions_Screener":
            sup_b = self.live_agents.get("Compliance_Supervisor", self.fleet.sup_b)
            clean_node = await sup_b.spawn_child(
                child_id="Sanctions_Screener",
                requested_role="L2_Sanctions_Screener_Restored",
                requested_scopes=["kyc:check"],
                token_budget=1800,
                branch_id="Branch_B",
            )
            self.live_agents["Sanctions_Screener"] = clean_node
        elif failed_agent_id == "Audit_Logger":
            sup_b = self.live_agents.get("Compliance_Supervisor", self.fleet.sup_b)
            clean_node = await sup_b.spawn_child(
                child_id="Audit_Logger",
                requested_role="L2_Audit_Logger_Restored",
                requested_scopes=["audit:log"],
                token_budget=1800,
                branch_id="Branch_B",
            )
            self.live_agents["Audit_Logger"] = clean_node
        elif failed_agent_id == "Compliance_Supervisor":
            clean_node = await root_node.spawn_child(
                child_id="Compliance_Supervisor",
                requested_role="L1_Compliance_Supervisor_Restored",
                requested_scopes=["kyc:check", "audit:log"],
                token_budget=4000,
                branch_id="Branch_B",
            )
            self.live_agents["Compliance_Supervisor"] = clean_node
            sanc_c = await clean_node.spawn_child(
                child_id="Sanctions_Screener",
                requested_role="L2_Sanctions_Screener_Restored",
                requested_scopes=["kyc:check"],
                token_budget=1800,
                branch_id="Branch_B",
            )
            aud_c = await clean_node.spawn_child(
                child_id="Audit_Logger",
                requested_role="L2_Audit_Logger_Restored",
                requested_scopes=["audit:log"],
                token_budget=1800,
                branch_id="Branch_B",
            )
            self.live_agents["Sanctions_Screener"] = sanc_c
            self.live_agents["Audit_Logger"] = aud_c
        else:
            parent_id = proc.get("parent_id", "Chief_Treasury_Orchestrator")
            parent_node = self.live_agents.get(parent_id, root_node)
            scopes = CANONICAL_AGENT_SCOPES.get(
                failed_agent_id,
                [s for s in parent_node.scopes if s != "swift:wire"],
            )
            clean_node = await parent_node.spawn_child(
                child_id=failed_agent_id,
                requested_role=f"L2_{failed_agent_id}_Restored",
                requested_scopes=scopes,
                token_budget=1800,
                branch_id=branch_id,
            )
            self.live_agents[failed_agent_id] = clean_node

        if failed_agent_id in self.kernel.c3.process_table:
            self.kernel.c3.process_table[failed_agent_id]["status"] = "RESTORED"

        # 6. Save post-rollback restored checkpoint in C5
        if clean_node is not None:
            restored_state = dict(
                latest_chk.get("state_blob", {}) if latest_chk else {}
            )
            restored_state.update(
                {
                    "branch_id": branch_id,
                    "stage": "POST_SURGICAL_RESTORED",
                    "restored_from": (
                        latest_chk["checkpoint_id"] if latest_chk else "chk_Branch_A_1"
                    ),
                    "failed_agent_id": failed_agent_id,
                    "restored_ts": time.time(),
                }
            )
            restored_chk_id = (
                f"chk_{branch_id}_restored_{len(self.kernel.recovery_history)}"
            )
            await clean_node.checkpoint(
                state=restored_state,
                checkpoint_id=restored_chk_id,
            )
            rb_report["post_rollback_checkpoint_id"] = restored_chk_id

        # 7. Record SYSTEM_RECOVERY turn in transcript
        self._record_turn(
            {
                "turn": len(self.transcript),
                "type": "SYSTEM_RECOVERY",
                "agent_id": "AEGIS_Control_Plane",
                "role": "Control_Plane_Kernel",
                "branch_id": branch_id,
                "action": "SURGICAL_ROLLBACK_AND_RESPAWN",
                "target": (
                    latest_chk["checkpoint_id"] if latest_chk else "chk_Branch_A_1"
                ),
                "content": (
                    f"[SYSTEM_RECOVERY] Node `{failed_agent_id}` revoked in C1 after failure ({reason}). "
                    f"Rolled back branch `{branch_id}` to latest snapshot `{latest_chk['checkpoint_id'] if latest_chk else 'chk_Branch_A_1'}`. "
                    f"Respawned clean agent `{failed_agent_id}` with canonical scopes {CANONICAL_AGENT_SCOPES.get(failed_agent_id, [])}."
                ),
                "receipt": {
                    "trace_id": f"trace_recovery_{uuid4().hex[:8]}",
                    "tool": "killswitch:surgical_restore",
                    "http_code": 200,
                    "hmac_prefix": "RESTORED_OK",
                    "overridden": False,
                    "rollback_report": rb_report,
                },
            }
        )
        self._flush_kernel_events_to_journal()
        return {
            "kill_receipt": kill_receipt,
            "rollback_report": rb_report,
            "clean_node": clean_node,
        }

    async def run_goal(
        self,
        goal: str,
        max_turns: int = 24,
        start_agent_id: str = "Chief_Treasury_Orchestrator",
    ) -> dict[str, Any]:
        """
        Drive the multi-agent fleet turn-by-turn from a natural-language `goal`
        (capped at `max_turns`).
        """
        await self.initialize_fleet()
        assert self.human_operator is not None

        self.is_running = True
        start_len = len(self.transcript)
        trace_id = f"trace_{uuid4().hex[:8]}"
        governor_resolved_in_run = False

        # If start_agent_id was left at default Chief_Treasury_Orchestrator, but the user's goal
        # explicitly asks an existing specialist worker (like FX_Quote_Reader) to attempt an action first,
        # route directly to that worker so it cannot be bypassed by L0 master scopes.
        if start_agent_id == "Chief_Treasury_Orchestrator":
            start_agent_id = resolve_start_agent(goal, default_agent_id=start_agent_id)

        # Parse custom requested amount if provided in the user goal
        extracted_amt = extract_amount_from_goal(goal)
        if extracted_amt is not None and extracted_amt > 0:
            self.latest_context_data["amount_usd"] = extracted_amt
            fx_val = float(self.latest_context_data.get("fx_rate", 0.92))
            self.latest_context_data["settlement_eur"] = round(extracted_amt * fx_val, 2)

        # Parse invoice ID if explicitly provided in the goal
        m_inv = re.search(r"\b(INV-\d{4}-\d+)\b", goal or "", re.I)
        if m_inv:
            self.latest_context_data["invoice_id"] = m_inv.group(1).upper()

        # 1. Record signed Human_Operator prompt in C4 Lineage Graph & transcript
        await self.human_operator.send_a2a(
            receiver_id=start_agent_id,
            payload={"goal": goal, "issued_by": "Human_Operator"},
            trace_id=trace_id,
            optimistic_pipeline=False,
        )
        self._record_turn(
            {
                "turn": 0,
                "type": "HUMAN_GOAL",
                "agent_id": "Human_Operator",
                "role": "Human_Governor",
                "branch_id": "Root",
                "content": goal,
                "receipt": {
                    "trace_id": trace_id,
                    "tool": "a2a:human_prompt",
                    "http_code": 200,
                    "hmac_prefix": "SIGNED_ED25519",
                    "overridden": False,
                },
            }
        )

        current_node = self.live_agents.get(
            start_agent_id, self.live_agents["Chief_Treasury_Orchestrator"]
        )
        final_summary = ""
        is_conv = is_conversational_message(goal) and self.brain.transport is None
        wire_settled_in_run = False
        last_wire_output: dict[str, Any] = {}
        rollback_in_run = False
        last_a2a_payload: dict[str, Any] = {}
        consecutive_self_loops = 0

        try:
            effective_max_turns = 1 if is_conv else max_turns
            for turn_idx in range(1, effective_max_turns + 1):
                if self.step_by_step_mode:
                    self._step_event.clear()
                    self.waiting_for_step = True
                    try:
                        await asyncio.wait_for(
                            self._step_event.wait(), timeout=self.governor_timeout_sec
                        )
                    except asyncio.TimeoutError:
                        self.step_by_step_mode = False
                    finally:
                        self.waiting_for_step = False

                current_node = self.live_agents.get(current_node.agent_id, current_node)

                decision = await self.brain.decide(
                    node=current_node,
                    kernel=self.kernel,
                    transcript=self.transcript,
                    goal=goal,
                    available_agents=list(self.live_agents.keys()),
                )

                # If user sent a simple greeting like "hi" and LLM tried to call a tool instead of replying conversationally:
                if is_conv and decision["action"] != "COMPLETE":
                    reg_tools_list = ", ".join(f"`{k}`" for k in self.kernel.c2.registered_tools.keys())
                    agents_list = ", ".join(f"`{k}`" for k in self.live_agents.keys() if k != "Human_Operator")
                    decision = {
                        "action": "COMPLETE",
                        "target": "COMPLETE",
                        "message": (
                            f"Hello! I am `{current_node.agent_id}` ({current_node.role}), governed by the "
                            f"AEGIS-CONTROL Out-of-Band Control Plane.\n\n"
                            f"- **Active Fleet Agents:** {agents_list}\n"
                            f"- **Registered Tools:** {reg_tools_list}\n\n"
                            f"How can I help you today? You can ask me to read an invoice from ERP (`INV-2026-9981`), "
                            f"fetch a live USD/EUR FX quote, run KYC screening, or execute a governed SWIFT settlement."
                        ),
                    }

                # 0. In live mode, ensure Chief_Treasury_Orchestrator delegates settlement/quote tasks
                # to specialist workers rather than executing leaf tools directly.
                if (
                    self.brain.transport is None
                    and current_node.agent_id == "Chief_Treasury_Orchestrator"
                    and decision["action"] == "CALL_TOOL"
                    and str(decision.get("target") or "") in ("swift:wire", "fx:quote", "erp:read")
                    and "chief_treasury_orchestrator" not in (goal or "").lower()
                ):
                    decision = {
                        "action": "SEND_A2A",
                        "to": "FX_Quote_Reader",
                        "target": "FX_Quote_Reader",
                        "payload": dict(self.latest_context_data),
                        "message": "Delegating treasury settlement operation to specialist agent `FX_Quote_Reader` on Branch_A as required by hierarchical governance.",
                    }

                # Anti-recursion & completion guards:
                # 1. Once swift:wire has settled in this run, prevent post-settlement A2A loops and complete cleanly.
                elif wire_settled_in_run and decision["action"] != "COMPLETE" and (
                    self.brain.transport is None or decision["action"] == "SEND_A2A"
                ):
                    decision = {
                        "action": "COMPLETE",
                        "target": "COMPLETE",
                        "message": (
                            f"SWIFT wire settlement completed cleanly: UETR `{last_wire_output.get('uetr', 'SETTLED')}`, "
                            f"invoice `{last_wire_output.get('invoice_id', 'INV-2026-9981')}`, "
                            f"FX rate `{last_wire_output.get('fx_rate')}`, "
                            f"EUR `{last_wire_output.get('settlement_eur')}` via `{last_wire_output.get('provider')}`."
                        ),
                    }

                # 2. Prevent SWIFT_Wire_Executor from looping on SEND_A2A (to itself or others) or calling out-of-scope tools
                elif current_node.agent_id == "SWIFT_Wire_Executor" and not wire_settled_in_run:
                    if decision["action"] == "SEND_A2A" or (
                        self.brain.transport is None
                        and decision["action"] == "CALL_TOOL"
                        and str(decision.get("target") or "") != "swift:wire"
                    ):
                        wire_args = dict(self.latest_context_data)
                        if last_a2a_payload:
                            wire_args.update(last_a2a_payload)
                        if isinstance(decision.get("payload"), dict):
                            for k_p in ("invoice_id", "supplier", "iban", "amount_usd", "fx_rate", "settlement_eur", "provider"):
                                if k_p in decision["payload"]:
                                    wire_args[k_p] = decision["payload"][k_p]
                        decision = {
                            "action": "CALL_TOOL",
                            "target": "swift:wire",
                            "args": wire_args,
                            "message": (
                                decision.get("message")
                                or "Executing SWIFT MT103 wire settlement with routed payload."
                            ),
                        }

                # 3. In live mode, ensure FX_Quote_Reader has a provenance receipt in the current trace before delegating via SEND_A2A,
                #    and never self-loops SEND_A2A to FX_Quote_Reader.
                elif self.brain.transport is None and current_node.agent_id == "FX_Quote_Reader":
                    if decision["action"] == "SEND_A2A":
                        dest = str(decision.get("to") or decision.get("target") or "")
                        if trace_id not in self.kernel.c4.provenance_receipts:
                            quote_tool = "fx:quote_reuters" if rollback_in_run else "fx:quote"
                            decision = {
                                "action": "CALL_TOOL",
                                "target": quote_tool,
                                "args": {"amount_usd": self.latest_context_data.get("amount_usd", 2000000.0)},
                                "message": f"Fetching fresh quote via `{quote_tool}` for current trace before routing to `SWIFT_Wire_Executor`.",
                            }
                        elif dest == "FX_Quote_Reader":
                            decision = {
                                **decision,
                                "to": "SWIFT_Wire_Executor",
                                "target": "SWIFT_Wire_Executor",
                                "payload": dict(self.latest_context_data),
                            }

                action = decision["action"]
                target = str(decision.get("target") or "")
                to_agent = str(decision.get("to") or target or "")
                args = dict(decision.get("args") or {})
                payload = dict(decision.get("payload") or args or {})
                message = str(decision.get("message") or "")

                # Guard against FX_Quote_Reader looping erp:read after BOTH erp:read and quote have already executed
                erp_read_done = any(
                    t.get("type") == "CALL_TOOL" and t.get("target") == "erp:read"
                    for t in self.transcript
                )
                fx_quote_done = any(
                    t.get("type") == "CALL_TOOL" and t.get("target") in ("fx:quote", "fx:quote_reuters")
                    for t in self.transcript
                )
                if (
                    self.brain.transport is None
                    and current_node.agent_id == "FX_Quote_Reader"
                    and action == "CALL_TOOL"
                    and (target == "erp:read" or args.get("tool_name") == "erp:read")
                    and erp_read_done
                    and fx_quote_done
                ):
                    action = "SEND_A2A"
                    next_worker = (
                        "Sanctions_Screener"
                        if self.latest_context_data.get("sanctions_status") != "CLEAR"
                        else "SWIFT_Wire_Executor"
                    )
                    target = next_worker
                    to_agent = next_worker
                    payload = dict(self.latest_context_data)
                    message = f"Delegating trade payload for invoice {self.latest_context_data.get('invoice_id')} to `{next_worker}`."

                # Guard against FX_Settlement_Supervisor directly executing swift:wire when SWIFT_Wire_Executor exists
                if (
                    self.brain.transport is None
                    and current_node.agent_id == "FX_Settlement_Supervisor"
                    and action == "CALL_TOOL"
                    and (target == "swift:wire" or args.get("tool_name") == "swift:wire")
                    and "SWIFT_Wire_Executor" in self.active_fleet_agents
                ):
                    action = "SEND_A2A"
                    target = "SWIFT_Wire_Executor"
                    to_agent = "SWIFT_Wire_Executor"
                    payload = dict(self.latest_context_data)
                    message = f"Delegating wire execution for invoice {self.latest_context_data.get('invoice_id')} to SWIFT_Wire_Executor."

                # =============================================================
                # Action 1: CALL_TOOL
                # =============================================================
                if action == "CALL_TOOL":
                    tool_name = target or str(args.pop("tool_name", "erp:read"))
                    call_args = dict(args)

                    # In live interactive mode, ensure consistent context fields when not testing chaos faults
                    if self.brain.transport is None:
                        if tool_name == "erp:read":
                            if "amount_usd" not in call_args and "amount_usd" in self.latest_context_data:
                                call_args["amount_usd"] = self.latest_context_data["amount_usd"]
                            if "invoice_id" not in call_args and "invoice_id" in self.latest_context_data:
                                call_args["invoice_id"] = self.latest_context_data["invoice_id"]
                        elif tool_name in ("fx:quote", "fx:quote_reuters") and "amount_usd" not in call_args:
                            call_args["amount_usd"] = self.latest_context_data.get("amount_usd", 2000000.0)
                        elif tool_name == "kyc:check":
                            if "supplier" not in call_args and "supplier" in self.latest_context_data:
                                call_args["supplier"] = self.latest_context_data["supplier"]
                        elif tool_name == "audit:log":
                            if "invoice_id" not in call_args and "invoice_id" in self.latest_context_data:
                                call_args["invoice_id"] = self.latest_context_data["invoice_id"]
                            if "supplier" not in call_args and "supplier" in self.latest_context_data:
                                call_args["supplier"] = self.latest_context_data["supplier"]
                            if "sanctions_status" not in call_args:
                                call_args["sanctions_status"] = self.latest_context_data.get("sanctions_status", "CLEAR")
                        elif tool_name == "swift:wire":
                            has_active_chaos = bool(self.kernel.c6.list_active_rules())
                            if not has_active_chaos and not self.kernel.c4.taint_status.get(current_node.agent_id, False):
                                for k_ctx in ("invoice_id", "supplier", "iban", "amount_usd", "fx_rate", "settlement_eur", "provider"):
                                    if k_ctx not in call_args and k_ctx in self.latest_context_data:
                                        call_args[k_ctx] = self.latest_context_data[k_ctx]
                                if "amount_usd" in call_args and "fx_rate" in call_args and "settlement_eur" not in call_args:
                                    call_args["settlement_eur"] = round(float(call_args["amount_usd"]) * float(call_args["fx_rate"]), 2)

                    # Auto-enqueue into Governor Review Queue before swift:wire if requested or if Branch_A is frozen
                    branch_a_frozen = not self.kernel.c3.ensure_branch_event("Branch_A").is_set()
                    should_queue_gov = (
                        tool_name == "swift:wire"
                        and not governor_resolved_in_run
                        and (
                            self.require_governor_on_wire
                            or _goal_requests_governor(goal)
                            or branch_a_frozen
                        )
                    )
                    if should_queue_gov:
                        gov_item, resume_ev = await self.enqueue_governor_review(
                            branch_id="Branch_A",
                            agent_id=current_node.agent_id,
                            trace_id=trace_id,
                            proposed_payload=call_args,
                            message="Pre-wire Human Governor approval queued before executing `swift:wire`.",
                        )
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "REQUEST_HUMAN",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": "Branch_A",
                                "action": "REQUEST_HUMAN",
                                "target": "Branch_A",
                                "content": (
                                    f"[SOFT_FREEZE] Paused `Branch_A` and queued Governor review "
                                    f"`{gov_item['item_id']}` before `swift:wire`: {json.dumps(gov_item['proposed_payload'])}"
                                ),
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "killswitch:SOFT_FREEZE",
                                    "http_code": 200,
                                    "hmac_prefix": gov_item["item_id"],
                                    "overridden": False,
                                },
                            }
                        )
                        try:
                            await asyncio.wait_for(
                                resume_ev.wait(), timeout=self.governor_timeout_sec
                            )
                        except asyncio.TimeoutError:
                            gov_item["status"] = "TIMEOUT"
                            await self.client.trigger_kill_switch(
                                mode="SOFT_FREEZE",
                                target_id="Branch_A",
                                operator_override_state=None,
                            )
                            final_summary = (
                                f"[GOVERNOR_TIMEOUT] Governor review `{gov_item['item_id']}` timed out after "
                                f"{self.governor_timeout_sec}s; aborting goal."
                            )
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "SYSTEM_BLOCK",
                                    "agent_id": current_node.agent_id,
                                    "role": current_node.role,
                                    "branch_id": "Branch_A",
                                    "action": "GOVERNOR_TIMEOUT",
                                    "target": "Branch_A",
                                    "content": final_summary,
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "governor:timeout",
                                        "http_code": 408,
                                        "hmac_prefix": "BLOCKED",
                                        "overridden": False,
                                    },
                                }
                            )
                            break

                        governor_resolved_in_run = True
                        if gov_item.get("status") == "REJECTED":
                            final_summary = (
                                f"[GOVERNOR_REJECTED] Human Governor rejected review `{gov_item['item_id']}`; "
                                f"aborting goal without executing `{tool_name}`."
                            )
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "SYSTEM_BLOCK",
                                    "agent_id": "Human_Operator",
                                    "role": "Human_Governor",
                                    "branch_id": "Branch_A",
                                    "action": "GOVERNOR_REJECT",
                                    "target": tool_name,
                                    "content": final_summary,
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "governor:reject",
                                        "http_code": 403,
                                        "hmac_prefix": "REJECTED",
                                        "overridden": False,
                                    },
                                }
                            )
                            break

                        override_applied = gov_item.get("operator_override_state") or {}
                        if override_applied:
                            call_args.update(override_applied)
                            self.latest_context_data.update(override_applied)
                        prov_rec = self.kernel.c4.provenance_receipts.get(trace_id)
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "GOVERNOR_RESUMED",
                                "agent_id": "Human_Operator",
                                "role": "Human_Governor",
                                "branch_id": "Branch_A",
                                "action": "GOVERNOR_OVERRIDE",
                                "target": "Branch_A",
                                "content": (
                                    f"[HUMAN_GOVERNOR_APPROVED] Resumed `Branch_A` with "
                                    f"operator_override_state={json.dumps(override_applied)}"
                                ),
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "governor:resume",
                                    "http_code": 200,
                                    "hmac_prefix": (
                                        prov_rec["hmac_sha256"][:16]
                                        if prov_rec and "hmac_sha256" in prov_rec
                                        else "OVERRIDDEN_HMAC"
                                    ),
                                    "overridden": bool(override_applied),
                                    "operator_override_state": override_applied,
                                },
                            }
                        )

                    if self.feed_mode == "live" and "feed_mode" not in call_args:
                        call_args["feed_mode"] = "live"

                    try:
                        res = await current_node.call_tool(
                            tool_name,
                            trace_id=trace_id,
                            **call_args,
                        )
                        data = res.get("data", {})
                        if isinstance(data, dict):
                            for k_save in (
                                "invoice_id",
                                "supplier",
                                "iban",
                                "amount_usd",
                                "fx_rate",
                                "settlement_eur",
                                "provider",
                                "sanctions_status",
                                "risk_score",
                                "audit_id",
                            ):
                                if k_save in data:
                                    self.latest_context_data[k_save] = data[k_save]
                            if tool_name == "swift:wire" and data.get("wire_status") == "SETTLED":
                                wire_settled_in_run = True
                                last_wire_output = dict(data)
                        consecutive_self_loops = 0

                        prov_receipt = self.kernel.c4.provenance_receipts.get(trace_id)
                        if (
                            prov_receipt
                            and prov_receipt.get("tool_name") == tool_name
                            and "hmac_sha256" in prov_receipt
                        ):
                            hmac_prefix = prov_receipt["hmac_sha256"][:16]
                        else:
                            hmac_prefix = "NONE"
                        overridden_flag = bool(
                            (prov_receipt and prov_receipt.get("overridden"))
                            or self.kernel.c3.branch_overrides.get(current_node.branch_id)
                            or self.kernel.c3.branch_overrides.get("Branch_A")
                        )
                        receipt = {
                            "trace_id": trace_id,
                            "tool": tool_name,
                            "http_code": 200,
                            "hmac_prefix": hmac_prefix,
                            "overridden": overridden_flag,
                            "output": data,
                        }
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "CALL_TOOL",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": tool_name,
                                "content": (
                                    f"Executed `{tool_name}` -> {json.dumps(data)}"
                                    + (f" ({message})" if message else "")
                                ),
                                "receipt": receipt,
                            }
                        )
                    except AegisHTTPException as exc:
                        err_receipt = {
                            "trace_id": trace_id,
                            "tool": tool_name,
                            "http_code": exc.status_code,
                            "hmac_prefix": "BLOCKED",
                            "overridden": False,
                            "error": exc.detail,
                        }
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SYSTEM_BLOCK",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": tool_name,
                                "content": f"[SYSTEM_BLOCK HTTP {exc.status_code}] {exc.detail}",
                                "receipt": err_receipt,
                            }
                        )

                        # If governor fault verification is enabled, pause via SOFT_FREEZE
                        # and await human operator verification in Governor Review Queue
                        if self.require_governor_on_fault and exc.status_code in (403, 409, 503):
                            fault_label = (
                                "Gate L4 Pre-Commit Violation"
                                if exc.status_code == 409
                                else "RBAC Unauthorized Tool Block"
                                if exc.status_code == 403
                                else "Infrastructure Outage"
                            )
                            gov_item, resume_ev = await self.enqueue_governor_review(
                                branch_id=current_node.branch_id,
                                agent_id=current_node.agent_id,
                                trace_id=trace_id,
                                proposed_payload=call_args or self.latest_context_data,
                                message=(
                                    f"Fault detected: {fault_label} on `{tool_name}` ({exc.detail}). "
                                    f"Awaiting Human Governor verification in Governor Review Queue to approve recovery or reject."
                                ),
                            )
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "REQUEST_HUMAN",
                                    "agent_id": current_node.agent_id,
                                    "role": current_node.role,
                                    "branch_id": current_node.branch_id,
                                    "action": "REQUEST_HUMAN",
                                    "target": "Human_Governor",
                                    "content": (
                                        f"[HUMAN_VERIFICATION_REQUIRED] Fault detected during `{tool_name}` on `{current_node.agent_id}` "
                                        f"({exc.detail}). Branch `{current_node.branch_id}` paused via `SOFT_FREEZE`. "
                                        f"Please verify in Governor Review Queue `{gov_item['item_id']}` to approve recovery or reject."
                                    ),
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "governor:fault_verification",
                                        "http_code": 200,
                                        "hmac_prefix": gov_item["item_id"],
                                        "overridden": False,
                                    },
                                }
                            )
                            try:
                                await asyncio.wait_for(
                                    resume_ev.wait(), timeout=self.governor_timeout_sec
                                )
                            except asyncio.TimeoutError:
                                gov_item["status"] = "TIMEOUT"
                                self._record_turn(
                                    {
                                        "turn": turn_idx,
                                        "type": "SYSTEM_BLOCK",
                                        "agent_id": current_node.agent_id,
                                        "role": current_node.role,
                                        "branch_id": current_node.branch_id,
                                        "action": "GOVERNOR_TIMEOUT",
                                        "target": current_node.agent_id,
                                        "content": (
                                            f"[GOVERNOR_TIMEOUT] Governor review `{gov_item['item_id']}` timed out after "
                                            f"{self.governor_timeout_sec}s; aborting goal."
                                        ),
                                        "receipt": {
                                            "trace_id": trace_id,
                                            "tool": "governor:timeout",
                                            "http_code": 408,
                                            "hmac_prefix": "TIMEOUT",
                                            "overridden": False,
                                        },
                                    }
                                )
                                break

                            if gov_item.get("status") == "REJECTED":
                                self._record_turn(
                                    {
                                        "turn": turn_idx,
                                        "type": "SYSTEM_BLOCK",
                                        "agent_id": current_node.agent_id,
                                        "role": current_node.role,
                                        "branch_id": current_node.branch_id,
                                        "action": "GOVERNOR_REJECTED",
                                        "target": current_node.agent_id,
                                        "content": (
                                            f"[GOVERNOR_REJECTED] Governor review `{gov_item['item_id']}` was rejected by "
                                            f"Human Governor; aborting goal."
                                        ),
                                        "receipt": {
                                            "trace_id": trace_id,
                                            "tool": "governor:reject",
                                            "http_code": 403,
                                            "hmac_prefix": "REJECTED",
                                            "overridden": False,
                                        },
                                    }
                                )
                                break

                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "GOVERNOR_RESUMED",
                                    "agent_id": "Human_Operator",
                                    "role": "Human_Governor",
                                    "branch_id": current_node.branch_id,
                                    "action": "HUMAN_OVERRIDE_RESUME",
                                    "target": current_node.agent_id,
                                    "content": (
                                        f"[HUMAN_GOVERNOR_APPROVED] Human operator verified and approved recovery for `{gov_item['item_id']}`. "
                                        + (
                                            f"Applied override state: {json.dumps(gov_item.get('operator_override_state'))}. "
                                            if gov_item.get("operator_override_state")
                                            else ""
                                        )
                                        + "Proceeding with recovery."
                                    ),
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "governor:resume",
                                        "http_code": 200,
                                        "hmac_prefix": "APPROVED",
                                        "overridden": bool(gov_item.get("operator_override_state")),
                                    },
                                }
                            )

                        if exc.status_code == 409:
                            rb = await self._recover_from_precommit_gate_block(
                                current_node=current_node,
                                trace_id=trace_id,
                                exc=exc,
                            )
                            rollback_in_run = True
                            last_a2a_payload.clear()
                            trace_id = f"trace_retry_{uuid4().hex[:8]}"
                            current_node = self.live_agents.get(
                                "FX_Quote_Reader", current_node
                            )
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "SYSTEM_RECOVERY",
                                    "agent_id": "AEGIS_Control_Plane",
                                    "role": "Control_Plane_Kernel",
                                    "branch_id": "Branch_A",
                                    "action": "ISOLATE_AND_ROLLBACK",
                                    "target": rb["restored_checkpoint_id"],
                                    "content": (
                                        f"[SYSTEM_RECOVERY] Causal BFS Taint isolated {rb['tainted_bfs_nodes']}; "
                                        f"rolled back Branch_A to `{rb['restored_checkpoint_id']}` "
                                        f"(R_blast={rb['blast_radius_ratio']}). Respawned clean Branch_A agents; "
                                        f"use fallback tool `fx:quote_reuters` for clean quote."
                                    ),
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "isolate_and_rollback",
                                        "http_code": 200,
                                        "hmac_prefix": "ROLLBACK_OK",
                                        "overridden": False,
                                        "rollback_report": rb,
                                    },
                                }
                            )
                        elif exc.status_code == 403 and self.require_governor_on_fault:
                            recovery_res = await self.recover_and_restart_node(
                                failed_agent_id=current_node.agent_id,
                                reason=f"RBAC 403 block on tool `{tool_name}`: {exc.detail}",
                            )
                            rollback_in_run = True
                            last_a2a_payload.clear()
                            trace_id = f"trace_retry_{uuid4().hex[:8]}"
                            current_node = self.live_agents.get(
                                current_node.agent_id, current_node
                            )

                # =============================================================
                # Action 2: SPAWN_CHILD
                # =============================================================
                elif action == "SPAWN_CHILD":
                    child_id = target or str(args.get("child_id", f"Worker_{turn_idx}"))
                    req_role = str(args.get("requested_role", f"L2_{child_id}"))
                    raw_req_scopes = args.get("requested_scopes")
                    req_scopes = (
                        list(raw_req_scopes)
                        if raw_req_scopes
                        else [s for s in current_node.scopes if s != "swift:wire"]
                        or list(current_node.scopes)
                    )
                    req_budget = int(args.get("token_budget", 1500))
                    req_branch = str(args.get("branch_id", current_node.branch_id))

                    # Enforce canonical least-privilege role ceiling so SPAWN_CHILD can NEVER
                    # overwrite a canonical agent (e.g. FX_Quote_Reader) with wider scopes like swift:wire!
                    if child_id in CANONICAL_AGENT_SCOPES:
                        allowed_ceiling = CANONICAL_AGENT_SCOPES[child_id]
                        req_scopes = [
                            s for s in req_scopes if s in allowed_ceiling
                        ] or [
                            s for s in allowed_ceiling if s in current_node.scopes
                        ]
                        existing_canonical = self.live_agents.get(child_id)
                        if (
                            existing_canonical is not None
                            and existing_canonical.jti
                            not in self.kernel.c1.revocation_set
                        ):
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "SPAWN_CHILD",
                                    "agent_id": current_node.agent_id,
                                    "role": current_node.role,
                                    "branch_id": current_node.branch_id,
                                    "action": action,
                                    "target": child_id,
                                    "content": (
                                        f"Delegated to existing canonical agent `{child_id}` preserving "
                                        f"immutable least-privilege scopes {existing_canonical.scopes}"
                                        + (f" ({message})" if message else "")
                                    ),
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": "token:preserve_canonical",
                                        "http_code": 200,
                                        "hmac_prefix": (
                                            existing_canonical.jti or "JTI"
                                        )[:16],
                                        "overridden": False,
                                    },
                                }
                            )
                            current_node = existing_canonical
                            continue

                    try:
                        child_node = await current_node.spawn_child(
                            child_id=child_id,
                            requested_role=req_role,
                            requested_scopes=req_scopes,
                            token_budget=req_budget,
                            branch_id=req_branch,
                        )
                        self.live_agents[child_id] = child_node
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SPAWN_CHILD",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": child_id,
                                "content": (
                                    f"Spawned child agent `{child_id}` with attenuated scopes {child_node.scopes}"
                                    + (f" ({message})" if message else "")
                                ),
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "token:mint",
                                    "http_code": 200,
                                    "hmac_prefix": (child_node.jti or "JTI")[:16],
                                    "overridden": False,
                                },
                            }
                        )
                        current_node = child_node
                    except AegisHTTPException as exc:
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SYSTEM_BLOCK",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": child_id,
                                "content": f"[SYSTEM_BLOCK HTTP {exc.status_code}] {exc.detail}",
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "token:mint",
                                    "http_code": exc.status_code,
                                    "hmac_prefix": "BLOCKED",
                                    "overridden": False,
                                },
                            }
                        )

                # =============================================================
                # Action 3: SEND_A2A
                # =============================================================
                elif action == "SEND_A2A":
                    receiver_id = to_agent or "FX_Settlement_Supervisor"
                    if receiver_id == current_node.agent_id:
                        consecutive_self_loops += 1
                        if consecutive_self_loops == 1:
                            self._record_turn(
                                {
                                    "turn": turn_idx,
                                    "type": "SEND_A2A",
                                    "agent_id": current_node.agent_id,
                                    "role": current_node.role,
                                    "branch_id": current_node.branch_id,
                                    "action": action,
                                    "target": receiver_id,
                                    "content": (
                                        f"[A2A_SELF_LOOP_IGNORED] Agent `{current_node.agent_id}` attempted to send "
                                        f"A2A message to itself; skipping kernel call."
                                    ),
                                    "receipt": {
                                        "trace_id": trace_id,
                                        "tool": f"a2a->{receiver_id}",
                                        "http_code": 200,
                                        "hmac_prefix": "SELF_LOOP_SKIP",
                                        "overridden": False,
                                    },
                                }
                            )
                            continue
                        # Break repeated self-loop by routing to the appropriate downstream worker
                        if current_node.agent_id in ("Chief_Treasury_Orchestrator", "FX_Settlement_Supervisor"):
                            receiver_id = (
                                "SWIFT_Wire_Executor"
                                if trace_id in self.kernel.c4.provenance_receipts
                                else "FX_Quote_Reader"
                            )
                        elif current_node.agent_id == "FX_Quote_Reader":
                            receiver_id = (
                                "Sanctions_Screener"
                                if self.latest_context_data.get("sanctions_status") != "CLEAR"
                                else "SWIFT_Wire_Executor"
                            )
                        elif current_node.agent_id == "Sanctions_Screener":
                            receiver_id = "Audit_Logger"
                        elif current_node.agent_id == "Audit_Logger":
                            receiver_id = "SWIFT_Wire_Executor"
                        else:
                            receiver_id = "Chief_Treasury_Orchestrator"
                    else:
                        consecutive_self_loops = 0

                    schema_name = (
                        "TreasurySettlementSchema"
                        if {
                            "amount_usd",
                            "fx_rate",
                            "settlement_eur",
                        }.issubset(payload.keys())
                        else None
                    )
                    try:
                        a2a_res = await current_node.send_a2a(
                            receiver_id=receiver_id,
                            payload=payload,
                            trace_id=trace_id,
                            schema_name=schema_name,
                            optimistic_pipeline=True,
                        )
                        delivered_payload = a2a_res.get("payload", payload)
                        if isinstance(delivered_payload, dict):
                            last_a2a_payload = dict(delivered_payload)
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SEND_A2A",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": receiver_id,
                                "content": (
                                    f"Routed A2A message to `{receiver_id}`: {json.dumps(delivered_payload)}"
                                    + (f" ({message})" if message else "")
                                ),
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": f"a2a->{receiver_id}",
                                    "http_code": 200,
                                    "hmac_prefix": str(a2a_res.get("edge_id", "EDGE"))[:16],
                                    "overridden": False,
                                },
                            }
                        )
                        if receiver_id in self.live_agents:
                            current_node = self.live_agents[receiver_id]
                    except AegisHTTPException as exc:
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SYSTEM_BLOCK",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": current_node.branch_id,
                                "action": action,
                                "target": receiver_id,
                                "content": f"[SYSTEM_BLOCK HTTP {exc.status_code}] {exc.detail}",
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": f"a2a->{receiver_id}",
                                    "http_code": exc.status_code,
                                    "hmac_prefix": "BLOCKED",
                                    "overridden": False,
                                },
                            }
                        )

                # =============================================================
                # Action 4: REQUEST_HUMAN (Soft-Freeze & Governor Queue)
                # =============================================================
                elif action == "REQUEST_HUMAN":
                    branch_to_freeze = (
                        target
                        if target in ("Branch_A", "Branch_B")
                        else (
                            current_node.branch_id
                            if current_node.branch_id != "Root"
                            else "Branch_A"
                        )
                    )
                    gov_item, resume_ev = await self.enqueue_governor_review(
                        branch_id=branch_to_freeze,
                        agent_id=current_node.agent_id,
                        trace_id=trace_id,
                        proposed_payload=payload,
                        message=message or "Human governor review requested before settlement commit.",
                    )

                    self._record_turn(
                        {
                            "turn": turn_idx,
                            "type": "REQUEST_HUMAN",
                            "agent_id": current_node.agent_id,
                            "role": current_node.role,
                            "branch_id": branch_to_freeze,
                            "action": action,
                            "target": branch_to_freeze,
                            "content": (
                                f"[SOFT_FREEZE] Paused `{branch_to_freeze}` and enqueued Governor review "
                                f"`{gov_item['item_id']}` with proposed payload {json.dumps(gov_item['proposed_payload'])}"
                            ),
                            "receipt": {
                                "trace_id": trace_id,
                                "tool": "killswitch:SOFT_FREEZE",
                                "http_code": 200,
                                "hmac_prefix": gov_item["item_id"],
                                "overridden": False,
                            },
                        }
                    )

                    try:
                        await asyncio.wait_for(
                            resume_ev.wait(), timeout=self.governor_timeout_sec
                        )
                    except asyncio.TimeoutError:
                        gov_item["status"] = "TIMEOUT"
                        await self.client.trigger_kill_switch(
                            mode="SOFT_FREEZE",
                            target_id=branch_to_freeze,
                            operator_override_state=None,
                        )
                        final_summary = (
                            f"[GOVERNOR_TIMEOUT] Governor review `{gov_item['item_id']}` timed out after "
                            f"{self.governor_timeout_sec}s; aborting goal."
                        )
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SYSTEM_BLOCK",
                                "agent_id": current_node.agent_id,
                                "role": current_node.role,
                                "branch_id": branch_to_freeze,
                                "action": "GOVERNOR_TIMEOUT",
                                "target": branch_to_freeze,
                                "content": final_summary,
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "governor:timeout",
                                    "http_code": 408,
                                    "hmac_prefix": "BLOCKED",
                                    "overridden": False,
                                },
                            }
                        )
                        break

                    governor_resolved_in_run = True
                    if gov_item.get("status") == "REJECTED":
                        final_summary = (
                            f"[GOVERNOR_REJECTED] Human Governor rejected review `{gov_item['item_id']}`; "
                            f"aborting goal without executing wire."
                        )
                        self._record_turn(
                            {
                                "turn": turn_idx,
                                "type": "SYSTEM_BLOCK",
                                "agent_id": "Human_Operator",
                                "role": "Human_Governor",
                                "branch_id": branch_to_freeze,
                                "action": "GOVERNOR_REJECT",
                                "target": branch_to_freeze,
                                "content": final_summary,
                                "receipt": {
                                    "trace_id": trace_id,
                                    "tool": "governor:reject",
                                    "http_code": 403,
                                    "hmac_prefix": "REJECTED",
                                    "overridden": False,
                                },
                            }
                        )
                        break

                    override_applied = gov_item.get("operator_override_state") or {}
                    if override_applied:
                        self.latest_context_data.update(override_applied)
                    prov_rec = self.kernel.c4.provenance_receipts.get(trace_id)
                    hmac_after = (
                        prov_rec["hmac_sha256"][:16]
                        if prov_rec and "hmac_sha256" in prov_rec
                        else "OVERRIDDEN_HMAC"
                    )
                    self._record_turn(
                        {
                            "turn": turn_idx,
                            "type": "GOVERNOR_RESUMED",
                            "agent_id": "Human_Operator",
                            "role": "Human_Governor",
                            "branch_id": branch_to_freeze,
                            "action": "GOVERNOR_OVERRIDE",
                            "target": branch_to_freeze,
                            "content": (
                                f"[HUMAN_GOVERNOR_APPROVED] Resumed `{branch_to_freeze}` with "
                                f"operator_override_state={json.dumps(override_applied)}. "
                                f"Proceed with `swift:wire` using {json.dumps(self.latest_context_data)}."
                            ),
                            "receipt": {
                                "trace_id": trace_id,
                                "tool": "governor:resume",
                                "http_code": 200,
                                "hmac_prefix": hmac_after,
                                "overridden": bool(override_applied),
                                "operator_override_state": override_applied,
                            },
                        }
                    )

                # =============================================================
                # Action 5: COMPLETE
                # =============================================================
                elif action == "COMPLETE":
                    final_summary = message or "Goal completed."
                    prov_rec = self.kernel.c4.provenance_receipts.get(trace_id)
                    self._record_turn(
                        {
                            "turn": turn_idx,
                            "type": "COMPLETE",
                            "agent_id": current_node.agent_id,
                            "role": current_node.role,
                            "branch_id": current_node.branch_id,
                            "action": "COMPLETE",
                            "target": "COMPLETE",
                            "content": final_summary,
                            "receipt": {
                                "trace_id": trace_id,
                                "tool": "workflow:complete",
                                "http_code": 200,
                                "hmac_prefix": (
                                    prov_rec["hmac_sha256"][:16]
                                    if prov_rec and "hmac_sha256" in prov_rec
                                    else "COMPLETE"
                                ),
                                "overridden": bool(
                                    prov_rec and prov_rec.get("overridden")
                                ),
                            },
                        }
                    )
                    break
            else:
                if not final_summary:
                    prov_rec = self.kernel.c4.provenance_receipts.get(trace_id)
                    if wire_settled_in_run:
                        final_summary = (
                            f"SWIFT wire settlement completed cleanly: UETR `{last_wire_output.get('uetr', 'SETTLED')}`, "
                            f"invoice `{last_wire_output.get('invoice_id', 'INV-2026-9981')}`, "
                            f"FX rate `{last_wire_output.get('fx_rate')}`, "
                            f"EUR `{last_wire_output.get('settlement_eur')}` via `{last_wire_output.get('provider')}`."
                        )
                    else:
                        final_summary = (
                            f"Completed {effective_max_turns} governed turns (latest context: "
                            f"invoice `{self.latest_context_data.get('invoice_id')}`, "
                            f"FX rate `{self.latest_context_data.get('fx_rate')}`, "
                            f"EUR `{self.latest_context_data.get('settlement_eur')}`)."
                        )
                    self._record_turn(
                        {
                            "turn": effective_max_turns,
                            "type": "COMPLETE",
                            "agent_id": current_node.agent_id,
                            "role": current_node.role,
                            "branch_id": current_node.branch_id,
                            "action": "COMPLETE",
                            "target": "COMPLETE",
                            "content": final_summary,
                            "receipt": {
                                "trace_id": trace_id,
                                "tool": "workflow:complete",
                                "http_code": 200,
                                "hmac_prefix": (
                                    prov_rec["hmac_sha256"][:16]
                                    if prov_rec and "hmac_sha256" in prov_rec
                                    else "COMPLETE"
                                ),
                                "overridden": bool(
                                    prov_rec and prov_rec.get("overridden")
                                ),
                            },
                        }
                    )

        finally:
            self.is_running = False
            self.waiting_for_step = False
            self._flush_kernel_events_to_journal()

        return {
            "goal": goal,
            "turns_taken": max(0, len(self.transcript) - start_len - 1),
            "final_summary": final_summary,
            "transcript": list(self.transcript),
            "governor_queue": list(self.governor_queue),
            "journal_path": str(self.journal.path),
            "telemetry": self.kernel.get_telemetry_state(),
        }
