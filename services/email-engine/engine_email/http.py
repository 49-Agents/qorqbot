"""Trusted-service API. Read credentials cannot submit or resume a send."""

import asyncio
import hmac
from contextlib import asynccontextmanager

import anyio
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .fastmail import ProviderError
from .models import MAX_HTTP_BYTES, Outgoing
from .store import Store


def create_app(service, *, background=True):
    settings = service.settings

    async def poll():
        while True:
            try:
                await anyio.to_thread.run_sync(service.sync)
                await anyio.to_thread.run_sync(service.reconcile_pending)
            except Exception:
                import logging
                logging.getLogger(__name__).error("Email background pass failed; mailbox state is not current")
            await asyncio.sleep(settings.sync_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poll()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            service.provider.close()

    def authenticate(request, *, send=False):
        supplied = request.headers.get("Authorization", "")
        expected = settings.send_token if send else settings.read_token
        if "token" in request.query_params or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)

    async def health(request):
        return JSONResponse({"live": True, "service": "engine-email", "provider": "fastmail"})

    async def read(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        kind = request.path_params.get("kind", "mailboxes")
        try:
            if kind == "mailboxes":
                return JSONResponse({"mailboxes": [box.model_dump() for box in settings.mailboxes],
                    "sync": await anyio.to_thread.run_sync(service.store.sync_status)})
            mailbox = settings.mailbox(request.path_params["mailbox"])
            if kind == "send":
                row = await anyio.to_thread.run_sync(lambda: service.store.send(request.query_params.get("request_id", "")))
                if row is None or row["mailbox"] != mailbox.ref or row["business_id"] != mailbox.business_id:
                    return JSONResponse({"error": "send_not_found"}, status_code=404)
                return JSONResponse(Store.receipt(row))
            if kind == "messages":
                after, limit = int(request.query_params.get("after", "0")), int(request.query_params.get("limit", "100"))
                if after < 0 or not 1 <= limit <= 500:
                    raise ValueError("Invalid page")
                result = await anyio.to_thread.run_sync(lambda: service.store.messages(mailbox.ref, after=after, limit=limit))
                return JSONResponse(result)
            identity = request.query_params.get("message_id", "")
            message = await anyio.to_thread.run_sync(lambda: service.store.message(mailbox.ref, identity))
            if message is None:
                return JSONResponse({"error": "message_not_found"}, status_code=404)
            if kind == "message":
                return JSONResponse({"message": message})
            attachments = await anyio.to_thread.run_sync(lambda: service.provider.attachments(mailbox, identity))
            if kind == "attachments":
                return JSONResponse({"attachments": attachments})
            attachment_id = request.query_params.get("attachment_id", "")
            selected = next((item for item in attachments if item["id"] == attachment_id), None)
            if selected is None:
                return JSONResponse({"error": "attachment_not_found"}, status_code=404)
            content = await anyio.to_thread.run_sync(lambda: service.provider.attachment(mailbox, identity, attachment_id))
            return Response(content, media_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store"})
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": exc.code, "provider_status": exc.status}, status_code=502)

    async def sync(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        result = await anyio.to_thread.run_sync(service.sync)
        return JSONResponse(result)

    async def submit(request):
        denied = authenticate(request, send=True)
        if denied is not None:
            return denied
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_HTTP_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            message = Outgoing.model_validate_json(bytes(content))
            receipt = await anyio.to_thread.run_sync(lambda: service.submit(message))
            return JSONResponse(receipt, status_code=200 if receipt["state"] in {"sent", "failed"} else 202)
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_message", "fields": [".".join(map(str, item["loc"])) for item in exc.errors()]}, status_code=400)
        except ValueError:
            return JSONResponse({"error": "invalid_or_conflicting_send"}, status_code=409)

    async def receipt(request):
        denied = authenticate(request, send=True)
        if denied is not None:
            return denied
        row = await anyio.to_thread.run_sync(lambda: service.store.send(request.path_params["request_id"]))
        if row is None:
            return JSONResponse({"error": "send_not_found"}, status_code=404)
        return JSONResponse(Store.receipt(row))

    routes = [Route("/health/live", health), Route("/mailboxes", read), Route("/sync", sync, methods=["POST"]),
        Route("/sends", submit, methods=["POST"]), Route("/sends/{request_id}", receipt)]
    # Explicit paths keep new provider features from becoming accidentally exposed.
    for kind in ("messages", "message", "attachments", "attachment", "send"):
        async def endpoint(request, selected=kind):
            request.path_params["kind"] = selected
            return await read(request)
        routes.append(Route("/mailboxes/{mailbox}/" + kind, endpoint))
    return Starlette(routes=routes, lifespan=lifespan)
