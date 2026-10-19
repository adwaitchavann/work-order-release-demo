"""Wires the demo together. This is the orchestrator's view; the executor only ever sees a verifier."""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock, SystemClock
from .demo_keys import DEMO_KEY_ID, DEMO_SECRET
from .determiner import MockDeterminer
from .evidence_registry import EvidenceRegistry
from .executor import ReleaseExecutor
from .fixtures import load_cases, load_policy, load_work_orders
from .mes import SimulatedMES
from .models import Policy
from .signing import ReceiptSigner
from .verification import ReceiptVerifier


@dataclass
class DemoEnvironment:
    clock: Clock
    policy: Policy
    signer: ReceiptSigner
    verifier: ReceiptVerifier
    determiner: MockDeterminer
    mes: SimulatedMES
    evidence: EvidenceRegistry
    executor: ReleaseExecutor

    @classmethod
    def build(cls, clock: Clock | None = None) -> "DemoEnvironment":
        clock = clock or SystemClock()
        policy = load_policy()
        signer = ReceiptSigner(DEMO_KEY_ID, DEMO_SECRET)
        verifier = ReceiptVerifier({DEMO_KEY_ID: DEMO_SECRET})
        mes = SimulatedMES.from_fixture(load_work_orders())
        evidence = EvidenceRegistry.from_cases(load_cases())
        return cls(
            clock=clock,
            policy=policy,
            signer=signer,
            verifier=verifier,
            determiner=MockDeterminer(policy, signer, clock, work_orders=mes.reader()),
            mes=mes,
            evidence=evidence,
            executor=ReleaseExecutor(mes, verifier, clock, active_policy=policy.ref(), evidence_source=evidence),
        )
