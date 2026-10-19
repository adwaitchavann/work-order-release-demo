"""Stage 3: the SIMULATED mock determiner.

This is a stand-in for a real determination service, written only for this demo.
It reads the proposed action, the MES work order record and the evidence,
returns one of the four verdicts with reason codes, and issues a signed receipt
for every determination (ADMIT, HALT, DEFER and REFER alike). It can look work
orders up but cannot release them: it is given a read-only reader, not the MES.

Facts come from the MES record, never from the proposal. Part number, revision,
line and planned quantity are read from the record; the proposal's own claims
about them are only compared against it.

Rules (synthetic):
  HALT  - the request is invalid (unsupported action type, unknown work order,
          quantity not a whole number from 1 to the planned quantity), the
          proposal's facts disagree with the MES record, a required approval was
          revoked and nothing else in force covers the action, no approval in
          force covers the record's facts and quantity, or a required check is False.
  DEFER - required evidence is missing or unusable: the base approval, or a
          required check that is absent, null or anything other than a boolean.
  REFER - the part on the MES record is safety-critical and the second
          authority has not signed off.
  ADMIT - none of the above. Only a check value of exactly True passes.

When several apply, precedence is HALT > DEFER > REFER > ADMIT.

Revoked, then reissued: revocation is per approval. A new active approval that
covers the action is honoured; the revoked one is simply not relied on. See
_assess_authority and the README.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Protocol

from .clock import Clock
from .models import (
    FACT_FIELDS,
    RELEASE_WORK_ORDER,
    Approval,
    EvidenceBundle,
    Policy,
    ProposedAction,
    WorkOrderRecord,
    valid_release_quantity,
)
from .receipts import Receipt
from .signing import ReceiptSigner
from .verdicts import Verdict

PRECEDENCE = (Verdict.HALT, Verdict.DEFER, Verdict.REFER)
ACTIVE, REVOKED = "ACTIVE", "REVOKED"


class WorkOrderLookup(Protocol):
    def lookup(self, work_order_id: str) -> WorkOrderRecord | None: ...


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class Determination:
    verdict: Verdict
    reasons: tuple[Reason, ...]
    referred_to: str | None
    action_digest: str
    determined_at: str
    receipt: Receipt
    determiner: str = "mock-determiner"
    simulated: bool = True
    policy_version: str | None = None
    evidence_digest: str | None = None
    work_order_facts: dict[str, Any] | None = None  # from the MES record, as used for the verdict

    def to_dict(self) -> dict:
        return {
            "simulated": self.simulated,
            "determiner": self.determiner,
            "verdict": self.verdict.value,
            "reasons": [{"code": r.code, "detail": r.detail} for r in self.reasons],
            "referred_to": self.referred_to,
            "action_digest": self.action_digest,
            "evidence_digest": self.evidence_digest,
            "policy_version": self.policy_version,
            "work_order_facts": self.work_order_facts,
            "determined_at": self.determined_at,
            "receipt": self.receipt.to_dict(),
        }


@dataclass
class _Findings:
    by_verdict: dict[Verdict, list[Reason]] = field(
        default_factory=lambda: {Verdict.HALT: [], Verdict.DEFER: [], Verdict.REFER: []}
    )
    satisfied: list[Reason] = field(default_factory=list)
    referred_to: str | None = None

    def add(self, verdict: Verdict, code: str, detail: str) -> None:
        self.by_verdict[verdict].append(Reason(code, detail))

    def ok(self, code: str, detail: str) -> None:
        self.satisfied.append(Reason(code, detail))


def _new_receipt_id() -> str:
    return "rcpt-" + uuid.uuid4().hex


class MockDeterminer:
    """SIMULATED. Not a real Coherence determination."""

    def __init__(
        self,
        policy: Policy,
        signer: ReceiptSigner,
        clock: Clock,
        receipt_id_factory: Callable[[], str] = _new_receipt_id,
        *,
        work_orders: WorkOrderLookup,
    ) -> None:
        self._policy = policy
        self._signer = signer
        self._clock = clock
        self._new_receipt_id = receipt_id_factory
        self._work_orders = work_orders

    def determine(self, action: ProposedAction, evidence: EvidenceBundle) -> Determination:
        findings = _Findings()
        policy = self._policy

        record = self._assess_request(action, findings)
        if record is not None:
            self._assess_authority(
                role=policy.base_approver_role,
                approvals=evidence.approvals,
                record=record,
                quantity=action.quantity,
                findings=findings,
                when_missing=Verdict.DEFER,
            )
            # Safety-critical is decided by the part on the MES record, not the part the proposal names.
            if record.part_number in policy.safety_critical_parts:
                self._assess_authority(
                    role=policy.safety_critical_approver_role,
                    approvals=evidence.approvals,
                    record=record,
                    quantity=action.quantity,
                    findings=findings,
                    when_missing=Verdict.REFER,
                )
        self._assess_checks(evidence, findings)

        verdict = next((v for v in PRECEDENCE if findings.by_verdict[v]), Verdict.ADMIT)
        if verdict is Verdict.ADMIT:
            reasons = tuple(findings.satisfied)
        else:
            # Report every blocking finding, most severe first, so the trace shows the full picture.
            reasons = tuple(r for v in PRECEDENCE for r in findings.by_verdict[v])

        now = self._clock.now()
        action_digest = action.digest()
        evidence_digest = evidence.digest()
        receipt = self._signer.sign(
            receipt_id=self._new_receipt_id(),
            verdict=verdict.value,
            work_order_id=action.work_order_id,
            action_digest=action_digest,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=policy.receipt_ttl_seconds)).isoformat(),
            evidence_digest=evidence_digest,
            policy_version=policy.version,
            policy_digest=policy.digest(),
            facts_digest=record.facts_digest() if record is not None else None,
        )
        return Determination(
            verdict=verdict,
            reasons=reasons,
            referred_to=findings.referred_to if verdict is Verdict.REFER else None,
            action_digest=action_digest,
            determined_at=now.isoformat(),
            receipt=receipt,
            policy_version=policy.version,
            evidence_digest=evidence_digest,
            work_order_facts=record.facts() if record is not None else None,
        )

    # -- rules ---------------------------------------------------------------

    def _assess_request(self, action: ProposedAction, findings: _Findings) -> WorkOrderRecord | None:
        """Is the request valid, judged against the MES record? Returns the record if one exists."""
        if action.action_type != RELEASE_WORK_ORDER:
            findings.add(Verdict.HALT, "ACTION_TYPE_INVALID",
                         f"action type {action.action_type!r} is not {RELEASE_WORK_ORDER}")

        record = self._work_orders.lookup(action.work_order_id)
        if record is None:
            findings.add(Verdict.HALT, "WORK_ORDER_UNKNOWN", f"{action.work_order_id} is not in the MES")
            return None

        if not valid_release_quantity(action.quantity, record.planned_quantity):
            findings.add(Verdict.HALT, "QUANTITY_INVALID",
                         f"quantity {action.quantity!r} must be a whole number from 1 to the planned "
                         f"{record.planned_quantity} on the MES record")

        mismatched = False
        for name in FACT_FIELDS:
            claimed, actual = getattr(action, name), getattr(record, name)
            if claimed != actual:
                mismatched = True
                findings.add(Verdict.HALT, "FACT_MISMATCH",
                             f"proposal says {name} {claimed!r}; MES record says {actual!r}")
        if not mismatched:
            findings.ok("FACTS_MATCH_RECORD", "plant, line, part and revision match the MES record")
        return record

    def _assess_authority(
        self,
        *,
        role: str,
        approvals: tuple[Approval, ...],
        record: WorkOrderRecord,
        quantity: Any,
        findings: _Findings,
        when_missing: Verdict,
    ) -> None:
        """One authority's sign-off, including the revoked-then-reissued rule.

        Revocation is per approval: revoking an approval withdraws that approval
        only. A different approval from the same role that is active, in force
        and covers the action is honoured on its own, so a revoked approval
        followed by a new one admits. If nothing in force covers the action and
        a revoked approval exists for this work order, the result is HALT, not
        DEFER: the authority withdrew consent rather than never giving it.

        An approval named in another approval's `supersedes` is out of force
        even if still marked ACTIVE, so an amended (for example narrowed)
        approval replaces the original instead of adding to it.

        Any status other than exactly ACTIVE or REVOKED halts.
        """
        mine = [a for a in approvals if a.approver_role == role]
        if not mine:
            if when_missing is Verdict.REFER:
                findings.referred_to = role
                findings.add(Verdict.REFER, "AUTHORITY_REQUIRED",
                             f"{record.part_number} is safety-critical; release also needs {role} sign-off")
            else:
                findings.add(Verdict.DEFER, "MISSING_EVIDENCE", f"no {role} approval on record")
            return

        unrecognized = [a for a in mine if a.status not in (ACTIVE, REVOKED)]
        for a in unrecognized:
            findings.add(Verdict.HALT, "APPROVAL_STATUS_UNRECOGNIZED", f"{a.approval_id} has status {a.status!r}")
        if unrecognized:
            return

        revoked = [a for a in mine if a.status == REVOKED and a.scope.work_order_id == record.work_order_id]
        replaced = {a.supersedes for a in mine if a.supersedes}
        in_force = [a for a in mine if a.status == ACTIVE and a.approval_id not in replaced]
        covering = [a for a in in_force if a.scope.covers(record, quantity)]

        if covering:
            chosen = covering[0]
            if chosen.supersedes:
                findings.ok("APPROVAL_REISSUED",
                            f"{chosen.approval_id} by {role} supersedes {chosen.supersedes} and covers the action")
            else:
                findings.ok("APPROVAL_ACTIVE", f"{chosen.approval_id} by {role} is active and covers the action")
            for r in revoked:
                findings.ok("REVOKED_APPROVAL_NOT_RELIED_ON", f"{r.approval_id} is revoked and was not used")
            return

        for r in revoked:
            why = f" ({r.revoked_reason})" if r.revoked_reason else ""
            findings.add(Verdict.HALT, "APPROVAL_REVOKED",
                         f"{r.approval_id} by {role} was revoked{why}, and no other approval in force covers the action")
        if in_force or not revoked:
            findings.add(Verdict.HALT, "SCOPE_MISMATCH",
                         f"no {role} approval in force covers this work order's line, part, revision "
                         f"and quantity on the MES record")

    def _assess_checks(self, evidence: EvidenceBundle, findings: _Findings) -> None:
        """Fail closed: only a value of exactly True passes."""
        all_passed = True
        for name in self._policy.required_checks:
            value = evidence.checks.get(name)
            if value is True:
                continue
            all_passed = False
            if value is None:
                findings.add(Verdict.DEFER, "MISSING_EVIDENCE", f"required check '{name}' not provided")
            elif value is False:
                findings.add(Verdict.HALT, "CHECK_FAILED", f"required check '{name}' failed")
            else:
                findings.add(Verdict.DEFER, "EVIDENCE_INVALID",
                             f"required check '{name}' is {value!r}; only true counts as passed")
        if all_passed:
            findings.ok("EVIDENCE_COMPLETE", "all required checks present and exactly true")
