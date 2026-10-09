"""One durable inbox owner per agent. No launch, delivery, or hook transport."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from ..adapters.capabilities import compatibility
from ..agents.registry import get_row
from ..app import Change, Operation
from ..contracts import CoreError, canonical
from ..runner.models import ProcessFingerprint
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from .models import CurrentOwner, NativeAttentionView, OwnerCandidate, OwnerConnection, ProcessObservation, RuntimeGet, RuntimeView, SessionConflict
from .process import observe_process as local_observation


def _installation(tx):
    return tuple(tx.connection.execute(
        "SELECT installation_id,recovery_epoch FROM installation WHERE singleton=1").fetchone())


def _active(tx, agent_id):
    row = tx.connection.execute("SELECT * FROM session_owners WHERE agent_id=? AND state='active'",
                                (str(agent_id),)).fetchone()
    return dict(row) if row is not None else None


def _binding(candidate):
    return {**candidate.model_dump(mode="json", exclude={"process"}),
            "boot_id": str(candidate.process.boot_id), "process_json": canonical(candidate.process)}


class SessionOwners:
    def __init__(self, application, *, observe_process=None):
        self.app, self.db = application, application.db
        self.observe_process = observe_process or (lambda process: local_observation(process, clock=self.db.clock))

    def operations(self):
        return (Operation("runtime_get", RuntimeGet, False, self.get, RuntimeView),)

    def _agent(self, tx, agent_id, *, admission=False):
        agent = get_row(tx, agent_id)
        if admission and agent["lifecycle"] in {"archiving", "archived"}:
            raise CoreError("invalid_transition", "Agent is not admitting session connections")
        host = tx.connection.execute("SELECT onbash_agent_id FROM installation WHERE singleton=1").fetchone()[0]
        if agent["kind"] != "host" or agent["id"] != host:
            raise CoreError("unsupported_capability", "Local session ownership supports only the reserved onbash agent")
        actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?", (agent["actor_id"],)).fetchone()
        if actor is None or tuple(actor) != ("agent", agent["id"]):
            raise CoreError("target_changed", "Registered agent actor binding changed")
        return agent

    def _observe(self, process):
        assert_external_io_allowed()
        try:
            return ProcessObservation.model_validate(self.observe_process(process))
        except Exception:
            # The observer is an external boundary. Never log its arbitrary error
            # text, release its claim, or bring down the monitor on inspection failure.
            return ProcessObservation(state="unknown", observed_at=self.db.clock(), reason="process_inspection_failed")

    def _event(self, tx, row, kind, reason, *, actor_id=None, operation_id=None, **extra):
        payload = {key: row[key] for key in (
            "owner_id", "agent_id", "conversation_id", "terminal_id", "recovery_epoch",
            "environment_generation", "boot_id", "run_generation", "provider", "cli_version", "native_session_id")}
        payload.update(run_id=row["owner_id"], reason=reason, **extra)
        append_event(tx, EventInput(kind=kind, actor_id=actor_id or row["connected_by"],
            agent_id=row["agent_id"], operation_id=operation_id, payload=payload))

    def _apply_observation(self, tx, row, observation):
        tx.require_write()
        if observation.state != row["observation"]:
            self._event(tx, row, "session.observation", observation.reason,
                        previous=row["observation"], observation=observation.state, observed_at=observation.observed_at)
        closed = observation.state == "exited"
        tx.connection.execute("""UPDATE session_owners SET observation=?,last_observed_at=?,
            state=?,closed_at=?,close_reason=? WHERE owner_id=?""",
            (observation.state, observation.observed_at, "closed" if closed else "active",
             observation.observed_at if closed else None, observation.reason if closed else None, row["owner_id"]))
        if closed:
            self._event(tx, row, "session.closed", observation.reason, observed_at=observation.observed_at)

    def connect(self, candidate: OwnerCandidate) -> OwnerConnection:
        try:
            candidate = OwnerCandidate.model_validate(candidate)
        except ValidationError as exc:
            raise CoreError("invalid_argument", "Invalid session owner candidate") from exc
        if not compatibility(candidate.provider, candidate.cli_version).known_preparation_contract:
            raise CoreError("unsupported_capability", "Session provider/version has no supported profile")
        binding = _binding(candidate)
        for _ in range(3):
            with self.db.read() as tx:
                installation = _installation(tx)
                if installation != (str(self.app.settings.installation_id), str(candidate.recovery_epoch)):
                    raise CoreError("target_changed", "Session candidate belongs to another installation epoch")
                self._agent(tx, candidate.agent_id, admission=True)
                prior = _active(tx, candidate.agent_id)
            same = prior is not None and all(prior[key] == value for key, value in binding.items())
            observed = self._observe(candidate.process)
            incumbent = None
            if prior is not None:
                incumbent = observed if same else self._observe(ProcessFingerprint.model_validate_json(prior["process_json"]))
            error, result = None, None
            with self.db.transaction() as tx:
                if _installation(tx) != installation or _active(tx, candidate.agent_id) != prior:
                    continue
                agent = self._agent(tx, candidate.agent_id, admission=True)
                if prior is not None and prior["recovery_epoch"] != installation[1]:
                    raise CoreError("unavailable", "Existing connection belongs to an unverified recovery epoch")
                if prior is not None:
                    self._apply_observation(tx, prior, incumbent)
                if observed.state == "unknown":
                    error = CoreError("unavailable", "Candidate connection could not be verified", retryable=True)
                elif incumbent is not None and incumbent.state == "unknown":
                    error = CoreError("unavailable", "Existing connection could not be verified; its ownership is retained", retryable=True)
                elif observed.state == "exited":
                    error = CoreError("not_ready", "Candidate process has exited")
                elif same:
                    result = OwnerConnection(outcome="reconnected", owner_id=prior["owner_id"], run_generation=prior["run_generation"])
                elif incumbent is not None and incumbent.state == "live":
                    error = SessionConflict()
                else:
                    generation = tx.connection.execute(
                        "SELECT coalesce(max(run_generation),0) FROM session_owners WHERE agent_id=?",
                        (str(candidate.agent_id),)).fetchone()[0]
                    if generation == 2**63 - 1:
                        raise CoreError("storage_blocked", "Session run generation exhausted")
                    row = {**binding, "owner_id": str(self.db.id_factory()), "installation_id": installation[0],
                           "conversation_id": agent["conversation_id"], "run_generation": generation + 1,
                           "connected_by": agent["actor_id"], "connected_at": self.db.clock(),
                           "state": "active", "observation": "live", "last_observed_at": observed.observed_at,
                           "closed_at": None, "close_reason": None}
                    columns = ",".join(row)
                    tx.connection.execute(f"INSERT INTO session_owners ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
                    self._event(tx, row, "session.connected", "process_verified", observed_at=observed.observed_at)
                    result = OwnerConnection(outcome="connected", owner_id=row["owner_id"], run_generation=generation + 1)
                self.db.fault("session_before_commit")
            # Commit observations even when admission fails. No conflict event is
            # emitted for polls and no public operation receipt is manufactured.
            if error is not None:
                raise error
            return result
        raise CoreError("not_ready", "Session ownership changed during observation; retry registration", retryable=True)

    def release(self, owner_id: UUID, process: ProcessFingerprint) -> bool:
        """True only for a verified exit applied to the exact active binding."""
        try:
            owner_id, process = UUID(str(owner_id)), ProcessFingerprint.model_validate(process)
        except (ValueError, ValidationError) as exc:
            raise CoreError("invalid_argument", "Invalid session release binding") from exc
        with self.db.read() as tx:
            installation = _installation(tx)
            found = tx.connection.execute("SELECT * FROM session_owners WHERE owner_id=? AND state='active'", (str(owner_id),)).fetchone()
            row = dict(found) if found else None
            if row is None or row["process_json"] != canonical(process) or row["recovery_epoch"] != installation[1]:
                return False
            self._agent(tx, row["agent_id"])
        observed = self._observe(process)
        with self.db.transaction() as tx:
            if _installation(tx) != installation or _active(tx, row["agent_id"]) != row:
                return False
            self._apply_observation(tx, row, observed)
        return observed.state == "exited"

    def reconcile(self):
        """A finite sequential snapshot; every observation is fenced on application."""
        with self.db.read() as tx:
            installation = _installation(tx)
            rows = [dict(row) for row in tx.connection.execute(
                "SELECT s.* FROM session_owners s JOIN agents a ON a.id=s.agent_id "
                "JOIN installation i ON i.onbash_agent_id=a.id "
                "WHERE s.state='active' AND a.kind='host' AND s.recovery_epoch=i.recovery_epoch")]
        for row in rows:
            observed = self._observe(ProcessFingerprint.model_validate_json(row["process_json"]))
            with self.db.transaction() as tx:
                if _installation(tx) == installation and _active(tx, row["agent_id"]) == row:
                    self._apply_observation(tx, row, observed)

    def retire_for_recovery(self, tx, context, operation_id):
        """Called only inside the existing recovery activation transaction."""
        tx.require_write()
        now = self.db.clock()
        for row in tx.connection.execute("SELECT * FROM session_owners WHERE state='active'").fetchall():
            tx.connection.execute("UPDATE session_owners SET state='retired',closed_at=?,close_reason='recovery_activated' WHERE owner_id=?",
                                  (now, row["owner_id"]))
            self._event(tx, row, "session.retired", "recovery_activated", actor_id=context.actor_id,
                        operation_id=operation_id, recovery_epoch_after=_installation(tx)[1])

    def get(self, tx, args, context, operation_id):
        get_row(tx, args.agent_id)
        observations = {
            "native_setup": self.app.native_setup.view(args.agent_id) if self.app.native_setup is not None else None,
            "native_capture": self.app.native_collector.view(args.agent_id) if self.app.native_collector is not None else None,
            "attention": self.app.native_attention.view(tx, args.agent_id),
        }
        row = _active(tx, args.agent_id)
        if row is None:
            return Change(RuntimeView(agent_id=args.agent_id, status="disconnected", current_owner=None,
                                      observed_at=None, **observations))
        age = (datetime.fromisoformat(self.db.clock()) - datetime.fromisoformat(row["last_observed_at"])).total_seconds()
        connected = row["observation"] == "live" and 0 <= age <= 5 and row["recovery_epoch"] == _installation(tx)[1]
        if not connected:
            observations["attention"] = NativeAttentionView()
        owner = CurrentOwner(**{key: row[key] for key in CurrentOwner.model_fields if key != "run_id"}, run_id=row["owner_id"])
        return Change(RuntimeView(agent_id=args.agent_id, status="connected" if connected else "unknown",
                                  current_owner=owner, observed_at=row["last_observed_at"], **observations))
