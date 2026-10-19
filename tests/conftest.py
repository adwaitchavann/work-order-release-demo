from datetime import timedelta

import pytest

from wo_demo.clock import FixedClock
from wo_demo.env import DemoEnvironment
from wo_demo.fixtures import get_case


@pytest.fixture
def sign_receipt(env):
    """Sign a receipt directly, bypassing the determiner, the way buggy or hostile calling code might.

    Bindings default to the correct values for the action, the evidence on record, the policy in force
    and the MES record, so a test can change exactly one thing.
    """

    def sign(action, verdict="ADMIT", **overrides):
        now = env.clock.now()
        record = env.mes.lookup(action.work_order_id)
        on_record = env.evidence.current(action.work_order_id)
        fields = dict(
            receipt_id=f"rcpt-direct-{verdict.lower()}-{action.work_order_id}-{action.quantity}",
            verdict=verdict,
            work_order_id=action.work_order_id,
            action_digest=action.digest(),
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=env.policy.receipt_ttl_seconds)).isoformat(),
            evidence_digest=on_record.digest() if on_record else None,
            policy_version=env.policy.version,
            policy_digest=env.policy.digest(),
            facts_digest=record.facts_digest() if record else None,
        )
        fields.update(overrides)
        return env.signer.sign(**fields)

    return sign


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def env(clock):
    return DemoEnvironment.build(clock)


@pytest.fixture
def admit_case():
    return get_case("01_admit_release")


@pytest.fixture
def failing_case():
    return get_case("02_admit_execution_failure")


@pytest.fixture
def halt_case():
    return get_case("03_halt_revoked_approval")


@pytest.fixture
def defer_case():
    return get_case("04_defer_missing_evidence")


@pytest.fixture
def refer_case():
    return get_case("05_refer_quality_signoff")


@pytest.fixture
def reissue_case():
    return get_case("06_admit_reissued_approval")


@pytest.fixture
def assert_mes_untouched(env):
    """Snapshot the MES now; calling the returned function asserts nothing changed and nothing was called."""
    before = env.mes.snapshot()

    def check():
        assert env.mes.snapshot() == before, "work order state changed"
        assert env.mes.call_log == [], "the MES was called"

    return check
