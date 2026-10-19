"""Runs one case through the four stages: proposed action -> evidence -> determination -> effect.

The orchestrator deliberately does not filter on the verdict. It hands every
receipt to the executor, so every case exercises the executor's own gate,
which is what keeps the system safe if calling code is wrong.
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
    status_before: str
    mes_calls: int
    record_facts: dict[str, Any] | None

    def trace(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "title": self.case.title,
            "stages": [
                {"stage": "1_proposed_action", "data": self.case.action.to_dict(),
                 "action_digest": self.case.action.digest()},
                {"stage": "2_evidence", "data": self.case.evidence.to_dict(),
                 "evidence_digest": self.case.evidence.digest(),
                 "work_order_record": self.record_facts},
                {"stage": "3_determination", "data": self.determination.to_dict()},
                {"stage": "4_effect", "status_before": self.status_before,
                 "mes_calls": self.mes_calls, "data": self.effect.to_dict()},
            ],
        }


def run_case(case: Case, clock: Clock | None = None) -> CaseRun:
    env = DemoEnvironment.build(clock)
    record = env.mes.lookup(case.action.work_order_id)
    status_before = record.status if record is not None else "UNKNOWN"

    # Stage 3: decide. The determiner can read the MES record but never release.
    determination = env.determiner.determine(case.action, case.evidence)

    # Stage 4: do. The executor gets only the action and the receipt.
    effect = env.executor.release(case.action, determination.receipt)

    return CaseRun(case, determination, effect, status_before, len(env.mes.call_log),
                   record.facts() if record is not None else None)
