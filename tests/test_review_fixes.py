"""Tests for the five review fixes: fail-closed checks, MES record as source of facts, request
validation in both components, revoked-then-reissued approvals, and receipt bindings."""

from dataclasses import replace

import pytest

from wo_demo.determiner import MockDeterminer
from wo_demo.executor import ExecutionOutcome, RefusalCause, ReleaseExecutor
from wo_demo.models import Approval, ApprovalScope
from wo_demo.verdicts import Verdict

RELEASED, REFUSED = ExecutionOutcome.RELEASED, ExecutionOutcome.REFUSED


def _codes(d):
    return [r.code for r in d.reasons]


def _with_checks(case, **checks):
    return replace(case.evidence, checks={**case.evidence.checks, **checks})


def _decide_and_release(env, case, evidence, action=None):
    """Put the evidence on record, decide on it, and hand the receipt straight to the executor."""
    action = action or case.action
    env.evidence.record(action.work_order_id, evidence)
    d = env.determiner.determine(action, evidence)
    return d, env.executor.release(action, d.receipt)


# --- 1. Fail closed on check values -----------------------------------------

@pytest.mark.parametrize("value", ["FAIL", "PASS", "true", "True", 0, 1, 1.0, "", [], {}, "yes"],
                         ids=repr)
def test_check_value_that_is_not_exactly_true_blocks(env, admit_case, value):
    d, result = _decide_and_release(env, admit_case, _with_checks(admit_case, quality_hold_clear=value))

    assert d.verdict is not Verdict.ADMIT
    assert d.verdict is Verdict.DEFER
    assert "EVIDENCE_INVALID" in _codes(d)
    assert result.outcome is REFUSED
    assert env.mes.status(admit_case.action.work_order_id) == "PLANNED"
    assert env.mes.call_log == []


def test_check_value_false_halts(env, admit_case):
    d, result = _decide_and_release(env, admit_case, _with_checks(admit_case, quality_hold_clear=False))
    assert d.verdict is Verdict.HALT
    assert result.outcome is REFUSED


@pytest.mark.parametrize("value", [None, "absent"], ids=["null", "absent"])
def test_missing_check_defers(env, admit_case, value):
    checks = dict(admit_case.evidence.checks)
    if value == "absent":
        del checks["quality_hold_clear"]
    else:
        checks["quality_hold_clear"] = None
    d, result = _decide_and_release(env, admit_case, replace(admit_case.evidence, checks=checks))
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)
    assert result.outcome is REFUSED


def test_only_exactly_true_on_every_check_admits(env, admit_case):
    d, result = _decide_and_release(env, admit_case, admit_case.evidence)
    assert d.verdict is Verdict.ADMIT
    assert result.outcome is RELEASED


# --- 2. Facts come from the MES record, not the proposal ---------------------

def test_mislabeled_part_cannot_skip_quality_engineering_referral(env, refer_case):
    """The proposal calls WO-SYN-1005 a non-safety-critical part. The MES record says otherwise."""
    action = replace(refer_case.action, part_number="PN-SYN-2210")

    d, result = _decide_and_release(env, refer_case, refer_case.evidence, action=action)

    assert d.verdict is not Verdict.ADMIT
    assert "FACT_MISMATCH" in _codes(d)
    assert "AUTHORITY_REQUIRED" in _codes(d)  # the QE requirement is still found, from the record
    assert result.outcome is REFUSED
    assert env.mes.call_log == []


def test_mislabeled_proposal_and_matching_mislabeled_approval_still_refer(env, refer_case):
    """Even if the approval was scoped to the same wrong part, the record decides what the part is."""
    action = replace(refer_case.action, part_number="PN-SYN-2210")
    approval = refer_case.evidence.approvals[0]
    wrong = replace(approval, scope=replace(approval.scope, part_number="PN-SYN-2210"))
    evidence = replace(refer_case.evidence, approvals=(wrong,))

    d, result = _decide_and_release(env, refer_case, evidence, action=action)

    assert d.verdict is Verdict.HALT
    assert {"FACT_MISMATCH", "SCOPE_MISMATCH", "AUTHORITY_REQUIRED"} <= set(_codes(d))
    assert result.outcome is REFUSED


def test_safety_critical_status_follows_the_mes_record(env, admit_case):
    """If the MES says WO-SYN-1001 is now a safety-critical part, the proposal can't argue otherwise."""
    env.mes.amend("WO-SYN-1001", part_number="PN-SYN-7730")
    approval = admit_case.evidence.approvals[0]
    scoped = replace(approval, scope=replace(approval.scope, part_number="PN-SYN-7730"))
    action = replace(admit_case.action, part_number="PN-SYN-7730")

    d = env.determiner.determine(action, replace(admit_case.evidence, approvals=(scoped,)))

    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"


def test_determination_reports_the_record_facts_it_used(env, refer_case):
    d = env.determiner.determine(refer_case.action, refer_case.evidence)
    assert d.work_order_facts == env.mes.lookup("WO-SYN-1005").facts()


# --- 3. Quantity and action type, in the determiner and the executor ---------

BAD_QUANTITIES = [0, -5, 401, 400.0, "400", True, None]


@pytest.mark.parametrize("quantity", BAD_QUANTITIES, ids=repr)
def test_determiner_halts_on_invalid_quantity(env, admit_case, quantity):
    d = env.determiner.determine(replace(admit_case.action, quantity=quantity), admit_case.evidence)
    assert d.verdict is Verdict.HALT
    assert "QUANTITY_INVALID" in _codes(d)


@pytest.mark.parametrize("quantity", BAD_QUANTITIES, ids=repr)
def test_executor_refuses_invalid_quantity_even_with_a_valid_admit_receipt(
        env, admit_case, sign_receipt, assert_mes_untouched, quantity):
    action = replace(admit_case.action, quantity=quantity)
    result = env.executor.release(action, sign_receipt(action))
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.QUANTITY_INVALID)
    assert_mes_untouched()


@pytest.mark.parametrize("action_type", ["CLOSE_WORK_ORDER", "release_work_order", "", None])
def test_determiner_halts_on_unsupported_action_type(env, admit_case, action_type):
    d = env.determiner.determine(replace(admit_case.action, action_type=action_type), admit_case.evidence)
    assert d.verdict is Verdict.HALT
    assert "ACTION_TYPE_INVALID" in _codes(d)


@pytest.mark.parametrize("action_type", ["CLOSE_WORK_ORDER", "release_work_order", "", None])
def test_executor_refuses_unsupported_action_type_even_with_a_valid_admit_receipt(
        env, admit_case, sign_receipt, assert_mes_untouched, action_type):
    action = replace(admit_case.action, action_type=action_type)
    result = env.executor.release(action, sign_receipt(action))
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.ACTION_TYPE_INVALID)
    assert_mes_untouched()


def test_unknown_work_order_is_halted_and_refused(env, admit_case, sign_receipt, assert_mes_untouched):
    action = replace(admit_case.action, work_order_id="WO-SYN-9999")
    d = env.determiner.determine(action, admit_case.evidence)
    assert d.verdict is Verdict.HALT
    assert "WORK_ORDER_UNKNOWN" in _codes(d)
    assert env.executor.release(action, sign_receipt(action)).cause is RefusalCause.WORK_ORDER_UNKNOWN
    assert_mes_untouched()


def test_partial_quantity_within_plan_and_approval_is_released(env, admit_case):
    action = replace(admit_case.action, quantity=250)
    d, result = _decide_and_release(env, admit_case, admit_case.evidence, action=action)
    assert d.verdict is Verdict.ADMIT
    assert result.outcome is RELEASED


def test_executor_checks_quantity_against_the_current_plan(env, admit_case, assert_mes_untouched):
    """Planned quantity cut after ADMIT: the executor re-reads the MES record and refuses."""
    d = env.determiner.determine(admit_case.action, admit_case.evidence)
    assert d.verdict is Verdict.ADMIT
    env.mes.amend("WO-SYN-1001", planned_quantity=300)
    before = env.mes.snapshot()

    result = env.executor.release(admit_case.action, d.receipt)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.QUANTITY_INVALID)
    assert env.mes.snapshot() == before and env.mes.call_log == []


# --- 4. Revoked, then reissued ----------------------------------------------

def _supervisor(approval_id, status="ACTIVE", *, supersedes=None, max_quantity=300, role="PRODUCTION_SUPERVISOR"):
    return Approval(
        approval_id=approval_id, approver_role=role, approver_id="SYN-SUP-03", status=status,
        supersedes=supersedes,
        scope=ApprovalScope("WO-SYN-1006", "LINE-3", "PN-SYN-5120", "B", max_quantity),
    )


def _reissue(env, case, *approvals):
    return _decide_and_release(env, case, replace(case.evidence, approvals=approvals))


def test_new_approval_after_a_revocation_admits_and_releases(env, reissue_case):
    d, result = _decide_and_release(env, reissue_case, reissue_case.evidence)
    assert d.verdict is Verdict.ADMIT
    assert "APPROVAL_REISSUED" in _codes(d)
    assert "REVOKED_APPROVAL_NOT_RELIED_ON" in _codes(d)
    assert result.outcome is RELEASED


def test_new_approval_need_not_name_the_revoked_one(env, reissue_case):
    """Revocation is per approval: an unlinked new approval stands on its own."""
    d, result = _reissue(env, reissue_case, _supervisor("R", "REVOKED"), _supervisor("N"))
    assert d.verdict is Verdict.ADMIT
    assert result.outcome is RELEASED


def test_revocation_with_nothing_else_in_force_halts_rather_than_defers(env, reissue_case):
    d, result = _reissue(env, reissue_case, _supervisor("R", "REVOKED"))
    assert d.verdict is Verdict.HALT
    assert "APPROVAL_REVOKED" in _codes(d)
    assert result.outcome is REFUSED


def test_revocation_plus_an_approval_that_does_not_cover_the_action_halts(env, reissue_case):
    d, _ = _reissue(env, reissue_case, _supervisor("R", "REVOKED"), _supervisor("N", max_quantity=100))
    assert d.verdict is Verdict.HALT
    assert {"APPROVAL_REVOKED", "SCOPE_MISMATCH"} <= set(_codes(d))


def test_approval_from_a_different_role_does_not_stand_in(env, reissue_case):
    d, _ = _reissue(env, reissue_case,
                    _supervisor("R", "REVOKED"),
                    _supervisor("N", supersedes="R", role="PLANT_MANAGER"))
    assert d.verdict is Verdict.HALT
    assert "APPROVAL_REVOKED" in _codes(d)


def test_superseded_active_approval_is_no_longer_in_force(env, reissue_case):
    """A (300) is amended to B (100). A request for 300 is no longer covered, even though A still says ACTIVE."""
    d, _ = _reissue(env, reissue_case, _supervisor("A"), _supervisor("B", supersedes="A", max_quantity=100))
    assert d.verdict is Verdict.HALT
    assert "SCOPE_MISMATCH" in _codes(d)


@pytest.mark.parametrize("status", ["active", "Revoked", "PENDING", "", None])
def test_unrecognized_approval_status_halts(env, reissue_case, status):
    d, _ = _reissue(env, reissue_case, _supervisor("N"), _supervisor("X", status))
    assert d.verdict is Verdict.HALT
    assert "APPROVAL_STATUS_UNRECOGNIZED" in _codes(d)


def test_approval_revoked_after_admit_stops_the_receipt(env, reissue_case, assert_mes_untouched):
    d = env.determiner.determine(reissue_case.action, reissue_case.evidence)
    assert d.verdict is Verdict.ADMIT
    revoked = tuple(replace(a, status="REVOKED") if a.status == "ACTIVE" else a
                    for a in reissue_case.evidence.approvals)
    env.evidence.record("WO-SYN-1006", replace(reissue_case.evidence, approvals=revoked))

    result = env.executor.release(reissue_case.action, d.receipt)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.EVIDENCE_NOT_CURRENT)
    assert_mes_untouched()


# --- 5. Evidence digest and policy version in the receipt --------------------

def test_receipt_binds_evidence_digest_and_policy_version(env, admit_case):
    r = env.determiner.determine(admit_case.action, admit_case.evidence).receipt
    assert r.evidence_digest == admit_case.evidence.digest()
    assert r.policy_version == env.policy.version == "SYN-REL-POLICY-v3"
    assert r.policy_digest == env.policy.digest()
    assert r.facts_digest == env.mes.lookup("WO-SYN-1001").facts_digest()


def test_different_evidence_gives_a_different_evidence_digest(env, admit_case):
    a = env.determiner.determine(admit_case.action, admit_case.evidence).receipt
    b = env.determiner.determine(admit_case.action, _with_checks(admit_case, material_available=None)).receipt
    assert a.evidence_digest != b.evidence_digest


def test_receipt_from_an_older_policy_is_refused(env, admit_case, assert_mes_untouched):
    old = MockDeterminer(replace(env.policy, version="SYN-REL-POLICY-v2"), env.signer, env.clock,
                         work_orders=env.mes.reader())
    result = env.executor.release(admit_case.action, old.determine(admit_case.action, admit_case.evidence).receipt)
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.POLICY_VERSION_MISMATCH)
    assert_mes_untouched()


def test_policy_edited_without_a_version_bump_is_refused(env, admit_case, assert_mes_untouched):
    edited = replace(env.policy, safety_critical_parts=frozenset())  # same version string, different content
    old = MockDeterminer(edited, env.signer, env.clock, work_orders=env.mes.reader())
    result = env.executor.release(admit_case.action, old.determine(admit_case.action, admit_case.evidence).receipt)
    assert result.cause is RefusalCause.POLICY_VERSION_MISMATCH
    assert_mes_untouched()


@pytest.mark.parametrize("missing", ["policy_version", "policy_digest"])
def test_receipt_without_policy_binding_is_refused(env, admit_case, sign_receipt, assert_mes_untouched, missing):
    result = env.executor.release(admit_case.action, sign_receipt(admit_case.action, **{missing: None}))
    assert result.cause is RefusalCause.POLICY_VERSION_MISMATCH
    assert_mes_untouched()


def test_receipt_without_evidence_digest_is_refused(env, admit_case, sign_receipt, assert_mes_untouched):
    result = env.executor.release(admit_case.action, sign_receipt(admit_case.action, evidence_digest=None))
    assert result.cause is RefusalCause.EVIDENCE_NOT_CURRENT
    assert_mes_untouched()


def test_admit_on_evidence_that_is_not_on_record_is_refused(env, admit_case, assert_mes_untouched):
    """Calling code decides on doctored evidence; the executor compares against the evidence on record."""
    doctored = _with_checks(admit_case, quality_hold_clear=True, extra_note=True)
    d = env.determiner.determine(admit_case.action, doctored)
    assert d.verdict is Verdict.ADMIT
    result = env.executor.release(admit_case.action, d.receipt)
    assert result.cause is RefusalCause.EVIDENCE_NOT_CURRENT
    assert_mes_untouched()


def test_mes_facts_changed_after_admit_is_refused(env, admit_case, assert_mes_untouched):
    d = env.determiner.determine(admit_case.action, admit_case.evidence)
    env.mes.amend("WO-SYN-1001", part_revision="D")
    before = env.mes.snapshot()
    result = env.executor.release(admit_case.action, d.receipt)
    assert result.cause is RefusalCause.WORK_ORDER_CHANGED
    assert env.mes.snapshot() == before and env.mes.call_log == []


def test_executor_without_policy_or_evidence_source_refuses(env, admit_case):
    receipt = env.determiner.determine(admit_case.action, admit_case.evidence).receipt
    bare = ReleaseExecutor(env.mes, env.verifier, env.clock)
    assert bare.release(admit_case.action, receipt).cause is RefusalCause.POLICY_VERSION_MISMATCH
    no_evidence = ReleaseExecutor(env.mes, env.verifier, env.clock, active_policy=env.policy.ref())
    assert no_evidence.release(admit_case.action, receipt).cause is RefusalCause.EVIDENCE_NOT_CURRENT
    assert env.mes.call_log == []


# --- Check order ------------------------------------------------------------

def test_signature_is_checked_before_the_verdict(env, halt_case):
    """A tampered receipt is reported as tampered, not as a HALT, because its verdict field means nothing."""
    halt = env.determiner.determine(halt_case.action, halt_case.evidence).receipt
    assert env.executor.release(halt_case.action, replace(halt, verdict="ADMIT")).cause is RefusalCause.SIGNATURE_INVALID
    assert env.executor.release(halt_case.action, replace(halt, work_order_id="X")).cause is RefusalCause.SIGNATURE_INVALID


def test_expiry_is_checked_before_the_verdict(env, halt_case):
    halt = env.determiner.determine(halt_case.action, halt_case.evidence).receipt
    env.clock.advance(env.policy.receipt_ttl_seconds)
    assert env.executor.release(halt_case.action, halt).cause is RefusalCause.RECEIPT_EXPIRED


def test_invalid_request_is_refused_before_the_receipt_is_examined(env, admit_case):
    action = replace(admit_case.action, quantity=0)
    assert env.executor.release(action, None).cause is RefusalCause.QUANTITY_INVALID


def test_refusal_never_consumes_the_receipt_but_an_attempt_always_does(env, failing_case):
    receipt = env.determiner.determine(failing_case.action, failing_case.evidence).receipt
    assert env.executor.release(replace(failing_case.action, quantity=0), receipt).outcome is REFUSED
    assert env.executor.release(failing_case.action, receipt).outcome is ExecutionOutcome.FAILED
    env.mes.unlock("WO-SYN-1002")
    assert env.executor.release(failing_case.action, receipt).cause is RefusalCause.RECEIPT_ALREADY_USED
