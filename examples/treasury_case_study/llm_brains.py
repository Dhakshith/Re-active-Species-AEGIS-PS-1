"""
NVIDIA NIM LLM Brain & Context Builder for Autonomous Treasury Agents.
Implements:
- `NimBrainError`: Raised when NIM API call or JSON action parsing fails after 2 retries.
- `ROLE_PROMPTS`: Tier-specific system prompts (L0 Orchestrator, L1 Supervisor, L2 Worker).
- `build_context(...)`: Builds dynamic agent prompt context including role, scopes, remaining
  C2 token budget, registered tool catalog (descriptions + constraints), and last N transcript
  entries (including `AegisHTTPException` system-block messages).
- `NimBrain`: Calls `https://integrate.api.nvidia.com/v1/chat/completions` (`temperature=0.2`,
  `max_tokens=400`), reuses a persistent HTTP/2 keep-alive client for sub-400ms latency,
  and parses the first `{...}` JSON action block with 2 retries.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from aegis_core.kernel import ControlPlaneKernel
from aegis_sdk.client import AegisAgentNode
from ui.config import require_nim_config


VALID_ACTIONS = frozenset(
    {"CALL_TOOL", "SPAWN_CHILD", "SEND_A2A", "REQUEST_HUMAN", "COMPLETE"}
)

ACTION_PROTOCOL_INSTRUCTIONS = """
You MUST respond with ONLY a single valid JSON object (no markdown prose outside JSON) matching this exact protocol:
{
  "action": "CALL_TOOL" | "SPAWN_CHILD" | "SEND_A2A" | "REQUEST_HUMAN" | "COMPLETE",
  "target": "<tool_name when action==CALL_TOOL, child_agent_id when action==SPAWN_CHILD, or branch/target when action==REQUEST_HUMAN>",
  "to": "<receiver_agent_id when action==SEND_A2A>",
  "args": { ... tool arguments or spawn_child parameters (requested_role, requested_scopes, token_budget, branch_id) ... },
  "payload": { ... A2A message payload or human review proposal ... },
  "message": "<concise explanation or conversational response>"
}

Action Rules:
0. CONVERSATIONAL GREETING / QUESTION RULE (CRITICAL):
   If the user's message is a simple greeting (e.g., "hi", "hello", "hey"), small talk, or a general question about who you are, what tools you have, or system status that does NOT explicitly ask to execute a tool or settle an invoice, DO NOT call any tools or send A2A messages! Immediately output on Turn 1:
   {"action": "COMPLETE", "target": "COMPLETE", "message": "<your friendly, direct, helpful answer to the user>"}
1. "CALL_TOOL": Set "target" to the tool name (e.g. "erp:read", "fx:quote", "fx:quote_reuters", "swift:wire", "kyc:check", "audit:log") and "args" to a JSON object of tool arguments. Only call tools whose required_scope is in your `allowed_scopes` unless explicitly instructed by the user goal to test a direct call.
2. "SPAWN_CHILD": Set "target" to a NEW custom child agent ID (never overwrite existing canonical fleet agents like `FX_Quote_Reader` or `SWIFT_Wire_Executor` — use `SEND_A2A` to delegate to existing agents in `active_fleet_agents`) and "args" to {"requested_role": "...", "requested_scopes": [...], "token_budget": 1600, "branch_id": "..."}. Requested scopes MUST be a subset of your `allowed_scopes`.
3. "SEND_A2A": Set "to" to a DIFFERENT target agent ID (never yourself) from `active_fleet_agents` (e.g. "FX_Settlement_Supervisor", "FX_Quote_Reader", "SWIFT_Wire_Executor", "Compliance_Supervisor", "Sanctions_Screener", "Audit_Logger") and "payload" to the structured data dict.
4. "REQUEST_HUMAN": Use when human governor approval/override is required before a high-value wire or when requested by the user. Set "target" to "Branch_A" and "payload" to the proposed settlement parameters (e.g. {"invoice_id": "INV-2026-9981", "amount_usd": 2000000.0, "fx_rate": 0.92, "settlement_eur": 1840000.0}).
5. "COMPLETE": Use when the user's goal has been fulfilled or after adapting to a governance block and completing the task. Set "message" to a clear summary of what was executed and any governance receipts/blocks encountered.
6. If the transcript shows a `[SYSTEM_BLOCK HTTP 403 ...]` or `[SYSTEM_BLOCK HTTP 503 ...]` or `[SYSTEM_RECOVERY ...]`, adapt immediately on your next turn (e.g., delegate to an authorized agent, switch to `fx:quote_reuters`, or summarize the outcome).
""".strip()

ROLE_PROMPTS: dict[str, str] = {
    "L0": (
        "You are an L0 Root Orchestrator (`Chief_Treasury_Orchestrator`) governed by the AEGIS-CONTROL "
        "Out-of-Band Control Plane. You coordinate L1 Supervisors (`FX_Settlement_Supervisor` on Branch_A "
        "and `Compliance_Supervisor` on Branch_B) and L2 workers (`FX_Quote_Reader`, `SWIFT_Wire_Executor`, "
        "`Sanctions_Screener`, `Audit_Logger`), or invoke tools within your scopes to satisfy the user's goal.\n\n"
        + ACTION_PROTOCOL_INSTRUCTIONS
    ),
    "L1": (
        "You are an L1 Domain Supervisor (`FX_Settlement_Supervisor` or `Compliance_Supervisor`) governed "
        "by the AEGIS-CONTROL Out-of-Band Control Plane. You manage checkpointing, delegate to L2 workers "
        "within your branch (`FX_Quote_Reader`, `SWIFT_Wire_Executor`, `Sanctions_Screener`, `Audit_Logger`), "
        "validate A2A payloads, or escalate to human governance via `REQUEST_HUMAN`.\n\n"
        + ACTION_PROTOCOL_INSTRUCTIONS
    ),
    "L2": (
        "You are an L2 Specialist Worker Agent (`FX_Quote_Reader`, `SWIFT_Wire_Executor`, "
        "`Sanctions_Screener`, or `Audit_Logger`) governed by the AEGIS-CONTROL Out-of-Band Control Plane. "
        "You execute leaf tools within your attenuated JWT scopes, route results via `SEND_A2A`, or "
        "adapt if the Control Plane blocks an action.\n\n"
        + ACTION_PROTOCOL_INSTRUCTIONS
    ),
}

_SIMPLE_CONVERSATIONAL_RE = re.compile(
    r"^\s*(hi|hello|hey|good\s+(morning|afternoon|evening)|howdy|greetings|yo|sup|thanks|thank\s+you|"
    r"who\s+are\s+you\??|what\s+can\s+you\s+do\??|what\s+are\s+your\s+tools\??|help\??|status\??)\s*[!?.]*\s*$",
    re.IGNORECASE,
)


def is_conversational_message(text: str) -> bool:
    """Return True if `text` is a simple greeting or conversational query that shouldn't trigger tools."""
    return bool(_SIMPLE_CONVERSATIONAL_RE.match(text or ""))


class NimBrainError(RuntimeError):
    """Raised when NVIDIA NIM API invocation or JSON action extraction fails after retries."""


def _resolve_tier_prompt(role: str) -> str:
    role_upper = (role or "").upper()
    if "L0" in role_upper or "ORCHESTRATOR" in role_upper:
        return ROLE_PROMPTS["L0"]
    if "L1" in role_upper or "SUPERVISOR" in role_upper:
        return ROLE_PROMPTS["L1"]
    return ROLE_PROMPTS["L2"]


def extract_first_json_object(raw_text: str) -> dict[str, Any]:
    """
    Extract and parse the first balanced `{...}` JSON object from `raw_text`.
    If `raw_text` contains no `{` at all (e.g. a plain-text conversational answer),
    wraps it cleanly as `{"action": "COMPLETE", "target": "COMPLETE", "message": raw_text}`.
    Raises `ValueError` if `{` is present with malformed JSON or an unrecognized `"action"`.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty response text from LLM")

    text = raw_text.strip()
    start_idx = text.find("{")
    if start_idx == -1:
        return {
            "action": "COMPLETE",
            "target": "COMPLETE",
            "message": text,
        }

    depth = 0
    in_str = False
    escape = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start_idx : idx + 1]
                    parsed = json.loads(candidate)
                    if not isinstance(parsed, dict):
                        raise ValueError("Parsed JSON is not a dictionary")
                    action = str(parsed.get("action", "")).upper().strip()
                    if action not in VALID_ACTIONS:
                        raise ValueError(
                            f"Invalid or missing 'action'={action!r}; expected one of {sorted(VALID_ACTIONS)}"
                        )
                    parsed["action"] = action
                    return parsed

    raise ValueError(f"Unbalanced braces in LLM output: {text[:200]!r}")


def build_context(
    node: AegisAgentNode,
    kernel: ControlPlaneKernel,
    transcript: list[dict[str, Any]],
    goal: str = "",
    available_agents: Optional[list[str]] = None,
    last_n: int = 12,
) -> list[dict[str, str]]:
    """
    Build the OpenAI-compatible `[system, user]` chat message list for `node`:
    - System message: tier prompt + agent identity, scopes, remaining token budget,
      and full registered toolbox catalog (descriptions + constraints).
    - User message: active goal + last `last_n` transcript entries (including any
      `AegisHTTPException` system-block messages).
    """
    system_base = _resolve_tier_prompt(node.role)
    budget_remaining = kernel.c2.get_remaining_budget(node.agent_id, fallback=node.token_budget)
    ledger_entry = kernel.c2.budget_ledger.get(node.agent_id, {})

    toolbox_lines: list[str] = []
    for t_name, spec in kernel.c2.registered_tools.items():
        constraints_str = (
            json.dumps(spec.parameter_constraints) if spec.parameter_constraints else "none"
        )
        prov_str = ",".join(spec.provenance_fields) if spec.provenance_fields else "none"
        fb_str = spec.fallback_tool_name or "none"
        toolbox_lines.append(
            f"- {t_name} (required_scope={spec.required_scope!r}, mutates_state={spec.mutates_state}, "
            f"provenance_fields=[{prov_str}], constraints={constraints_str}, fallback={fb_str}): "
            f"{spec.description}"
        )

    fleet_peers = available_agents or list(kernel.c3.process_table.keys())
    state_header = (
        f"\n\n=== YOUR LIVE AGENT PASSPORT & GOVERNANCE CONTEXT ===\n"
        f"- agent_id: {node.agent_id}\n"
        f"- role: {node.role}\n"
        f"- branch_id: {node.branch_id}\n"
        f"- allowed_scopes: {json.dumps(node.scopes)}\n"
        f"- token_budget_remaining: {budget_remaining} (used={ledger_entry.get('tokens_used', 0)}, "
        f"max={ledger_entry.get('max_tokens', node.token_budget)})\n"
        f"- active_fleet_agents: {json.dumps(fleet_peers)}\n"
        f"\n=== REGISTERED CONTROL PLANE TOOLBOX ===\n"
        + ("\n".join(toolbox_lines) if toolbox_lines else "(no tools registered)")
    )

    recent_entries = transcript[-last_n:] if last_n > 0 else transcript
    formatted_transcript: list[str] = []
    for idx, entry in enumerate(recent_entries, start=1):
        speaker = entry.get("agent_id") or entry.get("role") or "SYSTEM"
        kind = entry.get("type", "TURN")
        text = entry.get("content") or entry.get("message") or json.dumps(entry)
        receipt = entry.get("receipt")
        receipt_str = f" | RECEIPT={json.dumps(receipt)}" if receipt else ""
        formatted_transcript.append(f"[{idx}] ({kind}) {speaker}: {text}{receipt_str}")

    transcript_block = (
        "\n".join(formatted_transcript)
        if formatted_transcript
        else "(No prior turns yet.)"
    )

    conv_hint = ""
    if is_conversational_message(goal):
        conv_hint = (
            "\nNOTE: The user's message is a conversational greeting or general inquiry. "
            'Do NOT call any tools or send A2A messages! Output {"action": "COMPLETE", "target": "COMPLETE", "message": "..."} '
            "with a friendly, concise response explaining your role and available treasury capabilities.\n"
        )
    elif node.agent_id == "Chief_Treasury_Orchestrator":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `Chief_Treasury_Orchestrator`:\n"
            "- You are the L0 Root Orchestrator. Your role is hierarchical governance and cross-branch delegation.\n"
            "- To process an invoice or settle a wire: DO NOT execute leaf tools yourself! Initiate the workflow by delegating via `SEND_A2A` to `FX_Quote_Reader` (or `FX_Settlement_Supervisor`) on Branch_A to intake the invoice and market FX quote.\n"
            "- To screen counterparties or check compliance: delegate via `SEND_A2A` to `Sanctions_Screener` (or `Compliance_Supervisor`) on Branch_B.\n"
            "- To log audits: delegate via `SEND_A2A` to `Audit_Logger`.\n"
        )
    elif node.agent_id == "FX_Settlement_Supervisor":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `FX_Settlement_Supervisor`:\n"
            "- You are the L1 Supervisor for Branch_A. Your role is hierarchical supervision and coordination.\n"
            "- You coordinate L2 workers `FX_Quote_Reader` and `SWIFT_Wire_Executor`.\n"
            "- If quote or invoice is needed, delegate via `SEND_A2A` to `FX_Quote_Reader`.\n"
            "- If quote and invoice are ready, delegate wire execution via `SEND_A2A` to `SWIFT_Wire_Executor`.\n"
            "- Do not call `swift:wire` directly yourself; delegate it to `SWIFT_Wire_Executor`.\n"
        )
    elif node.agent_id == "FX_Quote_Reader":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `FX_Quote_Reader`:\n"
            "- Your allowed scopes are `['erp:read', 'fx:quote']`. NEVER send `SEND_A2A` to `FX_Quote_Reader` (yourself)!\n"
            "- Step 1: If invoice details (`supplier`, `amount_usd`, `iban`) are not yet in the transcript, call `erp:read`.\n"
            "- Step 2: Call `fx:quote` (or fallback `fx:quote_reuters`) to fetch the institutional FX quote.\n"
            "- Step 3: Once you have the quote and invoice details, route the trade via `SEND_A2A` to `Sanctions_Screener` on Branch_B for mandatory KYC/sanctions screening (or to `SWIFT_Wire_Executor` if sanctions are already cleared)!\n"
        )
    elif node.agent_id == "Sanctions_Screener":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `Sanctions_Screener`:\n"
            "- Your allowed scope is `['kyc:check']`. NEVER send `SEND_A2A` to `Sanctions_Screener` (yourself)!\n"
            "- Call `kyc:check` with the supplier name to screen against OFAC & EU sanctions lists.\n"
            "- Once cleared (`sanctions_status == 'CLEAR'`): if the goal involves invoice settlement or wire execution, delegate via `SEND_A2A` to `Audit_Logger` on Branch_B to record the compliance log; if the goal was only sanctions screening, output `COMPLETE`.\n"
        )
    elif node.agent_id == "Audit_Logger":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `Audit_Logger`:\n"
            "- Your allowed scope is `['audit:log']`. NEVER send `SEND_A2A` to `Audit_Logger` (yourself)!\n"
            "- Call `audit:log` with `invoice_id`, `supplier`, and `sanctions_status` to create an immutable audit record.\n"
            "- Once logged: if the workflow involves wire settlement, delegate via `SEND_A2A` to `SWIFT_Wire_Executor` on Branch_A to execute the cleared wire; if the goal was only audit logging, output `COMPLETE`.\n"
        )
    elif node.agent_id == "Compliance_Supervisor":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `Compliance_Supervisor`:\n"
            "- You are the L1 Supervisor for Branch_B. Coordinate `Sanctions_Screener` and `Audit_Logger`.\n"
        )
    elif node.agent_id == "SWIFT_Wire_Executor":
        conv_hint = (
            "\nAGENT DIRECTIVE FOR `SWIFT_Wire_Executor`:\n"
            "- Your ONLY allowed scope is `['swift:wire']`. NEVER call `erp:read`, `fx:quote`, or `kyc:check`, and NEVER send `SEND_A2A` to `SWIFT_Wire_Executor` (yourself)!\n"
            '- If `swift:wire` has NOT been executed yet in the current goal, output `{"action": "CALL_TOOL", "target": "swift:wire", "args": {...}, "message": "..."}` now.\n'
            '- If `swift:wire` has ALREADY succeeded (`HTTP 200`, `SETTLED`) in the current goal, immediately output `{"action": "COMPLETE", "target": "COMPLETE", "message": "..."}` with the complete settlement and compliance summary!\n'
        )

    user_prompt = (
        f"USER MESSAGE / GOAL: {goal}\n"
        f"{conv_hint}\n"
        f"RECENT EXECUTION TRANSCRIPT (last {len(recent_entries)} entries):\n"
        f"{transcript_block}\n\n"
        f"As `{node.agent_id}` (scopes={node.scopes}), output your next single JSON action object now:"
    )

    return [
        {"role": "system", "content": system_base + state_header},
        {"role": "user", "content": user_prompt},
    ]


class NimBrain:
    """
    NVIDIA NIM Chat Completions Client enforcing the AEGIS JSON action protocol.
    Reuses a persistent `httpx.AsyncClient` for low-latency connection pooling when
    talking to live NIM, and supports custom `httpx.AsyncBaseTransport` for `MockTransport` tests.
    """

    ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_sec: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        if api_key is None or model is None:
            cfg_key, cfg_model = require_nim_config()
            self.api_key = api_key or cfg_key
            self.model = model or cfg_model
        else:
            self.api_key = api_key
            self.model = model
        self.transport = transport
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self._persistent_client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self.transport is not None:
            return httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_sec
            )
        if self._persistent_client is None or self._persistent_client.is_closed:
            self._persistent_client = httpx.AsyncClient(
                timeout=self.timeout_sec,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._persistent_client

    async def aclose(self) -> None:
        """Close the persistent `httpx.AsyncClient` connection pool if open."""
        if (
            self._persistent_client is not None
            and not self._persistent_client.is_closed
        ):
            await self._persistent_client.aclose()
        self._persistent_client = None

    async def decide(
        self,
        node: AegisAgentNode,
        kernel: ControlPlaneKernel,
        transcript: list[dict[str, Any]],
        goal: str = "",
        available_agents: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Query NVIDIA NIM (`temperature=0.2`, `max_tokens=400`) and parse the JSON action.
        Retries up to `self.max_retries` times (default 2 retries = 3 total attempts) before
        raising `NimBrainError`.
        """
        messages = build_context(
            node=node,
            kernel=kernel,
            transcript=transcript,
            goal=goal,
            available_agents=available_agents,
        )
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                req_json: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 400,
                }
                if "nemotron-3.5" in self.model:
                    req_json["chat_template_kwargs"] = {"enable_thinking": False}

                client = self._get_client()
                try:
                    resp = await client.post(
                        self.ENDPOINT,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=req_json,
                    )
                    if (
                        resp.status_code in (404, 410)
                        and self.transport is None
                        and self.model != "nvidia/nemotron-3.5-lightning-30b-a3b"
                    ):
                        self.model = "nvidia/nemotron-3.5-lightning-30b-a3b"
                        req_json["model"] = self.model
                        req_json["chat_template_kwargs"] = {"enable_thinking": False}
                        resp = await client.post(
                            self.ENDPOINT,
                            headers={
                                "Authorization": f"Bearer {self.api_key}",
                                "Content-Type": "application/json",
                            },
                            json=req_json,
                        )
                finally:
                    if self.transport is not None and not client.is_closed:
                        await client.aclose()

                if resp.status_code != 200:
                    raise NimBrainError(
                        f"NIM HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                body = resp.json()
                msg_obj = body.get("choices", [{}])[0].get("message", {})
                raw_content = (
                    msg_obj.get("content")
                    or msg_obj.get("reasoning_content")
                    or ""
                )
                action_obj = extract_first_json_object(raw_content)
                return action_obj
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    messages = list(messages) + [
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response could not be parsed ({exc}). "
                                "Reply ONLY with a single valid JSON object containing "
                                '"action" in ["CALL_TOOL", "SPAWN_CHILD", "SEND_A2A", "REQUEST_HUMAN", "COMPLETE"].'
                            ),
                        }
                    ]

        raise NimBrainError(
            f"NimBrain failed after {self.max_retries + 1} attempts for agent '{node.agent_id}': {last_error}"
        ) from last_error
