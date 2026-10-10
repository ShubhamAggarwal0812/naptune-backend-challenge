from dataclasses import dataclass
from pathlib import Path

import pytest

from payguard.fakes import FakeClock, FakeDownstream, FakeEvidence, FakeProvider
from payguard.scenario import NOW, resolved_evidence
from payguard.service import PaymentService
from payguard.store import Store


@dataclass
class World:
    db: Path
    clock: FakeClock
    evidence: FakeEvidence
    provider: FakeProvider
    downstream: FakeDownstream

    def start(self) -> PaymentService:
        """A fresh process: new objects, same database file. Calling it again is a restart."""
        return PaymentService(Store(self.db), self.evidence, self.provider, self.downstream, self.clock)


@pytest.fixture
def world(tmp_path: Path) -> World:
    # Clean evidence by default; each test switches on the problem it's about.
    clock = FakeClock(NOW)
    evidence = FakeEvidence(clock, resolved_evidence)
    return World(tmp_path / "payments.db", clock, evidence, FakeProvider(), FakeDownstream())
