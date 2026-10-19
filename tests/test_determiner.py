"""The SIMULATED mock determiner: verdicts, precedence and the receipts it issues."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from wo_demo.fixtures import load_cases
from wo_demo.models import Approval, ApprovalScope, EvidenceBundle
from wo_demo.verdicts import Verdict
from wo_demo.verification import ReceiptVerifier
from wo_demo.demo_keys import DEMO_KEY_ID, DEMO_SECRET

CASES = load_cases()


def _codes(determination):
    return [r.code for r in determination.reasons]


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_fixture_cases_get_the_expected_verdict(env, case):
    d = env.determiner.determine(case.action, case.evidence)
    assert d.verdict.value == case.expected["verdict"]
    assert d.referred_to == case.expected.get("referred_to")


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_every_determination_and_receipt_is_labeled_simulated(env, case):
    d = env.determiner.determine(case.action, case.evidence)
    assert d.simulated is True
    assert d.receipt.simulated is True
    assert "SIMULATED" in d.receipt.issuer


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_every_verdict_gets_a_validly_signed_receipt_bound_to_the_action(env, case):
    d = env.determiner.determine(case.action, case.evidence)
    r = d.receipt
    assert ReceiptVerifier({DEMO_KEY_ID: DEMO_SECRET}).check_signature(r) is None
    assert r.verdict == d.verdict.value
    assert r.work_order_id == case.action.work_order_id
    assert r.action_digest == case.action.digest()
    assert r.evidence_digest == case.evidence.digest()
    assert (r.policy_version, r.policy_digest) == (env.policy.version, env.policy.digest())
    assert r.facts_digest == env.mes.lookup(case.action.work_order_id).facts_digest()
    ttl = datetime.fromisoformat(r.expires_at) - datetime.fromisoformat(r.issued_at)
    assert ttl == timedelta(seconds=env.policy.receipt_ttl_seconds)


def test_receipt_ids_are_unique(env, admit_case):
    ids = {env.determiner.determine(admit_case.action, admit_case.evidence).receipt.receipt_id for _ in range(20)}
    assert len(ids) == 20


def test_determiner_never_touches_the_mes(env):
    for case in CASES:
        env.determiner.determine(case.action, case.evidence)
    assert env.mes.call_log == []


# --- rules ------------------------------------------------------------------

def test_revoked_approval_halts(env, halt_case):
    d = env.determiner.determine(halt_case.action, halt_case.evidence)
    assert d.verdict is Verdict.HALT
    assert "APPROVAL_REVOKED" in _codes(d)


def test_missing_required_check_defers(env, defer_case):
    d = env.determiner.determine(defer_case.action, defer_case.evidence)
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)


def test_check_reported_as_null_counts_as_missing(env, admit_case):
    evidence = replace(admit_case.evidence, checks={**admit_case.evidence.checks, "material_available": None})
    assert env.determiner.determine(admit_case.action, evidence).verdict is Verdict.DEFER


def test_no_supervisor_approval_defers(env, admit_case):
    evidence = replace(admit_case.evidence, approvals=())
    d = env.determiner.determine(admit_case.action, evidence)
    assert d.verdict is Verdict.DEFER


def test_failed_check_halts(env, admit_case):
    evidence = replace(admit_case.evidence, checks={**admit_case.evidence.checks, "quality_hold_clear": False})
    d = env.determiner.determine(admit_case.action, evidence)
    assert d.verdict is Verdict.HALT
    assert "CHECK_FAILED" in _codes(d)


@pytest.mark.parametrize("change", [{"max_quantity": 399}, {"line": "LINE-3"}, {"part_revision": "D"},
                                    {"work_order_id": "WO-SYN-1003"}],
                         ids=lambda c: next(iter(c)))
def test_approval_scope_that_does_not_cover_the_mes_record_halts(env, admit_case, change):
    approval = admit_case.evidence.approvals[0]
    narrowed = replace(approval, scope=replace(approval.scope, **change))
    d = env.determiner.determine(admit_case.action, replace(admit_case.evidence, approvals=(narrowed,)))
    assert d.verdict is Verdict.HALT
    assert "SCOPE_MISMATCH" in _codes(d)


@pytest.mark.parametrize("change", [{"line": "LINE-3"}, {"part_revision": "D"}, {"part_number": "PN-SYN-9999"},
                                    {"plant": "PLANT-SYN-02"}],
                         ids=lambda c: next(iter(c)))
def test_proposal_facts_that_disagree_with_the_mes_record_halt(env, admit_case, change):
    d = env.determiner.determine(replace(admit_case.action, **change), admit_case.evidence)
    assert d.verdict is Verdict.HALT
    assert "FACT_MISMATCH" in _codes(d)


def test_safety_critical_part_without_quality_signoff_refers(env, refer_case):
    d = env.determiner.determine(refer_case.action, refer_case.evidence)
    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"


def test_safety_critical_part_with_quality_signoff_admits(env, refer_case):
    a = refer_case.action
    qe = Approval(
        approval_id="APR-SYN-6005", approver_role="QUALITY_ENGINEERING", approver_id="SYN-QE-02", status="ACTIVE",
        scope=ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity),
    )
    evidence = replace(refer_case.evidence, approvals=refer_case.evidence.approvals + (qe,))
    assert env.determiner.determine(a, evidence).verdict is Verdict.ADMIT


def test_revoked_quality_signoff_halts(env, refer_case):
    a = refer_case.action
    qe = Approval(
        approval_id="APR-SYN-6005", approver_role="QUALITY_ENGINEERING", approver_id="SYN-QE-02", status="REVOKED",
        scope=ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity),
    )
    evidence = replace(refer_case.evidence, approvals=refer_case.evidence.approvals + (qe,))
    assert env.determiner.determine(a, evidence).verdict is Verdict.HALT


# --- precedence: HALT > DEFER > REFER > ADMIT -------------------------------

def test_halt_takes_precedence_over_defer(env, halt_case):
    evidence = replace(halt_case.evidence, checks={})
    d = env.determiner.determine(halt_case.action, evidence)
    assert d.verdict is Verdict.HALT
    assert {"APPROVAL_REVOKED", "MISSING_EVIDENCE"} <= set(_codes(d))  # all findings are still reported
    assert _codes(d)[0] == "APPROVAL_REVOKED"


def test_defer_takes_precedence_over_refer(env, refer_case):
    evidence = replace(refer_case.evidence, checks={"bom_revision_confirmed": True})
    d = env.determiner.determine(refer_case.action, evidence)
    assert d.verdict is Verdict.DEFER
    assert d.referred_to is None
    assert "AUTHORITY_REQUIRED" in _codes(d)


def test_halt_takes_precedence_over_refer(env, refer_case):
    revoked = replace(refer_case.evidence.approvals[0], status="REVOKED")
    evidence = EvidenceBundle(approvals=(revoked,), checks=refer_case.evidence.checks)
    assert env.determiner.determine(refer_case.action, evidence).verdict is Verdict.HALT
