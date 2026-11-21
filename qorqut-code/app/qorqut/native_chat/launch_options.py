"""The owner's per-agent launch options (client, model, effort), behind two owner-only routes.

``GET  /api/root/agents/<id>/chat/options``   -> the view
``POST /api/root/agents/<id>/chat/options``   ``{client?, model?, effort?}`` (``null`` clears one) -> the same view
``GET  /api/root/agents/<id>/chat/models``    -> ``{client, models}`` the models and efforts the agent's client reports
                                                (``managed_runtime.model_catalog``); ``models`` is ``[]`` when it could not be asked

The view is ``{client, model, effort, effective_client, clients: {claude: {ready}, codex: {ready}}, session_open}``: ``client``,
``model`` and ``effort`` are what is stored (``null`` = not set), ``effective_client`` is the client an open would use now
(``managed_runtime.options.effective_client``), ``clients`` says which CLIs are installed and signed in, ``session_open`` whether
the agent's session is open (a host is running for it, or one claims to and cannot be verified).

When the installation runs its agents in terminals (``managed_runtime.mode``) the view and the change stay available (a terminal
start passes the stored model and effort too), ``session_open`` is false (no host exists to hold the client) and a change of
``client`` to Codex is refused: only Claude Code agents run in terminals.

The routes are registered in ``http.py``, which refuses any request carrying an ``Authorization`` header before it gets here. The
owner guard is the one of every other Chat route (``chat._target``: the installation owner, an active registered agent). A change
of ``client`` while the session is open is refused ``session_open``; model and effort may change at any time. A model or effort
that is set (not cleared) is also handed to an open session at once (the host's ``model`` command), so it applies from the next
message; the view's ``applied`` says whether that happened. A cleared value applies from the next session start. Nothing here starts or stops a session. The storage and rules are ``managed_runtime/options.py``.
"""

from __future__ import annotations

from contextlib import nullcontext

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import mode, models, options as stored

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


def _label(profile: dict) -> str:
    try:
        from qorqut.native_chat import store
        return store.profile_label(profile)
    except Exception:  # noqa: BLE001 - an unreadable profile is "this agent"
        return ""


def _view(chat, runtime, profile: dict, agent_id: str, *, ready=None, applied=False) -> dict:
    ready = ready or stored.ready_function()
    options = stored.read(runtime.paths, agent_id)
    effective = stored.effective_client(runtime.paths, agent_id, profile, ready)
    # ``model_fits``: the stored model can be used by the client an open would use (a model of the other client's family is
    # not passed to it: the client's own default applies until the owner picks one). ``suggestions``: names offered per client.
    return {**options.public(), "effective_client": effective, "model_fits": models.fits(effective, options.model),
            "clients": {name: {"ready": bool(ready(name))} for name in ("claude", "codex")},
            "suggestions": {name: list(models.SUGGESTIONS[name]) for name in ("claude", "codex")},
            "session_open": False if mode.is_terminal(runtime.paths) else _session_open(getattr(chat, "managed", None), agent_id),
            "applied": applied}


def view(chat, runtime, principal_id: str, agent_id: str, *, ready=None) -> dict:
    return _view(chat, runtime, _guard(chat, principal_id, agent_id), agent_id, ready=ready)


def change(chat, runtime, principal_id: str, agent_id: str, body, *, ready=None) -> dict:
    """Apply the owner's change and return the new view. ``DomainError``: ``validation_error`` (bad body or value),
    ``session_open`` (client change while open), ``session_opening`` (an open is in progress; retryable),
    ``operation_unavailable`` (the file could not be saved).

    A model must belong to the client the agent will use after the change (``managed_runtime.models``): a *named* model of the other
    client is refused with a plain sentence; a model that was already stored and no longer fits because the client changed is
    cleared in the same write (effort is valid for both clients and stays)."""
    profile = _guard(chat, principal_id, agent_id)
    if not isinstance(body, dict):
        raise DomainError("validation_error", "The options must be an object")
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise DomainError("validation_error", "Request fields are invalid",
                          field_errors={name: "unknown field" for name in unknown})
    ready = ready or stored.ready_function()
    terminals = mode.is_terminal(runtime.paths)
    managed = None if terminals else getattr(chat, "managed", None)
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
            if terminals and wanted == "codex":
                raise DomainError("validation_error", "Only Claude Code agents can run in terminals.")
            before = stored.effective_client(runtime.paths, agent_id, profile, ready)
            if _session_open(managed, agent_id) and stored.choose(wanted, profile, ready) != before:
                raise DomainError("session_open", "A session is open. End it, then change the client")
        # The client the agent uses once this change is in: the one named, else the stored one, else the one an open would pick.
        current = stored.read(runtime.paths, agent_id)
        try:
            after = stored.choose(stored._valid("client", body["client"]) if "client" in body else current.client, profile, ready)
            model = stored._valid("model", body["model"]) if "model" in body else current.model
        except stored.OptionsError as error:
            raise DomainError(error.code, str(error)) from None
        if model and not models.fits(after, model):
            if "model" in body:
                raise DomainError("validation_error", models.mismatch_text(after, model, _label(profile) or None))
            body = {**body, "model": None}
        try:
            stored.write(runtime.paths, agent_id, **body)
        except stored.OptionsError as error:
            raise DomainError(error.code, str(error)) from None
        except (OSError, ValueError):
            raise DomainError("operation_unavailable", "The options could not be saved") from None
        applied = _apply_live(managed, agent_id, body.get("model"), body.get("effort"))
    return _view(chat, runtime, profile, agent_id, ready=ready, applied=applied)


def _apply_live(managed, agent_id: str, model, effort) -> bool:
    """Hand a newly set model and/or effort to the agent's open session (the host's ``model`` command). False when no session is
    open, nothing was set, or the session did not take it: the stored value then applies at the next start."""
    if managed is None or (model is None and effort is None):
        return False
    try:
        host = managed.handle(agent_id)
        if host is None:
            return False
        reply = host.client.command("model", model=model, effort=effort)
    except Exception:  # noqa: BLE001 - the stored value still applies at the next start
        return False
    return isinstance(reply, dict) and reply.get("status") == "ok" and bool(reply.get("accepted"))


def models_view(chat, runtime, principal_id: str, agent_id: str, *, ready=None, catalog=None) -> dict:
    """The models and efforts the agent's client reports. ``client`` is the client an open would use now."""
    from qorqut.managed_runtime import model_catalog
    profile = _guard(chat, principal_id, agent_id)
    ready = ready or stored.ready_function()
    client = stored.effective_client(runtime.paths, agent_id, profile, ready)
    found = (catalog or model_catalog.catalog)(client) if client else None
    return {"client": client, "models": (found or {}).get("models") or []}
