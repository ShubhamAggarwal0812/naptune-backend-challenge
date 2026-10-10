"""Cases A–E from the brief. Each test runs the real service against fakes and a real SQLite file."""

from dataclasses import replace
from datetime import timedelta

import pytest

from payguard.decision import Observation
from payguard.fakes import SimulatedCrash
from payguard.scenario import AMOUNT, REQUEST, brief_evidence, resolved_evidence
from payguard.service import ConflictingRequest
from payguard.store import State

from helpers import KEY, evidence_with, reason_codes

# --- Case A: the same request arrives twice ------------------------------------

def test_same_request_twice_pays_once(world):
    svc = world.start()
    first = svc.handle(REQUEST)
    second = svc.handle(REQUEST)

    assert first.state == second.state == State.PROVIDER_ACCEPTED
    assert second.id == first.id
    assert world.provider.pay_calls == [KEY]


def test_same_invoice_with_a_different_amount_is_refused(world):
    svc = world.start()
    svc.handle(REQUEST)

    with pytest.raises(ConflictingRequest):
        svc.handle(replace(REQUEST, amount_paise=AMOUNT + 100))
    assert world.provider.pay_calls == [KEY]


def test_two_copies_racing_send_only_once(world):
    """The second copy runs start to finish while the first is still gathering evidence, so
    both decide from the same row. Only the compare-and-set winner may call the provider."""
    first, second = world.start(), world.start()
    raced = []

    def evidence_while_the_other_copy_runs(req):
        if not raced:
            raced.append(True)
            second.handle(REQUEST)
        return resolved_evidence(world.clock())

    first.evidence = evidence_while_the_other_copy_runs
    first.handle(REQUEST)

    assert world.provider.pay_calls == [KEY]


# --- Case B: the provider executes but the response times out ------------------

def test_timeout_then_crash_then_same_request_pays_once(world):
    """The exact sequence from Part 2 of the brief."""
    world.provider.mode = "timeout_after"
    p = world.start().handle(REQUEST)
    assert p.state == State.OUTCOME_UNKNOWN  # not FAILED: it may have executed...
    assert len(world.provider.executed) == 1  # ...and it did

    svc = world.start()  # crash and restart: nothing in memory survives
    world.provider.mode = "ok"
    assert svc.handle(REQUEST).state == State.OUTCOME_UNKNOWN  # the replayed request doesn't resend
    svc.run_pending()  # recovery asks the provider by key

    assert svc.store.get(p.id).state == State.PROVIDER_ACCEPTED
    assert world.provider.pay_calls == [KEY]
    assert [e["to_state"] for e in svc.store.history(p.id)] == [
        "NEW", "SUBMITTING", "OUTCOME_UNKNOWN", "PROVIDER_ACCEPTED",
    ]


def test_crash_mid_call_is_recovered_from_the_write_ahead_row(world):
    world.provider.mode = "crash_after"
    with pytest.raises(SimulatedCrash):
        world.start().handle(REQUEST)

    svc = world.start()
    assert svc.store.get_by_invoice("INV-8842").state == State.SUBMITTING  # committed before the call
    world.provider.mode = "ok"
    world.clock.advance(minutes=1)
    svc.run_pending()

    assert svc.store.get_by_invoice("INV-8842").state == State.PROVIDER_ACCEPTED
    assert len(world.provider.executed) == 1


def test_request_that_never_reached_the_provider_is_resent_with_the_same_key(world):
    world.provider.mode = "timeout_before"
    svc = world.start()
    assert svc.handle(REQUEST).state == State.OUTCOME_UNKNOWN

    world.provider.mode = "ok"
    svc.run_pending()  # lookup says NOT_FOUND, so resend with the same key

    assert svc.store.get_by_invoice("INV-8842").state == State.PROVIDER_ACCEPTED
    assert world.provider.pay_calls == [KEY, KEY]
    assert len(world.provider.executed) == 1


def test_unknown_outcome_goes_to_a_person_when_the_provider_cant_be_asked(world):
    world.provider.mode = "timeout_after"
    world.provider.lookup_down = True
    svc = world.start()
    svc.handle(REQUEST)

    svc.run_pending()
    assert svc.store.get_by_invoice("INV-8842").state == State.OUTCOME_UNKNOWN
    world.clock.advance(minutes=31)
    svc.run_pending()

    assert svc.store.get_by_invoice("INV-8842").state == State.NEEDS_HUMAN
    assert world.provider.pay_calls == [KEY]  # never resent blind


# --- Case C: source information conflicts --------------------------------------

def test_brief_scenario_is_held_and_nothing_is_sent(world):
    world.evidence.build = brief_evidence
    svc = world.start()
    p = svc.handle(REQUEST)

    assert p.state == State.HELD
    assert reason_codes(svc) == {"BANK_CHANGE_PENDING", "STALE", "UNAVAILABLE", "AGENT_CLAIM_CONTRADICTED"}
    assert world.provider.pay_calls == []


def test_system_of_record_beats_the_agents_claim(world):
    # The agent claims sufficient_balance=True; the ledger disagrees.
    world.evidence.build = evidence_with(
        ledger=lambda now: Observation(True, now, {"available_paise": AMOUNT - 100}),
    )
    svc = world.start()
    p = svc.handle(REQUEST)

    assert p.state == State.REJECTED
    assert {"INSUFFICIENT_BALANCE", "AGENT_CLAIM_CONTRADICTED"} <= reason_codes(svc)
    assert world.provider.pay_calls == []


# --- Case D: required evidence is unavailable ----------------------------------

def test_cached_compliance_clear_is_not_enough_when_the_service_is_down(world):
    world.evidence.build = evidence_with(
        compliance=lambda now: Observation(False, now - timedelta(days=1), {"result": "CLEAR"}),
    )
    svc = world.start()
    p = svc.handle(REQUEST)
    assert p.state == State.HELD
    assert "UNAVAILABLE" in reason_codes(svc)
    assert world.provider.pay_calls == []

    # Compliance comes back and the agent re-sends: re-assessed, and paid exactly once.
    world.evidence.build = resolved_evidence
    assert svc.handle(REQUEST).state == State.PROVIDER_ACCEPTED
    assert world.provider.pay_calls == [KEY]


# --- Case E: provider says success, downstream hasn't confirmed ----------------

def test_provider_success_is_not_settlement(world):
    svc = world.start()
    p = svc.handle(REQUEST)
    assert p.state == State.PROVIDER_ACCEPTED

    svc.run_pending()  # ledger PENDING, settlement down, no bank debit
    status = svc.status("INV-8842")
    assert status["state"] == State.PROVIDER_ACCEPTED
    assert status["sources"] == {
        "provider": "ACCEPTED", "ledger": "PENDING", "settlement": "UNAVAILABLE",
        "bank_debit": "NOT_SEEN", "bank_return": "NOT_SEEN",
    }

    world.provider.executed[KEY]["state"] = "SETTLED"  # the provider's word alone still isn't enough
    svc.run_pending()
    assert svc.status("INV-8842")["state"] == State.PROVIDER_ACCEPTED

    world.downstream.debits[p.provider_ref] = AMOUNT  # the bank debit appears
    svc.run_pending()
    assert svc.status("INV-8842")["state"] == State.SETTLED


def test_disagreement_that_never_resolves_goes_to_a_person_and_is_never_repaid(world):
    svc = world.start()
    svc.handle(REQUEST)

    world.clock.advance(days=1, minutes=1)
    svc.run_pending()

    assert svc.status("INV-8842")["state"] == State.NEEDS_HUMAN
    assert world.provider.pay_calls == [KEY]
