"""SIMULATED MES: an in-memory stand-in for a manufacturing execution system.

It is the system of record for work orders. It knows nothing about verdicts or
receipts. Release calls are logged so tests can prove that a refused action
never reached it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from .models import FACT_FIELDS, WorkOrderRecord


class MESError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkOrderReader:
    """Read-only view of the MES, given to the determiner so it can look up facts but not release anything."""

    def __init__(self, mes: "SimulatedMES") -> None:
        self._mes = mes

    def lookup(self, work_order_id: str) -> WorkOrderRecord | None:
        return self._mes.lookup(work_order_id)


class SimulatedMES:
    def __init__(self, work_orders: Iterable[WorkOrderRecord]) -> None:
        self._orders: dict[str, WorkOrderRecord] = {wo.work_order_id: wo for wo in work_orders}
        self.call_log: list[dict[str, Any]] = []

    @classmethod
    def from_fixture(cls, data: Mapping[str, Any]) -> "SimulatedMES":
        return cls(WorkOrderRecord.from_dict(w) for w in data["work_orders"])

    # -- reads -----------------------------------------------------------------

    def lookup(self, work_order_id: str) -> WorkOrderRecord | None:
        """Records are frozen, so handing one out cannot change the MES."""
        return self._orders.get(work_order_id)

    def reader(self) -> WorkOrderReader:
        return WorkOrderReader(self)

    def status(self, work_order_id: str) -> str:
        return self._orders[work_order_id].status

    def snapshot(self) -> dict[str, WorkOrderRecord]:
        return dict(self._orders)

    # -- simulated changes made outside the demo's flow ------------------------

    def unlock(self, work_order_id: str) -> None:
        self._orders[work_order_id] = replace(self._orders[work_order_id], locked=False)

    def amend(self, work_order_id: str, **changes: Any) -> None:
        """Simulates an engineering or planning change to a work order's facts."""
        allowed = set(FACT_FIELDS) | {"planned_quantity"}
        if not set(changes) <= allowed:
            raise ValueError(f"only {sorted(allowed)} can be amended")
        self._orders[work_order_id] = replace(self._orders[work_order_id], **changes)

    # -- the one write the executor uses ---------------------------------------

    def release(self, work_order_id: str, quantity: int | None = None) -> None:
        self.call_log.append({"call": "release", "work_order_id": work_order_id, "quantity": quantity})
        wo = self._orders.get(work_order_id)
        if wo is None:
            raise MESError("WORK_ORDER_NOT_FOUND", f"{work_order_id} does not exist")
        if wo.locked:
            raise MESError("WORK_ORDER_LOCKED", f"{work_order_id} is locked by another MES session")
        if wo.status != "PLANNED":
            raise MESError("INVALID_STATUS", f"{work_order_id} is {wo.status}, expected PLANNED")
        self._orders[work_order_id] = replace(wo, status="RELEASED")
