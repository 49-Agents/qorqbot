from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.config import HostConfig, ResourceLimits
from bazarlyk.sandbox.lifecycle import EnvironmentResult, SandboxLifecycle
from qorqut_operations.storage.admission import InstallationRecoveryLock
from foundation_support import BUSINESS, database_at
from bazarlyk.sandbox.disk import _GIB


CONFIG = HostConfig("sha256:" + "a" * 64, "sha256:" + "b" * 64, ResourceLimits(512, 512, 128, 1, 256, 8), 1148, enabled=True)


def empty_adoption(*_args, **_kwargs):
    """Explicit test-only inventory; production composition supplies host probes."""
    return None


@pytest.fixture(autouse=True)
def fixed_disk_capacity(monkeypatch):
    """Keep lifecycle unit tests independent of the host test filesystem."""
    capacity = SimpleNamespace(f_frsize=1, f_bsize=1, f_bavail=64 * _GIB)
    monkeypatch.setattr("bazarlyk.sandbox.disk.os.statvfs", lambda _: capacity)


def test_concurrent_reconciliation_holds_external_lock_and_no_database_transaction(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)
    entered, finish = Event(), Event()
    calls = []

    class Driver:
        def reconcile(self, business, paths, config, generation, *, heartbeat):
            calls.append(generation)
            # A separate SQLite writer can commit while the external driver runs.
            with database.transaction() as uow:
                row = uow.repository("business_sandboxes").get(BUSINESS)
                assert row["desired_generation"] == generation
            heartbeat()
            entered.set()
            assert finish.wait(10)
            return EnvironmentResult("test-environment")

    service = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda root: CONFIG,
                               adoption_inventory=empty_adoption)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.ensure_business_environment, BUSINESS)
        assert entered.wait(10)
        try:
            second = pool.submit(service.ensure_business_environment, BUSINESS).result(timeout=5)
            assert second["state"] == "pending"
            assert calls == [1]
        finally:
            finish.set()
        assert first.result(timeout=10)["state"] == "ready"


def test_missing_configuration_and_failures_are_visible_without_secret_diagnostics(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)
    config = [None]

    class Driver:
        def reconcile(self, *args, **kwargs):
            raise RuntimeError("private URL with password=canary must never be stored")

    service = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda root: config[0],
                               adoption_inventory=empty_adoption)
    pending = service.ensure_business_environment(BUSINESS)
    assert pending["state"] == "pending" and pending["error_code"] == "configuration_missing"
    config[0] = CONFIG
    failed = service.ensure_business_environment(BUSINESS)
    assert failed["state"] == "failed" and failed["error_code"] == "provisioning_failed"
    assert "canary" not in str(failed)


def test_disabled_host_configuration_records_intent_without_creating_workspace_or_runtime(tmp_path):
    database = database_at(tmp_path)
    calls = []

    class Driver:
        def reconcile(self, *args, **kwargs):
            calls.append(True)
            raise AssertionError("disabled configurations must not reconcile")

    disabled = HostConfig("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                          ResourceLimits(512, 512, 128, 1, 256, 8), 1148)
    row = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda root: disabled,
                           adoption_inventory=empty_adoption).ensure_business_environment(BUSINESS)
    assert row["state"] == "pending"
    assert calls == []
    assert not (tmp_path / "first/hq").exists()
    assert row["error_code"] == "configuration_missing"


def test_provisioner_waits_until_committed_seed_finishes_initial_files(tmp_path):
    database = database_at(tmp_path)
    calls = []
    class Driver:
        def reconcile(self, business, paths, config, generation, *, heartbeat):
            calls.append(generation)
            return EnvironmentResult("fixture")
    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG,
                                 adoption_inventory=empty_adoption)
    assert lifecycle.ensure_business_environment(BUSINESS)["state"] == "pending"
    hq = tmp_path / "first/hq"
    hq.mkdir(parents=True)
    marker = tmp_path / "first/.bazarlyk/bootstrap-seed.json"
    marker.parent.mkdir()
    marker.write_text("in progress")
    assert lifecycle.ensure_business_environment(BUSINESS)["state"] == "pending"
    assert calls == []
    marker.unlink()
    assert lifecycle.ensure_business_environment(BUSINESS)["state"] == "ready"
    assert calls == [1]


def test_invalid_host_configuration_records_sanitized_failed_intent(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)

    class Driver:
        def reconcile(self, *args, **kwargs):
            raise AssertionError("invalid host configuration must not reconcile")

    def invalid(root):
        raise DomainError("invalid_configuration", "private host details")

    row = SandboxLifecycle(database, tmp_path, Driver(), config_loader=invalid,
                           adoption_inventory=empty_adoption).ensure_business_environment(BUSINESS)
    assert row["state"] == "failed" and row["error_code"] == "provisioning_failed"
    assert "private" not in str(row)


def test_restart_reuses_observed_resource_and_fences_a_changed_generation(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)
    resources, creates = {}, []

    class Driver:
        def reconcile(self, business, paths, config, generation, *, heartbeat):
            if generation not in resources:
                creates.append(generation)
                resources[generation] = "test-environment-" + str(generation)
            heartbeat()
            return EnvironmentResult(resources[generation])

    def restarted():
        return SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda root: CONFIG,
                                adoption_inventory=empty_adoption)

    first = restarted().ensure_business_environment(BUSINESS)
    again = restarted().ensure_business_environment(BUSINESS)
    assert first["environment_id"] == again["environment_id"] and creates == [1]
    service = restarted()
    with database.transaction() as uow:
        service.state.request_replacement(uow, BUSINESS, expected_revision=again["revision"])
    changed = service.ensure_business_environment(BUSINESS)
    assert changed["observed_generation"] == 2 and creates == [1, 2]


def test_existing_adoption_requires_concrete_inventory_and_never_changes_a_foreign_business(tmp_path):
    database = database_at(tmp_path)

    class Driver:
        def reconcile(self, *_args, **_kwargs):
            return EnvironmentResult("fixture")

    unavailable = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG)
    with pytest.raises(DomainError, match="adoption inventory"):
        unavailable.ensure_business_environment(BUSINESS)
    with database.read() as uow:
        assert unavailable.state.get(uow, BUSINESS) is None
        assert unavailable.state.get(uow, "00000000-0000-4000-8000-000000000003") is None

    attempted = []
    def active_inventory(business, paths, *, timeout):
        attempted.append((business["id"], paths.business_id, timeout))
        raise DomainError("recovery_not_quiescent", "selected host terminal is live", retryable=True)

    retryable = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG,
                                 adoption_inventory=active_inventory)
    with pytest.raises(DomainError, match="selected host terminal"):
        retryable.ensure_business_environment(BUSINESS)
    with database.read() as uow:
        assert retryable.state.get(uow, BUSINESS) is None
    assert attempted == [(BUSINESS, BUSINESS, 60)]

    (tmp_path / "first/hq").mkdir(parents=True)
    adopted = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG,
                               adoption_inventory=empty_adoption).ensure_business_environment(BUSINESS)
    assert adopted["state"] == "ready"
    with database.read() as uow:
        assert adopted["business_id"] == BUSINESS
        assert retryable.state.get(uow, "00000000-0000-4000-8000-000000000003") is None


def test_adoption_exclusive_lock_blocks_terminal_admission_until_intent_commits(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)
    entered, release, admitted = Event(), Event(), Event()

    class Driver:
        def reconcile(self, *_args, **_kwargs):
            return EnvironmentResult("fixture")

    def inventory(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)

    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG,
                                 adoption_inventory=inventory)
    with ThreadPoolExecutor(2) as pool:
        adoption = pool.submit(lifecycle.ensure_business_environment, BUSINESS)
        assert entered.wait(5)

        def terminal_open():
            with InstallationRecoveryLock(tmp_path).shared(timeout=5):
                admitted.set()
        terminal = pool.submit(terminal_open)
        assert not admitted.wait(.1)
        release.set()
        assert adoption.result(timeout=5)["state"] == "ready"
        terminal.result(timeout=5)
    assert admitted.is_set()


def test_committed_creation_intent_skips_adoption_inventory(tmp_path):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        SandboxLifecycle(database, tmp_path, object()).state.insert_intent(uow, BUSINESS)

    class Driver:
        def reconcile(self, *_args, **_kwargs):
            raise AssertionError("disabled configuration must not reconcile")

    disabled = HostConfig("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                          ResourceLimits(512, 512, 128, 1, 256, 8), 1148)
    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: disabled,
                                 adoption_inventory=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    assert lifecycle.ensure_business_environment(BUSINESS)["state"] == "pending"


def test_explicit_host_rollback_deletes_only_unchanged_verified_stopped_intent(tmp_path):
    database = database_at(tmp_path)
    calls = []

    class Driver:
        def rollback_to_host(self, business, paths, *, stop_listener, profile_lock_unheld):
            calls.append((business["id"], paths.business_id, profile_lock_unheld(paths)))
            stop_listener(business["id"], timeout=20)

    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG)
    lifecycle.set_rollback_boundary(stop_listener=lambda identity, *, timeout: calls.append((identity, timeout)),
                                    profile_lock_unheld=lambda _paths: True)
    with database.transaction() as uow:
        row = lifecycle.state.insert_intent(uow, BUSINESS)
        lifecycle.state.request_state(uow, BUSINESS, desired_state="stopped", expected_revision=row["revision"])
        lease = lifecycle.state.claim(uow, BUSINESS)
        stopped = lifecycle.state.mark_stopped(uow, lease)
    removed = lifecycle.rollback_to_host(BUSINESS, expected_revision=stopped["revision"])
    assert removed["business_id"] == BUSINESS
    assert calls == [(BUSINESS, BUSINESS, True), (BUSINESS, 20)]
    with database.read() as uow:
        assert lifecycle.state.get(uow, BUSINESS) is None
        assert lifecycle.state.get(uow, "00000000-0000-4000-8000-000000000003") is None


def test_host_rollback_retains_intent_on_profile_or_state_ambiguity(tmp_path):
    database = database_at(tmp_path)

    class Driver:
        def rollback_to_host(self, business, paths, *, profile_lock_unheld, **_kwargs):
            if profile_lock_unheld(paths) is not True:
                raise DomainError("environment_conflict", "ambiguous profile")
            raise AssertionError("unexpected resource removal")

    lifecycle = SandboxLifecycle(database, tmp_path, Driver())
    lifecycle.set_rollback_boundary(stop_listener=lambda *_args, **_kwargs: None,
                                    profile_lock_unheld=lambda _paths: False)
    with database.transaction() as uow:
        row = lifecycle.state.insert_intent(uow, BUSINESS)
        requested = lifecycle.state.request_state(uow, BUSINESS, desired_state="stopped",
                                                  expected_revision=row["revision"])
        lease = lifecycle.state.claim(uow, BUSINESS)
        stopped = lifecycle.state.mark_stopped(uow, lease)
    with pytest.raises(DomainError, match="ambiguous profile"):
        lifecycle.rollback_to_host(BUSINESS, expected_revision=stopped["revision"])
    with database.read() as uow:
        assert lifecycle.state.get(uow, BUSINESS)["revision"] == stopped["revision"]
    with pytest.raises(DomainError, match="unchanged and stopped"):
        lifecycle.rollback_to_host(BUSINESS, expected_revision=requested["revision"])


def test_host_rollback_rechecks_current_authority_before_any_external_removal(tmp_path):
    database = database_at(tmp_path)

    class Driver:
        def rollback_to_host(self, *_args, **_kwargs):
            pytest.fail("authority failure must precede external rollback")

    lifecycle = SandboxLifecycle(database, tmp_path, Driver())
    lifecycle.set_rollback_boundary(stop_listener=lambda *_args, **_kwargs: None,
                                    profile_lock_unheld=lambda _paths: True,
                                    require_authority=lambda: (_ for _ in ()).throw(
                                        DomainError("authority_not_ready", "not current", retryable=True)))
    with database.transaction() as uow:
        row = lifecycle.state.insert_intent(uow, BUSINESS)
        lifecycle.state.request_state(uow, BUSINESS, desired_state="stopped", expected_revision=row["revision"])
        lease = lifecycle.state.claim(uow, BUSINESS)
        stopped = lifecycle.state.mark_stopped(uow, lease)
    with pytest.raises(DomainError, match="not current"):
        lifecycle.rollback_to_host(BUSINESS, expected_revision=stopped["revision"])
    with database.read() as uow:
        assert lifecycle.state.get(uow, BUSINESS)["revision"] == stopped["revision"]
