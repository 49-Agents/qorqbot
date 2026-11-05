from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox import SandboxStateStore
from bazarlyk.storage import Database
from foundation_support import BUSINESS, database_at


class Clock:
    def __init__(self, value="2026-09-12T12:00:00.000000Z"):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        current = datetime.fromisoformat(self.value.replace("Z", "+00:00"))
        self.value = (current.timestamp() + seconds)
        self.value = datetime.fromtimestamp(self.value, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_intent_is_transactional_idempotent_and_contains_no_config(tmp_path):
    database = database_at(tmp_path)
    store = SandboxStateStore()
    with pytest.raises(RuntimeError):
        with database.transaction() as uow:
            row = store.insert_intent(uow, BUSINESS)
            assert row["desired_generation"] == 1
            raise RuntimeError("business setup did not commit")
    with database.read() as uow:
        assert store.get(uow, BUSINESS) is None
    with database.transaction() as uow:
        first = store.insert_intent(uow, BUSINESS)
        second = store.insert_intent(uow, BUSINESS)
        assert first == second
        assert set(first) == {"business_id", "desired_generation", "desired_state", "observed_generation", "environment_id", "state", "error_code", "lease_fence", "lease_until", "revision", "created_at", "updated_at", "disk_used_bytes", "disk_budget_bytes", "disk_reserved_bytes", "disk_available_bytes", "disk_host_floor_bytes", "disk_checked_at", "disk_error_code"}
        assert first["desired_state"] == "running"
        assert first["state"] == "pending"
    with database.read() as uow:
        assert store.get(uow, BUSINESS)["desired_generation"] == 1


def test_replacement_preserves_previous_observation_and_invalidates_lease(tmp_path):
    database = database_at(tmp_path)
    clock = Clock()
    store = SandboxStateStore(clock=clock)
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
    with database.transaction() as uow:
        lease = store.claim(uow, BUSINESS)
    with database.transaction() as uow:
        ready = store.mark_ready(uow, lease, environment_id="sandbox-old")
    with database.transaction() as uow:
        pending = store.request_replacement(uow, BUSINESS, expected_revision=ready["revision"])
    assert pending["desired_generation"] == 2
    assert pending["observed_generation"] == 1
    assert pending["environment_id"] == "sandbox-old"
    assert pending["state"] == "pending"
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="lease"):
            store.mark_ready(uow, lease, environment_id="sandbox-stale")


def test_only_one_reconciler_claims_and_expiry_fences_the_stale_worker(tmp_path):
    database = database_at(tmp_path)
    clock = Clock()
    store = SandboxStateStore(clock=clock)
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
    def claim():
        with database.transaction() as uow:
            return store.claim(uow, BUSINESS, lease_seconds=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _ignored: claim(), range(2)))
    first = next(claim for claim in claims if claim is not None)
    assert claims.count(None) == 1
    clock.advance(6)
    recovered = Database(database.path).open_existing()
    with recovered.transaction() as uow:
        second = store.claim(uow, BUSINESS, lease_seconds=5)
    assert second.fence > first.fence
    with recovered.transaction() as uow:
        with pytest.raises(DomainError) as stale:
            store.mark_failed(uow, first, error_code="provisioning_failed")
    assert stale.value.code == "reconciliation_lease_lost"
    with recovered.transaction() as uow:
        ready = store.mark_ready(uow, second, environment_id="sandbox-current")
    assert ready["state"] == "ready"
    assert ready["observed_generation"] == ready["desired_generation"]


def test_lease_is_expired_at_its_exact_deadline_and_can_be_claimed_again(tmp_path):
    database = database_at(tmp_path)
    clock = Clock()
    store = SandboxStateStore(clock=clock)
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
        first = store.claim(uow, BUSINESS, lease_seconds=5)
    clock.advance(5)
    with database.transaction() as uow:
        with pytest.raises(DomainError) as expired:
            store.renew(uow, first, lease_seconds=5)
    assert expired.value.code == "reconciliation_lease_lost"
    with database.transaction() as uow:
        second = store.claim(uow, BUSINESS, lease_seconds=5)
    assert second.fence > first.fence


def test_failure_codes_are_sanitized_and_observation_requires_a_current_lease(tmp_path):
    database = database_at(tmp_path)
    store = SandboxStateStore(clock=Clock())
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
        lease = store.claim(uow, BUSINESS)
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="sanitized"):
            store.mark_failed(uow, lease, error_code="docker said password=secret")
    with database.transaction() as uow:
        failed = store.mark_failed(uow, lease, error_code="environment_unavailable")
    assert failed["state"] == "failed"
    assert failed["error_code"] == "environment_unavailable"
    with database.transaction() as uow:
        another = store.claim(uow, BUSINESS)
        stopped = store.mark_stopped(uow, another, environment_id="sandbox-current")
    assert stopped["state"] == "stopped"


def test_pending_configuration_reason_is_visible_and_renewal_requires_current_fence(tmp_path):
    database = database_at(tmp_path)
    clock = Clock()
    store = SandboxStateStore(clock=clock)
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
        lease = store.claim(uow, BUSINESS, lease_seconds=5)
    clock.advance(2)
    with database.transaction() as uow:
        renewed = store.renew(uow, lease, lease_seconds=5)
    assert renewed["lease_until"] == "2026-09-12T12:00:07.000000Z"
    with database.transaction() as uow:
        pending = store.mark_pending(uow, lease)
    assert pending["state"] == "pending"
    assert pending["error_code"] == "configuration_missing"
    assert pending["lease_until"] is None
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="pending"):
            store.mark_pending(uow, lease, error_code="environment_unavailable")
        with pytest.raises(DomainError) as stale:
            store.renew(uow, lease, lease_seconds=5)
    assert stale.value.code == "reconciliation_lease_lost"
