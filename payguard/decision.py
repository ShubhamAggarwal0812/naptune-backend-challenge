"""Part 1: decide whether a proposed payment may go ahead.

Every fact has one system of record and a maximum age. Data that is stale or
unavailable can't satisfy a check. The agent's own claims are compared with the
systems of record but never count as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping


class Outcome(StrEnum):
    PROCEED = "PROCEED"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    REJECT = "REJECT"


# The evidence policy for one action type (supplier payments): the sources that
# are required and how old their data may be. The numbers are my assumptions.
MAX_AGE: dict[str, timedelta] = {
    "authority": timedelta(minutes=5),
    "procurement": timedelta(minutes=15),
    "vendor_master": timedelta(minutes=15),
    "duplicate_check": timedelta(minutes=2),  # how far behind its event stream it may run
    "ledger": timedelta(minutes=2),
    "compliance": timedelta(hours=1),
}

# Why old data from a source is a problem, shown to whoever reviews a hold.
STALE_MEANS = {
    "duplicate_check": "a payment made in that window would not show",
    "compliance": "sanctions and watchlists change daily",
}


@dataclass(frozen=True)
class PaymentRequest:
    invoice_id: str
    supplier_id: str
    amount_paise: int  # money as integer paise, never floats
    agent_id: str
    # What the agent says is true. Checked against the systems of record, never trusted.
    agent_claims: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    """What one source told us, and the time its data was true as of."""

    available: bool
    as_of: datetime | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reason:
    outcome: Outcome  # REJECT or NEED_MORE_EVIDENCE
    code: str
    detail: str


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: tuple[Reason, ...]
    payee_account: str | None  # from the vendor master; the agent never picks the account


def assess(req: PaymentRequest, evidence: Mapping[str, Observation], now: datetime) -> Decision:
    reasons: list[Reason] = []
    verified: dict[str, bool] = {}  # facts a fresh system of record confirmed or refuted

    def hold(code: str, detail: str) -> None:
        reasons.append(Reason(Outcome.NEED_MORE_EVIDENCE, code, detail))

    def reject(code: str, detail: str) -> None:
        reasons.append(Reason(Outcome.REJECT, code, detail))

    def usable(source: str) -> Mapping[str, Any] | None:
        """The source's data if it may be used. Otherwise record why not and return None."""
        obs = evidence.get(source)
        why = f"; {STALE_MEANS[source]}" if source in STALE_MEANS else ""
        if obs is None or not obs.available:
            last = ""
            if obs is not None and obs.as_of is not None:
                last = f"; last result {dict(obs.data)} is {_age(now - obs.as_of)} old"
            hold("UNAVAILABLE", f"{source} is unavailable{last}{why}")
            return None
        if obs.as_of is None or now - obs.as_of > MAX_AGE[source]:
            age = f"{_age(now - obs.as_of)} old" if obs.as_of else "undated"
            hold("STALE", f"{source} data is {age} (max {_age(MAX_AGE[source])}){why}")
            return None
        return obs.data

    if (auth := usable("authority")) is not None:
        if not auth["delegation_valid"]:
            reject("DELEGATION_INVALID", f"{req.agent_id}'s delegation is not valid")
        elif req.amount_paise > auth["limit_paise"]:
            reject(
                "OVER_AUTHORITY",
                f"{inr(req.amount_paise)} is above {req.agent_id}'s limit of "
                f"{inr(auth['limit_paise'])}; the parent's higher limit is not the agent's to use",
            )

    if (inv := usable("procurement")) is not None:
        verified["invoice_approved"] = inv["status"] == "APPROVED"
        if inv["status"] != "APPROVED":
            reject("INVOICE_NOT_APPROVED", f"{req.invoice_id} is {inv['status']}")
        if inv["amount_paise"] != req.amount_paise or inv["supplier_id"] != req.supplier_id:
            reject(
                "INVOICE_MISMATCH",
                f"request is {inr(req.amount_paise)} to {req.supplier_id}; invoice is "
                f"{inr(inv['amount_paise'])} to {inv['supplier_id']}",
            )

    payee = None
    if (vendor := usable("vendor_master")) is not None:
        verified["bank_details_unchanged"] = not vendor["pending_bank_change"]
        if vendor["pending_bank_change"]:
            hold(
                "BANK_CHANGE_PENDING",
                "supplier asked to change its bank account and the change isn't approved; "
                "confirm with the supplier out-of-band before paying",
            )
        else:
            payee = vendor["verified_account"]

    if (dup := usable("duplicate_check")) is not None:
        if dup["payments_for_invoice"]:
            reject("DUPLICATE", f"{req.invoice_id} already paid: {dup['payments_for_invoice']}")

    if (ledger := usable("ledger")) is not None:
        verified["sufficient_balance"] = ledger["available_paise"] >= req.amount_paise
        if not verified["sufficient_balance"]:
            reject("INSUFFICIENT_BALANCE", f"available {inr(ledger['available_paise'])}")

    if (comp := usable("compliance")) is not None:
        verified["supplier_valid"] = comp["result"] == "CLEAR"
        if comp["result"] != "CLEAR":
            reject("COMPLIANCE_HIT", f"compliance result for {req.supplier_id} is {comp['result']}")

    verified["all_checks_complete"] = not reasons
    for claim, said in req.agent_claims.items():
        if claim in verified and verified[claim] != said:
            hold(
                "AGENT_CLAIM_CONTRADICTED",
                f"agent said {claim}={said}; the systems of record say {verified[claim]}",
            )

    if any(r.outcome is Outcome.REJECT for r in reasons):
        outcome = Outcome.REJECT
    elif reasons:
        outcome = Outcome.NEED_MORE_EVIDENCE
    else:
        outcome = Outcome.PROCEED
    return Decision(outcome, tuple(reasons), payee if outcome is Outcome.PROCEED else None)


def inr(paise: int) -> str:
    """Rupees with Indian digit grouping: 84000000 -> ₹8,40,000."""
    digits = str(paise // 100)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join(groups + [tail])


def _age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
