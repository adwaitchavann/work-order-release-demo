"""MES records handed out to readers are read-only.

Review finding: the readiness dictionary inside a "read-only" record could be
edited through the reader without the version moving, and a release still went
through. Records now carry an immutable readiness mapping (deep-copied when the
record is built), and every MES read returns a fresh copy, so nothing a reader
does can reach the MES.
"""

import contextlib
import dataclasses

import pytest

from wo_demo.executor import ExecutionOutcome
from wo_demo.fixtures import get_case
from wo_demo.mes import SimulatedMES
from wo_demo.verdicts import Verdict

WO = "WO-SYN-1001"
HELD = "WO-SYN-1007"  # the MES has an active quality hold on this work order


def _attempt(edit):
    """Try an edit. An immutable record refusing it is an acceptable outcome; the assertions after decide."""
    with contextlib.suppress(TypeError, AttributeError):
        edit()


# --- the regression from the review ------------------------------------------

def test_editing_a_record_through_the_reader_cannot_clear_a_hold_and_the_release_fails(env):
    case = get_case("07_halt_claim_contradicted_by_mes")
    before = env.mes.lookup(HELD)
    assert before.readiness["quality_hold_clear"] is False

    record = env.mes.reader().lookup(HELD)
    _attempt(lambda: record.readiness.__setitem__("quality_hold_clear", True))
    _attempt(lambda: record.readiness.update(quality_hold_clear=True))

    d = env.determiner.determine(case.action)  # no caller claims: only the MES decides
    result = env.executor.release(case.action, d.receipt)

    assert d.verdict is Verdict.HALT
    assert "CHECK_FAILED" in {r.code for r in d.reasons}
    assert result.outcome is ExecutionOutcome.REFUSED
    assert env.mes.lookup(HELD) == before  # same readiness, same version
    assert env.mes.call_log == []


def test_forcing_an_edit_onto_a_returned_record_still_leaves_the_mes_unchanged(env):
    """A frozen dataclass can be bypassed with object.__setattr__. The reader hands out a copy, so it doesn't matter."""
    case = get_case("07_halt_claim_contradicted_by_mes")
    before = env.mes.lookup(HELD)
    record = env.mes.reader().lookup(HELD)
    object.__setattr__(record, "readiness", {"bom_revision_confirmed": True, "material_available": True,
                                             "quality_hold_clear": True})

    d = env.determiner.determine(case.action)
    assert d.verdict is Verdict.HALT
    assert env.executor.release(case.action, d.receipt).outcome is ExecutionOutcome.REFUSED
    assert env.mes.lookup(HELD) == before


# --- how it's enforced --------------------------------------------------------

def test_reader_records_refuse_readiness_edits(env):
    record = env.mes.reader().lookup(WO)
    with pytest.raises(TypeError):
        record.readiness["quality_hold_clear"] = False
    with pytest.raises(TypeError):
        del record.readiness["material_available"]
    with pytest.raises(AttributeError):
        record.readiness.update(quality_hold_clear=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "RELEASED"


def test_each_read_returns_a_fresh_copy(env):
    a, b = env.mes.reader().lookup(WO), env.mes.reader().lookup(WO)
    assert a == b
    assert a is not b
    assert a.readiness is not b.readiness


def test_nested_values_are_copied_too(env):
    env.mes.set_readiness(WO, {"bom_revision_confirmed": True, "material_available": True,
                               "quality_hold_clear": [True]})
    env.mes.reader().lookup(WO).readiness["quality_hold_clear"].append(False)
    assert env.mes.lookup(WO).readiness["quality_hold_clear"] == [True]


def test_a_mapping_handed_to_the_mes_is_copied(env):
    signals = {"bom_revision_confirmed": True, "material_available": True, "quality_hold_clear": False}
    env.mes.set_readiness(WO, signals)
    signals["quality_hold_clear"] = True  # the caller keeps its dict; the MES must not share it
    assert env.mes.lookup(WO).readiness["quality_hold_clear"] is False


def test_records_given_to_the_mes_are_copied(env):
    original = env.mes.lookup(WO)
    mes = SimulatedMES([original])
    object.__setattr__(original, "status", "RELEASED")
    object.__setattr__(original, "readiness", {})
    assert mes.lookup(WO).status == "PLANNED"
    assert mes.lookup(WO).readiness["quality_hold_clear"] is True


def test_authoritative_view_cannot_be_used_to_change_the_mes(env):
    view = env.sources.current(HELD)
    with pytest.raises(TypeError):
        view.record.readiness["quality_hold_clear"] = True
    _attempt(lambda: view.evidence.checks.__setitem__("quality_hold_clear", True))
    assert env.mes.lookup(HELD).readiness["quality_hold_clear"] is False
    assert env.sources.current(HELD).evidence.checks["quality_hold_clear"] is False


def test_snapshot_returns_copies(env):
    snap = env.mes.snapshot()
    with pytest.raises(TypeError):
        snap[HELD].readiness["quality_hold_clear"] = True
    snap.clear()
    assert env.mes.lookup(HELD).readiness["quality_hold_clear"] is False


def test_approvals_read_through_the_sources_are_immutable(env):
    approvals = env.sources.current(WO).evidence.approvals
    assert isinstance(approvals, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        approvals[0].status = "REVOKED"
    with pytest.raises(dataclasses.FrozenInstanceError):
        approvals[0].scope.max_quantity = 10_000
