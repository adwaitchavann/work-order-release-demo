"""SIMULATED approval system: the system of record for approvals.

Stands in for whatever tool supervisors and quality engineers use to approve,
revoke and amend releases. Each work order's approvals carry a version that
increases on every change, so a receipt can't survive a revoke-and-restore.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import Approval


class ApprovalReader:
    """Read-only view of the approval system, so a reader can see approvals but not record or revoke them."""

    def __init__(self, registry: "ApprovalRegistry") -> None:
        self._registry = registry

    def current(self, work_order_id: str) -> tuple[tuple[Approval, ...], int]:
        return self._registry.current(work_order_id)


class ApprovalRegistry:
    def __init__(self) -> None:
        self._by_work_order: dict[str, tuple[Approval, ...]] = {}
        self._versions: dict[str, int] = {}

    @classmethod
    def from_fixture(cls, data: Mapping[str, Any]) -> "ApprovalRegistry":
        registry = cls()
        for work_order_id, approvals in data["approvals"].items():
            registry.record(work_order_id, tuple(Approval.from_dict(a) for a in approvals))
        return registry

    def record(self, work_order_id: str, approvals: tuple[Approval, ...]) -> None:
        """Replaces the approvals on record for a work order (approve, revoke, amend)."""
        self._by_work_order[work_order_id] = tuple(approvals)
        self._versions[work_order_id] = self._versions.get(work_order_id, 0) + 1

    def current(self, work_order_id: str) -> tuple[tuple[Approval, ...], int]:
        """The approvals on record and their version. No entry means no approvals (version 0)."""
        return self._by_work_order.get(work_order_id, ()), self._versions.get(work_order_id, 0)

    def reader(self) -> ApprovalReader:
        return ApprovalReader(self)
