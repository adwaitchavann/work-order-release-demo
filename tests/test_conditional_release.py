"""Conditional release at the MES write boundary.

The race from the review: between the executor's final state check and the MES
write, QA places a hold. The release must not go ahead against the earlier state.
The version check, the active-hold check and the write happen atomically inside
the MES. Leases and cross-system approval coordination are out of scope, so an
approval revoked in the same window is documented here as unresolved.
"""

import threading
from dataclasses import replace

import pytest

from wo_demo.executor import ExecutionOutcome, RefusalCause
from wo_demo.mes import MESError
from wo_demo.verdicts import Verdict

WO = "WO-SYN-1001"
FAILED, RELEASED = ExecutionOutcome.FAILED, ExecutionOutcome.RELEASED


def _admit(env, case):
    d = env.determiner.determine(case.action, case.caller_evidence)
    assert d.verdict is Verdict.ADMIT
    return d


def _once(fn):
    """A hook that runs fn only the first time it fires."""
    fired = []

    def hook(work_order_id):
        if not fired:
            fired.append(work_order_id)
            fn(work_order_id)

    return hook


def _changes(env, wo=WO):
    return [what for w, _, what in env.mes.history if w == wo]


# --- the race ----------------------------------------------------------------

def test_hold_placed_between_the_executors_check_and_the_mes_write_blocks_the_release(env, admit_case):
    d = _admit(env, admit_case)
    env.mes.before_release_hook = _once(lambda wo: env.mes.update_readiness(wo, quality_hold_clear=False))

    result = env.executor.release(admit_case.action, d.receipt)

    assert result.outcome is FAILED
    assert result.mes_error_code == "STATE_CHANGED_BEFORE_RELEASE"
    assert result.work_order_status == "PLANNED"
    assert env.mes.status(WO) == "PLANNED"
    assert len(env.mes.call_log) == 1                 # the attempt reached the MES
    assert "released" not in _changes(env)            # but only the hold was applied


def test_executor_makes_the_release_conditional_on_the_version_it_verified(env, admit_case):
    d = _admit(env, admit_case)
    env.executor.release(admit_case.action, d.receipt)
    assert env.mes.call_log[-1]["expected_version"] == d.authoritative["mes_version"]


def test_after_the_race_the_receipt_is_spent_and_a_new_determination_halts(env, admit_case):
    d = _admit(env, admit_case)
    env.mes.before_release_hook = _once(lambda wo: env.mes.update_readiness(wo, quality_hold_clear=False))
    assert env.executor.release(admit_case.action, d.receipt).outcome is FAILED

    assert env.executor.release(admit_case.action, d.receipt).cause is RefusalCause.RECEIPT_ALREADY_USED
    again = env.determiner.determine(admit_case.action, admit_case.caller_evidence)
    assert again.verdict is Verdict.HALT
    assert "CHECK_FAILED" in {r.code for r in again.reasons}


@pytest.mark.parametrize("change", [
    lambda mes: mes.update_readiness(WO, material_available=False),
    lambda mes: mes.update_readiness(WO, bom_revision_confirmed=None),
    lambda mes: mes.set_status(WO, "ON_HOLD"),
    lambda mes: mes.amend(WO, part_revision="D"),
], ids=["material_pulled", "bom_reading_cleared", "status_on_hold", "revision_changed"])
def test_any_mes_change_in_the_window_blocks_the_release(env, admit_case, change):
    d = _admit(env, admit_case)
    env.mes.before_release_hook = _once(lambda wo: change(env.mes))
    result = env.executor.release(admit_case.action, d.receipt)
    assert (result.outcome, result.mes_error_code) == (FAILED, "STATE_CHANGED_BEFORE_RELEASE")
    assert env.mes.status(WO) != "RELEASED"


def test_session_lock_released_in_the_window_does_not_block(env, failing_case):
    """The session lock is execution state, not a determination input, so it doesn't move the version."""
    d = _admit(env, failing_case)
    env.mes.before_release_hook = _once(lambda wo: env.mes.unlock(wo))
    assert env.executor.release(failing_case.action, d.receipt).outcome is RELEASED


# --- the active-hold check, independent of the version -----------------------

def test_hold_blocks_the_release_even_if_the_version_did_not_move(env, admit_case):
    """Simulates a version-tracking bug: the hold lands without a version bump. The MES still refuses."""
    d = _admit(env, admit_case)

    def buggy_hold(wo):
        record = env.mes._orders[wo]
        env.mes._orders[wo] = replace(record, readiness={**record.readiness, "quality_hold_clear": False})

    env.mes.before_release_hook = _once(buggy_hold)
    result = env.executor.release(admit_case.action, d.receipt)
    assert (result.outcome, result.mes_error_code) == (FAILED, "QUALITY_HOLD_ACTIVE")
    assert env.mes.status(WO) == "PLANNED"


@pytest.mark.parametrize("value", [False, None, "CLEAR", 1], ids=repr)
def test_mes_refuses_while_a_hold_is_active_or_unconfirmed(env, value):
    env.mes.update_readiness(WO, quality_hold_clear=value)
    with pytest.raises(MESError) as e:
        env.mes.release(WO, 400, expected_version=env.mes.lookup(WO).version)
    assert e.value.code == "QUALITY_HOLD_ACTIVE"
    assert env.mes.status(WO) == "PLANNED"


def test_mes_refuses_an_unconditional_release(env):
    with pytest.raises(MESError) as e:
        env.mes.release(WO, 400)
    assert e.value.code == "PRECONDITION_REQUIRED"
    assert env.mes.status(WO) == "PLANNED"


def test_mes_refuses_a_stale_version(env):
    stale = env.mes.lookup(WO).version
    env.mes.update_readiness(WO, quality_hold_clear=True)  # any change moves the version
    with pytest.raises(MESError) as e:
        env.mes.release(WO, 400, expected_version=stale)
    assert e.value.code == "STATE_CHANGED_BEFORE_RELEASE"


# --- atomicity ---------------------------------------------------------------

def test_a_hold_arriving_during_the_mes_check_and_write_waits_for_the_write(env, admit_case):
    """QA tries to place a hold while the MES is between its checks and its write. It has to wait, so the
    release commits first and the hold then lands on a released order. Without the critical section the
    hold would slip in between and the release would be applied against state that no longer holds."""
    d = _admit(env, admit_case)
    qa = threading.Thread(target=lambda: env.mes.update_readiness(WO, quality_hold_clear=False))
    waited = []

    def inside(wo):
        qa.start()
        qa.join(timeout=0.2)
        waited.append(qa.is_alive())  # still blocked on the MES critical section

    env.mes.inside_release_hook = inside
    result = env.executor.release(admit_case.action, d.receipt)
    qa.join(timeout=5)

    assert waited == [True]
    assert result.outcome is RELEASED
    assert _changes(env)[-2:] == ["released", "readiness {'quality_hold_clear': False}"]
    assert env.mes.status(WO) == "RELEASED"
    assert env.mes.lookup(WO).readiness["quality_hold_clear"] is False


# --- documented as unresolved ------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "UNRESOLVED: approvals live in a separate system. The MES conditional write can't see an approval "
    "revoked after the executor's last check, so the release still goes ahead. Leases and cross-system "
    "coordination are out of scope for this iteration. If this starts passing, update the docs."))
def test_approval_revoked_in_the_same_window_is_not_yet_caught(env, admit_case):
    d = _admit(env, admit_case)

    def revoke(wo):
        approvals, _ = env.approvals.current(wo)
        env.approvals.record(wo, tuple(replace(a, status="REVOKED") for a in approvals))

    env.mes.before_release_hook = _once(revoke)
    result = env.executor.release(admit_case.action, d.receipt)
    assert result.outcome is not RELEASED  # the desired behaviour; today it is RELEASED
