"""Mock enterprise treasury tools with multi-tier provider failover support."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

DEFAULT_PRIMARY_PROVIDERS: dict[str, str] = {
    "erp:read": "sap_s4_primary",
    "fx:quote": "bloomberg_primary",
    "swift:wire": "swift_gpi_primary",
    "kyc:check": "ofac_eu_primary",
    "audit:log": "worm_ledger_primary",
}


async def erp_read_tool(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Mock SAP/Oracle/Replica ERP Invoice Reader (erp:read)."""
    delay = float(tool_args.get("simulate_latency_sec", 0.002))
    await asyncio.sleep(delay)
    invoice_id = str(tool_args.get("invoice_id", "INV-2026-9981"))
    provider = str(tool_args.get("provider", DEFAULT_PRIMARY_PROVIDERS["erp:read"]))
    return {
        "invoice_id": invoice_id,
        "beneficiary": "SIEMENS_ENERGY_DE",
        "iban": "DE89370400440532013000",
        "amount_usd": 2_000_000.0,
        "currency": "USD",
        "target_currency": "EUR",
        "provider": provider,
        "due_status": "APPROVED_FOR_SETTLEMENT",
        "timestamp": time.time(),
    }


async def fx_quote_tool(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Mock Institutional FX Quote Feed for Bloomberg Primary, Reuters Fallback, and ECB Backup (fx:quote)."""
    delay = float(tool_args.get("simulate_latency_sec", 0.002))
    await asyncio.sleep(delay)
    provider = str(tool_args.get("provider", "bloomberg_primary"))
    amount_usd = float(tool_args.get("amount_usd", 2_000_000.0))

    if provider == "reuters_fallback":
        fx_rate = 0.9205
    elif provider == "ecb_reference_backup":
        fx_rate = 0.9202
    else:
        fx_rate = 0.9200

    settlement_eur = round(amount_usd * fx_rate, 2)
    return {
        "pair": "USD/EUR",
        "provider": provider,
        "amount_usd": amount_usd,
        "fx_rate": fx_rate,
        "settlement_eur": settlement_eur,
        "quote_id": f"FXQ-{uuid4().hex[:8].upper()}",
        "timestamp": time.time(),
    }


async def swift_wire_tool(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Mock SWIFT MT103 / SEPA Target2 Cross-Border Wire Settlement Engine (swift:wire)."""
    delay = float(tool_args.get("simulate_latency_sec", 0.002))
    await asyncio.sleep(delay)
    amount_usd = float(tool_args.get("amount_usd", 2_000_000.0))
    fx_rate = float(tool_args.get("fx_rate", 0.9200))
    settlement_eur = float(
        tool_args.get(
            "settlement_eur",
            tool_args.get("amount_eur", round(amount_usd * fx_rate, 2)),
        )
    )
    return {
        "uetr": str(uuid4()),
        "swift_message_type": "MT103",
        "invoice_id": str(tool_args.get("invoice_id", "INV-2026-9981")),
        "beneficiary": str(tool_args.get("beneficiary", "SIEMENS_ENERGY_DE")),
        "iban": str(tool_args.get("iban", "DE89370400440532013000")),
        "amount_usd": amount_usd,
        "fx_rate": fx_rate,
        "settlement_eur": settlement_eur,
        "provider": str(tool_args.get("provider", "bloomberg_primary")),
        "wire_network": str(
            tool_args.get("wire_network", DEFAULT_PRIMARY_PROVIDERS["swift:wire"])
        ),
        "wire_status": "SETTLED",
        "timestamp": time.time(),
    }


async def kyc_check_tool(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Mock OFAC / WorldCheck / UN Sanctions & KYC Counterparty Screener (kyc:check)."""
    delay = float(tool_args.get("simulate_latency_sec", 0.002))
    await asyncio.sleep(delay)
    provider = str(tool_args.get("provider", DEFAULT_PRIMARY_PROVIDERS["kyc:check"]))
    return {
        "beneficiary": str(tool_args.get("beneficiary", "SIEMENS_ENERGY_DE")),
        "iban": str(tool_args.get("iban", "DE89370400440532013000")),
        "kyc_status": "CLEAR",
        "provider": provider,
        "sanctions_lists_checked": ["OFAC_SDN", "EU_CONSOLIDATED", "UN_SC"],
        "risk_score": 0.01,
        "screening_id": f"KYC-{uuid4().hex[:8].upper()}",
        "timestamp": time.time(),
    }


async def audit_log_tool(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Mock Immutable Compliance Ledger Writer with DR Vault Failover (audit:log)."""
    delay = float(tool_args.get("simulate_latency_sec", 0.002))
    await asyncio.sleep(delay)
    provider = str(tool_args.get("provider", DEFAULT_PRIMARY_PROVIDERS["audit:log"]))
    return {
        "audit_id": f"AUD-{uuid4().hex[:8].upper()}",
        "invoice_id": str(tool_args.get("invoice_id", "INV-2026-9981")),
        "beneficiary": str(tool_args.get("beneficiary", "SIEMENS_ENERGY_DE")),
        "kyc_status": str(tool_args.get("kyc_status", "CLEAR")),
        "provider": provider,
        "audit_status": "LOGGED_IMMUTABLE",
        "timestamp": time.time(),
    }


TOOL_DISPATCH_TABLE = {
    "erp:read": erp_read_tool,
    "fx:quote": fx_quote_tool,
    "swift:wire": swift_wire_tool,
    "kyc:check": kyc_check_tool,
    "audit:log": audit_log_tool,
}


async def execute_mock_tool(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch and execute the requested mock tool."""
    if tool_name not in TOOL_DISPATCH_TABLE:
        raise ValueError(f"Unknown tool_name: {tool_name}")
    handler = TOOL_DISPATCH_TABLE[tool_name]
    return await handler(tool_args)
