"""The one authorized way to change the owning scope of stored Task rows.

Scope-immutability triggers on Task storage refuse every change of ``business_id``. A relocation is allowed only through
``relocation_permits(old_scope, new_scope, task_id)``, a SQLite function every connection carries. It answers ``0`` on any
connection that has not been explicitly armed - a raw open, a direct SQL statement, an ordinary operation - and ``1`` only
while the unit of work running a prepared, authorized move has armed it for exactly that source scope, destination scope
and set of Task ids. The guard belongs to one unit (``UnitOfWork.relocating``), is cleared in a ``finally`` and never reaches
the next unit that reuses the pooled connection."""

from __future__ import annotations

from dataclasses import dataclass

from qorqut_operations.contracts import DomainError

MAXIMUM_TASKS = 1000


@dataclass(frozen=True)
class RelocationGuard:
    """Exactly one source scope, one destination scope and the Task ids that may leave the first for the second."""

    source: str | None
    destination: str
    task_ids: frozenset

    def __post_init__(self):
        if (not isinstance(self.destination, str) or not self.destination or self.source == self.destination
                or (self.source is not None and not isinstance(self.source, str))
                or not isinstance(self.task_ids, frozenset) or not 0 < len(self.task_ids) <= MAXIMUM_TASKS
                or any(not isinstance(item, str) or not item for item in self.task_ids)):
            raise DomainError("validation_error", "A relocation names one source scope, one destination project and its Task ids")

    def permits(self, old, new, task_id) -> bool:
        return old == self.source and new == self.destination and task_id in self.task_ids


def refuse(_old, _new, _task_id) -> int:
    """The function every connection carries until a unit arms its own guard."""
    return 0


def permitted(unit, old, new, task_id) -> int:
    """The answer for a unit reference held weakly by its connection (``None`` once the unit is gone)."""
    guard = getattr(unit, "relocation", None) if unit is not None and getattr(unit, "writable", False) else None
    return 1 if guard is not None and guard.permits(old, new, task_id) else 0


__all__ = ["MAXIMUM_TASKS", "RelocationGuard", "permitted", "refuse"]
