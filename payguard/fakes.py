"""Stand-ins for the outside world, with failures you can switch on."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Mapping

from .decision import Observation, PaymentRequest
from .service import ProviderStatus, ProviderTimeout, ProviderUnavailable


class SimulatedCrash(BaseException):
    """The process dying. A BaseException, so the service's error handling can't catch it."""


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeEvidence:
    """Returns whatever `build(now)` says the source systems currently show."""

    def __init__(self, clock: FakeClock, build: Callable[[datetime], Mapping[str, Observation]]) -> None:
        self.clock = clock
        self.build = build

    def __call__(self, req: PaymentRequest) -> Mapping[str, Observation]:
        return self.build(self.clock())


class FakeProvider:
    """A payment provider that honours idempotency keys, like most real ones claim to.

    mode:
      ok              answers normally
      timeout_before  times out before the request reaches the provider (nothing executed)
      timeout_after   executes, then the response is lost (Case B)
      crash_after     executes, then our process dies before reading the response
      reject          definitively refuses
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.lookup_down = False
        self.pay_calls: list[str] = []  # every key we were called with, including resends
        self.executed: dict[str, dict] = {}  # key -> the one payment the provider made for it

    def pay(self, key: str, invoice_id: str, amount_paise: int, account: str) -> ProviderStatus:
        self.pay_calls.append(key)
        if self.mode == "timeout_before":
            raise ProviderTimeout("timed out before reaching the provider")
        if self.mode == "reject":
            return ProviderStatus("REJECTED")
        if key not in self.executed:  # same key again returns the original payment
            ref = f"UTR{len(self.executed) + 1:06d}"
            self.executed[key] = {
                "ref": ref, "state": "ACCEPTED", "amount_paise": amount_paise, "account": account,
            }
        if self.mode == "timeout_after":
            raise ProviderTimeout("no response within 8s")
        if self.mode == "crash_after":
            raise SimulatedCrash()
        return ProviderStatus(self.executed[key]["state"], self.executed[key]["ref"])

    def lookup(self, key: str) -> ProviderStatus:
        if self.lookup_down:
            raise ProviderUnavailable("status API down")
        if key not in self.executed:
            return ProviderStatus("NOT_FOUND")
        return ProviderStatus(self.executed[key]["state"], self.executed[key]["ref"])


class FakeDownstream:
    """Company ledger, settlement system and bank statement feed."""

    def __init__(self) -> None:
        self.ledger = "PENDING"
        self.settlement_up = False
        self.debits: dict[str, int] = {}  # provider ref -> amount debited from our account

    def ledger_status(self, invoice_id: str) -> str:
        return self.ledger

    def settlement_status(self, ref: str) -> str:
        if not self.settlement_up:
            raise ConnectionError("settlement system unavailable")
        return "SETTLED" if ref in self.debits else "PENDING"

    def bank_debit(self, ref: str) -> int | None:
        return self.debits.get(ref)
