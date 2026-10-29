"""The 7 Core Control Plane Methods and ControlPlaneKernel with Simultaneous Multi-Agent & Multi-Tool Resilience."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Optional
from uuid import uuid4

import httpx
from pydantic import ValidationError

from agents.mock_tools import execute_mock_tool
from control_plane.components import (
    Chaos_Monkey,
    IAM_Engine,
    Lineage_Graph,
    Policy_Store,
    Snapshot_DB,
    Task_Registry,
)
from control_plane.models import (
    A2AMessagePayload,
    MASTER_ROOT_SCOPES,
    TOOL_PROVIDER_CHAINS,
    VALID_FAULT_CATEGORIES,
    validate_tool_args_gate_l3,
)


class ControlPlaneKernel:
    """
    Unified Control Plane Kernel encapsulating C1–C6 and exposing the 7 core methods
    in a shared asyncio event loop for deterministic sub-50ms task/socket termination
    and simultaneous multi-agent / multi-tool failure recovery.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all 6 components, locks, and telemetry metrics to a clean state."""
        self.C1 = IAM_Engine()
        self.C2 = Policy_Store()
        self.C3 = Task_Registry()
        self.C4 = Lineage_Graph()
        self.C5 = Snapshot_DB()
        self.C6 = Chaos_Monkey()

        # Descriptive component aliases
        self.iam_engine = self.C1
        self.policy_store = self.C2
        self.task_registry = self.C3
        self.lineage_graph = self.C4
        self.snapshot_db = self.C5
        self.chaos_monkey = self.C6

        # Per-branch async locks for concurrent multi-branch rollback isolation
        self._rollback_locks: dict[str, asyncio.Lock] = {
            "Branch_A": asyncio.Lock(),
            "Branch_B": asyncio.Lock(),
            "Root": asyncio.Lock(),
        }

        # Telemetry metrics state
        self.last_kill_latency_ms: float = 0.0
        self.last_blast_radius_ratio: float = 1.0
        self.last_mttr_seconds: float = 0.0
        self.recovery_history: list[dict[str, Any]] = []
        self.kill_switch_history: list[dict[str, Any]] = []
        self.failover_events: list[dict[str, Any]] = []
        self.agent_respawn_events: list[dict[str, Any]] = []

    # =========================================================================
    # Method 0: Bootstrap Helper for L0 Root Orchestrator
    # =========================================================================
    def bootstrap_root_token(
        self,
        root_agent_id: str = "Chief_Treasury_Orchestrator",
        token_budget: int = 10000,
    ) -> str:
        """Mint the initial L0 root JWT with full master scopes S_root and register in C1–C4."""
        now = time.time()
        jti = f"tok_{root_agent_id}_{uuid4().hex[:6]}"
        claims = {
            "jti": jti,
            "sub": root_agent_id,
            "parent": None,
            "role": "L0_Root_Orchestrator",
            "branch": "Root",
            "scopes": list(MASTER_ROOT_SCOPES),
            "iat": now,
            "exp": now + 3600.0,
        }
        root_jwt = self.C1.sign_jwt(claims)
        self.C2.init_agent_budget(root_agent_id, max_tokens=token_budget)
        self.C3.register_agent(
            agent_id=root_agent_id,
            jti=jti,
            role="L0_Root_Orchestrator",
            parent_id=None,
            branch_id="Root",
            status="RUNNING",
        )
        self.C4.taint_status[root_agent_id] = False
        self.C4.vector_clock[root_agent_id] = 0
        return root_jwt

    # =========================================================================
    # Method 1: Cryptographic Delegation & Privilege Attenuation
    # =========================================================================
    def mint_delegated_token(
        self,
        parent_jwt: str,
        child_agent_id: str,
        requested_role: str,
        requested_scopes: list[str],
        token_budget: int,
        branch_id: str,
    ) -> str:
        """
        Verify parent_jwt, enforce S_child <= S_parent and budget attenuation,
        mint Ed25519 JWT for child_agent_id, and register in C1, C2, C3, C4.
        """
        # 1. Decode and verify parent_jwt via C1
        parent_claims = self.C1.verify_jwt(
            parent_jwt, error_prefix="PARENT_TOKEN_REVOKED_OR_INVALID"
        )
        parent_id = parent_claims["sub"]

        # 2. Enforce Mathematical Privilege Attenuation: S_child <= S_parent
        s_parent = set(parent_claims.get("scopes", []))
        s_child = set(requested_scopes)
        if not s_child.issubset(s_parent):
            unauthorized_scopes = s_child - s_parent
            self.C2.rbac_attempts_unauthorized += 1
            self.C2.rbac_blocks_enforced += 1
            raise PermissionError(
                f"PRIVILEGE_ESCALATION_BLOCKED: {unauthorized_scopes} not in parent scopes"
            )

        # 3. Enforce budget attenuation in C2
        if token_budget <= 0:
            raise ValueError("INVALID_TOKEN_BUDGET: token_budget must be positive")
        parent_budget = self.C2.budget_ledger.get(parent_id)
        if parent_budget is not None:
            parent_remaining = (
                parent_budget["max_tokens"] - parent_budget["tokens_used"]
            )
            if token_budget > parent_remaining:
                raise PermissionError(
                    f"BUDGET_ATTENUATION_VIOLATION: requested {token_budget} > parent remaining {parent_remaining}"
                )
        self.C2.init_agent_budget(child_agent_id, max_tokens=token_budget)

        # 4. Generate unique JTI and sign child claims with C1._private_key
        now = time.time()
        jti = f"tok_{child_agent_id}_{uuid4().hex[:6]}"
        child_claims = {
            "jti": jti,
            "sub": child_agent_id,
            "parent": parent_id,
            "role": requested_role,
            "branch": branch_id,
            "scopes": list(requested_scopes),
            "iat": now,
            "exp": now + 3600.0,
        }
        child_jwt = self.C1.sign_jwt(child_claims)

        # 5. Register child_agent_id in C3.process_table and link to parent
        self.C3.register_agent(
            agent_id=child_agent_id,
            jti=jti,
            role=requested_role,
            parent_id=parent_id,
            branch_id=branch_id,
            status="READY",
        )

        # 6. Initialize C4 taint_status and vector_clock
        self.C4.taint_status[child_agent_id] = False
        self.C4.taint_reasons.pop(child_agent_id, None)
        self.C4.vector_clock[child_agent_id] = self.C4.vector_clock.get(parent_id, 0)

        return child_jwt

    # =========================================================================
    # Method 2: Versioned Subtree Checkpointing
    # =========================================================================
    def create_checkpoint(
        self,
        agent_jwt: str,
        branch_id: str,
        current_graph_state: dict[str, Any],
    ) -> str:
        """
        Verify agent_jwt, confirm agent is not tainted, and persist clean subtree snapshot
        to C5 (both in-memory and SQLite).
        """
        # 1. Verify agent_jwt via C1
        claims = self.C1.verify_jwt(agent_jwt)
        agent_id = claims["sub"]

        # 2. Refuse to checkpoint tainted state
        if self.C4.taint_status.get(agent_id, False):
            raise RuntimeError("CANNOT_CHECKPOINT_TAINTED_STATE")

        # 3. Generate deterministic branch checkpoint ID (e.g. chk_Branch_A_1, chk_Branch_B_1)
        branch_snap_count = sum(
            1 for (b_id, _) in self.C5.snapshots.keys() if b_id == branch_id
        )
        checkpoint_id = f"chk_{branch_id}_{branch_snap_count + 1}"

        # 4. Deep-copy vector clock and serialize current_graph_state into JSON bytes
        vc_snapshot = copy.deepcopy(self.C4.vector_clock)
        serialized_blob = json.dumps(current_graph_state, sort_keys=True).encode(
            "utf-8"
        )

        # 5. Save into C5
        self.C5.save_snapshot(
            checkpoint_id=checkpoint_id,
            branch_id=branch_id,
            created_by=agent_id,
            timestamp=time.perf_counter(),
            vector_clock_snapshot=vc_snapshot,
            serialized_state_blob=serialized_blob,
        )
        return checkpoint_id

    # =========================================================================
    # Method 3: Zero-Trust Tool Execution Proxy & Gate L4 Pre-Commit Check
    # =========================================================================
    async def intercept_tool_call(
        self,
        agent_jwt: str,
        tool_name: str,
        tool_args: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        """
        Intercept all external tool invocations. Enforces C1 identity, C3 soft-freeze,
        C6 chaos hooks, C2 RBAC & rate limits, C4 Gate L4 pre-commit taint check,
        and multi-tool provider failover telemetry.
        """
        # 1. Identity & Revocation Check (C1)
        claims = self.C1.verify_jwt(agent_jwt, error_prefix="HTTP_401_TOKEN_REVOKED")
        agent_id = claims["sub"]
        branch_id = claims.get("branch", "Branch_A")
        effective_args = copy.deepcopy(tool_args)

        # 2. Soft-Freeze Gate Check (C3)
        branch_event = self.C3.branch_freeze_events.get(branch_id)
        global_event = self.C3.branch_freeze_events.get("ALL")
        if (branch_event and not branch_event.is_set()) or (
            global_event and not global_event.is_set()
        ):
            if agent_id in self.C3.process_table:
                self.C3.process_table[agent_id]["status"] = "PAUSED"
            if branch_event and not branch_event.is_set():
                await branch_event.wait()
            if global_event and not global_event.is_set():
                await global_event.wait()
            if agent_id in self.C3.process_table:
                self.C3.process_table[agent_id]["status"] = "RUNNING"

        # Merge human operator override if present for branch_id
        if branch_id in self.C3.operator_overrides:
            override_data = self.C3.operator_overrides[branch_id]
            effective_args.update(override_data)

        # 3. Chaos Privilege Escalation & Runaway Loop Hooks (C6)
        priv_rule = self.C6.get_active_rule(agent_id, "PRIVILEGE_ESCALATION")
        if priv_rule is not None:
            self.C6.consume_rule(priv_rule)
            tool_name = "swift:wire"
            effective_args = {
                "iban": "DE89370400440532013000",
                "amount_eur": 2_000_000.0,
                "amount_usd": 2_000_000.0,
                "fx_rate": 1.0,
            }

        loop_rule = self.C6.get_active_rule(agent_id, "RUNAWAY_LOOP")
        if loop_rule is not None:
            self.C6.consume_rule(loop_rule)
            ledger = self.C2.budget_ledger.setdefault(
                agent_id,
                {
                    "tokens_used": 0,
                    "max_tokens": 2000,
                    "calls_made": 0,
                    "max_calls_per_min": 10,
                    "wasted_tokens_on_failure": 0,
                },
            )
            ledger["calls_made"] = ledger["max_calls_per_min"] + 1

        # 4. RBAC & Rate-Limit Check (C2)
        allowed_scopes = claims.get("scopes", [])
        if tool_name not in allowed_scopes:
            self.C2.rbac_attempts_unauthorized += 1
            self.C2.rbac_blocks_enforced += 1
            if agent_id in self.C2.budget_ledger:
                self.C2.budget_ledger[agent_id]["wasted_tokens_on_failure"] += 45
            await self.trigger_kill_switch(mode="SURGICAL", target_id=agent_id)
            if agent_id in self.C3.process_table:
                self.C3.process_table[agent_id]["status"] = "BLOCKED_403"
            return {
                "status": "BLOCKED_403",
                "http_code": 403,
                "agent_id": agent_id,
                "error": f"RBAC_SCOPE_VIOLATION: {agent_id} lacks scope {tool_name}",
            }

        ledger = self.C2.budget_ledger.get(agent_id)
        tool_policy = self.C2.rbac_matrix.get(
            tool_name, {"mutates_state": False, "max_calls": 10}
        )
        if ledger is not None:
            if (
                ledger["calls_made"] >= ledger["max_calls_per_min"]
                or ledger["calls_made"] >= tool_policy.get("max_calls", 10)
                or ledger["tokens_used"] >= ledger["max_tokens"]
            ):
                ledger["wasted_tokens_on_failure"] += 50
                await self.trigger_kill_switch(mode="SURGICAL", target_id=agent_id)
                return {
                    "status": "BUDGET_EXCEEDED_429",
                    "http_code": 429,
                    "agent_id": agent_id,
                    "error": f"RATE_OR_BUDGET_EXCEEDED: {agent_id} exceeded call/token limits",
                }

        # 5. Gate L4 — Pre-Commit Taint & Lineage Check (C4)
        if tool_policy.get("mutates_state", False):
            tainted_branch_agents = [
                a_id
                for a_id, info in self.C3.process_table.items()
                if info.get("branch_id") == branch_id
                and self.C4.taint_status.get(a_id, False)
            ]
            is_caller_tainted = self.C4.taint_status.get(agent_id, False)

            # Verify Gate L2 (Provenance Receipt) on tool_args if receipt exists for trace_id
            l2_failed = False
            l2_reason: Optional[str] = None
            receipt = self.C4.provenance_receipts.get(trace_id)
            if receipt is not None and branch_id not in self.C3.operator_overrides:
                raw_out = receipt.get("raw_output", {})
                if "fx_rate" in effective_args and "fx_rate" in raw_out:
                    raw_fx = float(raw_out["fx_rate"])
                    arg_fx = float(effective_args["fx_rate"])
                    if abs(raw_fx - arg_fx) >= 1e-4:
                        l2_failed = True
                        l2_reason = (
                            f"GATE_L2_PROVENANCE_MISMATCH: tool_arg fx_rate={arg_fx} "
                            f"!= provenance receipt fx_rate={raw_fx}"
                        )
                if "kyc_status" in effective_args and "kyc_status" in raw_out:
                    if str(effective_args["kyc_status"]) != str(raw_out["kyc_status"]):
                        l2_failed = True
                        l2_reason = (
                            f"GATE_L2_KYC_PROVENANCE_MISMATCH: tool_arg kyc_status={effective_args['kyc_status']} "
                            f"!= provenance receipt kyc_status={raw_out['kyc_status']}"
                        )

            # Verify Gate L3 (Domain Math & Compliance Invariants) on tool_args
            l3_ok, l3_reason = validate_tool_args_gate_l3(tool_name, effective_args)

            if is_caller_tainted or tainted_branch_agents or l2_failed or not l3_ok:
                origin_agent = agent_id
                if tainted_branch_agents:
                    chk_id = self.C5.latest_by_branch.get(branch_id)
                    since_ts = (
                        self.C5.snapshots[(branch_id, chk_id)]["timestamp"]
                        if chk_id and (branch_id, chk_id) in self.C5.snapshots
                        else 0.0
                    )
                    branch_edges = [
                        e
                        for e in self.C4.dag_edges
                        if e["timestamp"] >= since_ts
                        and e["sender_id"] in tainted_branch_agents
                    ]
                    if branch_edges:
                        origin_agent = branch_edges[0]["sender_id"]
                    else:
                        origin_agent = tainted_branch_agents[0]

                reasons: list[str] = []
                for t_agent in tainted_branch_agents:
                    if (
                        t_agent in self.C4.taint_reasons
                        and self.C4.taint_reasons[t_agent] not in reasons
                    ):
                        reasons.append(self.C4.taint_reasons[t_agent])
                if l2_reason and l2_reason not in reasons:
                    reasons.append(l2_reason)
                if l3_reason and l3_reason not in reasons:
                    reasons.append(l3_reason)
                combined_reason = (
                    "; ".join(reasons)
                    if reasons
                    else "GATE_L4_PRE_COMMIT_TAINT_BLOCKED"
                )

                if agent_id in self.C2.budget_ledger:
                    self.C2.budget_ledger[agent_id]["wasted_tokens_on_failure"] += 60

                recovery_report = await self.isolate_and_rollback(
                    failed_agent_id=origin_agent,
                    failure_reason=combined_reason,
                )
                return {
                    "status": "PRE_COMMIT_BLOCKED_AND_ROLLED_BACK",
                    "http_code": 409,
                    "recovery_report": recovery_report,
                }

        # 6. Task & Socket Registration (C3)
        client = httpx.AsyncClient()
        current_task = asyncio.current_task()
        if agent_id in self.C3.process_table:
            self.C3.process_table[agent_id]["task_handle"] = current_task
            self.C3.process_table[agent_id]["http_client"] = client
            self.C3.process_table[agent_id]["status"] = "IN_FLIGHT_TOOL"
            self.C3.process_table[agent_id]["last_heartbeat"] = time.perf_counter()

        provider_chain = TOOL_PROVIDER_CHAINS.get(tool_name, ["primary", "fallback"])
        provider_key = "wire_network" if tool_name == "swift:wire" else "provider"
        requested_provider = str(effective_args.get(provider_key, provider_chain[0]))
        next_fallback = (
            provider_chain[1] if len(provider_chain) > 1 else "reuters_fallback"
        )
        if (
            requested_provider in provider_chain
            and provider_chain.index(requested_provider) + 1 < len(provider_chain)
        ):
            next_fallback = provider_chain[provider_chain.index(requested_provider) + 1]

        try:
            # 7. Chaos Infrastructure Fault Hook (C6) — Supports Simultaneous Multi-Tool & Multi-Hop Outages
            svc_rule = self.C6.get_active_rule(
                tool_name, "SERVICE_UNAVAILABLE"
            ) or self.C6.get_active_rule(agent_id, "SERVICE_UNAVAILABLE")
            if svc_rule is not None and requested_provider != provider_chain[-1]:
                self.C6.consume_rule(svc_rule)
                self.C6.record_tool_outage(
                    tool_name,
                    requested_provider,
                    "HTTP 503 Service Unavailable (Simulated Outage)",
                )
                if agent_id in self.C2.budget_ledger:
                    self.C2.budget_ledger[agent_id]["wasted_tokens_on_failure"] += 30
                self.failover_events.append(
                    {
                        "tool_name": tool_name,
                        "agent_id": agent_id,
                        "failed_provider": requested_provider,
                        "recommended_fallback": next_fallback,
                        "http_code": 503,
                        "timestamp": time.time(),
                    }
                )
                return {
                    "status": "ERROR_503",
                    "http_code": 503,
                    "tool_name": tool_name,
                    "failed_provider": requested_provider,
                    "recommended_fallback": next_fallback,
                    "error": f"HTTP 503 Service Unavailable on {tool_name} ({requested_provider})",
                }

            net_rule = self.C6.get_active_rule(
                tool_name, "NETWORK_OUTAGE"
            ) or self.C6.get_active_rule(agent_id, "NETWORK_OUTAGE")
            if net_rule is not None:
                self.C6.consume_rule(net_rule)
                sim_latency = float(effective_args.get("simulate_latency_sec", 0.002))
                fast_failover = bool(
                    net_rule.get("fault_config", {}).get(
                        "fast_failover", sim_latency < 1.0
                    )
                )
                if fast_failover:
                    await asyncio.sleep(0.01)
                    self.C6.record_tool_outage(
                        tool_name,
                        requested_provider,
                        "HTTP 504 Gateway Timeout (Simulated Network Outage)",
                    )
                    if agent_id in self.C2.budget_ledger:
                        self.C2.budget_ledger[agent_id][
                            "wasted_tokens_on_failure"
                        ] += 35
                    self.failover_events.append(
                        {
                            "tool_name": tool_name,
                            "agent_id": agent_id,
                            "failed_provider": requested_provider,
                            "recommended_fallback": next_fallback,
                            "http_code": 504,
                            "timestamp": time.time(),
                        }
                    )
                    return {
                        "status": "ERROR_504_TIMEOUT",
                        "http_code": 504,
                        "tool_name": tool_name,
                        "failed_provider": requested_provider,
                        "recommended_fallback": next_fallback,
                        "error": f"HTTP 504 Network Timeout on {tool_name} ({requested_provider})",
                    }
                else:
                    await asyncio.sleep(5.0)

            # 8. Execute Underlying Mock/Real Tool & Record Provenance Receipt (C4)
            raw_output = await execute_mock_tool(tool_name, effective_args)
            self.C6.record_tool_success(tool_name, requested_provider)

            if ledger is not None:
                ledger["tokens_used"] += 100
                ledger["calls_made"] += 1

            # Record cryptographic provenance receipt for read tools
            if not tool_policy.get("mutates_state", False):
                self.C4.record_tool_receipt(trace_id, tool_name, raw_output)

            if (
                agent_id in self.C3.process_table
                and self.C3.process_table[agent_id]["status"] == "IN_FLIGHT_TOOL"
            ):
                self.C3.process_table[agent_id]["status"] = "RUNNING"
                self.C3.process_table[agent_id]["last_heartbeat"] = time.perf_counter()

            return {
                "status": "SUCCESS",
                "http_code": 200,
                "data": raw_output,
                "trace_id": trace_id,
            }
        finally:
            if not client.is_closed:
                await client.aclose()

    # =========================================================================
    # Method 4: A2A Message Router & Gates L1–L3 Optimistic Taint Evaluator
    # =========================================================================
    async def route_a2a_message(
        self,
        sender_jwt: str,
        receiver_id: str,
        payload: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        """
        Route Agent-to-Agent messages through C4 Causal Lineage Tracker and
        evaluate Gates L1 (Schema), L2 (HMAC Provenance), and L3 (Domain Math & Compliance).
        """
        # 1. Verify sender_jwt via C1
        claims = self.C1.verify_jwt(sender_jwt)
        sender_id = claims["sub"]
        routed_payload = copy.deepcopy(payload)

        # 2. Chaos Semantic Corruption Hook (C6) — Supports Branch A (FX) and Branch B (KYC)
        corrupt_rule = self.C6.get_active_rule(sender_id, "CORRUPTED_OUTPUT")
        if corrupt_rule is not None:
            self.C6.consume_rule(corrupt_rule)
            if "fx_rate" in routed_payload or sender_id in (
                "FX_Quote_Reader",
                "FX_Settlement_Supervisor",
            ):
                multiplier = float(
                    corrupt_rule.get("fault_config", {}).get("multiplier", 10.0)
                )
                base_fx = float(routed_payload.get("fx_rate", 0.9200))
                base_usd = float(routed_payload.get("amount_usd", 2_000_000.0))
                corrupted_fx = round(base_fx * multiplier, 4)
                corrupted_eur = round(base_usd * corrupted_fx, 2)
                routed_payload["fx_rate"] = corrupted_fx
                routed_payload["settlement_eur"] = corrupted_eur
            elif "kyc_status" in routed_payload or sender_id in (
                "Sanctions_Screener",
                "Compliance_Supervisor",
            ):
                routed_payload["kyc_status"] = "SANCTIONED_BYPASS_CORRUPTED"

        # 3. Causal Edge Recording (C4)
        self.C4.vector_clock[sender_id] = self.C4.vector_clock.get(sender_id, 0) + 1
        self.C4.vector_clock[receiver_id] = (
            max(
                self.C4.vector_clock.get(receiver_id, 0),
                self.C4.vector_clock[sender_id],
            )
            + 1
        )
        edge = {
            "edge_id": f"edge_{len(self.C4.dag_edges) + 1}_{uuid4().hex[:6]}",
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "trace_id": trace_id,
            "vector_clock": copy.deepcopy(self.C4.vector_clock),
            "timestamp": time.perf_counter(),
            "payload_summary": copy.deepcopy(routed_payload),
        }
        self.C4.dag_edges.append(edge)

        # 4. 4-Layer Corruption Verification Pipeline (Gates L1, L2, L3)
        failure_reasons: list[str] = []

        # Propagate upstream taint if sender is already tainted
        if self.C4.taint_status.get(sender_id, False):
            upstream_reason = self.C4.taint_reasons.get(
                sender_id, f"CAUSAL_TAINT_PROPAGATED_FROM_{sender_id}"
            )
            failure_reasons.append(upstream_reason)

        # Gate L1: Syntactic Schema Check
        parsed_model: Optional[A2AMessagePayload] = None
        try:
            parsed_model = A2AMessagePayload.model_validate(routed_payload)
        except ValidationError as exc:
            failure_reasons.append(f"GATE_L1_SCHEMA_VIOLATION: {exc}")

        # Gate L2: Cryptographic Provenance Check
        receipt = self.C4.provenance_receipts.get(trace_id)
        if receipt is not None:
            raw_out = receipt.get("raw_output", {})
            if "fx_rate" in routed_payload and "fx_rate" in raw_out:
                receipt_fx = raw_out["fx_rate"]
                if abs(float(receipt_fx) - float(routed_payload["fx_rate"])) >= 1e-4:
                    msg = (
                        f"GATE_L2_PROVENANCE_MISMATCH: payload fx_rate={routed_payload['fx_rate']} "
                        f"!= HMAC receipt fx_rate={receipt_fx}"
                    )
                    if msg not in failure_reasons:
                        failure_reasons.append(msg)
            if "kyc_status" in routed_payload and "kyc_status" in raw_out:
                if str(routed_payload["kyc_status"]) != str(raw_out["kyc_status"]):
                    msg = (
                        f"GATE_L2_KYC_PROVENANCE_MISMATCH: payload kyc_status={routed_payload['kyc_status']} "
                        f"!= HMAC receipt kyc_status={raw_out['kyc_status']}"
                    )
                    if msg not in failure_reasons:
                        failure_reasons.append(msg)

        # Gate L3: Deterministic Domain Math & Compliance Invariant
        if parsed_model is not None:
            l3_ok, l3_reason = parsed_model.verify_gate_l3_invariants(
                max_settlement_eur=2_500_000.0
            )
            if not l3_ok and l3_reason:
                if l3_reason not in failure_reasons:
                    failure_reasons.append(l3_reason)

        # 5. Optimistic Taint Marking (C4)
        is_tainted = len(failure_reasons) > 0
        combined_reason = "; ".join(failure_reasons) if is_tainted else None

        if is_tainted and combined_reason:
            self.C4.taint_status[sender_id] = True
            self.C4.taint_status[receiver_id] = True
            self.C4.taint_reasons.setdefault(sender_id, combined_reason)
            self.C4.taint_reasons[receiver_id] = combined_reason
            if sender_id in self.C3.process_table:
                self.C3.process_table[sender_id]["status"] = "TAINTED"
            if receiver_id in self.C3.process_table:
                self.C3.process_table[receiver_id]["status"] = "TAINTED"

        return {
            "status": "DELIVERED_OPTIMISTIC",
            "tainted": is_tainted,
            "reason": combined_reason,
            "payload": routed_payload,
        }

    # =========================================================================
    # Method 5: Chaos Fault Injection (Single & Multi-Target Batch)
    # =========================================================================
    def inject_chaos_fault(
        self,
        target: str,
        fault_category: str,
        fault_config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Validate fault_category and register active fault rule in C6."""
        if fault_category not in VALID_FAULT_CATEGORIES:
            raise ValueError(
                f"INVALID_FAULT_CATEGORY: {fault_category}. Must be one of {sorted(VALID_FAULT_CATEGORIES)}"
            )
        return self.C6.set_rule(
            target=target,
            fault_category=fault_category,
            fault_config=fault_config,
        )

    def inject_multi_chaos_faults(
        self,
        faults: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Inject faults across multiple agents and multiple tools simultaneously."""
        for f in faults:
            cat = f.get("fault_category", "")
            if cat not in VALID_FAULT_CATEGORIES:
                raise ValueError(
                    f"INVALID_FAULT_CATEGORY: {cat}. Must be one of {sorted(VALID_FAULT_CATEGORIES)}"
                )
        return self.C6.set_batch_rules(faults)

    # =========================================================================
    # Method 6: Causal BFS Taint Isolation & Subtree Checkpoint Rollback
    # =========================================================================
    async def isolate_and_rollback(
        self,
        failed_agent_id: str,
        failure_reason: str,
    ) -> dict[str, Any]:
        """
        Compute causal BFS taint set T_fault (supporting multiple simultaneous tainted roots),
        quarantine tainted agents in C1/C3, restore latest clean branch checkpoint from C5,
        and verify R_blast = 1.0 under per-branch lock protection.
        """
        if failed_agent_id not in self.C3.process_table:
            raise KeyError(f"Agent {failed_agent_id} not found in C3.process_table")
        branch_id = self.C3.process_table[failed_agent_id]["branch_id"]
        branch_lock = self._rollback_locks.setdefault(branch_id, asyncio.Lock())

        async with branch_lock:
            # 1. Start recovery timer
            t_start = time.perf_counter()

            # 2. Lookup latest clean checkpoint for branch_id
            chk_id = self.C5.latest_by_branch.get(branch_id)
            if not chk_id or (branch_id, chk_id) not in self.C5.snapshots:
                raise RuntimeError(f"No clean checkpoint found for branch {branch_id}")
            chk_data = self.C5.snapshots[(branch_id, chk_id)]

            # 3. Multi-Origin Causal BFS Taint Calculation (C4)
            tainted_in_branch = [
                a_id
                for a_id, info in self.C3.process_table.items()
                if info.get("branch_id") == branch_id
                and self.C4.taint_status.get(a_id, False)
            ]
            origins = list(dict.fromkeys([failed_agent_id] + tainted_in_branch))
            t_fault_raw: set[str] = self.C4.compute_multi_origin_tainted_bfs_set(
                origin_agent_ids=origins,
                since_timestamp=chk_data["timestamp"],
            )
            for a_id in tainted_in_branch:
                t_fault_raw.add(a_id)

            # Verify T_fault does not bleed into other branches
            other_branch_nodes = {
                a_id
                for a_id, info in self.C3.process_table.items()
                if info.get("branch_id") != branch_id
            }
            cross_branch_leakage = t_fault_raw & other_branch_nodes
            assert (
                not cross_branch_leakage
            ), f"Blast radius containment breach: {cross_branch_leakage}"
            t_fault = t_fault_raw - other_branch_nodes

            # 4. Surgical Quarantine (C1, C3)
            revoked_list: list[str] = []
            current_task = asyncio.current_task()
            for agent_id in sorted(t_fault):
                proc = self.C3.process_table.get(agent_id)
                if proc is not None:
                    jti = proc.get("jti")
                    if jti:
                        self.C1.revoke_jti(jti)
                        revoked_list.append(jti)

                    task_handle: Optional[asyncio.Task[Any]] = proc.get("task_handle")
                    if (
                        task_handle is not None
                        and not task_handle.done()
                        and task_handle is not current_task
                    ):
                        task_handle.cancel()

                    http_client: Optional[httpx.AsyncClient] = proc.get("http_client")
                    if http_client is not None and not http_client.is_closed:
                        await http_client.aclose()

                    proc["status"] = "RESTORED"
                    proc["last_heartbeat"] = time.perf_counter()

                self.C4.taint_status[agent_id] = False
                self.C4.taint_reasons.pop(agent_id, None)

            # 5. Restore Clean Snapshot (C5) & Compute R_blast
            restored_state_dict = json.loads(
                chk_data["serialized_state_blob"].decode("utf-8")
            )
            total_other_count = max(len(other_branch_nodes), 1)
            blast_radius_ratio = round(
                1.0 - (len(cross_branch_leakage) / total_other_count), 4
            )

            mttr_seconds = round(time.perf_counter() - t_start, 4)
            self.last_blast_radius_ratio = blast_radius_ratio
            self.last_mttr_seconds = mttr_seconds

            fallback_prov = (
                "reuters_fallback"
                if branch_id == "Branch_A"
                else "worldcheck_fallback"
            )
            report = {
                "failed_agent_id": failed_agent_id,
                "failure_reason": failure_reason,
                "branch_rolled_back": branch_id,
                "restored_checkpoint_id": chk_id,
                "tainted_bfs_nodes": sorted(t_fault),
                "revoked_jtis": revoked_list,
                "restored_state": restored_state_dict,
                "fallback_provider": fallback_prov,
                "blast_radius_ratio": blast_radius_ratio,
                "mttr_seconds": mttr_seconds,
            }
            self.recovery_history.append(report)
            return report

    async def batch_isolate_and_rollback(
        self,
        failed_agents: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Concurrently isolate and rollback multiple failed agents/branches in parallel."""
        tasks = [
            self.isolate_and_rollback(agent_id, reason)
            for agent_id, reason in failed_agents
        ]
        return list(await asyncio.gather(*tasks))

    # =========================================================================
    # Method 7: Multi-Granular Kill-Switch (SURGICAL, SOFT_FREEZE, GLOBAL_HARD)
    # =========================================================================
    async def trigger_kill_switch(
        self,
        mode: str,
        target_id: str,
        operator_override_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Execute multi-granular kill-switch:
          - SURGICAL: Revoke target(s) + recursive children JTIs, cancel tasks, close sockets.
                      Supports comma-separated multi-agent targets (e.g. 'FX_Quote_Reader,Sanctions_Screener').
          - SOFT_FREEZE: Pause branch via asyncio.Event or resume with human operator override.
          - GLOBAL_HARD: Bulk-revoke all JTIs, rotate Ed25519 root key, cancel all tasks, close all sockets in <50ms.
        """
        t0 = time.perf_counter()
        current_task = asyncio.current_task()

        killed_agents: list[str] = []
        paused_agents: list[str] = []
        resumed_agents: list[str] = []
        revoked_jtis: list[str] = []
        closed_sockets_count = 0
        inspect_snapshot: Optional[dict[str, Any]] = None

        if mode == "SURGICAL":
            raw_targets = [t.strip() for t in target_id.split(",") if t.strip()]
            targets: list[str] = []
            for t_id in raw_targets:
                rec = self.C3.get_recursive_children(t_id)
                if not rec and t_id in self.C3.process_table:
                    rec = [t_id]
                for item in rec:
                    if item not in targets:
                        targets.append(item)

            for a_id in targets:
                proc = self.C3.process_table.get(a_id)
                if proc is None:
                    continue
                jti = proc.get("jti")
                if jti:
                    self.C1.revoke_jti(jti)
                    revoked_jtis.append(jti)

                task_handle: Optional[asyncio.Task[Any]] = proc.get("task_handle")
                if (
                    task_handle is not None
                    and not task_handle.done()
                    and task_handle is not current_task
                ):
                    task_handle.cancel()

                http_client: Optional[httpx.AsyncClient] = proc.get("http_client")
                if http_client is not None and not http_client.is_closed:
                    await http_client.aclose()
                    closed_sockets_count += 1

                proc["status"] = "KILLED"
                killed_agents.append(a_id)

        elif mode == "SOFT_FREEZE":
            if target_id not in self.C3.branch_freeze_events:
                ev = asyncio.Event()
                ev.set()
                self.C3.branch_freeze_events[target_id] = ev

            if operator_override_state is None:
                # Pause target branch at the proxy gate
                self.C3.branch_freeze_events[target_id].clear()
                for a_id, proc in self.C3.process_table.items():
                    if target_id == "ALL" or proc.get("branch_id") == target_id:
                        proc["status"] = "PAUSED"
                        paused_agents.append(a_id)

                chk_id = self.C5.latest_by_branch.get(target_id)
                if chk_id and (target_id, chk_id) in self.C5.snapshots:
                    snap = self.C5.snapshots[(target_id, chk_id)]
                    inspect_snapshot = json.loads(
                        snap["serialized_state_blob"].decode("utf-8")
                    )
            else:
                # Apply human operator override and resume execution
                self.C3.operator_overrides[target_id] = copy.deepcopy(
                    operator_override_state
                )
                self.C3.branch_freeze_events[target_id].set()
                for a_id, proc in self.C3.process_table.items():
                    if target_id == "ALL" or proc.get("branch_id") == target_id:
                        if proc.get("status") == "PAUSED":
                            proc["status"] = "RUNNING"
                        resumed_agents.append(a_id)

        elif mode == "GLOBAL_HARD":
            # Step 1 (t_revoke): Add every active JTI to revocation_set and rotate C1._private_key
            revoked_jtis = self.C1.bulk_revoke_all_and_rotate_key()

            # Step 2 (t_cancel) & Step 3 (t_socket_close)
            clients_to_close: list[httpx.AsyncClient] = []
            for a_id, proc in self.C3.process_table.items():
                jti = proc.get("jti")
                if jti and jti not in self.C1.revocation_set:
                    self.C1.revoke_jti(jti)
                    revoked_jtis.append(jti)

                task_handle = proc.get("task_handle")
                if (
                    task_handle is not None
                    and not task_handle.done()
                    and task_handle is not current_task
                ):
                    task_handle.cancel()

                http_client = proc.get("http_client")
                if http_client is not None and not http_client.is_closed:
                    clients_to_close.append(http_client)

                proc["status"] = "KILLED"
                killed_agents.append(a_id)

            if clients_to_close:
                await asyncio.gather(
                    *(client.aclose() for client in clients_to_close),
                    return_exceptions=True,
                )
                closed_sockets_count = len(clients_to_close)
        else:
            raise ValueError(f"Unsupported kill-switch mode: {mode}")

        termination_latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        self.last_kill_latency_ms = termination_latency_ms

        receipt = {
            "mode": mode,
            "target_id": target_id,
            "killed_agents": killed_agents,
            "paused_agents": paused_agents,
            "resumed_agents": resumed_agents,
            "revoked_jtis": revoked_jtis,
            "closed_sockets_count": closed_sockets_count,
            "termination_latency_ms": termination_latency_ms,
            "inspect_snapshot": inspect_snapshot,
            "operator_override_applied": operator_override_state,
        }
        self.kill_switch_history.append(receipt)
        return receipt

    # =========================================================================
    # State & Evaluation Metrics Inspection Helper
    # =========================================================================
    def get_full_state_and_metrics(self) -> dict[str, Any]:
        """Return live state of C1–C6 and the 5 formal evaluation metrics plus multi-failure stats."""
        total_tokens = sum(
            b.get("tokens_used", 0) + b.get("wasted_tokens_on_failure", 0)
            for b in self.C2.budget_ledger.values()
        )
        wasted_tokens = sum(
            b.get("wasted_tokens_on_failure", 0)
            for b in self.C2.budget_ledger.values()
        )
        wasted_overhead_pct = (
            round((wasted_tokens / total_tokens) * 100.0, 2)
            if total_tokens > 0
            else 0.0
        )

        return {
            "metrics": {
                "kill_switch_latency_ms": self.last_kill_latency_ms,
                "blast_radius_ratio": self.last_blast_radius_ratio,
                "blast_radius_pct": round(self.last_blast_radius_ratio * 100.0, 2),
                "rbac_block_rate_pct": self.C2.rbac_block_rate,
                "mttr_seconds": self.last_mttr_seconds,
                "wasted_token_overhead_pct": wasted_overhead_pct,
                "total_failover_events": len(self.failover_events),
                "total_agent_respawns": len(self.agent_respawn_events),
                "total_subtree_rollbacks": len(self.recovery_history),
            },
            "C1_iam_engine": {
                "revocation_set": sorted(self.C1.revocation_set),
                "active_tokens": self.C1.active_tokens,
                "agent_current_jti": self.C1.agent_current_jti,
            },
            "C2_policy_store": {
                "rbac_matrix": self.C2.rbac_matrix,
                "budget_ledger": self.C2.budget_ledger,
                "rbac_block_rate": self.C2.rbac_block_rate,
            },
            "C3_task_registry": {
                "process_table": self.C3.serialize_process_table(),
                "branch_freeze_states": {
                    k: ev.is_set() for k, ev in self.C3.branch_freeze_events.items()
                },
                "operator_overrides": self.C3.operator_overrides,
            },
            "C4_lineage_graph": {
                "vector_clock": self.C4.vector_clock,
                "dag_edges": self.C4.dag_edges,
                "taint_status": self.C4.taint_status,
                "taint_reasons": self.C4.taint_reasons,
                "provenance_receipts": self.C4.provenance_receipts,
            },
            "C5_snapshot_db": {
                "latest_by_branch": self.C5.latest_by_branch,
                "sql_snapshot_count": self.C5.get_sql_snapshot_count(),
                "snapshots": self.C5.serialize_snapshots(),
            },
            "C6_chaos_monkey": {
                "active_fault_rules": self.C6.active_fault_rules,
                "fault_queues": self.C6.fault_queues,
                "tool_health_registry": self.C6.tool_health_registry,
            },
            "recovery_history": self.recovery_history,
            "kill_switch_history": self.kill_switch_history,
            "failover_events": self.failover_events,
            "agent_respawn_events": self.agent_respawn_events,
        }


# Global singleton ControlPlaneKernel instance
kernel_singleton = ControlPlaneKernel()
