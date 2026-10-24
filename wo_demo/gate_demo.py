"""Misuse demo: what the executor does when the calling code gets it wrong.

Each step hands the executor something it should refuse (or, for the control
steps, something it should accept) and records the outcome and cause.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from .clock import FixedClock
from .determiner import MockDeterminer
from .env import DemoEnvironment
from .executor import ExecutionResult
from .fixtures import get_case
from .models import Approval, ApprovalScope
from .signing import ReceiptSigner


@dataclass(frozen=True)
class GateStep:
    label: str
    result: ExecutionResult


def run_gate_demo() -> tuple[list[GateStep], DemoEnvironment]:
    clock = FixedClock()
    env = DemoEnvironment.build(clock)
    ok_case = get_case("01_admit_release")
    locked_case = get_case("02_admit_execution_failure")
    halt_case = get_case("03_halt_revoked_approval")
    refer_case = get_case("05_refer_quality_signoff")
    reissue_case = get_case("06_admit_reissued_approval")
    steps: list[GateStep] = []

    def attempt(label, action, receipt):
        steps.append(GateStep(label, env.executor.release(action, receipt)))

    def admit(case):
        return env.determiner.determine(case.action, case.caller_evidence).receipt

    ok_receipt = admit(ok_case)

    # A. The request itself
    attempt("Action type changed to CLOSE_WORK_ORDER",
            replace(ok_case.action, action_type="CLOSE_WORK_ORDER"), ok_receipt)
    attempt("Quantity 401 on a work order planned for 400",
            replace(ok_case.action, quantity=401), ok_receipt)

    # B. Authenticity and freshness
    attempt("No receipt", ok_case.action, None)
    halt = env.determiner.determine(halt_case.action, halt_case.caller_evidence)
    attempt("HALT receipt edited to say ADMIT", halt_case.action, replace(halt.receipt, verdict="ADMIT"))
    now = clock.now()
    forged = ReceiptSigner("rogue-key", b"not-the-demo-key").sign(
        receipt_id="rcpt-forged",
        verdict="ADMIT",
        work_order_id=ok_case.action.work_order_id,
        action_digest=ok_case.action.digest(),
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=env.policy.receipt_ttl_seconds)).isoformat(),
    )
    attempt("ADMIT receipt signed with an unknown key", ok_case.action, forged)
    stale = admit(ok_case)
    clock.advance(env.policy.receipt_ttl_seconds + 1)
    attempt("ADMIT receipt used after it expired", ok_case.action, stale)

    # C. Binding
    fresh = admit(ok_case)
    attempt(f"ADMIT receipt for {ok_case.action.work_order_id} used on {halt_case.action.work_order_id}",
            halt_case.action, fresh)
    attempt("ADMIT receipt used after quantity changed 400 -> 300",
            replace(ok_case.action, quantity=300), fresh)
    old_policy = replace(env.policy, version="SYN-REL-POLICY-v2")
    old_determiner = MockDeterminer(old_policy, env.signer, clock, sources=env.sources)
    attempt("ADMIT receipt issued under an older policy version", ok_case.action,
            old_determiner.determine(ok_case.action, ok_case.caller_evidence).receipt)

    # D. Verdict
    attempt("HALT receipt, unmodified and fresh", halt_case.action, admit(halt_case))

    # E/F. Currency: the systems of record change after ADMIT
    reissued = admit(reissue_case)
    wo6 = reissue_case.action.work_order_id
    on_record, _ = env.approvals.current(wo6)
    env.approvals.record(wo6, tuple(replace(a, status="REVOKED", revoked_reason="line down")
                                    if a.status == "ACTIVE" else a for a in on_record))
    attempt("Approval revoked after ADMIT, before release", reissue_case.action, reissued)

    a = refer_case.action
    qe = Approval(
        approval_id="APR-SYN-6005", approver_role="QUALITY_ENGINEERING", approver_id="SYN-QE-02",
        status="ACTIVE",
        scope=ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity),
    )
    on_record, _ = env.approvals.current(a.work_order_id)
    env.approvals.record(a.work_order_id, on_record + (qe,))
    signed_off = admit(refer_case)
    env.mes.amend(a.work_order_id, part_revision="E")
    attempt("Part revision changed in the MES after ADMIT", a, signed_off)

    # Controls, single use and execution failure
    attempt("Valid ADMIT receipt (control)", ok_case.action, fresh)
    attempt("Same receipt replayed after a successful release", ok_case.action, fresh)
    first = admit(locked_case)
    attempt("Valid ADMIT receipt, MES rejects the release", locked_case.action, first)
    env.mes.unlock(locked_case.action.work_order_id)
    attempt("Same receipt replayed after the failed release", locked_case.action, first)
    attempt("Retry with a new determination (control)", locked_case.action, admit(locked_case))

    return steps, env
