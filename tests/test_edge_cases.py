"""Edge cases found in a code review of the first version, one test per fix."""

from dataclasses import replace
from datetime import timedelta

import pytest

from payguard.decision import Observation, inr
from payguard.scenario import AMOUNT, REQUEST, brief_evidence, resolved_evidence
from payguard.service import ProviderStatus
from payguard.store import State

KEY = "pay-INV-8842"


def state(svc, invoice_id="INV-8842"):
    return svc.store.get_by_invoice(invoice_id).state


def reason_codes(svc, invoice_id="INV-8842") -> set[str]:
    p = svc.store.get_by_invoice(invoice_id)
    decision = next(e for e in reversed(svc.store.history(p.id)) if "reasons" in e["detail"])
    return {r["code"] for r in decision["detail"]["reasons"]}


def evidence_with(**changes):
    def build(now):
        evidence = resolved_evidence(now)
        evidence.update({name: make(now) for name, make in changes.items()})
        return evidence
    return build


def settled(world):
    svc = world.start()
    p = svc.handle(REQUEST)
    world.downstream.debits[p.provider_ref] = AMOUNT
    svc.run_pending()
    assert state(svc) == State.SETTLED
    return svc, p


# --- Settlement: the bank decides, and closed payments are watched -------------

def test_bank_debit_outranks_a_provider_failure(world):
    svc = world.start()
    p = svc.handle(REQUEST)
    world.provider.executed[KEY]["state"] = "FAILED"
    world.downstream.debits[p.provider_ref] = AMOUNT  # ...but the money left our account

    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN  # not RETURNED, which would invite a re-payment


def test_failure_at_send_time_isnt_shown_as_settlement_pending(world):
    world.provider.mode = "failed"
    svc = world.start()

    assert svc.handle(REQUEST).state == State.RETURNED  # provider FAILED, and no debit at the bank
    assert any(e["detail"].get("provider") == "FAILED" for e in svc.status("INV-8842")["history"])


def test_failure_answer_counts_even_if_the_providers_lookup_hasnt_caught_up(world):
    world.provider.mode = "failed"
    world.provider.lookup = lambda key: ProviderStatus("NOT_FOUND")

    assert world.start().handle(REQUEST).state == State.RETURNED


def test_return_credit_after_settlement_is_noticed(world):
    svc, p = settled(world)
    world.provider.executed[KEY]["state"] = "RETURNED"
    world.downstream.returns[p.provider_ref] = AMOUNT
    world.clock.advance(hours=3)

    svc.run_pending()

    assert state(svc) == State.RETURNED


def test_provider_reporting_a_return_after_settlement_goes_to_a_person(world):
    svc, _ = settled(world)
    world.provider.executed[KEY]["state"] = "RETURNED"  # but nothing has come back to us yet
    world.clock.advance(hours=3)

    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN


def test_bank_debit_for_the_wrong_amount_goes_to_a_person(world):
    svc = world.start()
    p = svc.handle(REQUEST)
    world.downstream.debits[p.provider_ref] = AMOUNT + 100

    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN


def test_needs_human_payments_keep_their_facts_current_but_keep_their_state(world):
    world.provider.mode = "timeout_after"
    world.provider.lookup_down = True
    svc = world.start()
    svc.handle(REQUEST)
    world.clock.advance(minutes=31)
    svc.run_pending()
    assert state(svc) == State.NEEDS_HUMAN

    world.provider.lookup_down = False
    world.downstream.debits["UTR000001"] = AMOUNT
    svc.run_pending()

    p = svc.store.get_by_invoice("INV-8842")
    assert p.state == State.NEEDS_HUMAN  # the person decides, not the reconciler
    assert p.provider_ref == "UTR000001"
    assert p.view["bank_debit"] == AMOUNT


# --- Recovery ------------------------------------------------------------------

def test_one_odd_provider_answer_doesnt_stop_recovery_for_other_payments(world):
    world.provider.mode = "timeout_after"
    svc = world.start()
    svc.handle(REQUEST)
    svc.handle(replace(REQUEST, invoice_id="INV-9999"))
    real_lookup = world.provider.lookup
    world.provider.lookup = lambda key: ProviderStatus("PENDING") if key == KEY else real_lookup(key)

    svc.run_pending()
    assert state(svc, "INV-9999") == State.PROVIDER_ACCEPTED
    assert state(svc) == State.OUTCOME_UNKNOWN

    world.clock.advance(minutes=31)
    svc.run_pending()
    assert state(svc) == State.NEEDS_HUMAN


def test_an_error_on_one_payment_is_reported_and_the_rest_carry_on(world):
    world.provider.mode = "timeout_before"
    svc = world.start()
    svc.handle(REQUEST)
    svc.handle(replace(REQUEST, invoice_id="INV-9999"))
    world.provider.mode = "ok"

    def evidence(req):
        if req.invoice_id == "INV-8842":
            raise ConnectionError("vendor master down")
        return resolved_evidence(world.clock())
    svc.evidence = evidence

    errors = svc.run_pending()

    assert [invoice for invoice, _ in errors] == ["INV-8842"]
    assert state(svc, "INV-9999") == State.PROVIDER_ACCEPTED


REVOKED = {"limit_paise": 10_00_000_00, "delegation_valid": False}
NEW_ACCOUNT = {"verified_account": "XXXXX9999", "pending_bank_change": False}


@pytest.mark.parametrize("changed", [
    {"authority": lambda now: Observation(True, now, REVOKED)},
    {"vendor_master": lambda now: Observation(True, now, NEW_ACCOUNT)},
], ids=["delegation revoked", "verified account changed"])
def test_resend_checks_the_evidence_again(world, changed):
    world.provider.mode = "timeout_before"
    svc = world.start()
    svc.handle(REQUEST)
    world.provider.mode = "ok"
    world.evidence.build = evidence_with(**changed)
    world.clock.advance(minutes=20)

    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN
    assert world.provider.pay_calls == [KEY]  # not resent


def test_provider_refusal_is_final(world):
    world.provider.mode = "reject"
    svc = world.start()

    assert svc.handle(REQUEST).state == State.PROVIDER_REJECTED
    assert svc.handle(REQUEST).state == State.PROVIDER_REJECTED
    assert world.provider.pay_calls == [KEY]


# --- Deciding --------------------------------------------------------------------

def test_a_corrected_request_replaces_a_wrong_one_before_anything_is_sent(world):
    svc = world.start()
    assert svc.handle(replace(REQUEST, amount_paise=AMOUNT + 500_00)).state == State.REJECTED

    p = svc.handle(REQUEST)

    assert p.state == State.PROVIDER_ACCEPTED
    assert p.amount_paise == AMOUNT
    assert world.provider.executed[KEY]["amount_paise"] == AMOUNT


def test_evidence_age_is_measured_after_gathering_it(world):
    def three_minutes_to_gather(req):
        evidence = resolved_evidence(world.clock())
        world.clock.advance(minutes=3)
        return evidence
    svc = world.start()
    svc.evidence = three_minutes_to_gather

    assert svc.handle(REQUEST).state == State.HELD  # ledger is 3m30s old by decision time
    assert "STALE" in reason_codes(svc)


def test_evidence_dated_in_the_future_is_not_fresh(world):
    world.evidence.build = evidence_with(
        ledger=lambda now: Observation(True, now + timedelta(hours=1), {"available_paise": AMOUNT * 2}),
    )
    svc = world.start()

    assert svc.handle(REQUEST).state == State.HELD
    assert "FUTURE_TIMESTAMP" in reason_codes(svc)


def test_held_payments_reach_the_review_queue(world):
    svc = world.start()
    world.evidence.build = brief_evidence  # bank change pending: a person now
    svc.handle(REQUEST)
    world.evidence.build = evidence_with(  # compliance down: a person after REVIEW_AFTER
        compliance=lambda now: Observation(False, now - timedelta(days=1), {"result": "CLEAR"}),
    )
    svc.handle(replace(REQUEST, invoice_id="INV-9999", agent_claims={}))

    assert [item["invoice_id"] for item in svc.review_queue()] == ["INV-8842"]
    world.clock.advance(hours=5)
    assert [item["invoice_id"] for item in svc.review_queue()] == ["INV-8842", "INV-9999"]


# --- Small things ----------------------------------------------------------------

def test_status_of_an_unknown_invoice_is_none(world):
    assert world.start().status("INV-0000") is None


def test_amounts_show_paise():
    assert inr(AMOUNT) == "₹8,40,000"
    assert inr(AMOUNT + 50) == "₹8,40,000.50"
