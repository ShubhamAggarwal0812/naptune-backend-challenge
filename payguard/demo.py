"""Walk through the brief end to end:  python -m payguard.demo"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .decision import inr
from .fakes import FakeClock, FakeDownstream, FakeEvidence, FakeProvider
from .scenario import AMOUNT, NOW, REQUEST, brief_evidence, resolved_evidence
from .service import PaymentService
from .store import Store


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "payments.db"
        clock = FakeClock(NOW)
        evidence = FakeEvidence(clock, brief_evidence)
        provider = FakeProvider()
        downstream = FakeDownstream()

        def start() -> PaymentService:  # a fresh process: new objects, same database
            return PaymentService(Store(db), evidence, provider, downstream, clock)

        svc = start()
        step(f"Part 1: agent asks to pay {inr(AMOUNT)} on {REQUEST.invoice_id}, systems as in the brief")
        p = svc.handle(REQUEST)
        print(f"state: {p.state}")
        for reason in svc.store.history(p.id)[-1]["detail"]["reasons"]:
            print(f"  {reason['code']:<26} {reason['detail']}")
        print(f"provider calls: {len(provider.pay_calls)}")

        step("Gaps closed (bank change withdrawn after a call-back, duplicate check caught up, "
             "compliance back). Agent re-sends; the provider executes but the response times out")
        evidence.build = resolved_evidence
        provider.mode = "timeout_after"
        p = svc.handle(REQUEST)
        print(f"state: {p.state}    payments the provider made: {len(provider.executed)}")

        step("Part 2: the service crashes and restarts, and the same request arrives again")
        svc = start()
        clock.advance(minutes=1)
        provider.mode = "ok"
        p = svc.handle(REQUEST)
        print(f"replayed request -> state: {p.state}    provider calls so far: {len(provider.pay_calls)}")
        svc.run_pending()
        p = svc.store.get(p.id)
        print(f"after recovery   -> state: {p.state}    payments the provider made: {len(provider.executed)}")

        step("Part 3: provider says ACCEPTED; ledger PENDING, settlement down, no bank debit yet")
        svc.run_pending()
        status = svc.status(REQUEST.invoice_id)
        print(f"state: {status['state']}\n  {status['label']}\n  sources: {status['sources']}")

        step(f"The bank statement shows a debit of {inr(AMOUNT)} with reference {p.provider_ref}")
        downstream.debits[p.provider_ref] = AMOUNT
        downstream.ledger = "POSTED"
        clock.advance(hours=2)
        svc.run_pending()
        status = svc.status(REQUEST.invoice_id)
        print(f"state: {status['state']}\n  {status['label']}")

        step("Audit trail")
        for event in status["history"]:
            print(f"  {event['at'][11:19]}  {event['from_state'] or '-':>17} -> {event['to_state']}")


def step(title: str) -> None:
    print(f"\n== {title}")


if __name__ == "__main__":
    main()
