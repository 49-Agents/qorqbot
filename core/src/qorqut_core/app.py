"""Explicit assembly of shared callable operations. No server or background tasks."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable
from uuid import UUID

from pydantic import ValidationError

from .config import ConfigGet, ConfigUpdate, ConfigView, InstallationSettings, NewAgentDefaults
from .contracts import Actor, CoreError, Invocation, Model, Mutation, OperationResult, OperationView, canonical, canonical_hash, new_id, utc_now
from .storage.database import Database, HostLock, Transaction, assert_external_io_allowed
from .storage.events import EventInput, EventPage, EventsList, append_event, list_events
from .storage.files import Files
from .storage.jobs import Jobs, job_view


class OperationsGet(Model):
    operation_id: UUID


class RecoveryActivation(Mutation):
    expected_installation_id: UUID
    expected_recovery_epoch: UUID


@dataclass(frozen=True)
class Change:
    data: Model | dict
    event_seq: int | None = None
    outcome: str = "completed"


OperationHandler = Callable[[Transaction, Model, Invocation, UUID | None], Change]


@dataclass(frozen=True)
class Operation:
    name: str
    arguments: type[Model]
    mutation: bool
    handler: OperationHandler
    output: type[Model] | None = None
    prepare: Callable[[Model, Invocation], OperationHandler] | None = None
    description: str | None = None


def register_actor(tx: Transaction, actor: Actor):
    """Trusted registration primitive, called by setup/future identity operations."""
    tx.require_write()
    old = tx.connection.execute("SELECT * FROM actors WHERE actor_id=?", (str(actor.actor_id),)).fetchone()
    binding = (actor.kind, str(actor.agent_id) if actor.agent_id else None)
    if old:
        if (old["kind"], old["agent_id"]) != binding:
            raise CoreError("idempotency_conflict", "Actor identity is already registered differently")
        return
    tx.connection.execute("INSERT INTO actors VALUES (?,?,?,?)",
                          (str(actor.actor_id), *binding, tx.db.clock()))


def _check_actor(tx: Transaction, context: Invocation):
    if tx.connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (str(context.actor_id),)).fetchone() is None:
        raise CoreError("not_found", "Calling actor is not registered")


def _prior_result(tx, operation, context, validated, hashed):
    prior = tx.connection.execute("SELECT * FROM operations WHERE actor_id=? AND name=? AND idempotency_key=?",
        (str(context.actor_id), operation.name, validated.idempotency_key)).fetchone()
    if prior:
        if prior["input_hash"] != hashed:
            raise CoreError("idempotency_conflict", "Key already identifies different validated arguments")
        return OperationResult.model_validate_json(prior["result_json"])
    return None


def _physical_roots(settings: InstallationSettings):
    # Explicit startup work, never part of model validation or SQL transactions.
    assert_external_io_allowed()
    for root in (settings.data_root, settings.source_root, settings.release_root):
        if root.resolve() != root:
            raise CoreError("invalid_argument", "Installation roots cannot cross symlinks")


def _check_settings(settings: InstallationSettings):
    path = settings.data_root / "installation.json"
    if path.is_symlink():
        raise CoreError("invalid_argument", "Installation file cannot be a symbolic link")
    if path.exists():
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise CoreError("invalid_argument", "Installation file is too large")
        saved = InstallationSettings.model_validate_json(raw)
        if saved != settings:
            raise CoreError("target_changed", "Explicit settings differ from the installation file")
        return True
    return False


def load_settings(path: Path) -> InstallationSettings:
    """Read only the caller-specified static installation file."""
    assert_external_io_allowed()
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise CoreError("invalid_argument", "Installation file is too large")
    return InstallationSettings.model_validate_json(raw)


def _save_settings(settings: InstallationSettings, encoded: bytes):
    if _check_settings(settings):
        return
    target = settings.data_root / "installation.json"
    with tempfile.NamedTemporaryFile(dir=settings.data_root, prefix=".installation-", delete=False) as stream:
        staging = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            staging.unlink()
            raise
    try:
        os.replace(staging, target)
        fd = os.open(settings.data_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staging.unlink(missing_ok=True)


class Application:
    def __init__(self, settings: InstallationSettings, database: Database):
        """Pure assembly; caller must explicitly initialize/open storage first."""
        from .agents.registry import Agents
        from .agents.workspace import Workspaces
        from .conversation.ledger import Conversations
        from .conversation.publication import Publisher
        from .files.references import FileReferences
        from .messaging.service import Messaging
        from .messaging.dispatch import MessageDispatcher
        from .runtime import SessionOwners
        from .runtime.native_hooks import NativeHooks
        from .runtime.native_collector import NativeCollector
        from .runtime.native_setup import NativeSetup
        from .runtime.terminals import Terminals
        from .runtime.input_dispatch import RuntimeInput
        from .transcripts.archive import Archive

        self.settings, self.db = settings, database
        self.files = Files(database, settings.data_root)
        self.workspaces = Workspaces(database, settings.data_root)
        self.archive = Archive(database, settings.data_root)
        self.conversations = Conversations(self)
        self.publisher = Publisher(self)
        self.messaging = Messaging(self)
        self.message_dispatcher = MessageDispatcher(self)
        self.file_references = FileReferences(self)
        self.jobs = Jobs(database, (self.files.handler(), *self.workspaces.handlers(),
            self.messaging.handler(), self.publisher.handler()))
        self.files.jobs = self.jobs
        self.workspaces.jobs = self.jobs
        self.agents = Agents(self)
        self.session_owners = SessionOwners(self)
        self.terminals = Terminals(self)
        self.native_hooks = NativeHooks(self)
        self.native_setup = NativeSetup(self)
        self.native_collector = NativeCollector(self)
        self.runtime_input = RuntimeInput(self)
        operations = (
            Operation("config_get", ConfigGet, False, self._config_get, ConfigView),
            Operation("config_update", ConfigUpdate, True, self._config_update, ConfigView),
            Operation("operations_get", OperationsGet, False, self._operations_get, OperationView),
            Operation("events_list", EventsList, False, self._events_list, EventPage),
            *self.agents.operations(),
            *self.messaging.operations(),
            *self.conversations.operations(),
            *self.session_owners.operations(),
            *self.terminals.operations(),
            *self.file_references.operations(),
        )
        self.registry = MappingProxyType({op.name: op for op in operations})

    @staticmethod
    def _database(settings, *, clock, id_factory, fault):
        lock = HostLock(settings.data_root / "locks" / "host.lock")
        return Database(settings.data_root / "state.sqlite3", lock, busy_timeout_ms=settings.busy_timeout_ms,
                        clock=clock, id_factory=id_factory, fault=fault)

    @classmethod
    def initialize(cls, settings: InstallationSettings, initial_defaults: NewAgentDefaults,
                   initial_owner: Actor, *, clock=utc_now, id_factory=new_id, fault=None):
        assert_external_io_allowed()
        encoded = (settings.model_dump_json(indent=2) + "\n").encode("utf-8")
        if len(encoded) > 16384:
            raise CoreError("invalid_argument", "Installation file is too large")
        _physical_roots(settings)
        if initial_owner.kind != "owner":
            raise CoreError("invalid_argument", "Initial actor must be an owner")
        _check_settings(settings)
        db = cls._database(settings, clock=clock, id_factory=id_factory, fault=fault)
        if db.path.exists():
            db.validate(allow_older=True)
        settings.data_root.mkdir(parents=True, exist_ok=True)
        db.lock.acquire()
        try:
            db.check_installation_identity(settings.installation_id)
            _save_settings(settings, encoded)
            db.migrate()
            with db.transaction() as tx:
                old = tx.connection.execute("SELECT * FROM installation").fetchone()
                if old is None:
                    tx.connection.execute("INSERT INTO installation (singleton,installation_id,recovery_epoch,created_at) VALUES (1,?,?,?)",
                        (str(settings.installation_id), str(id_factory()), clock()))
                    defaults = initial_defaults
                    tx.connection.execute("INSERT INTO defaults VALUES (1,1,?,?,?,?,?)",
                        (defaults.cli.provider, defaults.cli.model, defaults.resources.cpu,
                         defaults.resources.memory_mib, defaults.resources.disk_gib))
                    register_actor(tx, initial_owner)
                    append_event(tx, EventInput(kind="installation.initialized", actor_id=initial_owner.actor_id,
                        payload={"installation_id": str(settings.installation_id)}))
                elif old["installation_id"] != str(settings.installation_id):
                    raise CoreError("target_changed", "Database belongs to another installation")
                elif tx.connection.execute("SELECT 1 FROM actors WHERE actor_id=? AND kind='owner'", (str(initial_owner.actor_id),)).fetchone() is None:
                    raise CoreError("target_changed", "Initialization cannot replace the registered owner")
            application = cls(settings, db)
            application.agents.ensure_onbash(Invocation(actor_id=initial_owner.actor_id))
            return application
        except BaseException:
            db.lock.close()
            raise

    @classmethod
    def open(cls, settings: InstallationSettings, *, clock=utc_now, id_factory=new_id, fault=None):
        from .agents.registry import reserved

        assert_external_io_allowed()
        _physical_roots(settings)
        if not _check_settings(settings):
            raise CoreError("not_ready", "Installation must be explicitly initialized")
        db = cls._database(settings, clock=clock, id_factory=id_factory, fault=fault)
        db.validate()  # Incompatible schemas are rejected before lock/WAL writes.
        db.lock.acquire()
        try:
            db.validate()
            with db.read() as tx:
                row = tx.connection.execute("SELECT installation_id FROM installation").fetchone()
                if row is None or row[0] != str(settings.installation_id):
                    raise CoreError("target_changed", "Installation and database identities disagree")
                reserved(tx)
            return cls(settings, db)
        except BaseException:
            db.lock.close()
            raise

    def close(self):
        self.terminals.close()
        self.db.lock.close()

    def local_owner(self) -> UUID:
        """Resolve the sole installation owner for local transport attribution."""
        with self.db.read() as tx:
            owners = tx.connection.execute("SELECT actor_id FROM actors WHERE kind='owner' LIMIT 2").fetchall()
            if len(owners) != 1:
                raise CoreError("invalid_argument", "Local access requires exactly one registered owner")
            return UUID(owners[0]["actor_id"])

    def invoke(self, name: str, context: Invocation, arguments: dict | None = None) -> OperationResult:
        try:
            operation = self.registry.get(name)
            if operation is None:
                raise CoreError("not_found", "Operation is not implemented")
            return self.execute(operation, context, {} if arguments is None else arguments)
        except ValidationError:
            # Validation diagnostics may contain the supplied input. Do not echo it.
            return OperationResult(ok=False, error=CoreError("invalid_argument", "Arguments do not match the operation schema").error)
        except CoreError as exc:
            return OperationResult(ok=False, error=exc.error)

    def execute(self, operation: Operation, context: Invocation, arguments: dict) -> OperationResult:
        """Assembly/internal seam for a concrete operation; no runtime registration."""
        if not isinstance(context, Invocation):
            raise CoreError("invalid_argument", "Attributed invocation context required")
        validated = operation.arguments.model_validate(arguments)
        if operation.mutation and not isinstance(validated, Mutation):
            raise CoreError("invalid_argument", "Mutation schema must require an idempotency key")
        hashed = canonical_hash({"version": 1, "arguments": validated.model_dump(mode="json", exclude={"idempotency_key"})})
        if operation.prepare is not None:
            if not operation.mutation:
                raise CoreError("invalid_transition", "Preparation requires a mutation")
            with self.db.lock.use(), self.db.lock.effects:
                with self.db.read() as tx:
                    _check_actor(tx, context)
                    prior = _prior_result(tx, operation, context, validated, hashed)
                    if prior is not None:
                        return prior
                handler = operation.prepare(validated, context)
                return self._execute_validated(operation, context, validated, hashed, handler)
        return self._execute_validated(operation, context, validated, hashed, operation.handler)

    def _execute_validated(self, operation, context, validated, hashed, handler):
        with self.db.transaction(writable=operation.mutation) as tx:
            _check_actor(tx, context)
            operation_id = None
            if operation.mutation:
                prior = _prior_result(tx, operation, context, validated, hashed)
                if prior is not None:
                    return prior
                operation_id = self.db.id_factory()
            change = handler(tx, validated, context, operation_id)
            data = change.data.model_dump(mode="json") if isinstance(change.data, Model) else change.data
            if operation.output is not None:
                try:
                    data = operation.output.model_validate(data).model_dump(mode="json")
                except ValidationError as exc:
                    raise CoreError("invalid_transition", "Handler produced an invalid operation result") from exc
            result = OperationResult(ok=True, operation_id=operation_id, outcome=change.outcome,
                                     data=data, event_seq=change.event_seq)
            if operation.mutation:
                self.db.fault("operation_before_result")
                epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
                tx.connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                    (str(operation_id), str(context.actor_id), operation.name, validated.idempotency_key,
                     hashed, canonical(result), self.db.clock(), epoch))
                self.db.fault("operation_after_result")
            return result

    def _view(self, tx: Transaction) -> ConfigView:
        row = tx.connection.execute("SELECT * FROM defaults WHERE singleton=1").fetchone()
        installation = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
        return ConfigView(installation_id=installation["installation_id"], recovery_epoch=installation["recovery_epoch"],
            revision=row["revision"], defaults=NewAgentDefaults(cli={"provider": row["cli_provider"], "model": row["model"]},
                resources={"cpu": row["cpu"], "memory_mib": row["memory_mib"], "disk_gib": row["disk_gib"]}))

    def _config_get(self, tx, args, context, operation_id):
        return Change(self._view(tx))

    def _config_update(self, tx, args: ConfigUpdate, context, operation_id):
        before = self._view(tx)
        if before.revision != args.expected_revision:
            raise CoreError("revision_conflict", "Defaults revision changed")
        if before.revision == 2**63 - 1:
            raise CoreError("storage_blocked", "Defaults revision exhausted")
        defaults = args.defaults
        tx.connection.execute("""UPDATE defaults SET revision=revision+1,cli_provider=?,model=?,cpu=?,memory_mib=?,disk_gib=? WHERE singleton=1""",
            (defaults.cli.provider, defaults.cli.model, defaults.resources.cpu, defaults.resources.memory_mib, defaults.resources.disk_gib))
        self.db.fault("config_after_update")
        view = self._view(tx)
        seq = append_event(tx, EventInput(kind="config.updated", actor_id=context.actor_id, operation_id=operation_id,
            payload={"old_defaults": before.defaults.model_dump(mode="json"), "defaults": view.defaults.model_dump(mode="json"), "revision": view.revision}))
        self.db.fault("config_after_event")
        return Change(view, seq)

    def _operations_get(self, tx, args, context, operation_id):
        row = tx.connection.execute("SELECT * FROM operations WHERE operation_id=?", (str(args.operation_id),)).fetchone()
        if row is None:
            raise CoreError("not_found", "Operation not found")
        jobs = [job_view(r) for r in tx.connection.execute("SELECT * FROM jobs WHERE operation_id=? ORDER BY created_at,job_id", (str(args.operation_id),))]
        # Saved result remains unchanged as jobs evolve.
        return Change({"operation_id": row["operation_id"], "name": row["name"], "actor_id": row["actor_id"],
            "created_at": row["created_at"], "recovery_epoch": row["recovery_epoch"],
            "original_result": json.loads(row["result_json"]), "jobs": jobs})

    def _events_list(self, tx, args, context, operation_id):
        return Change(list_events(tx, args))

    def activate_recovery(self, context: Invocation, arguments: RecoveryActivation) -> OperationResult:
        """Explicit restore/import activation ONLY. No backup or public registry entry."""
        with self.db.lock.maintenance():
            operation = Operation("recovery_activate", RecoveryActivation, True, self._activate_recovery)
            return self.execute(operation, context, arguments.model_dump(mode="json"))

    def _activate_recovery(self, tx, args, context, operation_id):
        old = tx.connection.execute("SELECT * FROM installation").fetchone()
        if old["installation_id"] != str(args.expected_installation_id) or old["recovery_epoch"] != str(args.expected_recovery_epoch):
            raise CoreError("target_changed", "Recovery activation requires the expected installation and epoch")
        epoch, now = self.db.id_factory(), self.db.clock()
        if str(epoch) == old["recovery_epoch"]:
            raise CoreError("invalid_transition", "Recovery must allocate a fresh epoch")
        rows = tx.connection.execute("SELECT * FROM jobs WHERE external_effect=1 AND state IN ('queued','waiting','running','uncertain')").fetchall()
        for row in rows:
            tx.connection.execute("""UPDATE jobs SET state='waiting',wait_reason='recovery_review',next_attempt_at=?,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE job_id=?""", (now, now, row["job_id"]))
            self.jobs._event(tx, row, row["state"], "waiting", "recovery_review", actor_id=context.actor_id)
        # Non-external DB-only jobs can safely continue in the new epoch.
        for row in tx.connection.execute("SELECT * FROM jobs WHERE external_effect=0 AND state IN ('queued','waiting','running','uncertain')").fetchall():
            tx.connection.execute("""UPDATE jobs SET recovery_epoch=?,state='queued',lease_owner=NULL,
                lease_token=NULL,lease_until=NULL,wait_reason=NULL,next_attempt_at=NULL,updated_at=?
                WHERE job_id=?""", (str(epoch), now, row["job_id"]))
            self.jobs._event(tx, row, row["state"], "queued", "recovery_epoch_changed", actor_id=context.actor_id)
        tx.connection.execute("UPDATE installation SET recovery_epoch=? WHERE singleton=1", (str(epoch),))
        self.session_owners.retire_for_recovery(tx, context, operation_id)
        self.messaging.hold_for_recovery(tx, context, operation_id)
        self.publisher.hold_for_recovery(tx)
        seq = append_event(tx, EventInput(kind="installation.recovery_activated", actor_id=context.actor_id,
            operation_id=operation_id, payload={"previous_epoch": old["recovery_epoch"], "recovery_epoch": str(epoch), "held_jobs": len(rows)}))
        return Change({"recovery_epoch": str(epoch), "held_jobs": len(rows)}, seq)
