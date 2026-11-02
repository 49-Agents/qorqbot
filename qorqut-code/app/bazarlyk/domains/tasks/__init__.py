"""Host side of the engine's Tasks: the retained v1 rules the Operations engine asks through its typed ports."""

from .ports import NOTE_REFS, task_ports

# Tasks whose snooze has not (yet) deferred them: one shared ``snoozed_until`` timestamp, expiry is a read. Bind the
# current UTC timestamp for the single placeholder; the Task row is aliased ``t``.
ACTIVE_SQL = "(t.snoozed_until IS NULL OR t.snoozed_until<=?)"

__all__ = ["ACTIVE_SQL", "NOTE_REFS", "task_ports"]
