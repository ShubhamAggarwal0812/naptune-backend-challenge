"""Edge cases found in a code review of the first version, one test per fix."""

from dataclasses import replace
from datetime import timedelta

import pytest

from payguard.decision import Observation, inr
from payguard.scenario import AMOUNT, REQUEST, brief_evidence, resolved_evidence
from payguard.service import ConflictingRequest, InvalidRequest, ProviderStatus
from payguard.store import State

from helpers import KEY, evidence_with, reason_codes

def state(svc, invoice_id="INV-8842"):
    return svc.store.get_by_invoice(invoice_id).state


def bank_feed_down(ref):
    raise ConnectionError("bank feed down")


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


def test_failure_at_send_time_is_its_own_state_even_with_the_bank_feed_down(world):
    world.provider.mode = "failed"
    world.downstream.bank_debit = bank_feed_down
    svc = world.start()

    assert svc.handle(REQUEST).state == State.PROVIDER_FAILED  # not "settlement pending"
    svc.run_pending()
    assert state(svc) == State.PROVIDER_FAILED  # can't confirm no debit while the feed is down


def test_failure_becomes_returned_only_once_the_bank_has_had_time(world):
    world.provider.mode = "failed"
    world.provider.lookup = lambda key: ProviderStatus("NOT_FOUND")  # its lookup hasn't caught up
    svc = world.start()
    svc.handle(REQUEST)

    world.clock.advance(hours=1)
    svc.run_pending()
    assert state(svc) == State.PROVIDER_FAILED  # no debit yet, but the feed may lag

    world.clock.advance(hours=6)
    svc.run_pending()
    assert state(svc) == State.RETURNED


def test_a_debit_after_a_failure_answer_goes_to_a_person(world):
    world.provider.mode = "failed"
    svc = world.start()
    p = svc.handle(REQUEST)

    world.downstream.debits[p.provider_ref] = AMOUNT
    world.clock.advance(hours=1)
    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN


def test_a_debit_once_seen_isnt_forgotten_when_the_feed_rolls_over(world):
    svc, p = settled(world)
    world.provider.executed[KEY]["state"] = "RETURNED"
    world.downstream.debits.clear()  # the statement feed's window has moved on
    world.clock.advance(hours=7)

    svc.run_pending()

    assert state(svc) == State.NEEDS_HUMAN  # a return reported, nothing back yet: not RETURNED
    assert svc.store.get(p.id).debited_paise == AMOUNT


def test_a_reference_found_by_lookup_is_kept(world):
    real_pay = world.provider.pay
    world.provider.pay = lambda *args: (real_pay(*args), ProviderStatus("ACCEPTED", None))[1]
    svc = world.start()
    svc.handle(REQUEST)
    world.downstream.debits["UTR000001"] = AMOUNT

    svc.run_pending()

    p = svc.store.get_by_invoice("INV-8842")
    assert p.state == State.SETTLED
    assert p.provider_ref == "UTR000001"


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


def test_a_short_evidence_gap_before_a_resend_waits_instead_of_escalating(world):
    world.provider.mode = "timeout_before"
    svc = world.start()
    svc.handle(REQUEST)
    world.provider.mode = "ok"
    world.evidence.build = evidence_with(  # the ledger is 3 minutes behind for a moment
        ledger=lambda now: Observation(True, now - timedelta(minutes=3), {"available_paise": AMOUNT * 2}),
    )
    world.clock.advance(minutes=1)
    svc.run_pending()
    assert state(svc) == State.OUTCOME_UNKNOWN  # waits; a person isn't needed yet

    world.evidence.build = resolved_evidence
    svc.run_pending()
    assert state(svc) == State.PROVIDER_ACCEPTED
    assert world.provider.pay_calls == [KEY, KEY]


def test_settled_payments_stop_being_rechecked_after_the_watch_window(world):
    svc, p = settled(world)
    world.clock.advance(days=8)
    world.downstream.returns[p.provider_ref] = AMOUNT

    svc.run_pending()

    assert state(svc) == State.SETTLED


def test_provider_refusal_is_final(world):
    world.provider.mode = "reject"
    svc = world.start()

    assert svc.handle(REQUEST).state == State.PROVIDER_REJECTED
    assert svc.handle(REQUEST).state == State.PROVIDER_REJECTED
    assert world.provider.pay_calls == [KEY]


# --- Deciding --------------------------------------------------------------------

def test_supplier_without_a_verified_account_is_held_not_paid(world):
    world.evidence.build = evidence_with(
        vendor_master=lambda now: Observation(True, now, {"verified_account": None, "pending_bank_change": False}),
    )
    svc = world.start()

    assert svc.handle(REQUEST).state == State.HELD
    assert "NO_VERIFIED_ACCOUNT" in reason_codes(svc)
    assert world.provider.pay_calls == []


def test_an_error_while_gathering_evidence_holds_the_payment(world):
    def procurement_down(req):
        raise ConnectionError("procurement down")
    svc = world.start()
    svc.evidence = procurement_down

    assert svc.handle(REQUEST).state == State.HELD  # not an exception that strands a NEW row
    world.clock.advance(hours=5)
    assert [item["invoice_id"] for item in svc.review_queue()] == ["INV-8842"]


@pytest.mark.parametrize("bad", [
    {"supplier_id": None}, {"supplier_id": "  "}, {"amount_paise": 0},
    {"amount_paise": -100}, {"amount_paise": 8_40_000.0}, {"amount_paise": True},
], ids=lambda bad: f"{next(iter(bad))}={next(iter(bad.values()))!r}")
def test_invalid_requests_are_refused(world, bad):
    with pytest.raises(InvalidRequest):
        world.start().handle(replace(REQUEST, **bad))


def test_agent_saying_its_checks_arent_done_isnt_a_contradiction(world):
    p = world.start().handle(replace(REQUEST, agent_claims={"all_checks_complete": False}))

    assert p.state == State.PROVIDER_ACCEPTED


@pytest.mark.parametrize("invoice_amount_the_loser_sees", [AMOUNT, AMOUNT + 100],
                         ids=["loser's decision rejects", "loser's decision proceeds (invoice amended)"])
def test_a_losing_copy_with_a_different_amount_is_refused(world, invoice_amount_the_loser_sees):
    """Both copies decide from the same HELD row. The winner sends its amount; the loser's
    different amount must come back as a conflict, not as the winner's payment."""
    world.evidence.build = brief_evidence
    world.start().handle(REQUEST)
    world.evidence.build = resolved_evidence
    loser, winner = world.start(), world.start()
    raced = []
    invoice = {"status": "APPROVED", "amount_paise": invoice_amount_the_loser_sees, "supplier_id": "ABC"}

    def evidence_while_the_winner_runs(req):
        if not raced:
            raced.append(True)
            winner.handle(REQUEST)
        return evidence_with(procurement=lambda now: Observation(True, now, invoice))(world.clock())

    loser.evidence = evidence_while_the_winner_runs
    with pytest.raises(ConflictingRequest):
        loser.handle(replace(REQUEST, amount_paise=AMOUNT + 100))
    assert world.provider.pay_calls == [KEY]


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


def test_returned_and_rejected_payments_reach_the_review_queue(world):
    world.provider.mode = "reject"
    svc = world.start()
    svc.handle(REQUEST)
    world.provider.mode = "failed"
    svc.handle(replace(REQUEST, invoice_id="INV-7777"))
    world.clock.advance(hours=7)
    svc.run_pending()

    queue = {item["invoice_id"]: item["state"] for item in svc.review_queue()}
    assert queue == {"INV-8842": State.PROVIDER_REJECTED, "INV-7777": State.RETURNED}


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


def test_amounts_show_paise_and_sign():
    assert inr(AMOUNT) == "₹8,40,000"
    assert inr(AMOUNT + 50) == "₹8,40,000.50"
    assert inr(-50) == "-₹0.50"
    assert inr(-AMOUNT) == "-₹8,40,000"


def test_an_age_just_over_a_limit_doesnt_read_as_the_limit(world):
    world.evidence.build = evidence_with(
        duplicate_check=lambda now: Observation(
            True, now - timedelta(minutes=2, seconds=59), {"payments_for_invoice": []}),
    )
    svc = world.start()
    p = svc.handle(REQUEST)

    reasons = svc.store.history(p.id)[-1]["detail"]["reasons"]
    assert "duplicate_check data is 2m59s old (max 2m)" in reasons[0]["detail"]
