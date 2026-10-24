"""The caller is not authoritative. The determination reads MES and approval-system state itself.

Scenarios from the brief:
  1. Caller claims releasable, authoritative state agrees -> expected determination.
  2. Caller claims releasable, authoritative state disagrees -> the claim must not cause ADMIT.
  3. Required authoritative state is missing -> DEFER, unless a higher-precedence verdict applies
     (precedence is HALT > REFER > DEFER > ADMIT).
  4. Authoritative state changes between two proposals -> the second determination reflects it.
  5. A receipt from earlier state cannot silently authorize a release after that state changed.
"""

from dataclasses import replace

import pytest

from wo_demo.executor import ExecutionOutcome, RefusalCause
from wo_demo.fixtures import get_case
from wo_demo.models import Approval, ApprovalScope, EvidenceBundle
from wo_demo.state_demo import run_state_demo
from wo_demo.verdicts import Verdict

RELEASED, REFUSED = ExecutionOutcome.RELEASED, ExecutionOutcome.REFUSED
WO = "WO-SYN-1001"


def _codes(d):
    return {r.code for r in d.reasons}


def _notes(d):
    return {n.code for n in d.notes}


def _supervisor_approval(env, wo=WO):
    return env.approvals.current(wo)[0][0]


def _propose(env, case):
    """The caller proposes with its claims. Returns the determination."""
    return env.determiner.determine(case.action, case.caller_evidence)


# --- 1. Caller claims releasable, authoritative state agrees -----------------

def test_agreeing_claim_gets_the_expected_admit_and_releases(env, admit_case):
    claims = admit_case.caller_evidence
    assert claims.status == "PLANNED" and all(v is True for v in claims.checks.values()) and claims.approvals

    d = _propose(env, admit_case)
    result = env.executor.release(admit_case.action, d.receipt)

    assert d.verdict is Verdict.ADMIT
    assert d.notes == ()  # every claim was confirmed by the systems of record
    assert result.outcome is RELEASED


def test_agreeing_claims_do_not_change_the_determination(env, admit_case):
    with_claims = env.determiner.determine(admit_case.action, admit_case.caller_evidence)
    without_claims = env.determiner.determine(admit_case.action)
    assert with_claims.verdict is without_claims.verdict is Verdict.ADMIT
    assert with_claims.reasons == without_claims.reasons
    assert with_claims.evidence_digest == without_claims.evidence_digest


def test_caller_claims_are_not_bound_into_the_receipt(env, admit_case):
    """Two different, non-conflicting sets of claims produce the same binding: only authority is bound."""
    a = env.determiner.determine(admit_case.action, EvidenceBundle(status="PLANNED"))
    b = env.determiner.determine(admit_case.action, EvidenceBundle(checks={"material_available": True}))
    assert a.receipt.evidence_digest == b.receipt.evidence_digest == env.sources.current(WO).digest()


# --- 2. Caller claims releasable, authoritative state disagrees -------------

def _quality_hold(env):
    env.mes.update_readiness(WO, quality_hold_clear=False)


def _material_not_staged(env):
    env.mes.update_readiness(WO, material_available=False)


def _on_hold(env):
    env.mes.set_status(WO, "ON_HOLD")


def _already_released(env):
    env.mes.set_status(WO, "RELEASED")


def _approval_revoked(env):
    env.approvals.record(WO, (replace(_supervisor_approval(env), status="REVOKED", revoked_reason="schedule change"),))


def _approval_narrowed(env):
    a = _supervisor_approval(env)
    env.approvals.record(WO, (replace(a, scope=replace(a.scope, max_quantity=300)),))


DISAGREEMENTS = {
    "quality_hold": (_quality_hold, {"CHECK_FAILED", "CLAIM_MISMATCH"}),
    "material_not_staged": (_material_not_staged, {"CHECK_FAILED", "CLAIM_MISMATCH"}),
    "status_on_hold": (_on_hold, {"WORK_ORDER_NOT_RELEASABLE", "CLAIM_MISMATCH"}),
    "status_released": (_already_released, {"WORK_ORDER_NOT_RELEASABLE", "CLAIM_MISMATCH"}),
    "approval_revoked": (_approval_revoked, {"APPROVAL_REVOKED", "CLAIM_MISMATCH"}),
    "approval_narrowed": (_approval_narrowed, {"SCOPE_MISMATCH", "CLAIM_MISMATCH"}),
}


@pytest.mark.parametrize("name", DISAGREEMENTS)
def test_claim_contradicted_by_authority_never_admits(env, admit_case, assert_mes_untouched, name):
    change, expected_codes = DISAGREEMENTS[name]
    change(env)
    env.mes.call_log.clear()
    untouched = env.mes.snapshot()

    d = _propose(env, admit_case)  # the caller still claims the work order is releasable
    result = env.executor.release(admit_case.action, d.receipt)

    assert d.verdict is Verdict.HALT
    assert expected_codes <= _codes(d)
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.VERDICT_NOT_ADMIT)
    assert env.mes.snapshot() == untouched and env.mes.call_log == []


def test_fixture_case_07_contradicted_claim_halts(env):
    case = get_case("07_halt_claim_contradicted_by_mes")
    d = _propose(env, case)
    assert d.verdict is Verdict.HALT
    assert {"CHECK_FAILED", "CLAIM_MISMATCH"} <= _codes(d)
    assert env.executor.release(case.action, d.receipt).outcome is REFUSED


def test_caller_cannot_create_an_approval(env, admit_case):
    """The approval system has none; the caller lists one. The caller's copy is not used."""
    env.approvals.record(WO, ())
    d = _propose(env, admit_case)
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)
    assert "CLAIM_NOT_USED" in _notes(d)


def test_caller_cannot_supply_quality_engineering_signoff(env, refer_case):
    """Safety-critical part: a QE approval that only the caller has heard of does not clear the referral."""
    a = refer_case.action
    invented = Approval("APR-SYN-9905", "QUALITY_ENGINEERING", "SYN-QE-99", "ACTIVE",
                        ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity))
    claims = replace(refer_case.caller_evidence, approvals=refer_case.caller_evidence.approvals + (invented,))

    d = env.determiner.determine(a, claims)

    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"
    assert "CLAIM_NOT_USED" in _notes(d)


def test_caller_claiming_wider_scope_than_the_approval_system_halts(env, admit_case):
    a = _supervisor_approval(env)
    claims = replace(admit_case.caller_evidence, approvals=(replace(a, scope=replace(a.scope, max_quantity=10_000)),))
    d = env.determiner.determine(admit_case.action, claims)
    assert d.verdict is Verdict.HALT
    assert "CLAIM_MISMATCH" in _codes(d)


def test_contradiction_in_the_cautious_direction_also_halts(env, admit_case):
    """Fail closed: a caller that thinks material isn't staged when the MES says it is doesn't get ADMIT either."""
    claims = replace(admit_case.caller_evidence, checks={**admit_case.caller_evidence.checks, "material_available": False})
    d = env.determiner.determine(admit_case.action, claims)
    assert d.verdict is Verdict.HALT
    assert "CLAIM_MISMATCH" in _codes(d)


@pytest.mark.parametrize("claimed", [1, "true", "True", 1.0], ids=repr)
def test_claims_are_compared_strictly(env, admit_case, claimed):
    claims = replace(admit_case.caller_evidence, checks={**admit_case.caller_evidence.checks, "quality_hold_clear": claimed})
    assert env.determiner.determine(admit_case.action, claims).verdict is Verdict.HALT


def test_malformed_caller_evidence_halts(env, admit_case):
    d = env.determiner.determine(admit_case.action, {"checks": {"quality_hold_clear": True}})
    assert d.verdict is Verdict.HALT
    assert "CLAIM_MALFORMED" in _codes(d)


# --- 3. Required authoritative state is missing -> DEFER unless higher precedence

def _readiness_without(env, name):
    readiness = dict(env.mes.lookup(WO).readiness)
    readiness.pop(name)
    env.mes.set_readiness(WO, readiness)


MISSING = {
    "material_reading_absent": lambda env: _readiness_without(env, "material_available"),
    "quality_reading_null": lambda env: env.mes.update_readiness(WO, quality_hold_clear=None),
    "no_readiness_at_all": lambda env: env.mes.set_readiness(WO, {}),
    "no_status": lambda env: env.mes.set_status(WO, None),
    "no_approvals_on_record": lambda env: env.approvals.record(WO, ()),
}


@pytest.mark.parametrize("name", MISSING)
def test_missing_authoritative_state_defers_even_when_the_caller_claims_it(env, admit_case, name):
    MISSING[name](env)
    d = _propose(env, admit_case)  # the caller claims every one of these is fine
    assert d.verdict is Verdict.DEFER
    assert "MISSING_EVIDENCE" in _codes(d)
    assert "CLAIM_NOT_USED" in _notes(d)
    assert env.executor.release(admit_case.action, d.receipt).cause is RefusalCause.VERDICT_NOT_ADMIT


def test_fixture_case_08_missing_reading_defers(env):
    case = get_case("08_defer_mes_state_missing")
    d = _propose(env, case)
    assert d.verdict is Verdict.DEFER
    assert "CLAIM_NOT_USED" in _notes(d)


@pytest.mark.parametrize("higher", ["approval_revoked", "quality_hold", "status_on_hold"])
def test_missing_state_plus_a_halt_condition_halts(env, admit_case, higher):
    MISSING["material_reading_absent"](env)
    DISAGREEMENTS[higher][0](env)
    d = env.determiner.determine(admit_case.action)
    assert d.verdict is Verdict.HALT
    assert "MISSING_EVIDENCE" in _codes(d)  # still reported


def test_a_referral_outranks_missing_state(env, refer_case):
    """REFER is higher than DEFER: missing MES state on a safety-critical part is still referred."""
    env.mes.update_readiness("WO-SYN-1005", material_available=None)
    d = env.determiner.determine(refer_case.action, refer_case.caller_evidence)
    assert d.verdict is Verdict.REFER
    assert d.referred_to == "QUALITY_ENGINEERING"
    assert {"MISSING_EVIDENCE", "AUTHORITY_REQUIRED"} <= _codes(d)


def test_unknown_work_order_is_not_missing_state(env, admit_case):
    """No such work order in the MES is a HALT (the target doesn't exist), not a DEFER."""
    d = env.determiner.determine(replace(admit_case.action, work_order_id="WO-SYN-9999"), admit_case.caller_evidence)
    assert d.verdict is Verdict.HALT
    assert "WORK_ORDER_UNKNOWN" in _codes(d)


# --- 4. Authoritative state changes between two proposals --------------------

def test_second_proposal_reflects_a_new_quality_hold(env, admit_case):
    first = _propose(env, admit_case)
    _quality_hold(env)
    second = _propose(env, admit_case)  # same proposal, same claims
    assert (first.verdict, second.verdict) == (Verdict.ADMIT, Verdict.HALT)
    assert second.authoritative["mes_version"] == first.authoritative["mes_version"] + 1
    assert first.evidence_digest != second.evidence_digest


def test_second_proposal_reflects_a_reading_that_arrived(env):
    case = get_case("08_defer_mes_state_missing")
    first = _propose(env, case)
    env.mes.update_readiness(case.action.work_order_id, material_available=True)
    second = _propose(env, case)
    assert (first.verdict, second.verdict) == (Verdict.DEFER, Verdict.ADMIT)
    assert env.executor.release(case.action, second.receipt).outcome is RELEASED


def test_second_proposal_reflects_a_revocation_and_a_reissue(env, admit_case):
    approval = _supervisor_approval(env)
    _approval_revoked(env)
    revoked = _propose(env, admit_case)
    env.approvals.record(WO, (replace(approval, approval_id="APR-SYN-5101"),))
    claims = replace(admit_case.caller_evidence, approvals=(replace(approval, approval_id="APR-SYN-5101"),))
    reissued = env.determiner.determine(admit_case.action, claims)
    assert (revoked.verdict, reissued.verdict) == (Verdict.HALT, Verdict.ADMIT)


# --- 5. A receipt from earlier state cannot authorize after the state changed

def _admit_receipt(env, case):
    d = _propose(env, case)
    assert d.verdict is Verdict.ADMIT
    return d.receipt


CHANGES_AFTER_ADMIT = {
    "quality_hold": _quality_hold,
    "status_on_hold": _on_hold,
    "approval_revoked": _approval_revoked,
    "approval_narrowed": _approval_narrowed,
}


@pytest.mark.parametrize("name", CHANGES_AFTER_ADMIT)
def test_earlier_admit_receipt_is_refused_after_the_state_changes(env, admit_case, name):
    receipt = _admit_receipt(env, admit_case)
    CHANGES_AFTER_ADMIT[name](env)
    untouched = env.mes.snapshot()

    result = env.executor.release(admit_case.action, receipt)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.EVIDENCE_NOT_CURRENT)
    assert env.mes.snapshot() == untouched and env.mes.call_log == []


def test_earlier_receipt_is_refused_even_after_the_state_changes_back(env, admit_case, assert_mes_untouched):
    receipt = _admit_receipt(env, admit_case)
    env.mes.update_readiness(WO, quality_hold_clear=False)
    env.mes.update_readiness(WO, quality_hold_clear=True)  # same values as when the receipt was issued
    assert env.sources.current(WO).evidence.checks == get_case("01_admit_release").caller_evidence.checks

    result = env.executor.release(admit_case.action, receipt)
    assert result.cause is RefusalCause.EVIDENCE_NOT_CURRENT


def test_earlier_receipt_is_refused_after_an_approval_is_revoked_and_restored(env, admit_case):
    receipt = _admit_receipt(env, admit_case)
    approval = _supervisor_approval(env)
    _approval_revoked(env)
    env.approvals.record(WO, (approval,))  # restored exactly
    assert env.executor.release(admit_case.action, receipt).cause is RefusalCause.EVIDENCE_NOT_CURRENT


def test_earlier_receipt_is_refused_after_the_work_order_facts_change(env, admit_case):
    receipt = _admit_receipt(env, admit_case)
    env.mes.amend(WO, part_revision="D")
    assert env.executor.release(admit_case.action, receipt).cause is RefusalCause.WORK_ORDER_CHANGED


def test_stale_receipt_stays_unusable_and_a_fresh_determination_works(env, admit_case):
    stale = _admit_receipt(env, admit_case)
    _quality_hold(env)
    env.mes.update_readiness(WO, quality_hold_clear=True)
    assert env.executor.release(admit_case.action, stale).cause is RefusalCause.EVIDENCE_NOT_CURRENT
    assert env.executor.release(admit_case.action, stale).cause is RefusalCause.EVIDENCE_NOT_CURRENT

    fresh = _admit_receipt(env, admit_case)
    assert env.executor.release(admit_case.action, fresh).outcome is RELEASED


def test_defer_receipt_stays_unusable_after_the_missing_state_arrives(env):
    case = get_case("08_defer_mes_state_missing")
    deferred = _propose(env, case).receipt
    env.mes.update_readiness(case.action.work_order_id, material_available=True)
    assert env.executor.release(case.action, deferred).cause is RefusalCause.VERDICT_NOT_ADMIT


def test_session_lock_is_execution_state_and_does_not_invalidate_a_receipt(env, failing_case):
    """WO-SYN-1002 is locked when decided; unlocking it is not a change to any determination input."""
    receipt = _admit_receipt(env, failing_case)
    version = env.mes.lookup("WO-SYN-1002").version
    env.mes.unlock("WO-SYN-1002")
    assert env.mes.lookup("WO-SYN-1002").version == version
    assert env.executor.release(failing_case.action, receipt).outcome is RELEASED


def test_replay_after_release_is_reported_as_already_used(env, admit_case):
    """The release itself changes MES status, so single use is checked before currency."""
    receipt = _admit_receipt(env, admit_case)
    assert env.executor.release(admit_case.action, receipt).outcome is RELEASED
    assert env.executor.release(admit_case.action, receipt).cause is RefusalCause.RECEIPT_ALREADY_USED


# --- the state-change demo ---------------------------------------------------

def test_state_demo_sequence():
    steps, env = run_state_demo()
    assert [(s.kind, s.result) for s in steps] == [
        ("decide", "ADMIT"),
        ("state", "quality_hold_clear = false"),
        ("decide", "HALT"),
        ("execute", "REFUSED EVIDENCE_NOT_CURRENT"),
        ("execute", "REFUSED VERDICT_NOT_ADMIT"),
        ("state", "quality_hold_clear = true"),
        ("execute", "REFUSED EVIDENCE_NOT_CURRENT"),
        ("decide", "ADMIT"),
        ("execute", "RELEASED"),
        ("decide", "DEFER"),
        ("state", "material_available = true"),
        ("execute", "REFUSED VERDICT_NOT_ADMIT"),
        ("decide", "ADMIT"),
        ("execute", "RELEASED"),
        ("decide", "ADMIT"),
        ("state", "quality_hold_clear = false"),
        ("execute", "FAILED STATE_CHANGED_BEFORE_RELEASE"),
        ("decide", "HALT"),
    ]
    assert len(env.mes.call_log) == 3  # two releases and the attempt the MES rejected
