"""Stage 4: the release executor, the only component that can release a work order.

It enforces the gate itself instead of trusting the caller. Given a proposed
action and a receipt, it checks, in this order:

  A. The request, judged against the MES record (no receipt needed)
     1. the action type is RELEASE_WORK_ORDER          -> ACTION_TYPE_INVALID
     2. the work order exists in the MES               -> WORK_ORDER_UNKNOWN
     3. quantity is a whole number, 1..planned         -> QUANTITY_INVALID
  B. The receipt is authentic and current, before anything on it is read
     4. a receipt is present                           -> RECEIPT_MISSING
     5. the signature is valid and the key is known    -> SIGNATURE_INVALID
     6. the receipt has not expired                    -> RECEIPT_EXPIRED
  C. It was issued for exactly this request, under the policy in force
     7. same work order                                -> WORK_ORDER_MISMATCH
     8. same action (digest recomputed here)           -> ACTION_MISMATCH
     9. same policy version and content                -> POLICY_VERSION_MISMATCH
  D. The determination permits execution
    10. the verdict is ADMIT                           -> VERDICT_NOT_ADMIT
  E. Single use
    11. the receipt has not been used                  -> RECEIPT_ALREADY_USED
  F. What the determination relied on is still true (re-read from the authoritative sources)
    12. the MES record's facts are unchanged           -> WORK_ORDER_CHANGED
    13. the authoritative view (MES status and
        readiness, approvals, and both versions)
        still matches evidence_digest                  -> EVIDENCE_NOT_CURRENT

If every check passes, it marks the receipt consumed *before* calling the MES,
so the receipt is spent whether the release then succeeds or fails. A retry
needs a new determination.

  G. The MES write is conditional (see mes.py). The executor passes the MES
     version it verified in step 13. Inside one critical section the MES checks
     that the work order is still at that version, has no active quality hold,
     isn't session-locked and is still PLANNED, and only then writes. If QA
     places a hold after step 13, the release fails (FAILED,
     STATE_CHANGED_BEFORE_RELEASE) instead of going ahead on earlier state.
     Approvals live in a separate system the MES can't see, so an approval
     revoked after step 13 is NOT caught here. That window is unresolved.

Single use is checked before currency because a successful release itself
changes the work order's status; a replayed receipt should be reported as
already used, not as stale.

The executor never produces or infers a verdict. Its only outcomes are
RELEASED, FAILED and REFUSED. It does not import the determiner, the signer or
the demo key; it is handed a verifier. Missing configuration (no active policy,
no authoritative source) refuses rather than skipping the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .clock import Clock
from .mes import MESError, SimulatedMES
from .models import RELEASE_WORK_ORDER, PolicyRef, ProposedAction, valid_release_quantity
from .receipts import Receipt
from .verdicts import Verdict
from .verification import ReceiptVerifier


class ExecutionOutcome(str, Enum):
    RELEASED = "RELEASED"
    FAILED = "FAILED"
    REFUSED = "REFUSED"


class RefusalCause(str, Enum):
    ACTION_TYPE_INVALID = "ACTION_TYPE_INVALID"
    WORK_ORDER_UNKNOWN = "WORK_ORDER_UNKNOWN"
    QUANTITY_INVALID = "QUANTITY_INVALID"
    RECEIPT_MISSING = "RECEIPT_MISSING"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    RECEIPT_EXPIRED = "RECEIPT_EXPIRED"
    WORK_ORDER_MISMATCH = "WORK_ORDER_MISMATCH"
    ACTION_MISMATCH = "ACTION_MISMATCH"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    VERDICT_NOT_ADMIT = "VERDICT_NOT_ADMIT"
    EVIDENCE_NOT_CURRENT = "EVIDENCE_NOT_CURRENT"
    WORK_ORDER_CHANGED = "WORK_ORDER_CHANGED"
    RECEIPT_ALREADY_USED = "RECEIPT_ALREADY_USED"


class _VersionedRecord(Protocol):
    version: int


class _View(Protocol):
    record: _VersionedRecord

    def digest(self) -> str: ...


class EvidenceSource(Protocol):
    """In the demo this is AuthoritativeSources: the same read-only view the determiner uses."""

    def current(self, work_order_id: str) -> _View | None: ...


@dataclass(frozen=True)
class _Cleared:
    """Every executor check passed. Carries the MES version the release must be conditional on."""

    mes_version: int


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    work_order_id: str
    receipt_id: str | None
    detail: str
    work_order_status: str
    cause: RefusalCause | None = None
    mes_error_code: str | None = None
    simulated: bool = True

    def to_dict(self) -> dict:
        return {
            "simulated": self.simulated,
            "outcome": self.outcome.value,
            "cause": self.cause.value if self.cause else None,
            "mes_error_code": self.mes_error_code,
            "detail": self.detail,
            "work_order_id": self.work_order_id,
            "work_order_status": self.work_order_status,
            "receipt_id": self.receipt_id,
        }


class ConsumedReceipts:
    """In-memory single-use ledger. A real system would persist this."""

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def __contains__(self, receipt_id: str) -> bool:
        return receipt_id in self._ids

    def mark(self, receipt_id: str) -> None:
        self._ids.add(receipt_id)


class ReleaseExecutor:
    def __init__(
        self,
        mes: SimulatedMES,
        verifier: ReceiptVerifier,
        clock: Clock,
        consumed: ConsumedReceipts | None = None,
        *,
        active_policy: PolicyRef | None = None,
        evidence_source: EvidenceSource | None = None,
    ) -> None:
        self._mes = mes
        self._verifier = verifier
        self._clock = clock
        self._consumed = consumed if consumed is not None else ConsumedReceipts()
        self._active_policy = active_policy
        self._evidence = evidence_source

    def release(self, action: ProposedAction, receipt: Receipt | None) -> ExecutionResult:
        checked = self._check(action, receipt)
        if not isinstance(checked, _Cleared):
            cause, detail = checked
            return self._result(action, receipt, ExecutionOutcome.REFUSED, detail, cause=cause)

        assert receipt is not None
        self._consumed.mark(receipt.receipt_id)  # spent from here on, whatever happens next
        try:
            # G. Conditional write. The MES re-checks, atomically with the write, that the work order is still
            # at the version verified in F and has no active quality hold. A change that lands after F but
            # before the write makes the MES reject the release instead of applying it against earlier state.
            self._mes.release(action.work_order_id, action.quantity, expected_version=checked.mes_version)
        except MESError as e:
            return self._result(action, receipt, ExecutionOutcome.FAILED,
                                f"MES rejected the release: {e.message}", mes_error_code=e.code)
        return self._result(action, receipt, ExecutionOutcome.RELEASED, "work order released")

    # -- checks --------------------------------------------------------------

    def _check(self, action: ProposedAction, receipt: Receipt | None) -> "tuple[RefusalCause, str] | _Cleared":
        # A. The request itself, against the MES record. Nothing here reads the receipt.
        if action.action_type != RELEASE_WORK_ORDER:
            return (RefusalCause.ACTION_TYPE_INVALID,
                    f"action type {action.action_type!r} is not {RELEASE_WORK_ORDER}")
        record = self._mes.lookup(action.work_order_id)
        if record is None:
            return RefusalCause.WORK_ORDER_UNKNOWN, f"{action.work_order_id} is not in the MES"
        if not valid_release_quantity(action.quantity, record.planned_quantity):
            return (RefusalCause.QUANTITY_INVALID,
                    f"quantity {action.quantity!r} must be a whole number from 1 to the planned "
                    f"{record.planned_quantity} on the MES record")

        # B. Authenticity and freshness, before any field on the receipt is trusted.
        if receipt is None:
            return RefusalCause.RECEIPT_MISSING, "no receipt was presented"
        if not isinstance(receipt, Receipt):
            return RefusalCause.SIGNATURE_INVALID, "presented object is not a receipt"
        problem = self._verifier.check_signature(receipt)
        if problem is not None:
            return RefusalCause.SIGNATURE_INVALID, problem
        try:
            expired = self._clock.now() >= datetime.fromisoformat(receipt.expires_at)
        except (TypeError, ValueError):
            return RefusalCause.RECEIPT_EXPIRED, "receipt expiry is unreadable; treated as expired"
        if expired:
            return RefusalCause.RECEIPT_EXPIRED, f"receipt expired at {receipt.expires_at}"

        # C. Bound to exactly this request, under the policy in force.
        if receipt.work_order_id != action.work_order_id:
            return (RefusalCause.WORK_ORDER_MISMATCH,
                    f"receipt is for {receipt.work_order_id}, not {action.work_order_id}")
        if receipt.action_digest != action.digest():
            return RefusalCause.ACTION_MISMATCH, "action differs from the one the receipt was issued for"
        if self._active_policy is None:
            return RefusalCause.POLICY_VERSION_MISMATCH, "executor has no active policy configured"
        if (receipt.policy_version, receipt.policy_digest) != (self._active_policy.version, self._active_policy.digest):
            return (RefusalCause.POLICY_VERSION_MISMATCH,
                    f"receipt was issued under policy {receipt.policy_version!r}; "
                    f"policy in force is {self._active_policy.version!r}")

        # D. Only ADMIT permits execution.
        if receipt.verdict != Verdict.ADMIT.value:
            return RefusalCause.VERDICT_NOT_ADMIT, f"receipt verdict is {receipt.verdict}; only ADMIT permits release"

        # E. Single use.
        if receipt.receipt_id in self._consumed:
            return RefusalCause.RECEIPT_ALREADY_USED, f"{receipt.receipt_id} was already used"

        # F. What the determination relied on must still be true, re-read from the authoritative sources.
        if receipt.facts_digest != record.facts_digest():
            return RefusalCause.WORK_ORDER_CHANGED, "MES record facts have changed since the determination"
        if self._evidence is None:
            return RefusalCause.EVIDENCE_NOT_CURRENT, "executor has no authoritative source configured"
        on_record = self._evidence.current(action.work_order_id)
        if on_record is None:
            return RefusalCause.EVIDENCE_NOT_CURRENT, f"no authoritative state for {action.work_order_id}"
        if receipt.evidence_digest != on_record.digest():
            return (RefusalCause.EVIDENCE_NOT_CURRENT,
                    "authoritative state (MES status or readiness, or approvals) has changed since the determination")

        # The digest matched, so this is the MES version the determination was based on.
        return _Cleared(mes_version=on_record.record.version)

    def _result(
        self,
        action: ProposedAction,
        receipt: Receipt | None,
        outcome: ExecutionOutcome,
        detail: str,
        *,
        cause: RefusalCause | None = None,
        mes_error_code: str | None = None,
    ) -> ExecutionResult:
        record = self._mes.lookup(action.work_order_id)
        return ExecutionResult(
            outcome=outcome,
            work_order_id=action.work_order_id,
            receipt_id=receipt.receipt_id if isinstance(receipt, Receipt) else None,
            detail=detail,
            work_order_status=record.status if record is not None else "UNKNOWN",
            cause=cause,
            mes_error_code=mes_error_code,
        )
