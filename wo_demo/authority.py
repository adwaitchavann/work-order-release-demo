"""Authoritative sources for every input a release determination depends on. SIMULATED, local only.

Who is authoritative for what:

  MES (SimulatedMES, read through WorkOrderReader)
      work order facts: plant, line, part number, revision, planned quantity
      work order status (only a status in the policy's releasable list can be released)
      readiness signals: bom_revision_confirmed, material_available, quality_hold_clear
  Approval system (ApprovalRegistry)
      supervisor and quality engineering approvals, revocations and amendments
  Policy file (fixtures/policy.json)
      the rules: required approvals, required checks, safety-critical parts, releasable statuses
  The proposing caller (the AI agent)
      only what it is asking to do: action type, work order, quantity.
      Anything it says about status, checks or approvals is a claim. Caller claims cannot
      establish admissibility; contradictions may independently cause HALT. A claim never
      supplies a missing input.

The determiner and the executor both read through AuthoritativeSources, so the
executor re-checks exactly what the determiner relied on.

Consistency: a view is built from two sequential reads, the MES first and then
the approval system. It is NOT a consistent cross-system snapshot: the two halves
may describe slightly different moments, and nothing here prevents a change in
one system landing between the two reads. Any change after a source was read
moves that source's version, so the executor's re-read still catches it before
release. What is not guaranteed is that the view as a whole existed at a single
instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import sha256_digest
from .models import Approval, EvidenceBundle, WorkOrderRecord


class WorkOrderLookup(Protocol):
    def lookup(self, work_order_id: str) -> WorkOrderRecord | None: ...


class ApprovalLookup(Protocol):
    def current(self, work_order_id: str) -> tuple[tuple[Approval, ...], int]: ...


@dataclass(frozen=True)
class AuthoritativeView:
    """Everything a determination about a work order depends on, from two sequential reads (MES, then
    approvals). Not a consistent cross-system snapshot; see the module docstring."""

    record: WorkOrderRecord
    evidence: EvidenceBundle  # status and readiness from the MES; approvals from the approval system
    approvals_version: int

    @property
    def work_order_id(self) -> str:
        return self.record.work_order_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "mes_version": self.record.version,
            "approvals_version": self.approvals_version,
            "evidence": self.evidence.to_dict(),
        }

    def digest(self) -> str:
        """Bound into the receipt as evidence_digest. Includes both versions, so any change, even one
        that is later reverted, makes an earlier receipt stale."""
        return sha256_digest(self.to_dict())


class AuthoritativeSources:
    """Read-only access to the systems of record. Exposes no way to change them."""

    def __init__(self, work_orders: WorkOrderLookup, approvals: ApprovalLookup) -> None:
        self._work_orders = work_orders
        self._approvals = approvals

    def current(self, work_order_id: str) -> AuthoritativeView | None:
        """None if the MES has no such work order. Reads the MES, then the approval system (not atomic)."""
        record = self._work_orders.lookup(work_order_id)
        if record is None:
            return None
        approvals, approvals_version = self._approvals.current(work_order_id)
        evidence = EvidenceBundle(approvals=approvals, checks=dict(record.readiness), status=record.status)
        return AuthoritativeView(record=record, evidence=evidence, approvals_version=approvals_version)


def unknown_work_order_digest(work_order_id: str) -> str:
    return sha256_digest({"work_order_id": work_order_id, "on_record": False})
