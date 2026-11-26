"""The owner's WhatsApp card in Settings > Connections: setup state, linking from Settings, and the code image.

``GET /api/root/extensions/whatsapp/setup`` answers ``{"state", "note", "qr", "send_ready"}``; ``POST`` with
``{"action": "link" | "cancel"}`` starts linking for the installation's active Global Ordubashy or stops it;
``GET /api/root/extensions/whatsapp/qr`` is the current code image (no-store), 404 when none is shown. Owner only, after
the host's cookie, Origin and CSRF checks; a bearer credential is refused."""
from __future__ import annotations

from qorqut_operations.contracts import DomainError

BASE = ("root", "extensions", "whatsapp")


def _owner(api, request, actor):
    if request.headers.get("authorization") is not None:
        raise DomainError("forbidden", "WhatsApp is set up from the owner's dashboard")
    context, _ = api.context(actor)
    with api.runtime.database.read() as uow:
        owner = uow.repository("installation").get("service")
    if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
        raise DomainError("forbidden", "Only the installation owner can set up WhatsApp")


def _setup(extension):
    if extension.setup is None:
        raise DomainError("operation_unavailable", "WhatsApp is not part of this installation")
    return extension.setup


def _answer(api, data, request_id):
    from qorqut_operations.contracts import OperationResult
    from qorqut.transport.http import response
    result = response(OperationResult.ok(data, request_id=request_id), statuses=api.statuses)
    result.headers["Cache-Control"] = "private, no-store"
    return result


def change(extension, body) -> dict:
    setup = _setup(extension)
    if not isinstance(body, dict) or set(body) != {"action"} or body["action"] not in {"link", "cancel"}:
        raise DomainError("validation_error", "Send action link or cancel")
    if body["action"] == "cancel":
        return setup.cancel()
    agent_id = setup.host.coordinator()
    if agent_id is None:
        raise DomainError("operation_unavailable", "Ordubashy isn't ready yet, so WhatsApp can't be linked now")
    return setup.begin(agent_id)


def owner_routes(extension):
    async def status(api, request, actor, request_id):
        import anyio
        _owner(api, request, actor)
        return _answer(api, await anyio.to_thread.run_sync(lambda: _setup(extension).status()), request_id)

    async def post(api, request, actor, request_id):
        import anyio
        from qorqut.transport.http import read_json
        _owner(api, request, actor)
        body = await read_json(request, limit=1024)
        return _answer(api, await anyio.to_thread.run_sync(lambda: change(extension, body)), request_id)

    async def qr(api, request, actor, request_id):
        import anyio
        from starlette.responses import Response
        _owner(api, request, actor)
        image = await anyio.to_thread.run_sync(lambda: _setup(extension).qr())
        if image is None:
            raise DomainError("not_found", "No WhatsApp code is shown right now")
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    from qorqut.extensions import OwnerRoute
    return (OwnerRoute("GET", (*BASE, "setup"), status), OwnerRoute("POST", (*BASE, "setup"), post),
            OwnerRoute("GET", (*BASE, "qr"), qr))
