"""Parts 2 and 3: send a payment at most once, then find out whether money really moved."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .decision import Observation, Outcome, PaymentRequest, assess
from .store import PRE_SUBMIT, DuplicateInvoice, Payment, State, Store

CALL_TIMEOUT = timedelta(seconds=30)      # a SUBMITTING row older than this is not a live call
UNKNOWN_DEADLINE = timedelta(minutes=30)  # after this, an unknown outcome goes to a person
DEDUPE_WINDOW = timedelta(hours=24)       # how long the provider remembers keys (assumed)
SETTLEMENT_DEADLINE = timedelta(days=1)   # NEFT/RTGS should settle well within T+1

# Resending is only safe while the provider still remembers the key.
assert UNKNOWN_DEADLINE < DEDUPE_WINDOW

# Provider states meaning the payment reached the provider. reconcile() works out the rest.
REACHED_PROVIDER = {"ACCEPTED", "SETTLED", "FAILED", "RETURNED"}

LABELS = {
    State.NEW: "Received, not assessed yet.",
    State.HELD: "On hold: evidence missing, stale or conflicting. Nothing sent.",
    State.REJECTED: "Rejected on current evidence. Nothing sent.",
    State.SUBMITTING: "Sending to the provider.",
    State.OUTCOME_UNKNOWN: "Outcome unknown, checking with the provider. Do not retry or pay again.",
    State.PROVIDER_ACCEPTED: (
        "Settlement pending: the provider accepted it, the bank hasn't confirmed. "
        "Do not retry or pay again."
    ),
    State.PROVIDER_REJECTED: "The provider rejected it. No money moved.",
    State.SETTLED: "Paid: bank debit confirmed.",
    State.RETURNED: "Failed or returned after the provider accepted it. Re-paying is a human decision.",
    State.NEEDS_HUMAN: "Needs a person: couldn't be resolved automatically. Do not retry or pay again.",
}


class ProviderError(Exception):
    """Anything that isn't a definitive answer from the provider: timeout, reset, 5xx."""


class ProviderTimeout(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ConflictingRequest(Exception):
    """Same invoice, different supplier or amount. Not a retry, and not a new payment."""


@dataclass(frozen=True)
class ProviderStatus:
    state: str  # ACCEPTED | REJECTED | NOT_FOUND | SETTLED | FAILED | RETURNED
    ref: str | None = None  # provider reference / UTR


class Provider(Protocol):
    def pay(self, key: str, invoice_id: str, amount_paise: int, account: str) -> ProviderStatus: ...
    def lookup(self, key: str) -> ProviderStatus: ...


class Downstream(Protocol):
    def ledger_status(self, invoice_id: str) -> str: ...
    def settlement_status(self, ref: str) -> str: ...
    def bank_debit(self, ref: str) -> int | None: ...  # amount debited in paise, None if not seen


class PaymentService:
    def __init__(
        self,
        store: Store,
        evidence: Callable[[PaymentRequest], Mapping[str, Observation]],
        provider: Provider,
        downstream: Downstream,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.evidence = evidence
        self.provider = provider
        self.downstream = downstream
        self.clock = clock

    # -- Intake: the agent's entry point --------------------------------------

    def handle(self, req: PaymentRequest) -> Payment:
        """Safe to call any number of times for the same invoice."""
        p = self.store.get_by_invoice(req.invoice_id)
        if p is None:
            try:
                p = self.store.create(
                    req.invoice_id, req.supplier_id, req.amount_paise,
                    fingerprint(req), f"pay-{req.invoice_id}", self.clock(),
                )
            except DuplicateInvoice:  # a concurrent request created it first
                p = self.store.get_by_invoice(req.invoice_id)
        if p.fingerprint != fingerprint(req):
            raise ConflictingRequest(
                f"{req.invoice_id} already exists with a different supplier or amount"
            )
        if p.state not in PRE_SUBMIT:
            # Already sent or in flight. Only run_pending() talks to the provider about it now.
            return p
        return self._decide(p, req)

    def _decide(self, p: Payment, req: PaymentRequest) -> Payment:
        now = self.clock()
        evidence = self.evidence(req)
        decision = assess(req, evidence, now)
        detail = {
            "decision": decision.outcome,
            "reasons": [
                {"outcome": r.outcome, "code": r.code, "detail": r.detail} for r in decision.reasons
            ],
            "agent_claims": dict(req.agent_claims),
            "evidence": {
                name: {"available": o.available, "as_of": o.as_of, "data": dict(o.data)}
                for name, o in evidence.items()
            },
        }
        if decision.outcome is Outcome.PROCEED:
            # Write-ahead: SUBMITTING, the key and the payee are committed before the provider call.
            sending = self.store.transition(
                p, State.SUBMITTING, detail, now,
                payee_account=decision.payee_account, submitted_at=now,
            )
            return self._send(sending) if sending else self.store.get(p.id)
        to = State.REJECTED if decision.outcome is Outcome.REJECT else State.HELD
        return self._move(p, to, detail)

    # -- Sending ---------------------------------------------------------------

    def _send(self, p: Payment) -> Payment:
        """p is already SUBMITTING in the database."""
        try:
            status = self.provider.pay(p.idempotency_key, p.invoice_id, p.amount_paise, p.payee_account)
        except ProviderError as e:
            # The provider may or may not have executed it. Never treat this as failed.
            return self._move(p, State.OUTCOME_UNKNOWN, {"error": repr(e)})
        return self._record_provider_answer(p, status)

    def _record_provider_answer(self, p: Payment, status: ProviderStatus) -> Payment:
        if status.state in REACHED_PROVIDER:
            return self._move(
                p, State.PROVIDER_ACCEPTED, {"provider": status.state, "ref": status.ref},
                provider_ref=status.ref, accepted_at=self.clock(),
            )
        if status.state == "REJECTED":
            return self._move(p, State.PROVIDER_REJECTED, {"provider": status.state})
        raise ValueError(f"unexpected provider state {status.state!r}")

    # -- Recovery: runs at startup and on a timer ------------------------------

    def run_pending(self) -> None:
        """Pick up in-flight work from the database. Nothing here relies on memory."""
        now = self.clock()
        for p in self.store.in_state(State.SUBMITTING):
            if now - p.updated_at > CALL_TIMEOUT:  # not a call still running on a live worker
                note = "found SUBMITTING after a restart; the call may have gone out"
                self._move(p, State.OUTCOME_UNKNOWN, {"note": note})
        for p in self.store.in_state(State.OUTCOME_UNKNOWN):
            self._resolve_unknown(p)
        for p in self.store.in_state(State.PROVIDER_ACCEPTED):
            self.reconcile(p)

    def _resolve_unknown(self, p: Payment) -> Payment:
        """Ask the provider before resending. Resend only with the same key."""
        now = self.clock()
        try:
            status: ProviderStatus | None = self.provider.lookup(p.idempotency_key)
            lookup = status.state
        except ProviderError as e:
            status, lookup = None, f"lookup failed: {e!r}"
        if status is not None and status.state != "NOT_FOUND":
            return self._record_provider_answer(p, status)
        if now - p.submitted_at > UNKNOWN_DEADLINE:
            why = "outcome still unknown at deadline"
            return self._move(p, State.NEEDS_HUMAN, {"why": why, "lookup": lookup})
        if status is None:
            return p  # couldn't ask; try again on the next run
        # NOT_FOUND. If the first request is still in flight, the provider dedupes on the same key.
        note = "provider has no record; resending with the same key"
        sending = self.store.transition(p, State.SUBMITTING, {"note": note}, now)
        return self._send(sending) if sending else self.store.get(p.id)

    # -- Part 3: did money really move? ----------------------------------------

    def reconcile(self, p: Payment) -> Payment:
        """The provider accepting the instruction isn't money moving. A matching bank debit is."""
        view = {
            "provider": _read(lambda: self.provider.lookup(p.idempotency_key).state),
            "ledger": _read(lambda: self.downstream.ledger_status(p.invoice_id)),
            "settlement": _read(lambda: self.downstream.settlement_status(p.provider_ref)),
            "bank_debit": _read(lambda: self.downstream.bank_debit(p.provider_ref)),
        }
        self.store.save_view(p, view)
        debit = view["bank_debit"]
        if view["provider"] in ("FAILED", "RETURNED"):
            return self._move(p, State.RETURNED, {"view": view})
        if isinstance(debit, int):
            if debit == p.amount_paise:
                return self._move(p, State.SETTLED, {"view": view})
            why = "bank debit amount differs from the payment"
            return self._move(p, State.NEEDS_HUMAN, {"why": why, "view": view})
        if self.clock() - p.accepted_at > SETTLEMENT_DEADLINE:
            why = "bank hasn't confirmed by the deadline"
            return self._move(p, State.NEEDS_HUMAN, {"why": why, "view": view})
        return self.store.get(p.id)  # still pending; check again on the next run

    def status(self, invoice_id: str) -> dict[str, Any]:
        """What the agent or an ops screen sees. While sources disagree, it says so."""
        p = self.store.get_by_invoice(invoice_id)
        return {
            "invoice_id": invoice_id,
            "state": p.state,
            "label": LABELS[p.state],
            "sources": p.view,
            "history": self.store.history(p.id),
        }

    def _move(self, p: Payment, to: State, detail: Mapping[str, Any], **fields: Any) -> Payment:
        moved = self.store.transition(p, to, detail, self.clock(), **fields)
        return moved or self.store.get(p.id)  # someone else moved it first; theirs stands


def fingerprint(req: PaymentRequest) -> str:
    raw = f"{req.invoice_id}|{req.supplier_id}|{req.amount_paise}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read(fetch: Callable[[], Any]) -> Any:
    """One source's current answer, or UNAVAILABLE. Never lets one source block the others."""
    try:
        value = fetch()
    except Exception:
        return "UNAVAILABLE"
    return "NOT_SEEN" if value is None else value
