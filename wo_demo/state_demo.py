"""Authoritative-state demo: what happens when the systems of record change between proposals.

Shows that each determination reflects the MES and approval system as they are at
that moment, whatever the caller claims; that a receipt issued from earlier state
cannot authorize a release after that state has changed, even if it later changes
back; and that a hold landing between the executor's last check and the MES write
makes the conditional release fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import FixedClock
from .env import DemoEnvironment
from .fixtures import get_case


@dataclass(frozen=True)
class StateStep:
    kind: str        # "state" (a system of record changed), "decide" or "execute"
    label: str
    result: str      # verdict, outcome and cause, or the new state
    mes_version: int
    approvals_version: int
    codes: tuple[str, ...] = ()


def run_state_demo() -> tuple[list[StateStep], DemoEnvironment]:
    env = DemoEnvironment.build(FixedClock())
    steps: list[StateStep] = []

    def versions(wo):
        view = env.sources.current(wo)
        return view.record.version, view.approvals_version

    def changed(wo, label, what):
        steps.append(StateStep("state", label, what, *versions(wo)))

    def decide(case, label):
        mv, av = versions(case.action.work_order_id)
        d = env.determiner.determine(case.action, case.caller_evidence)
        codes = tuple(r.code for r in d.reasons) + tuple(r.code for r in d.notes)
        steps.append(StateStep("decide", label, d.verdict.value, mv, av, codes))
        return d.receipt

    def execute(case, receipt, label):
        mv, av = versions(case.action.work_order_id)
        r = env.executor.release(case.action, receipt)
        result = r.outcome.value + (f" {r.cause.value}" if r.cause else f" {r.mes_error_code}" if r.mes_error_code else "")
        steps.append(StateStep("execute", label, result, mv, av))

    # A. The MES changes between two identical proposals (the caller's claims never change).
    ok = get_case("01_admit_release")
    wo = ok.action.work_order_id
    r1 = decide(ok, "Proposal 1: caller says releasable, MES agrees")
    env.mes.update_readiness(wo, quality_hold_clear=False)
    changed(wo, "QA places a quality hold in the MES", "quality_hold_clear = false")
    r2 = decide(ok, "Proposal 2: same proposal and claims")
    execute(ok, r1, "Receipt from proposal 1 presented")
    execute(ok, r2, "Receipt from proposal 2 presented")
    env.mes.update_readiness(wo, quality_hold_clear=True)
    changed(wo, "QA clears the hold (same values as at proposal 1)", "quality_hold_clear = true")
    execute(ok, r1, "Receipt from proposal 1 presented again")
    r3 = decide(ok, "Proposal 3: same proposal and claims")
    execute(ok, r3, "Receipt from proposal 3 presented")

    # B. Missing MES state arrives between two proposals.
    missing = get_case("08_defer_mes_state_missing")
    wo8 = missing.action.work_order_id
    r4 = decide(missing, "Proposal 4: caller says material staged; MES has no reading")
    env.mes.update_readiness(wo8, material_available=True)
    changed(wo8, "MES reports material staged", "material_available = true")
    execute(missing, r4, "Receipt from proposal 4 presented")
    r5 = decide(missing, "Proposal 5: same proposal and claims")
    execute(missing, r5, "Receipt from proposal 5 presented")

    # C. The race: QA places a hold after the executor's last check but before the MES write.
    race = get_case("06_admit_reissued_approval")
    wo6 = race.action.work_order_id
    r6 = decide(race, "Proposal 6: caller says releasable, MES agrees")

    def qa_hold_mid_release(work_order_id):
        env.mes.before_release_hook = None
        env.mes.update_readiness(work_order_id, quality_hold_clear=False)
        changed(work_order_id, "QA hold lands after executor's check, before MES write", "quality_hold_clear = false")

    env.mes.before_release_hook = qa_hold_mid_release
    execute(race, r6, "Receipt from proposal 6 presented (conditional release)")
    decide(race, "Proposal 7: same proposal and claims")

    return steps, env
