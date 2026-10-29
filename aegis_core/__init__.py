"""AEGIS Out-of-Band Governance Control Plane Core Package."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ControlPlaneKernel",
    "default_kernel",
    "create_aegis_app",
    "app",
    "C1_IAM_Engine",
    "C2_Policy_Store",
    "C3_Task_Registry",
    "C4_Lineage_Graph",
    "C5_Snapshot_DB",
    "C6_Chaos_Monkey",
]


def __getattr__(name: str) -> Any:
    if name in ("ControlPlaneKernel", "default_kernel"):
        from aegis_core import kernel

        return getattr(kernel, name)
    if name in ("create_aegis_app", "app"):
        from aegis_core import server

        return getattr(server, name)
    if name in (
        "C1_IAM_Engine",
        "C2_Policy_Store",
        "C3_Task_Registry",
        "C4_Lineage_Graph",
        "C5_Snapshot_DB",
        "C6_Chaos_Monkey",
    ):
        from aegis_core import components

        return getattr(components, name)
    raise AttributeError(f"module 'aegis_core' has no attribute {name!r}")
