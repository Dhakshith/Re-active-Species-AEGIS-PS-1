"""AEGIS Treasury Multi-Agent package exports (PEP 562 lazy loading to prevent circular imports)."""

from __future__ import annotations

from typing import Any

__all__ = ["TreasuryWorkflowRunner", "execute_mock_tool"]


def __getattr__(name: str) -> Any:
    if name == "TreasuryWorkflowRunner":
        from agents.treasury_graph import TreasuryWorkflowRunner

        return TreasuryWorkflowRunner
    if name == "execute_mock_tool":
        from agents.mock_tools import execute_mock_tool

        return execute_mock_tool
    raise AttributeError(f"module 'agents' has no attribute {name!r}")
