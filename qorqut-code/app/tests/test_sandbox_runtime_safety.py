import pytest
from types import SimpleNamespace

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.docker import Ownership
from bazarlyk.sandbox.runtime import DockerEnvironmentDriver


@pytest.mark.parametrize("pids,cpus", [(31, 1), (256, .00001)])
def test_undersized_replacement_does_not_stop_existing_environment(pids, cpus):
    def refused(*args):
        raise AssertionError("Invalid replacement attempted external work")
    driver = DockerEnvironmentDriver(readiness=refused, proxy_loader=refused)
    config = SimpleNamespace(limits=SimpleNamespace(pids=pids, cpus=cpus))
    with pytest.raises(DomainError, match="budget"):
        driver.reconcile({}, object(), config, 2, heartbeat=refused)


def test_failed_stop_preserves_old_resources_instead_of_removing_containment():
    calls = []
    class Docker:
        def call(self, *args, **kwargs):
            calls.append(args)
            raise DomainError("environment_unavailable", "Stop did not complete")
    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *args: True)
    owner = Ownership("a" * 20, "00000000-0000-4000-8000-000000000001")
    with pytest.raises(DomainError):
        driver._drain(owner, [{"Id": "b" * 64, "State": {"Running": True}}])
    assert calls == [("stop", "--time", "20", "b" * 64)]


def test_successful_stop_response_still_requires_no_running_writer_before_removal():
    calls = []
    current = {"Id": "b" * 64, "State": {"Running": True}}
    class Docker:
        def call(self, *args, **kwargs):
            calls.append(args)
        def owned(self, *args):
            return current
    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *args: True)
    owner = Ownership("a" * 20, "00000000-0000-4000-8000-000000000001")
    with pytest.raises(DomainError, match="writers"):
        driver._drain(owner, [current])
    assert calls == [("stop", "--time", "20", "b" * 64)]


def test_explicit_host_rollback_removes_only_preproven_stopped_owned_resources_then_rechecks_absence():
    calls, containers, networks = [], [{"Id": "b" * 64, "State": {"Running": False, "Paused": False,
                                                                     "Restarting": False}}], [
        {"Id": "n" * 64}]

    class Docker:
        def call(self, *args, **_kwargs):
            calls.append(args)
            if args[0] == "rm":
                containers.clear()
            elif args[:2] == ("network", "rm"):
                networks.clear()

    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *args: True,
                                     network=SimpleNamespace(remove_bridge=lambda identity: calls.append(("bridge", identity))))
    owner = Ownership("a" * 20, "00000000-0000-4000-8000-000000000001")
    driver.ownership = lambda _paths: owner
    driver._inventory = lambda kind, _owner: list(containers if kind == "container" else networks)
    listener = []
    driver.rollback_to_host({"id": owner.business_id}, object(),
                            stop_listener=lambda identity, *, timeout: listener.append((identity, timeout)),
                            profile_lock_unheld=lambda _paths: True)
    assert listener == [(owner.business_id, 20)]
    assert calls == [("rm", "b" * 64), ("bridge", "n" * 64), ("network", "rm", "n" * 64)]


def test_host_rollback_refuses_running_or_paused_writer_without_stopping_or_removing_it():
    calls = []
    driver = DockerEnvironmentDriver(docker=SimpleNamespace(call=lambda *args, **kwargs: calls.append(args)),
                                     readiness=lambda *args: True)
    owner = Ownership("a" * 20, "00000000-0000-4000-8000-000000000001")
    driver.ownership = lambda _paths: owner
    driver._inventory = lambda kind, _owner: [{"Id": "b" * 64, "State": {"Running": False, "Paused": True}}] \
        if kind == "container" else []
    with pytest.raises(DomainError, match="must be stopped"):
        driver.rollback_to_host({"id": owner.business_id}, object(),
                                stop_listener=lambda *_args, **_kwargs: pytest.fail("listener must not drain"),
                                profile_lock_unheld=lambda _paths: True)
    assert calls == []
