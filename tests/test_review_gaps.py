"""Regression tests from Monica's review of the work-order release demo (Oct 2026).

Each test below FAILS against the submitted code and should pass once the gap is fixed.
Drop into tests/ and run: python -m pytest tests/test_review_gaps.py -v
"""

from dataclasses import replace

import pytest

from wo_demo.models import Approval, ApprovalScope
from wo_demo.verdicts import Verdict


# 1. Fail-open on non-boolean check values. A check that is not exactly True/False/None
#    currently produces no finding at all, so the action is ADMITTED and RELEASED.
@pytest.mark.parametrize("value", [0, 1, "false", "FAIL", "yes"])
def test_non_boolean_check_value_never_admits(env, admit_case, assert_mes_untouched, value):
    evidence = replace(admit_case.evidence, checks={**admit_case.evidence.checks, "quality_hold_clear": value})
    d = env.determiner.determine(admit_case.action, evidence)
    assert d.verdict is not Verdict.ADMIT
    env.executor.release(admit_case.action, d.receipt)
    assert_mes_untouched()


# 2. Safety-critical status is taken from the agent's proposal, not the system of record.
#    WO-SYN-1005 is PN-SYN-7730 in the MES; proposing it as PN-SYN-2210 skips the REFER.
def test_proposal_cannot_relabel_a_safety_critical_work_order(env, refer_case):
    a = replace(refer_case.action, part_number="PN-SYN-2210", part_revision="C")
    sup = Approval("APR-X", "PRODUCTION_SUPERVISOR", "SYN-SUP-11", "ACTIVE",
                   ApprovalScope(a.work_order_id, a.line, a.part_number, a.part_revision, a.quantity))
    d = env.determiner.determine(a, replace(refer_case.evidence, approvals=(sup,)))
    assert d.verdict is not Verdict.ADMIT


# 3. Quantity is never sanity-checked (and planned_quantity in work_orders.json is never read).
@pytest.mark.parametrize("qty", [0, -50, 401])
def test_quantity_must_be_positive_and_within_plan(env, admit_case, qty):
    a = replace(admit_case.action, quantity=qty)
    sup = replace(admit_case.evidence.approvals[0],
                  scope=replace(admit_case.evidence.approvals[0].scope, max_quantity=10_000))
    d = env.determiner.determine(a, replace(admit_case.evidence, approvals=(sup,)))
    assert d.verdict is not Verdict.ADMIT


# 4. Neither the determiner nor the executor checks action_type, so an ADMITted
#    SCRAP_WORK_ORDER receipt drives a release.
def test_executor_only_performs_release_actions(env, admit_case, assert_mes_untouched):
    a = replace(admit_case.action, action_type="SCRAP_WORK_ORDER")
    d = env.determiner.determine(a, admit_case.evidence)
    env.executor.release(a, d.receipt)
    assert_mes_untouched()


# 5. An old revoked approval blocks a newer active approval forever (design call; decide and document).
def test_reissued_approval_after_revocation_admits(env, admit_case):
    active = admit_case.evidence.approvals[0]
    old = replace(active, approval_id="APR-OLD", status="REVOKED", revoked_reason="superseded")
    d = env.determiner.determine(admit_case.action, replace(admit_case.evidence, approvals=(old, active)))
    assert d.verdict is Verdict.ADMIT
