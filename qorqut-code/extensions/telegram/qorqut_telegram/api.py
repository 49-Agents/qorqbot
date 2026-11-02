"""Bot API port and its bounded HTTPS adapter.

Every call has a connect timeout and a read timeout, and failures are classified by what they prove about the
effect: ``unreachable`` failed before any request bytes were written (safe to retry), ``refused`` and
``rate_limited`` are the provider's own answer, and ``timeout``/``uncertain`` happened after the request may have
been applied (never auto-resent). The adapter never calls ``setWebhook`` or ``deleteWebhook``.

Stream uploads (``send_document_stream``/``send_photo_stream``) are the standard ``sendDocument``/``sendPhoto`` multipart
requests with a fixed ``Content-Length`` written in bounded chunks from a seekable source, so a file is never held in memory.
They run under one absolute total deadline (name resolution, connect, TLS, every write and the whole response, however slowly
the peer trickles), which is shorter than the bridge's stop allowance. A failure before the first request byte is written is
``unreachable``; once any byte may have reached the provider the outcome is ``timeout``/``uncertain``, never retried.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from .config import Secret

LOGGER = logging.getLogger(__name__)
ALLOWED_UPDATES = ("message", "message_reaction")
MAX_LONG_POLL_SECONDS = 25
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Stream uploads. The total deadline stays below the bridge's 35 second stop allowance and every existing timeout maximum.
STREAM_DEADLINE_SECONDS = 30.0
STREAM_CONNECT_SECONDS = 10.0
STREAM_CHUNK_BYTES = 64 * 1024
STREAM_LIMITS = {"document": 50_000_000, "photo": 10_000_000}
PHOTO_CONTENT_TYPES = ("image/jpeg", "image/png")
MAX_CAPTION_CHARS = 1024
MAX_CAPTION_UTF16_UNITS = 1024
MAX_FILENAME_BYTES = 255
MAX_RESOLVE_ADDRESSES = 4
RESOLVER_LIMIT = 2
_RESOLVER_PERMITS = threading.BoundedSemaphore(RESOLVER_LIMIT)
CONTENT_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}")
TOKEN_PATH = re.compile(r"[A-Za-z0-9:_-]{1,200}")


class TelegramApiError(Exception):
    """A classified Bot API failure. ``kind`` states what is known about whether the effect happened."""

    KINDS = ("unreachable", "refused", "rate_limited", "timeout", "uncertain")

    def __init__(self, kind, description, *, error_code=None, retry_after=None, status=None, local_code=None):
        super().__init__(description)
        if kind not in self.KINDS:
            raise ValueError("Unknown Telegram API failure kind")
        self.kind, self.description = kind, description
        self.error_code, self.retry_after, self.status = error_code, retry_after, status
        # Set only for a refusal made locally before any request existed (a stable code the outbox records as is).
        self.local_code = local_code

    @property
    def uncertain(self):
        return self.kind in ("timeout", "uncertain")

    @property
    def retryable_before_send(self):
        return self.kind in ("unreachable", "rate_limited")

    @property
    def message_missing(self):
        text = self.description.lower()
        return self.kind == "refused" and ("message to delete not found" in text or "message not found" in text
                                           or "message identifier is not specified" in text)

    @property
    def too_old(self):
        return self.kind == "refused" and "can't be deleted" in self.description.lower()


class BotApi:
    """The port used by the bridge. Tests inject a fake that implements exactly these methods."""

    def get_me(self): raise NotImplementedError
    def get_webhook_info(self): raise NotImplementedError
    def get_chat(self, chat_id): raise NotImplementedError
    def get_chat_member(self, chat_id, user_id): raise NotImplementedError
    def get_updates(self, offset, timeout): raise NotImplementedError
    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None): raise NotImplementedError
    def send_document(self, chat_id, filename, data, caption=None, message_thread_id=None, reply_to_message_id=None):
        raise NotImplementedError

    def send_document_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                             reply_to_message_id=None, content_type=None):
        raise NotImplementedError

    def send_photo_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                          reply_to_message_id=None, content_type=None):
        raise NotImplementedError

    def edit_message_text(self, chat_id, message_id, text): raise NotImplementedError
    def delete_message(self, chat_id, message_id): raise NotImplementedError


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    for name, (filename, data) in files.items():
        safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _interpret(status, payload):
    """The provider's answer to one request, as its ``result`` or a classified failure (shared by every call path).

    Total over untrusted bytes: only a well-formed answer that is consistent with its HTTP status is trusted. A server error
    (HTTP 5xx) is uncertain whatever its body says. A success needs a real boolean ``ok`` and a 2xx status; a refusal needs a
    4xx (or 2xx) status and a 4xx code; a rate limit needs HTTP 429 and code 429 together, because it is the one answer that is
    retried automatically. Anything malformed or contradictory is ``uncertain``: it is never a definite refusal, never a retry.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        data = None
    if status >= 500:
        description = data.get("description") if isinstance(data, dict) else None
        if isinstance(description, str) and description:
            raise TelegramApiError("uncertain", description[:300], error_code=status, status=status)
        raise TelegramApiError("uncertain", f"Unrecognized provider response (HTTP {status})", status=status)
    if not isinstance(data, dict) or type(data.get("ok")) is not bool:
        # A gateway error or a truncated body says nothing certain about the effect.
        raise TelegramApiError("uncertain", f"Unrecognized provider response (HTTP {status})", status=status)
    if data["ok"]:
        if not 200 <= status < 300:
            raise TelegramApiError("uncertain", f"Contradictory provider response (HTTP {status})", status=status)
        return data.get("result")
    description = data.get("description", "Provider refused the request")
    code = data.get("error_code", status)
    parameters = data.get("parameters")
    if (not isinstance(description, str) or type(code) is not int
            or (parameters is not None and not isinstance(parameters, dict))):
        raise TelegramApiError("uncertain", f"Malformed provider response (HTTP {status})", status=status)
    description = description[:300]
    if status == 429 and code == 429:
        retry_after = (parameters or {}).get("retry_after")
        seconds = retry_after if type(retry_after) in (int, float) and 0 <= retry_after <= 86400 else 30
        raise TelegramApiError("rate_limited", description, error_code=429, retry_after=float(seconds), status=status)
    if (200 <= status < 300 or 400 <= status < 500) and status != 429 and 400 <= code < 500 and code != 429:
        raise TelegramApiError("refused", description, error_code=code, status=status)
    raise TelegramApiError("uncertain", f"Contradictory provider response (HTTP {status})", status=status)


# ----- stream uploads ------------------------------------------------------------------------------------------


def _local_refusal(message, code="media_invalid"):
    """A refusal made before any request existed: nothing was sent and nothing can have been applied."""
    return TelegramApiError("refused", message, error_code=400, local_code=code)


def _check_filename(filename):
    if type(filename) is not str or not filename:
        raise _local_refusal("The upload needs a file name")
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError:
        raise _local_refusal("The file name is not valid Unicode") from None
    if len(encoded) > MAX_FILENAME_BYTES or filename in (".", ".."):
        raise _local_refusal("The file name is empty, a dot name or longer than 255 UTF-8 bytes")
    for char in filename:
        # A name that could close the quoted header value, split a header or name a path is never sent.
        if char in '/\\"' or unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp"):
            raise _local_refusal("The file name contains a separator, quote or control character")


def _check_caption(caption):
    if caption is None or caption == "":
        return None
    if type(caption) is not str:
        raise _local_refusal("The caption must be text")
    try:
        caption.encode("utf-8")
    except UnicodeEncodeError:
        raise _local_refusal("The caption is not valid Unicode") from None
    if len(caption) > MAX_CAPTION_CHARS or len(caption.encode("utf-16-le")) // 2 > MAX_CAPTION_UTF16_UNITS:
        raise _local_refusal("The caption exceeds 1024 characters or 1024 UTF-16 code units")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in caption):
        raise _local_refusal("The caption contains a control character")
    return caption


def validate_upload(mode, filename, size, caption=None, content_type=None):
    """Check one upload's metadata before anything is written or opened. Returns ``(caption, content_type)`` as sent.

    ``mode`` is ``document`` or ``photo``. A photo must state the validated image type; a document defaults to a plain
    byte stream. Every failure is a local ``refused`` (code ``media_invalid``), never an uncertain outcome.
    """
    limit = STREAM_LIMITS.get(mode)
    if limit is None:
        raise _local_refusal("Unknown upload mode", "media_unsupported")
    if type(size) is not int or not 1 <= size <= limit:
        raise _local_refusal(f"A {mode} must be 1-{limit} bytes")
    _check_filename(filename)
    caption = _check_caption(caption)
    if content_type is None and mode == "document":
        content_type = "application/octet-stream"
    if type(content_type) is not str or CONTENT_TYPE.fullmatch(content_type) is None:
        raise _local_refusal("The content type is missing or malformed")
    if mode == "photo" and content_type not in PHOTO_CONTENT_TYPES:
        raise _local_refusal("A photo must be a JPEG or PNG image")
    return caption, content_type


def _check_stream(stream, size):
    """The source must be seekable and exactly ``size`` bytes long; it is rewound to its start."""
    try:
        ok = callable(getattr(stream, "read", None)) and stream.seekable()
        if ok:
            end = stream.seek(0, os.SEEK_END)
            stream.seek(0)
    except (AttributeError, OSError, ValueError):
        ok = False
    if not ok:
        raise _local_refusal("The upload source is not a readable, seekable stream")
    if end != size:
        raise _local_refusal("The upload source is not the declared size")


class _Deadline:
    """One absolute budget for a whole request. Every blocking call is armed with what is left, never a fresh timeout."""

    def __init__(self, seconds, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def left(self):
        return self._end - self._clock()

    def arm(self, sock):
        left = self.left()
        if left <= 0:
            raise TimeoutError("The request deadline passed")
        sock.settimeout(left)


class _DeadlineReader(io.RawIOBase):
    """Reads the response through the deadline, so bytes trickled one at a time cannot stretch it."""

    def __init__(self, sock, deadline):
        self._sock, self._deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self._deadline.arm(self._sock)
        return self._sock.recv_into(buffer)


class _ResponseSource:
    """The minimal socket surface ``http.client.HTTPResponse`` reads from."""

    def __init__(self, sock, deadline):
        self._reader = _DeadlineReader(sock, deadline)

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(self._reader)


def _send_all(sock, deadline, data):
    view = memoryview(data)
    while len(view):
        deadline.arm(sock)
        sent = sock.send(view)
        view = view[sent:]


def _resolve(host, port, seconds):
    """Socket addresses for a host within a time budget. Name resolution cannot be interrupted, so a name is looked up on a
    helper thread that is abandoned (it ends when the resolver returns) once the budget is spent. At most ``RESOLVER_LIMIT``
    such helpers exist in the process: a permit is taken before one starts and given back only when its resolver has really
    returned, so a hung resolver cannot accumulate threads across retries. Waiting for a permit and for the answer share one
    budget; running out of it is a failure before anything was sent."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    end = time.monotonic() + max(seconds, 0.0)
    if not _RESOLVER_PERMITS.acquire(timeout=max(end - time.monotonic(), 0.0)):
        raise TimeoutError("Name resolution is saturated by earlier lookups that have not returned")
    outcome = {}

    def lookup():
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:  # noqa: BLE001 - reported to the caller as a failed lookup
            outcome["error"] = exc
        finally:
            _RESOLVER_PERMITS.release()

    try:
        thread = threading.Thread(target=lookup, name="telegram-resolve", daemon=True)
        thread.start()
    except BaseException:
        _RESOLVER_PERMITS.release()
        raise OSError("A name lookup could not be started") from None
    thread.join(max(end - time.monotonic(), 0.0))
    if "error" in outcome:
        error = outcome["error"]
        raise error if isinstance(error, OSError) else OSError(type(error).__name__)
    if "infos" not in outcome:
        raise TimeoutError("Name resolution did not finish in time")
    return outcome["infos"]


def _multipart_head(fields, name, filename, content_type):
    """The multipart bytes before the file, and the closing bytes after it. Text values are UTF-8 as sent by browsers."""
    while True:
        boundary = "qorqut-" + uuid.uuid4().hex
        marker = boundary.encode()
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
                 for key, value in fields]
        if not any(marker in part[len(marker) + 2:] for part in parts):
            break
    head = b"".join(parts) + (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                              f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
    return boundary, head, f"\r\n--{boundary}--\r\n".encode()


class HttpBotApi(BotApi):
    def __init__(self, token, *, base_url="https://api.telegram.org", connect_timeout=10.0, read_timeout=30.0,
                 context=None, stream_deadline=STREAM_DEADLINE_SECONDS):
        if not isinstance(token, Secret):
            raise TypeError("The bot token must be wrapped as a Secret")
        parts = urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("Invalid Bot API base URL")
        if not 0 < connect_timeout <= 60 or not 0 < read_timeout <= 120:
            raise ValueError("Bot API timeouts must be positive and bounded")
        if not 0 < stream_deadline <= STREAM_DEADLINE_SECONDS:
            raise ValueError("The upload deadline must be positive and within the bridge stop allowance")
        self._token = token
        self._polling = set()
        self._polling_lock = threading.Lock()
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, parts.port
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.stream_deadline = stream_deadline
        self._context = context

    def _connect(self):
        if self._scheme == "https":
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=self.connect_timeout,
                                                     context=self._context or ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=self.connect_timeout)
        try:
            connection.connect()
        except (OSError, ssl.SSLError) as exc:
            connection.close()
            # Nothing has been written yet: the request cannot have been applied.
            raise TelegramApiError("unreachable", type(exc).__name__) from None
        return connection

    def abort_polling(self):
        """Interrupt in-flight long polls for shutdown. Sends are never aborted: that would turn them uncertain."""
        with self._polling_lock:
            connections = list(self._polling)
        for connection in connections:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass

    def call(self, method, params=None, *, files=None, read_timeout=None):
        connection = self._connect()
        polling = method == "getUpdates"
        if polling:
            with self._polling_lock:
                self._polling.add(connection)
        try:
            path = f"/bot{self._token.reveal()}/{method}"
            if files:
                content_type, body = _multipart({k: (json.dumps(v) if not isinstance(v, str) else v)
                                                 for k, v in (params or {}).items()}, files)
            else:
                content_type, body = "application/json", json.dumps(params or {}, ensure_ascii=False).encode()
            connection.sock.settimeout(read_timeout or self.read_timeout)
            try:
                connection.request("POST", path, body=body, headers={"Content-Type": content_type,
                                                                     "Content-Length": str(len(body))})
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            except (socket.timeout, TimeoutError):
                raise TelegramApiError("timeout", "The request timed out after it may have been sent") from None
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise TelegramApiError("uncertain", type(exc).__name__) from None
            if len(payload) > MAX_RESPONSE_BYTES:
                raise TelegramApiError("uncertain", "Oversized provider response", status=response.status)
            return _interpret(response.status, payload)
        finally:
            if polling:
                with self._polling_lock:
                    self._polling.discard(connection)
            connection.close()

    def get_me(self):
        return self.call("getMe")

    def get_webhook_info(self):
        return self.call("getWebhookInfo")

    def get_chat(self, chat_id):
        return self.call("getChat", {"chat_id": chat_id})

    def get_chat_member(self, chat_id, user_id):
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_updates(self, offset, timeout):
        timeout = min(int(timeout), MAX_LONG_POLL_SECONDS)
        return self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": list(ALLOWED_UPDATES)},
                         read_timeout=timeout + 10.0)

    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        params = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if message_thread_id is not None:
            # A forum topic is always addressed explicitly: a missing thread would land in General.
            params["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return self.call("sendMessage", params)

    def send_document(self, chat_id, filename, data, caption=None, message_thread_id=None, reply_to_message_id=None):
        if len(data) > MAX_DOCUMENT_BYTES:
            raise TelegramApiError("refused", "The document exceeds Telegram's 50 MB bot upload limit", error_code=413)
        params = {"chat_id": str(chat_id)}
        if message_thread_id is not None:
            params["message_thread_id"] = str(message_thread_id)
        if caption:
            params["caption"] = caption
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return self.call("sendDocument", params, files={"document": (filename, data)})

    def send_document_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                             reply_to_message_id=None, content_type=None):
        """Upload ``size`` bytes of a seekable stream as a document (a downloadable file) to the exact chat and topic."""
        return self._upload("sendDocument", "document", chat_id, filename, stream, size, caption, message_thread_id,
                            reply_to_message_id, content_type)

    def send_photo_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                          reply_to_message_id=None, content_type=None):
        """Upload a JPEG or PNG image as a displayed photo. It is never sent as a document instead."""
        return self._upload("sendPhoto", "photo", chat_id, filename, stream, size, caption, message_thread_id,
                            reply_to_message_id, content_type)

    def _upload(self, method, mode, chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id,
                content_type):
        if type(chat_id) is not int or chat_id == 0:
            raise _local_refusal("The chat identifier is not valid")
        for value, name in ((message_thread_id, "topic"), (reply_to_message_id, "reply target")):
            if value is not None and (type(value) is not int or value <= 0):
                raise _local_refusal(f"The {name} identifier is not valid")
        caption, content_type = validate_upload(mode, filename, size, caption, content_type)
        _check_stream(stream, size)
        token = self._token.reveal()
        if TOKEN_PATH.fullmatch(token) is None:
            raise _local_refusal("The bot token cannot be used in a request path", "media_unsupported")
        fields = [("chat_id", str(chat_id))]
        if message_thread_id is not None:
            # A forum topic is always addressed explicitly: a missing thread would land in General.
            fields.append(("message_thread_id", str(message_thread_id)))
        if caption:
            fields.append(("caption", caption))      # plain text: no parse_mode is ever sent
        if reply_to_message_id is not None:
            fields.append(("reply_parameters", json.dumps({"message_id": reply_to_message_id, "allow_sending_without_reply": True})))
        boundary, head, tail = _multipart_head(fields, mode, filename, content_type)
        host = f"[{self._host}]" if ":" in self._host else self._host
        if self._port and self._port != (443 if self._scheme == "https" else 80):
            host += f":{self._port}"
        request = (f"POST /bot{token}/{method} HTTP/1.1\r\nHost: {host}\r\nAccept: application/json\r\n"
                   f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
                   f"Content-Length: {len(head) + size + len(tail)}\r\nConnection: close\r\n\r\n").encode("ascii")
        deadline = _Deadline(self.stream_deadline)
        sock = self._open(deadline)
        if deadline.left() <= 0:
            sock.close()
            raise TelegramApiError("unreachable", "The upload deadline passed before the request was written")
        try:
            # From the first byte written, the provider may have received the request: nothing below is retried.
            try:
                _send_all(sock, deadline, request + head)
                remaining = size
                while remaining:
                    chunk = stream.read(min(STREAM_CHUNK_BYTES, remaining))
                    if not isinstance(chunk, bytes) or not 0 < len(chunk) <= min(STREAM_CHUNK_BYTES, remaining):
                        raise OSError("The upload source ended or misbehaved after the request began")
                    _send_all(sock, deadline, chunk)
                    remaining -= len(chunk)
                _send_all(sock, deadline, tail)
                response = http.client.HTTPResponse(_ResponseSource(sock, deadline), method="POST")
                try:
                    response.begin()
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                finally:
                    response.close()
            except TimeoutError:
                raise TelegramApiError("timeout", "The upload timed out after it may have been sent") from None
            except (OSError, http.client.HTTPException) as exc:
                raise TelegramApiError("uncertain", type(exc).__name__) from None
            except Exception as exc:  # noqa: BLE001 - the request may be on the wire: uncertain, and the cause stays visible
                LOGGER.exception("Telegram upload failed unexpectedly")
                raise TelegramApiError("uncertain", type(exc).__name__) from None
        finally:
            sock.close()
        try:
            if len(payload) > MAX_RESPONSE_BYTES:
                raise TelegramApiError("uncertain", "Oversized provider response", status=response.status)
            result = _interpret(response.status, payload)
            if not isinstance(result, dict):
                raise TelegramApiError("uncertain", "The provider answered without a message", status=response.status)
        except TelegramApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - the request was sent: never leave it unrecorded; the cause stays visible
            LOGGER.exception("Telegram upload response could not be interpreted")
            raise TelegramApiError("uncertain", type(exc).__name__) from None
        return result

    def _open(self, deadline):
        """A connected (and, for HTTPS, handshaken) socket within the connect allowance. Nothing has been written yet,
        so every failure here is ``unreachable``."""
        cap = min(self.connect_timeout, STREAM_CONNECT_SECONDS)
        limit = _Deadline(max(min(deadline.left(), cap), 0.0))
        port = self._port or (443 if self._scheme == "https" else 80)
        sock = None
        try:
            for family, kind, proto, _name, address in _resolve(self._host, port, limit.left())[:MAX_RESOLVE_ADDRESSES]:
                candidate = socket.socket(family, kind, proto)
                try:
                    limit.arm(candidate)
                    candidate.connect(address)
                except OSError:
                    candidate.close()
                    continue
                sock = candidate
                break
            if sock is None:
                raise OSError("No address accepted the connection")
            if self._scheme == "https":
                limit.arm(sock)
                sock = (self._context or ssl.create_default_context()).wrap_socket(sock, server_hostname=self._host)
        except (OSError, ssl.SSLError) as exc:
            if sock is not None:
                sock.close()
            raise TelegramApiError("unreachable", type(exc).__name__) from None
        return sock

    def edit_message_text(self, chat_id, message_id, text):
        return self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                              "link_preview_options": {"is_disabled": True}})

    def delete_message(self, chat_id, message_id):
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
