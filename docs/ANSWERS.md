# Answers: Parts 1–3 and the final questions

An agent wants to pay **₹8,40,000 on INV-8842 to Supplier ABC** and says every check passed. Some of the systems disagree with it.

## Part 1: before execution

### 1. What I trust, and why

I trust a source for a fact only if it's the **system of record for that fact** and its data is **fresh enough for that fact**.

| Source | What it owns | Trust? | Why |
|---|---|---|---|
| Authority config | Agent limit ₹10L, delegation valid | Yes | Our own policy data. ₹8.4L is within the agent's own limit. Checked again at execution, since a delegation can be revoked. |
| Procurement | INV-8842 is APPROVED for ₹8,40,000 | Yes | The system of record for approvals, and 6 minutes old. The request's amount and supplier must match it exactly. |
| Vendor master | ABC's bank account, and that a change is pending | Yes, including the bad news | The system of record for bank details. The pending change is the most important fact in the scenario. |
| Company ledger | Balance is sufficient | Yes, for now | 30 seconds old. "Sufficient now" isn't "reserved", so the funds get reserved when the payment is sent. |

### 2. What I don't treat as authoritative

- **The agent's summary.** The agent is the one asking for the money, so what it says is a claim to check, not evidence. It's also wrong: it says the bank details haven't changed, and the vendor master shows a change requested 2 hours ago.
- **"No previous payment" from the duplicate check.** That system is 14 minutes behind. A payment made in the last 14 minutes wouldn't show, and the invoice itself was updated 6 minutes ago, inside that blind window. An empty answer from a lagging system is an absence of evidence, not evidence of absence.
- **Yesterday's compliance CLEAR.** It's a cached result, and the service that would refresh it is down. Sanctions lists and watchlists change daily, so a day-old pass isn't a pass for a ₹8.4L payment.
- **The parent system's ₹20L limit.** That's the parent's authority, not the agent's. The agent is checked against its own ₹10L. Going above it would need the parent's own approval, not the agent borrowing it.

### 3. Can it proceed now?

**No. Decision: `NEED_MORE_EVIDENCE`**, routed to a person in accounts payable or treasury because of the bank change.

- Three required checks aren't satisfied: bank details (change pending), duplicates (14-minute blind window), and compliance (stale, and the service is down).
- The agent misreported a fact I can check, so I don't rely on its judgement for things I can't check.
- **Not `REJECT`**, because nothing shows the payment is wrong. The invoice is approved, the amount is within authority, and the funds are there. Once the gaps close, it can be paid.

On the bank change: the payment would go to the current verified account (XXXXX4321), so you could argue it's safe. I still hold it, because an open change request means one of two things. Either the supplier is moving banks, and the old account may be closing so the payment could bounce. Or someone is impersonating the supplier, which is the classic invoice-fraud pattern. And if the change gets approved between my decision and the payment, the money could go to an unverified account. Standard accounts-payable practice is to freeze payments to a vendor while its bank details are changing.

The costs aren't symmetric. Holding costs a delay. A wrong ₹8.4L payment to a fraudster usually can't be recovered.

### 4. What exact information would change the decision

To move to **PROCEED**, all of these:

1. **Vendor master:** the bank change is resolved. Either it's withdrawn or rejected, or it's approved after an out-of-band check with the supplier (a call-back to a contact we already had, not one from the change request). The payment then goes to whichever account the vendor master shows as verified at that moment.
2. **Duplicate check:** caught up to within 2 minutes of real time, so it covers the invoice's last update, and still showing no payment for INV-8842. Our own payment store must also have no payment for INV-8842.
3. **Compliance:** a fresh CLEAR for Supplier ABC from the live service. I've assumed it must be under 1 hour old for a payment this size.
4. **At execution:** procurement still shows APPROVED for ₹8,40,000, the ledger still has the balance, and the delegation is still valid.

To move to **REJECT**: the bank change turns out to be fraud, compliance returns a hit, the duplicate check finds a payment, the invoice is no longer approved or its amount differs, or the amount is over the agent's ₹10L.

### 5. Handling contradictory information

- **Each fact has one system of record:** the vendor master owns bank details, procurement owns approval, the ledger owns balance. For its own fact, the system of record wins.
- **The agent's claims rank last.** They never satisfy a check. When one contradicts a system of record, the record wins, the contradiction is logged, and the payment can't go through automatically (`AGENT_CLAIM_CONTRADICTED` in `decision.py`). An agent that's wrong about something I checked may be wrong about something I didn't.
- **If two systems of record disagree about the same thing**, say procurement and the vendor master name different suppliers for the invoice, I don't pick one. No "latest wins" and no majority vote across systems. The payment holds and a person resolves it.
- **Stale or unavailable data** can't satisfy a check, and it isn't treated as a "no" either. It means "unknown", and unknown means hold.
- Every decision is stored with what each source said and when, so a contradiction can be explained later.

### 6. When to stop gathering

The list of checks is fixed per action type and isn't made up at runtime. For a supplier payment it's the six sources above, each with an owner and a maximum age (`MAX_AGE` in `decision.py`).

- **Stop** when every check is satisfied (PROCEED), or when a check fails in a way more data won't fix (REJECT). I still collect all six, so the person reviewing sees every problem at once instead of one per round trip.
- **Don't** query anything outside the list. A source that can't change the decision isn't worth the time.
- **Scale the list with risk.** A bigger amount, a new payee or a recent bank change adds checks, such as a second approver. Adding a check is a policy change that someone reviews, not something the agent improvises.
- **Don't wait forever** on a source that's down. Hold the payment, assess it again when the agent re-sends or the source comes back, and hand it to a person after a deadline.

## Part 2: execution failure

We sent `PAY INV-8842 ₹8,40,000`, waited 8 seconds, timed out, crashed, restarted, and the same request arrived again.

### 7. How I avoid paying twice

There are three layers, and each covers a different failure:

1. **One payment per invoice in our database.** `invoice_id` is UNIQUE. A repeated request finds the existing row and gets its current state back. It never creates a second payment.
2. **Only one worker can send.** Every state change is a compare-and-set on a version number (`UPDATE … WHERE id = ? AND version = ?`). Only the worker whose update succeeds may call the provider.
3. **The provider dedupes too.** Every call for this payment carries the same idempotency key, `pay-INV-8842`. If we resend, the provider returns the original payment instead of making a new one.

What ties them together: **a timeout means the outcome is unknown, never that it failed.** We never create a new key for a payment whose outcome is unknown.

### 8. What I save before calling the provider

One row, committed **before** the network call (write-ahead):

- the invoice id, the amount in paise (integers, never floats), and the supplier;
- the **payee account as read from the vendor master** when the decision was made (the agent never chooses where money goes);
- the idempotency key;
- state `SUBMITTING` and `submitted_at`;
- in the audit log, the decision together with the evidence it was based on.

If that row can't be written, the provider isn't called.

### 9. How retries work

There are two different kinds of retry.

- **The agent re-sends its request.** This is always safe: it gets the current state back. If the payment was on hold, it's assessed again with fresh evidence.
- **We retry the provider call.** This happens only from `OUTCOME_UNKNOWN`, and only after asking the provider first:
  - If the lookup by key says ACCEPTED or REJECTED, I record that and stop.
  - If it says NOT_FOUND, I resend **with the same key**, but only inside a deadline that's well within the provider's dedupe window.
  - If the lookup itself fails, I try again later with backoff (30 s, 1 min, 2 min, capped around 15 min, with jitter). After 30 minutes a person takes over.
- **A definitive rejection** (account closed, invalid IFSC) is never retried.

### 10. What happens after a crash

Memory is gone, and the database says what's in flight. `run_pending()` runs at startup and on a timer:

- It finds rows stuck in `SUBMITTING`. If a row is older than the call timeout, it isn't a live call on another worker. It moves those rows to `OUTCOME_UNKNOWN`, because we can't know whether the call went out.
- It resolves every `OUTCOME_UNKNOWN` row with the lookup described in question 9.
- It reconciles every `PROVIDER_ACCEPTED` row (Part 3).

The replayed request from the brief finds the existing row, sees `OUTCOME_UNKNOWN` and returns it, without calling the provider. Only the recovery path talks to the provider about a payment that was already sent.

### 11. When the database committed but the network response was lost

This can happen in two places:

- **On our call to the provider.** We committed `SUBMITTING`, the provider executed, and its response was lost. The row moves to `OUTCOME_UNKNOWN`, the lookup by key finds the payment, and the row moves to `PROVIDER_ACCEPTED`. This is Case B.
- **On our answer to the agent.** We committed the result, but the agent never heard back. It re-sends, and the unique invoice row returns the stored result.

Either way the money moves once, because the key and the row already exist.

## Part 3: the provider says success

The provider says ACCEPTED, the ledger says PENDING, the settlement system is unavailable, and no bank debit has been seen.

### 12. Is provider success enough?

**No.** ACCEPTED means the provider took the instruction, not that money moved. The beneficiary bank can still reject it (account closed, name mismatch), the payment rail can return it, or the provider can fail it later. Here only one of four signals says success, and it's the one furthest from the money. I count the payment as done when **the bank shows a debit of exactly ₹8,40,000 with the provider's reference (UTR)**.

### 13. What I'd watch next

- **The bank statement feed:** a ₹8,40,000 debit with the provider's reference. This is the strongest signal.
- **The provider's status** moving to SETTLED, FAILED or RETURNED. I'd take it from webhooks and back that up with polling, because webhooks get lost.
- **The settlement system**, once it's back.
- **The ledger** entry moving from PENDING to POSTED.
- **Bad signals too:** a return or reversal showing up in the bank feed.

### 14. What state the system shows while they disagree

`PROVIDER_ACCEPTED`, labelled **"Settlement pending: the provider accepted it, the bank hasn't confirmed. Do not retry or pay again."** It's neither "complete" nor "failed".

Next to it goes a view of each source: provider ACCEPTED, ledger PENDING, settlement UNAVAILABLE, bank NOT_SEEN (see `status()` in `service.py`). While a payment is in this state, the funds stay reserved, the invoice stays locked against manual payment, and nothing can retry it.

### 15. How I eventually find out what happened

A reconciliation job (`reconcile()`) re-reads all four sources by the payment's reference. It runs on a timer and whenever a webhook arrives:

- **Bank debit matches the amount and reference:** `SETTLED`. If the ledger is still PENDING, it catches up from the bank record, because the ledger is our books and the bank is where the money is.
- **Provider says FAILED or RETURNED:** `RETURNED`. The funds are released, and paying again is a human decision.
- **Bank debit for the wrong amount:** `NEEDS_HUMAN`.
- **Provider says SETTLED but there's no bank debit:** still pending. The provider's word doesn't settle it.

### 16. If the disagreement never resolves

After a deadline (T+1 business day for NEFT/RTGS; 24 hours in the code), the payment moves to **`NEEDS_HUMAN`** as a reconciliation exception, with every observation attached.

The funds stay reserved and the invoice stays locked. The system never re-pays and never marks the payment complete on its own. Someone takes the UTR to the provider and the bank. Whoever closes the exception records the evidence and their name in the audit log.

## Final questions

### What is the most dangerous assumption in my design?

That **the provider honours idempotency keys and its status lookup is accurate.** All of Part 2 rests on "same key means same payment" and "NOT_FOUND means it didn't execute". If the lookup is eventually consistent (it says NOT_FOUND for a payment it's still writing), or if the provider forgets keys sooner than its docs say, my resend can pay twice.

What I do about it: I resend only with the same key, and only within a deadline well inside the dedupe window. After that, a person decides. Before trusting this in production, I'd test it against the provider's sandbox (kill the client mid-call, look up straight away, resend) instead of relying on its documentation. If the provider had no idempotency keys at all, I'd never resend automatically, and every unknown outcome would go to a person.

### What failure could still make the system decide wrongly?

- **A compromised system of record.** If an attacker gets the bank change approved in the vendor master, for example by fooling the approver, every check passes and the system pays the attacker. My system checks that sources are fresh and agree with each other. It can't check that they're right. Mitigation: a cooling-off period after any bank change (say 72 hours with a call-back), and a human approves the first payment to a new account.
- **Evidence changing between the check and the payment.** I decide and send in one step, and I pay the account I read during the decision, but a small window remains. Closing it properly needs the vendor master to support a version check or a lock.
- **Timestamps that lie.** "Updated 30 seconds ago" might be when a cache refreshed, not when the data underneath was true. If two "independent" sources read the same stale upstream, they'll agree and both be wrong.
- **A check that's missing from the policy.** The system checks only what's on the list. An invoice that is approved but fraudulent in itself would pass.

### How I'd show the system behaved correctly

- Every decision is stored with its evidence (each source, its value and its as-of time), so it can be re-run and explained.
- Every state change is an append-only audit event, written in the same transaction as the change.
- A daily reconciliation report: every payment we sent matches exactly one bank debit, and every supplier debit at the bank matches one of our payments. Anything unmatched raises an alert.
