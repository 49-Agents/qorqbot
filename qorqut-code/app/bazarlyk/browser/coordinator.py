"""Explicit business browser selection by any active Global agent.

Ordubashy, workers and Learning Applier may name any active business on
``acquire`` when their current role allows ``browser_turn`` and selects the browser-control
skill; no membership, stored business list or delegation capability is involved. Every later
call resolves the stored handle, never a caller selector. The owner's viewer and durable
workers share the same exclusive queue and ``control.lock``/``lease.json``, so a busy browser
is refused and never taken. The credential and machine registration, identity, role, skill,
operation permission and business state are re-read on every operation, and again on every
CDP request of a leased sandbox command.
"""

import json
import re
import secrets
import time
import uuid

from bazarlyk.bootstrap.browser import SKILL_NAME
from bazarlyk.bootstrap.client import write_json_private
from bazarlyk.service.browser_surfaces import GLOBAL_ROLES
from bazarlyk.transport.auth import current_agent_credential
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from .config import browser_state_directory
from .control import coordinator_lease_ended, locked_state
from .operations import BROWSER_OPERATION

PREFIX = "coordinator:"


def requester_id(principal_id, consumer):
    return PREFIX + principal_id + ":" + consumer


class CoordinatorBrowser:
    def __init__(self, access):
        self.access = access

    @property
    def runtime(self):
        return self.access.runtime

    @property
    def turns(self):
        return self.access.turns

    def authorize(self, actor, business_id=None, *, ordubashy=False):
        """Fresh proof that this call is a current Global agent allowed to drive this active business's browser. Without a
        business it is the same proof for the agent alone; ``ordubashy`` additionally requires the configured Ordubashy."""
        if not actor.registration_id or not actor.credential_id or actor.delegation_assignment_id is not None:
            raise DomainError("forbidden", "Project browser selection requires a registered agent session")
        from qorqut_operations.global_agents import effective_business_ids
        resolver, application = self.runtime.resolver, self.runtime.application
        with self.runtime.database.read() as uow:
            try:
                current_agent_credential(uow.connection, actor, now=time.time())
            except DomainError:
                raise DomainError("forbidden", "Browser selection requires the current credential and machine registration") from None
            row = uow.repository("principals").get(actor.principal.id)
            if row is None or row["kind"] != "agent" or not row["active"]:
                raise DomainError("forbidden", "Browser selection requires an active agent")
            profile, _row = resolver.profile(actor.principal, connection=uow.connection)
            if (profile.scope.kind != "ecosystem" or profile.scope.business_id is not None
                    or profile.data["role"]["id"] not in GLOBAL_ROLES):
                raise DomainError("forbidden", "Explicit project browser selection is limited to active Global agents")
            if business_id is not None and business_id not in effective_business_ids(uow.connection, actor.principal.id):
                raise DomainError("forbidden", "The project is not active for this agent")
            if ordubashy:
                from bazarlyk.management.identity import configured_ordubashy
                if configured_ordubashy(uow, require_codex=False) != actor.principal.id:
                    raise DomainError("forbidden", "Browser selection by id is limited to the configured Ordubashy")
            package = resolver.catalog.get("role", profile.data["role"])
            if SKILL_NAME not in {skill["name"] for skill in package.skills}:
                raise DomainError("forbidden", "The current role does not select browser control")
        # Identity and policy only, never business work: the same operation permission the MCP bridge checks.
        context = resolver.runtime_context(actor.principal, session_registration_id=actor.registration_id)
        descriptor = self.runtime.registry.get(BROWSER_OPERATION)
        with self.runtime.database.read() as uow:
            application.authorize(context, descriptor, (), uow,
                                  namespace=application.namespace(context, descriptor, {"action": "status"}))

    def verify(self, actor, handle, saved):
        """Per CDP request of a leased command: the caller proof plus this handle's exact owning lease and record."""
        self.authorize(actor, saved["business_id"])
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT superseded FROM browser_handles WHERE id=?", (handle,)).fetchone()
            turn = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (saved["turn_id"],)).fetchone()
        if (row is None or row["superseded"] or turn is None or turn["state"] != "owning"
                or turn["lease_id"] != saved["lease_id"] or turn["lease_until"] <= self.turns.clock()):
            raise DomainError("forbidden", "Browser handle no longer owns this project browser")
        lease = self._read_lease(self._directory(saved["business_id"]))
        if (not lease or lease.get("agent_id") != actor.principal.id or lease.get("token") != saved["turn_token"]
                or lease.get("turn_id") != saved["turn_id"] or lease.get("turn_lease_id") != saved["lease_id"]
                or lease.get("instance") != saved["instance"]):
            raise DomainError("forbidden", "Browser handle no longer owns this project browser")

    @staticmethod
    def _key(business_id):
        return "business:" + business_id

    def _directory(self, business_id):
        return browser_state_directory(self.runtime.paths, business_id)

    def _read_lease(self, directory):
        path = directory / "lease.json"
        try:
            lease = json.loads(safe_io.read_bytes(path, limit=8192, private=True)) if path.exists() else None
        except (OSError, ValueError, TypeError) as exc:
            raise DomainError("operation_unavailable", "Browser ownership state needs operator recovery") from exc
        return lease or None

    def acquire(self, actor, business_id, body):
        if body.get("protocol") != "handles":
            raise DomainError("validation_error", "Explicit project browser selection requires protocol=handles")
        try:
            business_id = str(uuid.UUID(business_id))
            consumer = "default" if body.get("consumer_id") is None else str(uuid.UUID(body["consumer_id"]))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("validation_error", "business_id and consumer_id must be UUIDs") from None
        key = self._key(business_id)
        if body.get("browser_key") not in {None, key}:
            raise DomainError("forbidden", "The browser target is fixed by business_id")
        principal, requester = actor.principal, requester_id(actor.principal.id, consumer)
        with self.runtime.application.writer_admission.writer():
            self.runtime.require_authority()
            self.authorize(actor, business_id)
            token = secrets.token_hex(32)
            # Resolve the running browser before taking the turn; a stopped browser holds nothing.
            target = self.access._worker_target(key, principal.id)
            with self.runtime.database.transaction(operation="browser.coordinator.acquire") as uow:
                prior = uow.connection.execute(
                    "SELECT id FROM browser_turns WHERE browser_key=? AND requester_kind='agent' AND requester_id=? "
                    "AND state IN ('queued','owning')", (key, requester)).fetchone()
                turn = self.turns.acquire(uow, browser_key=key, business_id=business_id,
                    parent_principal_id=principal.id, requester_kind="agent", requester_id=requester)
                # A refused repeat acquisition must not release this session's existing turn.
                fresh = prior is None or prior["id"] != turn["id"]
                if turn["state"] != "owning":
                    raise DomainError("busy", "Another task owns this project browser; wait for its turn")
            try:
                handle = self._publish(principal, actor, business_id, consumer, turn, target, token)
            except Exception:
                if fresh:
                    with self.runtime.database.transaction(operation="browser.coordinator.recover") as uow:
                        self.turns.release(uow, turn["id"], parent_principal_id=principal.id,
                            requester_id=requester, reason="project browser acquisition failed")
                raise
        result = {"status": "owning", "handle": handle, "business_id": business_id}
        if turn.get("lease_until"):
            result["lease_until"] = turn["lease_until"]
        return result

    def _publish(self, principal, actor, business_id, consumer, turn, target, token):
        """Write the shared lease, then the handle bound to this exact turn and browser instance."""
        directory = self._directory(business_id)
        with locked_state(directory):
            lease = self._read_lease(directory)
            if lease and lease.get("instance") == target["instance"]:
                mine = lease.get("agent_id") == principal.id and lease.get("turn_id") == turn["id"]
                # Only a coordinator's own record whose turn has ended may be replaced; any other holder's
                # record needs its owner (or the browser's owner) to detach it.
                abandoned = coordinator_lease_ended(self.runtime.database, lease)
                if not (mine or abandoned):
                    raise DomainError("busy", "Another task owns this project browser; wait for detach or ask the owner to recover it")
            write_json_private(directory / "lease.json", {"agent_id": principal.id, "token": token,
                "instance": target["instance"], "turn_id": turn["id"], "turn_lease_id": turn["lease_id"],
                "consumer_id": consumer, "coordinator": True})
        from .handles import binding_for
        while True:
            handle = "b" + secrets.token_hex(6)
            if self.access.native_target(principal.id, handle) is None:  # Never look like a stateless handle.
                break
        saved = {"business_id": business_id, "consumer_id": consumer, "browser_key": self._key(business_id),
                 "turn_id": turn["id"], "lease_id": turn["lease_id"], "turn_token": token, "coordinator": True,
                 "generation": target["generation"], "instance": target["instance"]}
        with self.runtime.database.transaction(operation="browser.coordinator.handle") as uow:
            current = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()
            if current is None or current["state"] != "owning" or current["lease_id"] != turn["lease_id"]:
                raise DomainError("stale_browser_handle", "Browser ownership changed during acquisition; acquire again")
            uow.connection.execute("INSERT INTO browser_handles (id,turn_id,binding_json,arguments_json) VALUES (?,?,?,?)",
                (handle, turn["id"], json.dumps(binding_for(actor), sort_keys=True), json.dumps(saved, sort_keys=True)))
            # A repeated acquire rotates the lease token; retire the former references.
            uow.connection.execute("UPDATE browser_handles SET superseded=1 WHERE turn_id=? AND id<>?", (turn["id"], handle))
        return handle

    def _clear_lease(self, business_id, saved):
        directory = self._directory(business_id)
        with locked_state(directory):
            lease = self._read_lease(directory)
            if (lease and lease.get("token") == saved["turn_token"] and lease.get("turn_id") == saved["turn_id"]
                    and lease.get("turn_lease_id") == saved["lease_id"]):
                write_json_private(directory / "lease.json", {})

    def _current(self, row, saved, principal_id, *, instance):
        """The handle still names the owning lease and, when it will be used, the same browser instance."""
        with self.runtime.database.read() as uow:
            turn = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (saved["turn_id"],)).fetchone()
        if (row["superseded"] or turn is None or turn["state"] != "owning" or turn["lease_id"] != saved["lease_id"]
                or turn["lease_until"] <= self.turns.clock()):
            raise DomainError("stale_browser_handle", "Browser handle expired or was released/replaced; acquire again, and reconcile uncertain actions before retrying")
        if instance:
            target = self.access._worker_target(saved["browser_key"], principal_id)
            if target["instance"] != saved["instance"] or target["generation"] != saved["generation"]:
                raise DomainError("stale_browser_handle", "The project browser was replaced or reconfigured; acquire again")

    def call(self, actor, action, body, row, saved):
        """Run one handle-bound operation after re-proving the coordinator's authority."""
        if action not in {"status", "command", "checkpoint", "renew", "release", "artifact", "reconcile"}:
            raise DomainError("validation_error", "Unsupported browser action")
        principal, business_id = actor.principal, saved["business_id"]
        requester = requester_id(principal.id, saved["consumer_id"])
        turn_id, lease_id = saved["turn_id"], saved["lease_id"]
        self.authorize(actor, business_id)
        if action == "release":
            if row["superseded"]:
                raise DomainError("stale_browser_handle", "Browser handle was replaced by a newer acquisition; use the newer handle")
            with self.runtime.application.writer_admission.writer():
                self.runtime.require_authority()
                with self.runtime.database.transaction(operation="browser.coordinator.release") as uow:
                    result = self.turns.release(uow, turn_id, parent_principal_id=principal.id, requester_id=requester,
                        lease_id=lease_id, reason=body.get("reason") or "coordinator released browser")
                self._clear_lease(business_id, saved)
            return {"turn": result}
        if action == "reconcile":
            # Uncertain work of a lapsed lease is still reconciled by the handle that started it.
            with self.runtime.database.transaction(operation="browser.coordinator.reconcile") as uow:
                settled = self.turns.reconcile_action(uow, turn_id, parent_principal_id=principal.id,
                    requester_id=requester, generation=None, lease_id=lease_id, action_id=body.get("action_id"),
                    outcome=body.get("pending_outcome") or {"status": "reconciled"})
            self._clear_lease(business_id, saved)  # Reconciling ends the turn, so it ends the shared record too.
            return {"action": settled}
        self._current(row, saved, principal.id, instance=action in {"status", "command", "renew", "checkpoint"})
        if action == "status":
            with self.runtime.database.transaction(operation="browser.coordinator.status") as uow:
                return {"turn": self.turns.status(uow, turn_id, parent_principal_id=principal.id, requester_id=requester)}
        if action == "renew":
            with self.runtime.database.transaction(operation="browser.coordinator.renew") as uow:
                return {"turn": self.turns.renew(uow, turn_id, parent_principal_id=principal.id, requester_id=requester,
                    lease_id=lease_id, lease_seconds=body.get("lease_seconds"))}
        if action == "checkpoint":
            with self.runtime.database.transaction(operation="browser.coordinator.checkpoint") as uow:
                return {"turn": self.turns.checkpoint(uow, turn_id, parent_principal_id=principal.id,
                    requester_id=requester, lease_id=lease_id, generation=None, checkpoint=body.get("checkpoint", {}),
                    last_action=body.get("last_action"), pending_outcome=body.get("pending_outcome"),
                    next_action=body.get("next_action"))}
        if action == "artifact":
            with self.runtime.database.transaction(operation="browser.coordinator.artifact") as uow:
                self.turns._owned(uow, turn_id, parent_principal_id=principal.id, requester_id=requester, lease_id=lease_id)
            return self.access._read_artifact(principal.id, turn_id, body)
        token = saved["turn_token"]
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise DomainError("forbidden", "Browser turn token is invalid")
        return self.access._turn_command(actor, body, turn_id=turn_id, lease_id=lease_id, requester=requester,
            generation=None, browser_key=saved["browser_key"], token=token, owner_id=principal.id,
            expected_instance=saved["instance"], expected_generation=saved["generation"],
            recheck=lambda: self.verify(actor, row["id"], saved))
