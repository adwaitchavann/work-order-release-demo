"""Loads the synthetic fixtures in ../fixtures. No customer data is used anywhere."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import EvidenceBundle, Policy, ProposedAction

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    description: str
    action: ProposedAction
    evidence: EvidenceBundle
    expected: dict[str, Any]


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_policy() -> Policy:
    return Policy.from_dict(_read(FIXTURES_DIR / "policy.json"))


def load_work_orders() -> dict[str, Any]:
    return _read(FIXTURES_DIR / "work_orders.json")


def load_case(path: Path) -> Case:
    d = _read(path)
    return Case(
        case_id=d["case_id"],
        title=d["title"],
        description=d["description"],
        action=ProposedAction.from_dict(d["proposed_action"]),
        evidence=EvidenceBundle.from_dict(d["evidence"]),
        expected=d["expected"],
    )


def load_cases() -> list[Case]:
    return [load_case(p) for p in sorted((FIXTURES_DIR / "cases").glob("*.json"))]


def get_case(case_id: str) -> Case:
    for case in load_cases():
        if case.case_id == case_id:
            return case
    raise KeyError(f"no case {case_id!r}")
