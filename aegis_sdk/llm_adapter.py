"""Pluggable LLM Reasoning Adapter (OpenAI / NVIDIA NIM / Ollama) with Deterministic Fallback."""

from __future__ import annotations

import os
from typing import Any, Optional
import httpx


class AegisLLMAdapter:
    """
    Dual-mode reasoning engine:
      - Uses live LLM APIs (NVIDIA_API_KEY, OPENAI_API_KEY, or OLLAMA_HOST) when configured.
      - Automatically falls back to deterministic policy state-machine reasoning when no key is set,
        guaranteeing <2s execution and 0% test flakiness on CPU instances.
    """

    def __init__(self) -> None:
        self.nvidia_key: Optional[str] = os.environ.get("NVIDIA_API_KEY")
        self.openai_key: Optional[str] = os.environ.get("OPENAI_API_KEY")
        self.ollama_host: Optional[str] = os.environ.get("OLLAMA_HOST")

        if self.nvidia_key:
            self.mode = "NVIDIA_NIM"
            self.base_url = "https://integrate.api.nvidia.com/v1"
            self.api_key = self.nvidia_key
            self.model = os.environ.get(
                "AEGIS_LLM_MODEL", "meta/llama-3.1-70b-instruct"
            )
        elif self.openai_key:
            self.mode = "OPENAI"
            self.base_url = "https://api.openai.com/v1"
            self.api_key = self.openai_key
            self.model = os.environ.get("AEGIS_LLM_MODEL", "gpt-4o-mini")
        elif self.ollama_host:
            self.mode = "OLLAMA"
            self.base_url = f"{self.ollama_host.rstrip('/')}/v1"
            self.api_key = "ollama"
            self.model = os.environ.get("AEGIS_LLM_MODEL", "llama3.1")
        else:
            self.mode = "DETERMINISTIC_FALLBACK"
            self.base_url = ""
            self.api_key = ""
            self.model = "aegis-deterministic-v2"

    async def decide_next_action(
        self,
        agent_id: str,
        role: str,
        context: dict[str, Any],
        default_decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Return reasoning decision (using deterministic policy fallback when offline/no API key)."""
        if self.mode == "DETERMINISTIC_FALLBACK":
            return {
                **default_decision,
                "reasoning_engine": self.mode,
                "agent_id": agent_id,
                "role": role,
            }

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": f"You are autonomous agent {agent_id} ({role}). Confirm the next action concisely.",
                            },
                            {"role": "user", "content": str(context)},
                        ],
                        "max_tokens": 64,
                        "temperature": 0.0,
                    },
                )
                if resp.status_code == 200:
                    llm_text = (
                        resp.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    return {
                        **default_decision,
                        "reasoning_engine": self.mode,
                        "llm_rationale": llm_text,
                    }
        except Exception:
            pass

        return {
            **default_decision,
            "reasoning_engine": "DETERMINISTIC_FALLBACK",
            "agent_id": agent_id,
            "role": role,
        }
