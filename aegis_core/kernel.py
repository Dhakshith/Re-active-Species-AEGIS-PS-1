"""Domain-agnostic ControlPlaneKernel implementing the 7 Core Methods and Gates L1–L4."""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
from typing import Any, Callable, Optional, Union

import httpx

from aegis_core.components import (
    C1_IAM_Engine,
    C2_Policy_Store,
    C3_Task_Registry,
    C4_Lineage_Graph,
    C5_Snapshot_DB,
    C6_Chaos_Monkey,
)
from aegis_core.models import (
    AegisControlPlaneError,
    ChaosFaultRule,
    KillSwitchReceipt,
    NetworkOutageFaultError,
    PreCommitGateError,
    PrivilegeEscalationError,
    ServiceUnavailableFaultError,
    TokenRevokedOrInvalidError,
    ToolRegistrationSpec,
)


class ControlPlaneKernel:
    """
    Out-of-Band Governance Control Plane Kernel (100% Domain-Agnostic).
    Encapsulates C1–C6 and enforces Zero-Trust Identity, RBAC, Gates L1–L4,
    Causal BFS Taint Quarantine, Versioned Checkpointing, and Sub-50ms Kill-Switches.
    """

    def __init__(self, db_path: str = "aegis_snapshots.db") -> None:
        self.db_path = db_path
        self.c1 = C1_IAM_Engine()
        self.c2 = C2_Policy_Store()
        self.c3 = C3_Task_Registry()
        self.c4 = C4_Lineage_Graph()
        self.c5 = C5_Snapshot_DB(db_path=self.db_path)
        self.c6 = C6_Chaos_Monkey()

        # Uppercase aliases
        self.C1 = self.c1
        self.C2 = self.c2
        self.C3 = self.c3
        self.C4 = self.c4
        self.C5 = self.c5
        self.C6 = self.c6

        self.event_log: list[dict[str, Any]] = []
        self.recovery_history: list[dict[str, Any]] = []
        self.kill_switch_history: list[dict[str, Any]] = []
        self.last_kill_latency_ms: float = 0.0
        self.last_blast_radius_ratio: float = 1.0
        self.last_mttr_seconds: float = 0.0

    def reset_state(self, preserve_registered_tools: bool = True) -> None:
        """Reset runtime state across C1–C6 between scenario runs while optionally preserving registered domain tools/gates."""
        saved_tools = copy.deepcopy(self.c2.registered_tools)
        saved_handlers = dict(self.c2.tool_handlers)
        saved_l1 = dict(self.c2.gate_l1_validators)
        saved_l3 = dict(self.c2.gate_l3_validators)

        self.c1 = C1_IAM_Engine()
        self.c2 = C2_Policy_Store()
        self.c3 = C3_Task_Registry()
        self.c4 = C4_Lineage_Graph()
        self.c5.clear_all()
        self.c6 = C6_Chaos_Monkey()

        if preserve_registered_tools:
            self.c2.registered_tools = saved_tools
            self.c2.tool_handlers = saved_handlers
            self.c2.gate_l1_validators = saved_l1
            self.c2.gate_l3_validators = saved_l3

        self.C1 = self.c1
        self.C2 = self.c2
        self.C3 = self.c3
        self.C4 = self.c4
        self.C5 = self.c5
        self.C6 = self.c6

        self.event_log.clear()
        self.recovery_history.clear()
        self.kill_switch_history.clear()
        self.last_kill_latency_ms = 0.0
        self.last_blast_radius_ratio = 1.0
        self.last_mttr_seconds = 0.0

    def _log_event(
        self,
        event_type: str,
        agent_id: str,
        branch_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.event_log.append(
            {
                "event_id": len(self.event_log) + 1,
                "timestamp": time.time(),
                "event_type": event_type,
                "agent_id": agent_id,
                "branch_id": branch_id,
                "message": message,
                "details": details or {},
            }
        )

    def _resolve_claims(
        self, token_or_claims: Union[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if isinstance(token_or_claims, str):
            return self.c1.verify_jwt(token_or_claims)
        if isinstance(token_or_claims, dict):
            jti = token_or_claims.get("jti")
            if jti and jti in self.c1.revocation_set:
                raise TokenRevokedOrInvalidError(f"Token jti '{jti}' is revoked")
            return token_or_claims
        raise TokenRevokedOrInvalidError("Invalid agent claims format")

    def register_tool(
        self,
        spec: ToolRegistrationSpec,
        handler: Optional[Callable[..., Any]] = None,
    ) -> ToolRegistrationSpec:
        return self.c2.register_tool(spec, handler)

    # =========================================================================
    # Root Bootstrap Helper
    # =========================================================================
    def mint_root_token(
        self,
        agent_id: str = "Root_Orchestrator",
        role: str = "L0_Orchestrator",
        scopes: Optional[list[str]] = None,
        token_budget: int = 10000,
        branch_id: str = "Root",
    ) -> dict[str, Any]:
        effective_scopes = (
            scopes
            if scopes is not None
            else sorted(
                {spec.required_scope for spec in self.c2.registered_tools.values()}
            )
        )
        token_str, claims = self.c1.mint_root_token(
            agent_id=agent_id,
            role=role,
            scopes=effective_scopes,
            token_budget=token_budget,
            branch_id=branch_id,
        )
        self.c2.init_budget(agent_id, max_tokens=token_budget)
        self.c3.register_agent(
            agent_id=agent_id,
            jti=claims["jti"],
            role=role,
            parent_id=None,
            branch_id=branch_id,
            status="RUNNING",
        )
        self.c4.taint_status[agent_id] = False
        self._log_event(
            "MINT_ROOT_TOKEN",
            agent_id,
            branch_id,
            f"Minted root Ed25519 JWT for '{agent_id}' with scopes={effective_scopes}",
            {"jti": claims["jti"], "scopes": effective_scopes},
        )
        return {"jwt_token": token_str, "claims": claims}

    # =========================================================================
    # Method 1: mint_delegated_token
    # =========================================================================
    def mint_delegated_token(
        self,
        parent_claims: Union[str, dict[str, Any]],
        child_agent_id: str,
        requested_role: str,
        requested_scopes: list[str],
        token_budget: int,
        branch_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_parent = self._resolve_claims(parent_claims)
        parent_id = resolved_parent["sub"]
        parent_remaining = self.c2.get_remaining_budget(
            parent_id, fallback=int(resolved_parent.get("token_budget", 10000))
        )
        effective_branch = branch_id or resolved_parent.get("branch_id", "Root")

        try:
            token_str, child_claims = self.c1.mint_delegated_token(
                parent_claims=resolved_parent,
                child_agent_id=child_agent_id,
                requested_role=requested_role,
                requested_scopes=requested_scopes,
                token_budget=token_budget,
                branch_id=effective_branch,
                parent_remaining_budget=parent_remaining,
            )
        except PrivilegeEscalationError as exc:
            self.c2.unauthorized_attempts += 1
            self.c2.unauthorized_blocked += 1
            self._log_event(
                "DELEGATION_BLOCKED_403",
                parent_id,
                effective_branch,
                str(exc),
                exc.details,
            )
            raise

        self.c2.init_budget(child_agent_id, max_tokens=token_budget)
        self.c3.register_agent(
            agent_id=child_agent_id,
            jti=child_claims["jti"],
            role=requested_role,
            parent_id=parent_id,
            branch_id=effective_branch,
            status="READY",
        )
        self.c4.taint_status[child_agent_id] = False
        self.c4.taint_reasons.pop(child_agent_id, None)

        self._log_event(
            "MINT_DELEGATED_TOKEN",
            child_agent_id,
            effective_branch,
            f"Delegated token from '{parent_id}' -> '{child_agent_id}' (scopes={requested_scopes})",
            {"jti": child_claims["jti"], "parent_id": parent_id},
        )
        return {"jwt_token": token_str, "claims": child_claims}

    # =========================================================================
    # Method 2: create_checkpoint
    # =========================================================================
    def create_checkpoint(
        self,
        agent_claims: Union[str, dict[str, Any]],
        branch_id: str,
        current_graph_state: dict[str, Any],
        checkpoint_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_claims(agent_claims)
        agent_id = resolved["sub"]
        vc = self.c4.vector_clocks.get(branch_id, {})

        record = self.c5.save_checkpoint(
            branch_id=branch_id,
            agent_id=agent_id,
            state_blob=current_graph_state,
            vector_clock=vc,
            taint_status=self.c4.taint_status,
            checkpoint_id=checkpoint_id,
        )
        self._log_event(
            "CREATE_CHECKPOINT",
            agent_id,
            branch_id,
            f"Saved clean checkpoint '{record['checkpoint_id']}' for branch '{branch_id}'",
            {"checkpoint_id": record["checkpoint_id"]},
        )
        return record

    # =========================================================================
    # Method 3: intercept_tool_call
    # =========================================================================
    async def intercept_tool_call(
        self,
        agent_claims: Union[str, dict[str, Any]],
        tool_name: str,
        tool_args: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        # 1. C1 Identity & Revocation Check
        resolved = self._resolve_claims(agent_claims)
        agent_id = resolved["sub"]
        branch_id = resolved.get("branch_id", "Root")
        effective_args = copy.deepcopy(tool_args)

        # 2. C3 Soft-Freeze Gate & Operator Override Merge
        await self.c3.wait_if_frozen(branch_id, agent_id)
        if branch_id == "Root":
            await self.c3.wait_if_frozen("Branch_A", agent_id)
        branch_override = self.c3.branch_overrides.get(branch_id) or (
            self.c3.branch_overrides.get("Branch_A") if branch_id == "Root" else None
        )
        if branch_override:
            effective_args.update(copy.deepcopy(branch_override))
            self.c4.update_provenance_on_override(branch_override, trace_id=trace_id)

        # 3. C6 Privilege Escalation Chaos Redirection
        priv_rule = self.c6.get_rule(agent_id, "PRIVILEGE_ESCALATION")
        if priv_rule is not None:
            self.c6.consume_rule(priv_rule)
            forced_tool = priv_rule.config.get(
                "unauthorized_tool"
            ) or priv_rule.config.get("forced_tool")
            if not forced_tool:
                # Pick any registered tool whose required_scope is NOT in caller's scopes
                caller_scopes = set(resolved.get("scopes", []))
                for cand_name, cand_spec in self.c2.registered_tools.items():
                    if cand_spec.required_scope not in caller_scopes:
                        forced_tool = cand_name
                        break
            if forced_tool:
                tool_name = str(forced_tool)
            if "forced_args" in priv_rule.config and isinstance(
                priv_rule.config["forced_args"], dict
            ):
                effective_args = copy.deepcopy(priv_rule.config["forced_args"])

        # Lookup registered tool specification
        if tool_name not in self.c2.registered_tools:
            raise AegisControlPlaneError(
                f"Tool '{tool_name}' is not registered in C2_Policy_Store",
                http_code=404,
                error_code="HTTP_404_TOOL_NOT_REGISTERED",
            )
        tool_spec = self.c2.registered_tools[tool_name]

        # 4. C2 RBAC Scope Check & Budget Check
        try:
            self.c2.verify_scope_and_budget(resolved, tool_spec)
        except PrivilegeEscalationError as exc:
            if agent_id in self.c3.process_table:
                self.c3.process_table[agent_id]["status"] = "BLOCKED_403"
            self._log_event(
                "RBAC_BLOCKED_403",
                agent_id,
                branch_id,
                str(exc),
                exc.details,
            )
            raise
        except Exception as exc:
            if agent_id in self.c3.process_table:
                self.c3.process_table[agent_id]["status"] = "BLOCKED_429"
            self._log_event(
                "RATE_OR_BUDGET_BLOCKED_429",
                agent_id,
                branch_id,
                str(exc),
                getattr(exc, "details", {}),
            )
            raise

        # 5. Gate L4 (Pre-Commit Taint, Provenance L2 & Invariant L3 Gate)
        if tool_spec.mutates_state:
            gate_failures: list[str] = []

            # 5a. Check if caller or any node in branch_id is tainted in C4
            branch_tainted_nodes = [
                a_id
                for a_id, proc in self.c3.process_table.items()
                if proc.get("branch_id") == branch_id
                and self.c4.taint_status.get(a_id, False)
            ]
            if self.c4.taint_status.get(agent_id, False) and agent_id not in branch_tainted_nodes:
                branch_tainted_nodes.append(agent_id)

            for t_node in branch_tainted_nodes:
                t_reason = self.c4.taint_reasons.get(
                    t_node, f"Node '{t_node}' is marked tainted in C4"
                )
                if t_reason not in gate_failures:
                    gate_failures.append(t_reason)

            # 5b. Run Gate L2 Provenance check on effective_args
            l2_ok, l2_msg = self.c4.verify_provenance(
                trace_id,
                effective_args,
                branch_overrides=branch_override,
            )
            if not l2_ok:
                gate_failures.append(l2_msg)

            # 5c. Run C2 declarative parameter_constraints
            c2_ok, c2_msg = self.c2.check_parameter_constraints(
                tool_spec, effective_args
            )
            if not c2_ok:
                gate_failures.append(f"Gate L3 Parameter Constraint Violation: {c2_msg}")

            # 5d. Run registered Gate L3 domain invariant validators
            for inv_name, inv_fn in self.c2.gate_l3_validators.items():
                inv_ok, inv_msg = inv_fn(effective_args)
                if not inv_ok:
                    gate_failures.append(
                        f"Gate L3 Invariant '{inv_name}' Failed: {inv_msg}"
                    )

            if gate_failures:
                combined_reason = " | ".join(gate_failures)
                # Determine earliest tainted origin in branch
                origin_agent = (
                    branch_tainted_nodes[0] if branch_tainted_nodes else agent_id
                )
                for edge in self.c4.dag_edges:
                    if (
                        edge["branch_id"] == branch_id
                        and edge["sender_id"] in branch_tainted_nodes
                    ):
                        origin_agent = edge["sender_id"]
                        break

                self.c4.taint_status[agent_id] = True
                self.c4.taint_reasons[agent_id] = combined_reason
                if agent_id in self.c3.process_table:
                    self.c3.process_table[agent_id]["status"] = "TAINTED"

                if agent_id in self.c2.budget_ledger:
                    self.c2.budget_ledger[agent_id]["wasted_tokens"] += (
                        tool_spec.token_cost_per_call
                    )

                self._log_event(
                    "GATE_L4_PRE_COMMIT_BLOCK",
                    agent_id,
                    branch_id,
                    f"Gate L4 blocked state-mutating tool '{tool_name}': {combined_reason}",
                    {
                        "tool_name": tool_name,
                        "origin_tainted_agent": origin_agent,
                        "reasons": gate_failures,
                    },
                )
                raise PreCommitGateError(
                    f"Gate L4 Pre-Commit Block on '{tool_name}': {combined_reason}",
                    details={
                        "tool_name": tool_name,
                        "agent_id": agent_id,
                        "branch_id": branch_id,
                        "origin_tainted_agent": origin_agent,
                        "failure_reason": combined_reason,
                        "fallback_tool_name": tool_spec.fallback_tool_name,
                    },
                )

        # 6. Wrap execution in an asyncio.Task & httpx.AsyncClient registered in C3
        http_client = httpx.AsyncClient()
        handler = self.c2.tool_handlers[tool_name]

        async def _run_tool_coroutine() -> Any:
            # 7. C6 SERVICE_UNAVAILABLE or NETWORK_OUTAGE fault checks
            svc_rule = self.c6.get_rule(
                tool_name, "SERVICE_UNAVAILABLE"
            ) or self.c6.get_rule(agent_id, "SERVICE_UNAVAILABLE")
            if svc_rule is not None:
                self.c6.consume_rule(svc_rule)
                self._log_event(
                    "CHAOS_SERVICE_UNAVAILABLE_503",
                    agent_id,
                    branch_id,
                    f"Simulated HTTP 503 Service Unavailable on tool '{tool_name}'",
                    {
                        "tool_name": tool_name,
                        "fallback_tool_name": tool_spec.fallback_tool_name,
                    },
                )
                raise ServiceUnavailableFaultError(
                    f"HTTP 503 Service Unavailable on tool '{tool_name}'",
                    details={
                        "tool_name": tool_name,
                        "fallback_tool_name": tool_spec.fallback_tool_name,
                    },
                )

            net_rule = self.c6.get_rule(
                tool_name, "NETWORK_OUTAGE"
            ) or self.c6.get_rule(agent_id, "NETWORK_OUTAGE")
            if net_rule is not None:
                self.c6.consume_rule(net_rule)
                sleep_sec = float(net_rule.config.get("sleep_seconds", 0.05))
                await asyncio.sleep(sleep_sec)
                if net_rule.config.get("raise_timeout", True):
                    raise NetworkOutageFaultError(
                        f"HTTP 504 Network Outage / Timeout on tool '{tool_name}'",
                        details={
                            "tool_name": tool_name,
                            "fallback_tool_name": tool_spec.fallback_tool_name,
                        },
                    )

            # Call underlying tool handler
            sig = inspect.signature(handler)
            accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            call_kwargs = (
                effective_args
                if accepts_kwargs
                else {
                    k: v
                    for k, v in effective_args.items()
                    if k in sig.parameters
                }
            )
            res = handler(**call_kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res

        task = asyncio.create_task(_run_tool_coroutine())
        if agent_id in self.c3.process_table:
            self.c3.process_table[agent_id]["task_handle"] = task
            self.c3.process_table[agent_id]["http_client"] = http_client
            self.c3.process_table[agent_id]["status"] = "IN_FLIGHT_TOOL"
            self.c3.process_table[agent_id]["last_heartbeat_ts"] = time.time()

        try:
            raw_output = await task
        except ServiceUnavailableFaultError:
            if agent_id in self.c3.process_table:
                self.c3.process_table[agent_id]["status"] = "ERROR_503"
            raise
        except NetworkOutageFaultError:
            if agent_id in self.c3.process_table:
                self.c3.process_table[agent_id]["status"] = "ERROR_504"
            raise
        finally:
            if not http_client.is_closed:
                await http_client.aclose()

        # 8. Deduct budget, record C4 provenance receipt, and apply tool-level C6 CORRUPTED_OUTPUT if active
        self.c2.deduct_budget(agent_id, tool_spec.token_cost_per_call)

        if isinstance(raw_output, dict) and tool_spec.provenance_fields:
            self.c4.record_provenance_receipt(
                trace_id=trace_id,
                tool_name=tool_name,
                tool_output=raw_output,
                provenance_fields=tool_spec.provenance_fields,
                overridden=bool(branch_override),
            )

        # Check if CORRUPTED_OUTPUT is configured directly on the tool (or on agent with target_stage=='tool')
        corrupt_tool_rule = self.c6.get_rule(tool_name, "CORRUPTED_OUTPUT")
        if corrupt_tool_rule is not None and isinstance(raw_output, dict):
            self.c6.consume_rule(corrupt_tool_rule)
            raw_output = self.c6.apply_corruption(raw_output, corrupt_tool_rule.config)

        if (
            agent_id in self.c3.process_table
            and self.c3.process_table[agent_id]["status"] == "IN_FLIGHT_TOOL"
        ):
            self.c3.process_table[agent_id]["status"] = "RUNNING"
            self.c3.process_table[agent_id]["last_heartbeat_ts"] = time.time()

        self._log_event(
            "TOOL_EXECUTED_200",
            agent_id,
            branch_id,
            f"Agent '{agent_id}' executed tool '{tool_name}' cleanly",
            {"tool_name": tool_name, "trace_id": trace_id, "output": raw_output},
        )
        return {
            "status": "SUCCESS",
            "http_code": 200,
            "tool_name": tool_name,
            "trace_id": trace_id,
            "data": raw_output,
        }

    # =========================================================================
    # Method 4: route_a2a_message
    # =========================================================================
    async def route_a2a_message(
        self,
        sender_claims: Union[str, dict[str, Any]],
        receiver_id: str,
        payload: dict[str, Any],
        trace_id: str,
        schema_name: Optional[str] = None,
        optimistic_pipeline: bool = True,
    ) -> dict[str, Any]:
        resolved = self._resolve_claims(sender_claims)
        sender_id = resolved["sub"]
        branch_id = resolved.get("branch_id", "Root")
        routed_payload = copy.deepcopy(payload)

        # Apply C6 CORRUPTED_OUTPUT mutation on payload if active for sender_id
        corrupt_rule = self.c6.get_rule(sender_id, "CORRUPTED_OUTPUT")
        if corrupt_rule is not None:
            self.c6.consume_rule(corrupt_rule)
            routed_payload = self.c6.apply_corruption(
                routed_payload, corrupt_rule.config
            )
            self._log_event(
                "CHAOS_CORRUPTED_OUTPUT",
                sender_id,
                branch_id,
                f"Chaos Monkey mutated outgoing A2A payload from '{sender_id}' -> '{receiver_id}'",
                {"mutated_payload": routed_payload},
            )

        # Record causal DAG edge in C4
        edge = self.c4.record_edge(
            sender_id=sender_id,
            receiver_id=receiver_id,
            trace_id=trace_id,
            branch_id=branch_id,
            payload_summary=routed_payload,
        )

        # Run Gate L1, Gate L2, and Gate L3
        gate_failures: list[str] = []

        # Propagate upstream causal taint
        if self.c4.taint_status.get(sender_id, False):
            prev_reason = self.c4.taint_reasons.get(
                sender_id, f"Upstream taint from '{sender_id}'"
            )
            gate_failures.append(prev_reason)

        # Gate L1: Syntactic Schema Validation
        if schema_name and schema_name in self.c2.gate_l1_validators:
            try:
                self.c2.gate_l1_validators[schema_name](routed_payload)
            except Exception as exc:
                gate_failures.append(
                    f"Gate L1 Schema '{schema_name}' Violation: {exc}"
                )

        # Gate L2: Generic Provenance Receipt Check
        branch_override = self.c3.branch_overrides.get(branch_id)
        l2_ok, l2_msg = self.c4.verify_provenance(
            trace_id=trace_id,
            candidate_data=routed_payload,
            branch_overrides=branch_override,
        )
        if not l2_ok and l2_msg not in gate_failures:
            gate_failures.append(l2_msg)

        # Gate L3: Registered Domain Invariants
        for inv_name, inv_fn in self.c2.gate_l3_validators.items():
            inv_ok, inv_msg = inv_fn(routed_payload)
            if not inv_ok:
                formatted = f"Gate L3 Invariant '{inv_name}' Violation: {inv_msg}"
                if formatted not in gate_failures:
                    gate_failures.append(formatted)

        is_tainted = len(gate_failures) > 0
        reason_str = " | ".join(gate_failures) if is_tainted else None

        if is_tainted and reason_str:
            self.c4.taint_status[sender_id] = True
            self.c4.taint_status[receiver_id] = True
            self.c4.taint_reasons.setdefault(sender_id, reason_str)
            self.c4.taint_reasons[receiver_id] = reason_str
            if sender_id in self.c3.process_table:
                self.c3.process_table[sender_id]["status"] = "TAINTED"
            if receiver_id in self.c3.process_table:
                self.c3.process_table[receiver_id]["status"] = "TAINTED"

            self._log_event(
                "A2A_TAINT_FLAGGED",
                sender_id,
                branch_id,
                f"Taint flagged on A2A edge '{sender_id}' -> '{receiver_id}': {reason_str}",
                {
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "trace_id": trace_id,
                    "reasons": gate_failures,
                },
            )
            if not optimistic_pipeline:
                rollback_res = await self.isolate_and_rollback(sender_id, reason_str)
                return {
                    "status": "BLOCKED_AND_ROLLED_BACK",
                    "tainted": True,
                    "reason": reason_str,
                    "payload": routed_payload,
                    "rollback_report": rollback_res,
                }
        else:
            self._log_event(
                "A2A_ROUTED_CLEAN",
                sender_id,
                branch_id,
                f"Clean A2A handoff '{sender_id}' -> '{receiver_id}' (VC={edge['vector_clock']})",
                {"edge_id": edge["edge_id"], "trace_id": trace_id},
            )

        return {
            "status": "DELIVERED",
            "edge_id": edge["edge_id"],
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "trace_id": trace_id,
            "branch_id": branch_id,
            "vector_clock": edge["vector_clock"],
            "tainted": is_tainted,
            "reason": reason_str,
            "payload": routed_payload,
        }

    # =========================================================================
    # Method 5: inject_chaos_fault & remove_chaos_fault
    # =========================================================================
    def inject_chaos_fault(
        self,
        target: str,
        fault_category: str,
        probability: float = 1.0,
        config: Optional[dict[str, Any]] = None,
    ) -> ChaosFaultRule:
        rule = self.c6.inject_rule(
            target=target,
            fault_category=fault_category,
            probability=probability,
            config=config,
        )
        self._log_event(
            "CHAOS_FAULT_INJECTED",
            target,
            "Operator",
            f"Injected {fault_category} fault on target '{target}'",
            rule.model_dump(),
        )
        return rule

    def remove_chaos_fault(
        self, target_or_id: str = "ALL", clear_agent_status: bool = True
    ) -> dict[str, Any]:
        """Remove active chaos fault(s) by rule_id, target, `target:category`, or 'ALL'."""
        removed_count = self.c6.remove_rule(target_or_id)
        if clear_agent_status:
            for a_id, proc in self.c3.process_table.items():
                if target_or_id in ("ALL", a_id) or target_or_id.startswith(f"{a_id}:"):
                    if proc.get("status") in (
                        "ERROR_503",
                        "ERROR_504",
                        "BLOCKED_403",
                        "BLOCKED_429",
                        "TAINTED",
                        "ERROR_LLM",
                    ):
                        proc["status"] = "RUNNING"
                    self.c4.taint_status.pop(a_id, None)
                    self.c4.taint_reasons.pop(a_id, None)
        self._log_event(
            "CHAOS_FAULT_REMOVED",
            target_or_id,
            "Operator",
            f"Removed chaos fault(s) matching '{target_or_id}' (removed={removed_count})",
            {"target_or_id": target_or_id, "removed_count": removed_count},
        )
        return {
            "status": "REMOVED",
            "target_or_id": target_or_id,
            "removed_count": removed_count,
            "active_fault_rules": self.c6.list_active_rules(),
        }

    # =========================================================================
    # Method 6: isolate_and_rollback
    # =========================================================================
    async def isolate_and_rollback(
        self,
        failed_agent_id: str,
        failure_reason: str,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        proc = self.c3.process_table.get(failed_agent_id, {})
        branch_id = proc.get("branch_id", "Root")

        latest_chk = self.c5.get_latest_checkpoint(branch_id)
        since_ts = latest_chk["timestamp"] if latest_chk else 0.0

        # Compute T_fault via Causal BFS in C4
        t_fault = self.c4.compute_tainted_bfs_set(
            origin_agent_id=failed_agent_id,
            since_timestamp=since_ts,
            process_table=self.c3.process_table,
        )

        # Revoke all JWTs for agents in T_fault in C1 and cancel tasks in C3
        revoked_jtis: list[str] = []
        current_task = asyncio.current_task()
        for a_id in t_fault:
            jti = self.c1.revoke_agent(a_id)
            if jti:
                revoked_jtis.append(jti)
            a_proc = self.c3.process_table.get(a_id)
            if a_proc:
                t_handle = a_proc.get("task_handle")
                if (
                    t_handle is not None
                    and not t_handle.done()
                    and t_handle is not current_task
                ):
                    t_handle.cancel()
                h_client = a_proc.get("http_client")
                if h_client is not None and not h_client.is_closed:
                    await h_client.aclose()
                a_proc["status"] = "RESTORED"
            self.c4.taint_status[a_id] = False
            self.c4.taint_reasons.pop(a_id, None)
            self.c6.deactivate_target_rules(a_id)

        # Compute empirical Blast Radius Containment Ratio R_blast across healthy branches
        healthy_branch_nodes = [
            a_id
            for a_id, info in self.c3.process_table.items()
            if info.get("branch_id") not in (branch_id, "Root")
        ]
        disrupted_healthy_nodes = [
            a_id for a_id in healthy_branch_nodes if a_id in t_fault
        ]
        denom = max(len(healthy_branch_nodes), 1)
        r_blast = round(1.0 - (len(disrupted_healthy_nodes) / denom), 4)

        fallback_tools: dict[str, str] = {
            spec.tool_name: spec.fallback_tool_name
            for spec in self.c2.registered_tools.values()
            if spec.fallback_tool_name
        }

        mttr_sec = round(time.perf_counter() - t0, 4)
        self.last_blast_radius_ratio = r_blast
        self.last_mttr_seconds = mttr_sec

        report = {
            "failed_agent_id": failed_agent_id,
            "failure_reason": failure_reason,
            "branch_id": branch_id,
            "tainted_bfs_nodes": t_fault,
            "revoked_jtis": revoked_jtis,
            "restored_checkpoint_id": (
                latest_chk["checkpoint_id"] if latest_chk else None
            ),
            "restored_state": latest_chk["state_blob"] if latest_chk else {},
            "fallback_tool_map": fallback_tools,
            "blast_radius_ratio": r_blast,
            "healthy_branch_disrupted_count": len(disrupted_healthy_nodes),
            "mttr_seconds": mttr_sec,
        }
        self.recovery_history.append(report)
        self._log_event(
            "ISOLATE_AND_ROLLBACK",
            failed_agent_id,
            branch_id,
            f"Isolated BFS cone {t_fault} and restored '{report['restored_checkpoint_id']}' (R_blast={r_blast*100:.0f}%)",
            report,
        )
        return report

    # =========================================================================
    # Method 7: trigger_kill_switch
    # =========================================================================
    async def trigger_kill_switch(
        self,
        mode: str,
        target_id: str = "ALL",
        operator_override_state: Optional[dict[str, Any]] = None,
    ) -> KillSwitchReceipt:
        t0 = time.perf_counter()
        current_task = asyncio.current_task()

        if mode == "SURGICAL":
            raw_targets = [t.strip() for t in target_id.split(",") if t.strip()]
            subtree_targets: list[str] = []
            for t in raw_targets:
                for node in self.c3.get_subtree_agents(t):
                    if node not in subtree_targets:
                        subtree_targets.append(node)
                if t not in subtree_targets:
                    subtree_targets.append(t)

            t_rev_start = time.perf_counter()
            revoked: list[str] = []
            for a_id in subtree_targets:
                jti = self.c1.revoke_agent(a_id)
                if jti:
                    revoked.append(jti)
            t_revoke_ms = (time.perf_counter() - t_rev_start) * 1000.0

            t_can_start = time.perf_counter()
            cancelled: list[str] = []
            tasks_to_await: list[asyncio.Task[Any]] = []
            for a_id in subtree_targets:
                proc = self.c3.process_table.get(a_id)
                if not proc:
                    continue
                t_handle = proc.get("task_handle")
                if (
                    t_handle is not None
                    and not t_handle.done()
                    and t_handle is not current_task
                ):
                    t_handle.cancel()
                    tasks_to_await.append(t_handle)
                    cancelled.append(a_id)
                proc["status"] = "KILLED"
            if tasks_to_await:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
            t_cancel_ms = (time.perf_counter() - t_can_start) * 1000.0

            t_sock_start = time.perf_counter()
            closed_sockets = 0
            for a_id in subtree_targets:
                proc = self.c3.process_table.get(a_id)
                if not proc:
                    continue
                h_client = proc.get("http_client")
                if h_client is not None and not h_client.is_closed:
                    await h_client.aclose()
                    closed_sockets += 1
            t_socket_ms = (time.perf_counter() - t_sock_start) * 1000.0

            total_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            self.last_kill_latency_ms = total_ms
            receipt = KillSwitchReceipt(
                mode="SURGICAL",
                target_id=target_id,
                revoked_jtis=revoked,
                cancelled_tasks=cancelled,
                closed_sockets_count=closed_sockets,
                t_revoke_ms=round(t_revoke_ms, 3),
                t_cancel_ms=round(t_cancel_ms, 3),
                t_socket_close_ms=round(t_socket_ms, 3),
                termination_latency_ms=total_ms,
            )

        elif mode == "SOFT_FREEZE":
            ev = self.c3.ensure_branch_event(target_id)
            if operator_override_state is None:
                ev.clear()
                for a_id, proc in self.c3.process_table.items():
                    if target_id == "ALL" or proc.get("branch_id") == target_id:
                        proc["status"] = "PAUSED_FREEZE"
                total_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                receipt = KillSwitchReceipt(
                    mode="SOFT_FREEZE",
                    target_id=target_id,
                    paused_branch=target_id,
                    termination_latency_ms=total_ms,
                )
            else:
                self.c3.branch_overrides[target_id] = copy.deepcopy(
                    operator_override_state
                )
                self.c5.apply_operator_override(target_id, operator_override_state)
                self.c4.update_provenance_on_override(operator_override_state)
                ev.set()
                for a_id, proc in self.c3.process_table.items():
                    if target_id == "ALL" or proc.get("branch_id") == target_id:
                        if proc.get("status") == "PAUSED_FREEZE":
                            proc["status"] = "RUNNING"
                total_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                receipt = KillSwitchReceipt(
                    mode="SOFT_FREEZE",
                    target_id=target_id,
                    resumed_branch=target_id,
                    applied_override=copy.deepcopy(operator_override_state),
                    termination_latency_ms=total_ms,
                )

        elif mode == "GLOBAL_HARD":
            # Step 1: t_revoke
            t_rev_start = time.perf_counter()
            revoked = self.c1.revoke_all()
            t_revoke_ms = (time.perf_counter() - t_rev_start) * 1000.0

            # Step 2: t_cancel
            t_can_start = time.perf_counter()
            cancelled = []
            tasks_to_await = []
            for a_id, proc in self.c3.process_table.items():
                t_handle = proc.get("task_handle")
                if (
                    t_handle is not None
                    and not t_handle.done()
                    and t_handle is not current_task
                ):
                    t_handle.cancel()
                    tasks_to_await.append(t_handle)
                    cancelled.append(a_id)
                proc["status"] = "KILLED"
            if tasks_to_await:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
            t_cancel_ms = (time.perf_counter() - t_can_start) * 1000.0

            # Step 3: t_socket_close
            t_sock_start = time.perf_counter()
            clients_to_close: list[httpx.AsyncClient] = []
            for proc in self.c3.process_table.values():
                h_client = proc.get("http_client")
                if h_client is not None and not h_client.is_closed:
                    clients_to_close.append(h_client)
            if clients_to_close:
                await asyncio.gather(
                    *(c.aclose() for c in clients_to_close),
                    return_exceptions=True,
                )
            t_socket_ms = (time.perf_counter() - t_sock_start) * 1000.0

            total_ms = round(t_revoke_ms + t_cancel_ms + t_socket_ms, 3)
            self.last_kill_latency_ms = total_ms
            receipt = KillSwitchReceipt(
                mode="GLOBAL_HARD",
                target_id=target_id,
                revoked_jtis=revoked,
                cancelled_tasks=cancelled,
                closed_sockets_count=len(clients_to_close),
                t_revoke_ms=round(t_revoke_ms, 3),
                t_cancel_ms=round(t_cancel_ms, 3),
                t_socket_close_ms=round(t_socket_ms, 3),
                termination_latency_ms=total_ms,
            )
        else:
            raise ValueError(f"Unknown kill switch mode: {mode}")

        self.kill_switch_history.append(receipt.model_dump())
        self._log_event(
            f"KILL_SWITCH_{mode}",
            target_id,
            target_id,
            f"Executed {mode} kill-switch on '{target_id}' in {receipt.termination_latency_ms:.3f} ms",
            receipt.model_dump(),
        )
        return receipt

    def get_telemetry_state(self) -> dict[str, Any]:
        """Return full live state of C1–C6, event logs, and empirical KPIs."""
        total_tokens = sum(
            int(b.get("tokens_used", 0)) + int(b.get("wasted_tokens", 0))
            for b in self.c2.budget_ledger.values()
        )
        wasted_tokens = sum(
            int(b.get("wasted_tokens", 0))
            for b in self.c2.budget_ledger.values()
        )
        wasted_pct = (
            round((wasted_tokens / total_tokens) * 100.0, 2)
            if total_tokens > 0
            else 0.0
        )

        return {
            "metrics": {
                "kill_switch_latency_ms": self.last_kill_latency_ms,
                "blast_radius_ratio": self.last_blast_radius_ratio,
                "blast_radius_pct": round(self.last_blast_radius_ratio * 100.0, 2),
                "rbac_block_rate_pct": self.c2.rbac_block_rate,
                "mttr_seconds": self.last_mttr_seconds,
                "wasted_token_overhead_pct": wasted_pct,
            },
            "c1_iam": {
                "public_key_pem": self.c1.public_key_pem,
                "revocation_set": sorted(self.c1.revocation_set),
                "active_tokens": self.c1.active_tokens,
            },
            "c2_policy": {
                "registered_tools": {
                    k: v.model_dump() for k, v in self.c2.registered_tools.items()
                },
                "gate_l1_schemas": list(self.c2.gate_l1_validators.keys()),
                "gate_l3_invariants": list(self.c2.gate_l3_validators.keys()),
                "budget_ledger": self.c2.budget_ledger,
                "rbac_block_rate": self.c2.rbac_block_rate,
            },
            "c3_tasks": {
                "process_table": self.c3.serialize_process_table(),
                "branch_freeze_states": {
                    k: ev.is_set() for k, ev in self.c3.branch_freeze_events.items()
                },
                "branch_overrides": self.c3.branch_overrides,
            },
            "c4_lineage": {
                "dag_edges": self.c4.dag_edges,
                "vector_clocks": self.c4.vector_clocks,
                "taint_status": self.c4.taint_status,
                "taint_reasons": self.c4.taint_reasons,
                "provenance_receipts": self.c4.provenance_receipts,
            },
            "c5_snapshots": {
                "latest_by_branch": self.c5.latest_by_branch,
                "snapshots": [
                    snap for snap in self.c5.snapshots.values()
                ],
            },
            "c6_chaos": {
                "active_fault_rules": self.c6.list_active_rules(),
            },
            "event_log": self.event_log,
            "recovery_history": self.recovery_history,
            "kill_switch_history": self.kill_switch_history,
        }


default_kernel = ControlPlaneKernel()
