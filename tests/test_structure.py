"""Structural guarantees: deciding and doing stay separate, and the executor cannot sign."""

import ast
from pathlib import Path

import wo_demo
from wo_demo.executor import ExecutionOutcome, RefusalCause
from wo_demo.verdicts import Verdict
from wo_demo.verification import ReceiptVerifier

PKG = Path(wo_demo.__file__).resolve().parent


def _imports(module_file: str) -> set[str]:
    tree = ast.parse((PKG / module_file).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[-1])
    return names


def test_verdict_definitions_are_exactly_the_four_core_verdicts():
    assert [v.value for v in Verdict] == ["ADMIT", "HALT", "DEFER", "REFER"]


def test_execution_outcomes_and_refusal_causes_are_not_verdicts():
    verdicts = {v.value for v in Verdict}
    assert not verdicts & {o.value for o in ExecutionOutcome}
    assert not verdicts & {c.value for c in RefusalCause}


def test_executor_does_not_import_the_determiner_the_signer_or_the_key():
    assert not _imports("executor.py") & {"determiner", "signing", "demo_keys", "env"}


def test_verifier_does_not_import_the_signer_or_the_key():
    assert not _imports("verification.py") & {"signing", "demo_keys"}


def test_verifier_exposes_no_way_to_sign():
    public = {n for n in dir(ReceiptVerifier) if not n.startswith("_")}
    assert public == {"check_signature"}


def test_determiner_does_not_import_the_executor_or_the_mes():
    assert not _imports("determiner.py") & {"executor", "mes", "evidence_registry"}


def test_determiner_is_given_a_read_only_view_of_the_mes(env):
    reader = env.determiner._work_orders
    assert {n for n in dir(reader) if not n.startswith("_")} == {"lookup"}


def test_gitignore_excludes_os_and_notebook_clutter():
    ignored = (PKG.parent / ".gitignore").read_text().split()
    assert {".DS_Store", "__MACOSX/", ".ipynb_checkpoints/"} <= set(ignored)
