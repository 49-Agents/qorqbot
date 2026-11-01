"""The one port the engine needs from its host: current source authorization of inbox events.

The engine decides which delivery rows are still pending (unacknowledged) and nothing else. Whether the agent may *currently*
see an event is the host's domain authorization (per source type, per scope, private conversations, retired and revoked
sources). The engine never trusts a directory, tag or display name, never reads a summary, and treats an event the host does not
confirm as not pending for nudging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class Visibility(Protocol):
    def authorized(self, uow, agent_id: str, events: Sequence[Mapping]) -> set[int]:
        """The ids, among the given ``inbox_events`` rows, that ``agent_id`` may currently read.

        ``uow`` is the engine's open unit of work, for reads only. An event whose source is missing, unavailable, revoked or of
        an unknown type is simply not in the result; the answer must not raise for an ordinary denial."""
