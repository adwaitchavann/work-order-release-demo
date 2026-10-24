"""Stage 3: the SIMULATED mock determiner.

This is a stand-in for a real determination service, written only for this demo.
It returns one of the four verdicts with reason codes and issues a signed receipt
for every determination (ADMIT, HALT, DEFER and REFER alike).

Where its inputs come from (see authority.py):
  - The caller supplies the proposed action: what it wants to do.
  - Everything the verdict depends on is read from the authoritative sources and
    assembled into one view: facts, status and readiness from the MES, then
    approvals from the approval system. These are two sequential reads, not a
    consistent cross-system snapshot (see authority.py). The determiner can read
    the sources but cannot change or release anything.
  - The caller may also send its own evidence (status, checks, approvals). Caller
    claims cannot establish admissibility; contradictions may independently cause
    HALT. A claim that contradicts the record halts (like FACT_MISMATCH for the
    proposal's facts), and a claim the record can't confirm is noted and ignored,
    so it never fills in missing state.

Rules (synthetic), all evaluated on the authoritative view:
  HALT  - invalid request (action type, unknown work order, quantity); proposal facts
          or caller claims that contradict the record; a status that isn't releasable;
          a revoked approval with nothing else in force covering the action; no
          approval in force covers the record's facts and quantity; an unrecognized
          approval status; a readiness check that is False.
  REFER - the part on the record is safety-critical and the second authority hasn't
          signed off in the approval system.
  DEFER - required state is missing or unusable: no status reported, no base
          approval on record, or a readiness check that is absent, null or not a boolean.
  ADMIT - none of the above.

When several apply, precedence is HALT > REFER > DEFER > ADMIT. Every finding is
still reported, most severe first. A REFER that also has missing state is still
referred; the missing state is listed alongside the referral.

Revoked, then reissued: revocation is per approval (see _assess_authority).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from .authority import AuthoritativeSources, AuthoritativeView, unknown_work_order_digest
from .canonical import canonical_json
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

PRECEDENCE = (Verdict.HALT, Verdict.REFER, Verdict.DEFER)
ACTIVE, REVOKED = "ACTIVE", "REVOKED"


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
    authoritative: dict[str, Any] | None = None     # the authoritative view the verdict was based on
    notes: tuple[Reason, ...] = ()                  # caller claims that were not used, and why

    def to_dict(self) -> dict:
        return {
            "simulated": self.simulated,
            "determiner": self.determiner,
            "verdict": self.verdict.value,
            "reasons": [{"code": r.code, "detail": r.detail} for r in self.reasons],
            "notes": [{"code": r.code, "detail": r.detail} for r in self.notes],
            "referred_to": self.referred_to,
            "action_digest": self.action_digest,
            "evidence_digest": self.evidence_digest,
            "policy_version": self.policy_version,
            "work_order_facts": self.work_order_facts,
            "authoritative": self.authoritative,
            "determined_at": self.determined_at,
            "receipt": self.receipt.to_dict(),
        }


@dataclass
class _Findings:
    by_verdict: dict[Verdict, list[Reason]] = field(
        default_factory=lambda: {Verdict.HALT: [], Verdict.DEFER: [], Verdict.REFER: []}
    )
    satisfied: list[Reason] = field(default_factory=list)
    notes: list[Reason] = field(default_factory=list)
    referred_to: str | None = None

    def add(self, verdict: Verdict, code: str, detail: str) -> None:
        self.by_verdict[verdict].append(Reason(code, detail))

    def ok(self, code: str, detail: str) -> None:
        self.satisfied.append(Reason(code, detail))

    def note(self, code: str, detail: str) -> None:
        self.notes.append(Reason(code, detail))


def _new_receipt_id() -> str:
    return "rcpt-" + uuid.uuid4().hex


def _same(a: Any, b: Any) -> bool:
    """Strict equality: True and 1, or 400 and 400.0, are different values."""
    try:
        return canonical_json(a) == canonical_json(b)
    except (TypeError, ValueError):
        return False


class MockDeterminer:
    """SIMULATED. Not a real Coherence determination."""

    def __init__(
        self,
        policy: Policy,
        signer: ReceiptSigner,
        clock: Clock,
        receipt_id_factory: Callable[[], str] = _new_receipt_id,
        *,
        sources: AuthoritativeSources,
    ) -> None:
        self._policy = policy
        self._signer = signer
        self._clock = clock
        self._new_receipt_id = receipt_id_factory
        self._sources = sources

    def determine(self, action: ProposedAction, caller_evidence: EvidenceBundle | None = None) -> Determination:
        """`caller_evidence` is whatever the proposing caller asserts. It is not authoritative."""
        findings = _Findings()
        policy = self._policy

        # One view of the authoritative sources (MES read, then approvals read); everything below uses it.
        view = self._sources.current(action.work_order_id)
        record = view.record if view is not None else None

        self._assess_request(action, record, findings)
        if view is not None:
            authority = view.evidence
            self._assess_status(authority.status, findings)
            self._assess_authority(
                role=policy.base_approver_role,
                approvals=authority.approvals,
                record=view.record,
                quantity=action.quantity,
                findings=findings,
                when_missing=Verdict.DEFER,
            )
            # Safety-critical is decided by the part on the MES record, not the part the proposal names.
            if view.record.part_number in policy.safety_critical_parts:
                self._assess_authority(
                    role=policy.safety_critical_approver_role,
                    approvals=authority.approvals,
                    record=view.record,
                    quantity=action.quantity,
                    findings=findings,
                    when_missing=Verdict.REFER,
                )
            self._assess_checks(authority.checks, findings)
            self._compare_claims(caller_evidence, view, findings)

        verdict = next((v for v in PRECEDENCE if findings.by_verdict[v]), Verdict.ADMIT)
        if verdict is Verdict.ADMIT:
            reasons = tuple(findings.satisfied)
        else:
            # Report every blocking finding, most severe first, so the trace shows the full picture.
            reasons = tuple(r for v in PRECEDENCE for r in findings.by_verdict[v])

        now = self._clock.now()
        action_digest = action.digest()
        evidence_digest = view.digest() if view is not None else unknown_work_order_digest(action.work_order_id)
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
            authoritative=view.to_dict() if view is not None else None,
            notes=tuple(findings.notes),
        )

    # -- rules ---------------------------------------------------------------

    def _assess_request(self, action: ProposedAction, record: WorkOrderRecord | None, findings: _Findings) -> None:
        """Is the request valid, judged against the MES record?"""
        if action.action_type != RELEASE_WORK_ORDER:
            findings.add(Verdict.HALT, "ACTION_TYPE_INVALID",
                         f"action type {action.action_type!r} is not {RELEASE_WORK_ORDER}")

        if record is None:
            findings.add(Verdict.HALT, "WORK_ORDER_UNKNOWN", f"{action.work_order_id} is not in the MES")
            return

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

    def _assess_status(self, status: Any, findings: _Findings) -> None:
        if status is None:
            findings.add(Verdict.DEFER, "MISSING_EVIDENCE", "the MES reports no status for this work order")
        elif status in self._policy.releasable_statuses:
            findings.ok("STATUS_RELEASABLE", f"MES status is {status}")
        else:
            findings.add(Verdict.HALT, "WORK_ORDER_NOT_RELEASABLE",
                         f"MES status is {status!r}; only {', '.join(self._policy.releasable_statuses)} can be released")

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
        """One authority's sign-off, as recorded in the approval system.

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
                findings.add(Verdict.DEFER, "MISSING_EVIDENCE", f"no {role} approval in the approval system")
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

    def _assess_checks(self, checks: Any, findings: _Findings) -> None:
        """Readiness signals as reported by the MES. Fail closed: only exactly True passes."""
        all_passed = True
        for name in self._policy.required_checks:
            value = checks.get(name)
            if value is True:
                continue
            all_passed = False
            if value is None:
                findings.add(Verdict.DEFER, "MISSING_EVIDENCE", f"the MES reports no value for '{name}'")
            elif value is False:
                findings.add(Verdict.HALT, "CHECK_FAILED", f"the MES reports '{name}' as false")
            else:
                findings.add(Verdict.DEFER, "EVIDENCE_INVALID",
                             f"the MES reports '{name}' as {value!r}; only true counts as passed")
        if all_passed:
            findings.ok("EVIDENCE_COMPLETE", "the MES reports every required check as exactly true")

    def _compare_claims(self, claims: Any, view: AuthoritativeView, findings: _Findings) -> None:
        """Caller claims cannot establish admissibility; contradictions may independently cause HALT.
        Anything the record can't confirm is noted and ignored."""
        if claims is None:
            return
        if not isinstance(claims, EvidenceBundle):
            findings.add(Verdict.HALT, "CLAIM_MALFORMED", "caller evidence is not in a recognizable form")
            return
        authority = view.evidence

        if claims.status is not None:
            if authority.status is None:
                findings.note("CLAIM_NOT_USED", f"caller says status {claims.status!r}; the MES reports none")
            elif not _same(claims.status, authority.status):
                findings.add(Verdict.HALT, "CLAIM_MISMATCH",
                             f"caller says status {claims.status!r}; the MES says {authority.status!r}")

        for name, claimed in claims.checks.items():
            if claimed is None:
                continue
            actual = authority.checks.get(name)
            if actual is None:
                findings.note("CLAIM_NOT_USED",
                              f"caller says {name}={claimed!r}; the MES has no value, so the claim is not used")
            elif not _same(claimed, actual):
                findings.add(Verdict.HALT, "CLAIM_MISMATCH",
                             f"caller says {name}={claimed!r}; the MES says {actual!r}")

        on_record = {a.approval_id: a for a in authority.approvals}
        for claimed in claims.approvals:
            actual = on_record.get(getattr(claimed, "approval_id", None))
            if actual is None:
                findings.note("CLAIM_NOT_USED",
                              f"caller lists {getattr(claimed, 'approval_id', claimed)!r}; "
                              f"the approval system has no such approval, so it is not used")
            elif not _same(claimed.to_dict(), actual.to_dict()):
                findings.add(Verdict.HALT, "CLAIM_MISMATCH",
                             f"caller's copy of {actual.approval_id} differs from the approval system "
                             f"(caller: {claimed.status}, scope max {claimed.scope.max_quantity}; "
                             f"system: {actual.status}, scope max {actual.scope.max_quantity})")
