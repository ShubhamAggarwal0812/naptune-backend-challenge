# Architecture (Part 4)

## Components

```mermaid
flowchart TD
    AGENT["AI agent<br/>PAY INV-8842 ₹8,40,000"] -->|"F1: same request twice"| INTAKE["Intake<br/>one row per invoice"]

    subgraph EVIDENCE["Evidence: systems of record"]
        PROC["Procurement"]
        VENDOR["Vendor master"]
        DUP["Duplicate check"]
        BAL["Ledger balance"]
        COMP["Compliance"]
    end

    INTAKE --> DECIDE["Decision engine<br/>owner + max age per fact"]
    AUTH["Authority store<br/>limits, delegation"] --> DECIDE
    EVIDENCE -->|"F2: stale, down or contradicting"| DECIDE

    DECIDE -->|"HOLD / REJECT"| HUMAN["Human review queue"]
    DECIDE -->|"PROCEED"| EXEC["Executor"]
    EXEC -->|"1: write-ahead SUBMITTING + key<br/>F7: DB down"| DB[("Payments DB<br/>state + audit log")]
    EXEC -->|"2: pay with idempotency key<br/>F3: timeout, F4: crash"| PSP["Payment provider"]

    DB --> RECOVER["Recovery worker<br/>run_pending"]
    RECOVER -->|"lookup by key"| PSP
    DB --> RECON["Reconciler"]
    RECON -->|"F5: accepted, not settled"| PSP

    subgraph PROOF["Did money move?"]
        BANK["Bank statement feed"]
        POSTINGS["Ledger postings"]
        SETTLE["Settlement system"]
    end
    RECON --> PROOF
    RECON -->|"F6: never resolves"| HUMAN
```

In the prototype: `decision.py` is the decision engine, `service.py` holds the intake, executor, recovery worker and reconciler, and `store.py` is the payments DB with its audit log. The external systems are fakes in `fakes.py`.

## Where it can fail

| # | Where | What goes wrong | What the system does |
|---|---|---|---|
| F1 | Intake | The same request arrives twice, maybe at the same moment, or a malformed one arrives | Requests are validated first. `invoice_id` is UNIQUE, so the existing row is returned. A compare-and-set on `version` lets only one worker send; a losing copy with a different amount gets a conflict. |
| F2 | Evidence | A source is stale, future-dated, down or erroring, the supplier has no verified account, or the agent's claims contradict it | Hold. Such data never satisfies a check, and agent claims never count as evidence. A bank change or a contradiction puts it in the review queue straight away; any other hold joins after 4 hours. |
| F3 | Provider call | Timeout after the provider executed | `OUTCOME_UNKNOWN`, then a lookup by key. A resend only ever uses the same key, and only after the evidence is checked again. |
| F4 | Executor process | Crash between the write-ahead and the response | The row stays `SUBMITTING`. Recovery treats it as unknown and looks it up. |
| F5 | Downstream | The provider says ACCEPTED or FAILED, but the bank hasn't confirmed | `PROVIDER_ACCEPTED` ("settlement pending") or `PROVIDER_FAILED`. It's reconciled until the bank shows a debit, a return, or no debit for 6 hours. Where the bank and the provider disagree, the bank wins, or a person decides. |
| F6 | Reconciler | The disagreement never resolves, or the money comes back after settling | `NEEDS_HUMAN` after a deadline. No automatic re-pay, and no automatic "complete". Settled and returned payments are watched for 7 days. |
| F7 | Payments DB | It's down, or the write fails | No write-ahead means no provider call. The system fails closed. |
| F8 | Between decision and payment | The bank details change after the check | The payment goes to the account captured at decision time, deciding and sending happen in one step, and a resend checks the evidence again. A small window remains (see the final questions). |
| F9 | Recovery worker | One payment raises (an odd provider answer, a source down) | Each payment is handled on its own. The error is reported, the rest carry on, and the deadline still hands the stuck one to a person. |

## Payment states

```mermaid
stateDiagram-v2
    [*] --> NEW: request
    NEW --> HELD: need more evidence
    NEW --> REJECTED: evidence says no
    NEW --> SUBMITTING: proceed, committed before the call
    HELD --> SUBMITTING: re-sent, evidence now OK
    HELD --> HELD: re-sent, still missing
    HELD --> REJECTED: re-sent, evidence says no
    REJECTED --> SUBMITTING: re-sent, evidence now OK
    REJECTED --> HELD: re-sent, evidence missing
    SUBMITTING --> PROVIDER_ACCEPTED: provider accepted
    SUBMITTING --> PROVIDER_REJECTED: provider refused
    SUBMITTING --> PROVIDER_FAILED: provider says it failed
    SUBMITTING --> OUTCOME_UNKNOWN: timeout or crash
    OUTCOME_UNKNOWN --> PROVIDER_ACCEPTED: lookup found it
    OUTCOME_UNKNOWN --> PROVIDER_REJECTED: lookup says refused
    OUTCOME_UNKNOWN --> PROVIDER_FAILED: lookup says it failed
    OUTCOME_UNKNOWN --> SUBMITTING: not found, evidence still OK, resend same key
    OUTCOME_UNKNOWN --> NEEDS_HUMAN: past deadline, or evidence changed
    PROVIDER_ACCEPTED --> SETTLED: bank debit matches
    PROVIDER_ACCEPTED --> RETURNED: money came back
    PROVIDER_ACCEPTED --> PROVIDER_FAILED: provider now says it failed, no debit
    PROVIDER_FAILED --> RETURNED: no debit in 6h, or money came back
    PROVIDER_FAILED --> NEEDS_HUMAN: a debit appears, or deadline
    PROVIDER_ACCEPTED --> NEEDS_HUMAN: deadline, wrong amount, or bank and provider disagree
    SETTLED --> RETURNED: money came back within 7 days
    SETTLED --> NEEDS_HUMAN: provider reports a return, nothing back yet
    RETURNED --> NEEDS_HUMAN: a debit shows up late
```

**The rule:** anything before `SUBMITTING` can be assessed again, and a newer request for the invoice replaces the old one, because nothing has been sent. From `SUBMITTING` onward, the payment is tied to its idempotency key and its amount, and only the recovery worker and the reconciler move it. Once a payment is `NEEDS_HUMAN`, the reconciler keeps its facts current but leaves the state to the person.

## Running this on Temporal

This is how I'd build it for real, since it's what I use at work. I kept the prototype to plain Python and SQLite so the failure handling is visible and runs without a server.

- **One workflow per invoice**, with workflow ID `payment-INV-8842`. Temporal refuses a second start with the same ID, which handles Case A.
- **Activities, timers and signals.** Assessment, pay, lookup and the reconciliation reads are activities. The settlement deadline is a durable timer. Signals carry webhooks and a person's decision on a hold.
- **What Temporal doesn't fix.** It makes *my* code durable, not the provider call. An activity that timed out may still have executed at the provider, and Temporal will retry it. So the pay activity still needs the idempotency key and the same "look up first, then resend with the same key" logic as `_resolve_unknown`. Definitive rejections are marked non-retryable in the activity's retry policy.
