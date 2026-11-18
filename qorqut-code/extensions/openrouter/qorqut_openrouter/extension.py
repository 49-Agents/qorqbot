"""The OpenRouter extension: ``qorqut_openrouter.extension:create``.

OpenRouter (openrouter.ai) is a hosted gateway to models from many providers behind one key the owner pays for. This
extension keeps the owner's own key and uses it for one thing: voice input, where the owner's recorded speech is sent
to OpenRouter and comes back as text for the Chat composer. It adds one Global read for the coordinator,
``openrouter_status``, and three owner routes for the dashboard.

The owner gets the key by signing in with OpenRouter (its own sign-in page, with a one-time code traded for a key by
this service) or by pasting a key. Each owner uses their own OpenRouter account; nothing is resold or shared.

The factory validates the configuration and returns the declaration; it reads no key and contacts nothing.

Selection in the trusted installation configuration::

    "extensions": [{"factory": "qorqut_openrouter.extension:create", "config": {}}]
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.contracts import DomainError

from qorqut.extensions import Extension, OwnerRoute

from . import config as settings
from . import operations
from .client import AUDIO_TYPES, BASE, OpenRouterClient

ID = "openrouter"
STATUSES = {"openrouter_key_refused": 422}
KEY_LABEL = "Qorqbot"
# A started sign-in is finished within this long or started again; OpenRouter's own code lasts ten minutes.
SIGN_IN_SECONDS = 15 * 60
MAX_RECORDING_BYTES = 20 * 1024 * 1024
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LANGUAGE_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")


def challenge(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def can_return_to(origin: str) -> bool:
    """Whether OpenRouter can send the owner's browser back to this dashboard address: one opened on this computer, or
    one served over https."""
    parts = urlsplit(origin)
    return parts.scheme == "https" or (parts.scheme == "http" and (parts.hostname or "") in LOCAL_HOSTS)


class OpenRouter(Extension):
    id = ID

    def __init__(self, config, *, client=None, clock=None):
        self.config = config
        self.client = client or OpenRouterClient()
        self.clock = clock or time.monotonic
        self.instance_root = self.operations = None
        self._sign_in, self._lock = None, threading.Lock()

    # -- declarations ----------------------------------------------------------------------------------------------
    def catalog(self):
        return (CatalogContribution(ID, entries=operations.catalog_entries()),)

    def policy_rules(self):
        return {name: operations.policy for name in operations.NAMES}

    def owner_routes(self):
        base = ("root", "extensions", ID)
        return (OwnerRoute("POST", (*base, "key"), json_route(self, self.change_key, limit=8192)),
                OwnerRoute("POST", (*base, "connect"), connect_route(self)),
                OwnerRoute("POST", (*base, "transcribe"), transcribe_route(self)))

    def error_statuses(self):
        return dict(STATUSES)

    # -- runtime wiring --------------------------------------------------------------------------------------------
    def install(self, runtime):
        self.instance_root = runtime.paths.instance_root
        self.operations = operations.OpenRouterOperations(runtime, self).register(runtime.registry)

    # -- the owner's key ---------------------------------------------------------------------------------------------
    def status(self) -> str:
        """``connected`` once a key is saved, otherwise ``not_set_up``. Reads no key and contacts nothing."""
        return "connected" if self.instance_root is not None and settings.has_key(self.instance_root) else "not_set_up"

    def change_key(self, change) -> dict:
        """``{"key": "<pasted key>"}`` checks the key with OpenRouter and saves it; ``{"key": null}`` removes the saved
        key."""
        if not isinstance(change, dict) or set(change) != {"key"}:
            raise DomainError("validation_error", "Send the OpenRouter key, or null to remove it")
        if change["key"] is None:
            settings.remove_key(self.instance_root)
        else:
            key = settings.check_key(change["key"])
            self.client.key_info(key)            # OpenRouter refuses a wrong key here, before anything is saved
            settings.save_key(self.instance_root, key)
        return {"status": self.status()}

    # -- signing in with OpenRouter ------------------------------------------------------------------------------------
    def start_sign_in(self, origin: str) -> dict:
        """The OpenRouter address the owner's browser opens to sign in and approve a key for this installation.

        The verifier stays in this process; only its challenge travels. OpenRouter sends the browser back to the
        dashboard at ``origin`` with a one-time code, which ``finish_sign_in`` trades for the key."""
        if not can_return_to(origin):
            raise DomainError("operation_unavailable", "Signing in with OpenRouter works when this dashboard is opened on "
                              "this computer or over https. Paste an OpenRouter key here instead.")
        verifier = secrets.token_urlsafe(48)
        with self._lock:
            self._sign_in = {"verifier": verifier, "origin": origin, "started": self.clock()}
        query = urlencode({"callback_url": origin + "/", "code_challenge": challenge(verifier),
                           "code_challenge_method": "S256", "key_label": KEY_LABEL})
        return {"url": f"{BASE}/auth?{query}"}

    def finish_sign_in(self, origin: str, code) -> dict:
        """Trade the code for the owner's key, check it and save it. A code is used once, with the sign-in that asked
        for it; a code from any other sign-in fails at OpenRouter because its challenge does not match."""
        if not isinstance(code, str) or not 8 <= len(code) <= 2048 or any(ch.isspace() for ch in code):
            raise DomainError("validation_error", "That is not an OpenRouter sign-in code")
        with self._lock:
            started, self._sign_in = self._sign_in, None
        if started is None or started["origin"] != origin or self.clock() - started["started"] > SIGN_IN_SECONDS:
            raise DomainError("openrouter_key_refused", "This OpenRouter sign-in has expired. Press Connect and sign in again.")
        key = settings.check_key(self.client.exchange(code, started["verifier"]))
        self.client.key_info(key)
        settings.save_key(self.instance_root, key)
        return {"status": self.status()}

    def connect(self, origin: str, change) -> dict:
        if isinstance(change, dict) and change == {"action": "start"}:
            return self.start_sign_in(origin)
        if isinstance(change, dict) and set(change) == {"action", "code"} and change["action"] == "finish":
            return self.finish_sign_in(origin, change["code"])
        raise DomainError("validation_error", "Send action start, or action finish with the code")

    # -- voice input -------------------------------------------------------------------------------------------------
    def transcribe(self, audio: bytes, media_type: str, language=None) -> dict:
        """The text of one recording, made by OpenRouter with the owner's key. Nothing is kept."""
        if media_type not in AUDIO_TYPES:
            raise DomainError("validation_error", "This kind of recording is not supported")
        if not audio:
            raise DomainError("validation_error", "The recording is empty")
        if language is not None and not (2 <= len(language) <= 3 and set(language) <= LANGUAGE_LETTERS):
            raise DomainError("validation_error", "language is a two or three letter language code")
        key = settings.read_key(self.instance_root)
        if key is None:
            raise DomainError("operation_unavailable", "Voice input needs OpenRouter. Connect it in Settings, Connections.")
        answer = self.client.transcribe(key, audio, media_type=media_type, model=self.config.transcription_model,
                                        language=language, provider=self.config.transcription_provider)
        return {"text": answer["text"], "cost_usd": answer["cost_usd"]}


def _owner(api, request, actor):
    """Refuse anything but the installation owner's own dashboard, after the host's Origin and CSRF checks."""
    if request.headers.get("authorization") is not None:
        raise DomainError("forbidden", "OpenRouter is set up from the owner's dashboard")
    context, _ = api.context(actor)
    with api.runtime.database.read() as uow:
        owner = uow.repository("installation").get("service")
    if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
        raise DomainError("forbidden", "Only the installation owner can use OpenRouter here")


def _answer(api, data, request_id):
    from qorqut_operations.contracts import OperationResult
    from qorqut.transport.http import response
    result = response(OperationResult.ok(data, request_id=request_id), statuses=api.statuses)
    result.headers["Cache-Control"] = "private, no-store"
    return result


def json_route(extension, change, *, limit):
    """An owner route that reads one small JSON object and answers with ``change(body)``. The key is never returned."""
    async def handle(api, request, actor, request_id):
        import anyio
        from qorqut.transport.http import read_json
        _owner(api, request, actor)
        body = await read_json(request, limit=limit)
        return _answer(api, await anyio.to_thread.run_sync(lambda: change(body)), request_id)
    return handle


def connect_route(extension):
    """Start or finish the owner's sign-in with OpenRouter. The dashboard's own checked origin is where OpenRouter sends
    the browser back; a request cannot name another address."""
    async def handle(api, request, actor, request_id):
        import anyio
        from qorqut.transport.http import read_json
        _owner(api, request, actor)
        origin = api.origins.request_origin(request)
        body = await read_json(request, limit=8192)
        return _answer(api, await anyio.to_thread.run_sync(lambda: extension.connect(origin, body)), request_id)
    return handle


def transcribe_route(extension):
    """Turn one recording from the owner's dashboard into text. The request body is the recording itself, its
    ``Content-Type`` the kind of audio; ``?language=`` optionally names the spoken language."""
    async def handle(api, request, actor, request_id):
        import anyio
        from qorqut.transport.http import check_body_length
        _owner(api, request, actor)
        check_body_length(request, MAX_RECORDING_BYTES)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        audio = bytearray()
        async for part in request.stream():
            audio.extend(part)
            if len(audio) > MAX_RECORDING_BYTES:
                raise DomainError("payload_too_large", "The recording is too long. Record a shorter message.")
        language = request.query_params.get("language") or None
        data = await anyio.to_thread.run_sync(lambda: extension.transcribe(bytes(audio), media_type, language))
        return _answer(api, data, request_id)
    return handle


def create(config=None, **injected):
    """Validate the configuration and return the declaration. ``injected`` is the Python-only test transport
    (``client``, ``clock``); a startup selection passes none."""
    return OpenRouter(settings.from_config(config), **injected)
