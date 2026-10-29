"""Plug-and-Play Decorators for Registering Tools, Gate L1 Schemas, and Gate L3 Invariants."""

from __future__ import annotations

from typing import Any, Callable, Optional, Type, Union
from pydantic import BaseModel

from aegis_core.kernel import ControlPlaneKernel, default_kernel
from aegis_core.models import ToolRegistrationSpec


def aegis_tool(
    tool_name: str,
    required_scope: str,
    mutates_state: bool = False,
    provenance_fields: Optional[list[str]] = None,
    parameter_constraints: Optional[dict[str, Any]] = None,
    fallback_tool_name: Optional[str] = None,
    rate_limit_per_min: int = 60,
    token_cost_per_call: int = 100,
    description: str = "",
    kernel: Optional[ControlPlaneKernel] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register any Python function or coroutine as a governed tool in C2_Policy_Store."""

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolRegistrationSpec(
            tool_name=tool_name,
            required_scope=required_scope,
            mutates_state=mutates_state,
            provenance_fields=list(provenance_fields or []),
            parameter_constraints=dict(parameter_constraints or {}),
            fallback_tool_name=fallback_tool_name,
            rate_limit_per_min=rate_limit_per_min,
            token_cost_per_call=token_cost_per_call,
            description=description or (fn.__doc__ or "").strip(),
        )
        target_kernel = kernel or default_kernel
        target_kernel.register_tool(spec, handler=fn)
        setattr(fn, "_aegis_tool_spec", spec)
        return fn

    return _decorator


def aegis_gate_l1(
    schema_name: str,
    kernel: Optional[ControlPlaneKernel] = None,
) -> Callable[[Union[Type[BaseModel], Callable[[dict[str, Any]], Any]]], Any]:
    """Decorator to register a Pydantic BaseModel or callable as a Gate L1 A2A schema validator."""

    def _decorator(
        target: Union[Type[BaseModel], Callable[[dict[str, Any]], Any]]
    ) -> Any:
        target_kernel = kernel or default_kernel
        if isinstance(target, type) and issubclass(target, BaseModel):

            def _validate_pydantic(payload: dict[str, Any]) -> Any:
                return target.model_validate(payload)

            target_kernel.c2.register_gate_l1_validator(
                schema_name, _validate_pydantic
            )
        else:
            target_kernel.c2.register_gate_l1_validator(schema_name, target)
        setattr(target, "_aegis_gate_l1_name", schema_name)
        return target

    return _decorator


def aegis_gate_l3(
    invariant_name: str,
    kernel: Optional[ControlPlaneKernel] = None,
) -> Callable[
    [Callable[[dict[str, Any]], tuple[bool, str]]],
    Callable[[dict[str, Any]], tuple[bool, str]],
]:
    """Decorator to register a domain invariant function returning (is_valid: bool, message: str) into Gate L3."""

    def _decorator(
        fn: Callable[[dict[str, Any]], tuple[bool, str]]
    ) -> Callable[[dict[str, Any]], tuple[bool, str]]:
        target_kernel = kernel or default_kernel
        target_kernel.c2.register_gate_l3_validator(invariant_name, fn)
        setattr(fn, "_aegis_gate_l3_name", invariant_name)
        return fn

    return _decorator


class _AegisDecoratorNamespace:
    """Namespace alias allowing @aegis.register_tool, @aegis.gate_l1_schema, @aegis.gate_l3_invariant."""

    register_tool = staticmethod(aegis_tool)
    tool = staticmethod(aegis_tool)
    gate_l1_schema = staticmethod(aegis_gate_l1)
    gate_l3_invariant = staticmethod(aegis_gate_l3)


aegis = _AegisDecoratorNamespace()
register_tool = aegis_tool
gate_l1_schema = aegis_gate_l1
gate_l3_invariant = aegis_gate_l3
