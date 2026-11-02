"""The terminal-outcome contribution of the Context queue.

Whenever a queue input becomes ``published`` or ``discarded`` the queue calls this one typed boundary in the same
transaction, so a source adapter that bound the input can keep its own durable completion record without a second
queue or a later best-effort scan. The contribution is required, not discovered: ``ContextQueueOperations`` and the
terminal ``transitions`` take it as an explicit argument, and nothing falls back when it is absent. ``context_filing``
implements it; an input no adapter bound simply produces no receipt.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TerminalOutcomes(Protocol):
    def record(self, uow, input_row) -> dict[str, Any] | None:
        """Record the terminal outcome of ``input_row`` (a stored, final input) inside the caller's transaction.

        Returns the stable completion receipt of a bound input, or ``None`` when no adapter bound it. Repeating an
        identical outcome returns the same receipt; a different outcome for the same input conflicts."""


def require_outcomes(outcomes: Any) -> TerminalOutcomes:
    if not isinstance(outcomes, TerminalOutcomes):
        raise TypeError("The Context queue requires an explicit terminal-outcome contribution")
    return outcomes
