"""Core state components C1 through C6 for the AEGIS Control Plane."""

from __future__ import annotations

import asyncio
from collections import deque
import copy
import hashlib
import hmac
import json
import os
import sqlite3
import time
from typing import Any, Iterable, Optional
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import httpx
import jwt

from control_plane.models import TOOL_PROVIDER_CHAINS


class IAM_Engine:
    """C1: Identity & Cryptographic Authority (Ed25519 JWT Passports & O(1) Revocation)."""

    def __init__(self) -> None:
        self._private_key: Ed25519PrivateKey = Ed25519PrivateKey.generate()
        self.public_key: Ed25519PublicKey = self._private_key.public_key()
        self.revocation_set: set[str] = set()
        self.active_tokens: dict[str, dict[str, Any]] = {}
        self.agent_current_jti: dict[str, str] = {}

    def sign_jwt(self, claims: dict[str, Any]) -> str:
        """Sign JWT claims using EdDSA (Ed25519) private key and register token."""
        token = jwt.encode(claims, self._private_key, algorithm="EdDSA")
        jti = claims["jti"]
        agent_id = claims["sub"]
        self.active_tokens[jti] = copy.deepcopy(claims)
        self.agent_current_jti[agent_id] = jti
        return token

    def verify_jwt(
        self, token: str, error_prefix: str = "HTTP_401_TOKEN_REVOKED"
    ) -> dict[str, Any]:
        """
        Verify JWT signature, expiration (exp), and confirm jti not in revocation_set.
        Raises PermissionError if invalid, expired, or revoked.
        """
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            jti = unverified.get("jti")
            if jti and jti in self.revocation_set:
                raise PermissionError(f"{error_prefix}: Token {jti} has been revoked")
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError(f"{error_prefix}: Malformed JWT ({exc})") from exc

        try:
            claims = jwt.decode(token, self.public_key, algorithms=["EdDSA"])
        except Exception as exc:
            raise PermissionError(
                f"{error_prefix}: Signature or expiration check failed ({exc})"
            ) from exc

        if claims.get("jti") in self.revocation_set:
            raise PermissionError(
                f"{error_prefix}: Token {claims.get('jti')} is in revocation_set"
            )

        return claims

    def revoke_jti(self, jti: str) -> None:
        """Revoke a single JWT Token ID in O(1) time."""
        if jti:
            self.revocation_set.add(jti)

    def bulk_revoke_all_and_rotate_key(self) -> list[str]:
        """Bulk-revoke all active JTIs in <1ms and rotate the Ed25519 keypair."""
        all_jtis = list(self.active_tokens.keys())
        self.revocation_set.update(all_jtis)
        self._private_key = Ed25519PrivateKey.generate()
        self.public_key = self._private_key.public_key()
        return all_jtis


class Policy_Store:
    """C2: RBAC Matrix & Tokenomics Ledger."""

    def __init__(self) -> None:
        self.rbac_matrix: dict[str, dict[str, Any]] = {
            "erp:read": {"mutates_state": False, "max_calls": 10},
            "fx:quote": {"mutates_state": False, "max_calls": 10},
            "swift:wire": {
                "mutates_state": True,
                "max_amount_eur": 5_000_000.0,
                "max_calls": 3,
            },
            "kyc:check": {"mutates_state": False, "max_calls": 10},
            "audit:log": {"mutates_state": True, "max_calls": 10},
        }
        self.budget_ledger: dict[str, dict[str, int]] = {}
        self.rbac_attempts_unauthorized: int = 0
        self.rbac_blocks_enforced: int = 0

    def init_agent_budget(
        self, agent_id: str, max_tokens: int, max_calls_per_min: int = 15
    ) -> dict[str, int]:
        """Initialize or reset the tokenomics budget ledger for an agent."""
        existing_wasted = (
            self.budget_ledger.get(agent_id, {}).get("wasted_tokens_on_failure", 0)
        )
        entry = {
            "tokens_used": 0,
            "max_tokens": max_tokens,
            "calls_made": 0,
            "max_calls_per_min": max_calls_per_min,
            "wasted_tokens_on_failure": existing_wasted,
        }
        self.budget_ledger[agent_id] = entry
        return entry

    @property
    def rbac_block_rate(self) -> float:
        """Percentage of unauthorized tool/scope escalation attempts blocked (target 100.0%)."""
        if self.rbac_attempts_unauthorized == 0:
            return 100.0
        return round(
            (self.rbac_blocks_enforced / self.rbac_attempts_unauthorized) * 100.0, 2
        )


class Task_Registry:
    """C3: Active Coroutine & Socket Table."""

    def __init__(self) -> None:
        self.process_table: dict[str, dict[str, Any]] = {}
        self.branch_freeze_events: dict[str, asyncio.Event] = {
            "Root": asyncio.Event(),
            "Branch_A": asyncio.Event(),
            "Branch_B": asyncio.Event(),
            "ALL": asyncio.Event(),
        }
        for ev in self.branch_freeze_events.values():
            ev.set()  # Initially unpaused
        self.operator_overrides: dict[str, dict[str, Any]] = {}

    def register_agent(
        self,
        agent_id: str,
        jti: str,
        role: str,
        parent_id: Optional[str],
        branch_id: str,
        status: str = "READY",
    ) -> dict[str, Any]:
        """Register or update an agent in C3 process_table and link to parent."""
        existing_children: list[str] = []
        if agent_id in self.process_table:
            existing_children = self.process_table[agent_id].get("children_ids", [])

        entry: dict[str, Any] = {
            "agent_id": agent_id,
            "jti": jti,
            "role": role,
            "parent_id": parent_id,
            "children_ids": existing_children,
            "branch_id": branch_id,
            "task_handle": None,
            "http_client": None,
            "last_heartbeat": time.perf_counter(),
            "status": status,
        }
        self.process_table[agent_id] = entry

        if parent_id and parent_id in self.process_table:
            parent_children = self.process_table[parent_id]["children_ids"]
            if agent_id not in parent_children:
                parent_children.append(agent_id)

        if branch_id not in self.branch_freeze_events:
            ev = asyncio.Event()
            ev.set()
            self.branch_freeze_events[branch_id] = ev

        return entry

    def get_recursive_children(self, target_id: str) -> list[str]:
        """Return target_id and all recursive children_ids from process_table."""
        visited: list[str] = []
        queue: deque[str] = deque([target_id])
        while queue:
            curr = queue.popleft()
            if curr not in visited and curr in self.process_table:
                visited.append(curr)
                for child in self.process_table[curr].get("children_ids", []):
                    if child not in visited:
                        queue.append(child)
        return visited

    def serialize_process_table(self) -> dict[str, dict[str, Any]]:
        """Return JSON-serializable view of process_table (stripping live coroutine/socket objects)."""
        out: dict[str, dict[str, Any]] = {}
        for agent_id, info in self.process_table.items():
            out[agent_id] = {
                "agent_id": info["agent_id"],
                "jti": info["jti"],
                "role": info["role"],
                "parent_id": info["parent_id"],
                "children_ids": list(info["children_ids"]),
                "branch_id": info["branch_id"],
                "has_active_task": info["task_handle"] is not None
                and not info["task_handle"].done(),
                "has_open_socket": info["http_client"] is not None
                and not info["http_client"].is_closed,
                "last_heartbeat": info["last_heartbeat"],
                "status": info["status"],
            }
        return out


class Lineage_Graph:
    """C4: Causal DAG, Taint Tracker & Cryptographic Provenance Store."""

    def __init__(self) -> None:
        self.hmac_secret: bytes = os.urandom(32)
        self.vector_clock: dict[str, int] = {}
        self.dag_edges: list[dict[str, Any]] = []
        self.taint_status: dict[str, bool] = {}
        self.taint_reasons: dict[str, str] = {}
        self.provenance_receipts: dict[str, dict[str, Any]] = {}

    def record_tool_receipt(
        self, trace_id: str, tool_name: str, raw_output: dict[str, Any]
    ) -> dict[str, Any]:
        """Compute HMAC-SHA256 over canonical JSON of raw_output and store in provenance_receipts."""
        canonical_json = json.dumps(raw_output, sort_keys=True).encode("utf-8")
        digest = hmac.new(self.hmac_secret, canonical_json, hashlib.sha256).hexdigest()
        receipt = {
            "trace_id": trace_id,
            "tool_name": tool_name,
            "raw_output": copy.deepcopy(raw_output),
            "hmac_sha256": digest,
            "timestamp": time.perf_counter(),
        }
        self.provenance_receipts[trace_id] = receipt
        return receipt

    def verify_tool_receipt_hmac(self, trace_id: str) -> bool:
        """Verify stored HMAC-SHA256 signature for a provenance receipt."""
        receipt = self.provenance_receipts.get(trace_id)
        if not receipt:
            return False
        canonical_json = json.dumps(receipt["raw_output"], sort_keys=True).encode("utf-8")
        expected = hmac.new(self.hmac_secret, canonical_json, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, receipt["hmac_sha256"])

    def compute_tainted_bfs_set(
        self, origin_agent_id: str, since_timestamp: float
    ) -> set[str]:
        """
        Run Breadth-First Search (BFS) over dag_edges where edge['timestamp'] >= since_timestamp
        starting from origin_agent_id, returning the exact causal tainted node set T_fault.
        """
        return self.compute_multi_origin_tainted_bfs_set(
            [origin_agent_id], since_timestamp
        )

    def compute_multi_origin_tainted_bfs_set(
        self, origin_agent_ids: Iterable[str], since_timestamp: float
    ) -> set[str]:
        """
        Run Breadth-First Search (BFS) over dag_edges starting from multiple simultaneous
        tainted origin agents where edge['timestamp'] >= since_timestamp.
        """
        origins = [a for a in origin_agent_ids if a]
        t_fault: set[str] = set(origins)
        queue: deque[str] = deque(origins)

        while queue:
            current = queue.popleft()
            for edge in self.dag_edges:
                if (
                    edge["timestamp"] >= since_timestamp
                    and edge["sender_id"] == current
                ):
                    receiver = edge["receiver_id"]
                    if receiver not in t_fault:
                        t_fault.add(receiver)
                        queue.append(receiver)

        return t_fault


class Snapshot_DB:
    """C5: Versioned Subtree Checkpoint Store (In-Memory + SQLite Persistence)."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self.latest_by_branch: dict[str, str] = {}
        self.sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_sql_schema()

    def _init_sql_schema(self) -> None:
        with self.sqlite_conn:
            self.sqlite_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subtree_snapshots (
                    checkpoint_id TEXT PRIMARY KEY,
                    branch_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    vector_clock_json TEXT NOT NULL,
                    serialized_state_blob BLOB NOT NULL
                )
                """
            )

    def save_snapshot(
        self,
        checkpoint_id: str,
        branch_id: str,
        created_by: str,
        timestamp: float,
        vector_clock_snapshot: dict[str, int],
        serialized_state_blob: bytes,
    ) -> dict[str, Any]:
        """Save snapshot to in-memory dictionary and persist to SQLite."""
        record = {
            "checkpoint_id": checkpoint_id,
            "branch_id": branch_id,
            "created_by": created_by,
            "timestamp": timestamp,
            "vector_clock_snapshot": copy.deepcopy(vector_clock_snapshot),
            "serialized_state_blob": serialized_state_blob,
        }
        self.snapshots[(branch_id, checkpoint_id)] = record
        self.latest_by_branch[branch_id] = checkpoint_id

        with self.sqlite_conn:
            self.sqlite_conn.execute(
                """
                INSERT OR REPLACE INTO subtree_snapshots
                (checkpoint_id, branch_id, created_by, timestamp, vector_clock_json, serialized_state_blob)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    branch_id,
                    created_by,
                    timestamp,
                    json.dumps(vector_clock_snapshot),
                    serialized_state_blob,
                ),
            )
        return record

    def get_sql_snapshot_count(self) -> int:
        cur = self.sqlite_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM subtree_snapshots")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def serialize_snapshots(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (_, _), snap in self.snapshots.items():
            out.append(
                {
                    "checkpoint_id": snap["checkpoint_id"],
                    "branch_id": snap["branch_id"],
                    "created_by": snap["created_by"],
                    "timestamp": snap["timestamp"],
                    "vector_clock_snapshot": snap["vector_clock_snapshot"],
                    "state_preview": json.loads(
                        snap["serialized_state_blob"].decode("utf-8")
                    ),
                }
            )
        return out


class Chaos_Monkey:
    """
    C6: Deterministic & Probabilistic Fault Injector supporting simultaneous
    multi-agent and multi-tool fault queues and multi-tier tool failover tracking.
    """

    def __init__(self) -> None:
        self.active_fault_rules: dict[str, dict[str, Any]] = {}
        self.fault_queues: dict[str, list[dict[str, Any]]] = {}
        self.tool_health_registry: dict[str, dict[str, Any]] = {}
        self._init_tool_health()

    def _init_tool_health(self) -> None:
        for tool_name, providers in TOOL_PROVIDER_CHAINS.items():
            self.tool_health_registry[tool_name] = {
                "tool_name": tool_name,
                "primary_provider": providers[0],
                "fallback_providers": providers[1:],
                "active_provider": providers[0],
                "status": "HEALTHY",
                "outage_events": 0,
                "failover_switches": 0,
                "last_error": None,
            }

    def set_rule(
        self,
        target: str,
        fault_category: str,
        fault_config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        cfg = copy.deepcopy(fault_config) if fault_config else {}
        max_triggers = int(cfg.get("max_triggers", 1))
        rule = {
            "rule_id": f"chaos_{target}_{uuid4().hex[:6]}",
            "target": target,
            "fault_category": fault_category,
            "fault_config": cfg,
            "triggered_count": 0,
            "max_triggers": max_triggers,
            "active": True,
        }
        queue = self.fault_queues.setdefault(target, [])
        # Replace existing active rule of the exact same category on the same target, or append
        replaced = False
        for idx, existing in enumerate(queue):
            if existing.get("fault_category") == fault_category and existing.get(
                "active", False
            ):
                queue[idx] = rule
                replaced = True
                break
        if not replaced:
            queue.append(rule)

        self.active_fault_rules[target] = rule
        if target in self.tool_health_registry:
            self.tool_health_registry[target]["status"] = "FAULT_ARMED"
        return rule

    def set_batch_rules(self, faults: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Inject multiple agent and tool faults simultaneously."""
        created: list[dict[str, Any]] = []
        for f in faults:
            rule = self.set_rule(
                target=f["target"],
                fault_category=f["fault_category"],
                fault_config=f.get("fault_config"),
            )
            created.append(rule)
        return created

    def get_active_rule(
        self, target: str, fault_category: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Lookup an active rule for target (and optional fault_category) across the queue."""
        queue = self.fault_queues.get(target, [])
        for rule in queue:
            if not rule.get("active", False):
                continue
            if fault_category is None or rule.get("fault_category") == fault_category:
                return rule

        # Fallback to direct active_fault_rules dict if mutated externally
        direct = self.active_fault_rules.get(target)
        if direct and direct.get("active", False):
            if fault_category is None or direct.get("fault_category") == fault_category:
                return direct
        return None

    def consume_rule(self, rule: dict[str, Any]) -> None:
        """Increment triggered_count and deactivate when max_triggers is reached."""
        rule["triggered_count"] = int(rule.get("triggered_count", 0)) + 1
        max_triggers = int(rule.get("max_triggers", 1))
        if rule["triggered_count"] >= max_triggers:
            rule["active"] = False

        target = rule.get("target", "")
        # Sync active_fault_rules[target] to next still-active rule if any
        for candidate in self.fault_queues.get(target, []):
            if candidate.get("active", False):
                self.active_fault_rules[target] = candidate
                break

    def record_tool_outage(
        self, tool_name: str, failed_provider: str, error_msg: str
    ) -> None:
        if tool_name in self.tool_health_registry:
            entry = self.tool_health_registry[tool_name]
            entry["outage_events"] += 1
            entry["status"] = "OUTAGE_DETECTED"
            entry["last_error"] = f"{failed_provider}: {error_msg}"

    def record_tool_success(self, tool_name: str, used_provider: str) -> None:
        if tool_name in self.tool_health_registry:
            entry = self.tool_health_registry[tool_name]
            primary = entry["primary_provider"]
            if used_provider != primary:
                if entry["active_provider"] != used_provider:
                    entry["failover_switches"] += 1
                entry["active_provider"] = used_provider
                entry["status"] = "FAILOVER_ACTIVE"
            else:
                entry["active_provider"] = primary
                if entry["outage_events"] > 0:
                    entry["status"] = "RECOVERED"
                else:
                    entry["status"] = "HEALTHY"
