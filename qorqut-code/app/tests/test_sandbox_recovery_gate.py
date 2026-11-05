import pytest

from bazarlyk.application.engine import Application
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.recovery_gate import DatabaseMaintenanceGate
from foundation_support import database_at


@pytest.mark.parametrize("initial", [None, "open", "closed", "retired"])
def test_capture_blocks_writes_and_preserves_previous_maintenance_state(tmp_path, initial):
    database = database_at(tmp_path)
    if initial:
        with database.transaction() as uow:
            uow.repository("maintenance_state").insert({"id": "service", "state": initial, "message": "Original message"})
    gate = DatabaseMaintenanceGate(database)
    closure = gate.close()
    with database.read() as uow:
        with pytest.raises(DomainError) as refused:
            Application.ensure_writes_open(uow)
        assert refused.value.code == "maintenance"
    gate.restore(closure)
    with database.read() as uow:
        current = uow.repository("maintenance_state").get("service")
        if initial is None:
            assert current is None
        else:
            assert current["state"] == initial and current["message"] == "Original message"


def test_capture_cannot_reopen_another_operators_maintenance_gate(tmp_path):
    database = database_at(tmp_path)
    gate = DatabaseMaintenanceGate(database)
    closure = gate.close()
    with database.transaction() as uow:
        current = uow.repository("maintenance_state").get("service")
        uow.repository("maintenance_state").update("service", {"message": "Another maintenance operation"},
                                                  expected_revision=current["revision"])
    with pytest.raises(DomainError, match="ownership changed"):
        gate.restore(closure)
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
