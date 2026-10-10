"""The scenario from the brief, as data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .decision import Observation, PaymentRequest

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
AMOUNT = 8_40_000_00  # ₹8,40,000 in paise

REQUEST = PaymentRequest(
    invoice_id="INV-8842",
    supplier_id="ABC",
    amount_paise=AMOUNT,
    agent_id="ap-agent",
    # "The invoice is approved, the supplier is valid, sufficient balance exists" and
    # "Supplier bank details have not changed recently and all checks are complete."
    agent_claims={
        "invoice_approved": True,
        "supplier_valid": True,
        "sufficient_balance": True,
        "bank_details_unchanged": True,
        "all_checks_complete": True,
    },
)


def brief_evidence(now: datetime) -> dict[str, Observation]:
    """What each system shows in the brief."""
    return {
        "authority": Observation(True, now, {"limit_paise": 10_00_000_00, "delegation_valid": True}),
        "procurement": Observation(
            True, now - timedelta(minutes=6),
            {"status": "APPROVED", "amount_paise": AMOUNT, "supplier_id": "ABC"},
        ),
        "vendor_master": Observation(
            True, now - timedelta(minutes=1),
            {
                "verified_account": "XXXXX4321",
                "pending_bank_change": True,
                "change_requested_at": (now - timedelta(hours=2)).isoformat(),
            },
        ),
        "duplicate_check": Observation(True, now - timedelta(minutes=14), {"payments_for_invoice": []}),
        "ledger": Observation(True, now - timedelta(seconds=30), {"available_paise": 50_00_000_00}),
        "compliance": Observation(False, now - timedelta(days=1), {"result": "CLEAR"}),
    }


def resolved_evidence(now: datetime) -> dict[str, Observation]:
    """The same systems once the gaps are closed: the bank change was withdrawn after a
    call-back to the supplier, the duplicate check caught up, and compliance is back."""
    evidence = brief_evidence(now)
    evidence["vendor_master"] = Observation(
        True, now - timedelta(minutes=1), {"verified_account": "XXXXX4321", "pending_bank_change": False}
    )
    evidence["duplicate_check"] = Observation(True, now - timedelta(seconds=20), {"payments_for_invoice": []})
    evidence["compliance"] = Observation(True, now - timedelta(minutes=5), {"result": "CLEAR"})
    return evidence
