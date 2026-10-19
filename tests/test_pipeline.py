"""End to end: each fixture case through all four stages, plus the CLI."""

import json

import pytest

from wo_demo.cli import main
from wo_demo.clock import FixedClock
from wo_demo.executor import ExecutionOutcome, RefusalCause
from wo_demo.fixtures import load_cases
from wo_demo.gate_demo import run_gate_demo
from wo_demo.pipeline import run_case

CASES = load_cases()


def test_fixture_set_covers_all_four_verdicts_and_a_failure():
    verdicts = {c.expected["verdict"] for c in CASES}
    outcomes = {c.expected["outcome"] for c in CASES}
    assert verdicts == {"ADMIT", "HALT", "DEFER", "REFER"}
    assert {"RELEASED", "FAILED", "REFUSED"} <= outcomes


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_case_runs_end_to_end_as_expected(case):
    run = run_case(case, FixedClock())
    exp = case.expected
    assert run.determination.verdict.value == exp["verdict"]
    assert run.effect.outcome.value == exp["outcome"]
    assert (run.effect.cause.value if run.effect.cause else None) == exp["cause"]
    assert run.effect.mes_error_code == exp.get("mes_error_code")
    assert run.effect.work_order_status == exp["work_order_status"]


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_only_admit_reaches_the_mes(case):
    run = run_case(case, FixedClock())
    expected_calls = 1 if case.expected["verdict"] == "ADMIT" else 0
    assert run.mes_calls == expected_calls
    if case.expected["verdict"] != "ADMIT":
        assert run.effect.work_order_status == run.status_before == "PLANNED"


def test_trace_has_the_four_stages_in_order_and_is_labeled_simulated():
    trace = run_case(CASES[0], FixedClock()).trace()
    assert [s["stage"] for s in trace["stages"]] == [
        "1_proposed_action", "2_evidence", "3_determination", "4_effect"]
    assert trace["stages"][2]["data"]["simulated"] is True
    assert trace["stages"][3]["data"]["simulated"] is True
    assert "verdict" not in trace["stages"][3]["data"]


def test_gate_demo_refuses_every_misuse():
    steps, env = run_gate_demo()
    got = [(s.result.outcome, s.result.cause) for s in steps]
    R, F, X = ExecutionOutcome.RELEASED, ExecutionOutcome.FAILED, ExecutionOutcome.REFUSED
    assert got == [
        (X, RefusalCause.ACTION_TYPE_INVALID),
        (X, RefusalCause.QUANTITY_INVALID),
        (X, RefusalCause.RECEIPT_MISSING),
        (X, RefusalCause.SIGNATURE_INVALID),
        (X, RefusalCause.SIGNATURE_INVALID),
        (X, RefusalCause.RECEIPT_EXPIRED),
        (X, RefusalCause.WORK_ORDER_MISMATCH),
        (X, RefusalCause.ACTION_MISMATCH),
        (X, RefusalCause.POLICY_VERSION_MISMATCH),
        (X, RefusalCause.VERDICT_NOT_ADMIT),
        (X, RefusalCause.EVIDENCE_NOT_CURRENT),
        (X, RefusalCause.WORK_ORDER_CHANGED),
        (R, None),
        (X, RefusalCause.RECEIPT_ALREADY_USED),
        (F, None),
        (X, RefusalCause.RECEIPT_ALREADY_USED),
        (R, None),
    ]
    assert len(env.mes.call_log) == 3


def test_cli_run_and_gate(capsys, tmp_path):
    out_file = tmp_path / "trace.json"
    assert main(["run", "--json", str(out_file)]) == 0
    assert main(["gate"]) == 0
    printed = capsys.readouterr().out
    assert "SIMULATED" in printed
    data = json.loads(out_file.read_text())
    assert data["simulated"] is True
    assert len(data["cases"]) == len(CASES)
