"""The owner's per-agent launch options (client, model, effort), behind two owner-only routes.

``GET  /api/root/agents/<id>/chat/options``   -> the view
``POST /api/root/agents/<id>/chat/options``   ``{client?, model?, effort?}`` (``null`` clears one) -> the same view

The view is ``{client, model, effort, effective_client, clients: {claude: {ready}, codex: {ready}}, session_open}``: ``client``,
``model`` and ``effort`` are what is stored (``null`` = not set), ``effective_client`` is the client an open would use now
(``managed_runtime.options.effective_client``), ``clients`` says which CLIs are installed and signed in, ``session_open`` whether
the agent's session is open (a host is running for it, or one claims to and cannot be verified).

The routes are registered in ``http.py``, which refuses any request carrying an ``Authorization`` header before it gets here. The
owner guard is the one of every other Chat route (``chat._target``: the installation owner, an active registered agent). A change
of ``client`` while the session is open is refused ``session_open``; model and effort may change at any time and apply from the
next session start. Nothing here starts or stops a session. The storage and rules are ``managed_runtime/options.py``.
"""

from __future__ import annotations

from contextlib import nullcontext

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import options as stored

FIELDS = stored.FIELDS


def _guard(chat, principal_id: str, agent_id: str) -> dict:
    """The agent's profile, after the same owner and agent checks as every Chat read."""
    with chat.database.read() as uow:
        return chat._target(uow, principal_id, agent_id)


def _session_open(managed, agent_id: str) -> bool:
    """Whether a host runs for this agent. One that cannot be verified counts as running: fail closed."""
    if managed is None:
        return False
    try:
        return managed.handle(agent_id) is not None
    except Exception:  # noqa: BLE001
        return True


def _view(chat, runtime, profile: dict, agent_id: str, *, ready=None) -> dict:
    ready = ready or stored.ready_function()
    options = stored.read(runtime.paths, agent_id)
    return {**options.public(), "effective_client": stored.effective_client(runtime.paths, agent_id, profile, ready),
            "clients": {name: {"ready": bool(ready(name))} for name in ("claude", "codex")},
            "session_open": _session_open(getattr(chat, "managed", None), agent_id)}


def view(chat, runtime, principal_id: str, agent_id: str, *, ready=None) -> dict:
    return _view(chat, runtime, _guard(chat, principal_id, agent_id), agent_id, ready=ready)


def change(chat, runtime, principal_id: str, agent_id: str, body, *, ready=None) -> dict:
    """Apply the owner's change and return the new view. ``DomainError``: ``validation_error`` (bad body or value),
    ``session_open`` (client change while open), ``session_opening`` (an open is in progress; retryable),
    ``operation_unavailable`` (the file could not be saved)."""
    profile = _guard(chat, principal_id, agent_id)
    if not isinstance(body, dict):
        raise DomainError("validation_error", "The options must be an object")
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise DomainError("validation_error", "Request fields are invalid",
                          field_errors={name: "unknown field" for name in unknown})
    ready = ready or stored.ready_function()
    managed = getattr(chat, "managed", None)
    # No open of this agent runs while the client changes (the lifecycle lock, as the runtime switch does it).
    hold = managed.hold(agent_id) if managed is not None and hasattr(managed, "hold") and "client" in body else nullcontext()
    with hold:
        if "client" in body:
            try:
                wanted = stored._valid("client", body["client"])
            except stored.OptionsError as error:
                raise DomainError(error.code, str(error)) from None
            # A change is what moves the client an open would use: naming the one already in use, or clearing a choice that
            # resolves to it anyway, changes nothing and is allowed while the session is open.
            before = stored.effective_client(runtime.paths, agent_id, profile, ready)
            if _session_open(managed, agent_id) and stored.choose(wanted, profile, ready) != before:
                raise DomainError("session_open", "A session is open. End it, then change the client")
        try:
            stored.write(runtime.paths, agent_id, **body)
        except stored.OptionsError as error:
            raise DomainError(error.code, str(error)) from None
        except (OSError, ValueError):
            raise DomainError("operation_unavailable", "The options could not be saved") from None
    return _view(chat, runtime, profile, agent_id, ready=ready)
