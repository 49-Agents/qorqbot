"""Passive D40 composition of admitted discovery, raw capture and projection.

The monitor owns tick/close and the local SQLite connections. Read-side views
copy immutable memory only. Saved owners authorize history independently of
process liveness; capture receipts, archive lineage and parser checkpoints
remain the only durable progress. This service neither launches nor inputs a CLI.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
import os
import sqlite3
import stat
import threading
import time

from ..adapters.discovery import discover
from ..adapters.native_records import PARSER_VERSION
from ..adapters.types import ManagedRoots, NativeBinding, RunBinding as AdapterRun
from ..agents.registry import reserved
from ..agents.workspace import child_directory, same_child
from ..contracts import CoreError, Invocation
from ..conversation.native import NativeProjector
from ..storage.database import assert_external_io_allowed
from ..transcripts.native_capture import NativeCapture, NativeCaptureError
from ..transcripts.spool import CaptureError, RunBinding
from .native_models import NativeCollectionView

_BYTES = 1048576
_LIMIT = 16
_OPERATIONAL = (CoreError, CaptureError, OSError, sqlite3.Error, ValueError)
_RECOVERY_ERRORS = {"archive_recovery_held", "archive_recovery_changed"}
_DISCOVERY_LIMITS = {"child_association_contract_unverified", "attachment_coverage_partial",
                     "bound_native_source_not_found", "managed_source_unavailable:FileNotFoundError",
                     "incomplete_metadata_record", "metadata_record_too_large", "invalid_metadata_record"}


class _RecoveryHeld(CoreError):
    def __init__(self):
        super().__init__("not_ready", "Native collection requires recovery review")


def _binding(owner):
    return RunBinding(**{name: owner["owner_id" if name == "run_id" else name]
                         for name in RunBinding.__dataclass_fields__})


def _reason(error):
    if isinstance(error, _RecoveryHeld) or (
            isinstance(error, NativeCaptureError) and str(error) in _RECOVERY_ERRORS):
        return "recovery_review"
    return "collection_blocked"


def _round_robin(queue, keys, priority=(), limit=_LIMIT):
    """Reserve at least half the finite batch for the continuing historical ring."""
    keys = tuple(dict.fromkeys(keys))
    wanted = set(keys)
    retained = [key for key in queue if key in wanted]
    known = set(retained)
    queue.clear()
    queue.extend(retained)
    queue.extend(key for key in keys if key not in known)
    selected = list(dict.fromkeys(key for key in priority if key in wanted))[:limit // 2]
    for _ in range(len(queue)):
        if len(selected) == limit:
            break
        key = queue.popleft()
        queue.append(key)
        if key not in selected:
            selected.append(key)
    return selected


class NativeCollector:
    def __init__(self, application):
        self.app, self.db = application, application.db
        self._enabled = bool(application.settings.local_terminal
                             and application.settings.local_terminal.native_programs)
        self._lock = threading.Lock()
        self._cache = None
        self._thread = None
        self._closed = False
        self._capture = None
        self._directories = None
        self._database_identity = None
        self._discovered = set()
        self._rediscover = set()
        self._attempted = {}
        self._owner_queue, self._source_queue = deque(), deque()
        self._epoch_queues = {}
        self._errors, self._discovery_gaps = {}, set()
        self._projector = NativeProjector(application)

    def _own_thread(self):
        thread = threading.get_ident()
        if self._thread is None:
            self._thread = thread
        elif self._thread != thread:
            raise RuntimeError("native_collector_thread_changed")

    def view(self, agent_id) -> NativeCollectionView:
        with self._lock:
            cached = self._cache
        if cached is None:
            return NativeCollectionView(state="unknown", reason="observation_stale")
        agent, view, observed = cached
        if str(agent_id) != agent:
            return NativeCollectionView(state="disabled", reason="unsupported_target",
                                        observed_at=view.observed_at)
        if time.monotonic() - observed > 5:
            return view.model_copy(update={"state": "unknown", "reason": "observation_stale"})
        return view

    def _publish(self, agent_id, **fields):
        view = NativeCollectionView(observed_at=self.db.clock(), **fields)
        with self._lock:
            self._cache = agent_id, view, time.monotonic()

    def close(self):
        self._own_thread()
        self._closed = True
        try:
            if self._capture is not None:
                self._capture.close()
        finally:
            self._capture = None
            with self._lock:
                self._cache = None

    def _snapshot(self):
        with self.db.read() as tx:
            agent = dict(reserved(tx))
            installation = dict(tx.connection.execute("SELECT * FROM installation").fetchone())
            actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?",
                                          (agent["actor_id"],)).fetchone()
            if (agent["kind"] != "host" or tuple(actor or ()) != ("agent", agent["id"])
                    or installation["installation_id"] != str(self.app.settings.installation_id)):
                raise CoreError("target_changed", "Native collection binding changed")
            owners = {row["owner_id"]: dict(row) for row in tx.connection.execute(
                "SELECT * FROM session_owners WHERE agent_id=? ORDER BY run_generation", (agent["id"],))}
        return agent, installation, owners

    def _recovery_guard(self, agent, installation):
        with self.db.read() as tx:
            current = tx.connection.execute("SELECT * FROM installation").fetchone()
            job = tx.connection.execute("SELECT recovery_epoch,wait_reason FROM jobs WHERE job_id=?",
                                        (agent["provisioning_job_id"],)).fetchone()
            if (current["recovery_epoch"] != installation["recovery_epoch"] or job is None
                    or job["recovery_epoch"] != current["recovery_epoch"] or job["wait_reason"] == "recovery_review"):
                raise _RecoveryHeld()

    @contextmanager
    def _owned(self, agent, installation):
        with self.app.workspaces.owned(agent, installation) as (root, workspace_guard):
            with child_directory(root, "workspace") as workspace, child_directory(root, "runtime") as runtime:
                with child_directory(runtime, "home") as home:
                    fds = (workspace, runtime, home)
                    identities = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in fds)
                    for fd in fds:
                        info = os.fstat(fd)
                        if info.st_uid != os.geteuid() or (fd != workspace and info.st_mode & 0o077):
                            raise CoreError("target_changed", "Native directory ownership changed")
                    if self._directories is not None and self._directories != identities:
                        raise CoreError("target_changed", "Native directories changed")

                    def guard(*, recovery=True):
                        workspace_guard()
                        same_child(root, "workspace", workspace)
                        same_child(root, "runtime", runtime)
                        same_child(runtime, "home", home)
                        if self._database_identity is not None:
                            info = os.stat("native.sqlite3", dir_fd=runtime, follow_symlinks=False)
                            if (info.st_dev, info.st_ino) != self._database_identity:
                                raise CoreError("target_changed", "Native capture store changed")
                        if recovery:
                            self._recovery_guard(agent, installation)

                    guard(recovery=False)
                    if self._capture is None:
                        # Precreate only this private file, never repair/adopt links
                        # or a foreign/insecure store. SQLite retains its own schema.
                        try:
                            os.stat("native.sqlite3", dir_fd=runtime, follow_symlinks=False)
                        except FileNotFoundError:
                            self._recovery_guard(agent, installation)
                        fd = os.open("native.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                                     | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=runtime)
                        try:
                            info = os.fstat(fd)
                            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                                    or info.st_nlink != 1 or info.st_mode & 0o077):
                                raise CoreError("target_changed", "Native capture store ownership changed")
                            os.fsync(fd)
                            os.fsync(runtime)
                            self._database_identity = info.st_dev, info.st_ino
                        finally:
                            os.close(fd)
                        base = self.app.settings.data_root / "agents" / agent["id"]
                        roots = ManagedRoots(workspace=base / "workspace", home=base / "runtime/home")
                        self._capture = NativeCapture(base / "runtime/native.sqlite3", roots)
                        self._directories = identities
                        states = self._capture.list_states()
                        self._discovered.update(run.run_id for state in states for run in state.runs)
                        current = {(r.source_id, r.source_epoch) for r in self._capture.list_registered()}
                        self._rediscover.update(run.run_id for state in states
                            if (state.source_id, state.source_epoch) in current and state.observation_required
                            for run in state.runs)
                    guard(recovery=False)
                    yield guard
                    guard(recovery=False)

    @staticmethod
    def _admitted(state, owners, agent, installation):
        return bool(state.runs) and all(
            (owner := owners.get(run.run_id)) is not None and _binding(owner) == run
            and owner["installation_id"] == installation["installation_id"]
            and owner["conversation_id"] == agent["conversation_id"]
            and owner["provider"] == state.provider and owner["native_session_id"] == state.native_session_id
            for run in state.runs)

    def _archive(self, agent, installation, owners):
        """Inventory includes historical epochs and archive-only pending parses."""
        with self.db.read() as tx:
            epochs = { (row["source_id"], row["source_epoch"]): dict(row) for row in tx.connection.execute(
                """SELECT e.*,s.native_session_id,s.parent_source_id,
                    coalesce(c.through_offset,0) AS handled,coalesce(c.scan_offset,0) AS scanned,
                    EXISTS(SELECT 1 FROM native_parse_gaps g WHERE g.source_id=e.source_id
                        AND g.source_epoch=e.source_epoch) AS parse_gap
                    FROM archive_sources s JOIN archive_epochs e USING(source_id)
                    LEFT JOIN native_projections p USING(source_id,source_epoch)
                    LEFT JOIN native_parse_checkpoints c ON c.source_id=e.source_id
                        AND c.source_epoch=e.source_epoch AND c.parser_version=coalesce(p.parser_version,?)
                    WHERE s.agent_id=? AND s.installation_id=? AND s.source_type='native'
                    ORDER BY e.rowid""", (PARSER_VERSION, agent["id"], installation["installation_id"]))}
            for row in tx.connection.execute("""SELECT r.*,l.source_id,l.source_epoch
                    FROM archive_run_sources l JOIN archive_runs r USING(run_id)
                    WHERE r.agent_id=? ORDER BY l.rowid""", (agent["id"],)):
                epoch = epochs.get((row["source_id"], row["source_epoch"]))
                owner = owners.get(row["run_id"])
                if (epoch is None or owner is None or "binding" in epoch or epoch["parent_source_id"] is not None
                        or owner["native_session_id"] != epoch["native_session_id"]
                        or owner["conversation_id"] != agent["conversation_id"]):
                    continue
                binding = _binding(owner)
                if all(row[key] == value for key, value in asdict(binding).items()):
                    # ORDER BY link rowid fixes provenance across resume/restart.
                    epoch["binding"] = binding
        return epochs

    def _discover(self, owners, installation, guard):
        now = time.monotonic()
        eligible = [key for key, owner in owners.items()
                    if (owner["recovery_epoch"] == installation["recovery_epoch"]
                        or (key in self._discovered and key in self._rediscover))
                    and (key not in self._discovered or key in self._rediscover)
                    and now - self._attempted.get(key, float("-inf")) >= 1]
        priority = [key for key in eligible if owners[key]["state"] == "active"]
        priority += [key for key in reversed(eligible) if key not in self._attempted]
        for key in _round_robin(self._owner_queue, eligible, priority):
            self._attempted[key] = now
            owner = owners[key]
            try:
                run = _binding(owner)
                native = NativeBinding(run=AdapterRun(conversation_id=owner["conversation_id"],
                    **{name: value for name, value in asdict(run).items() if name != "installation_id"}),
                    native_session_id=owner["native_session_id"])
                guard()
                result = discover(owner["provider"], owner["cli_version"], self._capture.roots, native)
                guard()
                observations = result.sources
                if owner["recovery_epoch"] != installation["recovery_epoch"]:
                    # Historical lineage permits verified reobservation only
                    # of its already admitted mappings, never fresh old sources.
                    paths = {state.relative_path for state in self._capture.list_states()
                             if run in state.runs and state.provider == owner["provider"]
                             and state.native_session_id == owner["native_session_id"]}
                    observations = tuple(source for source in observations if source.relative_path in paths)
                if any(gap.code not in _DISCOVERY_LIMITS for gap in result.gaps):
                    self._discovery_gaps.add(key)
                else:
                    self._discovery_gaps.discard(key)
                for observation in observations:
                    guard()
                    self._capture.register(observation, run)
                    guard()
                if observations:
                    self._discovered.add(key)
                    self._rediscover.discard(key)
                self._errors.pop(("owner", key), None)
            except _OPERATIONAL as exc:
                self._errors["owner", key] = _reason(exc)

    def _service(self, agent, installation, owners, guard):
        states = self._capture.list_states()
        archived = self._archive(agent, installation, owners)
        source_ids = list(dict.fromkeys([s.source_id for s in states] + [key[0] for key in archived]))
        priority = [s.source_id for s in states if any(owners.get(r.run_id, {}).get("state") == "active"
                                                     for r in s.runs)]
        priority += [key for key in reversed(source_ids) if key not in self._source_queue]
        context = Invocation(actor_id=agent["actor_id"])
        for source_id in _round_robin(self._source_queue, source_ids, priority):
            local = [s for s in states if s.source_id == source_id]
            try:
                guard()
                if local:
                    if not all(self._admitted(s, owners, agent, installation) for s in local):
                        raise CoreError("target_changed", "Native capture admission changed")
                    current = self._capture.state(source_id)
                    polled = self._capture.poll(source_id, max_bytes=_BYTES)
                    guard()
                    # Drain the held descriptor in bounded ticks before verified
                    # rediscovery replaces it. A closed/unreopenable fd makes no progress.
                    if (polled.observation_required
                            and polled.local_captured_offset - current.local_captured_offset < _BYTES):
                        self._rediscover.update(r.run_id for r in polled.runs)
                    self._capture.transfer(source_id, self.app.archive, context, max_chunks=_LIMIT)
                    guard()
                self._errors.pop(("source", source_id), None)
            except _OPERATIONAL as exc:
                self._errors["source", source_id] = _reason(exc)
            # A transfer outage does not hide already durable, independently
            # parseable epochs. Recovery and conflicts are checked by the parser.
            epochs = self._archive(agent, installation, owners)
            pending = [key for key, epoch in epochs.items() if key[0] == source_id
                       and epoch["host_archived_offset"] > epoch["scanned"]]
            for key in epochs:
                if key[0] == source_id and key not in pending:
                    self._errors.pop(("epoch", key), None)
            queue = self._epoch_queues.setdefault(source_id, deque())
            budget = _BYTES
            for key in _round_robin(queue, pending, limit=len(pending)):
                if not budget:
                    break
                queue.remove(key)
                queue.append(key)
                epoch = epochs[key]
                allowance = min(budget, epoch["host_archived_offset"] - epoch["scanned"])
                # Reserve the allowance even on failure: the parser may have
                # committed a prefix before an operational error was raised.
                budget -= allowance
                try:
                    guard()
                    if epoch["registered_recovery_epoch"] != installation["recovery_epoch"]:
                        raise _RecoveryHeld()
                    if "binding" not in epoch:
                        raise CoreError("target_changed", "Native archive has no admitted parsing context")
                    self._projector.consume(context, epoch["binding"], *key, max_bytes=allowance)
                    guard()
                    self._errors.pop(("epoch", key), None)
                except _OPERATIONAL as exc:
                    self._errors["epoch", key] = _reason(exc)

    def _observation(self, agent, installation, owners, *, failure=None):
        states = self._capture.list_states() if self._capture is not None else ()
        epochs = self._archive(agent, installation, owners)
        sources = {s.source_id for s in states} | {key[0] for key in epochs}
        errors = set(self._errors.values()) | ({failure} if failure else set())
        if (any(e["registered_recovery_epoch"] != installation["recovery_epoch"] for e in epochs.values())
                or any(s.last_error in _RECOVERY_ERRORS for s in states)):
            errors.add("recovery_review")
        if any(s.last_error for s in states) or any(e["conflicted"] or "binding" not in e for e in epochs.values()):
            errors.add("collection_blocked")
        if "recovery_review" in errors:
            state, reason = "blocked", "recovery_review"
        elif "collection_blocked" in errors:
            state, reason = "blocked", "collection_blocked"
        elif any(s.gap or s.divergence_reason or (s.checkpoint and s.checkpoint.gap) for s in states) or self._discovery_gaps:
            state, reason = "gap", "capture_gap"
        elif any(e["parse_gap"] for e in epochs.values()):
            state, reason = "gap", "parser_gap"
        elif any(key not in self._discovered for key, owner in owners.items()
                 if owner["recovery_epoch"] == installation["recovery_epoch"]):
            state, reason = "waiting", "waiting_source"
        elif sources:
            state, reason = "collecting", "active"
        else:
            state, reason = "waiting", "waiting_source" if owners else "waiting_session"
        self._publish(agent["id"], state=state, reason=reason, source_count=len(sources),
            pending_archive_bytes=sum(s.local_captured_offset - s.host_archived_offset for s in states),
            pending_parse_bytes=sum(e["host_archived_offset"] - e["handled"] for e in epochs.values()))

    def tick(self):
        self._own_thread()
        if self._closed:
            raise RuntimeError("native_collector_closed")
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            agent, installation, owners = self._snapshot()
            if not self._enabled:
                self._publish(agent["id"], state="disabled", reason="not_configured")
                return
            failure = None
            try:
                if agent["workspace_initialized_at"] is None:
                    self._observation(agent, installation, owners)
                    return
                if not owners and self._capture is None:
                    self._observation(agent, installation, owners)
                    return
                with self._owned(agent, installation) as guard:
                    guard()
                    self._discover(owners, installation, guard)
                    self._service(agent, installation, owners, guard)
            except _OPERATIONAL as exc:
                failure = _reason(exc)
            self._observation(agent, installation, owners, failure=failure)
