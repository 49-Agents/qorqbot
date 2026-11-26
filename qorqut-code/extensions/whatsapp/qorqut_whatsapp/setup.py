"""Setting up the owner's personal WhatsApp from a conversation or from Settings, with no terminal.

The extension ships in every installation and stays dormant until the owner asks: no binding, no helper process, no
``whatsapp_read`` and nothing that contacts WhatsApp. ``Setup`` is the whole lifecycle of the helper under the service:

* ``start`` binds the read-only reader to the calling Global Ordubashy (``install_reader``), writes the private setup
  marker and starts the helper in pairing mode (``serve --pair --pair-only``) as a child of the service. The helper writes
  its rotating ``pair.png`` into the private data folder; only the owner's dashboard route reads it. The QR, its contents
  and its path never reach a tool result, a log line of the service or the helper's normal log: the pairing child logs to
  ``private/whatsapp/pair.log`` beside the image.
* The helper exits 0 once the phone is linked (or when it was already linked); any other exit is a failed or expired
  pairing, and the never-linked binding is removed so a later start is clean.
* After a successful scan the app finishes by itself (``finish``): the managed-send secret file, the receiver restarted
  with the managed-send transport, the opaque account reference read from the helper and written into a version 2
  binding, the trusted ``whatsapp.send`` worker registered and the owner's grant set on the Ordubashy profile, then
  Ordubashy's session restarted so it opens with ``whatsapp_read``. Every step is idempotent and the marker stays until
  the last one ran, so a service restart part way finishes on the next start, ``status`` or ``start``.

An installation set up by hand earlier (a version 2 binding and no marker) is left exactly as it is. A linked version 1
binding gains sending only when ``start`` is called for it. State lives on disk (binding, marker, session store); only a
running pairing and the last failure are held in memory.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import threading

from qorqut.private_files import fsync_directory, require_private_directory, write_private_file
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, plain

from . import binding
from .client import ManagedWhatsApp, read_secret
from .receiver import open_log

LOG = logging.getLogger(__name__)

STATES = ("unsupported", "not_set_up", "linking", "finishing", "connected", "failed")
NOTES = {
    "unsupported": "WhatsApp can't be linked on this computer yet.",
    "not_set_up": "WhatsApp isn't linked yet.",
    "linking": "Waiting for the phone to scan the code in Settings > Connections > WhatsApp.",
    "finishing": "The phone is linked. Qorqbot is finishing the setup by itself.",
    "connected": "WhatsApp is linked. Ordubashy can read your chats, and a message is sent only after you approve it.",
    "read_only": "WhatsApp is linked for reading. Sending through your approval isn't set up yet.",
    "expired": "The code expired before it was scanned. Try again.",
    "pair_failed": "Linking didn't finish. Try again.",
    "finish_failed": "The phone is linked, but Qorqbot couldn't finish the setup. Try again.",
    "damaged": "The WhatsApp setup file on this computer is damaged.",
}
ASK_OWNER = ("Ask the owner to open Settings > Connections > WhatsApp in the dashboard and scan the code shown there with "
             "their phone: in WhatsApp, Settings > Linked devices > Link a device. The code changes every few seconds and "
             "stops after a few minutes. Everything after the scan happens by itself; you get a message when WhatsApp is "
             "linked. Never ask for the code or a screenshot of it.")
LINKED_NOTICE = ("<qorqut-whatsapp-notice>\nThe owner's WhatsApp is now linked: whatsapp_read is available to you. Tell the owner "
                 "in one line that WhatsApp is connected and that any message you propose with whatsapp_send is sent only "
                 "after they approve it. This notice needs no other reply.\n</qorqut-whatsapp-notice>")
OPERATION = "whatsapp_send"


class Stopping(Exception):
    """The service is stopping; the setup resumes on the next start."""


def _replace_private(directory: Path, name: str, data: bytes) -> None:
    """Write a private file under ``name`` by an exclusive temporary file and an atomic rename."""
    temporary = "." + name + "." + secrets.token_hex(6)
    write_private_file(directory, temporary, data)
    os.replace(directory / temporary, directory / name)
    fsync_directory(directory)


class Setup:
    FINISH_ATTEMPTS = 8
    RETRY_SECONDS = 15.0
    ACCOUNT_SECONDS = 60.0

    def __init__(self, paths, receiver, host, *, popen=subprocess.Popen, client_factory=ManagedWhatsApp):
        self.paths, self.receiver, self.host = paths, receiver, host
        self.popen, self.client_factory = popen, client_factory
        self.lock = threading.RLock()
        self.pairing = self.pair_log = self.finisher = None
        self.cancelled, self.failure = False, None
        self.stopping = threading.Event()

    @property
    def root(self) -> Path:
        return self.paths.instance_root

    # -- service lifecycle ---------------------------------------------------------------------------------------------
    def start(self):
        self.stopping.clear()
        with self.lock:
            self.receiver.start()       # dormant, unlinked and unbound installations start nothing
            if binding.setup_marker_path(self.root).is_file() and binding.linked(self.root):
                self._kick()

    def stop(self, timeout=5):
        self.stopping.set()
        with self.lock:
            self._end_pairing(timeout)
            finisher = self.finisher
        if finisher is not None:
            finisher.join(timeout)
        with self.lock:
            self.receiver.stop(timeout)

    def running(self) -> bool:
        return (self.receiver.running() or self._pairing_alive()
                or (self.finisher is not None and self.finisher.is_alive()))

    # -- the state ---------------------------------------------------------------------------------------------------
    def _pairing_alive(self) -> bool:
        return self.pairing is not None and self.pairing.poll() is None

    @staticmethod
    def _view(state, note=None, *, qr=False, send_ready=False) -> dict:
        return {"state": state, "note": NOTES[note or state], "qr": qr, "send_ready": send_ready}

    def status(self) -> dict:
        with self.lock:
            return self._status()

    def _status(self) -> dict:
        root = self.root
        try:
            value = binding.load_binding(root)
        except DomainError:
            return self._view("failed", "damaged")
        if self._pairing_alive():
            return self._view("linking", qr=(binding.data_path(root) / "pair.png").is_file())
        if value is None or not binding.linked(root):
            if self.failure:
                return self._view("failed", self.failure)
            return self._view("not_set_up" if value is not None or binding.supported() else "unsupported")
        if binding.setup_marker_path(root).is_file():
            if self.failure:
                return self._view("failed", self.failure)
            self._kick()     # resumes a setup a restart interrupted
            return self._view("finishing")
        ready = value["version"] == 2 and self.host.send_ready(value["agent_id"])
        return self._view("connected", None if ready else "read_only", send_ready=ready)

    # -- the owner's or Ordubashy's actions ----------------------------------------------------------------------------------
    def begin(self, agent_id: str) -> dict:
        """Start linking for ``agent_id`` (the active Global Ordubashy), or finish sending for a linked read-only binding.
        Idempotent while linking or finishing. Returns the status."""
        with self.lock:
            if self.stopping.is_set():
                raise DomainError("operation_unavailable", "Qorqbot is stopping. Try again in a moment.", retryable=True)
            root = self.root
            value = binding.load_binding(root)
            if self._pairing_alive():
                return self._status()
            if value is not None and binding.linked(root):
                if value["agent_id"] != agent_id:
                    raise DomainError("forbidden", "WhatsApp is linked for another Ordubashy")
                if value["version"] == 2 and not binding.setup_marker_path(root).is_file():
                    return self._status()        # set up earlier: left exactly as it is
                self.failure = None
                self._mark(agent_id)
                self._kick()
                return self._status()
            if value is None and not binding.supported():
                return self._view("unsupported")
            self.failure = None
            if value is not None and (value["agent_id"] != agent_id or not Path(value["binary"]).is_file()):
                self._unlink(binding.root_binding(root))
                value = None
            if value is None:
                from .__main__ import install_reader
                try:
                    value = install_reader(root, agent_id)
                except ValueError as problem:
                    raise DomainError("operation_unavailable", str(problem)) from None
            if not all(binding.socket_fits(Path(value["socket"]).parent / name) for name in ("read.sock", "send.sock")):
                raise DomainError("invalid_configuration", "This installation path is too long for WhatsApp; use a shorter Qorqbot folder path")
            self._mark(agent_id)
            self.receiver.stop()       # never linked, so nothing runs; the helper's lock admits one reader only
            self._pair(value)
            return self._status()

    def cancel(self) -> dict:
        with self.lock:
            self._end_pairing(5)
            self.failure = None
            if not binding.linked(self.root):
                self._forget()
            return self._status()

    def qr(self) -> bytes | None:
        """The current code image, for the owner's dashboard route only; None when no pairing shows one."""
        with self.lock:
            if not self._pairing_alive():
                return None
            path = binding.data_path(self.root) / "pair.png"
            try:
                if path.is_symlink():
                    return None
                return path.read_bytes()
            except OSError:
                return None

    # -- pairing -------------------------------------------------------------------------------------------------------
    def _pair(self, value):
        from .__main__ import pair_command
        args, data, environment = pair_command(self.root, value)
        # The pairing log names the code image, so it stays in the private data folder beside it.
        log = data / "pair.log"
        try:
            log.unlink()
        except FileNotFoundError:
            pass
        self.pair_log = open_log(log)
        try:
            process = self.popen(args, stdin=subprocess.DEVNULL, stdout=self.pair_log, stderr=self.pair_log, cwd=data,
                                 env=environment, start_new_session=True)
        except BaseException:
            self.pair_log.close(); self.pair_log = None
            raise
        self.pairing, self.cancelled = process, False
        threading.Thread(target=self._watch, args=(process,), name="whatsapp-pairing", daemon=True).start()

    def _watch(self, process):
        code = process.wait()
        with self.lock:
            if process is not self.pairing:
                return
            self._close_pairing()
            if self.cancelled or self.stopping.is_set():
                return
            if code != 0 or not binding.linked(self.root):
                self.failure = "expired" if self._expired() else "pair_failed"
                LOG.info("WhatsApp linking ended without a linked phone")
                self._forget()
                return
            LOG.info("WhatsApp is linked; finishing the setup")
            self._kick()

    def _expired(self) -> bool:
        try:
            with open(binding.data_path(self.root) / "pair.log", "rb") as source:
                source.seek(0, os.SEEK_END)
                source.seek(max(0, source.tell() - 4096))
                return b"pairing ended (timeout)" in source.read()
        except OSError:
            return False

    def _end_pairing(self, timeout):
        process = self.pairing
        if process is None:
            return
        self.cancelled = True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
        self._close_pairing()

    def _close_pairing(self):
        self.pairing = None
        if self.pair_log is not None:
            self.pair_log.close(); self.pair_log = None

    def _mark(self, agent_id):
        directory = require_private_directory(self.root / "private", create=True)
        _replace_private(directory, binding.setup_marker_path(self.root).name,
                         (json.dumps({"version": 1, "agent_id": agent_id}) + "\n").encode())

    @staticmethod
    def _unlink(path: Path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _forget(self):
        """Back to not set up: the never-linked binding and the marker are removed. The copied helper stays."""
        try:
            value = binding.load_binding(self.root)
        except DomainError:
            value = None
        if value is not None and not binding.linked(self.root):
            self._unlink(binding.root_binding(self.root))
        self._unlink(binding.setup_marker_path(self.root))

    # -- finishing after the scan ------------------------------------------------------------------------------------------
    def _kick(self):
        """Run ``finish`` in the background, at most once at a time, retrying a step that fails."""
        if self.stopping.is_set() or (self.finisher is not None and self.finisher.is_alive()):
            return
        self.finisher = threading.Thread(target=self._finish_loop, name="whatsapp-setup", daemon=True)
        self.finisher.start()

    def _finish_loop(self):
        for _ in range(self.FINISH_ATTEMPTS):
            if self.stopping.is_set():
                return
            try:
                self.finish()
                return
            except Stopping:
                return
            except Exception as problem:  # noqa: BLE001 - every step is retried; the reason's code only, never data
                LOG.info("A WhatsApp setup step did not finish (%s); it is retried", getattr(problem, "code", type(problem).__name__))
            if self.stopping.wait(self.RETRY_SECONDS):
                return
        with self.lock:
            self.failure = "finish_failed"

    def _check(self):
        if self.stopping.is_set():
            raise Stopping()

    def finish(self):
        root = self.root
        marker = binding.setup_marker_path(root)
        value = binding.load_binding(root)
        if value is None or not marker.is_file() or not binding.linked(root):
            return
        agent_id = value["agent_id"]
        self._secret()
        with self.lock:
            self._check()
            if not (self.receiver.running() and self.receiver.managed):
                self.receiver.stop()
                self.receiver.start()
        send = value.get("managed_send") or binding.pending_send(value, root)
        if send is None:
            raise DomainError("operation_unavailable", "The WhatsApp send transport is not ready")
        reference = self._account(send)
        if value["version"] == 1 or value["managed_send"]["account_ref"] != reference:
            # A first link, or a phone linked again to another account: the owner's scan is the deliberate rebind.
            updated = {**{key: value[key] for key in binding.BINDING_V1}, "version": 2,
                       "managed_send": {"socket": send["socket"], "secret_file": send["secret_file"], "account_ref": reference}}
            binding.validate_binding(updated, root)
            _replace_private(require_private_directory(root / "config"), binding.root_binding(root).name,
                             (json.dumps(updated, indent=2) + "\n").encode())
        self._check()
        self.host.register_worker()
        self.host.grant(agent_id)
        self._check()
        try:
            self.host.restart(agent_id, LINKED_NOTICE, "whatsapp-linked:" + reference)
        except Exception as problem:  # noqa: BLE001 - the session picks WhatsApp up whenever it next opens
            LOG.info("Ordubashy's session was not restarted after WhatsApp was linked (%s)",
                     getattr(problem, "code", type(problem).__name__))
        self._unlink(marker)
        LOG.info("WhatsApp setup finished")

    def _secret(self):
        """The managed-send secret: 64 random characters in a new private (0600) file, created once and kept."""
        path = binding.send_secret_path(self.root)
        if os.path.lexists(path):
            read_secret(path)        # an unusable existing secret refuses with a fixed message; it is never printed
            return
        directory = require_private_directory(path.parent, create=True)
        write_private_file(directory, path.name, secrets.token_hex(32).encode())

    def _account(self, send) -> str:
        client = self.client_factory({"socket": send["socket"], "secret_file": send["secret_file"], "account_ref": None})
        waited = 0.0
        while True:
            self._check()
            try:
                return client.account()["account_ref"]
            except DomainError:
                if waited >= self.ACCOUNT_SECONDS or not self.receiver.running():
                    raise
            if self.stopping.wait(1.0):
                raise Stopping()
            waited += 1.0


class RuntimeHost:
    """What finishing needs from the running installation, by the same means as the documented manual steps."""

    def __init__(self, runtime):
        self.runtime = runtime

    def coordinator(self):
        from qorqut.management.identity import configured_ordubashy
        with self.runtime.database.read() as uow:
            return configured_ordubashy(uow)

    def _authority(self):
        authority = getattr(self.runtime.programs, "authority", None)
        if authority is None:
            raise DomainError("operation_unavailable", "Background programs are not installed")
        return authority

    def register_worker(self):
        """The maintenance step of every new program: create missing system workers, never reactivate a disabled one."""
        authority = self._authority()
        with self.runtime.database.transaction() as uow:
            authority.register_workers(uow)

    @staticmethod
    def _profile(uow, agent_id):
        row = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "Ordubashy's profile was not found")
        return row["revision"], json.loads(row["profile_json"]).get("tool_policy", {}).get("overrides", {})

    def grant(self, agent_id) -> bool:
        """The owner's grant of ``whatsapp_send`` (mode signoff) on the Ordubashy profile, made as the installation owner
        through ``permissions_set``. An explicit decision already there, a deny included, is left as it is."""
        from qorqut.agents.models import GLOBAL_CAPABILITIES
        from qorqut.management.identity import installation_owner
        from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal
        with self.runtime.database.read() as uow:
            owner = installation_owner(uow)
            revision, overrides = self._profile(uow, agent_id)
        if OPERATION in overrides:
            return overrides[OPERATION] != "deny"
        result = self.runtime.application.invoke(
            InvocationContext(Principal(owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
            "permissions_set", {"agent_id": agent_id, "overrides": {**overrides, OPERATION: "signoff"}},
            InvocationOptions(expected_revision=revision, idempotency_key=f"whatsapp-setup-grant-{agent_id}-{revision}"))
        if result.status != "ok":
            raise DomainError("operation_unavailable", "The WhatsApp send grant was not saved")
        return True

    def send_ready(self, agent_id) -> bool:
        from .operations import PROGRAM
        authority = getattr(self.runtime.programs, "authority", None)
        if authority is None:
            return False
        with self.runtime.database.read() as uow:
            try:
                authority.require_worker(authority.worker_id(PROGRAM, uow), None, uow)
                _, overrides = self._profile(uow, agent_id)
            except DomainError:
                return False
        return overrides.get(OPERATION) in {"signoff", "allow"}

    def restart(self, agent_id, text, key):
        """Ordubashy's managed session closed and opened again (``SessionOpener.restart``), so its preparation adds the
        reader; ``text`` is its first message. An agent that is off, or an installation in terminals, is left alone."""
        from qorqut.native_chat import store
        chat = getattr(self.runtime, "native_chat", None)
        opener = getattr(chat, "opener", None)
        if opener is None:
            return False
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if profile is None:
            return False
        opener.restart(profile, text, key)
        return True


# -- the tool ---------------------------------------------------------------------------------------------------------------

TOOL = "whatsapp_setup"
TOOL_ARGUMENTS = schema({"action": {"enum": ["status", "start", "cancel"],
                                    "description": "status: where setup stands. start: begin linking. cancel: stop a linking in progress."}},
                        ("action",))
TOOL_DESCRIPTION = (
    "Ordubashy only. Link the owner's personal WhatsApp when the owner asks for it. start binds the reader to you and shows "
    "a code in Settings > Connections > WhatsApp; the result says what to ask the owner. After the scan everything finishes "
    "by itself and you get a message when it is linked. status says where setup stands; cancel stops a linking in "
    "progress. This sends nothing. Once linked, read with whatsapp_read; a message is sent only through whatsapp_send, "
    "after the owner approves it.")


class SetupTool:
    """``whatsapp_setup`` for the configured Global Ordubashy in person. No Signoff: nothing reaches the account unless the
    owner scans the code. The work runs in ``external`` (outside any transaction); ``apply`` hands the outcome back."""

    def __init__(self, application, setup):
        self.app, self.setup = application, setup
        self.outcomes, self.guard = {}, threading.Lock()

    def register(self, registry):
        registry.register(OperationDescriptor(TOOL, TOOL_ARGUMENTS, effect_kind="records", supports_signoff=False,
            scope_kinds=frozenset({"global"}), exposure=frozenset({"mcp"}), resolve_namespace=lambda ctx, args: "global",
            global_capability="read_global_context", description=TOOL_DESCRIPTION,
            prepare=self.prepare, external=self.external, apply=self.apply))
        return self

    def caller(self, ctx, uow) -> str:
        """The configured coordinator in person, at Global; resolved from the installation record, never from a caller
        field. It works before any binding exists. A role grant or a policy override cannot widen this."""
        from dataclasses import replace
        from qorqut.management.identity import configured_ordubashy
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        coordinator = configured_ordubashy(uow)
        if (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None or ctx.project_id is not None
                or not coordinator or ctx.principal_id != coordinator):
            raise DomainError("forbidden", "WhatsApp is set up by the owner's Ordubashy")
        return ctx.principal_id

    def prepare(self, ctx, args, options, uow):
        self.caller(ctx, uow)
        return PlanBuilder(ctx, TOOL, args, "WhatsApp setup").build()

    def external(self, ctx, plan):
        with self.app.database.read() as uow:
            agent_id = self.caller(ctx, uow)
        action = plan.arguments["action"]
        view = (self.setup.begin(agent_id) if action == "start" else self.setup.cancel() if action == "cancel"
                else self.setup.status())
        outcome = {"state": view["state"], "explanation": view["note"]}
        if view["state"] == "connected":
            outcome["sending_through_approval"] = view["send_ready"]
        if view["state"] == "linking":
            outcome["next"] = ASK_OWNER
        with self.guard:
            self.outcomes[plan.payload_hash] = outcome
            while len(self.outcomes) > 64:
                self.outcomes.pop(next(iter(self.outcomes)))

    def apply(self, ctx, plan, uow):
        self.caller(ctx, uow)
        with self.guard:
            if plan.payload_hash not in self.outcomes:
                raise DomainError("operation_failed", "WhatsApp setup did not run. Try again.", retryable=True)
            outcome = self.outcomes.pop(plan.payload_hash)
        return plain(outcome)


def policy(name, defaults, overrides, registry, *, role_id, scope, resolve):
    """Before any override: the tool exists for Ordubashy only; for Ordubashy the ordinary policy decides (allowed, unless
    the owner denies it in the agent's settings). The handler still proves the configured coordinator in person."""
    from qorqut_operations.contracts import PolicyDecision
    return None if role_id == "ordubashy" else PolicyDecision("deny", "whatsapp_setup_ordubashy_only")
