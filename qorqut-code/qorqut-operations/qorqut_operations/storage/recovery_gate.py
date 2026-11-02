"""Preserve the installation's maintenance gate across a recovery capture."""

from dataclasses import dataclass

from qorqut_operations.contracts import DomainError


@dataclass(frozen=True)
class GateClosure:
    database_identity: tuple[int, int]
    previous: dict | None
    revision: int | None


class DatabaseMaintenanceGate:
    def __init__(self, database):
        self.database = database

    def _identity(self):
        try:
            info = self.database.path.stat()
        except OSError:
            raise DomainError("recovery_not_quiescent", "Recovery database identity is unavailable") from None
        return info.st_dev, info.st_ino

    def close(self):
        identity = self._identity()
        with self.database.transaction() as uow:
            repository = uow.repository("maintenance_state")
            previous = repository.get("service")
            if previous and previous["state"] != "open":
                return GateClosure(identity, previous, None)
            values = {"state": "closed", "message": "Installation recovery in progress"}
            current = (repository.update("service", values, expected_revision=previous["revision"])
                       if previous else repository.insert({"id": "service", **values}))
        return GateClosure(identity, previous, current["revision"])

    def restore(self, closure):
        # A restored or replaced database must retain its own closed authority
        # gates, even when record revisions happen to match the old database.
        if self._identity() != closure.database_identity:
            raise DomainError("recovery_not_quiescent", "Database identity changed; maintenance remains closed")
        if closure.revision is None:
            return  # This capture did not open or close the existing gate.
        with self.database.transaction() as uow:
            repository = uow.repository("maintenance_state")
            current = repository.get("service")
            if current is None or current["revision"] != closure.revision or current["state"] != "closed":
                raise DomainError("recovery_not_quiescent", "Maintenance ownership changed during recovery")
            if closure.previous is None:
                repository.delete("service", expected_revision=current["revision"])
            else:
                repository.update("service", {"state": closure.previous["state"], "message": closure.previous["message"]},
                                  expected_revision=current["revision"])
