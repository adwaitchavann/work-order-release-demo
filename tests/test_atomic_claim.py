"""Receipt consumption is an atomic check-and-claim.

Review finding: the "not used" check and marking the receipt used were separate
steps, so two concurrent requests carrying the same receipt could both pass the
check and both reach the MES. The executor now claims the receipt in one atomic
check-and-set immediately before the MES call, so exactly one request gets past it.
"""

import threading

import pytest

from wo_demo.executor import ConsumedReceipts, ExecutionOutcome, RefusalCause
from wo_demo.verdicts import Verdict

WO = "WO-SYN-1001"


class _AllChecksDoneTogether:
    """Wraps the executor's authoritative source. Every request blocks at its last check (the authoritative
    re-read) until all of them have reached it, so all of them have already passed the 'not used' pre-check
    before any of them can claim the receipt."""

    def __init__(self, inner, parties):
        self._inner = inner
        self._barrier = threading.Barrier(parties, timeout=5)

    def current(self, work_order_id):
        view = self._inner.current(work_order_id)
        self._barrier.wait()
        return view


def _race(env, action, receipt, n):
    env.executor._evidence = _AllChecksDoneTogether(env.sources, n)
    results = [None] * n

    def present(i):
        results[i] = env.executor.release(action, receipt)

    threads = [threading.Thread(target=present, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(r is not None for r in results), "a request did not finish"
    return results


def _admit(env, case):
    d = env.determiner.determine(case.action, case.caller_evidence)
    assert d.verdict is Verdict.ADMIT
    return d.receipt


@pytest.mark.parametrize("n", [2, 8])
def test_concurrent_requests_with_the_same_receipt_release_exactly_once(env, admit_case, n):
    results = _race(env, admit_case.action, _admit(env, admit_case), n)

    assert [r.outcome for r in results].count(ExecutionOutcome.RELEASED) == 1
    losers = [r for r in results if r.outcome is not ExecutionOutcome.RELEASED]
    assert [(r.outcome, r.cause) for r in losers] == [(ExecutionOutcome.REFUSED, RefusalCause.RECEIPT_ALREADY_USED)] * (n - 1)
    assert len(env.mes.call_log) == 1  # only the winner reached the MES
    assert env.mes.status(WO) == "RELEASED"


def test_concurrent_requests_on_a_failing_release_attempt_it_exactly_once(env, failing_case):
    """WO-SYN-1002 is locked, so the one attempt fails; the other request must not get a second attempt."""
    results = _race(env, failing_case.action, _admit(env, failing_case), 2)
    assert sorted(r.outcome.value for r in results) == ["FAILED", "REFUSED"]
    assert [r.cause for r in results if r.outcome is ExecutionOutcome.REFUSED] == [RefusalCause.RECEIPT_ALREADY_USED]
    assert len(env.mes.call_log) == 1


class _WinnerFinishesFirst:
    """While the first request is at its authoritative re-read (it has already passed the 'not used' pre-check),
    a second request with the same receipt runs to completion."""

    def __init__(self, inner, run_winner):
        self._inner = inner
        self._run_winner = run_winner
        self.winner = None

    def current(self, work_order_id):
        if self._run_winner is not None:
            run, self._run_winner = self._run_winner, None
            self.winner = run()
        return self._inner.current(work_order_id)


def test_a_duplicate_that_passed_the_pre_check_before_the_winner_released_is_still_refused(env, admit_case):
    receipt = _admit(env, admit_case)
    source = _WinnerFinishesFirst(env.sources, lambda: env.executor.release(admit_case.action, receipt))
    env.executor._evidence = source

    late = env.executor.release(admit_case.action, receipt)

    assert source.winner.outcome is ExecutionOutcome.RELEASED
    assert (late.outcome, late.cause) == (ExecutionOutcome.REFUSED, RefusalCause.EVIDENCE_NOT_CURRENT)
    assert len(env.mes.call_log) == 1


def test_claim_is_a_single_check_and_set():
    ledger = ConsumedReceipts()
    assert ledger.claim("rcpt-1") is True
    assert ledger.claim("rcpt-1") is False
    assert "rcpt-1" in ledger
    assert ledger.claim("rcpt-2") is True


def test_claim_under_contention_has_exactly_one_winner():
    ledger = ConsumedReceipts()
    start = threading.Barrier(16, timeout=5)
    wins = []

    def contend():
        start.wait()
        wins.append(ledger.claim("rcpt-x"))

    threads = [threading.Thread(target=contend) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(wins) == 16
    assert wins.count(True) == 1


def test_a_refusal_after_the_pre_check_does_not_claim_the_receipt(env, admit_case):
    receipt = _admit(env, admit_case)
    env.mes.update_readiness(WO, quality_hold_clear=False)  # makes the receipt stale

    result = env.executor.release(admit_case.action, receipt)

    assert result.cause is RefusalCause.EVIDENCE_NOT_CURRENT
    assert receipt.receipt_id not in env.executor._consumed
    assert env.mes.call_log == []
