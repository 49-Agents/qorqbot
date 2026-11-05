"""The database-backed service of the inbox nudge engine.

It owns no thread and starts nothing: the host's backend dispatcher calls ``prepare``, sends through its one native input
arbiter, then ``settle``s with the transport's report. Authorization is gathered in a read unit of work so no host permission
check runs while the writer lock is held."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from qorqut_operations.application.plans import timestamp, utc_clock

from . import state
from .contracts import IDLE, Binding, Observation, Outcome, Reservation, Waiting
from .ports import Visibility


class InboxNudges:
    def __init__(self, database, visibility: Visibility, *, policy: state.Policy | None = None, clock: Callable = utc_clock):
        self.database, self.visibility = database, visibility
        self.policy, self.clock = policy or state.Policy(), clock

    def now(self) -> str:
        """The engine's current time, as it stamps attempts (the host compares a waiting verdict's ``not_before`` with it)."""
        return timestamp(self.clock)


    def candidates(self, agent_ids: Sequence[str]) -> list[str]:
        """The given agents that have pending deliveries or an active need (indexed per-agent lookups)."""
        with self.database.read() as uow:
            return state.candidates(uow, list(agent_ids))

    def prepare(self, agent_id: str, binding: Binding | Any | None, observation: Observation | None = None) -> Reservation | Waiting:
        """Reserve one notice of the events eligible for exactly this verified session's conversation, or say why none is due.
        ``binding=None`` is an offline agent: the need stays durable and waits for a manual start. ``observation`` is recorded
        for diagnostics only and decides nothing (a neutral ``Observation()`` is fine). No native I/O."""
        observation = observation or Observation()
        exact = Binding.of(binding, agent_id) if binding is not None else None
        with self.database.read() as uow:
            facts = state.gather(uow, self.visibility, agent_id, exact)
        if not facts.scan.ids and facts.scan.empty and not facts.announced and not facts.active and facts.scan.after == 0:
            return Waiting("idle", status=IDLE)
        with self.database.transaction(operation="inbox_nudges.prepare") as uow:
            return state.prepare(uow, facts, agent_id, exact, observation, self.now(), self.policy)

    def validate(self, attempt_id: str, binding: Binding | Any) -> bool:
        """The fresh check immediately before provider input: the exact binding and reservation are current, and every covered
        event that is still pending is still eligible (authorized, not announced to this conversation since), with at least one
        left. A reservation that is no longer valid is cancelled (stale), never typed."""
        with self.database.read() as uow:
            reason = state.check(uow, self.visibility, attempt_id, binding)
        if reason is None:
            return True
        if reason != "not_reserved":
            with self.database.transaction(operation="inbox_nudges.cancel") as uow:
                state.cancel(uow, attempt_id, reason, self.now())
        return False

    def settle(self, attempt_id: str, outcome: Outcome) -> dict:
        with self.database.transaction(operation="inbox_nudges.settle") as uow:
            return state.settle(uow, attempt_id, outcome, self.now())

    def cancel(self, attempt_id: str, reason: str) -> dict:
        with self.database.transaction(operation="inbox_nudges.cancel") as uow:
            return state.cancel(uow, attempt_id, reason, self.now())

    def unsettled(self, agent_id: str | None = None, *, states: tuple[str, ...] = ("prepared", "uncertain")) -> list[dict]:
        with self.database.read() as uow:
            return state.unsettled(uow, agent_id, tuple(states))

    def status(self, agent_id: str, *, limit: int = 5, binding: Binding | Any | None = None) -> dict:
        """The operator diagnostics of one agent. With the host's current live ``binding`` (it must be this agent's) coverage is
        read for that conversation; without one, for the conversation of the agent's latest attempt."""
        with self.database.read() as uow:
            return state.status(uow, self.visibility, agent_id, limit=limit, binding=binding)
