"""Shared by both test files."""

from payguard.scenario import resolved_evidence

KEY = "pay-INV-8842"


def reason_codes(svc, invoice_id: str = "INV-8842") -> set[str]:
    """Reason codes from the payment's latest decision."""
    p = svc.store.get_by_invoice(invoice_id)
    decision = next(e for e in reversed(svc.store.history(p.id)) if "reasons" in e["detail"])
    return {r["code"] for r in decision["detail"]["reasons"]}


def evidence_with(**changes):
    """Clean evidence with some sources replaced."""
    def build(now):
        evidence = resolved_evidence(now)
        evidence.update({name: make(now) for name, make in changes.items()})
        return evidence
    return build
