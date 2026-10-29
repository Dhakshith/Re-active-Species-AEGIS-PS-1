"""AEGIS Control Plane package exports (PEP 562 lazy loading to prevent circular imports)."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ControlPlaneKernel",
    "kernel_singleton",
    "IAM_Engine",
    "Policy_Store",
    "Task_Registry",
    "Lineage_Graph",
    "Snapshot_DB",
    "Chaos_Monkey",
]


def __getattr__(name: str) -> Any:
    if name in ("ControlPlaneKernel", "kernel_singleton"):
        from control_plane import methods

        return getattr(methods, name)
    if name in (
        "IAM_Engine",
        "Policy_Store",
        "Task_Registry",
        "Lineage_Graph",
        "Snapshot_DB",
        "Chaos_Monkey",
    ):
        from control_plane import components

        return getattr(components, name)
    raise AttributeError(f"module 'control_plane' has no attribute {name!r}")
