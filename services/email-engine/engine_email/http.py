"""Trusted-service API. Read credentials cannot submit or resume a send."""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager

import anyio
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .fastmail import ProviderError
from .account import AccountAction, AccountMailService
from .business import ATTACHMENT_PAGE_BYTES, BusinessMailService, parse_owned_ids
from .models import MAX_HTTP_BYTES, HandoffRequest, HistoryEvent, IntentSubmission, Outgoing, StopRequest
from .service import IntentConflict
from .store import Store
from .mailbox_settings import MailboxUpdate, configure, revisions as mailbox_revisions


def create_app(service, *, background=True):
    settings = service.settings
    account = AccountMailService(settings, service.store, service.provider)
    business = BusinessMailService(settings, service.provider)

    async def poll():
        while True:
            for operation in (service.sync, service.collect_history, service.collect_outlook,
                    lambda: service.reconcile_pending(scheduled=True)):
                try:
                    await anyio.to_thread.run_sync(operation)
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
            service.close()

    def authenticate(request, *, send=False):
        supplied = request.headers.get("Authorization", "")
        expected = settings.send_token if send else settings.read_token
        if "token" in request.query_params or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)

    def authenticate_account(request):
        token = settings.admin_token
        if (not token or "token" in request.query_params or not hmac.compare_digest(
                request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)

    def integration_client_for(request):
        """The matching configured integration client, or None. Never compares against
        the read/send/admin token pool, so a legacy token can never authenticate here."""
        if "token" in request.query_params:
            return None
        supplied = request.headers.get("Authorization", "")
        if not supplied.startswith("Bearer "):
            return None
        return settings.integration_client(supplied[len("Bearer "):])

    def authenticate_integration(request):
        """``/integrations/*`` routes: only a configured integration client, never a
        legacy read/send/admin token."""
        client = integration_client_for(request)
        if client is None:
            return None, JSONResponse({"error": "unauthenticated"}, status_code=401)
        return client, None

    async def fastmail_account(request):
        denied = authenticate_account(request)
        if denied is not None:
            return denied
        path = request.path_params.get("path", "").strip("/")
        try:
            if request.method == "GET" and path in {"", "metadata"}:
                return JSONResponse(await anyio.to_thread.run_sync(account.metadata))
            if request.method == "GET" and path == "folders":
                return JSONResponse(await anyio.to_thread.run_sync(account.folders))
            if request.method == "GET" and path == "identities":
                return JSONResponse({"identities": await anyio.to_thread.run_sync(service.provider.account_identities)})
            if request.method == "GET" and path == "messages":
                params = dict(request.query_params)
                return JSONResponse(await anyio.to_thread.run_sync(lambda: account.search(params)))
            if request.method == "GET" and path.startswith("messages/"):
                bits = path.split("/")
                identity = bits[1]
                if len(bits) == 3 and bits[2] == "attachments":
                    raw = await anyio.to_thread.run_sync(lambda: service.provider.account_raw(identity, metadata=True))
                    return JSONResponse({"attachments": raw.get("attachments", [])})
                if len(bits) == 2:
                    return JSONResponse(await anyio.to_thread.run_sync(lambda: account.message(identity)))
            if request.method == "GET" and path.startswith("threads/"):
                identity = path.split("/", 1)[1]
                limit = int(request.query_params.get("limit", "50"))
                position = int(request.query_params.get("position", "0"))
                if not 1 <= limit <= 100 or position < 0:
                    raise ValueError("Invalid page")
                return JSONResponse(await anyio.to_thread.run_sync(
                    lambda: service.provider.account_thread(identity, limit=limit, position=position)))
            if request.method == "GET" and path.startswith("actions/"):
                request_id = path.split("/", 1)[1]
                receipt = await anyio.to_thread.run_sync(lambda: account.receipt(request_id))
                if receipt is None:
                    return JSONResponse({"error": "operation_not_found"}, status_code=404)
                return JSONResponse(receipt)
            if request.method == "GET" and path.startswith("attachments/"):
                bits = path.split("/")
                identity = request.query_params.get("message_id", "")
                if len(bits) == 2 and identity:
                    offset = int(request.query_params.get("offset", "0"))
                    limit = int(request.query_params.get("limit", str(5 * 1024 * 1024)))
                    if offset < 0 or not 1 <= limit <= 5 * 1024 * 1024:
                        raise ValueError("Invalid attachment range")
                    content = await anyio.to_thread.run_sync(lambda: account.attachment(identity, bits[1]))
                    if offset > len(content):
                        raise ValueError("Invalid attachment offset")
                    selected = content[offset:offset + limit]
                    return Response(selected, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                            "X-Attachment-Total": str(len(content)), "X-Attachment-Offset": str(offset)})
            if request.method == "POST" and path == "actions":
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > MAX_HTTP_BYTES:
                        return JSONResponse({"error": "request_too_large"}, status_code=413)
                parsed = json.loads(bytes(content))
                if not isinstance(parsed, dict):
                    raise ValueError("Account action must be a JSON object")
                action = AccountAction.model_validate(parsed)
                request_digest = hashlib.sha256(json.dumps(parsed, sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                receipt = await anyio.to_thread.run_sync(lambda: account.apply(action, request_digest=request_digest))
                return JSONResponse(receipt, status_code=200 if receipt["state"] in {"completed", "failed", "uncertain"} else 202)
            return JSONResponse({"error": "not_found"}, status_code=404)
        except ValidationError:
            return JSONResponse({"error": "invalid_account_action"}, status_code=400)
        except ValueError:
            return JSONResponse({"error": "invalid_account_request"}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status or 502)

    async def configure_mailbox(request):
        token = settings.admin_token
        if (not token or "token" in request.query_params or not hmac.compare_digest(
                request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            change = MailboxUpdate.model_validate_json(bytes(content))
            result = await anyio.to_thread.run_sync(lambda: configure(service, change))
            return JSONResponse(result)
        except ValidationError:
            return JSONResponse({"error": "invalid_mailbox"}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": "mailbox_configuration_conflict", "message": str(exc)}, status_code=409)
        except ProviderError as exc:
            invalid_setup = exc.code in {"business_folder_missing_or_not_dedicated",
                "private_history_folder_missing_or_not_dedicated", "sender_outside_business",
                "fastmail_sender_identity_missing_or_ambiguous", "fastmail_identity_remove_auto_bcc_and_reply_to"}
            return JSONResponse({"error": exc.code}, status_code=400 if invalid_setup else 502)

    async def health(request):
        return JSONResponse({"live": True, "service": "engine-email", "provider": "fastmail"})

    async def read(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        kind = request.path_params.get("kind", "mailboxes")
        try:
            if kind == "mailboxes":
                return JSONResponse({"mailboxes": [box.model_dump() for box in service.settings.mailboxes],
                    "mailbox_revisions": await anyio.to_thread.run_sync(lambda: mailbox_revisions(service.store)),
                    "shared_senders": service.shared_sender_metadata(),
                    "sync": await anyio.to_thread.run_sync(service.store.sync_status)})
            mailbox = service.settings.mailbox(request.path_params["mailbox"])
            if kind == "send":
                row = await anyio.to_thread.run_sync(lambda: service.store.send(request.query_params.get("request_id", "")))
                if row is None or row["mailbox"] != mailbox.ref or row["business_id"] != mailbox.business_id:
                    return JSONResponse({"error": "send_not_found"}, status_code=404)
                return JSONResponse(Store.receipt(row))
            identity = request.query_params.get("message_id", "")
            owned = parse_owned_ids(request.query_params.get("owned_ids", ""))
            ranged = "offset" in request.query_params or "limit" in request.query_params
            if kind == "attachment" and (owned or ranged) or kind == "attachments" and owned:
                # Provider-side scoped reads mirroring the /fastmail ranged pages;
                # their 403/404 outcomes are results, not upstream failures.
                try:
                    if kind == "attachments":
                        listed = await anyio.to_thread.run_sync(
                            lambda: business.attachments(mailbox, identity, owned))
                        return JSONResponse({"attachments": listed})
                    attachment_id = request.query_params.get("attachment_id", "")
                    offset = int(request.query_params.get("offset", "0"))
                    limit = int(request.query_params.get("limit", str(ATTACHMENT_PAGE_BYTES)))
                    if offset < 0 or not 1 <= limit <= ATTACHMENT_PAGE_BYTES:
                        raise ValueError("Invalid attachment range")
                    content = await anyio.to_thread.run_sync(
                        lambda: business.attachment(mailbox, identity, attachment_id, owned))
                    if offset > len(content):
                        raise ValueError("Invalid attachment offset")
                    return Response(content[offset:offset + limit], media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                            "X-Attachment-Total": str(len(content)), "X-Attachment-Offset": str(offset)})
                except ProviderError as exc:
                    return JSONResponse({"error": exc.code}, status_code=exc.status or 502)
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

    async def mailbox_messages(request):
        """Shared with the scoped integration reply view (docs/INTEGRATION-CONTRACT.md);
        an integration secret filters out sent copies, a legacy token sees everything."""
        client = integration_client_for(request)
        if client is None:
            denied = authenticate(request)
            if denied is not None:
                return denied
        try:
            mailbox_ref = request.path_params["mailbox"]
            if client is not None and mailbox_ref not in client.allowed_mailboxes:
                return JSONResponse({"error": "mailbox_not_allowed_for_client"}, status_code=403)
            mailbox = service.settings.mailbox(mailbox_ref)
            after, limit = int(request.query_params.get("after", "0")), int(request.query_params.get("limit", "100"))
            if after < 0 or not 1 <= limit <= 500:
                raise ValueError("Invalid page")
            result = await anyio.to_thread.run_sync(lambda: service.store.messages(mailbox.ref, after=after,
                limit=limit, exclude_sent_copies=client is not None))
            return JSONResponse(result)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    async def integration_intents_submit(request):
        client, denied = authenticate_integration(request)
        if denied is not None:
            return denied
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_HTTP_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            submission = IntentSubmission.model_validate_json(bytes(content))
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_intent",
                "fields": [".".join(map(str, item["loc"])) for item in exc.errors()]}, status_code=400)
        try:
            receipt = await anyio.to_thread.run_sync(lambda: service.submit_intent(client, submission))
            return JSONResponse(receipt, status_code=200 if receipt["state"] in
                {"confirmed", "failed", "cancelled", "uncertain"} else 202)
        except IntentConflict:
            return JSONResponse({"error": "intent_conflict"}, status_code=409)
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_message",
                "fields": [".".join(map(str, item["loc"])) for item in exc.errors()]}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_intent_request", "message": str(exc)}, status_code=400)

    async def integration_intent_get(request):
        client, denied = authenticate_integration(request)
        if denied is not None:
            return denied
        receipt = await anyio.to_thread.run_sync(lambda: service.intent(client, request.path_params["intent_id"]))
        if receipt is None:
            return JSONResponse({"error": "intent_not_found"}, status_code=404)
        return JSONResponse(receipt)

    async def integration_intent_cancel(request):
        client, denied = authenticate_integration(request)
        if denied is not None:
            return denied
        receipt = await anyio.to_thread.run_sync(lambda: service.cancel_intent(client, request.path_params["intent_id"]))
        if receipt is None:
            return JSONResponse({"error": "intent_not_found"}, status_code=404)
        return JSONResponse(receipt)

    async def integration_stops_create(request):
        client, denied = authenticate_integration(request)
        if denied is not None:
            return denied
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            stop = StopRequest.model_validate_json(bytes(content))
        except ValidationError:
            return JSONResponse({"error": "invalid_stop"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(lambda: service.record_stop(client, stop))
            return JSONResponse(result)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_stop_request", "message": str(exc)}, status_code=400)

    async def integration_handoff(request):
        client, denied = authenticate_integration(request)
        if denied is not None:
            return denied
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            payload = HandoffRequest.model_validate_json(bytes(content))
        except ValidationError:
            return JSONResponse({"error": "invalid_handoff"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(lambda: service.handoff(
                client, request.path_params["thread_or_message_id"], payload))
            return JSONResponse(result)
        except IntentConflict:
            return JSONResponse({"error": "handoff_conflict"}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_handoff_request", "message": str(exc)}, status_code=400)

    async def business_search(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        try:
            mailbox = service.settings.mailbox(request.path_params["mailbox"])
            params = dict(request.query_params)
            return JSONResponse(await anyio.to_thread.run_sync(lambda: business.search(mailbox, params)))
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status or 502)

    async def business_drafts(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        try:
            mailbox = service.settings.mailbox(request.path_params["mailbox"])
            params = dict(request.query_params)
            return JSONResponse(await anyio.to_thread.run_sync(lambda: business.drafts(mailbox, params)))
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status or 502)

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
            return JSONResponse(receipt, status_code=200 if receipt["state"] in {"sent", "failed", "canceled"} else 202)
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

    async def cancel(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 1024:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            import json
            data = json.loads(content)
            if not isinstance(data, dict) or set(data) != {"request_id"} or not isinstance(data["request_id"], str):
                raise ValueError("Invalid cancellation")
            mailbox = service.settings.mailbox(request.path_params["mailbox"])
            result = await anyio.to_thread.run_sync(lambda: service.cancel(mailbox, data["request_id"]))
            return JSONResponse(result)
        except ValueError:
            return JSONResponse({"error": "invalid_cancellation"}, status_code=400)

    async def append_history(request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        if service.history is None:
            return JSONResponse({"error": "history_unavailable"}, status_code=503)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_HTTP_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            event = HistoryEvent.model_validate_json(bytes(content))
        except ValueError:
            return JSONResponse({"error": "invalid_history_event"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(lambda: service.history.append(event.event_id, event.kind, event.content))
            return JSONResponse(result)
        except ValueError:
            return JSONResponse({"error": "history_event_conflict"}, status_code=409)

    routes = [Route("/health/live", health), Route("/mailboxes", read), Route("/sync", sync, methods=["POST"]),
        Route("/sends", submit, methods=["POST"]), Route("/sends/{request_id}", receipt)]
    routes.append(Route("/mailboxes/{mailbox}/send/cancel", cancel, methods=["POST"]))
    routes.append(Route("/history/events", append_history, methods=["POST"]))
    # Explicit paths keep new provider features from becoming accidentally exposed.
    for kind in ("message", "attachments", "attachment", "send"):
        async def endpoint(request, selected=kind):
            request.path_params["kind"] = selected
            return await read(request)
        routes.append(Route("/mailboxes/{mailbox}/" + kind, endpoint))
    routes.append(Route("/mailboxes/{mailbox}/messages", mailbox_messages))
    routes.append(Route("/mailboxes/{mailbox}/search", business_search))
    routes.append(Route("/mailboxes/{mailbox}/drafts", business_drafts))
    routes.insert(0, Route("/mailboxes/configure", configure_mailbox, methods=["POST"]))
    routes.append(Route("/fastmail", fastmail_account, methods=["GET"]))
    routes.append(Route("/fastmail/{path:path}", fastmail_account, methods=["GET", "POST"]))
    routes.append(Route("/integrations/intents", integration_intents_submit, methods=["POST"]))
    routes.append(Route("/integrations/intents/{intent_id}", integration_intent_get))
    routes.append(Route("/integrations/intents/{intent_id}/cancel", integration_intent_cancel, methods=["POST"]))
    routes.append(Route("/integrations/stops", integration_stops_create, methods=["POST"]))
    routes.append(Route("/integrations/threads/{thread_or_message_id}/handoff", integration_handoff, methods=["POST"]))
    return Starlette(routes=routes, lifespan=lifespan)
