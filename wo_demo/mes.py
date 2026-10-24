"""SIMULATED MES: an in-memory stand-in for a manufacturing execution system.

It is the system of record for work orders: their facts, status and
release-readiness signals. It knows nothing about verdicts or receipts.

Every change to a determination input (facts, status, readiness) increases the
work order's version. The session lock is execution state and does not.

Conditional release. A release is accepted only if the work order is still at
the version the caller expects, has no active quality hold, isn't locked by
another session and is still PLANNED. Those checks and the write happen inside
one critical section that every other change also has to take, so nothing can
land between the check and the write. A hold that arrives first makes the
release fail; a hold that arrives after the release committed lands on a
released order.

Two test seams simulate timing that is hard to produce on demand:
  before_release_hook  runs when a release request arrives, before the critical
                       section: stands in for a change that lands after the
                       executor's last check but before the MES write.
  inside_release_hook  runs inside the critical section, after the checks and
                       before the write: used to show that a concurrent change
                       has to wait.
Both are None in normal use.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from .models import FACT_FIELDS, WorkOrderRecord


class MESError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkOrderReader:
    """Read-only view of the MES, so a reader can look up work orders but not release or change them."""

    def __init__(self, mes: "SimulatedMES") -> None:
        self._mes = mes

    def lookup(self, work_order_id: str) -> WorkOrderRecord | None:
        return self._mes.lookup(work_order_id)


class SimulatedMES:
    def __init__(self, work_orders: Iterable[WorkOrderRecord]) -> None:
        self._orders: dict[str, WorkOrderRecord] = {wo.work_order_id: wo for wo in work_orders}
        self._lock = threading.RLock()
        self.call_log: list[dict[str, Any]] = []
        self.history: list[tuple[str, int, str]] = []  # (work order, version after the change, what changed)
        self.before_release_hook: Callable[[str], None] | None = None
        self.inside_release_hook: Callable[[str], None] | None = None

    @classmethod
    def from_fixture(cls, data: Mapping[str, Any]) -> "SimulatedMES":
        return cls(WorkOrderRecord.from_dict(w) for w in data["work_orders"])

    # -- reads -----------------------------------------------------------------

    def lookup(self, work_order_id: str) -> WorkOrderRecord | None:
        """Records are frozen, so handing one out cannot change the MES."""
        with self._lock:
            return self._orders.get(work_order_id)

    def reader(self) -> WorkOrderReader:
        return WorkOrderReader(self)

    def status(self, work_order_id: str) -> str | None:
        with self._lock:
            return self._orders[work_order_id].status

    def snapshot(self) -> dict[str, WorkOrderRecord]:
        with self._lock:
            return dict(self._orders)

    # -- simulated changes made outside the demo's flow ------------------------

    def _change(self, work_order_id: str, what: str, **changes: Any) -> None:
        with self._lock:
            wo = self._orders[work_order_id]
            updated = replace(wo, version=wo.version + 1, **changes)
            self._orders[work_order_id] = updated
            self.history.append((work_order_id, updated.version, what))

    def amend(self, work_order_id: str, **changes: Any) -> None:
        """Simulates an engineering or planning change to a work order's facts."""
        allowed = set(FACT_FIELDS) | {"planned_quantity"}
        if not set(changes) <= allowed:
            raise ValueError(f"only {sorted(allowed)} can be amended")
        self._change(work_order_id, f"amend {changes}", **changes)

    def set_status(self, work_order_id: str, status: str | None) -> None:
        """Simulates a planner putting the work order on hold, closing it, and so on."""
        self._change(work_order_id, f"status {status}", status=status)

    def set_readiness(self, work_order_id: str, readiness: Mapping[str, Any]) -> None:
        """Replaces the work order's readiness signals. A signal left out (or None) is unknown to the MES."""
        self._change(work_order_id, f"readiness {dict(readiness)}", readiness=dict(readiness))

    def update_readiness(self, work_order_id: str, **signals: Any) -> None:
        """Changes some readiness signals, for example quality_hold_clear=False when QA places a hold."""
        with self._lock:
            merged = {**self._orders[work_order_id].readiness, **signals}
            self._change(work_order_id, f"readiness {signals}", readiness=merged)

    def unlock(self, work_order_id: str) -> None:
        """Releases the session lock. Execution state only, so the version does not change."""
        with self._lock:
            self._orders[work_order_id] = replace(self._orders[work_order_id], locked=False)

    # -- the one write the executor uses ---------------------------------------

    def release(self, work_order_id: str, quantity: int | None = None, *,
                expected_version: int | None = None) -> None:
        """Conditional release. Checks and write are atomic with respect to every other change."""
        self.call_log.append({"call": "release", "work_order_id": work_order_id, "quantity": quantity,
                              "expected_version": expected_version})
        if self.before_release_hook is not None:
            self.before_release_hook(work_order_id)

        with self._lock:
            wo = self._orders.get(work_order_id)
            if wo is None:
                raise MESError("WORK_ORDER_NOT_FOUND", f"{work_order_id} does not exist")
            if expected_version is None:
                raise MESError("PRECONDITION_REQUIRED", "a release must name the version it was checked against")
            if wo.version != expected_version:
                raise MESError("STATE_CHANGED_BEFORE_RELEASE",
                               f"{work_order_id} is at version {wo.version}; the release was conditional on "
                               f"version {expected_version}")
            if wo.readiness.get("quality_hold_clear") is not True:
                raise MESError("QUALITY_HOLD_ACTIVE",
                               f"{work_order_id} has an active or unconfirmed quality hold")
            if wo.locked:
                raise MESError("WORK_ORDER_LOCKED", f"{work_order_id} is locked by another MES session")
            if wo.status != "PLANNED":
                raise MESError("INVALID_STATUS", f"{work_order_id} is {wo.status}, expected PLANNED")

            if self.inside_release_hook is not None:
                self.inside_release_hook(work_order_id)

            self._change(work_order_id, "released", status="RELEASED")
