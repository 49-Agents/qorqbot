"""Set-up and background work of contract extensions (EXTENSION-CONTRACT.md sections 3, 6 and 10).

``ExtensionSetups`` is the one place a set-up is run: the owner's card and Ordubashy's tool both go through it. It calls
the extension's ``SetupHandler`` only when somebody acts on that extension, and writes the outcome as the extension's
status through the registry (``ExtensionRegistry.set_status``). Nothing calls a handler at start or to show a list: the
list shows the status the registry holds. ``GatedService`` keeps an extension's background service running only while
the extension is ``ready`` and in effect for at least one agent.

``KeySetup`` is the ready-made ``SetupHandler`` for set-up kind ``key``: the owner types the values the manifest lists in
``setup.fields`` on the Connections card, and they go straight to the extension's private folder as ``setup.json``. A
typed value is never logged, never returned and never put into a note or an error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import logging
import threading

from qorqut_operations.contracts import DomainError

from .extension_private import PrivateFolder
from .extensions import ServiceDeclaration, SetupStart, SetupStatus
from .service import stopping

LOGGER = logging.getLogger(__name__)

SETUP_FILE = "setup.json"
CHECK_FAILED = "The values were not accepted. Check them and type them again."
# True both for a damaged file and for one saved before an update added or dropped a set-up field.
SET_UP_AGAIN = "The saved set-up does not fit this extension any more. Set it up again."


class KeySetup:
    """Set-up kind ``key``. ``fields`` are the manifest's ``setup.fields`` (each with an ``id``). ``check(values)`` is the
    extension's own test of the typed values before they are kept; it may ask its service. It accepts by returning None
    and refuses by raising ``DomainError``; anything else it returns is a refusal too. The message of that
    ``DomainError`` is shown to the owner and to Ordubashy as the note, so it must be a fixed sentence that never contains
    any part of a value. (``ExtensionSetups.accept`` replaces a note that contains a whole typed value with a fixed one; a
    partial quote cannot be caught.)

    The status is ``not_set_up`` while nothing is saved, ``ready`` while ``setup.json`` reads as exactly the current
    fields, and ``needs_attention`` otherwise (a file that is damaged or not private, or one saved before an update of the
    extension changed its fields)."""

    def __init__(self, folder: PrivateFolder, fields, check: Callable[[Mapping[str, str]], None]):
        if not isinstance(folder, PrivateFolder):
            raise DomainError("invalid_installation", "A key set-up keeps its values in the extension's private folder")
        ids = tuple(getattr(field, "id", None) for field in fields)
        if not ids or not all(isinstance(name, str) and name for name in ids) or len(set(ids)) != len(ids):
            raise DomainError("invalid_installation", "A key set-up needs the manifest's setup fields, each with its own id")
        if not callable(check):
            raise DomainError("invalid_installation", "A key set-up needs a check of the typed values")
        self.folder, self.fields, self.check = folder, ids, check

    def status(self) -> SetupStatus:
        try:
            saved = self.values()
        except DomainError:
            return SetupStatus("needs_attention", SET_UP_AGAIN)
        return SetupStatus("not_set_up" if saved is None else "ready")

    def start(self, request) -> SetupStatus:
        # The owner types the values on the card; Qorqbot holds "setting up" meanwhile.
        return self.status()

    def accept(self, values: Mapping[str, str]) -> SetupStatus:
        """Keep the owner's values when ``check`` accepts them. A refused check keeps what was saved before and gives
        ``needs_attention`` with the check's fixed message."""
        if not isinstance(values, Mapping) or any(name not in self.fields for name in values):
            raise DomainError("validation_error", "Only these values are accepted: " + ", ".join(self.fields))
        missing = [name for name in self.fields if not (isinstance(values.get(name), str) and values[name])]
        if missing:
            raise DomainError("validation_error", "These values are missing: " + ", ".join(missing))
        typed = {name: values[name] for name in self.fields}
        try:
            answer = self.check(dict(typed))
        except DomainError as problem:
            return SetupStatus("needs_attention", str(problem.message or "") or CHECK_FAILED)
        except Exception:
            return SetupStatus("needs_attention", CHECK_FAILED)
        if answer is not None:
            return SetupStatus("needs_attention", CHECK_FAILED)
        self.folder.write(SETUP_FILE, json.dumps(typed, sort_keys=True).encode())
        return self.status()

    def image(self) -> bytes | None:
        return None

    def cancel(self) -> SetupStatus:
        return self.status()

    def disconnect(self) -> SetupStatus:
        self.folder.remove(SETUP_FILE)
        return self.status()

    def values(self) -> dict | None:
        """The saved values, for the extension's own work only; None when nothing is saved. Saved values that cannot be
        read as exactly the current fields are refused with a fixed message."""
        data = self.folder.read(SETUP_FILE)
        if data is None:
            return None
        try:
            saved = json.loads(data)
        except ValueError:
            raise DomainError("operation_unavailable", SET_UP_AGAIN) from None
        if not isinstance(saved, dict) or set(saved) != set(self.fields) or not all(isinstance(v, str) for v in saved.values()):
            raise DomainError("operation_unavailable", SET_UP_AGAIN)
        return saved


# -- running a set-up -------------------------------------------------------------------------------------------------
HANDLER_FAILED = "Set-up stopped with an error. Try again."
NOT_LOADED = "The extension's code is not loaded, so it cannot be set up."
PNG = b"\x89PNG\r\n\x1a\n"     # the pairing image is a PNG: its first eight bytes


class ExtensionSetups:
    """Runs the set-up of the installed extensions. ``registry`` is the ``ExtensionRegistry``; ``handler(ext_id)`` gives
    the loaded extension's ``setup_handler()``, or None when its code is not loaded.

    Each action takes the extension's own lock, calls its handler and writes what the handler reports as the status, in
    one transaction of the registry's store. A start that the handler answers with ``not_set_up`` (the owner still has to
    type a key or sign in) is written as ``setting_up``. A ``DomainError`` from a handler is a refusal: it is raised again
    and the status is left as it was. ``accept`` holds the typed values, so it is the one place that screens them: a note
    or a refusal message that contains a whole typed value is replaced by a fixed sentence. Any other error writes ``needs_attention`` with a fixed note; only the kind of error
    is logged. For set-up kind ``none`` there is nothing to do and every action answers the status as it is. Every
    action answers the extension as the registry now lists it (``InstalledExtension``)."""

    def __init__(self, registry, handler: Callable[[str], object]):
        self.registry, self._handler = registry, handler
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, ext_id):
        with self._guard:
            return self._locks.setdefault(ext_id, threading.Lock())

    def _usable(self, ext_id):
        entry = self.registry.get(ext_id)
        if entry.refused_findings or entry.status == "unsupported" or entry.manifest is None:
            raise DomainError("invalid_transition", f"The extension {ext_id!r} cannot be set up: {entry.note}")
        return entry

    def status(self, ext_id, *, connection=None):
        """The extension as the registry lists it. No handler is asked."""
        return self.registry.get(ext_id, connection=connection)

    def _run(self, ext_id, action, call, *, starting=False, screen=lambda text: text):
        entry = self._usable(ext_id)
        if entry.manifest.setup_kind == "none":
            return entry
        handler = self._handler(ext_id)
        if handler is None:
            raise DomainError("operation_unavailable", NOT_LOADED)
        with self._lock(ext_id):
            try:
                outcome = call(handler)
            except DomainError as problem:
                message = str(problem.message or "")
                if screen(message) == message:
                    raise
                raise DomainError(problem.code, screen(message)) from None
            except Exception as problem:  # noqa: BLE001 - a broken handler is the extension's note, never its text
                LOGGER.warning("extension_setup_failed extension=%s action=%s error=%s", ext_id, action,
                               type(problem).__name__)
                outcome = None
            if not isinstance(outcome, SetupStatus):
                outcome = SetupStatus("needs_attention", HANDLER_FAILED)
            state = "setting_up" if starting and outcome.state == "not_set_up" else outcome.state
            with self.registry.database.transaction() as uow:
                self.registry.set_status(ext_id, state, screen(outcome.note) or None, uow=uow)
        return self.registry.get(ext_id)

    def start(self, ext_id, request: SetupStart):
        if not isinstance(request, SetupStart):
            raise DomainError("validation_error", "A set-up start says who asked for it.")
        return self._run(ext_id, "start", lambda handler: handler.start(request), starting=True)

    def accept(self, ext_id, values: Mapping[str, str]):
        """The owner's values from the card; they reach the handler only and are never logged or answered."""
        typed = [value for value in (values.values() if isinstance(values, Mapping) else ())
                 if isinstance(value, str) and value]

        def screen(text):
            return CHECK_FAILED if text and any(value in text for value in typed) else text
        return self._run(ext_id, "accept", lambda handler: handler.accept(values), screen=screen)

    def cancel(self, ext_id):
        return self._run(ext_id, "cancel", lambda handler: handler.cancel())

    def disconnect(self, ext_id):
        return self._run(ext_id, "disconnect", lambda handler: handler.disconnect())

    def image(self, ext_id) -> bytes | None:
        """The pairing code image for the owner's route: a PNG, or None for anything else. The status is not changed."""
        entry = self._usable(ext_id)
        handler = None if entry.manifest.setup_kind == "none" else self._handler(ext_id)
        if handler is None:
            return None
        with self._lock(ext_id):
            try:
                image = handler.image()
            except Exception as problem:  # noqa: BLE001 - no image is shown, the card keeps its status
                LOGGER.warning("extension_setup_failed extension=%s action=image error=%s", ext_id, type(problem).__name__)
                return None
        if not (isinstance(image, bytes) and image.startswith(PNG)):
            if image is not None:
                LOGGER.warning("extension_setup_failed extension=%s action=image error=not_png", ext_id)
            return None
        return image


# -- background work --------------------------------------------------------------------------------------------------
def _in_background(work):
    threading.Thread(target=work, name="extension-gate", daemon=True).start()


class GatedService:
    """One ``ServiceDeclaration`` of a contract extension, run only while the extension is ``ready`` and in effect for
    at least one agent (``ExtensionRegistry.active``, rule 9).

    ``declaration()`` is what joins the service lifetime in place of the extension's own: the lifetime starts and stops
    this object, and the recovery barrier drains and resumes it, as it would the extension's service. While the lifetime
    has it started, every registry change of the extension (``ExtensionRegistry.subscribe``) checks again whether the
    inner service should run, and starts or stops it. Stopping for the barrier or the lifetime stops the inner service
    through the extension's own stop adapter, so its proof is the extension's. ``changes`` is the process's change feed:
    a commit that writes agent profiles (an agent retired, or its role changed) checks again too, since it writes nothing
    to the registry. ``schedule`` runs a check off the committing thread (a thread of its own unless a test gives
    another).

    ``running()`` says whether the lifetime has this step started, not whether the inner service runs: the barrier
    resumes only what it found running, and a gated off service must come back after it. An inner start that fails is
    logged and not running; the next change tries again."""

    def __init__(self, ext_id, declaration: ServiceDeclaration, registry, *, changes=None, schedule=_in_background):
        self.ext_id, self.inner, self.registry, self._schedule = ext_id, declaration, registry, schedule
        self._inner_stop = declaration.stop or stopping.void(lambda service, timeout: service.stop(timeout=timeout))
        self._lock = threading.RLock()
        self._started = False       # the lifetime has this step started
        self._running = False       # the inner service was started and not proven stopped since
        registry.subscribe(self.changed)
        if changes is not None:
            changes.add_listener(self.feed)

    def declaration(self) -> ServiceDeclaration:
        inner = self.inner
        return ServiceDeclaration(inner.name, self, inner.start_before, inner.stop_before,
                                  start=lambda service: service.start(), stop=self._stop,
                                  running=lambda service: service.running(), timeout=inner.timeout)

    def start(self):
        with self._lock:
            self._started = True
        self.reconcile()

    def _stop(self, name, service, timeout):
        with self._lock:
            self._started = False
            if not self._running:
                return stopping.drained(name)
            proof = self._inner_stop(name, self.inner.service, timeout)
            self._running = getattr(proof, "state", None) != "drained"
            return proof

    def running(self) -> bool:
        with self._lock:
            return self._started

    def changed(self, changes):
        """The registry's subscriber, on the committing thread: only schedules a check."""
        if any(change.extension == self.ext_id for change in changes):
            self._schedule(self.reconcile)

    def feed(self, event):
        """The change feed's listener, on the committing thread: only schedules a check."""
        if "agent_profiles" in (event.get("tables") or ()):
            self._schedule(self.reconcile)

    def reconcile(self):
        with self._lock:
            if not self._started:
                return
            try:
                self._reconcile()
            except Exception as problem:  # noqa: BLE001 - the next change or the next start checks again
                LOGGER.warning("extension_service_failed extension=%s service=%s error=%s", self.ext_id, self.inner.name,
                               type(problem).__name__)

    def _reconcile(self):
        wanted = self.registry.active(self.ext_id)
        if wanted and self._running and self.inner.running and not self.inner.running(self.inner.service):
            # The inner service ended by itself: this check starts it again.
            self._running = False
        if wanted and not self._running:
            (self.inner.start or (lambda service: service.start()))(self.inner.service)
            self._running = True
        elif not wanted and self._running:
            proof = self._inner_stop(self.inner.name, self.inner.service, self.inner.timeout)
            # Only a drained proof is a stop; anything else still runs and the next check stops it again.
            self._running = getattr(proof, "state", None) != "drained"
