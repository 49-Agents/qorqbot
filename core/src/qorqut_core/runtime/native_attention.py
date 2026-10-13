"""Monitor-owned native attention; operation reads copy bounded cached data."""

from datetime import datetime
import json
import sqlite3
import threading
import time

from ..adapters.attention import classify_attention
from ..adapters.types import AdapterError
from ..agents.registry import get_row, local_agent_ids
from ..contracts import CoreError
from ..runner.models import RunnerError
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from .models import NativeAttentionView
from .owners import _active


def _identity(owner):
    if owner is None:
        return None
    return tuple(owner[key] for key in ("installation_id", "agent_id", "conversation_id",
        "owner_id", "terminal_id", "recovery_epoch", "environment_generation", "boot_id",
        "run_generation", "provider", "cli_version", "native_session_id", "process_json"))


def _normalized(payload):
    return {**payload, "attention": {k: v for k, v in payload["attention"].items() if k != "observed_at"}}


class NativeAttention:
    def __init__(self, application):
        self.app, self.db = application, application.db
        self._lock = threading.Lock()
        self._cache = {}

    def view(self, tx, agent_id):
        """SQL and memory only, including exact owner and recovery fencing."""
        owner = _active(tx, agent_id)
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        with self._lock:
            cached = self._cache.get(str(agent_id))
        if cached is None or owner is None:
            return NativeAttentionView()
        identity, view, observed = cached
        age = (datetime.fromisoformat(self.db.clock()) - datetime.fromisoformat(owner["last_observed_at"])).total_seconds()
        if (identity != _identity(owner) or owner["recovery_epoch"] != epoch
                or owner["observation"] != "live" or not 0 <= age <= 5
                or time.monotonic() - observed > 5):
            return NativeAttentionView()
        return view

    def tick(self):
        """A finite snapshot of local agents; an error for one never skips the others."""
        assert_external_io_allowed()
        with self.db.read() as tx:
            agents = local_agent_ids(tx)
        for agent_id in agents:
            try:
                self.tick_agent(agent_id)
            except (CoreError, AdapterError, RunnerError, OSError, sqlite3.Error):
                pass  # The old observation ages out; other agents continue.

    def tick_agent(self, agent_id):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                agent = dict(get_row(tx, agent_id))
                owner = _active(tx, agent["id"])
            view = NativeAttentionView(owner_id=owner["owner_id"] if owner else None,
                                       terminal_id=owner["terminal_id"] if owner else None)
            observed = time.monotonic()
            if owner is not None:
                try:
                    observed_owner, snapshot = self.app.runtime_input.attention_snapshot(agent["id"])
                    if _identity(observed_owner) != _identity(owner):
                        raise CoreError("target_changed", "Native attention owner changed")
                    verdict = classify_attention(owner["provider"], owner["cli_version"], snapshot)
                    view = NativeAttentionView(**verdict.__dict__, observed_at=self.db.clock(),
                        owner_id=owner["owner_id"], terminal_id=owner["terminal_id"])
                except CoreError:
                    # A failed capture never claims that the prompt was answered.
                    pass
            with self.db.transaction() as tx:
                current = _active(tx, agent["id"])
                epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
                if (_identity(current) != _identity(owner) or current is not None
                        and (current["observation"] != "live" or current["recovery_epoch"] != epoch)):
                    owner = current
                    view = NativeAttentionView(owner_id=owner["owner_id"] if owner else None,
                                               terminal_id=owner["terminal_id"] if owner else None)
                payload = {"recovery_epoch": epoch, "attention": view.model_dump(mode="json")}
                prior = tx.connection.execute("SELECT payload_json FROM events WHERE agent_id=? "
                    "AND kind='native.attention_changed' ORDER BY event_seq DESC LIMIT 1", (agent["id"],)).fetchone()
                if prior is None or _normalized(json.loads(prior[0])) != _normalized(payload):
                    append_event(tx, EventInput(kind="native.attention_changed", agent_id=agent["id"],
                        actor_id=agent["actor_id"], payload=payload))
            with self._lock:
                self._cache[agent["id"]] = _identity(owner), view, observed

    def close(self):
        with self._lock:
            self._cache = {}
