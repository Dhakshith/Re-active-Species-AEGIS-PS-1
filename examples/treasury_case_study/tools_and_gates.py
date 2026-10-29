"""Corporate Treasury Tools & Gate L1/L3 Registration Plugin using aegis_sdk."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from uuid import uuid4
from pydantic import BaseModel, ConfigDict

from aegis_core.kernel import ControlPlaneKernel, default_kernel
from aegis_sdk.decorators import aegis_gate_l1, aegis_gate_l3, aegis_tool
from examples.treasury_case_study.feeds import (
    live_erp_amount,
    live_fx_rate,
    live_risk_score,
)


MASTER_TREASURY_SCOPES: list[str] = [
    "erp:read",
    "fx:quote",
    "swift:wire",
    "kyc:check",
    "audit:log",
]


@aegis_gate_l1("TreasurySettlementSchema")
class TreasurySettlementSchema(BaseModel):
    """Gate L1 Schema for Branch A FX & Settlement A2A messages."""

    model_config = ConfigDict(extra="allow")

    invoice_id: str = "INV-2026-9981"
    supplier: str = "Siemens Energy AG"
    iban: str = "DE89370400440532013000"
    amount_usd: float
    fx_rate: float
    settlement_eur: float
    provider: Optional[str] = None


@aegis_gate_l3("financial_conservation_invariant")
def verify_financial_conservation(data: dict[str, Any]) -> tuple[bool, str]:
    """
    Gate L3 Financial Conservation Invariant:
      1. |amount_usd * fx_rate - settlement_eur| < 0.01
      2. 0 < settlement_eur <= 2,000,000.0
    """
    if (
        "amount_usd" not in data
        or "fx_rate" not in data
        or "settlement_eur" not in data
    ):
        return True, "NON_FX_PAYLOAD_SKIPPED"

    amount_usd = float(data["amount_usd"])
    fx_rate = float(data["fx_rate"])
    settlement_eur = float(data["settlement_eur"])

    if not (0.0 < settlement_eur <= 2_000_000.0):
        return (
            False,
            f"Settlement ceiling violation: settlement_eur={settlement_eur} outside (0, 2000000.0]",
        )

    if not (0.5 <= fx_rate <= 1.5):
        return (
            False,
            f"FX rate bounds violation: fx_rate={fx_rate} outside [0.5, 1.5]",
        )

    diff = abs(amount_usd * fx_rate - settlement_eur)
    if diff >= 0.01:
        return (
            False,
            f"Financial conservation violation: |{amount_usd} * {fx_rate} - {settlement_eur}| = {diff:.4f} >= 0.01",
        )
    return True, "CONSERVATION_VERIFIED"


@aegis_tool(
    tool_name="erp:read",
    required_scope="erp:read",
    mutates_state=False,
    description="Read approved corporate supplier invoice from SAP S/4HANA ERP",
)
async def erp_read(invoice_id: str = "INV-2026-9981", **kwargs: Any) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    amount_usd = float(kwargs.get("amount_usd", 2000000.0))
    if "amount_usd" not in kwargs and kwargs.get("feed_mode") == "live":
        amount_usd = live_erp_amount(invoice_id)
    return {
        "invoice_id": invoice_id,
        "supplier": kwargs.get("supplier", "Siemens Energy AG"),
        "iban": kwargs.get("iban", "DE89370400440532013000"),
        "amount_usd": amount_usd,
        "currency": "USD",
    }


@aegis_tool(
    tool_name="fx:quote",
    required_scope="fx:quote",
    mutates_state=False,
    provenance_fields=["fx_rate", "amount_usd"],
    fallback_tool_name="fx:quote_reuters",
    description="Fetch live institutional USD/EUR FX rate from Bloomberg Primary feed",
)
async def fx_quote(amount_usd: float = 2000000.0, **kwargs: Any) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    fx_rate = 0.9200
    if kwargs.get("feed_mode") == "live":
        fx_rate = live_fx_rate()
    return {
        "provider": "Bloomberg_Primary",
        "pair": "USD/EUR",
        "fx_rate": fx_rate,
        "amount_usd": float(amount_usd),
        "settlement_eur": round(float(amount_usd) * fx_rate, 2),
    }


@aegis_tool(
    tool_name="fx:quote_reuters",
    required_scope="fx:quote",
    mutates_state=False,
    provenance_fields=["fx_rate", "amount_usd"],
    description="Fetch fallback institutional USD/EUR FX rate from Reuters Fallback feed",
)
async def fx_quote_reuters(
    amount_usd: float = 2000000.0, **kwargs: Any
) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    fx_rate = 0.9200
    if kwargs.get("feed_mode") == "live":
        fx_rate = live_fx_rate()
    return {
        "provider": "Reuters_Fallback",
        "pair": "USD/EUR",
        "fx_rate": fx_rate,
        "amount_usd": float(amount_usd),
        "settlement_eur": round(float(amount_usd) * fx_rate, 2),
    }


@aegis_tool(
    tool_name="swift:wire",
    required_scope="swift:wire",
    mutates_state=True,
    provenance_fields=["fx_rate", "amount_usd"],
    parameter_constraints={
        "settlement_eur": {"min": 1.0, "max": 2000000.0},
        "fx_rate": {"min": 0.5, "max": 1.5},
    },
    description="Execute state-mutating cross-border SWIFT MT103 wire settlement",
)
async def swift_wire(
    invoice_id: str = "INV-2026-9981",
    supplier: str = "Siemens Energy AG",
    iban: str = "DE89370400440532013000",
    amount_usd: float = 2000000.0,
    fx_rate: float = 0.9200,
    settlement_eur: float = 1840000.0,
    provider: str = "Bloomberg_Primary",
    **kwargs: Any,
) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    if kwargs.get("feed_mode") == "live" and "settlement_eur" not in kwargs:
        settlement_eur = round(float(amount_usd) * float(fx_rate), 2)
    return {
        "uetr": f"SWIFT-MT103-{uuid4().hex[:10].upper()}",
        "invoice_id": invoice_id,
        "supplier": supplier,
        "iban": iban,
        "amount_usd": float(amount_usd),
        "fx_rate": float(fx_rate),
        "settlement_eur": float(settlement_eur),
        "provider": provider,
        "wire_status": "SETTLED",
    }


@aegis_tool(
    tool_name="kyc:check",
    required_scope="kyc:check",
    mutates_state=False,
    description="Run OFAC & EU sanctions counterparty screening",
)
async def kyc_check(
    supplier: str = "Siemens Energy AG", **kwargs: Any
) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    risk = 0.02
    if kwargs.get("feed_mode") == "live":
        risk = live_risk_score(supplier)
    return {
        "supplier": supplier,
        "sanctions_status": "CLEAR",
        "risk_score": risk,
    }


@aegis_tool(
    tool_name="audit:log",
    required_scope="audit:log",
    mutates_state=False,
    description="Write compliance clearance record to immutable audit log",
)
async def audit_log(
    invoice_id: str = "INV-2026-9981",
    supplier: str = "Siemens Energy AG",
    sanctions_status: str = "CLEAR",
    **kwargs: Any,
) -> dict[str, Any]:
    delay = float(kwargs.get("simulate_delay_sec", 0.002))
    await asyncio.sleep(delay)
    return {
        "audit_id": "AUD-901",
        "invoice_id": invoice_id,
        "supplier": supplier,
        "sanctions_status": sanctions_status,
        "logged": True,
    }


def register_treasury_plugin(
    kernel: Optional[ControlPlaneKernel] = None,
) -> ControlPlaneKernel:
    """Register all 6 Corporate Treasury tools and Gate L1/L3 validators into a ControlPlaneKernel."""
    target = kernel or default_kernel
    for fn in (
        erp_read,
        fx_quote,
        fx_quote_reuters,
        swift_wire,
        kyc_check,
        audit_log,
    ):
        spec = getattr(fn, "_aegis_tool_spec")
        target.register_tool(spec, handler=fn)

    target.c2.register_gate_l1_validator(
        "TreasurySettlementSchema",
        lambda payload: TreasurySettlementSchema.model_validate(payload),
    )
    target.c2.register_gate_l3_validator(
        "financial_conservation_invariant",
        verify_financial_conservation,
    )
    return target
