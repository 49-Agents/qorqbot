"""Trusted post-commit reconciliation, separate from agents and their routines.

Construction starts no thread. Only explicit service startup starts this worker;
its inputs are registered SQLite intents, never commands supplied by an agent.
Stopping the worker leaves existing native terminals and browsers running.
"""

import logging
import threading
import time

from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError
from .config import business_identity


class SandboxAdministration:
    """Local operator interface; deliberately not registered in HTTP or MCP."""

    def __init__(self, lifecycle):
        self.lifecycle = lifecycle

    def status(self, business_id):
        return self.lifecycle.status(business_id)

    def ensure(self, business_id):
        with self.lifecycle.database.read() as uow:
            Application.ensure_writes_open(uow)
        return self.lifecycle.ensure_business_environment(business_id)

    def request(self, business_id, action, *, expected_revision):
        business_identity(business_id)
        if action not in {"stop", "start", "replace"}:
            raise DomainError("validation_error", "Unsupported sandbox administration action")
        # Persist the exact operator request before any external action. A crash
        # after this commit is recovered by the same post-commit worker.
        with self.lifecycle.database.transaction() as uow:
            Application.ensure_writes_open(uow)
            if action == "replace":
                row = self.lifecycle.state.request_replacement(uow, business_id,
                                                               expected_revision=expected_revision)
            else:
                row = self.lifecycle.state.request_state(uow, business_id,
                    desired_state="stopped" if action == "stop" else "running",
                    expected_revision=expected_revision)
        return row

    def rollback_to_host(self, business_id, *, expected_revision):
        """Trusted local operator action; no automatic host fallback exists."""
        with self.lifecycle.database.read() as uow:
            Application.ensure_writes_open(uow)
        return self.lifecycle.rollback_to_host(business_id, expected_revision=expected_revision)


class SandboxWorker:
    """One bounded loop with per-business retry delays and periodic health checks.

    The lifecycle's filesystem lock serializes external effects across multiple
    service processes. Local scheduling state only avoids polling Docker too
    often; losing it never loses a provisioning intent or changes its meaning.
    """

    def __init__(self, lifecycle, *, require_authority, retry_seconds=30,
                 health_seconds=300, clock=time.monotonic):
        if not 1 <= retry_seconds <= 3600 or not retry_seconds <= health_seconds <= 86400:
            raise ValueError("Sandbox worker intervals must be bounded")
        self.lifecycle, self.require_authority = lifecycle, require_authority
        self.retry_seconds, self.health_seconds, self.clock = retry_seconds, health_seconds, clock
        self._stop, self._guard, self._tick = threading.Event(), threading.Lock(), threading.Lock()
        self._thread, self._due = None, {}

    def run_once(self):
        if not self._tick.acquire(blocking=False):
            return []
        try:
            self.require_authority()
            with self.lifecycle.database.read() as uow:
                Application.ensure_writes_open(uow)
                # Operational reconciliation must not inherit the presentation
                # repository's default 50-row page limit.
                rows = [dict(row) for row in uow.connection.execute(
                    "SELECT * FROM business_sandboxes ORDER BY created_at, business_id")]
            results = []
            current_ids = {row["business_id"] for row in rows}
            self._due = {key: value for key, value in self._due.items() if key in current_ids}
            for row in rows:
                if self._stop.is_set():
                    break
                identity = row["business_id"]
                # Revisions from an explicit stop/start/replacement or creation
                # bypass a previous health/retry delay.
                previous = self._due.get(identity)
                if previous and previous[0] == row["revision"] and self.clock() < previous[1]:
                    continue
                try:
                    self.require_authority()
                    result = self.lifecycle.ensure_business_environment(identity)
                except DomainError as error:
                    if error.code in {"maintenance", "authority_not_ready", "authority_invalid"}:
                        raise
                    # No exception text, URLs, output, or environment values in logs.
                    logging.getLogger(__name__).warning("Sandbox reconciliation needs retry for business %s", identity)
                    result = self.lifecycle.status(identity)
                if result is not None:
                    delay = self.health_seconds if result["state"] in {"ready", "stopped"} else self.retry_seconds
                    self._due[identity] = (result["revision"], self.clock() + delay)
                    results.append(result)
            return results
        finally:
            self._tick.release()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except DomainError:
                logging.getLogger(__name__).warning("Sandbox reconciliation paused by installation authority")
            except Exception:
                # Keep other businesses retryable after an unexpected adapter
                # failure, without exposing private provider diagnostics.
                logging.getLogger(__name__).error("Sandbox reconciliation encountered an internal failure")
            self._stop.wait(min(self.retry_seconds, 5))

    def start(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="qorqut-sandbox-provisioner", daemon=True)
            self._thread.start()

    def stop(self, *, timeout=5):
        if not 0 <= timeout <= 60:
            raise ValueError("Sandbox worker shutdown timeout must be bounded")
        with self._guard:
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Sandbox reconciliation is still in flight")
