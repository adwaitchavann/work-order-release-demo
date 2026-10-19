"""SIMULATED system of record for evidence (stands in for the approval and quality systems).

The determiner judges the evidence it is given. The executor uses this registry
to confirm that evidence is still what's on record at release time, by comparing
digests. If an approval is revoked after ADMIT, the receipt stops working.
"""

from __future__ import annotations

from typing import Iterable

from .models import EvidenceBundle


class EvidenceRegistry:
    def __init__(self) -> None:
        self._by_work_order: dict[str, EvidenceBundle] = {}

    @classmethod
    def from_cases(cls, cases: Iterable) -> "EvidenceRegistry":
        registry = cls()
        for case in cases:
            registry.record(case.action.work_order_id, case.evidence)
        return registry

    def record(self, work_order_id: str, evidence: EvidenceBundle) -> None:
        self._by_work_order[work_order_id] = evidence

    def current(self, work_order_id: str) -> EvidenceBundle | None:
        return self._by_work_order.get(work_order_id)
