"""Command line entry point.

    python -m wo_demo list
    python -m wo_demo run [--case CASE_ID] [--json PATH]
    python -m wo_demo gate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .executor import ExecutionOutcome
from .fixtures import get_case, load_cases, load_policy
from .gate_demo import run_gate_demo
from .pipeline import CaseRun, run_case
from .receipts import SIGNATURE_ALGORITHM

BANNER = "SIMULATED: mock determinations, demo HMAC key, in-memory MES. Synthetic data only."
RULE = "-" * 78


def _yn(v) -> str:
    if v is True:
        return "pass"
    if v is False:
        return "FAIL"
    if v is None:
        return "MISSING"
    return f"INVALID({v!r})"


def render(run: CaseRun) -> str:
    c, d, e = run.case, run.determination, run.effect
    a = c.action
    out = [RULE, f"Case {c.case_id}: {c.title}", RULE]

    out.append("[1] PROPOSED ACTION")
    out.append(f"    {a.agent_id} proposes {a.action_type} {a.work_order_id}")
    out.append(f"    plant {a.plant}, {a.line}, part {a.part_number} rev {a.part_revision}, qty {a.quantity}")
    out.append(f"    action digest {a.digest()[:23]}...")

    out.append("[2] EVIDENCE")
    f = run.record_facts
    if f:
        out.append(f"    MES record (source of facts): {f['line']}, part {f['part_number']} rev {f['part_revision']},"
                   f" planned {f['planned_quantity']}")
    else:
        out.append("    MES record: none")
    if not c.evidence.approvals:
        out.append("    approvals: none")
    for ap in c.evidence.approvals:
        s = ap.scope
        extra = f", supersedes {ap.supersedes}" if ap.supersedes else ""
        out.append(f"    approval {ap.approval_id}  {ap.approver_role}  {ap.status}"
                   f"  (scope {s.work_order_id}, {s.line}, {s.part_number} rev {s.part_revision},"
                   f" max {s.max_quantity}{extra})")
    checks = {name: c.evidence.checks.get(name) for name in load_policy().required_checks}
    out.append("    checks: " + ", ".join(f"{k}={_yn(v)}" for k, v in checks.items()))

    out.append("[3] DETERMINATION  [SIMULATED - mock determiner, not a real Coherence determination]")
    out.append(f"    verdict: {d.verdict.value}" + (f"  (referred to {d.referred_to})" if d.referred_to else ""))
    for r in d.reasons:
        out.append(f"      - {r}")
    out.append(f"    receipt {d.receipt.receipt_id[:17]}... bound to {d.receipt.work_order_id}, "
               f"expires {d.receipt.expires_at[11:19]} UTC, {SIGNATURE_ALGORITHM}")
    out.append(f"    also bound: policy {d.receipt.policy_version}, evidence {d.receipt.evidence_digest[:19]}...,"
               f" MES facts {(d.receipt.facts_digest or 'none')[:19]}...")

    out.append("[4] EFFECT  [SIMULATED MES]")
    line = f"    outcome: {e.outcome.value}"
    if e.cause:
        line += f"  cause: {e.cause.value}"
    if e.mes_error_code:
        line += f"  mes error: {e.mes_error_code}"
    out.append(line)
    out.append(f"    {e.detail}")
    out.append(f"    work order {e.work_order_id}: {run.status_before} -> {e.work_order_status}"
               f"   (MES calls: {run.mes_calls})")
    if e.outcome is ExecutionOutcome.FAILED:
        out.append("    note: ADMIT permitted the attempt; the attempt itself failed. Receipt is now spent.")
    return "\n".join(out)


def cmd_list(_args) -> int:
    for c in load_cases():
        print(f"{c.case_id:32} {c.title}")
    return 0


def cmd_run(args) -> int:
    cases = load_cases() if args.case in (None, "all") else [get_case(args.case)]
    print(BANNER)
    runs = [run_case(c) for c in cases]
    for r in runs:
        print(render(r))
    print(RULE)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"simulated": True, "cases": [r.trace() for r in runs]}, indent=2))
        print(f"Trace written to {path}")
    return 0


def cmd_gate(_args) -> int:
    steps, env = run_gate_demo()
    print(BANNER)
    print(RULE)
    print("Executor gate: what happens when the calling code gets it wrong")
    print(RULE)
    for i, s in enumerate(steps, 1):
        r = s.result
        verdictless = r.cause.value if r.cause else (r.mes_error_code or "")
        print(f"{i:>2}. {s.label:58} {r.outcome.value:8} {verdictless}")
    print(RULE)
    print(f"MES release calls: {len(env.mes.call_log)} (only the steps that passed every check)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wo_demo", description=BANNER)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the synthetic cases").set_defaults(func=cmd_list)
    r = sub.add_parser("run", help="run cases through the four stages")
    r.add_argument("--case", default="all", help="case id, or 'all' (default)")
    r.add_argument("--json", metavar="PATH", help="also write a JSON trace to PATH")
    r.set_defaults(func=cmd_run)
    sub.add_parser("gate", help="show the executor refusing misuse").set_defaults(func=cmd_gate)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
