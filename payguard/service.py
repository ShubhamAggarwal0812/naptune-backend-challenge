"""Parts 2 and 3: send a payment at most once, then find out whether money really moved."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .decision import Decision, Observation, Outcome, PaymentRequest, assess
from .store import PRE_SUBMIT, DuplicateInvoice, Payment, State, Store

CALL_TIMEOUT = timedelta(seconds=30)      # a SUBMITTING row older than this is not a live call
UNKNOWN_DEADLINE = timedelta(minutes=30)  # after this, an unknown outcome goes to a person
DEDUPE_WINDOW = timedelta(hours=24)       # how long the provider remembers keys (assumed)
SETTLEMENT_DEADLINE = timedelta(days=1)   # NEFT/RTGS should settle well within T+1
WATCH_WINDOW = timedelta(days=7)          # closed payments are re-checked this long for late news
REVIEW_AFTER = timedelta(hours=4)         # a payment held this long joins the review queue

# Resending is only safe while the provider still remembers the key.
if UNKNOWN_DEADLINE >= DEDUPE_WINDOW:
    raise RuntimeError("UNKNOWN_DEADLINE must be shorter than the provider's DEDUPE_WINDOW")

# Hold reasons a person should see straight away, not after REVIEW_AFTER.
REVIEW_NOW = {"BANK_CHANGE_PENDING", "AGENT_CLAIM_CONTRADICTED", "FUTURE_TIMESTAMP"}

# Provider answers meaning the payment reached it. Whether money moved is _reconcile()'s job.
REACHED_PROVIDER = {"ACCEPTED", "SETTLED", "FAILED", "RETURNED"}
DEFINITE_ANSWERS = REACHED_PROVIDER | {"REJECTED"}
PROVIDER_FAILED = {"FAILED", "RETURNED"}

# States whose outcome can still change, so run_pending() keeps checking them.
WATCHED = (State.PROVIDER_ACCEPTED, State.SETTLED, State.RETURNED, State.NEEDS_HUMAN)

LABELS = {
    State.NEW: "Received, not assessed yet.",
    State.HELD: "On hold: evidence missing, stale or conflicting. Nothing sent.",
    State.REJECTED: "Rejected on current evidence. Nothing sent.",
    State.SUBMITTING: "Sending to the provider.",
    State.OUTCOME_UNKNOWN: "Outcome unknown, checking with the provider. Do not retry or pay again.",
    State.PROVIDER_ACCEPTED: (
        "Settlement pending: the provider has it, the bank hasn't confirmed the outcome. "
        "Do not retry or pay again."
    ),
    State.PROVIDER_REJECTED: "The provider rejected it. No money moved.",
    State.SETTLED: "Paid: bank debit confirmed.",
    State.RETURNED: (
        "Failed or returned. Check the bank statement before anyone re-pays; "
        "re-paying is a human decision."
    ),
    State.NEEDS_HUMAN: "Needs a person: couldn't be resolved automatically. Do not retry or pay again.",
}


class ProviderError(Exception):
    """Anything that isn't a definitive answer from the provider: timeout, reset, 5xx."""


class ProviderTimeout(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ConflictingRequest(Exception):
    """A different supplier or amount for an invoice that was already sent."""


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
    def bank_debit(self, ref: str) -> int | None: ...   # paise debited from us, None if not seen
    def bank_return(self, ref: str) -> int | None: ...  # paise credited back to us, None if not seen


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
                    req.invoice_id, req.supplier_id, req.amount_paise, req.agent_id,
                    f"pay-{req.invoice_id}", self.clock(),
                )
            except DuplicateInvoice:  # a concurrent request created it first
                p = self.store.get_by_invoice(req.invoice_id)
        if p.state in PRE_SUBMIT:
            # Nothing has been sent, so this request replaces the last one and is assessed afresh.
            return self._decide(p, req)
        if (req.supplier_id, req.amount_paise) != (p.supplier_id, p.amount_paise):
            raise ConflictingRequest(f"{req.invoice_id} was already sent for a different supplier or amount")
        # Already sent or in flight. Only run_pending() talks to the provider about it now.
        return p

    def _decide(self, p: Payment, req: PaymentRequest) -> Payment:
        evidence = self.evidence(req)
        now = self.clock()  # read after gathering, so evidence ages are measured at the decision
        decision = assess(req, evidence, now)
        detail = _decision_detail(decision, req, evidence)
        asked = {"supplier_id": req.supplier_id, "amount_paise": req.amount_paise, "agent_id": req.agent_id}
        if decision.outcome is Outcome.PROCEED:
            # Write-ahead: SUBMITTING, the key and the payee are committed before the provider call.
            sending = self.store.transition(
                p, State.SUBMITTING, detail, now,
                payee_account=decision.payee_account, submitted_at=now, **asked,
            )
            return self._send(sending) if sending else self.store.get(p.id)
        to = State.REJECTED if decision.outcome is Outcome.REJECT else State.HELD
        return self._move(p, to, detail, **asked)

    # -- Sending ---------------------------------------------------------------

    def _send(self, p: Payment) -> Payment:
        """p is already SUBMITTING in the database."""
        try:
            status = self.provider.pay(p.idempotency_key, p.invoice_id, p.amount_paise, p.payee_account)
        except Exception as e:
            # Timeout, reset, 5xx, even a bug in the client: it may or may not have executed.
            return self._move(p, State.OUTCOME_UNKNOWN, {"error": repr(e)})
        if status.state not in DEFINITE_ANSWERS:
            return self._move(p, State.OUTCOME_UNKNOWN, {"unexpected answer": status.state})
        return self._record_provider_answer(p, status)

    def _record_provider_answer(self, p: Payment, status: ProviderStatus) -> Payment:
        if status.state == "REJECTED":
            return self._move(p, State.PROVIDER_REJECTED, {"provider": status.state})
        accepted = self._move(
            p, State.PROVIDER_ACCEPTED, {"provider": status.state, "ref": status.ref},
            provider_ref=status.ref, accepted_at=self.clock(),
        )
        if status.state in PROVIDER_FAILED and accepted.state is State.PROVIDER_ACCEPTED:
            # Don't show "settlement pending" for a payment the provider says failed: ask the bank now,
            # using the answer we just got (a lookup straight after may not have caught up yet).
            return self._reconcile(accepted, provider_said=status.state)
        return accepted

    # -- Recovery: runs at startup and on a timer ------------------------------

    def run_pending(self) -> list[tuple[str, str]]:
        """Pick up in-flight work from the database; nothing here relies on memory.

        Each payment is handled on its own, so one that raises can't stop the rest.
        Returns (invoice_id, error) for those that raised; in production, a log line and an alert.
        """
        errors: list[tuple[str, str]] = []

        def each(payments: list[Payment], step: Callable[[Payment], Any]) -> None:
            for p in payments:
                try:
                    step(p)
                except Exception as e:
                    errors.append((p.invoice_id, repr(e)))

        now = self.clock()
        stuck = [p for p in self.store.in_state(State.SUBMITTING) if now - p.updated_at > CALL_TIMEOUT]
        note = {"note": "found SUBMITTING after a restart; the call may have gone out"}
        each(stuck, lambda p: self._move(p, State.OUTCOME_UNKNOWN, note))
        each(self.store.in_state(State.OUTCOME_UNKNOWN), self._resolve_unknown)
        watched = [
            p for state in WATCHED for p in self.store.in_state(state)
            if state is State.PROVIDER_ACCEPTED or now - p.updated_at <= WATCH_WINDOW
        ]
        each(watched, self._reconcile)
        return errors

    def _resolve_unknown(self, p: Payment) -> Payment:
        """Ask the provider before resending, and resend only with the same key."""
        try:
            status: ProviderStatus | None = self.provider.lookup(p.idempotency_key)
            answer = status.state
        except Exception as e:
            status, answer = None, f"lookup failed: {e!r}"
        if status is not None and status.state in DEFINITE_ANSWERS:
            return self._record_provider_answer(p, status)
        if self.clock() - p.submitted_at > UNKNOWN_DEADLINE:
            why = "outcome still unknown at deadline"
            return self._move(p, State.NEEDS_HUMAN, {"why": why, "lookup": answer})
        if answer != "NOT_FOUND":
            return p  # no usable answer; ask again on the next run

        # NOT_FOUND, so a resend is in effect a new payment: check the evidence again first.
        req = PaymentRequest(p.invoice_id, p.supplier_id, p.amount_paise, p.agent_id)
        evidence = self.evidence(req)
        decision = assess(req, evidence, self.clock())
        if decision.outcome is not Outcome.PROCEED or decision.payee_account != p.payee_account:
            # Not HELD: the first request may still land at the provider, so this isn't "nothing sent".
            why = "provider has no record and the evidence no longer supports sending it"
            return self._move(p, State.NEEDS_HUMAN, {"why": why, **_decision_detail(decision, req, evidence)})
        note = "provider has no record and the evidence still holds; resending with the same key"
        sending = self.store.transition(p, State.SUBMITTING, {"note": note}, self.clock())
        return self._send(sending) if sending else self.store.get(p.id)

    # -- Part 3: did money really move? ----------------------------------------

    def _reconcile(self, p: Payment, provider_said: str | None = None) -> Payment:
        """The provider accepting the instruction isn't money moving. The bank is the proof."""
        if p.state not in WATCHED:
            return p
        view, ref = self._observe(p, provider_said)
        self.store.save_view(p, view)

        if p.state is State.NEEDS_HUMAN:
            # A person owns it. Keep their facts current, but never change the state for them.
            if ref and not p.provider_ref:
                note = {"note": "provider reference found", "view": view}
                return self._move(p, State.NEEDS_HUMAN, note, provider_ref=ref)
            return p

        target, why = _what_the_money_says(p, view)
        if p.state is State.PROVIDER_ACCEPTED:
            if target is not None:
                return self._move(p, target, {"why": why, "view": view})
            if self.clock() - p.accepted_at > SETTLEMENT_DEADLINE:
                why = "bank hasn't confirmed by the deadline"
                return self._move(p, State.NEEDS_HUMAN, {"why": why, "view": view})
            return p

        # SETTLED or RETURNED, inside the watch window: has the picture changed since?
        if target is not None and target is not p.state:
            to = State.RETURNED if target is State.RETURNED else State.NEEDS_HUMAN
            return self._move(p, to, {"why": f"changed after {p.state}: {why}", "view": view})
        return p

    def _observe(self, p: Payment, provider_said: str | None = None) -> tuple[dict[str, Any], str | None]:
        """Ask every source about this payment. One source failing never blocks the others."""
        if provider_said is not None:
            provider: Any = ProviderStatus(provider_said, p.provider_ref)
        else:
            provider = _read(lambda: self.provider.lookup(p.idempotency_key))
        found = provider if isinstance(provider, ProviderStatus) else None
        ref = p.provider_ref or (found.ref if found else None)

        def by_ref(fetch: Callable[[str], Any]) -> Any:
            return _read(lambda: fetch(ref)) if ref else "NO_REFERENCE"

        view = {
            "provider": found.state if found else provider,
            "ledger": _read(lambda: self.downstream.ledger_status(p.invoice_id)),
            "settlement": by_ref(self.downstream.settlement_status),
            "bank_debit": by_ref(self.downstream.bank_debit),
            "bank_return": by_ref(self.downstream.bank_return),
        }
        return view, ref

    # -- What people see -------------------------------------------------------

    def status(self, invoice_id: str) -> dict[str, Any] | None:
        """What the agent or an ops screen sees. While sources disagree, it says so."""
        p = self.store.get_by_invoice(invoice_id)
        if p is None:
            return None
        return {
            "invoice_id": invoice_id,
            "state": p.state,
            "label": LABELS[p.state],
            "sources": p.view,
            "history": self.store.history(p.id),
        }

    def review_queue(self) -> list[dict[str, Any]]:
        """What a person in accounts payable or treasury should look at.

        Every NEEDS_HUMAN payment; a HELD one straight away if its reasons need a person
        (a bank change, a contradicting agent, a clock problem), otherwise once it has
        been waiting longer than REVIEW_AFTER.
        """
        now = self.clock()
        queue = []
        for p in self.store.in_state(State.NEEDS_HUMAN):
            queue.append({"invoice_id": p.invoice_id, "state": p.state, "why": [self._latest(p, "why")]})
        for p in self.store.in_state(State.HELD):
            reasons = self._latest(p, "reasons") or []
            if {r["code"] for r in reasons} & REVIEW_NOW or now - p.created_at > REVIEW_AFTER:
                why = [r["detail"] for r in reasons]
                queue.append({"invoice_id": p.invoice_id, "state": p.state, "why": why})
        return queue

    def _latest(self, p: Payment, key: str) -> Any:
        """The most recent value of `key` in this payment's audit trail."""
        events = reversed(self.store.history(p.id))
        return next((e["detail"][key] for e in events if key in e["detail"]), None)

    def _move(self, p: Payment, to: State, detail: Mapping[str, Any], **fields: Any) -> Payment:
        moved = self.store.transition(p, to, detail, self.clock(), **fields)
        return moved or self.store.get(p.id)  # someone else moved it first; theirs stands


def _what_the_money_says(p: Payment, view: Mapping[str, Any]) -> tuple[State | None, str]:
    """Where the bank and the provider together put this payment. None: not known yet.

    The bank decides. The provider's word only counts when the bank has nothing to contradict it.
    """
    debit, back = view["bank_debit"], view["bank_return"]
    provider_failed = view["provider"] in PROVIDER_FAILED
    if isinstance(debit, int) and debit != p.amount_paise:
        return State.NEEDS_HUMAN, "bank debit amount differs from the payment"
    if isinstance(back, int):
        if back == p.amount_paise:
            return State.RETURNED, "bank shows the money came back"
        return State.NEEDS_HUMAN, "bank return amount differs from the payment"
    if isinstance(debit, int):
        if provider_failed:
            why = f"bank shows the debit but the provider says {view['provider']}, and nothing has come back"
            return State.NEEDS_HUMAN, why
        return State.SETTLED, "bank debit matches"
    if provider_failed and debit == "NOT_SEEN":
        return State.RETURNED, f"provider says {view['provider']} and the bank shows no debit"
    return None, "waiting for the bank"


def _decision_detail(
    decision: Decision, req: PaymentRequest, evidence: Mapping[str, Observation]
) -> dict[str, Any]:
    """A decision as it goes into the audit log, with everything it was based on."""
    return {
        "decision": decision.outcome,
        "reasons": [{"outcome": r.outcome, "code": r.code, "detail": r.detail} for r in decision.reasons],
        "agent_claims": dict(req.agent_claims),
        "evidence": {
            name: {"available": o.available, "as_of": o.as_of, "data": dict(o.data)}
            for name, o in evidence.items()
        },
    }


def _read(fetch: Callable[[], Any]) -> Any:
    """One source's current answer, or UNAVAILABLE. Never lets one source block the others."""
    try:
        value = fetch()
    except Exception:
        return "UNAVAILABLE"
    return "NOT_SEEN" if value is None else value
