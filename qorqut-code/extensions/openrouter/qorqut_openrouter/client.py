"""The HTTP requests this extension makes to OpenRouter, and nothing else.

Three requests exist. Trading a sign-in code for a key carries the code and its verifier and no key. Reading a key's
own limit and usage, and a transcription, carry the owner's key in ``Authorization``. A transcription also carries the
recorded audio and the model name. Redirects are never followed, so the key cannot be sent to another address. Every
answer is read up to a fixed size."""

from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from qorqut_operations.contracts import DomainError

BASE = "https://openrouter.ai"
AGENT = "Qorqbot"
ACCOUNT_TIMEOUT = 15
# OpenRouter's own providers stop a transcription after 60 seconds; this waits a little longer than that.
TRANSCRIPTION_TIMEOUT = 90
ANSWER_LIMIT = 2 * 1024 * 1024
# OpenRouter accepts an uploaded recording of up to 25 MB.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# What a browser records, by the media type it reports, and the file extension OpenRouter recognises it by.
AUDIO_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
               "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


_OPENER = build_opener(_NoRedirect)


@dataclass(frozen=True)
class Answer:
    status: int
    body: object           # parsed JSON, or text when the answer is not JSON

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _unreachable():
    return DomainError("operation_unavailable", "OpenRouter could not be reached. Try again in a moment.")


def send(method, path, *, body=None, data=None, content_type=None, key=None, timeout=ACCOUNT_TIMEOUT) -> Answer:
    """One request to OpenRouter: ``body`` is sent as JSON, or ``data`` as the given content type. A 4xx or 5xx is an
    Answer, not an exception; only a failed connection raises."""
    sent = {"Accept": "application/json", "User-Agent": AGENT, "X-Title": AGENT}
    if key is not None:
        sent["Authorization"] = "Bearer " + key
    if body is not None:
        data, content_type = json.dumps(body, separators=(",", ":")).encode(), "application/json"
    if data is not None:
        sent["Content-Type"] = content_type
    request = Request(BASE + path, data=data, headers=sent, method=method)
    try:
        try:
            response = _OPENER.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(ANSWER_LIMIT + 1)
            status = response.status
    except (OSError, ValueError):
        raise _unreachable() from None
    text = raw[:ANSWER_LIMIT].decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if len(raw) <= ANSWER_LIMIT else text
    except ValueError:
        parsed = text
    return Answer(status, parsed)


def multipart(fields, *, file_name, file_type, file_bytes, boundary=None):
    """``(content_type, body)`` of a form upload with text ``fields`` and one file part named ``file``."""
    boundary = boundary or "qorqbot-" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                 f"Content-Type: {file_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return "multipart/form-data; boundary=" + boundary, b"".join(parts)


def _message(body):
    """OpenRouter's own short error text, from ``{"error": {"message": ...}}`` or ``{"error": "..."}``."""
    error = body.get("error") if isinstance(body, dict) else None
    text = error.get("message") if isinstance(error, dict) else error
    return " ".join(text.split())[:300] if isinstance(text, str) and text.strip() else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class OpenRouterClient:
    """OpenRouter's sign-in, key and transcription requests. ``send`` is replaceable in tests."""

    def __init__(self, send=send):
        self.send = send

    def exchange(self, code, verifier) -> str:
        """Trade the one-time code OpenRouter gave the owner's browser for the owner's key."""
        answer = self.send("POST", "/api/v1/auth/keys",
                           body={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"})
        key = answer.body.get("key") if answer.ok and isinstance(answer.body, dict) else None
        if not isinstance(key, str) or not key:
            raise DomainError("openrouter_key_refused", "OpenRouter did not accept the sign-in. Press Connect and sign in again.")
        return key

    def key_info(self, key) -> dict:
        """What OpenRouter says about this key: its spending limit and use, in US dollars. A refused key raises
        ``openrouter_key_refused``. ``limit_usd`` and ``remaining_usd`` are None for a key without a limit."""
        answer = self.send("GET", "/api/v1/key", key=key)
        if answer.status in (401, 403):
            raise DomainError("openrouter_key_refused", "OpenRouter did not accept this key")
        data = answer.body.get("data") if answer.ok and isinstance(answer.body, dict) else None
        if not isinstance(data, dict):
            raise DomainError("operation_unavailable", "OpenRouter did not answer about the key. Try again in a moment.")
        return {"limit_usd": _number(data.get("limit")), "remaining_usd": _number(data.get("limit_remaining")),
                "used_usd": _number(data.get("usage")), "free_tier": data.get("is_free_tier") is True}

    def transcribe(self, key, audio, *, media_type, model, language=None, provider=None) -> dict:
        """``{"text", "cost_usd", "seconds"}`` for one recording. ``provider`` is the provider OpenRouter asks first;
        it may still use another when that one cannot serve the request."""
        extension = AUDIO_TYPES[media_type]
        fields = {"model": model, **({"language": language} if language else {}),
                  **({"provider": json.dumps({"order": [provider]}, separators=(",", ":"))} if provider else {})}
        content_type, data = multipart(fields, file_name="recording." + extension, file_type=media_type, file_bytes=audio)
        answer = self.send("POST", "/api/v1/audio/transcriptions", data=data, content_type=content_type, key=key,
                           timeout=TRANSCRIPTION_TIMEOUT)
        if answer.status in (401, 403):
            raise DomainError("operation_unavailable", "OpenRouter did not accept the saved key. Connect OpenRouter again "
                                                       "in Settings, Connections.")
        if answer.status == 402:
            raise DomainError("operation_unavailable", "Your OpenRouter balance is too low for voice input. Add credits "
                                                       "at openrouter.ai.")
        text = answer.body.get("text") if answer.ok and isinstance(answer.body, dict) else None
        if not isinstance(text, str):
            reason = _message(answer.body)
            raise DomainError("operation_unavailable", "OpenRouter could not turn the recording into text"
                              + (f": {reason}" if reason else ". Try again in a moment."))
        usage = answer.body.get("usage") if isinstance(answer.body.get("usage"), dict) else {}
        return {"text": text.strip(), "cost_usd": _number(usage.get("cost")), "seconds": _number(usage.get("seconds"))}
