"""Delivery of published question batches from the Context engine's outbox into the question store.

Only the filer creates questions: each published event may leave one outbox batch in the Context's own runtime. This module
lists those batches, asks the engine for each one (``question_output`` refuses an event that is not fully published),
delivers it to the store in one transaction and only then acknowledges exactly that event and commit. Every step is
repeatable: the store's delivery ledger makes a redelivery a no-op, including after the questions were deleted, and the
engine's acknowledgement is idempotent. A failure leaves the batch where it is for a later step and is reported as a
diagnostic naming the event and stage, never question text; it neither refiles anything nor changes a published event.
Draining never starts a model."""

from __future__ import annotations

from bisect import bisect_right
import os
import re
import threading

from qorqut_context import Conflict, ContextError

from .store import QuestionError, QuestionStoreError

BATCH_LIMIT = 16
ERROR_LIMIT = 5
EVENT_ID = re.compile(r"[a-z0-9]{5}")
AUTHORED = (QuestionError, QuestionStoreError, ContextError)


def failure(event_id, stage, error):
    label = type(error).__name__
    if isinstance(error, AUTHORED):
        label = f"{label}: {error}"[:200]
    return {"event_id": event_id, "stage": stage, "error": label}


class QuestionOutbox:
    def __init__(self, context, pipeline, store, *, limit: int = BATCH_LIMIT):
        if type(limit) is not int or limit < 1:
            raise ValueError("The question outbox needs a positive batch limit")
        self.context, self.pipeline, self.store, self.limit = context, pipeline, store, limit
        self.last: dict | None = None
        self._cursor: str | None = None
        self._guard = threading.Lock()

    def directory(self):
        return self.context.runtime / "questions"

    def pending(self) -> list[str]:
        """The event IDs that have an outbox entry, in a stable order. Reads a directory listing only."""
        try:
            names = os.listdir(self.directory())
        except FileNotFoundError:
            return []
        return sorted(name[:-5] for name in names if name.endswith(".json") and EVENT_ID.fullmatch(name[:-5]))

    def drain(self) -> dict:
        """Deliver up to ``limit`` batches. Never raises for an operational failure; the result is the diagnostic."""
        if not self._guard.acquire(blocking=False):
            return {"status": "busy"}
        try:
            outcome = {"status": "drained", "examined": 0, "delivered": 0, "inserted": 0, "waiting": 0, "failed": 0,
                       "errors": []}
            try:
                selected = self._select(self.pending())
            except OSError as error:
                outcome.update(status="list_failed", failed=1, errors=[failure(None, "list", error)])
                self.last = outcome
                return outcome
            for event_id in selected:
                self._deliver(event_id, outcome)
            self.last = outcome
            return outcome
        finally:
            self._guard.release()

    def diagnostics(self) -> dict:
        return {"last": self.last, "limit": self.limit}

    def _select(self, identifiers):
        if not identifiers:
            return []
        start = bisect_right(identifiers, self._cursor) if self._cursor is not None else 0
        chosen = (identifiers[start:] + identifiers[:start])[:self.limit]
        self._cursor = chosen[-1]
        return chosen

    def _deliver(self, event_id, outcome):
        outcome["examined"] += 1
        stage = "question_output"
        try:
            output = self.pipeline.question_output(event_id)
            questions, commit = output["questions"], output["published_commit"]
            if not questions:
                return
            stage = "deliver"
            result = self.store.deliver(event_id, commit, questions)
            stage = "acknowledge"
            self.pipeline.acknowledge_question_output(event_id, commit)
        except Conflict:
            outcome["waiting"] += 1
        except Exception as error:
            outcome["failed"] += 1
            if len(outcome["errors"]) < ERROR_LIMIT:
                outcome["errors"].append(failure(event_id, stage, error))
        else:
            outcome["delivered"] += 1
            outcome["inserted"] += result["inserted"]
