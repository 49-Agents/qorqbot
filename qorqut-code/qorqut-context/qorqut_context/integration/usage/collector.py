"""The bounded Context usage collector: private library receipts into the operational usage ledger, one small step at a time.

``ContextUsageCollector.collect_next`` is a synchronous step the final lifecycle owner calls (before and after Context driver
steps and on idle ticks). This module owns no thread, timer, daemon or scheduled program. A step reads at most ``max_pages``
pages of ``page_size`` receipts of one (queue input, Context event) pair through the library's read-only
``usage_receipts(event_id, *, limit, cursor)``, which is not a provider or a model runner, and writes them with the ledger's
own atomic ``ingest`` and ``record_scan`` and nothing else: it never starts or retries a Context input, changes queue state,
emits an inbox event or acknowledges work. It holds no database write transaction while it reads a page; a fetched page and
its sync status are saved in one unit of work.

The pairs come from the queue's actual attempts (historical, failed, discarded and published inputs included; attempts that
never reserved a Context event are skipped) ordered by input sequence and event ID. The traversal state is in memory only and a
restart begins a fresh traversal: call IDs are immutable, so replaying a page changes nothing. At the end of the ordered
inventory the next step wraps to its start, so started, missing or unavailable evidence can later advance. A pair whose evidence
is unavailable, corrupt or refused never blocks the others.

One nonblocking installation lock (an advisory ``flock`` on the supplied private runtime path, so it also excludes other
processes) serializes every step; a concurrent step returns ``busy``. That is stronger than per-input serialization and keeps a
stale scan from overwriting a later one.

Every statement about completeness is truthful: ``ready`` means a successful traversal reached the end of the library's pages
(an empty ``ready`` page proves only that no receipt was observed, never zero usage or cost), ``partial`` means a continuation
remains, ``unavailable`` records a bounded diagnostic while earlier imports stay intact. A write database that cannot be used
raises ``CollectorStorageError``; it is never reported as an empty or ready scan.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import threading
from typing import Any, Protocol

from qorqut_context.integration import usage as context_usage
from qorqut_operations.contracts import DomainError, utc_now

STEP_STATUSES = ("busy", "idle", "partial", "ready", "unavailable")
MAX_PAGE_SIZE = context_usage.MAX_BATCH
MAX_STEP_PAGES = 100
MAX_CURSOR = 4096
CODE_LIMIT, MESSAGE_LIMIT = 64, 300
# Ledger refusals that describe the evidence itself; anything else from a unit of work is the database or the configuration.
EVIDENCE_REFUSALS = {"validation_error": "invalid_receipt", "usage_identity_conflict": "receipt_conflict",
                     "usage_outcome_conflict": "receipt_conflict", "usage_evidence_conflict": "receipt_conflict"}
PAGE_KEYS = frozenset({"status", "event_id", "items", "next_cursor", "error"})


class UsageReader(Protocol):
    """The accepted library's read-only receipt reader (``Pipeline.usage_receipts``)."""

    def usage_receipts(self, event_id: str, *, limit: int, cursor: str | None = None) -> Mapping[str, Any]: ...


class UsageCollection(Protocol):
    """What the lifecycle owner needs: one bounded step, and the facts about the steps so far."""

    def collect_next(self) -> CollectionStep: ...

    def diagnostics(self) -> dict: ...


class CollectorStorageError(RuntimeError):
    """The usage ledger could not be written (database lost or locked, or the driver is not the system principal)."""


class _Refused(Exception):
    """A page or receipt that must not be ingested; carries the bounded diagnostic to record."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


@dataclass(frozen=True)
class CollectionStep:
    """What one ``collect_next`` did. ``status`` is the final page's: ``partial`` while pages remain for the pair."""

    status: str
    input_id: str | None = None
    context_event_id: str | None = None
    pages: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    stale: int = 0
    error: dict | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "input_id": self.input_id, "context_event_id": self.context_event_id, "pages": self.pages,
                "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged, "stale": self.stale,
                "error": self.error}


@dataclass
class _Traversal:
    sequence: int
    input_id: str
    event_id: str
    cursor: str | None = None
    seen: set = field(default_factory=set)
    pages: int = 0


def _bounded(code: str, message: str) -> dict:
    return {"code": code[:CODE_LIMIT], "message": message[:MESSAGE_LIMIT]}


class ContextUsageCollector:
    def __init__(self, database, reader: UsageReader, *, driver_id: str, lock_path, page_size: int = 100, max_pages: int = 5):
        if not isinstance(driver_id, str) or not driver_id:
            raise ValueError("The collector needs the configured backend system principal ID")
        lock_path = Path(lock_path)
        if not lock_path.is_absolute():
            raise ValueError("The collector lock needs an explicit absolute private runtime path")
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be an integer from 1 to {MAX_PAGE_SIZE}")
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_STEP_PAGES:
            raise ValueError(f"max_pages must be an integer from 1 to {MAX_STEP_PAGES}")
        self.database, self.reader, self.driver_id = database, reader, driver_id
        self.lock_path, self.page_size, self.max_pages = lock_path, page_size, max_pages
        self._current: _Traversal | None = None
        self._last_key: tuple[int, str] | None = None
        self._guard = threading.Lock()
        self._steps = {status: 0 for status in STEP_STATUSES}
        self._traversals = 0
        self._failures = 0
        self._last: CollectionStep | None = None
        self._last_error: dict | None = None
        self._last_step_at: str | None = None

    # -- the step ------------------------------------------------------------------------------------------------------------

    def collect_next(self) -> CollectionStep:
        """One bounded step. Returns ``busy`` when another step (here or in another process) holds the installation lock."""
        with self._exclusive() as acquired:
            if not acquired:
                return self._record(CollectionStep("busy"))
            try:
                step = self._step()
            except CollectorStorageError as error:
                with self._guard:
                    self._failures += 1
                    self._last_error = _bounded("storage_error", str(error))
                    self._last_step_at = utc_now()
                raise
        return self._record(step)

    def diagnostics(self) -> dict:
        """Facts about this collector's steps; it reads and changes nothing and never invokes a step."""
        with self._guard:
            current = self._current
            return {
                "page_size": self.page_size, "max_pages": self.max_pages, "steps": dict(self._steps),
                "traversals": self._traversals, "storage_failures": self._failures,
                "state": None if self._last is None else self._last.status,
                "current": None if current is None else {"input_id": current.input_id, "context_event_id": current.event_id,
                                                          "pages_read": current.pages, "continues": current.cursor is not None},
                "last_completed": None if self._last_key is None else {"input_sequence": self._last_key[0],
                                                                        "context_event_id": self._last_key[1]},
                "last": None if self._last is None else self._last.as_dict(), "last_error": self._last_error,
                "last_step_at": self._last_step_at}

    def _record(self, step: CollectionStep) -> CollectionStep:
        with self._guard:
            self._steps[step.status] += 1
            self._last, self._last_step_at = step, utc_now()
            if step.error is not None:
                self._last_error = {**step.error, "input_id": step.input_id, "context_event_id": step.context_event_id}
        return step

    @contextmanager
    def _exclusive(self):
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held = False
            else:
                held = True
            try:
                yield held
            finally:
                if held:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _step(self) -> CollectionStep:
        traversal = self._current or self._next()
        if traversal is None:
            return CollectionStep("idle")
        with self._guard:
            self._current = traversal
        totals = {"inserted": 0, "updated": 0, "unchanged": 0, "stale": 0}
        status, error = "partial", None
        pages = 0
        while pages < self.max_pages:
            status, error, counts = self._page(traversal)
            pages += 1
            for name, value in counts.items():
                totals[name] += value
            if status != "partial":
                with self._guard:
                    self._current, self._last_key = None, (traversal.sequence, traversal.event_id)
                break
        return CollectionStep(status, traversal.input_id, traversal.event_id, pages, error=error, **totals)

    def _next(self) -> _Traversal | None:
        """The first reserved (input, event) after the last completed one, wrapping to the start at the end of the inventory."""
        try:
            with self.database.read() as uow:
                row = self._pair_after(uow, self._last_key)
                if row is None and self._last_key is not None:
                    with self._guard:
                        self._traversals += 1
                        self._last_key = None
                    row = self._pair_after(uow, None)
        except Exception as failure:      # no inventory is no knowledge: never a quiet "idle"
            raise CollectorStorageError(
                f"The Context event inventory could not be read ({type(failure).__name__})") from failure
        return None if row is None else _Traversal(row["sequence"], row["input_id"], row["context_event_id"])

    @staticmethod
    def _pair_after(uow, key):
        after, parameters = ("AND (i.sequence, a.context_event_id) > (?, ?)", key) if key else ("", ())
        return uow.connection.execute(
            "SELECT DISTINCT i.sequence AS sequence, a.input_id AS input_id, a.context_event_id AS context_event_id "
            "FROM context_input_attempts a JOIN context_inputs i ON i.id = a.input_id "
            f"WHERE a.context_event_id IS NOT NULL {after} ORDER BY i.sequence, a.context_event_id LIMIT 1", parameters).fetchone()

    # -- one page ------------------------------------------------------------------------------------------------------------

    def _page(self, traversal: _Traversal) -> tuple[str, dict | None, dict]:
        """Read, validate and save one page; returns (status, bounded error or None, ingest counts)."""
        none = {"inserted": 0, "updated": 0, "unchanged": 0, "stale": 0}
        try:
            page = self.reader.usage_receipts(traversal.event_id, limit=self.page_size, cursor=traversal.cursor)
        except Exception as error:      # the reader is the library's: any failure is unreadable evidence, never an empty scan
            return self._unavailable(traversal, "read_failed",
                                     f"The Context usage evidence could not be read ({type(error).__name__})", none)
        try:
            items, following, problem = self._envelope(page, traversal)
        except _Refused as refused:
            return self._unavailable(traversal, refused.code, refused.message, none)
        if problem is not None:
            return self._unavailable(traversal, problem["code"], problem["message"], none)
        status = "partial" if following is not None else "ready"
        try:
            with self.database.transaction() as uow:
                result = (context_usage.ingest(uow, driver_id=self.driver_id, input_id=traversal.input_id, receipts=items)
                          if items else None)
                context_usage.record_scan(uow, driver_id=self.driver_id, input_id=traversal.input_id,
                                          context_event_id=traversal.event_id, status=status)
        except DomainError as error:
            if error.code in EVIDENCE_REFUSALS:
                return self._unavailable(traversal, EVIDENCE_REFUSALS[error.code],
                                         "The Context usage receipts were refused by the ledger and were not imported", none)
            return self._unavailable(traversal, "storage_error", "The usage ledger could not save this page", none)
        except Exception:
            return self._unavailable(traversal, "storage_error", "The usage ledger could not save this page", none)
        traversal.pages += 1
        if following is not None:
            traversal.cursor = following
            traversal.seen.add(following)
        counts = none if result is None else {"inserted": result.inserted, "updated": result.updated,
                                              "unchanged": result.unchanged, "stale": result.stale}
        return status, None, counts

    def _envelope(self, page, traversal: _Traversal):
        """The page's items and continuation, or a bounded unavailable diagnostic; ``_Refused`` for a page not to be trusted."""
        if not isinstance(page, Mapping) or set(page) != PAGE_KEYS:
            raise _Refused("invalid_page", "The library page is not a usage receipt page")
        if page["event_id"] != traversal.event_id:
            raise _Refused("invalid_page", "The library page belongs to a different Context event")
        status, items, following, error = page["status"], page["items"], page["next_cursor"], page["error"]
        if not isinstance(items, list):
            raise _Refused("invalid_page", "The library page items are not a list")
        if following is not None and (type(following) is not str or not 1 <= len(following) <= MAX_CURSOR):
            raise _Refused("invalid_page", "The library page continuation is not a cursor")
        if status == "unavailable":
            if items or following is not None or not isinstance(error, Mapping) or set(error) != {"code", "message"}:
                raise _Refused("invalid_page", "An unavailable library page must carry only its error")
            code, message = error["code"], error["message"]
            if (type(code) is not str or not 1 <= len(code) <= CODE_LIMIT or type(message) is not str
                    or len(message) > MESSAGE_LIMIT):
                raise _Refused("invalid_page", "The library error is not a bounded code and message")
            return [], None, {"code": code, "message": message}
        if status != "ready" or error is not None:
            raise _Refused("invalid_page", "The library page status and error disagree")
        if len(items) > self.page_size:
            raise _Refused("invalid_page", "The library page has more receipts than were requested")
        if following is not None:
            if not items:
                raise _Refused("invalid_page", "An empty library page cannot claim a continuation")
            if following == traversal.cursor or following in traversal.seen:
                raise _Refused("invalid_page", "The library page repeated a continuation cursor")
        for item in items:
            if not isinstance(item, Mapping) or item.get("event_id") != traversal.event_id:
                raise _Refused("invalid_page", "A library receipt belongs to a different Context event")
        return items, following, None

    def _unavailable(self, traversal: _Traversal, code: str, message: str, counts: dict) -> tuple[str, dict, dict]:
        """Record the bounded diagnostic and let the scan move on; if even that cannot be saved, say so explicitly."""
        error = _bounded(code, message)
        try:
            with self.database.transaction() as uow:
                context_usage.record_scan(uow, driver_id=self.driver_id, input_id=traversal.input_id,
                                          context_event_id=traversal.event_id, status="unavailable", error=error)
        except Exception as failure:
            raise CollectorStorageError(
                f"The usage scan of Context event {traversal.event_id} could not be recorded ({type(failure).__name__})") from failure
        return "unavailable", error, counts
