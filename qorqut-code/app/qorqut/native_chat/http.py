"""Owner-only HTTP surface: /api/root/agents/<uuid>/chat/...

Mounted by the shared transport after owner authentication. ``root`` is only the transport: the resource is the exact agent
UUID, global or project. An agent bearer credential is refused outright, an agent that is not an active registered profile is
not found, and a read never starts or resumes anything. The conversation is derived from the transcript archive; the response
shapes are described in ``docs/v1.2/NATIVE_CHAT_DTO.md``.

Every agent runs as a headless session (see ``managed_runtime``). All routes are owner-only:

* ``POST   .../messages``   ``{text, idempotency_key, expected_binding?, new_conversation?}``: the pin may be left out only when the
  agent has no session open; the message then opens one. That open is refused ``resume_unavailable`` when the
  agent's earlier conversation can no longer be continued; ``new_conversation: true`` (no session open, no
  pin) is the owner's explicit choice to start a fresh one instead. Retryable refusals: ``open_timeout``, ``open_busy``; a retry
  with the same ``idempotency_key`` neither sends the message twice nor opens a second session.
* ``POST   .../interrupt``  ``{expected_binding}`` -> ``{stopped}``: stop the running turn.
* ``POST   .../answer``     ``{expected_binding, request_id, decision}`` -> ``{answered}``: ``decision`` is ``"allow"``/``"deny"``
  for a permission request, ``{"answers": {<question text>: <answer>}}`` for a question.
* ``POST   .../close``      ``{expected_binding?}`` -> ``{closing}``: end the open session and turn the agent off.

* ``GET/POST .../options``  ``{client?, model?, effort?}`` -> ``{client, model, effort, effective_client, clients, session_open}``: the
  agent's launch options (``launch_options.py``); a change of ``client`` while a session is open is refused ``session_open``.

``GET .../commands`` marks the commands a terminal alone can show ``unavailable``, and the ``changed`` stream event carries
``managed`` (the session summary).

When the installation runs its agents in terminals (``managed_runtime.mode``, asked per request) the same routes answer for a
terminal agent: the state has ``execution: "terminal"`` and no ``managed``, a message needs the pin of the live session,
``interrupt``/``answer``/``close`` are refused ``not_managed``, ``GET .../commands`` marks nothing unavailable, and
``GET .../attachment`` serves the selected-conversation attachment of the agent's terminal (a managed installation does not
register it).
"""

from __future__ import annotations

import json
import re
import time
from functools import partial
from urllib.parse import unquote_to_bytes

import anyio
from starlette.responses import StreamingResponse

from qorqut_operations.contracts import DomainError, OperationResult
from qorqut.managed_runtime import mode
from qorqut.transport.http import check_body_length, checked_fields, read_json
from qorqut.transport.results import response

from .contracts import PROVIDERS

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CONVERSATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
POLL_SECONDS = 0.4
HEARTBEAT_SECONDS = 15


def matches(parts):
    return parts == ["root", "thread-attachments"] or (
        len(parts) >= 4 and parts[0] == "root" and parts[1] == "agents" and parts[3] == "chat")


def _conversation(request):
    """The explicit ``conversation`` + ``provider`` selection of a state/history read, as keyword arguments. Both or neither:
    a native id alone is ambiguous across providers, and the facade refuses a half selection."""
    raw, provider = request.query_params.get("conversation"), request.query_params.get("provider")
    if raw is not None and not _CONVERSATION.fullmatch(raw):
        raise DomainError("validation_error", "conversation must be a native conversation id")
    if provider is not None and provider not in PROVIDERS:
        raise DomainError("validation_error", "provider must be claude or codex")
    return {"conversation": raw, "provider": provider}


def _integer(request, name, default=None, *, minimum=0):
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]{1,12}", raw) or int(raw) < minimum:
        raise DomainError("validation_error", f"{name} must be a non-negative integer")
    return int(raw)


async def route(runtime, request, principal_id, parts, request_id):
    """Handle one chat request. ``parts`` is the full path split, already known to satisfy :func:`matches`."""
    if request.headers.get("authorization") is not None:
        raise DomainError("forbidden", "Agent chat is for the owner's dashboard")
    thread_upload = parts == ["root", "thread-attachments"]
    chat = runtime.native_chat
    run = anyio.to_thread.run_sync
    if thread_upload:
        if request.method != "POST":
            raise DomainError("not_found", "Attachment endpoint is not registered")
        agent_id = await run(partial(chat.thread_upload_target, principal_id))
        tail = ["attachments"]
    else:
        if not _UUID.fullmatch(parts[2]):
            raise DomainError("not_found", "Select an active registered agent")
        agent_id, tail = parts[2], parts[4:]

    def ok(data):
        result = response(OperationResult.ok(data, request_id=request_id))
        result.headers["Cache-Control"] = "private, no-store"
        return result

    if request.method == "GET":
        if not tail:
            return ok(await run(partial(chat.state, principal_id, agent_id, before=_integer(request, "before"),
                                        limit=_integer(request, "limit", 50, minimum=1), **_conversation(request))))
        if tail == ["history"]:
            before = _integer(request, "before")
            if before is None:
                raise DomainError("validation_error", "before is required")
            return ok(await run(partial(chat.history, principal_id, agent_id, before=before,
                                        limit=_integer(request, "limit", 50, minimum=1), **_conversation(request))))
        if tail == ["attachment"]:
            if not mode.is_terminal(runtime.paths):
                raise DomainError("not_found", "Chat endpoint is not registered")
            return ok(await run(partial(chat.attachment_state, principal_id, agent_id)))
        if tail == ["commands"]:
            # Owner-only like every Chat read; the catalog reads the agent's folder and the owner's own commands.
            await run(partial(chat.check_events, principal_id, agent_id))
            from qorqut.composition.locations import agent_workspace
            from qorqut.connectors import _probe_environment
            from .commands import catalog
            client = request.query_params.get("client") or ""
            workspace = agent_workspace(runtime.paths, agent_id)
            environment = _probe_environment()
            return ok(await run(partial(catalog, client, home=environment.get("HOME"),
                                        claude_home=environment.get("CLAUDE_CONFIG_DIR"), workspace=workspace,
                                        managed=not mode.is_terminal(runtime.paths))))
        if tail == ["events"]:
            return await _events(chat, request, principal_id, agent_id)
        if tail == ["options"]:
            from . import launch_options
            return ok(await run(partial(launch_options.view, chat, runtime, principal_id, agent_id)))
        if tail == ["models"]:
            from . import launch_options
            return ok(await run(partial(launch_options.models_view, chat, runtime, principal_id, agent_id)))
        raise DomainError("not_found", "Chat endpoint is not registered")
    if request.method != "POST":
        raise DomainError("not_found", "Chat endpoint is not registered")
    if tail == ["attachments"]:
        from .uploads import MAX_BYTES
        # Authorize before reading any bytes; the facade repeats the check at
        # the save boundary and pins the upload to the exact live conversation.
        await run(partial(chat.check_events, principal_id, agent_id))
        check_body_length(request, MAX_BYTES)
        encoded = request.headers.get("x-file-name", "")
        try:
            name = unquote_to_bytes(encoded).decode("utf-8")
        except UnicodeError:
            raise DomainError("validation_error", "The filename is invalid") from None
        body, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BYTES:
                raise DomainError("payload_too_large", "Files must be 25 MB or smaller")
            body.append(chunk)
        if thread_upload:
            return ok(await run(partial(chat.upload_thread, principal_id, agent_id,
                                        request.headers.get("idempotency-key"), name, b"".join(body))))
        return ok(await run(partial(chat.upload, principal_id, agent_id, request.headers.get("idempotency-key"),
                                    request.headers.get("x-chat-binding"), name, b"".join(body))))
    body = await read_json(request, limit=64 * 1024)
    if tail == ["messages"]:
        # The pin is required unless the agent has no session open, which the facade decides.
        checked_fields(body, {"text", "idempotency_key", "expected_binding", "new_conversation"}, {"text", "idempotency_key"})
        return ok(await run(partial(chat.send, principal_id, agent_id, body["text"], body["idempotency_key"],
                                    body.get("expected_binding"), new_conversation=body.get("new_conversation", False))))
    if tail == ["interrupt"]:
        checked_fields(body, {"expected_binding"}, {"expected_binding"})
        return ok(await run(partial(chat.interrupt, principal_id, agent_id, body["expected_binding"])))
    if tail == ["answer"]:
        fields = {"expected_binding", "request_id", "decision"}
        checked_fields(body, fields, fields)
        return ok(await run(partial(chat.answer, principal_id, agent_id, body["expected_binding"], body["request_id"],
                                    body["decision"])))
    if tail == ["close"]:
        checked_fields(body, {"expected_binding"})
        return ok(await run(partial(chat.close, principal_id, agent_id, body.get("expected_binding"))))
    if tail == ["options"]:
        # Owner-only like every Chat route (the Authorization header was refused above). The fields are checked by the handler.
        from . import launch_options
        return ok(await run(partial(launch_options.change, chat, runtime, principal_id, agent_id, body)))
    if tail == ["select"]:
        # The owner's explicit selection of the running native conversation: the one way to replace a selection.
        fields = {"provider", "conversation_id", "expected_generation"}
        checked_fields(body, fields, fields)
        return ok(await run(partial(chat.select, principal_id, agent_id, body["provider"], body["conversation_id"],
                                    body["expected_generation"])))
    if tail == ["read"]:
        checked_fields(body, {"sequence"}, {"sequence"})
        return ok(await run(partial(chat.mark_read, principal_id, agent_id, body["sequence"])))
    raise DomainError("not_found", "Chat endpoint is not registered")


async def _events(chat, request, principal_id, agent_id):
    """SSE replay-then-tail by durable cursor (``Last-Event-ID`` or ``?cursor=``); each poll re-authorizes the owner."""
    run = anyio.to_thread.run_sync
    await run(partial(chat.check_events, principal_id, agent_id))
    header = request.headers.get("last-event-id")
    if header is not None and not re.fullmatch(r"[0-9]{1,18}", header):
        header = None
    cursor = int(header) if header is not None else _integer(request, "cursor", 0)

    from ..transport.http import STOPPING  # set when the service is asked to stop; the stream ends instead of holding the shutdown

    async def stream():
        position, beat = cursor, time.monotonic()
        limiter = anyio.CapacityLimiter(1)
        try:
            found, latest = await run(partial(chat.events, principal_id, agent_id, position, limit=1), limiter=limiter)
            if position > latest:
                # The cursor is ahead of anything this installation has (a restored database): the client must reload.
                yield b"event: reset\ndata: {}\n\n"
                position = 0
            while not STOPPING.is_set() and not await request.is_disconnected():
                events, latest = await run(partial(chat.events, principal_id, agent_id, position), limiter=limiter)
                for event in events:
                    position = event["cursor"]
                    yield (f"id: {position}\nevent: chat\ndata: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
                    beat = time.monotonic()
                if len(events) >= 200:
                    continue
                if time.monotonic() - beat >= HEARTBEAT_SECONDS:
                    yield b": heartbeat\n\n"
                    beat = time.monotonic()
                await anyio.sleep(POLL_SECONDS)
        except DomainError:
            yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
