"""The service-owned Telegram bridge: lifecycle, verification, single poller and the durable worker cycle.

Construction is inert. ``start`` reads the private configuration and spawns two bounded threads; all token reads,
network verification and database recovery happen on those threads. A bridge failure degrades bridge health only.
No terminal session is ever started, restarted or killed from here.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import threading
import time

from qorqut_operations.contracts import DomainError, canonical_hash
from bazarlyk.agents.global_scope import active_global
from bazarlyk.private_files import require_private_directory
from . import store
from .api import HttpBotApi, TelegramApiError
from .config import AGENT_CHATS, CONFIG_ENV, TelegramConfig, load_config, read_token
from .media_payloads import MediaPayloads
from .spool import SUFFIX, SecretSpool

LOGGER = logging.getLogger(__name__)
STATE_DIR_ENV = "QORQUT_TELEGRAM_STATE_DIR"
DEFAULT_STOP_TIMEOUT = 35
VERIFY_RETRY_SECONDS = (5, 15, 30, 60)
POLL_TIMEOUT_SECONDS = 20
# Telegram keeps undelivered updates for about a day; a gap this long may have lost some.
OUTAGE_WARNING_SECONDS = 20 * 3600
# Refusals of the pinned identity. Each one closes every effect (polling, sends, gateway, deletion, direct input and Signoff
# decisions) until the operator reconfigures; the first two are never repaired automatically.
MISMATCH_CODES = ("owner_binding_mismatch", "installation_binding_mismatch")
FAIL_CLOSED = MISMATCH_CODES + ("binding_changed", "binding_invalid")


def default_state_dir(environ):
    explicit = environ.get(STATE_DIR_ENV)
    if explicit:
        return Path(explicit)
    base = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "qorqut" / "telegram"


def require_unversioned(directory, *roots):
    """The private state directory must sit outside the operations root, the workspace and every Git worktree."""
    resolved = Path(directory).resolve()
    for root in roots:
        if root is not None:
            root = Path(root).resolve()
            if resolved == root or root in resolved.parents:
                raise DomainError("invalid_configuration", "The Telegram state directory must be outside the operations root and workspace")
    for candidate in (resolved, *resolved.parents):
        marker = candidate / ".git"
        if marker.is_file() or (marker / "HEAD").exists():
            raise DomainError("invalid_configuration", "The Telegram state directory must be outside every versioned tree")


class PollerLock:
    """One poller per installation and bot: an OS lock that dies with the process."""

    def __init__(self, directory, installation_id, bot_id):
        key = hashlib.sha256(f"{installation_id}:{bot_id}".encode()).hexdigest()[:16]
        self.path = Path(directory) / f"poller-{key}.lock"
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DomainError("lock_held", "Another Telegram poller holds this installation's lock", retryable=True) from None
        self._fd = fd

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class TelegramBridge:
    def __init__(self, runtime, *, config_path=None, environ=None, api_factory=None, clock=None, sleep=None, rng=None,
                 state_dir=None, authority_check=None, poll_timeout=POLL_TIMEOUT_SECONDS, tick=1.0):
        self.runtime = runtime
        self.environ = os.environ if environ is None else environ
        self.config_path = config_path
        self.api_factory = api_factory or (lambda config, token: HttpBotApi(token))
        from datetime import datetime, timezone
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.rng = rng or random.Random()
        self.state_dir_override = state_dir
        self.authority_check = authority_check
        self.poll_timeout, self.tick = poll_timeout, tick
        self.config = TelegramConfig()
        self.api = None
        self.spool = None
        self.lock = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._lifecycle = threading.RLock()
        self._threads = {}
        self._health = {}
        self._config_signature = None
        self._verify_attempts = 0
        self._recovered = False
        self._spool_swept = None
        self._media_swept = None
        self._media_recovered = False
        self.steps = []
        self.gateway = None
        # Source-kind hooks: admission before a send, and follow-up after its receipt commits.
        self.admit_hooks = {}
        self.sent_hooks = {}
        # Source kind -> context-manager factory yielding one verified upload: (filename, stream, size, caption, content_type).
        self.media_loaders = {}
        self.timings = {"backoff_base": 5.0, "backoff_cap": 900.0, "max_attempts": 12, "sweep_interval": 900.0,
                        "cleanup_attempts": 8, "spool_sweep_interval": 60.0, "media_sweep_interval": 300.0}
        # The private sealed snapshots of agent attachments. Construction is inert: no directory is read or created until a
        # file is staged, and its upkeep (recover, sweep, status) runs on this lifecycle whether or not Telegram becomes ready.
        self.media = MediaPayloads(self)
        from .outbox import Dispatcher, Cleanup
        from .inbound import Inbound
        from .owner_input import OwnerInputs
        from .signoffs import SignoffBridge
        self.signoff_bridge = SignoffBridge(self)
        self.dispatcher = Dispatcher(self)
        self.cleanup = Cleanup(self)
        self.inbound = Inbound(self)
        self.owner_inputs = OwnerInputs(self)
        self.steps += [self.dispatcher.run, self.cleanup.run, self.owner_inputs.run]

    # ----- small shared helpers -------------------------------------------------------------------------

    @property
    def database(self):
        return self.runtime.database

    @property
    def application(self):
        return self.runtime.application

    def now(self):
        return store.iso(self.clock())

    def wall_state_dir(self):
        return Path(self.state_dir_override) if self.state_dir_override else default_state_dir(self.environ)

    @contextmanager
    def write(self, operation):
        """A short write unit under recovery admission and the maintenance gate. Never hold it across I/O."""
        admission = getattr(self.application, "writer_admission", None)
        with (admission.writer() if admission is not None else nullcontext()):
            with self.database.transaction(operation="telegram." + operation) as uow:
                self.application.ensure_writes_open(uow)
                yield uow

    @contextmanager
    def read(self):
        with self.database.read() as uow:
            yield uow

    def backoff(self, attempts):
        ceiling = min(self.timings["backoff_cap"], self.timings["backoff_base"] * 2 ** max(0, attempts - 1))
        return ceiling * (0.5 + self.rng.random() * 0.5)

    def degrade(self, component, reason):
        if self._health.get(component) != reason:
            LOGGER.warning("Telegram bridge %s degraded: %s", component, reason)
        self._health[component] = reason

    def recover_component(self, component):
        self._health.pop(component, None)

    def health(self):
        return dict(self._health)

    # ----- configuration -------------------------------------------------------------------------------

    def _selected_path(self):
        return self.config_path or self.environ.get(CONFIG_ENV)

    def _signature(self, path):
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    def load_config(self):
        path = self._selected_path()
        self.config = load_config(path, environ=self.environ)
        self._config_signature = self._signature(path) if path else None
        return self.config

    def current_config(self):
        """The active config for an effect. It re-verifies when the file changed, and refuses when it is not ready."""
        path = self._selected_path()
        if path and self._signature(path) != self._config_signature:
            self._ready.clear()
        if not self._ready.is_set() or not self.config.enabled:
            raise DomainError("operation_unavailable", "The Telegram bridge is not ready", retryable=True)
        try:
            with self.read() as uow:
                self.check_binding(uow)
        except DomainError as exc:
            if exc.code in FAIL_CLOSED:
                self.fail_closed(exc)
            raise
        return self.config

    # ----- the pinned identity -----------------------------------------------------------------------------

    def check_binding(self, uow):
        """Revalidate the pinned installation and Qorqut owner against the authoritative records and the saved generation.

        Returns the identity every stored receipt, decision intent and input delivery freezes: ``generation``, binding
        ``fingerprint``, ``owner`` and ``installation``. It never adopts a new pin: an owner or installation that no longer
        matches is a refusal until the operator edits the configuration and the bridge starts a new generation.
        """
        config = self.config
        if not config.enabled:
            raise DomainError("operation_unavailable", "The Telegram bridge is not enabled")
        installation = uow.repository("installation").get("service")
        if not installation:
            raise DomainError("binding_invalid", "The installation record is missing")
        state = store.get_state(uow)
        pinned_installation = state["installation_id"] if state else config.installation_id
        pinned_owner = state["owner_principal_id"] if state else config.owner_principal_id
        if installation["installation_id"] != config.installation_id or installation["installation_id"] != pinned_installation:
            raise DomainError("installation_binding_mismatch", "This is not the installation the Telegram bridge is bound to")
        if installation["owner_principal_id"] != pinned_owner or installation["owner_principal_id"] != config.owner_principal_id:
            raise DomainError("owner_binding_mismatch", "The installation owner is not the owner the Telegram bridge is bound to")
        owner = uow.repository("principals").get(pinned_owner)
        if not owner or not owner["active"] or owner["kind"] != "human":
            raise DomainError("binding_invalid", "The bound installation owner is missing or inactive")
        fingerprint = config.binding_fingerprint()
        if state is not None and state["binding_fingerprint"] != fingerprint:
            raise DomainError("binding_changed", "The Telegram binding configuration changed; the bridge is verifying it", retryable=True)
        return {"generation": state["config_generation"] if state else 1, "fingerprint": fingerprint, "owner": pinned_owner,
                "installation": pinned_installation}

    def verify_frozen(self, uow, frozen):
        """The current identity when it is exactly the frozen one; otherwise ``binding_changed`` (never an upgrade)."""
        current = self.check_binding(uow)
        if frozen != current:
            raise DomainError("binding_changed", "The Telegram binding or owner changed after this was accepted")
        return current

    @staticmethod
    def _component(code):
        return ("binding" if code in MISMATCH_CODES + ("binding_changed",)
                else "startup" if code in ("binding_invalid", "lock_held", "invalid_configuration", "binding_conflict")
                else "service" if code in ("maintenance", "busy") else "verification")

    def fail_closed(self, exc, *, stop_gateway=False):
        """Stop every effect until the identity verifies again. The local spool sweep and body-free status keep running."""
        self._ready.clear()
        self.degrade(self._component(exc.code), exc.code)
        if stop_gateway and self.gateway is not None and self.gateway.running:
            self.gateway.stop()

    # ----- lifecycle -----------------------------------------------------------------------------------

    def start(self):
        with self._lifecycle:
            if any(t.is_alive() for t in self._threads.values()):
                return
            self._stop.clear()
            self._threads = {}
            self._media_swept, self._media_recovered = None, False
            try:
                self.load_config()
            except DomainError as exc:
                self.config = TelegramConfig()
                self.degrade("config", exc.message)
                self.sweep_spool(startup=True)
                self.sweep_media()
                self._watch_spool_files()
                self._watch_media_files()
                return
            self.recover_component("config")
            # Erase expired and orphaned secret files before any verification can fail or the integration stay disabled.
            self.sweep_spool(startup=True)
            # Sealed attachment snapshots are reconciled the same way: whatever Telegram's state, retention and cleanup go on.
            self.sweep_media()
            if not self.config.enabled:
                self._watch_spool_files()
                self._watch_media_files()
                return
            self._recovered = False
            self._verify_attempts = 0
            self._ready.clear()
            self._threads = {"control": threading.Thread(target=self._control_loop, name="telegram-control", daemon=True),
                             "poller": threading.Thread(target=self._poll_loop, name="telegram-poller", daemon=True)}
            for thread in self._threads.values():
                thread.start()

    def _watch_spool_files(self):
        """Keep expiring secret files while the process lives even though no control loop runs (disabled or unreadable
        configuration). A thread exists only while a still-valid file does, so an installation without one stays thread-free."""
        if self.spool is None or not any(self.spool.directory.glob("*" + SUFFIX)):
            return
        self._threads["spool"] = threading.Thread(target=self._spool_loop, name="telegram-spool", daemon=True)
        self._threads["spool"].start()

    def _watch_media_files(self):
        """The same for sealed attachment snapshots: a thread exists only while a snapshot record still needs upkeep."""
        if not self._media_open():
            return
        self._threads["media"] = threading.Thread(target=self._media_loop, name="telegram-media", daemon=True)
        self._threads["media"].start()

    def _media_open(self):
        """Whether any snapshot record is not yet deleted (a storage problem counts as yes, so it stays watched and visible)."""
        try:
            with self.read() as uow:
                return uow.connection.execute("SELECT 1 FROM telegram_file_payloads WHERE state != 'deleted' LIMIT 1").fetchone() is not None
        except Exception:  # noqa: BLE001
            return True

    def _media_loop(self):
        while not self._stop.wait(self.timings["media_sweep_interval"]):
            try:
                self.sweep_media()
                if not self._media_open():
                    return
            except Exception:  # noqa: BLE001 - local upkeep must never take the service down; the next pass tries again
                LOGGER.exception("Telegram media watch failed")

    def _spool_loop(self):
        while not self._stop.wait(self.timings["spool_sweep_interval"]):
            try:
                self.sweep_spool()
                if not any(self.spool.directory.glob("*" + SUFFIX)):
                    return
            except Exception:  # noqa: BLE001 - local upkeep must never take the service down; the next pass tries again
                LOGGER.exception("Telegram spool watch failed")

    @property
    def _thread(self):
        thread = self._threads.get("control")
        return thread if thread is not None and thread.is_alive() else None

    def stop(self, timeout=DEFAULT_STOP_TIMEOUT):
        """Bounded stop. An in-flight send finishes within its own connect/read timeouts; long polls are aborted."""
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            abort = getattr(self.api, "abort_polling", None)
            if abort:
                abort()
            deadline = time.monotonic() + timeout
            for thread in self._threads.values():
                thread.join(max(0.0, deadline - time.monotonic()))
            alive = [name for name, thread in self._threads.items() if thread.is_alive()]
            if alive:
                raise TimeoutError("Telegram worker did not stop: " + ", ".join(alive))
            self._threads = {}
            self._ready.clear()
            if self.gateway is not None:
                self.gateway.stop()
            if self.lock is not None:
                self.lock.release()
                self.lock = None
            self._wake.clear()

    def wake(self):
        self._wake.set()

    # ----- readiness: local checks, lock, remote verification --------------------------------------------

    @staticmethod
    def check_agent_chat(uow, chat):
        """The chat's configured agent must be the exact active Global agent of the chat's role. Role name alone is not enough."""
        profile = active_global(uow.connection, chat.agent_id)
        if profile is None or profile["role"]["id"] != AGENT_CHATS[chat.name]:
            raise DomainError("binding_invalid", f"The {chat.name} chat has no active Global {AGENT_CHATS[chat.name]} agent")

    def verify_local(self, uow, config):
        installation = uow.repository("installation").get("service")
        owner = uow.repository("principals").get(installation["owner_principal_id"]) if installation else None
        if not installation or not owner or not owner["active"] or owner["kind"] != "human":
            raise DomainError("binding_invalid", "The installation owner is missing or inactive")
        for chat in config.chats:
            if chat.name in AGENT_CHATS:
                self.check_agent_chat(uow, chat)
        return installation

    def verify_remote(self, api, config):
        me = api.get_me()
        if not isinstance(me, dict) or me.get("id") != config.bot_id or me.get("is_bot") is not True:
            raise DomainError("verification_failed", "The token does not belong to the expected bot")
        hook = api.get_webhook_info()
        if isinstance(hook, dict) and hook.get("url"):
            raise DomainError("webhook_present", "A webhook is installed for this bot; remove it explicitly before polling")
        # Each distinct provider chat is verified once, however many topics it backs. There is no Bot API read of one topic, so
        # this proves the forum group and the rights, not that each topic ID exists; later live acceptance covers each route.
        for chat_id in config.group_ids():
            bindings = config.chats_in(chat_id)
            chat = bindings[0]
            topics = any(binding.message_thread_id is not None for binding in bindings)
            where = ("the forum group of " + ", ".join(binding.name for binding in bindings)) if topics else f"the {chat.name} chat"
            info = api.get_chat(chat_id)
            if (not isinstance(info, dict) or info.get("id") != chat_id or info.get("type") != chat.chat_type
                    or info.get("username") or info.get("active_usernames")):
                raise DomainError("verification_failed", f"{where[0].upper() + where[1:]} is missing, the wrong type or public")
            if topics and info.get("is_forum") is not True:
                raise DomainError("verification_failed", f"{where[0].upper() + where[1:]} does not have topics enabled (it is not a forum)")
            if chat.chat_type == "private":
                continue
            bot = api.get_chat_member(chat_id, config.bot_id)
            # A channel needs the right to post; a group needs the right to delete (its posts and replies are cleaned up).
            right, action = ("can_post_messages", "post messages") if chat.chat_type == "channel" else ("can_delete_messages", "delete messages")
            if (not isinstance(bot, dict) or bot.get("status") not in ("administrator", "creator")
                    or (bot.get("status") == "administrator" and bot.get(right) is not True)):
                raise DomainError("verification_failed", f"The bot lacks administrator rights to {action} in {where}")
            owner = api.get_chat_member(chat_id, config.owner_telegram_user_id)
            if not isinstance(owner, dict) or owner.get("status") not in ("creator", "administrator", "member"):
                raise DomainError("verification_failed", f"The owner is not a member of {where}")

    def _ensure_ready(self):
        """Advance toward ready. Returns True when effects are allowed, else False after recording why."""
        if self.authority_check is not None:
            try:
                self.authority_check()
            except DomainError as exc:
                self.degrade("service", exc.code)
                return False
            self.recover_component("service")
        path = self._selected_path()
        if path and self._signature(path) != self._config_signature:
            self._ready.clear()
            try:
                self.load_config()
                self.recover_component("config")
            except DomainError as exc:
                self.degrade("config", exc.message)
                return False
            self._verify_attempts = 0
        if not self.config.enabled:
            self.degrade("config", "disabled")
            return False
        if self._ready.is_set():
            # Ready is not a standing licence: the owner and installation pin is revalidated on every pass.
            try:
                with self.read() as uow:
                    self.check_binding(uow)
            except DomainError as exc:
                if exc.code not in FAIL_CLOSED:
                    raise
                self.fail_closed(exc, stop_gateway=True)
                return False
            return True
        try:
            with self.write("readiness") as uow:
                installation = self.verify_local(uow, self.config)
                store.ensure_state(uow, self.config, installation, now=self.now())
            self.recover_component("binding")
            token = read_token(self.config)
            self.spool = self.spool or SecretSpool(self.wall_state_dir() / "spool")
            directory = require_private_directory(self.wall_state_dir(), create=True)
            require_unversioned(directory, getattr(self.runtime, "root", None), getattr(self.runtime, "workspace", None))
            self.spool.ensure()
            if self.lock is None:
                self.lock = PollerLock(directory, installation["installation_id"], self.config.bot_id)
            self.lock.acquire()
            api = self.api_factory(self.config, token)
            self.verify_remote(api, self.config)
            self.api = api
            self.recover()
            self._ready.set()
            for component in ("startup", "verification"):
                self.recover_component(component)
            self._verify_attempts = 0
            return True
        except TelegramApiError as exc:
            self.degrade("verification", "api_" + exc.kind)
        except DomainError as exc:
            if exc.code in MISMATCH_CODES:
                self.fail_closed(exc, stop_gateway=True)
            else:
                self.degrade(self._component(exc.code), exc.code)
        self._verify_attempts += 1
        return False

    def _ensure_gateway(self):
        """Serve the local program gateway while it is configured on; the poller lock is already held."""
        config = self.config
        wanted = bool(config.enabled and config.gateway.enabled)
        try:
            if wanted:
                if self.gateway is not None and self.gateway.running and self.gateway.path == config.gateway.socket_path:
                    return
                from .gateway import Gateway
                if self.gateway is not None:
                    self.gateway.stop()
                self.gateway = Gateway(self)
                self.gateway.start(config)
                self.recover_component("gateway")
            elif self.gateway is not None and self.gateway.running:
                self.gateway.stop()
                self.recover_component("gateway")
        except (DomainError, OSError) as exc:
            self.degrade("gateway", getattr(exc, "code", "socket_error"))

    def recover(self):
        """Startup recovery. An intent left dispatching may or may not have been sent, so it becomes uncertain."""
        now = self.now()
        with self.write("recover") as uow:
            uow.connection.execute(
                "UPDATE telegram_outbox SET state='uncertain', revision=revision+1, updated_at=?, "
                "error_code='interrupted', error_message='The service stopped after the send was attempted' "
                "WHERE state='dispatching'", (now,))
            uow.connection.execute(
                "UPDATE telegram_owner_inputs SET state='uncertain', revision=revision+1, updated_at=?, "
                "error_code='interrupted' WHERE state='submitting'", (now,))
            uow.connection.execute(
                "UPDATE telegram_cleanup_jobs SET state='queued', revision=revision+1, updated_at=? WHERE state='deleting'", (now,))
            referenced = self._spool_referenced(uow)
        if self.spool is not None:
            # Only the first recovery of a run may erase a fresh orphan: later ones can overlap an ingestion in flight.
            self.spool.sweep(referenced, now, orphan_grace=60.0 if self._recovered else 0.0)
        self._recovered = True

    @staticmethod
    def _spool_referenced(uow):
        """Spool files something still owns: a queued or submitting input, or a stored update that is not routed yet."""
        referenced = {row[0] for row in uow.connection.execute(
            "SELECT spool_id FROM telegram_owner_inputs WHERE spool_id IS NOT NULL AND state IN ('queued','submitting')")}
        return referenced | {spool for row in uow.connection.execute(
            "SELECT normalized_json FROM telegram_updates WHERE state='received' AND normalized_json LIKE '%spool_id%'")
            for spool in [json.loads(row[0]).get("spool_id")] if spool}

    def sweep_spool(self, *, startup=False):
        """Erase expired and orphaned secret files. Purely local: no token, network, poller lock or readiness is needed.

        It runs before verification at startup and then periodically while the process lives, so an unavailable Telegram,
        a failed verification or a disabled integration never keeps a secret on disk past its expiry. A still-valid file
        that a queued input or an unrouted stored update owns is kept until it is routed.
        """
        try:
            if self.spool is None:
                directory = self.wall_state_dir() / "spool"
                if not directory.is_dir():
                    return 0
                self.spool = SecretSpool(directory)
            referenced = None
            try:
                with self.read() as uow:
                    referenced = self._spool_referenced(uow)
            except Exception:  # noqa: BLE001 - storage unavailable: still erase what has expired, keep the rest
                LOGGER.warning("Telegram spool sweep could not read the queue; only expired files were erased")
            return self.spool.sweep(referenced, self.now(), orphan_grace=0.0 if startup else 60.0)
        except OSError:
            LOGGER.warning("Telegram spool sweep failed")
            return 0

    def sweep_media(self):
        """Startup reconciliation (until it has succeeded once), then retention and cleanup of the sealed attachment snapshots.

        Purely local: no token, network, poller lock or readiness is needed, and it never touches an open delivery's bytes or a
        valid copy lease. A problem is recorded as the visible ``media`` health code (never a path or a message) and leaves text,
        report and input processing untouched; the next pass tries again."""
        self._media_swept = self.clock()
        try:
            if not self._media_recovered:
                self.media.recover()
                self._media_recovered = True
            self.media.sweep()
        except Exception as exc:  # noqa: BLE001 - media upkeep must never disable anything else
            self.degrade("media", getattr(exc, "code", None) if isinstance(exc, DomainError) else (
                "storage_error" if isinstance(exc, OSError) else "internal_error"))
            LOGGER.warning("Telegram media upkeep failed (%s)", type(exc).__name__)
        else:
            self.recover_component("media")

    def _housekeeping(self):
        """Bounded local upkeep that must not wait for readiness."""
        now = self.clock()
        if self._spool_swept is None or (now - self._spool_swept).total_seconds() >= self.timings["spool_sweep_interval"]:
            self._spool_swept = now
            self.sweep_spool()
        if self._media_swept is None or (now - self._media_swept).total_seconds() >= self.timings["media_sweep_interval"]:
            self.sweep_media()

    # ----- workers ---------------------------------------------------------------------------------------

    def cycle(self):
        """One control pass; returns seconds to wait. Synchronous so tests can drive it with fake time."""
        self._housekeeping()
        if not self._ensure_ready():
            delay = VERIFY_RETRY_SECONDS[min(self._verify_attempts, len(VERIFY_RETRY_SECONDS) - 1)]
            return delay if self.config.enabled else 30
        self._ensure_gateway()
        worked = False
        failure = None
        for index, step in enumerate(self.steps):
            if index and self._stop.is_set():
                # Stopping: the step that was running has finished and recorded its outcome; no later step starts, so no
                # further external request (a deletion, a send) begins while the service is trying to stop.
                break
            # One failing step never stalls the others: a broken Signoff decision must not stop sends, cleanup or owner input.
            try:
                worked = bool(step()) or worked
            except DomainError as exc:
                if exc.code in FAIL_CLOSED:
                    self.fail_closed(exc, stop_gateway=True)
                    return 5
                failure = failure or exc.code
            except TelegramApiError as exc:
                failure = failure or "api_" + exc.kind
        if failure is not None:
            self.degrade("worker", failure)
            return 5
        self.recover_component("worker")
        return 0.1 if worked else self.tick

    def _control_loop(self):
        delay = 0.0
        while not self._stop.is_set():
            if delay and self._wake.wait(delay):
                self._wake.clear()
            if self._stop.is_set():
                return
            try:
                delay = self.cycle()
            except Exception:  # noqa: BLE001 - a worker bug degrades the bridge, never the service
                LOGGER.exception("Telegram control cycle failed")
                self.degrade("worker", "internal_error")
                delay = 5.0

    def _poll_loop(self):
        while not self._stop.is_set():
            if not self._ready.wait(0.5):
                continue
            try:
                delay = self.poll_once()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Telegram poll cycle failed")
                self.degrade("poller", "internal_error")
                delay = 5.0
            if delay and self._stop.wait(delay):
                return

    def poll_once(self):
        """Process stored unfinished updates, long-poll once, persist the batch with its cursor, then process it."""
        from .poller import poll_once
        return poll_once(self)

    # ----- status ---------------------------------------------------------------------------------------

    def status(self):
        """Owner status. Counts and states only: no bodies, no tokens, no chat identifiers (an unavailable target is named by its agent ID)."""
        try:
            config = self.config if self._selected_path() or self.config.enabled else TelegramConfig()
        except DomainError:
            config = TelegramConfig()
        result = {"enabled": bool(config.enabled), "state": self._state_label(config), "health": self.health(),
                  "configuration": config.redacted(), "media": self._media_status()}
        if not config.enabled:
            return result
        with self.read() as uow:
            state = store.get_state(uow)
            if state:
                result.update(last_poll_at=state["last_poll_at"], last_update_received_at=state["last_update_received_at"],
                              activation_at=state["activation_at"], config_generation=state["config_generation"],
                              outage=state["outage_json"], last_error=state["last_error_json"])
            result["binding"] = self._binding_status(uow, state)
            result.update(store.status_counts(uow, self.now()))
            result["cleanup_failures"] = [self._manual_cleanup(row, config) for row in result["cleanup_failures"]]
            result["signoffs"] = self.signoff_bridge.status_counts(uow)
        return result

    def _media_status(self):
        """Counts, bytes and one short code of the sealed attachment snapshots; a storage problem is a code, never a path."""
        try:
            return self.media.status()
        except Exception as exc:  # noqa: BLE001 - status must always answer
            return {"error_code": getattr(exc, "code", None) if isinstance(exc, DomainError) else "internal_error"}

    def _binding_status(self, uow, state):
        """Whether the pinned identity still matches, as a code and never an identifier. Reading it repairs nothing."""
        try:
            self.check_binding(uow)
            label = "matched"
        except DomainError as exc:
            label = exc.code
        return {"state": label, "generation": state["config_generation"] if state else None,
                "effective_at": state["binding_effective_at"] if state else None}

    @staticmethod
    def _manual_cleanup(row, config):
        """A failed deletion with what the owner needs to remove it by hand: where it is and, where Telegram has one, the link.

        Everything comes from what was saved for the message (its logical name, chat and topic), so the link always opens the
        original message and never a destination that was configured afterwards. ``still_configured`` says whether that
        destination is still the one the configuration names for it.
        """
        name = row["chat_name"]
        topic = row["message_thread_id"]
        provider_id = row["telegram_message_id"]
        current = config.destination(row["chat_id"], topic)
        link = None
        if provider_id is not None and re.fullmatch(r"-100[0-9]{5,}", str(row["chat_id"])):
            base = f"https://t.me/c/{str(row['chat_id'])[4:]}"
            link = f"{base}/{topic}/{provider_id}" if topic is not None else f"{base}/{provider_id}"
        label = name.replace("_", " ")
        where = f"the {label} topic" if topic is not None else f"the {label} chat"
        return {"message_id": row["message_id"], "error_code": row["error_code"], "reason": row["reason"],
                "chat": name, "message_thread_id": topic, "telegram_message_id": provider_id, "link": link,
                "still_configured": current is not None and current.name == name,
                "instructions": f"Delete this message by hand in {where}" + (f" (open {link})." if link else ".")}

    def _state_label(self, config):
        if not config.enabled:
            return "disabled"
        if self._ready.is_set() and not self._health:
            return "running"
        if self._threads:
            return "degraded" if self._health else "starting"
        return "stopped"
