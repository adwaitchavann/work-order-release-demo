"""Stage 1 (proposed action) and stage 2 (evidence) data, the MES work order record, and the synthetic policy."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from .canonical import sha256_digest

RELEASE_WORK_ORDER = "RELEASE_WORK_ORDER"

# Facts a proposal may describe but never supplies: they always come from the MES record.
FACT_FIELDS = ("plant", "line", "part_number", "part_revision")


def is_whole_number(value: Any) -> bool:
    """True only for real ints. Booleans, floats and numeric strings don't count."""
    return type(value) is int


def valid_release_quantity(quantity: Any, planned_quantity: Any) -> bool:
    """A release quantity must be a whole number from 1 up to the planned quantity on the MES record."""
    return (
        is_whole_number(quantity)
        and is_whole_number(planned_quantity)
        and 0 < quantity <= planned_quantity
    )


def read_only_mapping(m: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """A read-only view over a private deep copy: edits through it fail, and nothing in it is shared
    with the mapping it was made from."""
    return MappingProxyType(copy.deepcopy(dict(m or {})))


@dataclass(frozen=True)
class ProposedAction:
    """What the AI agent wants to do.

    plant, line, part_number and part_revision are the agent's claims about the work order. Decisions
    use the MES record for those facts; the claims are only checked against it. The executor
    recomputes the digest itself and never trusts a stored one.
    """

    action_id: str
    agent_id: str
    action_type: str
    work_order_id: str
    plant: str
    line: str
    part_number: str
    part_revision: str
    quantity: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        return sha256_digest(self.to_dict())

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ProposedAction":
        return cls(**d)


@dataclass(frozen=True)
class WorkOrderRecord:
    """The MES system of record for one work order.

    Authoritative for the work order's facts (part, revision, line, planned quantity), its
    status, and its release-readiness signals (BOM confirmed, material staged, quality hold
    clear). `version` increases on every change to any of those, so a receipt can tell
    "changed and changed back" apart from "never changed". `locked` is a transient MES
    session lock: execution state, not a determination input, and it does not bump the version.

    Read-only. `frozen` stops fields being reassigned but not a dict inside one being edited, so
    readiness is stored as a read-only mapping over a private deep copy, made every time a record
    is built (including by dataclasses.replace). The MES also hands out a copy on every read, so
    even a forced edit to a returned record can't reach the MES.
    """

    work_order_id: str
    plant: str
    line: str
    part_number: str
    part_revision: str
    planned_quantity: int
    status: str | None  # "PLANNED" is releasable; None means the MES reports no status
    locked: bool = False
    readiness: Mapping[str, Any] = field(default_factory=dict, hash=False)
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness", read_only_mapping(self.readiness))

    def copy(self) -> "WorkOrderRecord":
        """An equal record that shares nothing mutable with this one."""
        return replace(self)

    def facts(self) -> dict[str, Any]:
        """The facts a determination relies on. Status and lock state are execution state, not facts."""
        return {
            "work_order_id": self.work_order_id,
            "plant": self.plant,
            "line": self.line,
            "part_number": self.part_number,
            "part_revision": self.part_revision,
            "planned_quantity": self.planned_quantity,
        }

    def facts_digest(self) -> str:
        return sha256_digest(self.facts())

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "WorkOrderRecord":
        return cls(
            work_order_id=d["work_order_id"],
            plant=d["plant"],
            line=d["line"],
            part_number=d["part_number"],
            part_revision=d["part_revision"],
            planned_quantity=d["planned_quantity"],
            status=d.get("status"),
            locked=bool(d.get("locked", False)),
            readiness=d.get("readiness") or {},
        )


@dataclass(frozen=True)
class ApprovalScope:
    work_order_id: str
    line: str
    part_number: str
    part_revision: str
    max_quantity: int

    def covers(self, record: WorkOrderRecord, quantity: Any) -> bool:
        """Compared against the MES record's facts, never the proposal's."""
        return (
            is_whole_number(self.max_quantity)
            and is_whole_number(quantity)
            and self.work_order_id == record.work_order_id
            and self.line == record.line
            and self.part_number == record.part_number
            and self.part_revision == record.part_revision
            and 0 < quantity <= self.max_quantity
        )


@dataclass(frozen=True)
class Approval:
    approval_id: str
    approver_role: str
    approver_id: str
    status: str  # "ACTIVE" or "REVOKED"; anything else is treated as unrecognized and halts
    scope: ApprovalScope
    revoked_reason: str | None = None
    supersedes: str | None = None  # approval_id this one replaces; the replaced approval is out of force

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Approval":
        d = dict(d)
        d["scope"] = ApprovalScope(**d["scope"])
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceBundle:
    """Approvals, readiness checks and work order status.

    The same shape is used for two very different things: what the authoritative sources report
    (assembled by AuthoritativeSources) and what the proposing caller claims (never authoritative).
    A check passes only if its authoritative value is exactly True.
    """

    approvals: tuple[Approval, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)
    status: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EvidenceBundle":
        return cls(
            approvals=tuple(Approval.from_dict(a) for a in d.get("approvals", [])),
            checks=dict(d.get("checks", {})),
            status=d.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"approvals": [a.to_dict() for a in self.approvals], "checks": dict(self.checks),
                "status": self.status}

    def digest(self) -> str:
        return sha256_digest(self.to_dict())


@dataclass(frozen=True)
class PolicyRef:
    """Identifies one exact policy: its declared version plus a digest of its content."""

    version: str
    digest: str


@dataclass(frozen=True)
class Policy:
    """Synthetic release policy used by the mock determiner."""

    base_approver_role: str
    required_checks: tuple[str, ...]
    safety_critical_parts: frozenset[str]
    safety_critical_approver_role: str
    receipt_ttl_seconds: int
    version: str
    releasable_statuses: tuple[str, ...] = ("PLANNED",)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Policy":
        return cls(
            base_approver_role=d["base_approver_role"],
            required_checks=tuple(d["required_checks"]),
            safety_critical_parts=frozenset(d["safety_critical_parts"]),
            safety_critical_approver_role=d["safety_critical_approver_role"],
            receipt_ttl_seconds=int(d["receipt_ttl_seconds"]),
            version=d["policy_version"],
            releasable_statuses=tuple(d["releasable_statuses"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.version,
            "base_approver_role": self.base_approver_role,
            "required_checks": list(self.required_checks),
            "safety_critical_parts": sorted(self.safety_critical_parts),
            "safety_critical_approver_role": self.safety_critical_approver_role,
            "receipt_ttl_seconds": self.receipt_ttl_seconds,
            "releasable_statuses": list(self.releasable_statuses),
        }

    def digest(self) -> str:
        return sha256_digest(self.to_dict())

    def ref(self) -> PolicyRef:
        return PolicyRef(self.version, self.digest())
