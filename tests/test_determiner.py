"""The SIMULATED mock determiner: verdicts, precedence and the receipts it issues.

Rule tests change the authoritative state (MES readiness, approval system) and then
decide with no caller claims, because the rules are evaluated on authoritative state only.
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from wo_demo.demo_keys import DEMO_KEY_ID, DEMO_SECRET
from wo_demo.fixtures import load_cases
from wo_demo.models import Approval, ApprovalScope
from wo_demo.verdicts import Verdict
from wo_demo.verification import ReceiptVerifier

CASES = load_cases()


def _codes(determination):
    return [r.code for r in determination.reasons]


def _decide(env, case, *, approvals=None, checks=None, status="unchanged", action=None):
    """Set the authoritative state for the case's work order, then decide on it with no caller claims."""
    action = action or case.action
    wo = action.work_order_id
    if approvals is not None:
        env.approvals.record(wo, tuple(approvals))
    if checks is not None:
        env.mes.set_readiness(wo, checks)
    if status != "unchanged":
        env.mes.set_status(wo, status)
    return env.determiner.determine(action)


def _on_record(env, case):
    return env.approvals.current(case.action.work_order_id)[0]


def _readiness(env, case):
    return dict(env.mes.lookup(case.action.work_order_id).readiness)


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_fixture_cases_get_the_expected_verdict(env, case):
    d = env.determiner.determine(case.action, case.caller_evidence)
    assert d.verdict.value == case.expected["verdict"]
    assert d.referred_to == case.expected.get("referred_to")


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_every_determination_and_receipt_is_labeled_simulated(env, case):
    d = env.determiner.determine(case.action, case.caller_evidence)
    assert d.simulated is True
    assert d.receipt.simulated is True
    assert "SIMULATED" in d.receipt.issuer


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_every_verdict_gets_a_validly_signed_receipt_bound_to_the_authoritative_view(env, case):
    d = env.determiner.determine(case.action, case.caller_evidence)
    r = d.receipt
    assert ReceiptVerifier({DEMO_KEY_ID: DEMO_SECRET}).check_signature(r) is None
    assert r.verdict == d.verdict.value
    assert r.work_order_id == case.action.work_order_id
    assert r.action_digest == case.action.digest()
    assert r.evidence_digest == env.sources.current(case.action.work_order_id).digest()
    assert (r.policy_version, r.policy_digest) == (env.policy.version, env.policy.digest())
    assert r.facts_digest == env.mes.lookup(case.action.work_order_id).facts_digest()
    ttl = datetime.fromisoformat(r.expires_at) - datetime.fromisoformat(r.issued_at)
    assert ttl == timedelta(seconds=env.policy.receipt_ttl_seconds)


def test_receipt_ids_are_unique(env, admit_case):
    ids = {env.determiner.determine(admit_case.action).receipt.receipt_id for _ in range(20)}
    assert len(ids) == 20


def test_determiner_never_touches_the_mes(env):
    before = env.mes.snapshot()
    for case in CASES:
        env.determiner.determine(case.action, case.caller_evidence)
    assert env.mes.call_log == []
    assert env.mes.snapshot() == before


# --- rules, evaluated on authoritative state --------------------------------

def test_revoked_approval_halts(env, halt_case):
    d = env.determiner.determine(halt_case.action)
    assert d.verdict is Verdict.HALT
    assert "APPROVAL_REVOKED" in _codes(d)


def test_missing_required_check_defers(env, defer_case):
    d = env.determiner.determine(defer_case.action)
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)


def test_check_reported_as_null_counts_as_missing(env, admit_case):
    d = _decide(env, admit_case, checks={**_readiness(env, admit_case), "material_available": None})
    assert d.verdict is Verdict.DEFER


def test_no_supervisor_approval_defers(env, admit_case):
    d = _decide(env, admit_case, approvals=())
    assert d.verdict is Verdict.DEFER


def test_failed_check_halts(env, admit_case):
    d = _decide(env, admit_case, checks={**_readiness(env, admit_case), "quality_hold_clear": False})
    assert d.verdict is Verdict.HALT
    assert "CHECK_FAILED" in _codes(d)


@pytest.mark.parametrize("status", ["RELEASED", "ON_HOLD", "CLOSED", "planned", ""])
def test_status_that_is_not_releasable_halts(env, admit_case, status):
    d = _decide(env, admit_case, status=status)
    assert d.verdict is Verdict.HALT
    assert "WORK_ORDER_NOT_RELEASABLE" in _codes(d)


def test_missing_status_defers(env, admit_case):
    d = _decide(env, admit_case, status=None)
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)


@pytest.mark.parametrize("change", [{"max_quantity": 399}, {"line": "LINE-3"}, {"part_revision": "D"},
                                    {"work_order_id": "WO-SYN-1003"}],
                         ids=lambda c: next(iter(c)))
def test_approval_scope_that_does_not_cover_the_mes_record_halts(env, admit_case, change):
    approval = _on_record(env, admit_case)[0]
    narrowed = replace(approval, scope=replace(approval.scope, **change))
    d = _decide(env, admit_case, approvals=(narrowed,))
    assert d.verdict is Verdict.HALT
    assert "SCOPE_MISMATCH" in _codes(d)


@pytest.mark.parametrize("change", [{"line": "LINE-3"}, {"part_revision": "D"}, {"part_number": "PN-SYN-9999"},
                                    {"plant": "PLANT-SYN-02"}],
                         ids=lambda c: next(iter(c)))
def test_proposal_facts_that_disagree_with_the_mes_record_halt(env, admit_case, change):
    d = env.determiner.determine(replace(admit_case.action, **change))
    assert d.verdict is Verdict.HALT
    assert "FACT_MISMATCH" in _codes(d)


def test_safety_critical_part_without_quality_signoff_refers(env, refer_case):
    d = env.determiner.determine(refer_case.action)
    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"


def _qe(case, status):
    a = case.action
    return Approval(
        approval_id="APR-SYN-6005", approver_role="QUALITY_ENGINEERING", approver_id="SYN-QE-02", status=status,
        scope=ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity),
    )


def test_safety_critical_part_with_quality_signoff_admits(env, refer_case):
    d = _decide(env, refer_case, approvals=_on_record(env, refer_case) + (_qe(refer_case, "ACTIVE"),))
    assert d.verdict is Verdict.ADMIT


def test_revoked_quality_signoff_halts(env, refer_case):
    d = _decide(env, refer_case, approvals=_on_record(env, refer_case) + (_qe(refer_case, "REVOKED"),))
    assert d.verdict is Verdict.HALT


# --- precedence: HALT > REFER > DEFER > ADMIT -------------------------------

def test_precedence_order_is_halt_refer_defer():
    from wo_demo.determiner import PRECEDENCE
    assert [v.value for v in PRECEDENCE] == ["HALT", "REFER", "DEFER"]

def test_halt_takes_precedence_over_defer(env, halt_case):
    d = _decide(env, halt_case, checks={})
    assert d.verdict is Verdict.HALT
    assert {"APPROVAL_REVOKED", "MISSING_EVIDENCE"} <= set(_codes(d))  # all findings are still reported
    assert _codes(d)[0] == "APPROVAL_REVOKED"


def test_refer_takes_precedence_over_defer(env, refer_case):
    d = _decide(env, refer_case, checks={"bom_revision_confirmed": True})
    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"
    assert "MISSING_EVIDENCE" in _codes(d)  # still reported
    assert _codes(d)[0] == "AUTHORITY_REQUIRED"


def test_halt_takes_precedence_over_refer_and_defer_together(env, refer_case):
    revoked = replace(_on_record(env, refer_case)[0], status="REVOKED")
    d = _decide(env, refer_case, approvals=(revoked,), checks={"bom_revision_confirmed": True})
    assert d.verdict is Verdict.HALT
    assert d.referred_to is None
    codes = _codes(d)
    assert {"APPROVAL_REVOKED", "AUTHORITY_REQUIRED", "MISSING_EVIDENCE"} <= set(codes)
    assert codes.index("APPROVAL_REVOKED") < codes.index("AUTHORITY_REQUIRED") < codes.index("MISSING_EVIDENCE")


def test_halt_takes_precedence_over_refer(env, refer_case):
    revoked = replace(_on_record(env, refer_case)[0], status="REVOKED")
    assert _decide(env, refer_case, approvals=(revoked,)).verdict is Verdict.HALT
