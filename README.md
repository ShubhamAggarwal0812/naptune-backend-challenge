# Agent payment safety: Naptune backend challenge

An AI agent asks to pay **₹8,40,000 to Supplier ABC** for invoice INV-8842. This repo answers three questions from the challenge:

- Should the payment go ahead?
- How does it get paid at most once when the network and the process fail?
- How do we find out whether money actually moved?

**For the scenario as given: don't pay yet.** The decision is `NEED_MORE_EVIDENCE`, for four reasons:

- The supplier has an unapproved bank-account change.
- The duplicate check is 14 minutes behind.
- The only compliance result is a day old, and the compliance service is down.
- The agent's own summary contradicts the vendor master.

Nothing proves the payment is wrong, so it isn't rejected. It waits, and because of the bank change, a person looks at it.

## Where things are

| | |
|---|---|
| [docs/ANSWERS.md](docs/ANSWERS.md) | Parts 1–3 and the two final questions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Part 4: components, failure points, payment states, how it maps to Temporal |
| [payguard/decision.py](payguard/decision.py) | Part 1 in code: the evidence policy and `assess()` |
| [payguard/service.py](payguard/service.py) | Parts 2–3 in code: idempotent intake, write-ahead, crash recovery, reconciliation |
| [payguard/store.py](payguard/store.py) | SQLite state with compare-and-set updates, plus an append-only audit log |
| [payguard/fakes.py](payguard/fakes.py) | Fake provider, bank and clock, with failures you can switch on |
| [tests/test_cases.py](tests/test_cases.py) | Cases A–E from the brief |

## Run it

You need Python 3.11+. The only dependency is pytest.

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest
python -m pytest -v        # Cases A–E
python -m payguard.demo    # the brief end to end: hold, timeout + crash + replay, settlement
```

The demo prints:

```
== Part 1: agent asks to pay ₹8,40,000 on INV-8842, systems as in the brief
state: HELD
  BANK_CHANGE_PENDING        supplier asked to change its bank account and the change isn't approved; ...
  STALE                      duplicate_check data is 14m old (max 2m); a payment made in that window would not show
  UNAVAILABLE                compliance is unavailable; last result {'result': 'CLEAR'} is 1d old; ...
  AGENT_CLAIM_CONTRADICTED   agent said bank_details_unchanged=True; the systems of record say False
  AGENT_CLAIM_CONTRADICTED   agent said all_checks_complete=True; the systems of record say False
...
== Audit trail
  12:00:00                  - -> NEW
  12:00:00                NEW -> HELD
  12:00:00               HELD -> SUBMITTING
  12:00:00         SUBMITTING -> OUTCOME_UNKNOWN
  12:01:00    OUTCOME_UNKNOWN -> PROVIDER_ACCEPTED
  14:01:00  PROVIDER_ACCEPTED -> SETTLED
```

## Tests

Each test drives the real service against the fakes and a real SQLite file. A "restart" builds new objects on the same database file, so nothing survives in memory.

| Case | Test | What it shows |
|---|---|---|
| A: same request twice | `test_same_request_twice_pays_once` | The second request returns the first payment, and the provider is called once. |
| | `test_same_invoice_with_a_different_amount_is_refused` | Same invoice with a different amount is refused. It's neither a retry nor a new payment. |
| | `test_two_workers_holding_the_same_row_only_one_can_move_it` | When two workers race, only one wins the compare-and-set and may send. |
| B: executed, response timed out | `test_timeout_then_crash_then_same_request_pays_once` | The exact sequence in the brief: timeout → `OUTCOME_UNKNOWN`, then crash, restart and replay. The replay doesn't resend, recovery finds the payment by key, and it's paid once. |
| | `test_crash_mid_call_is_recovered_from_the_write_ahead_row` | The process dies mid-call. The `SUBMITTING` row written before the call is how recovery knows to check. |
| | `test_request_that_never_reached_the_provider_is_resent_with_the_same_key` | The lookup says NOT_FOUND, so it resends with the same key. |
| | `test_unknown_outcome_goes_to_a_person_when_the_provider_cant_be_asked` | If the lookup keeps failing, it never resends blind and hands over to a person after 30 minutes. |
| C: sources conflict | `test_brief_scenario_is_held_and_nothing_is_sent` | The brief's scenario: held with the four reasons above, and nothing sent. |
| | `test_system_of_record_beats_the_agents_claim` | The agent says the balance is sufficient and the ledger says it isn't. The ledger wins. |
| D: evidence unavailable | `test_cached_compliance_clear_is_not_enough_when_the_service_is_down` | Yesterday's CLEAR doesn't count. When compliance comes back, the re-sent request is paid once. |
| E: provider success, bank not yet | `test_provider_success_is_not_settlement` | ACCEPTED, and even a provider SETTLED, stays "settlement pending" until the bank debit appears. |
| | `test_disagreement_that_never_resolves_goes_to_a_person_and_is_never_repaid` | After the deadline it goes to a person and is never paid again. |

To check the tests catch real mistakes, I broke the code on purpose 11 ways and confirmed at least one test fails each time:

- treating a timeout as a failure
- resending on a replayed request
- resending without a lookup
- using a new key on a resend
- trusting the provider's SETTLED
- ignoring staleness
- using a cached result while a service is down
- ignoring agent contradictions
- ignoring a pending bank change
- dropping the version check
- dropping the settlement deadline

**Tests I'd add against real systems:**

- Kill the process between the write-ahead and the provider's response, against the provider's sandbox. This also checks whether its lookup is consistent straight after a call.
- Replay a day of real bank statements through the reconciler, and check every debit matches exactly one payment.

## Assumptions

- **One payment per invoice.** Partial payments would need a payment number in the key.
- **The provider** supports idempotency keys, remembers them for at least 24 hours, and has a status lookup by key. This is the riskiest assumption. See the final questions in [ANSWERS.md](docs/ANSWERS.md).
- **Freshness limits** are my numbers: procurement and vendor master 15 min, ledger 2 min, duplicate-check lag 2 min, compliance 1 h. In practice they'd be policy, set with finance and compliance.
- **The agent is untrusted input.** It names the invoice, supplier and amount. Where the money goes comes from the vendor master.
- **Over the agent's ₹10L limit means REJECT** for the agent. Routing to the parent for approval would be a separate flow, which isn't modelled.
- **Settlement deadline** is 24 hours, standing in for T+1 business day.
- **SQLite stands in for Postgres.** The same UNIQUE constraint and `UPDATE … WHERE version = ?` work there.

## What I left out

- **Reserving funds in the ledger** at send time. It's described in the answers, not built.
- **Backoff scheduling.** `run_pending()` is meant to be called by a timer or a queue, and I didn't build the scheduler.
- **Webhooks.** `reconcile()` polls, and a webhook would just trigger it sooner.
- **The human review screen,** and re-paying a returned payment.
- **An HTTP API.** `PaymentService.handle()` is the entry point, and an API would map `ConflictingRequest` to a 409.

## AI tools used

- **Claude Code** (Anthropic's coding agent for the terminal, running the Claude Opus 5.5 model). I gave it the brief. It wrote the reasoning in `docs/`, the code, the tests and this README, and set up the repository. I reviewed all of it before submitting.

No other AI tools were used.
