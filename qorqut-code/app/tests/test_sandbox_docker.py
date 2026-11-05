import json
import subprocess

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.docker import Docker, Ownership


OWNER = Ownership("a" * 20, "00000000-0000-4000-8000-000000000002")
CONTAINER = "b" * 64


def test_inventory_failure_is_not_an_absent_environment_or_a_secret_diagnostic():
    def unavailable(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "permission denied password=canary")
    with pytest.raises(DomainError) as error:
        Docker(runner=unavailable).inspect("container", "qbs-example")
    assert error.value.code == "environment_unavailable"
    assert "canary" not in str(error.value)
    def missing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "[]", "Error: No such container: qbs-example")
    assert Docker(runner=missing).inspect("container", "qbs-example") is None


def test_execution_rejects_foreign_resource_even_when_name_or_id_matches():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": {
            **OWNER.labels, "qorqut.business": "00000000-0000-4000-8000-000000000003"}},
            "State": {"Running": True}}]), "")
    with pytest.raises(DomainError) as error:
        Docker(runner=runner).exec(CONTAINER, OWNER, "true")
    assert error.value.code == "environment_conflict"
    assert len(calls) == 1 and calls[0][1:3] == ("container", "inspect")


def test_execution_does_not_follow_a_reused_name_or_stopped_container():
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": OWNER.labels},
            "State": {"Running": False}}]), "")
    client = Docker(runner=runner)
    for value in ("qbs-reused-name", CONTAINER):
        with pytest.raises(DomainError):
            client.exec(value, OWNER, "true")
