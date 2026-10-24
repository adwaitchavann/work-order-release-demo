"""Wires the demo together. This is the orchestrator's view; the executor only ever sees a verifier."""

from __future__ import annotations

from dataclasses import dataclass

from .approval_registry import ApprovalRegistry
from .authority import AuthoritativeSources
from .clock import Clock, SystemClock
from .demo_keys import DEMO_KEY_ID, DEMO_SECRET
from .determiner import MockDeterminer
from .executor import ReleaseExecutor
from .fixtures import load_approvals, load_policy, load_work_orders
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
    mes: SimulatedMES               # authoritative: work order facts, status, readiness
    approvals: ApprovalRegistry     # authoritative: approvals
    sources: AuthoritativeSources   # read-only view over both, shared by determiner and executor
    determiner: MockDeterminer
    executor: ReleaseExecutor

    @classmethod
    def build(cls, clock: Clock | None = None) -> "DemoEnvironment":
        clock = clock or SystemClock()
        policy = load_policy()
        signer = ReceiptSigner(DEMO_KEY_ID, DEMO_SECRET)
        verifier = ReceiptVerifier({DEMO_KEY_ID: DEMO_SECRET})
        mes = SimulatedMES.from_fixture(load_work_orders())
        approvals = ApprovalRegistry.from_fixture(load_approvals())
        sources = AuthoritativeSources(mes.reader(), approvals.reader())
        return cls(
            clock=clock,
            policy=policy,
            signer=signer,
            verifier=verifier,
            mes=mes,
            approvals=approvals,
            sources=sources,
            determiner=MockDeterminer(policy, signer, clock, sources=sources),
            executor=ReleaseExecutor(mes, verifier, clock, active_policy=policy.ref(), evidence_source=sources),
        )
