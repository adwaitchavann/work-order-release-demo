"""Runs one case through the four stages: proposed action -> evidence -> determination -> effect.

Stage 2 now has two parts that are kept apart on purpose: the authoritative
state (read from the MES and the approval system) and the caller's claims (what
the proposing agent says, which is never authoritative).

The orchestrator deliberately does not filter on the verdict. It hands every
receipt to the executor, so every case exercises the executor's own gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clock import Clock
from .determiner import Determination
from .env import DemoEnvironment
from .executor import ExecutionResult
from .fixtures import Case


@dataclass(frozen=True)
class CaseRun:
    case: Case
    determination: Determination
    effect: ExecutionResult
    status_before: str | None
    mes_calls: int
    authoritative: dict[str, Any] | None   # the view as the determiner read it
    record_facts: dict[str, Any] | None

    def trace(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "title": self.case.title,
            "stages": [
                {"stage": "1_proposed_action", "data": self.case.action.to_dict(),
                 "action_digest": self.case.action.digest()},
                {"stage": "2_evidence",
                 "authoritative": {"sources": {"facts, status, readiness": "MES", "approvals": "approval system"},
                                   "work_order_facts": self.record_facts,
                                   "view": self.authoritative},
                 "caller_claims": {"authoritative": False, "data": self.case.caller_evidence.to_dict()}},
                {"stage": "3_determination", "data": self.determination.to_dict()},
                {"stage": "4_effect", "status_before": self.status_before,
                 "mes_calls": self.mes_calls, "data": self.effect.to_dict()},
            ],
        }


def run_case(case: Case, clock: Clock | None = None) -> CaseRun:
    env = DemoEnvironment.build(clock)
    view = env.sources.current(case.action.work_order_id)
    status_before = view.record.status if view is not None else "UNKNOWN"

    # Stage 3: decide. The determiner reads the authoritative sources; the caller's evidence is only a claim.
    determination = env.determiner.determine(case.action, case.caller_evidence)

    # Stage 4: do. The executor gets only the action and the receipt.
    effect = env.executor.release(case.action, determination.receipt)

    return CaseRun(case, determination, effect, status_before, len(env.mes.call_log),
                   view.to_dict() if view is not None else None,
                   view.record.facts() if view is not None else None)
