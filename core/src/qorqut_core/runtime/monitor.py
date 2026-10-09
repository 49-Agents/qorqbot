"""Explicit one-thread session observation; construction and imports are passive."""

import logging
import sqlite3
import threading

from ..adapters.types import AdapterError
from ..contracts import CoreError
from ..runner.models import RunnerError

_LOG = logging.getLogger(__name__)


class SessionMonitor:
    def __init__(self, session_owners, *, native_setup=None, native_collector=None, native_attention=None):
        self.session_owners = session_owners
        self.native_setup, self.native_collector = native_setup, native_collector
        self.native_attention = native_attention
        self._stop = threading.Event()
        self._healthy = threading.Event()
        self._thread = threading.Thread(target=self._run, name="qorqut-sessions", daemon=False)

    @property
    def ready(self):
        return self._thread.is_alive() and self._healthy.is_set() and not self._stop.is_set()

    def start(self):
        with self.session_owners.db.lock.use():
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.session_owners.reconcile()
                except CoreError as exc:
                    self._healthy.clear()
                    if not exc.error.retryable:
                        raise
                else:
                    self._healthy.set()
                for service in (self.native_setup, self.native_attention, self.native_collector):
                    if service is not None:
                        try:
                            service.tick()
                        except (CoreError, AdapterError, RunnerError, OSError, sqlite3.Error):
                            # A bounded setup/source hold must not stop other work.
                            # The service owns its observations; old views expire.
                            pass
                self._stop.wait(1)
        except BaseException:
            # No observer/process content in logs or thread exception hooks.
            _LOG.error("session_monitor_stopped")
        else:
            # Only an orderly stop reaches this pass, after the current iteration
            # has settled. Keep collection on its owning thread and its usual
            # finite budgets; shutdown neither prepares nor stops a native CLI.
            try:
                self.session_owners.reconcile()
                if self.native_collector is not None:
                    self.native_collector.tick()
            except BaseException:
                _LOG.error("session_monitor_final_collection_failed")
        finally:
            self._healthy.clear()
            for service in (self.native_setup, self.native_attention, self.native_collector):
                if service is not None:
                    try:
                        service.close()
                    except BaseException:
                        _LOG.error("native_observation_close_failed")
