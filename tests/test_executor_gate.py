"""The executor's own gate. These tests call the executor directly, as buggy calling code would."""

from dataclasses import fields, replace
from datetime import timedelta

import pytest

from wo_demo.executor import ExecutionOutcome, ExecutionResult, RefusalCause
from wo_demo.signing import ReceiptSigner
from wo_demo.verdicts import Verdict

REFUSED = ExecutionOutcome.REFUSED


def _admit(env, case):
    d = env.determiner.determine(case.action, case.evidence)
    assert d.verdict is Verdict.ADMIT
    return d.receipt


# --- HALT, DEFER and REFER can never release --------------------------------

@pytest.mark.parametrize("case_fixture,verdict", [
    ("halt_case", "HALT"),
    ("defer_case", "DEFER"),
    ("refer_case", "REFER"),
], ids=["HALT", "DEFER", "REFER"])
def test_non_admit_receipt_cannot_release(request, env, assert_mes_untouched, case_fixture, verdict):
    case = request.getfixturevalue(case_fixture)
    d = env.determiner.determine(case.action, case.evidence)
    assert d.verdict.value == verdict

    result = env.executor.release(case.action, d.receipt)

    assert result.outcome is REFUSED
    assert result.cause is RefusalCause.VERDICT_NOT_ADMIT
    assert result.work_order_status == "PLANNED"
    assert_mes_untouched()


@pytest.mark.parametrize("verdict", ["HALT", "DEFER", "REFER"])
def test_non_admit_receipt_cannot_release_even_with_a_valid_action(env, admit_case, assert_mes_untouched,
                                                                   sign_receipt, verdict):
    """A correctly signed and bound HALT/DEFER/REFER receipt for an otherwise releasable work order releases nothing."""
    receipt = sign_receipt(admit_case.action, verdict)
    result = env.executor.release(admit_case.action, receipt)
    assert result.cause is RefusalCause.VERDICT_NOT_ADMIT
    assert_mes_untouched()


# --- missing or invalid receipts --------------------------------------------

def test_missing_receipt_is_refused(env, admit_case, assert_mes_untouched):
    result = env.executor.release(admit_case.action, None)
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.RECEIPT_MISSING)
    assert_mes_untouched()


def test_object_that_is_not_a_receipt_is_refused(env, admit_case, assert_mes_untouched):
    result = env.executor.release(admit_case.action, {"verdict": "ADMIT"})
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.SIGNATURE_INVALID)
    assert_mes_untouched()


def test_halt_receipt_edited_to_admit_is_refused(env, halt_case, assert_mes_untouched):
    halt = env.determiner.determine(halt_case.action, halt_case.evidence).receipt
    tampered = replace(halt, verdict="ADMIT")

    result = env.executor.release(halt_case.action, tampered)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.SIGNATURE_INVALID)
    assert_mes_untouched()


@pytest.mark.parametrize("field,value", [
    ("work_order_id", "WO-SYN-1003"),
    ("action_digest", "sha256:" + "0" * 64),
    ("expires_at", "2099-01-01T00:00:00+00:00"),
    ("simulated", False),
    ("evidence_digest", "sha256:" + "1" * 64),
    ("policy_version", "SYN-REL-POLICY-v9"),
    ("policy_digest", "sha256:" + "2" * 64),
    ("facts_digest", "sha256:" + "3" * 64),
])
def test_any_edit_to_a_signed_field_is_refused(env, admit_case, assert_mes_untouched, field, value):
    receipt = replace(_admit(env, admit_case), **{field: value})
    result = env.executor.release(admit_case.action, receipt)
    assert result.cause is RefusalCause.SIGNATURE_INVALID
    assert_mes_untouched()


@pytest.mark.parametrize("key_id,secret", [
    ("rogue-key", b"rogue-secret"),          # key the executor has never heard of
    ("demo-key-1", b"wrong-secret-same-id"),  # right key id, wrong secret
], ids=["unknown_key", "wrong_secret"])
def test_receipt_not_signed_by_the_determiner_is_refused(env, admit_case, assert_mes_untouched, key_id, secret):
    now = env.clock.now()
    forged = ReceiptSigner(key_id, secret).sign(
        receipt_id="rcpt-forged",
        verdict="ADMIT",
        work_order_id=admit_case.action.work_order_id,
        action_digest=admit_case.action.digest(),
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    result = env.executor.release(admit_case.action, forged)
    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.SIGNATURE_INVALID)
    assert_mes_untouched()


# --- freshness --------------------------------------------------------------

def test_expired_receipt_is_refused(env, admit_case, assert_mes_untouched):
    receipt = _admit(env, admit_case)
    env.clock.advance(env.policy.receipt_ttl_seconds + 1)

    result = env.executor.release(admit_case.action, receipt)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.RECEIPT_EXPIRED)
    assert_mes_untouched()


def test_receipt_is_expired_at_exactly_its_expiry_time(env, admit_case, assert_mes_untouched):
    receipt = _admit(env, admit_case)
    env.clock.advance(env.policy.receipt_ttl_seconds)
    assert env.executor.release(admit_case.action, receipt).cause is RefusalCause.RECEIPT_EXPIRED
    assert_mes_untouched()


def test_receipt_is_still_fresh_one_second_before_expiry(env, admit_case):
    receipt = _admit(env, admit_case)
    env.clock.advance(env.policy.receipt_ttl_seconds - 1)
    assert env.executor.release(admit_case.action, receipt).outcome is ExecutionOutcome.RELEASED


# --- binding to the exact work order and action -----------------------------

def test_receipt_issued_for_a_different_work_order_is_refused(env, admit_case, halt_case, assert_mes_untouched):
    receipt_for_1001 = _admit(env, admit_case)
    assert halt_case.action.work_order_id != receipt_for_1001.work_order_id

    result = env.executor.release(halt_case.action, receipt_for_1001)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.WORK_ORDER_MISMATCH)
    assert result.work_order_id == halt_case.action.work_order_id
    assert_mes_untouched()


@pytest.mark.parametrize("change", [
    {"quantity": 300},  # still a valid quantity, so only the binding can catch it
    {"line": "LINE-3"},
    {"part_revision": "D"},
    {"agent_id": "SYN-OTHER-AGENT"},
], ids=lambda c: next(iter(c)))
def test_receipt_for_a_changed_action_on_the_same_work_order_is_refused(env, admit_case, assert_mes_untouched, change):
    receipt = _admit(env, admit_case)
    changed = replace(admit_case.action, **change)

    result = env.executor.release(changed, receipt)

    assert (result.outcome, result.cause) == (REFUSED, RefusalCause.ACTION_MISMATCH)
    assert_mes_untouched()


# --- ADMIT permits, it does not guarantee -----------------------------------

def test_admit_releases_the_work_order(env, admit_case):
    result = env.executor.release(admit_case.action, _admit(env, admit_case))
    assert result.outcome is ExecutionOutcome.RELEASED
    assert result.cause is None
    assert env.mes.status(admit_case.action.work_order_id) == "RELEASED"
    assert len(env.mes.call_log) == 1


def test_admit_followed_by_mes_failure_reports_failed_and_does_not_release(env, failing_case):
    result = env.executor.release(failing_case.action, _admit(env, failing_case))

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.cause is None
    assert result.mes_error_code == "WORK_ORDER_LOCKED"
    assert result.work_order_status == "PLANNED"
    assert env.mes.status(failing_case.action.work_order_id) == "PLANNED"
    assert len(env.mes.call_log) == 1  # it was attempted


# --- single use -------------------------------------------------------------

def test_replaying_a_receipt_after_a_successful_release_is_refused(env, admit_case):
    receipt = _admit(env, admit_case)
    assert env.executor.release(admit_case.action, receipt).outcome is ExecutionOutcome.RELEASED

    replay = env.executor.release(admit_case.action, receipt)

    assert (replay.outcome, replay.cause) == (REFUSED, RefusalCause.RECEIPT_ALREADY_USED)
    assert len(env.mes.call_log) == 1  # the replay never reached the MES


def test_replaying_a_receipt_after_a_failed_release_is_refused(env, failing_case):
    wo = failing_case.action.work_order_id
    receipt = _admit(env, failing_case)
    assert env.executor.release(failing_case.action, receipt).outcome is ExecutionOutcome.FAILED

    env.mes.unlock(wo)  # the cause of the failure has cleared
    replay = env.executor.release(failing_case.action, receipt)

    assert (replay.outcome, replay.cause) == (REFUSED, RefusalCause.RECEIPT_ALREADY_USED)
    assert env.mes.status(wo) == "PLANNED"
    assert len(env.mes.call_log) == 1


def test_retry_after_a_failed_release_needs_a_new_determination(env, failing_case):
    wo = failing_case.action.work_order_id
    first = _admit(env, failing_case)
    env.executor.release(failing_case.action, first)
    env.mes.unlock(wo)

    second = _admit(env, failing_case)
    assert second.receipt_id != first.receipt_id
    result = env.executor.release(failing_case.action, second)

    assert result.outcome is ExecutionOutcome.RELEASED
    assert env.mes.status(wo) == "RELEASED"


def test_a_refused_presentation_does_not_spend_the_receipt(env, admit_case, halt_case):
    """Consumption happens on a release attempt, not on a refusal."""
    receipt = _admit(env, admit_case)
    assert env.executor.release(halt_case.action, receipt).cause is RefusalCause.WORK_ORDER_MISMATCH
    assert env.executor.release(admit_case.action, receipt).outcome is ExecutionOutcome.RELEASED


# --- the executor never produces a verdict ----------------------------------

def test_execution_result_carries_no_verdict():
    names = {f.name for f in fields(ExecutionResult)}
    assert not any("verdict" in n for n in names)


def test_every_refusal_states_a_cause(env, admit_case, halt_case):
    results = [
        env.executor.release(admit_case.action, None),
        env.executor.release(halt_case.action, env.determiner.determine(halt_case.action, halt_case.evidence).receipt),
        env.executor.release(halt_case.action, _admit(env, admit_case)),
    ]
    for r in results:
        assert r.outcome is REFUSED
        assert isinstance(r.cause, RefusalCause)
        assert r.detail
        assert r.outcome.value not in {v.value for v in Verdict}
