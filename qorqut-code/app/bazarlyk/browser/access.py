"""Registered browser leases: own configured browsers, explicitly selected business browsers and assignment turns."""

from contextlib import contextmanager
import hashlib
import json
import mimetypes
import re
import secrets
import base64
from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from .cdp import BrowserCDPProxy, BrowserCDPRoute
from .config import browser_state_directory
from bazarlyk.composition.locations import agent_workspace_of
from .coordinator import CoordinatorBrowser
from .socket import BrowserSocketConnector
from .targets import DESKTOP_KEY, DESKTOP_TARGETS, BrowserTargets
from .turns import BrowserTurnQueue
from bazarlyk.sandbox.docker import Ownership

# What a stateless native handle names when it is the caller's own configured browser.
OWN = "own"


class _LoopbackCDP:
    """The minimal ASGI app of one command's loopback listener: lifespan, then that command's CDP proxy."""

    def __init__(self):
        self.proxy = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif self.proxy is not None:
            await self.proxy(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
        else:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b""})


class BusinessBrowserAccess:
    def __init__(self, runtime):
        self.runtime = runtime
        self.turns = getattr(runtime, "browser_turns", None) or BrowserTurnQueue()
        self.coordinator = CoordinatorBrowser(self)
        self.targets = BrowserTargets(runtime)

    def runtime_directory(self):
        from bazarlyk.installation import installation_config, component, app_manifest
        manifest=app_manifest()
        if manifest and manifest.get('browser_runtime'):return manifest['browser_runtime']
        config = installation_config()
        return config['browser_runtime'] if config else str(component(self.runtime.paths, 'app') / 'tools/browser')

    def configured_control(self, principal, action, *, consumer_id=None, turn_id=None, lease_id=None):
        """Main-session access to the agent's own configured Chrome is lease-free; nothing is stored."""
        browser_key = "configured:" + principal.id
        if action == "acquire":
            return {"turn": {"id": None, "lease_id": None, "state": "owning"},
                    "target": self._worker_target(browser_key, principal.id)}
        if action in {"renew", "release"}:
            return {"turn": {"id": None, "lease_id": None, "state": "owning" if action == "renew" else "released"}}
        raise DomainError("validation_error", "Unsupported configured browser action")

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode()).hexdigest()[:6]

    def native_handle(self, principal_id, consumer, surface_id=None):
        """Stateless handle: owner prefix plus a per-session label; it never expires. A handle for a managed desktop selected by
        id binds that surface in both parts, so it resolves to exactly that browser and to no other."""
        if surface_id is None:
            return "b" + self._digest("configured:" + principal_id) + self._digest(principal_id + ":" + consumer)
        return ("b" + self._digest("configured:" + principal_id + ":" + surface_id)
                + self._digest(principal_id + ":" + consumer + ":" + surface_id))

    def owns_native_handle(self, principal_id, handle):
        return (isinstance(handle, str) and re.fullmatch(r"b[a-f0-9]{12}", handle) is not None
                and handle[1:7] == self._digest("configured:" + principal_id))

    def native_target(self, principal_id, handle):
        """What a stateless native handle of this principal names: ``OWN`` (its configured browser), the surface ID of a managed
        desktop it selected, or ``None`` when the handle is not a stateless one of this principal."""
        if self.owns_native_handle(principal_id, handle):
            return OWN
        if not isinstance(handle, str) or re.fullmatch(r"b[a-f0-9]{12}", handle) is None:
            return None
        return next((surface for surface in DESKTOP_TARGETS
                     if handle[1:7] == self._digest("configured:" + principal_id + ":" + surface)), None)

    def _native(self, actor, action, body, browser_key, handle):
        """Configured browsers are lease-free: no turn, lease, fence or durable intent gates a call."""
        if not isinstance(handle, str) or not re.fullmatch(r"b[a-f0-9]{12}", handle):
            raise DomainError("forbidden", "Browser handle is unavailable for this caller")
        if action == "command":
            return self._native_command(actor, body, browser_key, handle)
        if action == "artifact":
            return self._read_artifact(actor.principal.id, handle, body)
        if action == "acquire":
            self._worker_target(browser_key, actor.principal.id)
            return {"status": "owning", "handle": handle}
        return {"status": "released" if action == "release" else "owning"}

    def _native_command(self, actor, body, browser_key, handle):
        from bazarlyk.bootstrap import browser
        command = body.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
            raise DomainError("validation_error", "command must be a non-empty list of strings")
        try:
            browser.validate_arguments(command)
        except browser.BrowserError as exc:
            raise DomainError("validation_error", str(exc)) from exc
        workspace = self._artifact_root(actor.principal.id, handle)
        try:
            target = self._worker_target(browser_key, actor.principal.id)
            code, stdout, errors = browser.run(workspace, command, target=(target["cdp_endpoint"],
                browser.runtime_executable(target["runtime_dir"])), instance=target["instance"], capture=True)
        except browser.BrowserTimeout:
            raise DomainError("browser_timeout", "Browser command timed out; inspect the page before "
                              "repeating a state-changing action. Nothing is locked.") from None
        except browser.BrowserError as exc:
            raise DomainError("browser_failed", str(exc)) from None
        result = {"exit_code": code, "stdout": stdout[-65_536:], "stderr": errors[-16_384:],
                  "artifacts": self._artifact_handles(workspace, actor.principal.id, handle)}
        action_id = body.get("action_id") or body.get("idempotency_key")
        if action_id:
            result["action"] = {"action_id": action_id, "state": "succeeded" if code == 0 else "failed"}
        return {key: value for key, value in result.items() if value or key in {"exit_code", "stdout"}}

    def tool(self, actor, action, body):
        """Agent-facing browser calls carry only a short, persisted handle."""
        from .handles import BrowserHandles
        if action == "list":
            return self.catalog(actor)
        # Keep prepared v1 callers usable until their role is published/adopted.
        # A handle is always resolved through its binding; it never downgrades.
        if body.get("handle") is None and body.get("protocol", "legacy") == "legacy":
            return self.worker(actor, action, body)
        return BrowserHandles(self).call(actor, action, body)

    def catalog(self, actor):
        """The browsers the configured Ordubashy can select by id, as ``{id, label, business_id}``. Read only: it takes no turn,
        lease or handle and returns no URL or endpoint."""
        self.coordinator.authorize(actor, ordubashy=True)
        with self.runtime.database.read() as uow:
            return {"browsers": self.targets.catalog(uow, actor.principal.id)}

    def _selected(self, actor, action, handle, body):
        """The managed desktop (surface ID) a native call addresses, or ``None`` for the caller's own configured browser.

        On acquire ``browser_id`` selects it; afterwards only the handle does, and a ``browser_id`` that is not exactly the
        handle's browser is refused. Selecting a browser other than one's own is the configured Ordubashy's, proved afresh on
        every call."""
        browser_id, principal_id = body.get("browser_id"), actor.principal.id
        if handle is not None:
            target = self.native_target(principal_id, handle)
            if target not in {None, OWN}:
                self.coordinator.authorize(actor, ordubashy=True)
            if browser_id is not None and not self._names(actor, target, browser_id):
                raise DomainError("forbidden", "Browser handle selectors cannot be changed")
            return None if target in {None, OWN} else target
        if browser_id is None:
            return None
        if action != "acquire":
            raise DomainError("validation_error", "browser_id is accepted only on acquire; later calls use the returned handle")
        if body.get("protocol") != "handles":
            raise DomainError("validation_error", "Selecting a browser by id requires protocol=handles")
        self.coordinator.authorize(actor, ordubashy=True)
        with self.runtime.database.read() as uow:
            registry = self.targets.registry()
            if browser_id in self.targets.own_surfaces(registry, principal_id):
                return None
            surface = registry["surfaces"].get(browser_id) if registry else None
            if (browser_id not in DESKTOP_TARGETS or not surface or surface["kind"] != "browser"
                    or self.targets._claimed(registry, browser_id) is not None):
                raise DomainError("not_found", "No such browser; list the available browsers first")
        return browser_id

    def _names(self, actor, target, browser_id):
        """Whether ``browser_id`` is exactly the browser a stateless handle's target names."""
        if target == OWN:
            return browser_id in self.targets.own_surfaces(self.targets.registry(), actor.principal.id)
        return target is not None and browser_id == target

    def _business_browser(self, actor, business_id, browser_id):
        """The business whose browser ``browser_id`` is, checked before the existing business acquisition runs."""
        import uuid
        try:
            business_id = str(uuid.UUID(business_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("validation_error", "business_id must be a UUID") from None
        self.coordinator.authorize(actor, business_id, ordubashy=True)
        with self.runtime.database.read() as uow:
            self.targets.resolve(uow, actor.principal.id, browser_id, business_id)

    def worker(self, actor, action, body, *, handle=None):
        """Assignment-bound browser operations over the authenticated MCP connection."""
        assignment_id = actor.delegation_assignment_id
        generation = actor.delegation_generation
        native = assignment_id is None
        if not native and type(generation) is not int:
            raise DomainError("forbidden", "Browser access requires a current worker generation")
        if action not in {"acquire", "status", "command", "checkpoint", "renew", "release", "artifact", "reconcile"}:
            raise DomainError("validation_error", "Unsupported browser action")
        business_id = body.get("business_id")
        turn_id = body.get("turn_id")
        lease_id = body.get("lease_id")
        requester = assignment_id
        if not native and body.get("browser_id") is not None:
            raise DomainError("forbidden", "Assignments use their approved browser scope; browser_id is not accepted")
        if native:
            import uuid
            if not actor.registration_id or not actor.credential_id:
                raise DomainError("forbidden", "Native MCP browser access requires a registered agent's configured browser")
            browser_id = body.get("browser_id")
            if business_id is not None:
                # Any active Global agent with browser permission may name a business browser, and only on acquire.
                # Business context alone never selects a target; every later call resolves from its handle.
                if handle is not None or action != "acquire":
                    raise DomainError("validation_error", "business_id is accepted only on acquire; later calls use the returned handle")
                if browser_id is not None:
                    if body.get("protocol") != "handles":
                        raise DomainError("validation_error", "Selecting a browser by id requires protocol=handles")
                    self._business_browser(actor, business_id, browser_id)
                receipt = self.coordinator.acquire(actor, business_id, body)
                return receipt if browser_id is None else {**receipt, "browser_id": browser_id}
            selected = self._selected(actor, action, handle, body)
            browser_key = "configured:" + actor.principal.id if selected is None else DESKTOP_KEY + selected
            if body.get("browser_key") not in {None, browser_key}:
                raise DomainError("forbidden", "Native browser access is limited to this agent's configured target")
            if handle is None:
                try:
                    consumer = "default" if body.get("consumer_id") is None else str(uuid.UUID(body["consumer_id"]))
                except (ValueError, TypeError, AttributeError):
                    raise DomainError("validation_error", "consumer_id must be a UUID") from None
                handle = self.native_handle(actor.principal.id, consumer, selected)
            receipt = self._native(actor, action, body, browser_key, handle)
            return receipt if browser_id is None or action != "acquire" else {**receipt, "browser_id": browser_id}
        else:
            with self.runtime.database.read() as uow:
                assignment = uow.repository("operational_assignments").get(assignment_id, required=True)
                if (assignment["parent_principal_id"] != actor.principal.id
                        or assignment["state"] != "running" or assignment["execution_generation"] != generation):
                    raise DomainError("stale_worker", "Assignment generation is no longer current")
                if not {"browser_use", "browser_control"}.intersection(assignment["allowed_operations_json"]):
                    raise DomainError("forbidden", "Browser access is outside the approved assignment operations")
                scope = assignment["scope_json"]
                browser_key = body.get("browser_key") or scope.get("browser_key")
                if browser_key is None and assignment["business_id"] is not None:
                    browser_key = "business:" + assignment["business_id"]
                if not isinstance(browser_key, str) or browser_key != (scope.get("browser_key") or
                        ("business:" + assignment["business_id"] if assignment["business_id"] else None)):
                    raise DomainError("forbidden", "Browser access requires the assignment's exact configured browser scope")
                if assignment["business_id"] is not None and business_id not in {None, assignment["business_id"]}:
                    raise DomainError("forbidden", "Browser access requires the assignment's exact project scope")
                if browser_key.startswith("business:"):
                    business_id = browser_key.split(":", 1)[1]
                    if assignment["business_id"] != business_id:
                        raise DomainError("forbidden", "Browser access requires the assignment's exact project scope")
                elif not browser_key.startswith("configured:"):
                    raise DomainError("forbidden", "Browser target is not an approved configured target")
        if action == "acquire":
            with self.runtime.database.transaction(operation="browser.turn.acquire") as uow:
                assignment = uow.repository("operational_assignments").get(assignment_id, required=True)
                result = self.turns.acquire(uow, browser_key=browser_key, business_id=business_id,
                    parent_principal_id=actor.principal.id, requester_kind="assignment", requester_id=requester,
                    assignment_id=assignment_id, generation=generation)
                if result["state"] != "owning":
                    waited, _assignment = self.turns.wait(uow, result["id"],
                        parent_principal_id=actor.principal.id, requester_id=requester, generation=generation,
                        lease_id=None, checkpoint=assignment["checkpoint_json"],
                        next_action="Waiting for the exclusive browser turn; dispatcher will resume automatically")
                    return {"turn": waited, "status": "waiting"}
                token = secrets.token_hex(32)
            try:
                target = self._worker_target(browser_key, actor.principal.id)
                if business_id is not None:
                    from bazarlyk.bootstrap.client import write_json_private
                    write_json_private(browser_state_directory(self.runtime.paths, business_id) / "lease.json", {
                        "agent_id": actor.principal.id, "token": token, "assignment_id": assignment_id,
                        "generation": generation, "turn_id": result["id"], "turn_lease_id": result["lease_id"],
                        "instance": target["instance"]})
            except Exception:
                with self.runtime.database.transaction(operation="browser.turn.acquire.recover") as uow:
                    self.turns.release(uow, result["id"], parent_principal_id=actor.principal.id,
                        requester_id=requester, generation=generation, lease_id=result["lease_id"],
                        reason="browser target resolution failed")
                raise
            return {"turn": result, "status": "owning", "target": target, "turn_token": token}
        if not isinstance(turn_id, str):
            raise DomainError("validation_error", "turn_id is required")
        if action == "status":
            with self.runtime.database.transaction(operation="browser.turn.status") as uow:
                return {"turn": self.turns.status(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester)}
        if action == "renew":
            with self.runtime.database.transaction(operation="browser.turn.renew") as uow:
                result = self.turns.renew(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester, lease_id=lease_id, generation=generation,
                    lease_seconds=body.get("lease_seconds"))
                return {"turn": result}
        if action == "checkpoint":
            with self.runtime.database.transaction(operation="browser.turn.checkpoint") as uow:
                result = self.turns.checkpoint(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester, lease_id=lease_id, generation=generation,
                    checkpoint=body.get("checkpoint", {}), last_action=body.get("last_action"),
                    pending_outcome=body.get("pending_outcome"), next_action=body.get("next_action"))
                return {"turn": result}
        if action == "release":
            with self.runtime.database.transaction(operation="browser.turn.release") as uow:
                result = self.turns.release(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester, generation=generation, lease_id=lease_id,
                    reason=body.get("reason", "worker released browser"))
                if business_id and body.get("turn_token"):
                    self._clear_lease_if_matches(business_id, body.get("turn_token"), turn_id, lease_id)
                return {"turn": result}
        if action in {"artifact", "reconcile"}:
            with self.runtime.database.transaction(operation="browser.turn." + action) as uow:
                if action == "reconcile":
                    outcome = body.get("pending_outcome") or {"status": "reconciled"}
                    return {"action": self.turns.reconcile_action(uow, turn_id,
                        parent_principal_id=actor.principal.id, requester_id=requester, generation=generation,
                        lease_id=lease_id, action_id=body.get("action_id"), outcome=outcome)}
                self.turns._owned(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester, generation=generation, lease_id=lease_id)
                return self._read_artifact(assignment_id, turn_id, body)
        if action == "command":
            token = body.get("turn_token")
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
                raise DomainError("forbidden", "Browser turn token is invalid")
            return self._turn_command(actor, body, turn_id=turn_id, lease_id=lease_id, requester=requester,
                generation=generation, browser_key=browser_key, token=token, owner_id=assignment_id,
                recheck=lambda: self._assignment_current(actor, business_id, turn_id, lease_id, token))

    def _turn_command(self, actor, body, *, turn_id, lease_id, requester, generation, browser_key, token,
                      owner_id, expected_instance=None, expected_generation=None, recheck=None):
        """One leased command with a durable intent; never retried after an uncertain outcome."""
        command = body.get("command")
        action_id = body.get("action_id") or body.get("idempotency_key")
        from bazarlyk.bootstrap import browser
        if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
            raise DomainError("validation_error", "command must be a non-empty list of strings")
        try:
            browser.validate_arguments(command)
        except browser.BrowserError as exc:
            raise DomainError("validation_error", str(exc)) from exc
        with self.runtime.database.transaction(operation="browser.turn.intent") as uow:
            intent, fresh = self.turns.begin_action(uow, turn_id, parent_principal_id=actor.principal.id,
                requester_id=requester, generation=generation, lease_id=lease_id,
                action_id=action_id, command=command)
        if not fresh:
            if intent["state"] in {"pending", "running", "uncertain"}:
                raise DomainError("browser_reconcile", "This browser action has an unsettled durable intent; reconcile it before retrying", retryable=True)
            return {"action": intent, "replayed": True}
        workspace = self._artifact_root(owner_id, turn_id)
        try:
            selected = []
            target = self._worker_target(browser_key, actor.principal.id, selected=selected)
            if ((expected_instance is not None and target["instance"] != expected_instance)
                    or (expected_generation is not None and target["generation"] != expected_generation)):
                raise DomainError("stale_browser_handle", "The project browser was replaced; acquire again")
            # The host browser runtime runs here; a sandbox browser is reached only through this command's proxy,
            # which is closed before the outcome is recorded.
            with self._cdp_endpoint(target, selected, token, recheck) as endpoint:
                code, stdout, errors = browser.run(workspace, command, target=(endpoint,
                    browser.runtime_executable(target["runtime_dir"])), instance=target["instance"], capture=True)
            outcome_state = "succeeded" if code == 0 else "failed"
            outcome = {"exit_code": code, "stdout": stdout[-65_536:], "stderr": errors[-16_384:]}
            with self.runtime.database.transaction(operation="browser.turn.settle") as uow:
                settled = self.turns.settle_action(uow, turn_id, parent_principal_id=actor.principal.id,
                    requester_id=requester, generation=generation, lease_id=lease_id, action_id=action_id,
                    state=outcome_state, outcome=outcome, error=None if code == 0 else "browser command failed")
                current = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
                if current["state"] == "owning" and current["lease_until"] and \
                        self.turns._timestamp(current["lease_until"]) > self.turns._timestamp(self.turns.clock()):
                    checkpoint = body.get("checkpoint") or ({"completed": [command[0]]} if code == 0 else {})
                    result = self.turns.checkpoint(uow, turn_id, parent_principal_id=actor.principal.id,
                        requester_id=requester, lease_id=lease_id, generation=generation, checkpoint=checkpoint,
                        last_action=command[0], pending_outcome=None if code == 0 else outcome,
                        next_action=body.get("next_action"))
                else:
                    result = self.turns._compact(uow, current)
            return {"action": settled, "turn": result, "artifacts": self._artifact_handles(workspace, owner_id, turn_id), **outcome}
        except Exception as exc:
            with self.runtime.database.transaction(operation="browser.turn.uncertain") as uow:
                try:
                    settled = self.turns.settle_action(uow, turn_id, parent_principal_id=actor.principal.id,
                        requester_id=requester, generation=generation, lease_id=lease_id, action_id=action_id,
                        state="uncertain", outcome={"error": str(exc)[:1000]}, error=str(exc)[:1000])
                except DomainError:
                    settled = {"action_id": action_id, "state": "pending", "reconcile_required": True}
            return {"action": settled, "outcome": "uncertain"}

    def _read_artifact(self, owner_id, turn_id, body):
        handle = body.get("artifact_handle")
        artifact, relative = self._artifact_path(owner_id, turn_id, handle)
        offset = body.get("offset", 0)
        limit = body.get("limit", 65_536)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65_536:
            raise DomainError("validation_error", "Artifact offset and limit are bounded integers")
        size = artifact.stat().st_size
        mime_type = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
        if mime_type.startswith("image/"):
            if size > 32 * 1024 * 1024:
                raise DomainError("artifact_too_large", "Image artifact exceeds the safe delivery limit")
            encoded = base64.b64encode(artifact.read_bytes()).decode("ascii")
            return {"artifact_handle": handle, "artifact_path": relative, "mime_type": mime_type,
                    "size": size, "encoding": "base64", "content_base64": encoded,
                    # The MCP transport removes this private envelope
                    # from JSON and emits it as ImageContent instead.
                    "_mcp_images": [{"data": encoded, "mime_type": mime_type}]}
        if size > 32 * 1024 * 1024:
            raise DomainError("artifact_too_large", "Text artifact exceeds the safe delivery limit")
        content = artifact.read_bytes().decode("utf-8", errors="replace")
        page = content[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(content) else None
        return {"artifact_handle": handle, "artifact_path": relative, "mime_type": "text/plain",
                "size": size, "offset": offset, "content": page, "next_offset": next_offset}

    def _artifact_root(self, assignment_id, turn_id):
        return self.runtime.paths.runtime / "browser-turns" / assignment_id / turn_id

    def _artifact_handles(self, workspace, assignment_id, turn_id):
        root = workspace.resolve()
        items = []
        try:
            paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        except OSError:
            return items
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            if size > 32 * 1024 * 1024:
                continue
            items.append({"artifact_handle": f"browser-artifact:{assignment_id}:{turn_id}:{relative}",
                          "artifact_path": relative, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                          "size": size})
            if len(items) >= 64:
                break
        return items

    def _artifact_path(self, assignment_id, turn_id, handle):
        prefix = f"browser-artifact:{assignment_id}:{turn_id}:"
        if not isinstance(handle, str) or not handle.startswith(prefix):
            raise DomainError("forbidden", "Artifact handle is not bound to this browser turn")
        relative = handle[len(prefix):]
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise DomainError("forbidden", "Artifact handle is outside the assignment browser workspace")
        root = self._artifact_root(assignment_id, turn_id).resolve()
        artifact = (root / relative).resolve()
        if not artifact.is_relative_to(root) or artifact.is_symlink() or not artifact.is_file():
            raise DomainError("not_found", "Browser artifact does not exist")
        return artifact, relative

    def _clear_lease_if_matches(self, business_id, token, turn_id, lease_id):
        from bazarlyk.bootstrap.client import write_json_private
        path = browser_state_directory(self.runtime.paths, business_id) / "lease.json"
        try:
            saved = json.loads(safe_io.read_bytes(path, limit=8192, private=True))
        except (OSError, ValueError, TypeError):
            return
        if (isinstance(saved, dict) and saved.get("token") == token and saved.get("turn_id") == turn_id
                and saved.get("turn_lease_id") == lease_id):
            write_json_private(path, {})

    def _worker_target(self, browser_key, parent_principal_id, *, selected=None):
        """The target's current identity; a sandbox browser's binding is appended to ``selected``, never an endpoint."""
        if browser_key.startswith(DESKTOP_KEY):
            return self.targets.desktop_target(browser_key, self.runtime_directory())
        if browser_key.startswith("configured:"):
            from bazarlyk.bootstrap import browser
            with self.runtime.database.read() as uow:
                profile = uow.connection.execute("SELECT p.home_business_id,b.slug FROM agent_profiles p "
                                                 "LEFT JOIN businesses b ON b.id=p.home_business_id WHERE p.agent_id=?",
                                                 (parent_principal_id,)).fetchone()
            if profile is None:
                raise DomainError("operation_unavailable", "The configured browser workspace is unavailable")
            endpoint, executable = browser.load_target(agent_workspace_of(
                self.runtime.paths, parent_principal_id, profile["home_business_id"], profile["slug"]))
            return {"cdp_endpoint": endpoint, "generation": "configured", "instance": browser_key,
                    "runtime_dir": str(browser.runtime_directory(executable)), "browser_key": browser_key}
        with self.runtime.database.read() as uow:
            business_id = browser_key.split(":", 1)[1]
            business = uow.repository("businesses").get(business_id, required=True)
            sandbox = uow.repository("business_sandboxes").get(business_id)
        if sandbox is not None:
            # Chrome stays in its browser container; a leased command reaches it only through its own loopback proxy.
            binding = self.runtime.sandbox.observed_browser_binding(business_id)
            if selected is not None:
                selected.append(binding)
            target = {"generation": binding.generation, "instance": binding.instance_id}
        else:
            from .config import configured_target
            from .control import ready_target
            target = ready_target(browser_state_directory(self.runtime.paths, business_id),
                                  configured_target(business, self.runtime.paths))
        return {**target, "business_id": business_id, "browser_key": browser_key, "runtime_dir": self.runtime_directory()}

    def _connector(self, business_id, current_binding):
        ownership = Ownership(hashlib.sha256(str(self.runtime.paths.operations).encode()).hexdigest()[:20], business_id)
        return BrowserSocketConnector(self.runtime.sandbox.docker, ownership, current_binding=current_binding)

    @contextmanager
    def _cdp_endpoint(self, target, selected, token, recheck):
        """A host target's endpoint, or a sandbox browser behind a 127.0.0.1 listener that exists for this command only.

        Every CDP request and the open WebSocket's periodic check re-prove the caller's exact lease through ``recheck``
        and the original observed browser binding; a replaced browser or lease is never adopted. Nothing about the
        listener is configured, persistent or returned to the caller."""
        if not selected:
            yield target["cdp_endpoint"]
            return
        if recheck is None:
            raise DomainError("forbidden", "A sandbox browser command requires its lease check")
        from bazarlyk.sandbox.listener import ListenerServer, PrivatePeerBoundary
        [binding] = selected
        business_id = target["business_id"]

        def current():
            return self.runtime.sandbox.observed_browser_binding(business_id)

        def authorize(scope, expected):
            origin = next((value.decode("latin-1") for key, value in scope.get("headers", [])
                           if key.lower() == b"origin"), None)
            if origin is not None and origin != expected.public_origin:
                return False
            self.runtime.require_authority()
            recheck()
            return current() == expected.browser

        app = _LoopbackCDP()
        server = ListenerServer(PrivatePeerBoundary(app, ("127.0.0.1",)), "127.0.0.1", 0)
        try:
            server.start()
            origin = f"http://127.0.0.1:{server.port}"
            app.proxy = BrowserCDPProxy(BrowserCDPRoute(binding, token, origin),
                                        connector=self._connector(business_id, current), authorize=authorize)
            yield origin + "/" + token
        finally:
            app.proxy = None
            server.stop()

    def _assignment_current(self, actor, business_id, turn_id, lease_id, token):
        """Per CDP request of an assignment's sandbox command: its live worker binding, owning turn and lease record."""
        from bazarlyk.agents.operational_delegation import OperationalDelegation
        with self.runtime.database.read() as uow:
            saved = uow.connection.execute("SELECT credential_hash FROM operational_delegation_bindings WHERE id=?",
                                           (actor.delegation_binding_id,)).fetchone()
            try:
                current = OperationalDelegation.authenticate_binding(uow, saved["credential_hash"]) if saved else None
            except DomainError:
                current = None      # revoked, expired or superseded: refused below like any other lost binding
            if (current is None or current["id"] != actor.delegation_binding_id
                    or current["assignment_id"] != actor.delegation_assignment_id
                    or current["execution_generation"] != actor.delegation_generation
                    or current["parent_principal_id"] != actor.principal.id):
                raise DomainError("forbidden", "Browser access requires the current worker binding")
            turn = uow.connection.execute(
                "SELECT 1 FROM browser_turns WHERE id=? AND browser_key=? AND state='owning' AND lease_id=? "
                "AND lease_until>? AND assignment_id=? AND execution_generation=?",
                (turn_id, "business:" + business_id, lease_id, self.turns.clock(), actor.delegation_assignment_id,
                 actor.delegation_generation)).fetchone()
        try:
            lease = json.loads(safe_io.read_bytes(browser_state_directory(self.runtime.paths, business_id) / "lease.json",
                                                 limit=8192, private=True))
        except (OSError, ValueError, TypeError):
            lease = None
        if (turn is None or not isinstance(lease, dict) or lease.get("token") != token
                or lease.get("turn_id") != turn_id or lease.get("turn_lease_id") != lease_id
                or lease.get("assignment_id") != actor.delegation_assignment_id
                or lease.get("generation") != actor.delegation_generation):
            raise DomainError("forbidden", "Browser capability is no longer owned by this assignment")
