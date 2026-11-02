"""This demo releases whole work orders only.

Review finding: requesting 1 unit of a 400-unit work order was admitted and marked the
whole order RELEASED, with nothing recording how much had been released. Partial releases
are now rejected by the determiner (HALT) and by the executor (REFUSED), and the MES write
itself refuses any quantity other than the full planned quantity. A release records the
quantity released, which is always the full order.
"""

from dataclasses import replace

import pytest

from wo_demo.executor import ExecutionOutcome, RefusalCause
from wo_demo.mes import MESError
from wo_demo.verdicts import Verdict

WO = "WO-SYN-1001"  # planned for 400


# --- the regression from the review ------------------------------------------

def test_one_unit_of_a_400_unit_order_is_not_released(env, admit_case, assert_mes_untouched):
    one = replace(admit_case.action, quantity=1)

    d = env.determiner.determine(one, admit_case.caller_evidence)
    result = env.executor.release(one, d.receipt)

    assert d.verdict is Verdict.HALT
    assert "QUANTITY_INVALID" in {r.code for r in d.reasons}
    assert (result.outcome, result.cause) == (ExecutionOutcome.REFUSED, RefusalCause.QUANTITY_INVALID)
    assert env.mes.lookup(WO).status == "PLANNED"
    assert env.mes.lookup(WO).released_quantity is None
    assert_mes_untouched()


# --- each layer enforces it on its own ----------------------------------------

@pytest.mark.parametrize("qty", [1, 250, 399])
def test_determiner_halts_partial_releases(env, admit_case, qty):
    d = env.determiner.determine(replace(admit_case.action, quantity=qty), admit_case.caller_evidence)
    assert d.verdict is Verdict.HALT
    assert "QUANTITY_INVALID" in {r.code for r in d.reasons}


@pytest.mark.parametrize("qty", [1, 250, 399])
def test_executor_refuses_a_partial_release_even_with_a_valid_admit_receipt(env, admit_case, sign_receipt,
                                                                            assert_mes_untouched, qty):
    """Calling code that skips the determiner, or a determiner bug, still can't get a partial release through."""
    partial = replace(admit_case.action, quantity=qty)
    result = env.executor.release(partial, sign_receipt(partial))
    assert (result.outcome, result.cause) == (ExecutionOutcome.REFUSED, RefusalCause.QUANTITY_INVALID)
    assert_mes_untouched()


@pytest.mark.parametrize("qty", [1, 399, 401, 0, None, True, 400.0, "400"])
def test_mes_write_refuses_anything_but_the_full_quantity(env, qty):
    before = env.mes.lookup(WO)
    with pytest.raises(MESError) as e:
        env.mes.release(WO, qty, expected_version=before.version)
    assert e.value.code == "FULL_QUANTITY_REQUIRED"
    assert env.mes.lookup(WO) == before


def test_mes_checks_quantity_against_its_own_record_not_the_caller(env):
    env.mes.amend(WO, planned_quantity=300)
    current = env.mes.lookup(WO).version
    with pytest.raises(MESError) as e:
        env.mes.release(WO, 400, expected_version=current)
    assert e.value.code == "FULL_QUANTITY_REQUIRED"
    env.mes.release(WO, 300, expected_version=current)
    assert env.mes.lookup(WO).released_quantity == 300


# --- a full release records what was released ----------------------------------

def test_full_release_records_the_released_quantity(env, admit_case):
    d = env.determiner.determine(admit_case.action, admit_case.caller_evidence)
    result = env.executor.release(admit_case.action, d.receipt)

    record = env.mes.lookup(WO)
    assert d.verdict is Verdict.ADMIT
    assert result.outcome is ExecutionOutcome.RELEASED
    assert (record.status, record.released_quantity, record.planned_quantity) == ("RELEASED", 400, 400)
    assert env.mes.call_log[-1]["quantity"] == 400
    assert env.mes.history[-1][2] == "released 400"


def test_released_quantity_is_not_set_by_any_other_change(env):
    env.mes.update_readiness(WO, quality_hold_clear=False)
    env.mes.set_status(WO, "ON_HOLD")
    assert env.mes.lookup(WO).released_quantity is None
