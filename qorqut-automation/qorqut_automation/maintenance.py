"""Bounded housekeeping that rides on the one existing background service's tick (``BackgroundService(before_tick=...)``).

There is no thread, loop or service of this extension's own. Each tick calls :meth:`Maintenance.tick`, which is cheap when nothing is
due (an in-memory clock check) and otherwise does one short, writer-admitted transaction:

* reconciliation (about every ``RECONCILE_SECONDS``): delivers a saved failure or unconfirmed-outcome notice that never reached a
  required delivery for the current Ordubashy (an earlier outage, a crash between the engine's failure and ours);
* script retention (about every ``RECONCILE_SECONDS``, outside any transaction): a queued run that has not yet frozen its own snapshot
  gets the exact admitted script version kept through the Agentic snapshot staging, so a later edit changes future runs only. Retained
  copies and run snapshots are recovery artifacts and are never pruned here;
* settlement of the external steps a *closed* run left unresolved (about every ``RECONCILE_SECONDS``, bounded): the same canonical-record
  reconciliation a requested stop uses, so a successor is held back only while an effect really is unresolved. A step that stays
  unresolved is stamped with its check time in its debug metadata and the oldest check goes first, so five persistent waits cannot starve
  the rest; a wait that says it is not due (the rare provider recheck) is not visited early;
* ordinary diagnostic-log expiry (about every ``CLEAN_SECONDS``, again at once while a full batch shows more is waiting): the
  ``debug_json`` of a *closed* step older than ``LOG_DAYS`` days is cleared and ``log_expired_at`` records it. Run snapshots, scripts,
  request bindings, results, errors, effect evidence, waits and recovery state are never touched here, so nothing needed to reconcile or
  explain an effect can expire.

Failures are left for the next tick; the service records the error. A closed maintenance fence just defers the work."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from qorqut_operations.contracts import DomainError

from . import cancellation, failures

RECONCILE_SECONDS = 30
RECONCILE_BATCH = 20
RETAIN_BATCH = 5
SETTLE_BATCH = 5
CLEAN_SECONDS = 3600
CLEAN_BATCH = 200
LOG_DAYS = 30


class Maintenance:
    def __init__(self, extension):
        self.ext = extension
        self._reconcile_at = 0.0
        self._retain_at = 0.0
        self._clean_at = 0.0
        self._settle_at = 0.0

    def retain_scripts(self):
        """Keep the admitted script version of queued runs that have not yet frozen their own snapshot. Bounded; all file work happens
        outside any database transaction, and nothing is ever removed."""
        ext = self.ext
        with ext.runtime.database.read() as uow:
            queued = {row["automation_id"] for row in ext.guard.unstaged_runs(uow, None, RETAIN_BATCH)}
        for automation_id in queued:
            ext.program.retain(automation_id)

    def settle_closed_runs(self):
        """Reconcile the unresolved external steps of runs that already ended, against their canonical records only (no child, no new
        call): a pending decision of an ended run is closed, a queued provider job is followed to its real outcome, an unconfirmed
        outcome keeps its visible recovery wait. Bounded, and rotating: each examined step is stamped with its check time, so steps that stay
        unresolved take turns instead of occupying every pass. Each step is settled in its own short writer-admitted units."""
        cancellation.sweep(self.ext, SETTLE_BATCH)

    def tick(self):
        ext = self.ext
        if ext.runtime is None:
            return
        now = time.monotonic()
        try:
            if now >= self._retain_at:
                self._retain_at = now + RECONCILE_SECONDS
                self.retain_scripts()
            if now >= self._reconcile_at:
                self._reconcile_at = now + RECONCILE_SECONDS
                with ext.writing() as uow:
                    failures.reconcile(ext.runtime, uow, limit=RECONCILE_BATCH)
            if now >= self._clean_at:
                self._clean_at = now + CLEAN_SECONDS
                with ext.writing() as uow:
                    expired = self.expire_logs(uow)
                if expired >= CLEAN_BATCH:
                    self._clean_at = 0.0         # a full batch: more is waiting, continue on the next tick
            if now >= self._settle_at:
                self._settle_at = now + RECONCILE_SECONDS
                self.settle_closed_runs()
        except DomainError as exc:
            if exc.code != "maintenance":
                raise

    @staticmethod
    def expire_logs(uow, *, days=LOG_DAYS, limit=CLEAN_BATCH) -> int:
        """Expire the ordinary diagnostic log of up to ``limit`` closed steps older than ``days``, oldest first. A step is marked
        whether or not it had a log, so the queue always drains and no old step can starve a newer one."""
        uow.require_write()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = uow.connection.execute(
            "SELECT id FROM automation_steps WHERE closed_at IS NOT NULL AND log_expired_at IS NULL AND closed_at < ? "
            "ORDER BY closed_at LIMIT ?", (cutoff, limit)).fetchall()
        for row in rows:
            uow.connection.execute("UPDATE automation_steps SET debug_json='{}', log_expired_at=? WHERE id=? AND log_expired_at IS NULL",
                                   (now, row["id"]))
        return len(rows)
