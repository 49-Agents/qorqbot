"""Small MCP references to persisted, identity-bound browser reservations.

Handles are references, not bearer credentials. The underlying queue remains the
ownership authority; a handle never looks up or adopts a newer lease.
"""
import json
import secrets

from qorqut_operations.contracts import DomainError


_PRIVATE = {"turn_id", "lease_id", "turn_token"}
_SELECTORS = {"consumer_id", "browser_key"}


def binding_for(actor):
    return {"principal_id": actor.principal.id,
            **{key: getattr(actor, key, None) for key in (
                "registration_id", "credential_id", "delegation_binding_id", "delegation_assignment_id", "delegation_generation")}}


def _compact(value, handle=None, *, operation="acquire"):
    result = {key: item for key, item in value.items() if key not in {"turn", "target", "turn_token"}}
    turn = value.get("turn")
    if turn:
        # Command success already has an action outcome. Only surface turn
        # state here when ownership changed or the caller asked about it.
        if operation != "command" or turn["state"] != "owning":
            result["status"] = value.get("status", turn["state"])
        fields = ("lease_until",) if operation == "renew" else ()
        if operation in {"acquire", "status"}:
            fields = ("lease_until", "position", "reason", "checkpoint", "pending_outcome")
        for key in fields:
            if turn.get(key) not in (None, {}, ""):
                result[key] = turn[key]
    if handle and operation == "acquire":
        result["handle"] = handle
    if isinstance(value.get("action"), dict):
        action = value["action"]
        result["action"] = {key: action[key] for key in ("action_id", "state", "error", "reconcile_required") if action.get(key) is not None}
        if "stdout" not in value and action.get("outcome_json"):
            result["action"]["outcome"] = json.loads(action["outcome_json"]) if isinstance(action["outcome_json"], str) else action["outcome_json"]
    for key in ("stderr", "artifacts"):
        if key in result and not result[key]:
            del result[key]
    return result


class BrowserHandles:
    def __init__(self, access):
        self.access = access
        self.database = access.runtime.database

    def call(self, actor, action, body):
        if any(body.get(key) is not None for key in _PRIVATE):
            raise DomainError("validation_error", "Use a browser handle; lease credentials are managed by the server")
        if actor.delegation_assignment_id is None:
            return self._native(actor, action, body)
        if action == "acquire":
            if body.get("handle") is not None:
                raise DomainError("validation_error", "Acquire does not accept an existing handle")
            value = self.access.worker(actor, action, body)
            if value["status"] != "owning":
                return _compact(value)
            turn = value["turn"]
            binding = binding_for(actor)
            saved = {key: body[key] for key in _SELECTORS if body.get(key) is not None}
            saved.update(turn_id=turn["id"], lease_id=turn["lease_id"], turn_token=value["turn_token"])
            handle = "b" + secrets.token_hex(6)
            with self.database.transaction(operation="browser.handle.issue") as uow:
                # Bind the exact receipt, never a newer lease observed after it.
                current = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()
                if (current is None or current["state"] != "owning" or current["lease_id"] != turn["lease_id"]
                        or current["execution_generation"] != actor.delegation_generation):
                    raise DomainError("stale_browser_handle", "Browser ownership changed during acquisition; acquire again")
                uow.connection.execute("INSERT INTO browser_handles (id,turn_id,binding_json,arguments_json) VALUES (?,?,?,?)",
                    (handle, turn["id"], json.dumps(binding, sort_keys=True), json.dumps(saved, sort_keys=True)))
                # A repeated acquire rotates the gateway capability. Retire the
                # former references too, including on configured-browser paths.
                uow.connection.execute("UPDATE browser_handles SET superseded=1 WHERE turn_id=? AND id<>?",
                                       (turn["id"], handle))
            return _compact(value, handle)
        handle = body.get("handle")
        if not isinstance(handle, str):
            raise DomainError("validation_error", "handle is required; acquire the browser first")
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM browser_handles WHERE id=?", (handle,)).fetchone()
            if row is None:
                raise DomainError("forbidden", "Browser handle is unavailable for this caller")
            binding = json.loads(row["binding_json"])
            expected = binding_for(actor)
            # Recovery may inspect/settle a previous generation's uncertain
            # action, but cannot use its handle for commands or lease renewal.
            recovery = (action == "reconcile" and expected["delegation_assignment_id"] is not None
                        and binding["principal_id"] == expected["principal_id"]
                        and binding["delegation_assignment_id"] == expected["delegation_assignment_id"])
            if binding != expected and not recovery:
                raise DomainError("forbidden", "Browser handle is unavailable for this caller")
            saved = json.loads(row["arguments_json"])
            if any(body.get(key) is not None and body[key] != saved.get(key) for key in _SELECTORS):
                raise DomainError("forbidden", "Browser handle selectors cannot be changed")
            if action != "reconcile":
                current = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (row["turn_id"],)).fetchone()
                if (row["superseded"] or current is None or current["state"] != "owning"
                        or current["lease_id"] != saved["lease_id"]
                        or current["execution_generation"] != expected["delegation_generation"]
                        or current["lease_until"] <= self.access.turns.clock()):
                    raise DomainError("stale_browser_handle", "Browser handle expired or was released/replaced; acquire again, and reconcile uncertain actions before retrying")
        arguments = {**body, **saved}
        arguments.pop("handle", None)
        value = self.access.worker(actor, action, arguments)
        result = _compact(value, handle, operation=action)
        # Exceptions/output from the gateway may contain a capability URL.
        return json.loads(json.dumps(result).replace(saved["turn_token"], "[browser-token]"))

    def _native(self, actor, action, body):
        """A native handle only names a lease-free configured browser session: the caller's own, or the managed desktop the
        configured Ordubashy selected (the handle itself carries that choice)."""
        handle = body.get("handle")
        if handle is None or action == "acquire":
            if handle is not None:
                raise DomainError("validation_error", "Acquire does not accept an existing handle")
            return self.access.worker(actor, action, body)
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM browser_handles WHERE id=?", (handle,)).fetchone()
        if row is not None and json.loads(row["arguments_json"]).get("coordinator"):
            # A former project-browser lease; projects have no browsers any more.
            raise DomainError("stale_browser_handle", "This browser handle named a project browser, which no longer exists")
        if self.access.native_target(actor.principal.id, handle) is None:
            # Handles issued before configured browsers became lease-free stay usable by their principal.
            if row is None or json.loads(row["binding_json"]).get("principal_id") != actor.principal.id:
                raise DomainError("forbidden", "Browser handle is unavailable for this caller")
        arguments = {key: value for key, value in body.items() if key != "handle"}
        return self.access.worker(actor, action, arguments, handle=handle)
