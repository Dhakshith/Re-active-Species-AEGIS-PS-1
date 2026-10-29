"""
Live Market & ERP Feeds for Corporate Treasury & Cross-Border SWIFT Settlement.
Provides:
- `live_fx_rate()`: bounded random-walk in [0.918, 0.923]
- `live_risk_score()`: bounded counterparty KYC risk score in [0.01, 0.04]
- `live_erp_amount(invoice_id)`: deterministic hash-derived invoice amount in [500k, 1.99M]
  (with INV-2026-9981 pinned at 2,000,000.0).
"""

from __future__ import annotations

import hashlib
import random


_CURRENT_FX_RATE: float = 0.9200


def live_fx_rate() -> float:
    """Bounded random-walk institutional USD/EUR rate in [0.9180, 0.9230]."""
    global _CURRENT_FX_RATE
    delta = random.uniform(-0.0008, 0.0008)
    _CURRENT_FX_RATE = max(0.9180, min(0.9230, _CURRENT_FX_RATE + delta))
    return round(_CURRENT_FX_RATE, 4)


def live_risk_score(supplier: str = "Siemens Energy AG") -> float:
    """Live counterparty sanctions risk score in [0.010, 0.045]."""
    seed_byte = hashlib.sha256(supplier.encode("utf-8")).digest()[0]
    base = 0.012 + (seed_byte % 20) * 0.001
    jitter = random.uniform(-0.002, 0.004)
    return round(max(0.005, min(0.049, base + jitter)), 3)


def live_erp_amount(invoice_id: str = "INV-2026-9981") -> float:
    """
    Compute invoice USD amount via invoice-id SHA-256 hash so any novel invoice ID
    produces a realistic corporate invoice in [500k, 1.99M] ([500_000.0, 1_990_000.0])
    that respects the 2,000,000 EUR Gate L3 settlement ceiling at ~0.92 FX rates
    (note: INV-2026-9981 is pinned at 2,000,000.0).
    """
    if invoice_id == "INV-2026-9981":
        return 2000000.0
    digest = hashlib.sha256(invoice_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 150  # 0 .. 149
    amount_usd = 500_000.0 + (bucket * 10_000.0)
    return round(float(amount_usd), 2)
