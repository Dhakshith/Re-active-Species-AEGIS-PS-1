"""The 6 Domain-Agnostic Control Plane Engines (C1 through C6) for AEGIS."""

from __future__ import annotations

import asyncio
from collections import deque
import copy
import hashlib
import hmac
import json
import os
import random
import sqlite3
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import httpx
import jwt

from aegis_core.models import (
    ChaosFaultRule,
    PrivilegeEscalationError,
    RateLimitExceededError,
    TokenRevokedOrInvalidError,
    ToolRegistrationSpec,
)


class C1_IAM_Engine:
    """C1: Asymmetric Ed25519 Identity, Privilege Attenuation & O(1) Revocation Engine."""

    def __init__(self) -> None:
        self._private_key: Ed25519PrivateKey = Ed25519PrivateKey.generate()
        self.public_key: Ed25519PublicKey = self._private_key.public_key()
        self.public_key_pem: str = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        self.revocation_set: set[str] = set()
        self.active_tokens: dict[str, dict[str, Any]] = {}
        self.tokens_by_jti: dict[str, dict[str, Any]] = {}

    def _rotate_keypair(self) -> None:
        self._private_key = Ed25519PrivateKey.generate()
        self.public_key = self._private_key.public_key()
        self.public_key_pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    def mint_root_token(
        self,
        agent_id: str = "Root_Orchestrator",
        role: str = "L0_Orchestrator",
        scopes: Optional[list[str]] = None,
        token_budget: int = 10000,
        branch_id: str = "Root",
        ttl_seconds: int = 3600,
    ) -> tuple[str, dict[str, Any]]:
        """Mint root L0 Ed25519 JWT token."""
        now = int(time.time())
        jti = f"jti_{agent_id}_{uuid4().hex[:8]}"
        claims: dict[str, Any] = {
            "jti": jti,
            "sub": agent_id,
            "parent_id": None,
            "branch_id": branch_id,
            "role": role,
            "scopes": list(scopes or []),
            "token_budget": int(token_budget),
            "iat": now,
            "exp": now + ttl_seconds,
        }
        token_str = jwt.encode(claims, self._private_key, algorithm="EdDSA")
        self.active_tokens[agent_id] = copy.deepcopy(claims)
        self.tokens_by_jti[jti] = copy.deepcopy(claims)
        return token_str, claims

    def mint_delegated_token(
        self,
        parent_claims: dict[str, Any],
        child_agent_id: str,
        requested_role: str,
        requested_scopes: list[str],
        token_budget: int,
        branch_id: Optional[str] = None,
        parent_remaining_budget: Optional[int] = None,
        ttl_seconds: int = 3600,
    ) -> tuple[str, dict[str, Any]]:
        """
        Enforce strict mathematical privilege & budget attenuation:
          set(requested_scopes) <= set(parent_claims["scopes"])
          token_budget <= parent_remaining_budget
        Raises PrivilegeEscalationError (HTTP 403) if violated.
        """
        parent_jti = parent_claims.get("jti", "")
        if parent_jti in self.revocation_set:
            raise TokenRevokedOrInvalidError(
                f"Parent token {parent_jti} has been revoked"
            )

        parent_scopes = set(parent_claims.get("scopes", []))
        child_scopes = set(requested_scopes)
        if not child_scopes.issubset(parent_scopes):
            excess = sorted(child_scopes - parent_scopes)
            raise PrivilegeEscalationError(
                f"Privilege Attenuation Violation: child '{child_agent_id}' requested unauthorized scopes {excess} not present in parent '{parent_claims.get('sub')}' scopes {sorted(parent_scopes)}",
                details={
                    "child_agent_id": child_agent_id,
                    "parent_id": parent_claims.get("sub"),
                    "unauthorized_scopes": excess,
                },
            )

        max_allowed_budget = (
            parent_remaining_budget
            if parent_remaining_budget is not None
            else int(parent_claims.get("token_budget", 10000))
        )
        if token_budget <= 0 or token_budget > max_allowed_budget:
            raise PrivilegeEscalationError(
                f"Budget Attenuation Violation: requested budget {token_budget} exceeds parent remaining budget {max_allowed_budget}",
                details={
                    "child_agent_id": child_agent_id,
                    "requested_budget": token_budget,
                    "parent_remaining_budget": max_allowed_budget,
                },
            )

        now = int(time.time())
        jti = f"jti_{child_agent_id}_{uuid4().hex[:8]}"
        effective_branch = branch_id or parent_claims.get("branch_id", "Root")
        claims: dict[str, Any] = {
            "jti": jti,
            "sub": child_agent_id,
            "parent_id": parent_claims.get("sub"),
            "branch_id": effective_branch,
            "role": requested_role,
            "scopes": list(requested_scopes),
            "token_budget": int(token_budget),
            "iat": now,
            "exp": now + ttl_seconds,
        }
        token_str = jwt.encode(claims, self._private_key, algorithm="EdDSA")
        self.active_tokens[child_agent_id] = copy.deepcopy(claims)
        self.tokens_by_jti[jti] = copy.deepcopy(claims)
        return token_str, claims

    def verify_jwt(self, token: str) -> dict[str, Any]:
        """Verify Ed25519 signature, expiration, and O(1) revocation blacklist."""
        if not token or not isinstance(token, str):
            raise TokenRevokedOrInvalidError("Missing or empty Bearer JWT token")
        raw_token = token.removeprefix("Bearer ").strip()
        try:
            unverified = jwt.decode(raw_token, options={"verify_signature": False})
            jti = unverified.get("jti")
            if jti and jti in self.revocation_set:
                raise TokenRevokedOrInvalidError(
                    f"JWT jti '{jti}' for agent '{unverified.get('sub')}' is revoked"
                )
        except TokenRevokedOrInvalidError:
            raise
        except Exception as exc:
            raise TokenRevokedOrInvalidError(f"Malformed JWT token: {exc}") from exc

        try:
            claims = jwt.decode(raw_token, self.public_key, algorithms=["EdDSA"])
        except Exception as exc:
            raise TokenRevokedOrInvalidError(
                f"Invalid Ed25519 JWT signature or expired token: {exc}"
            ) from exc

        if claims.get("jti") in self.revocation_set:
            raise TokenRevokedOrInvalidError(
                f"JWT jti '{claims.get('jti')}' is in revocation_set"
            )
        return claims

    def revoke_jti(self, jti: str) -> None:
        if jti:
            self.revocation_set.add(jti)

    def revoke_agent(self, agent_id: str) -> Optional[str]:
        claims = self.active_tokens.get(agent_id)
        if claims and "jti" in claims:
            jti = claims["jti"]
            self.revocation_set.add(jti)
            return jti
        return None

    def revoke_all(self) -> list[str]:
        all_jtis = list(self.tokens_by_jti.keys())
        for claims in self.active_tokens.values():
            if "jti" in claims and claims["jti"] not in all_jtis:
                all_jtis.append(claims["jti"])
        self.revocation_set.update(all_jtis)
        self._rotate_keypair()
        return all_jtis


class C2_Policy_Store:
    """C2: Dynamic Tool Registry, RBAC Matrix, Gate L1/L3 Validator Store & Tokenomics Ledger."""

    def __init__(self) -> None:
        self.registered_tools: dict[str, ToolRegistrationSpec] = {}
        self.tool_handlers: dict[str, Callable[..., Any]] = {}
        self.gate_l1_validators: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.gate_l3_validators: dict[
            str, Callable[[dict[str, Any]], tuple[bool, str]]
        ] = {}
        self.budget_ledger: dict[str, dict[str, Any]] = {}
        self.unauthorized_attempts: int = 0
        self.unauthorized_blocked: int = 0

    def register_tool(
        self,
        spec: ToolRegistrationSpec,
        handler: Optional[Callable[..., Any]] = None,
    ) -> ToolRegistrationSpec:
        self.registered_tools[spec.tool_name] = spec
        if handler is not None:
            self.tool_handlers[spec.tool_name] = handler
        else:
            default_out = spec.default_response or {
                "tool_name": spec.tool_name,
                "status": "EXECUTED_DYNAMIC_TOOL",
            }

            async def _default_handler(**kwargs: Any) -> dict[str, Any]:
                return {**default_out, "received_args": kwargs}

            self.tool_handlers[spec.tool_name] = _default_handler
        return spec

    def register_gate_l1_validator(
        self, schema_name: str, validator_fn: Callable[[dict[str, Any]], Any]
    ) -> None:
        self.gate_l1_validators[schema_name] = validator_fn

    def register_gate_l3_validator(
        self,
        invariant_name: str,
        validator_fn: Callable[[dict[str, Any]], tuple[bool, str]],
    ) -> None:
        self.gate_l3_validators[invariant_name] = validator_fn

    def init_budget(
        self,
        agent_id: str,
        max_tokens: int,
        max_calls_per_min: int = 60,
    ) -> dict[str, Any]:
        prev_wasted = int(
            self.budget_ledger.get(agent_id, {}).get("wasted_tokens", 0)
        )
        entry = {
            "tokens_used": 0,
            "max_tokens": int(max_tokens),
            "calls_made": 0,
            "max_calls_per_min": int(max_calls_per_min),
            "window_start_ts": time.time(),
            "wasted_tokens": prev_wasted,
        }
        self.budget_ledger[agent_id] = entry
        return entry

    def get_remaining_budget(self, agent_id: str, fallback: int = 10000) -> int:
        entry = self.budget_ledger.get(agent_id)
        if not entry:
            return fallback
        return max(0, int(entry["max_tokens"]) - int(entry["tokens_used"]))

    def verify_scope_and_budget(
        self,
        agent_claims: dict[str, Any],
        tool_spec: ToolRegistrationSpec,
    ) -> None:
        """Verify required_scope in agent_claims['scopes'] and check token/rate budgets."""
        agent_id = agent_claims["sub"]
        agent_scopes = set(agent_claims.get("scopes", []))

        if tool_spec.required_scope not in agent_scopes:
            self.unauthorized_attempts += 1
            self.unauthorized_blocked += 1
            if agent_id in self.budget_ledger:
                self.budget_ledger[agent_id]["wasted_tokens"] += int(
                    tool_spec.token_cost_per_call // 2
                )
            raise PrivilegeEscalationError(
                f"RBAC Scope Violation: Agent '{agent_id}' (scopes={sorted(agent_scopes)}) is forbidden from calling tool '{tool_spec.tool_name}' (requires '{tool_spec.required_scope}')",
                details={
                    "agent_id": agent_id,
                    "tool_name": tool_spec.tool_name,
                    "required_scope": tool_spec.required_scope,
                    "agent_scopes": sorted(agent_scopes),
                },
            )

        ledger = self.budget_ledger.setdefault(
            agent_id,
            {
                "tokens_used": 0,
                "max_tokens": int(agent_claims.get("token_budget", 5000)),
                "calls_made": 0,
                "max_calls_per_min": tool_spec.rate_limit_per_min,
                "window_start_ts": time.time(),
                "wasted_tokens": 0,
            },
        )
        now = time.time()
        if now - ledger["window_start_ts"] >= 60.0:
            ledger["calls_made"] = 0
            ledger["window_start_ts"] = now

        if (
            ledger["calls_made"] >= min(ledger["max_calls_per_min"], tool_spec.rate_limit_per_min)
            or ledger["tokens_used"] + tool_spec.token_cost_per_call > ledger["max_tokens"]
        ):
            ledger["wasted_tokens"] += tool_spec.token_cost_per_call
            raise RateLimitExceededError(
                f"Budget or Rate Limit Exceeded for agent '{agent_id}' on tool '{tool_spec.tool_name}'"
            )

    def deduct_budget(self, agent_id: str, token_cost: int) -> None:
        if agent_id in self.budget_ledger:
            self.budget_ledger[agent_id]["tokens_used"] += int(token_cost)
            self.budget_ledger[agent_id]["calls_made"] += 1

    def check_parameter_constraints(
        self, tool_spec: ToolRegistrationSpec, tool_args: dict[str, Any]
    ) -> tuple[bool, str]:
        """Validate declarative parameter_constraints (min, max, allowed_values) on tool_args."""
        for param_name, rules in tool_spec.parameter_constraints.items():
            if param_name not in tool_args or tool_args[param_name] is None:
                continue
            val = tool_args[param_name]
            if isinstance(rules, dict):
                if "min" in rules and float(val) < float(rules["min"]):
                    return (
                        False,
                        f"Parameter '{param_name}'={val} is below minimum {rules['min']}",
                    )
                if "max" in rules and float(val) > float(rules["max"]):
                    return (
                        False,
                        f"Parameter '{param_name}'={val} exceeds maximum ceiling {rules['max']}",
                    )
                if "allowed_values" in rules and val not in rules["allowed_values"]:
                    return (
                        False,
                        f"Parameter '{param_name}'={val} not in allowed_values {rules['allowed_values']}",
                    )
        return True, "OK"

    @property
    def rbac_block_rate(self) -> float:
        if self.unauthorized_attempts == 0:
            return 100.0
        return round(
            (self.unauthorized_blocked / self.unauthorized_attempts) * 100.0, 2
        )


class C3_Task_Registry:
    """C3: Active Coroutine Task Handles, Socket Pools & Soft-Freeze Synchronization Registry."""

    def __init__(self) -> None:
        self.process_table: dict[str, dict[str, Any]] = {}
        self.branch_freeze_events: dict[str, asyncio.Event] = {}
        self.branch_overrides: dict[str, dict[str, Any]] = {}

    def ensure_branch_event(self, branch_id: str) -> asyncio.Event:
        if branch_id not in self.branch_freeze_events:
            ev = asyncio.Event()
            ev.set()
            self.branch_freeze_events[branch_id] = ev
        return self.branch_freeze_events[branch_id]

    def register_agent(
        self,
        agent_id: str,
        jti: str,
        role: str,
        parent_id: Optional[str],
        branch_id: str,
        status: str = "READY",
    ) -> dict[str, Any]:
        self.ensure_branch_event(branch_id)
        self.ensure_branch_event("ALL")

        existing_children = (
            self.process_table.get(agent_id, {}).get("children_ids", [])
        )
        entry: dict[str, Any] = {
            "agent_id": agent_id,
            "jti": jti,
            "role": role,
            "parent_id": parent_id,
            "children_ids": list(existing_children),
            "branch_id": branch_id,
            "task_handle": None,
            "http_client": None,
            "status": status,
            "last_heartbeat_ts": time.time(),
        }
        self.process_table[agent_id] = entry

        if parent_id and parent_id in self.process_table:
            if agent_id not in self.process_table[parent_id]["children_ids"]:
                self.process_table[parent_id]["children_ids"].append(agent_id)
        return entry

    async def wait_if_frozen(self, branch_id: str, agent_id: str) -> None:
        """Pause coroutine cleanly if SOFT_FREEZE is active on branch_id or ALL."""
        branch_ev = self.ensure_branch_event(branch_id)
        all_ev = self.ensure_branch_event("ALL")
        if not branch_ev.is_set() or not all_ev.is_set():
            if agent_id in self.process_table:
                self.process_table[agent_id]["status"] = "PAUSED_FREEZE"
            if not branch_ev.is_set():
                await branch_ev.wait()
            if not all_ev.is_set():
                await all_ev.wait()
            if agent_id in self.process_table:
                self.process_table[agent_id]["status"] = "RUNNING"

    def get_subtree_agents(self, root_target_id: str) -> list[str]:
        visited: list[str] = []
        queue: deque[str] = deque([root_target_id])
        while queue:
            curr = queue.popleft()
            if curr not in visited and curr in self.process_table:
                visited.append(curr)
                for child in self.process_table[curr].get("children_ids", []):
                    if child not in visited:
                        queue.append(child)
        return visited

    def serialize_process_table(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for agent_id, info in self.process_table.items():
            t_handle = info.get("task_handle")
            h_client = info.get("http_client")
            out[agent_id] = {
                "agent_id": agent_id,
                "jti": info.get("jti"),
                "role": info.get("role"),
                "parent_id": info.get("parent_id"),
                "children_ids": list(info.get("children_ids", [])),
                "branch_id": info.get("branch_id"),
                "status": info.get("status"),
                "has_active_task": t_handle is not None and not t_handle.done(),
                "has_open_socket": h_client is not None and not h_client.is_closed,
                "last_heartbeat_ts": info.get("last_heartbeat_ts"),
            }
        return out


class C4_Lineage_Graph:
    """C4: Directed Acyclic Lineage Graph, Generic HMAC-SHA256 Provenance & Causal BFS Engine."""

    def __init__(self) -> None:
        self._hmac_secret: bytes = os.urandom(32)
        self.dag_edges: list[dict[str, Any]] = []
        self.vector_clocks: dict[str, dict[str, int]] = {}
        self.taint_status: dict[str, bool] = {}
        self.taint_reasons: dict[str, str] = {}
        self.provenance_receipts: dict[str, dict[str, Any]] = {}

    def record_edge(
        self,
        sender_id: str,
        receiver_id: str,
        trace_id: str,
        branch_id: str,
        payload_summary: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        vc = self.vector_clocks.setdefault(branch_id, {})
        vc[sender_id] = vc.get(sender_id, 0) + 1
        vc[receiver_id] = max(vc.get(receiver_id, 0), vc[sender_id]) + 1
        edge = {
            "edge_id": f"edge_{len(self.dag_edges) + 1}_{uuid4().hex[:6]}",
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "trace_id": trace_id,
            "branch_id": branch_id,
            "vector_clock": copy.deepcopy(vc),
            "timestamp": time.time(),
            "perf_ts": time.perf_counter(),
            "payload_summary": copy.deepcopy(payload_summary or {}),
        }
        self.dag_edges.append(edge)
        return edge

    def record_provenance_receipt(
        self,
        trace_id: str,
        tool_name: str,
        tool_output: dict[str, Any],
        provenance_fields: list[str],
        overridden: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Extract provenance_fields from tool_output, compute HMAC-SHA256, and store under trace_id."""
        if not provenance_fields or not isinstance(tool_output, dict):
            return None
        tracked: dict[str, Any] = {}
        for field_name in provenance_fields:
            if field_name in tool_output:
                tracked[field_name] = tool_output[field_name]
        if not tracked:
            return None

        existing = self.provenance_receipts.get(trace_id)
        if existing is not None:
            merged = {**existing["tracked_fields"], **tracked}
            tracked = merged

        canonical_bytes = json.dumps(tracked, sort_keys=True).encode("utf-8")
        signature = hmac.new(
            self._hmac_secret, canonical_bytes, hashlib.sha256
        ).hexdigest()
        receipt = {
            "trace_id": trace_id,
            "tool_name": tool_name,
            "tracked_fields": tracked,
            "hmac_sha256": signature,
            "timestamp": time.time(),
            "overridden": overridden,
        }
        self.provenance_receipts[trace_id] = receipt
        return receipt

    def update_provenance_on_override(
        self, override_dict: dict[str, Any], trace_id: Optional[str] = None
    ) -> None:
        """Update tracked provenance receipts when a human operator injects a SOFT_FREEZE override."""
        targets = (
            [self.provenance_receipts[trace_id]]
            if trace_id and trace_id in self.provenance_receipts
            else list(self.provenance_receipts.values())
        )
        for receipt in targets:
            tracked = receipt["tracked_fields"]
            updated = False
            for k, v in override_dict.items():
                if k in tracked:
                    tracked[k] = v
                    updated = True
            if updated:
                canonical_bytes = json.dumps(tracked, sort_keys=True).encode("utf-8")
                receipt["hmac_sha256"] = hmac.new(
                    self._hmac_secret, canonical_bytes, hashlib.sha256
                ).hexdigest()
                receipt["overridden"] = True

    def verify_provenance(
        self,
        trace_id: str,
        candidate_data: dict[str, Any],
        branch_overrides: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """
        Gate L2 Generic Provenance Verification:
        For any key present in both provenance_receipts[trace_id]['tracked_fields'] and candidate_data,
        verify numerical/value equality (within 1e-5 for floats) unless overridden by SOFT_FREEZE.
        """
        receipt = self.provenance_receipts.get(trace_id)
        if not receipt:
            return True, "NO_RECEIPT_FOR_TRACE"

        # Verify HMAC integrity of stored receipt
        tracked = receipt.get("tracked_fields", {})
        canonical_bytes = json.dumps(tracked, sort_keys=True).encode("utf-8")
        expected_hmac = hmac.new(
            self._hmac_secret, canonical_bytes, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_hmac, receipt.get("hmac_sha256", "")):
            return False, f"Gate L2 HMAC Tampering Detected for trace_id={trace_id}"

        overrides = branch_overrides or {}
        for key, ground_truth_val in tracked.items():
            if key not in candidate_data or candidate_data[key] is None:
                continue
            if key in overrides:
                continue
            candidate_val = candidate_data[key]
            if isinstance(ground_truth_val, (int, float)) and isinstance(
                candidate_val, (int, float)
            ):
                if abs(float(ground_truth_val) - float(candidate_val)) > 1e-5:
                    return (
                        False,
                        f"Gate L2 Provenance Mismatch on '{key}': candidate={candidate_val} != receipt={ground_truth_val} (trace_id={trace_id})",
                    )
            else:
                if ground_truth_val != candidate_val:
                    return (
                        False,
                        f"Gate L2 Provenance Mismatch on '{key}': candidate='{candidate_val}' != receipt='{ground_truth_val}' (trace_id={trace_id})",
                    )
        return True, "PROVENANCE_VERIFIED"

    def compute_tainted_bfs_set(
        self,
        origin_agent_id: str,
        since_timestamp: float = 0.0,
        process_table: Optional[dict[str, dict[str, Any]]] = None,
    ) -> list[str]:
        """
        Compute Causal Tainted Node Set T_fault via Breadth-First Search starting from origin_agent_id
        across dag_edges and same-branch execution links created after since_timestamp.
        """
        t_fault: set[str] = {origin_agent_id}
        queue: deque[str] = deque([origin_agent_id])

        origin_branch = None
        if process_table and origin_agent_id in process_table:
            origin_branch = process_table[origin_agent_id].get("branch_id")

        while queue:
            curr = queue.popleft()
            for edge in self.dag_edges:
                if edge["timestamp"] >= since_timestamp and edge["sender_id"] == curr:
                    nxt = edge["receiver_id"]
                    if nxt not in t_fault:
                        t_fault.add(nxt)
                        queue.append(nxt)

        if process_table and origin_branch:
            for a_id, info in process_table.items():
                if info.get("branch_id") == origin_branch and self.taint_status.get(
                    a_id, False
                ):
                    t_fault.add(a_id)

        return sorted(t_fault)


class C5_Snapshot_DB:
    """C5: Hybrid SQLite + In-Memory Versioned Subtree Checkpoint Store."""

    def __init__(self, db_path: str = "aegis_snapshots.db") -> None:
        self.db_path = db_path
        self.snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self.latest_by_branch: dict[str, str] = {}
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aegis_checkpoints (
                    branch_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    vector_clock_json TEXT NOT NULL,
                    state_blob_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (branch_id, checkpoint_id)
                )
                """
            )

    def clear_all(self) -> None:
        self.snapshots.clear()
        self.latest_by_branch.clear()
        with self.conn:
            self.conn.execute("DELETE FROM aegis_checkpoints")

    def save_checkpoint(
        self,
        branch_id: str,
        agent_id: str,
        state_blob: dict[str, Any],
        vector_clock: dict[str, int],
        taint_status: dict[str, bool],
        checkpoint_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if taint_status.get(agent_id, False):
            raise RuntimeError(
                f"Cannot save checkpoint: Agent '{agent_id}' is currently TAINTED"
            )

        branch_count = sum(
            1 for (b_id, _) in self.snapshots.keys() if b_id == branch_id
        )
        chk_id = checkpoint_id or f"chk_{branch_id}_{branch_count + 1}"
        now = time.time()
        record = {
            "checkpoint_id": chk_id,
            "branch_id": branch_id,
            "agent_id": agent_id,
            "vector_clock": copy.deepcopy(vector_clock),
            "state_blob": copy.deepcopy(state_blob),
            "timestamp": now,
        }
        self.snapshots[(branch_id, chk_id)] = record
        self.latest_by_branch[branch_id] = chk_id

        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO aegis_checkpoints
                (branch_id, checkpoint_id, agent_id, vector_clock_json, state_blob_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    branch_id,
                    chk_id,
                    agent_id,
                    json.dumps(vector_clock),
                    json.dumps(state_blob),
                    now,
                ),
            )
        return record

    def get_latest_checkpoint(self, branch_id: str) -> Optional[dict[str, Any]]:
        chk_id = self.latest_by_branch.get(branch_id)
        if not chk_id:
            return None
        return self.get_checkpoint(branch_id, chk_id)

    def get_checkpoint(
        self, branch_id: str, checkpoint_id: str
    ) -> Optional[dict[str, Any]]:
        snap = self.snapshots.get((branch_id, checkpoint_id))
        if snap:
            return copy.deepcopy(snap)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT agent_id, vector_clock_json, state_blob_json, timestamp FROM aegis_checkpoints WHERE branch_id=? AND checkpoint_id=?",
            (branch_id, checkpoint_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "checkpoint_id": checkpoint_id,
            "branch_id": branch_id,
            "agent_id": row[0],
            "vector_clock": json.loads(row[1]),
            "state_blob": json.loads(row[2]),
            "timestamp": float(row[3]),
        }

    def apply_operator_override(
        self, branch_id: str, override_dict: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        latest = self.get_latest_checkpoint(branch_id)
        if not latest:
            return None
        chk_id = latest["checkpoint_id"]
        latest["state_blob"].update(copy.deepcopy(override_dict))
        self.snapshots[(branch_id, chk_id)] = latest
        with self.conn:
            self.conn.execute(
                "UPDATE aegis_checkpoints SET state_blob_json=? WHERE branch_id=? AND checkpoint_id=?",
                (json.dumps(latest["state_blob"]), branch_id, chk_id),
            )
        return latest


class C6_Chaos_Monkey:
    """C6: Generic Deterministic & Probabilistic Fault Injection Engine."""

    def __init__(self) -> None:
        self.active_fault_rules: dict[str, ChaosFaultRule] = {}

    def inject_rule(
        self,
        target: str,
        fault_category: str,
        probability: float = 1.0,
        config: Optional[dict[str, Any]] = None,
    ) -> ChaosFaultRule:
        rule = ChaosFaultRule(
            rule_id=f"fault_{target}_{uuid4().hex[:6]}",
            target=target,
            fault_category=fault_category,  # type: ignore[arg-type]
            probability=probability,
            config=copy.deepcopy(config or {}),
            active=True,
            triggered_count=0,
        )
        self.active_fault_rules[ f"{target}:{fault_category}"] = rule
        self.active_fault_rules[target] = rule
        return rule

    def get_rule(
        self, target: str, fault_category: Optional[str] = None
    ) -> Optional[ChaosFaultRule]:
        if fault_category:
            keyed = self.active_fault_rules.get(f"{target}:{fault_category}")
            if keyed and keyed.active:
                if keyed.probability >= 1.0 or random.random() <= keyed.probability:
                    return keyed
        rule = self.active_fault_rules.get(target)
        if rule and rule.active:
            if fault_category is None or rule.fault_category == fault_category:
                if rule.probability >= 1.0 or random.random() <= rule.probability:
                    return rule
        return None

    def consume_rule(self, rule: ChaosFaultRule) -> None:
        rule.triggered_count += 1
        one_shot = bool(rule.config.get("one_shot", True))
        max_triggers = int(rule.config.get("max_triggers", 1))
        if one_shot and rule.triggered_count >= max_triggers:
            rule.active = False
            self.remove_rule(rule.rule_id)

    def deactivate_target_rules(self, target: str) -> None:
        for key in list(self.active_fault_rules.keys()):
            rule = self.active_fault_rules.get(key)
            if rule and (rule.target == target or key.startswith(f"{target}:")):
                rule.active = False
                self.active_fault_rules.pop(key, None)

    def remove_rule(self, target_or_id: str) -> int:
        """Remove fault rule(s) matching `rule_id`, `target`, or `target:fault_category`. Pass 'ALL' to clear all."""
        if target_or_id == "ALL":
            return self.clear_all_rules()
        removed = 0
        for key in list(self.active_fault_rules.keys()):
            rule = self.active_fault_rules.get(key)
            if rule is None:
                continue
            if (
                key == target_or_id
                or rule.rule_id == target_or_id
                or rule.target == target_or_id
                or key.startswith(f"{target_or_id}:")
            ):
                rule.active = False
                self.active_fault_rules.pop(key, None)
                removed += 1
        return removed

    def clear_all_rules(self) -> int:
        """Remove all active chaos fault rules."""
        count = len({r.rule_id for r in self.active_fault_rules.values()})
        for rule in self.active_fault_rules.values():
            rule.active = False
        self.active_fault_rules.clear()
        return count

    def list_active_rules(self) -> dict[str, dict[str, Any]]:
        """Return deduplicated dictionary of currently active fault rules keyed by `target:fault_category`."""
        out: dict[str, dict[str, Any]] = {}
        for rule in self.active_fault_rules.values():
            if rule.active:
                composite_key = f"{rule.target}:{rule.fault_category}"
                out[composite_key] = rule.model_dump()
        return out

    @staticmethod
    def apply_corruption(
        data: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Generic payload/output mutator supporting:
          1. config['mutations'] = {'metric_val': {'multiply': 10.0}}
          2. config['field'] & config['multiplier'] (or config['value'])
          3. config['multiplier'] across numeric fields in config.get('fields') or all numeric fields
        """
        mutated = copy.deepcopy(data)
        if "mutations" in config and isinstance(config["mutations"], dict):
            for field_name, spec in config["mutations"].items():
                if field_name in mutated and isinstance(spec, dict):
                    if "multiply" in spec and isinstance(
                        mutated[field_name], (int, float)
                    ):
                        mutated[field_name] = round(
                            float(mutated[field_name]) * float(spec["multiply"]), 6
                        )
                    elif "add" in spec and isinstance(
                        mutated[field_name], (int, float)
                    ):
                        mutated[field_name] = round(
                            float(mutated[field_name]) + float(spec["add"]), 6
                        )
                    elif "set" in spec:
                        mutated[field_name] = spec["set"]
            return mutated

        if "field" in config and config["field"] in mutated:
            f_name = config["field"]
            if "multiplier" in config and isinstance(mutated[f_name], (int, float)):
                mutated[f_name] = round(
                    float(mutated[f_name]) * float(config["multiplier"]), 6
                )
            elif "value" in config:
                mutated[f_name] = config["value"]
            # If dependent field also specified
            if "dependent_field" in config and config["dependent_field"] in mutated:
                dep = config["dependent_field"]
                if "multiplier" in config and isinstance(mutated[dep], (int, float)):
                    mutated[dep] = round(
                        float(mutated[dep]) * float(config["multiplier"]), 6
                    )
            return mutated

        if "multiplier" in config:
            mult = float(config["multiplier"])
            target_fields = config.get("fields")
            for k, v in list(mutated.items()):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if target_fields is None or k in target_fields:
                        mutated[k] = round(float(v) * mult, 6)
        return mutated
