"""Persistent state: one row per invoice, and an append-only audit log.

SQLite stands in for Postgres. What carries over: invoice_id is UNIQUE, and
every state change is a compare-and-set on `version`, written in the same
transaction as its audit event.

Each worker or thread opens its own Store. SQLite connections refuse to be
shared across threads, and that check is kept on purpose.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class State(StrEnum):
    NEW = "NEW"
    HELD = "HELD"                            # need more evidence; nothing sent
    REJECTED = "REJECTED"                    # evidence says no; nothing sent
    SUBMITTING = "SUBMITTING"                # committed before the provider call
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"      # call made, no answer: may or may not have executed
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"  # provider took it; the bank hasn't confirmed
    PROVIDER_FAILED = "PROVIDER_FAILED"      # provider says it failed; the bank hasn't confirmed that
    PROVIDER_REJECTED = "PROVIDER_REJECTED"  # provider definitively didn't execute it
    SETTLED = "SETTLED"                      # bank debit seen for the exact amount
    RETURNED = "RETURNED"                    # failed or reversed after the provider accepted it
    NEEDS_HUMAN = "NEEDS_HUMAN"              # couldn't be resolved automatically


# Nothing has been sent yet, so a re-sent request may be assessed again.
# From SUBMITTING on, the payment is tied to its idempotency key.
PRE_SUBMIT = frozenset({State.NEW, State.HELD, State.REJECTED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY,
    version         INTEGER NOT NULL DEFAULT 0,
    invoice_id      TEXT NOT NULL UNIQUE,   -- one payment per invoice (procurement's own ids)
    idempotency_key TEXT NOT NULL UNIQUE,   -- sent to the provider on every attempt
    supplier_id     TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,
    agent_id        TEXT NOT NULL,
    payee_account   TEXT,                   -- from the vendor master at decision time
    state           TEXT NOT NULL,
    provider_ref    TEXT,
    debited_paise   INTEGER,                -- bank debit once seen; it stays seen after the feed rolls over
    submitted_at    TEXT,
    accepted_at     TEXT,
    view            TEXT,                   -- latest per-source observations (Part 3)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL           -- time of the last state change
);
CREATE INDEX IF NOT EXISTS payments_by_state ON payments (state, updated_at);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    payment_id  INTEGER NOT NULL REFERENCES payments(id),
    at          TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_payment ON events (payment_id);
"""


class DuplicateInvoice(Exception):
    pass


@dataclass(frozen=True)
class Payment:
    id: int
    version: int
    invoice_id: str
    idempotency_key: str
    supplier_id: str
    amount_paise: int
    agent_id: str
    payee_account: str | None
    state: State
    provider_ref: str | None
    debited_paise: int | None
    submitted_at: datetime | None
    accepted_at: datetime | None
    updated_at: datetime
    view: dict[str, Any] | None
    created_at: datetime


class Store:
    _SETTABLE = frozenset({
        "supplier_id", "amount_paise", "agent_id",  # a newer request, before anything was sent
        "payee_account", "provider_ref", "debited_paise", "submitted_at", "accepted_at",
    })

    def __init__(self, path: Path | str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def create(
        self,
        invoice_id: str,
        supplier_id: str,
        amount_paise: int,
        agent_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> Payment:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO payments (invoice_id, idempotency_key, supplier_id, amount_paise,"
                    " agent_id, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (invoice_id, idempotency_key, supplier_id, amount_paise, agent_id,
                     State.NEW, now.isoformat(), now.isoformat()),
                )
                detail = {"supplier_id": supplier_id, "amount_paise": amount_paise}
                self._event(cur.lastrowid, None, State.NEW, detail, now)
        except sqlite3.IntegrityError:
            if self.get_by_invoice(invoice_id) is None:
                raise  # not a duplicate: some other constraint failed
            raise DuplicateInvoice(invoice_id) from None
        return self.get(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()

    def get(self, payment_id: int) -> Payment:
        row = self.conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        return _payment(row)

    def get_by_invoice(self, invoice_id: str) -> Payment | None:
        row = self.conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        return _payment(row) if row else None

    def in_state(self, state: State, changed_since: datetime | None = None) -> list[Payment]:
        """Payments in `state`, optionally only those whose state changed at or after `changed_since`."""
        since = changed_since.isoformat() if changed_since else ""  # timestamps are all UTC ISO strings
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE state = ? AND updated_at >= ? ORDER BY id", (state, since)
        ).fetchall()
        return [_payment(r) for r in rows]

    def transition(
        self, p: Payment, to: State, detail: Mapping[str, Any], now: datetime, **fields: Any
    ) -> Payment | None:
        """Move p to `to` if nobody has changed it since we read it. None if someone has."""
        if unknown := set(fields) - self._SETTABLE:
            raise ValueError(f"not settable: {sorted(unknown)}")
        sets = "".join(f", {name} = ?" for name in fields)
        values = [v.isoformat() if isinstance(v, datetime) else v for v in fields.values()]
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE payments SET state = ?, version = version + 1, updated_at = ?{sets}"
                " WHERE id = ? AND version = ?",
                (to, now.isoformat(), *values, p.id, p.version),
            )
            if cur.rowcount != 1:
                return None
            self._event(p.id, p.state, to, detail, now)
        return self.get(p.id)

    def save_view(self, p: Payment, view: Mapping[str, Any]) -> None:
        """Latest per-source observations. Not a state change, so no version bump."""
        with self.conn:
            self.conn.execute(
                "UPDATE payments SET view = ? WHERE id = ?", (json.dumps(view, default=str), p.id)
            )

    def history(self, payment_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT at, from_state, to_state, detail FROM events WHERE payment_id = ? ORDER BY id",
            (payment_id,),
        ).fetchall()
        return [
            {"at": r["at"], "from_state": r["from_state"], "to_state": r["to_state"],
             "detail": json.loads(r["detail"])}
            for r in rows
        ]

    def _event(
        self,
        payment_id: int,
        from_state: State | None,
        to_state: State,
        detail: Mapping[str, Any],
        now: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (payment_id, at, from_state, to_state, detail) VALUES (?, ?, ?, ?, ?)",
            (payment_id, now.isoformat(), from_state, to_state,
             json.dumps(detail, default=str, ensure_ascii=False)),
        )


def _payment(row: sqlite3.Row) -> Payment:
    def when(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    return Payment(
        id=row["id"],
        version=row["version"],
        invoice_id=row["invoice_id"],
        idempotency_key=row["idempotency_key"],
        supplier_id=row["supplier_id"],
        amount_paise=row["amount_paise"],
        agent_id=row["agent_id"],
        payee_account=row["payee_account"],
        state=State(row["state"]),
        provider_ref=row["provider_ref"],
        debited_paise=row["debited_paise"],
        submitted_at=when(row["submitted_at"]),
        accepted_at=when(row["accepted_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        view=json.loads(row["view"]) if row["view"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )
