"""Outbound Telegram media transport: bounded multipart uploads, photo/document dispatch, truthful receipts and fairness.

Three layers, all synthetic. No Telegram service, network, token, model or live configuration is used:

* the real ``HttpBotApi`` against a scripted loopback "Bot API" (plain and TLS with a throwaway certificate) that records the exact
  bytes it receives: method, headers, multipart framing, filename/Unicode/topic/reply fields, streamed bounds and the total
  deadline against peers that stall or trickle, and the classification of every failure by what it proves about the effect;
* the real ``Dispatcher._send`` with a fake bridge and loader, for document and photo descriptions that need no database kind;
* the real bridge, dispatcher and outbox on a synthetic empty target store, for fairness, receipts and recovery of attachments
  that use the ``document`` kind the stored schema already has. The assembled ``photo`` row and the whole operation await the
  integration worker: nothing here claims them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from contextlib import contextmanager

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.telegram import api as api_module
from bazarlyk.telegram import store
from bazarlyk.telegram.api import (BotApi, HttpBotApi, STREAM_CHUNK_BYTES, STREAM_DEADLINE_SECONDS, TelegramApiError,
                                   validate_upload)
from bazarlyk.telegram.bridge import DEFAULT_STOP_TIMEOUT, TelegramBridge
from bazarlyk.telegram.config import Secret
from bazarlyk.telegram.outbox import Dispatcher, enqueue_text

BOT_ID = 200200200
TOKEN = f"{BOT_ID}:" + "A" * 35
GROUP_ID = -100900100
TOPIC = 11
CHAT = {"ordubashy": TOPIC, "signoffs": 14, "urgent": 16}
INSTALLATION, OWNER_PRINCIPAL = "00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2"
AGENT = "11111111-1111-4111-8111-111111111111"
OWNER_TG = 1001
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PAYLOAD_ID = "8f14e45f-ceea-4672-9a7c-1d5b0c1f2a3b"


# ===== a scripted loopback Bot API =================================================================================


def ok_reply(request, conn, *, thread=TOPIC, chat=GROUP_ID, message_id=900):
    reply_json(conn, 200, {"ok": True, "result": {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"},
                                                  "message_thread_id": thread, "is_topic_message": True}})


def reply_json(conn, status, body, *, extra_headers=""):
    payload = json.dumps(body).encode()
    reply_raw(conn, status, payload, extra_headers=extra_headers)


def reply_raw(conn, status, payload, *, extra_headers=""):
    conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                 f"Connection: close\r\n{extra_headers}\r\n".encode() + payload)


class Provider:
    """A loopback server that records each request completely and answers as scripted. It can stall, trickle or hang up."""

    KEEP_BODY_UP_TO = 4 * 1024 * 1024

    def __init__(self, behavior=ok_reply, *, tls=None, read_body=True):
        self.behavior, self.read_body = behavior, read_body
        self.requests, self.connections, self.handshake_failures = [], 0, 0
        self.release = threading.Event()
        self._tls = tls
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self._open = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http{'s' if self._tls else ''}://{'localhost' if self._tls else '127.0.0.1'}:{self.port}"

    def _accept(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self._tls is not None:
                try:
                    conn = self._tls.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    self.handshake_failures += 1
                    return
            self._open.append(conn)
            conn.settimeout(20)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in lines[1:])}
            length = int(headers.get("content-length", "0"))
            request = {"method": method, "path": path, "headers": headers, "length": length, "reads": 0}
            if self.read_body:
                digest, kept, got, tail = hashlib.sha256(), bytearray(), 0, b""
                first = b""
                piece = rest
                while True:
                    if piece:
                        if not first:
                            first = piece[:4096]
                        digest.update(piece)
                        got += len(piece)
                        tail = (tail + piece)[-512:]
                        if length <= self.KEEP_BODY_UP_TO:
                            kept += piece
                    if got >= length:
                        break
                    piece = conn.recv(65536)
                    request["reads"] += 1
                    if not piece:
                        break
                request.update(received=got, sha256=digest.hexdigest(), head=first, tail=tail,
                               body=bytes(kept) if length <= self.KEEP_BODY_UP_TO else None)
            self.requests.append(request)
            self.behavior(request, conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.release.set()
        self._listener.close()
        for conn in list(self._open):
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def provider():
    servers = []

    def make(behavior=ok_reply, **kwargs):
        server = Provider(behavior, **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def api_for(server, **kwargs):
    kwargs.setdefault("stream_deadline", 5.0)
    return HttpBotApi(Secret(TOKEN), base_url=server.url, **kwargs)


def parse_multipart(content_type, body):
    """A strict reading of the framing: any deviation from RFC 7578 line endings or boundaries fails the assertion."""
    boundary = re.fullmatch(r"multipart/form-data; boundary=([A-Za-z0-9-]+)", content_type).group(1).encode()
    opening, closing = b"--" + boundary + b"\r\n", b"\r\n--" + boundary + b"--\r\n"
    assert body.startswith(opening) and body.endswith(closing)
    parts = []
    for piece in body[len(opening):-len(closing)].split(b"\r\n--" + boundary + b"\r\n"):
        head, separator, content = piece.partition(b"\r\n\r\n")
        assert separator
        headers = {}
        for line in head.split(b"\r\n"):
            key, _, value = line.partition(b": ")
            headers[key.decode().lower()] = value.decode("utf-8")
        name = re.search(r'name="([^"]*)"', headers["content-disposition"]).group(1)
        filename = re.search(r'filename="([^"]*)"', headers["content-disposition"])
        parts.append({"name": name, "filename": filename.group(1) if filename else None,
                      "content_type": headers.get("content-type"), "data": content, "headers": headers})
    return parts


def fields_of(request):
    parts = parse_multipart(request["headers"]["content-type"], request["body"])
    return {part["name"]: part for part in parts}, [part["name"] for part in parts]


class Virtual(io.RawIOBase):
    """A seekable stream of deterministic bytes that is never stored, so a test can push 50 MB without holding it."""

    BLOCK = bytes(random.Random(7).randrange(256) for _ in range(65521))

    def __init__(self, size):
        self.size, self.pos, self.asked = size, 0, []

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=os.SEEK_SET):
        self.pos = {os.SEEK_SET: offset, os.SEEK_CUR: self.pos + offset, os.SEEK_END: self.size + offset}[whence]
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        self.asked.append(n)
        start = self.pos % len(self.BLOCK)
        data = (self.BLOCK * (n // len(self.BLOCK) + 2))[start:start + n]
        self.pos += n
        return data


def digest_of(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    while block := stream.read(1 << 20):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


PDF = b"%PDF-1.7\r\n--not-a-boundary\r\n" + bytes(range(256)) * 40 + b"\r\n--\r\n\r\n\x00\xff"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


# ===== multipart framing, fields and headers =======================================================================


def test_a_document_is_a_standard_multipart_send_document_with_exact_bytes_topic_and_reply(provider):
    server = provider()
    result = api_for(server).send_document_stream(GROUP_ID, "report.pdf", io.BytesIO(PDF), len(PDF), "Quarterly report",
                                                  message_thread_id=TOPIC, reply_to_message_id=77, content_type="application/pdf")
    assert result["message_id"] == 900 and result["message_thread_id"] == TOPIC and result["chat"]["id"] == GROUP_ID
    [request] = server.requests
    assert (request["method"], request["path"]) == ("POST", f"/bot{TOKEN}/sendDocument")
    assert int(request["headers"]["content-length"]) == len(request["body"]) == request["received"]
    assert "transfer-encoding" not in request["headers"] and request["headers"]["connection"] == "close"
    fields, order = fields_of(request)
    assert order == ["chat_id", "message_thread_id", "caption", "reply_parameters", "document"]
    assert fields["chat_id"]["data"] == str(GROUP_ID).encode() and fields["message_thread_id"]["data"] == str(TOPIC).encode()
    assert fields["caption"]["data"] == b"Quarterly report"
    assert json.loads(fields["reply_parameters"]["data"]) == {"message_id": 77, "allow_sending_without_reply": True}
    assert "parse_mode" not in fields
    document = fields["document"]
    assert (document["filename"], document["content_type"], document["data"]) == ("report.pdf", "application/pdf", PDF)


def test_a_photo_is_a_standard_multipart_send_photo_and_never_a_document(provider):
    server = provider()
    api_for(server).send_photo_stream(GROUP_ID, "chart.png", io.BytesIO(PNG), len(PNG), None, message_thread_id=TOPIC,
                                      content_type="image/png")
    [request] = server.requests
    assert request["path"] == f"/bot{TOKEN}/sendPhoto"
    fields, order = fields_of(request)
    assert order == ["chat_id", "message_thread_id", "photo"] and "document" not in fields
    assert (fields["photo"]["filename"], fields["photo"]["content_type"], fields["photo"]["data"]) == ("chart.png", "image/png", PNG)


def test_optional_fields_are_omitted_and_a_whole_chat_send_names_no_topic(provider):
    server = provider()
    api_for(server).send_document_stream(-1001, "a.bin", io.BytesIO(b"x"), 1)
    fields, order = fields_of(server.requests[0])
    assert order == ["chat_id", "document"] and fields["document"]["content_type"] == "application/octet-stream"


def test_unicode_filename_and_caption_are_sent_as_utf8_and_markup_is_never_interpreted(provider):
    server = provider()
    name = "Отчёт за квартал 📊 (v2) ; x=1 'q' %41 &.pdf"
    caption = "<b>жирный</b> *star* _under_ [link](http://x) \U0001F600\nsecond line\ttab"
    api_for(server).send_document_stream(GROUP_ID, name, io.BytesIO(PDF), len(PDF), caption, message_thread_id=TOPIC)
    fields, order = fields_of(server.requests[0])
    assert fields["document"]["filename"] == name and fields["caption"]["data"].decode("utf-8") == caption
    assert "parse_mode" not in fields and "parse_mode" not in server.requests[0]["body"].decode("utf-8", "replace")


def test_binary_content_with_crlf_and_boundary_like_bytes_survives_unchanged_in_many_chunks(provider):
    server = provider()
    data = (b"\r\n--qorqut-\r\n" + bytes(range(256))) * 2000 + b"\r\n"
    api_for(server).send_document_stream(GROUP_ID, "bin.dat", io.BytesIO(data), len(data), message_thread_id=TOPIC)
    fields, _ = fields_of(server.requests[0])
    assert fields["document"]["data"] == data and len(data) > 3 * STREAM_CHUNK_BYTES


def test_the_source_is_rewound_before_it_is_read(provider):
    server = provider()
    stream = io.BytesIO(PDF)
    stream.seek(0, os.SEEK_END)
    api_for(server).send_document_stream(GROUP_ID, "x.pdf", stream, len(PDF), message_thread_id=TOPIC)
    assert fields_of(server.requests[0])[0]["document"]["data"] == PDF


def test_every_boundary_is_fresh_and_never_inside_a_text_value(provider):
    server = provider()
    api = api_for(server)
    for _ in range(3):
        api.send_document_stream(GROUP_ID, "x.pdf", io.BytesIO(b"abc"), 3, "caption", message_thread_id=TOPIC)
    boundaries = {re.search("boundary=(.*)", r["headers"]["content-type"]).group(1) for r in server.requests}
    assert len(boundaries) == 3


# ===== streaming, memory and exact byte bounds =====================================================================


def test_the_largest_document_streams_in_bounded_chunks_without_being_held_in_memory(provider):
    server = provider()
    size = api_module.STREAM_LIMITS["document"]
    source = Virtual(size)
    expected = digest_of(source)
    source.asked.clear()
    tracemalloc.start()
    try:
        api_for(server, stream_deadline=30.0).send_document_stream(GROUP_ID, "huge.bin", source, size, "cap", message_thread_id=TOPIC)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    [request] = server.requests
    assert peak < 6 * 1024 * 1024, peak
    assert max(source.asked) <= STREAM_CHUNK_BYTES and sum(source.asked) == size
    assert request["received"] == request["length"] and request["body"] is None
    # The file part is the exact source: its length is the declared size plus the (small) framing, and the hash proves the bytes.
    head_end = request["head"].index(b"\r\n\r\n", request["head"].index(b'name="document"')) + 4
    framing = head_end + len(b"\r\n--") + len(re.search(r"boundary=(.*)", request["headers"]["content-type"]).group(1)) + len(b"--\r\n")
    assert request["length"] == size + framing
    body_hash = hashlib.sha256()
    source.seek(0)
    body_hash.update(request["head"][:head_end])
    while block := source.read(1 << 20):
        body_hash.update(block)
    body_hash.update(request["tail"][-(len(b"\r\n--") + len(re.search(r"boundary=(.*)", request["headers"]["content-type"]).group(1)) + 4):])
    assert body_hash.hexdigest() == request["sha256"] and expected


@pytest.mark.parametrize("mode, limit", [("document", 50_000_000), ("photo", 10_000_000)])
def test_the_byte_bounds_are_exact_and_checked_before_a_connection_is_made(provider, mode, limit):
    server = provider()
    api = api_for(server)
    send = api.send_photo_stream if mode == "photo" else api.send_document_stream
    kind = "image/png" if mode == "photo" else None
    for size in (0, -1, limit + 1, True, 1.5, "10", None):
        with pytest.raises(TelegramApiError) as refused:
            send(GROUP_ID, "x.bin", io.BytesIO(b"abc"), size, message_thread_id=TOPIC, content_type=kind)
        assert refused.value.kind == "refused" and refused.value.local_code == "media_invalid" and not refused.value.uncertain
    assert server.connections == 0
    # The bounds themselves are accepted when the stream really has that size.
    assert validate_upload(mode, "x.bin", limit, None, kind) == (None, kind or "application/octet-stream")
    assert validate_upload(mode, "x.bin", 1, None, kind)[0] is None


def test_a_stream_that_is_not_exactly_the_declared_size_or_not_seekable_is_refused_before_connecting(provider):
    server = provider()
    api = api_for(server)

    class OneWay(io.RawIOBase):
        def readable(self):
            return True

        def read(self, n=-1):
            return b"x"

    class Closed(io.BytesIO):
        def seekable(self):
            raise ValueError("closed")

    for stream, size in ((io.BytesIO(b"abcdef"), 5), (io.BytesIO(b"abc"), 4), (OneWay(), 1), (Closed(b"a"), 1), (b"abc", 3),
                         (None, 1), (object(), 1)):
        with pytest.raises(TelegramApiError) as refused:
            api.send_document_stream(GROUP_ID, "x.bin", stream, size, message_thread_id=TOPIC)
        assert refused.value.kind == "refused" and refused.value.local_code == "media_invalid"
    assert server.connections == 0


@pytest.mark.parametrize("name", [
    "", ".", "..", "a/b", "a\\b", 'a"b', "a\r\nX-Injected: 1", "a\nb", "a\x00b", "tab\tname", "a\x7fb", "a\x85b", "a b",
    "a\udc80b", "x" * 256, "é" * 128, None, 5, b"x.pdf"])
def test_unsafe_file_names_are_refused_before_anything_is_written(provider, name):
    server = provider()
    with pytest.raises(TelegramApiError) as refused:
        api_for(server).send_document_stream(GROUP_ID, name, io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)
    assert refused.value.kind == "refused" and refused.value.local_code == "media_invalid"
    assert server.connections == 0


@pytest.mark.parametrize("name", ["x" * 255, "é" * 127 + "a", "файл 📊.pdf", "ЖЖ.tar.gz", ".hidden", "a b;c=d'e%f&g.pdf", "名前.png"])
def test_ordinary_names_up_to_255_utf8_bytes_are_accepted(provider, name):
    assert len(name.encode()) <= 255
    server = provider()
    api_for(server).send_document_stream(GROUP_ID, name, io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)
    assert fields_of(server.requests[0])[0]["document"]["filename"] == name


@pytest.mark.parametrize("caption", ["x" * 1025, "😀" * 513, "a\x00b", "a\x1bb", "a\ud800b", 12, b"cap"])
def test_a_bad_caption_is_refused_before_anything_is_written(provider, caption):
    server = provider()
    with pytest.raises(TelegramApiError) as refused:
        api_for(server).send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, caption, message_thread_id=TOPIC)
    assert refused.value.local_code == "media_invalid" and server.connections == 0


@pytest.mark.parametrize("caption", ["x" * 1024, "😀" * 512, "line\r\nbreak\tok", "", None])
def test_captions_at_the_limits_are_accepted(provider, caption):
    server = provider()
    api_for(server).send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, caption, message_thread_id=TOPIC)
    fields, _ = fields_of(server.requests[0])
    assert ("caption" in fields) == bool(caption)


@pytest.mark.parametrize("kwargs", [
    {"content_type": "text/plain\r\nX-Injected: 1"}, {"content_type": "plain"}, {"content_type": "a/b; charset=x"},
    {"content_type": "A B/c"}, {"content_type": 5}, {"content_type": ""}, {"chat_id": 0}, {"chat_id": "123"}, {"chat_id": True},
    {"chat_id": None}, {"message_thread_id": 0}, {"message_thread_id": -3}, {"message_thread_id": "11"},
    {"reply_to_message_id": 0}, {"reply_to_message_id": True}, {"reply_to_message_id": "9"}])
def test_malformed_headers_and_identifiers_are_refused_before_connecting(provider, kwargs):
    server = provider()
    call = {"chat_id": GROUP_ID, "filename": "x.bin", "stream": io.BytesIO(b"abc"), "size": 3, "message_thread_id": TOPIC}
    call.update(kwargs)
    with pytest.raises(TelegramApiError) as refused:
        api_for(server).send_document_stream(**call)
    assert refused.value.kind == "refused" and refused.value.local_code == "media_invalid"
    assert server.connections == 0


def test_a_photo_must_name_a_jpeg_or_png_and_is_not_downgraded(provider):
    server = provider()
    api = api_for(server)
    for content_type in (None, "application/pdf", "image/gif", "image/webp", "application/octet-stream"):
        with pytest.raises(TelegramApiError) as refused:
            api.send_photo_stream(GROUP_ID, "x.png", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC, content_type=content_type)
        assert refused.value.local_code == "media_invalid"
    api.send_photo_stream(GROUP_ID, "x.jpg", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC, content_type="image/jpeg")
    assert [r["path"] for r in server.requests] == [f"/bot{TOKEN}/sendPhoto"]


def test_the_bot_token_never_appears_in_a_local_error_or_reaches_a_bad_path(provider):
    server = provider()
    api = HttpBotApi(Secret("bad token/../x"), base_url=server.url)
    with pytest.raises(TelegramApiError) as refused:
        api.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)
    assert refused.value.kind == "refused" and "bad token" not in str(refused.value) and server.connections == 0


# ===== failure classification =======================================================================================


def provider_error(status, body):
    return lambda request, conn: reply_json(conn, status, body)


def send_stream(api, **kwargs):
    return api.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(PDF), len(PDF), message_thread_id=TOPIC, **kwargs)


def send_bytes(api):
    return api.send_document(GROUP_ID, "x.bin", PDF, None, message_thread_id=TOPIC)


@pytest.mark.parametrize("sender", [send_stream, send_bytes], ids=["stream", "bytes"])
@pytest.mark.parametrize("behavior, kind, check", [
    (provider_error(429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}}),
     "rate_limited", lambda e: e.retry_after == 7.0 and e.retryable_before_send and not e.uncertain),
    (provider_error(429, {"ok": False, "error_code": 429, "description": "Too Many Requests"}),
     "rate_limited", lambda e: e.retry_after == 30.0),
    (provider_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"}),
     "refused", lambda e: e.error_code == 400 and e.local_code is None and not e.uncertain and not e.retryable_before_send),
    (provider_error(413, {"ok": False, "error_code": 413, "description": "Request Entity Too Large"}),
     "refused", lambda e: e.error_code == 413),
    (provider_error(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}),
     "refused", lambda e: e.error_code == 403),
    (provider_error(502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}),
     "uncertain", lambda e: e.uncertain and not e.retryable_before_send),
    (provider_error(500, {"ok": False, "error_code": 500, "description": "Internal"}), "uncertain", lambda e: e.error_code == 500),
    (lambda request, conn: reply_raw(conn, 502, b"<html>bad gateway</html>"), "uncertain", lambda e: e.status == 502),
    (lambda request, conn: reply_raw(conn, 200, b"not json at all"), "uncertain", lambda e: e.status == 200),
    (lambda request, conn: reply_raw(conn, 200, b'{"result": {"message_id": 1}}'), "uncertain", lambda e: True),
    (lambda request, conn: reply_raw(conn, 200, b'{"ok": tr'), "uncertain", lambda e: True),
    (lambda request, conn: None, "uncertain", lambda e: True),
    (lambda request, conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"ok\": true"), "uncertain", lambda e: True),
], ids=["429-delay", "429-default", "400", "413", "403", "502-json", "500", "502-html", "not-json", "no-ok", "truncated-json",
        "hung-up", "short-body"])
def test_failures_are_classified_by_what_they_prove_and_both_paths_agree(provider, sender, behavior, kind, check):
    server = provider(behavior)
    with pytest.raises(TelegramApiError) as error:
        sender(api_for(server))
    assert error.value.kind == kind and check(error.value), (error.value.kind, error.value.description)
    assert len(server.requests) == 1                      # nothing is ever retried by the transport itself


def test_an_oversized_provider_answer_is_uncertain_not_trusted(provider, monkeypatch):
    monkeypatch.setattr(api_module, "MAX_RESPONSE_BYTES", 200)
    body = {"ok": True, "result": {"message_id": 5, "pad": "x" * 500}}
    server = provider(lambda request, conn: reply_json(conn, 200, body))
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server))
    assert error.value.kind == "uncertain" and "Oversized" in error.value.description


def test_a_success_without_a_message_object_is_uncertain(provider):
    server = provider(lambda request, conn: reply_json(conn, 200, {"ok": True, "result": True}))
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server))
    assert error.value.kind == "uncertain"


def test_a_refusal_the_provider_marks_as_ok_false_with_http_200_is_still_a_refusal(provider):
    server = provider(provider_error(200, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}))
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server))
    assert (error.value.kind, error.value.error_code) == ("refused", 400)


MALFORMED_ANSWERS = [
    (200, {"ok": "true", "result": {"message_id": 17}}), (200, {"ok": 1, "result": {"message_id": 17}}),
    (200, {"ok": None, "result": {"message_id": 17}}), (200, {"result": {"message_id": 17}}), (200, {"ok": [], "result": {}}),
    (200, {"ok": "false", "error_code": 400, "description": "x"}),
    (400, {"ok": False, "error_code": 400, "parameters": ["unexpected"]}),
    (429, {"ok": False, "error_code": 429, "parameters": "unexpected"}),
    (500, {"ok": False, "error_code": 400, "description": "contradictory server error"}),
    (500, {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}),
    (503, {"ok": True, "result": {"message_id": 1}}),
    (200, {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}),          # a rate limit needs HTTP 429 as well
    (429, {"ok": False, "error_code": 400, "description": "bad"}),
    (400, {"ok": False, "error_code": 429, "description": "slow down"}),
    (400, {"ok": False, "error_code": 500, "description": "boom"}),
    (400, {"ok": False, "error_code": "400", "description": "bad"}), (400, {"ok": False, "error_code": 400.0, "description": "bad"}),
    (400, {"ok": False, "error_code": True, "description": "bad"}), (400, {"ok": False, "error_code": None, "description": "bad"}),
    (400, {"ok": False, "error_code": 400, "description": 5}), (400, {"ok": False, "error_code": 400, "description": None}),
    (400, {"ok": True, "result": {"message_id": 1}}), (302, {"ok": False, "error_code": 400, "description": "moved"}),
    (200, {"ok": False}), (200, {"ok": False, "error_code": 200, "description": "odd"}),
    (200, {"ok": False, "error_code": 500, "description": "odd"}), (404, {"ok": False, "error_code": 404, "parameters": None, "description": 7})]


@pytest.mark.parametrize("status, body", MALFORMED_ANSWERS, ids=lambda value: value if isinstance(value, int) else "")
def test_a_malformed_or_contradictory_answer_is_uncertain_never_a_refusal_a_retry_or_a_crash(status, body):
    with pytest.raises(TelegramApiError) as error:
        api_module._interpret(status, json.dumps(body).encode())
    assert error.value.kind == "uncertain" and error.value.uncertain and not error.value.retryable_before_send
    assert error.value.status == status


@pytest.mark.parametrize("payload", [b"", b"\xff\xfe", b"[" * 200_000, b'{"ok": tru', b"null", b'"ok"', b"[1]", b'{"ok": NaN}',
                                     b'{"ok": true' + b" " * 10, b"{" * 5000])
def test_undecodable_or_hostile_bytes_are_uncertain(payload):
    for status in (200, 400, 429, 502):
        with pytest.raises(TelegramApiError) as error:
            api_module._interpret(status, payload)
        assert error.value.kind == "uncertain"


def test_a_server_error_is_uncertain_whatever_its_body_says():
    for status in (500, 502, 503, 504, 599):
        for body in ({"ok": True, "result": {"message_id": 1}}, {"ok": False, "error_code": 400, "description": "bad"},
                     {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}, {"ok": False, "error_code": status}, {}):
            with pytest.raises(TelegramApiError) as error:
                api_module._interpret(status, json.dumps(body).encode())
            assert error.value.kind == "uncertain" and error.value.status == status and error.value.retry_after is None


def test_the_standard_answers_keep_their_meaning():
    message = {"message_id": 5, "chat": {"id": GROUP_ID}}
    assert api_module._interpret(200, json.dumps({"ok": True, "result": message}).encode()) == message
    for other in (True, [{"update_id": 1}], {"id": BOT_ID, "is_bot": True}, [], None):
        assert api_module._interpret(200, json.dumps({"ok": True, "result": other}).encode()) == other
    for status, code in ((400, 400), (403, 403), (404, 404), (409, 409), (413, 413), (200, 400)):
        with pytest.raises(TelegramApiError) as error:
            api_module._interpret(status, json.dumps({"ok": False, "error_code": code, "description": "Bad Request: x"}).encode())
        assert (error.value.kind, error.value.error_code, error.value.description) == ("refused", code, "Bad Request: x")
    with pytest.raises(TelegramApiError) as error:                       # no body code: the HTTP status is the code
        api_module._interpret(403, json.dumps({"ok": False}).encode())
    assert (error.value.kind, error.value.error_code) == ("refused", 403)
    for parameters, seconds in (({"retry_after": 7}, 7.0), ({"retry_after": 2.5}, 2.5), ({"retry_after": 0}, 0.0), ({}, 30.0),
                                ({"retry_after": "7"}, 30.0), ({"retry_after": -1}, 30.0), ({"retry_after": 10 ** 6}, 30.0),
                                ({"retry_after": True}, 30.0), (None, 30.0)):
        with pytest.raises(TelegramApiError) as error:
            api_module._interpret(429, json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests",
                                                   "parameters": parameters}).encode())
        assert (error.value.kind, error.value.retry_after, error.value.retryable_before_send) == ("rate_limited", seconds, True)
    with pytest.raises(TelegramApiError) as error:
        api_module._interpret(429, b'{"ok": false, "parameters": {"retry_after": NaN}}')
    assert error.value.kind == "rate_limited" and error.value.retry_after == 30.0


@pytest.mark.parametrize("sender", [send_stream, send_bytes], ids=["stream", "bytes"])
@pytest.mark.parametrize("status, body", [MALFORMED_ANSWERS[0], MALFORMED_ANSWERS[6], MALFORMED_ANSWERS[7], MALFORMED_ANSWERS[8],
                                          MALFORMED_ANSWERS[9], MALFORMED_ANSWERS[10], MALFORMED_ANSWERS[11], MALFORMED_ANSWERS[14]],
                         ids=lambda value: value if isinstance(value, int) else "")
def test_a_malformed_or_contradictory_answer_over_the_wire_is_uncertain_on_both_paths(provider, sender, status, body):
    server = provider(provider_error(status, body))
    with pytest.raises(TelegramApiError) as error:
        sender(api_for(server))
    assert error.value.kind == "uncertain" and error.value.status == status and len(server.requests) == 1


def test_a_deeply_nested_answer_over_the_wire_is_uncertain_on_both_paths(provider):
    server = provider(lambda request, conn: reply_raw(conn, 200, b"[" * 300_000))
    for sender in (send_stream, send_bytes):
        with pytest.raises(TelegramApiError) as error:
            sender(api_for(server))
        assert error.value.kind == "uncertain"


def test_ordinary_calls_still_return_non_message_results(provider):
    answers = iter([{"ok": True, "result": True}, {"ok": True, "result": [{"update_id": 4}]}, {"ok": True, "result": {"id": BOT_ID}}])
    server = provider(lambda request, conn: reply_json(conn, 200, next(answers)))
    api = api_for(server)
    assert api.delete_message(GROUP_ID, 5) is True
    assert api.get_updates(0, 0) == [{"update_id": 4}]
    assert api.get_me() == {"id": BOT_ID}


def closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_an_unreachable_provider_is_proven_pre_send_and_retryable_and_writes_nothing():
    api = HttpBotApi(Secret(TOKEN), base_url=f"http://127.0.0.1:{closed_port()}", stream_deadline=5.0)
    source = Virtual(100)
    with pytest.raises(TelegramApiError) as error:
        api.send_document_stream(GROUP_ID, "x.bin", source, 100, message_thread_id=TOPIC)
    assert error.value.kind == "unreachable" and error.value.retryable_before_send and not error.value.uncertain
    assert source.asked == []                                # not a byte of the source was read


def test_a_stall_after_the_request_is_a_timeout_never_a_pre_send_failure(provider):
    server = provider(lambda request, conn: server.release.wait(10))
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server, stream_deadline=0.6))
    assert error.value.kind == "timeout" and error.value.uncertain and not error.value.retryable_before_send
    assert 0.5 <= time.monotonic() - started < 3


def test_a_connection_reset_in_the_middle_of_the_body_is_uncertain(provider):
    def reset(request, conn):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        conn.close()

    server = provider(reset, read_body=False)
    data = Virtual(30_000_000)
    with pytest.raises(TelegramApiError) as error:
        api_for(server, stream_deadline=10).send_document_stream(GROUP_ID, "x.bin", data, 30_000_000, message_thread_id=TOPIC)
    assert error.value.kind in ("uncertain", "timeout") and error.value.uncertain


def test_a_source_that_fails_after_the_request_began_is_uncertain_and_the_request_is_left_incomplete(provider):
    class Flaky(io.BytesIO):
        calls = 0

        def read(self, n=-1):
            Flaky.calls += 1
            if Flaky.calls > 2:
                raise OSError("disk error")
            return super().read(n)

    data = bytes(300_000)
    server = provider(lambda request, conn: server.release.wait(10), read_body=False)
    with pytest.raises(TelegramApiError) as error:
        api_for(server).send_document_stream(GROUP_ID, "x.bin", Flaky(data), len(data), message_thread_id=TOPIC)
    assert error.value.kind == "uncertain" and error.value.description == "OSError"
    deadline = time.monotonic() + 3
    while not server.requests and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.requests and server.requests[0]["length"] > len(data)         # the promised body was never completed


def test_a_source_that_returns_too_little_or_too_much_is_uncertain(provider):
    class Short(io.BytesIO):
        def read(self, n=-1):
            return b""

    class Long(io.BytesIO):
        def read(self, n=-1):
            return b"x" * (n + 5)

    server = provider(lambda request, conn: server.release.wait(10), read_body=False)
    for stream_type in (Short, Long):
        with pytest.raises(TelegramApiError) as error:
            api_for(server).send_document_stream(GROUP_ID, "x.bin", stream_type(b"abcdef"), 6, message_thread_id=TOPIC)
        assert error.value.kind == "uncertain"


# ===== the absolute total deadline =================================================================================


def test_the_default_deadline_fits_the_bridge_stop_allowance_and_cannot_be_raised_past_it():
    assert STREAM_DEADLINE_SECONDS == 30.0 < DEFAULT_STOP_TIMEOUT
    assert HttpBotApi(Secret(TOKEN)).stream_deadline == STREAM_DEADLINE_SECONDS
    for value in (0, -1, 30.5, 120):
        with pytest.raises(ValueError):
            HttpBotApi(Secret(TOKEN), stream_deadline=value)
    assert HttpBotApi(Secret(TOKEN), connect_timeout=60, read_timeout=120).stream_deadline == STREAM_DEADLINE_SECONDS


def trickle_headers(request, conn):
    # Valid response bytes, one per tick, long after the request was received: each recv alone would succeed forever.
    for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 40\r\n\r\n" + b'{"ok": true, "result": {"message_id": 1}}':
        conn.sendall(bytes([byte]))
        time.sleep(0.08)


def trickle_body(request, conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4000\r\n\r\n")
    for _ in range(4000):
        conn.sendall(b" ")
        time.sleep(0.08)


@pytest.mark.parametrize("behavior", [trickle_headers, trickle_body], ids=["headers", "body"])
def test_a_trickled_response_cannot_stretch_the_total_deadline(provider, behavior):
    server = provider(behavior)
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server, stream_deadline=0.7))
    elapsed = time.monotonic() - started
    assert error.value.kind == "timeout" and 0.6 <= elapsed < 2.5, elapsed


def test_a_peer_that_stops_reading_cannot_stretch_the_total_deadline(provider):
    server = provider(lambda request, conn: server.release.wait(10), read_body=False)
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        api_for(server, stream_deadline=0.8).send_document_stream(GROUP_ID, "x.bin", Virtual(40_000_000), 40_000_000,
                                                                  message_thread_id=TOPIC)
    assert error.value.kind == "timeout" and 0.7 <= time.monotonic() - started < 3


def test_a_peer_that_reads_slowly_cannot_stretch_the_total_deadline(provider):
    def slow(request, conn):
        while not server.release.is_set():
            conn.recv(1024)
            time.sleep(0.05)

    server = provider(slow, read_body=False)
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        api_for(server, stream_deadline=0.8).send_document_stream(GROUP_ID, "x.bin", Virtual(40_000_000), 40_000_000,
                                                                  message_thread_id=TOPIC)
    assert error.value.kind == "timeout" and 0.7 <= time.monotonic() - started < 3


def test_a_budget_spent_before_the_first_byte_is_a_pre_send_failure_not_a_timeout(provider, monkeypatch):
    server = provider()
    source = Virtual(100)
    connect = HttpBotApi._open

    def slow_connect(self, deadline):
        sock = connect(self, deadline)
        time.sleep(0.4)
        return sock

    monkeypatch.setattr(HttpBotApi, "_open", slow_connect)
    with pytest.raises(TelegramApiError) as error:
        api_for(server, stream_deadline=0.2).send_document_stream(GROUP_ID, "x.bin", source, 100, message_thread_id=TOPIC)
    assert error.value.kind == "unreachable" and error.value.retryable_before_send and source.asked == []
    assert server.requests == []


def test_the_larger_read_timeout_of_ordinary_calls_cannot_extend_an_upload_deadline(provider):
    server = provider(lambda request, conn: server.release.wait(10))
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        send_stream(api_for(server, stream_deadline=0.6, read_timeout=120, connect_timeout=60))
    assert error.value.kind == "timeout" and time.monotonic() - started < 3


def shim(**replacements):
    """The ``socket`` module as the transport sees it, with some members replaced, without touching the real module."""
    return SimpleNamespace(**{**{name: getattr(socket, name) for name in dir(socket)}, **replacements})


def test_connect_is_capped_at_ten_seconds_and_at_what_is_left_of_the_total(monkeypatch):
    armed = []

    class Stalled:
        def __init__(self, *args):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def connect(self, address):
            armed.append(self.timeout)
            raise TimeoutError("connect timed out")

        def close(self):
            pass

    monkeypatch.setattr(api_module, "socket", shim(socket=Stalled))
    monkeypatch.setattr(api_module, "_resolve", lambda host, port, seconds: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.1", 80))])
    for connect, total, ceiling in ((60, 30, 10.0), (3, 30, 3.0), (60, 2, 2.0)):
        armed.clear()
        api = HttpBotApi(Secret(TOKEN), base_url="http://bot.invalid", connect_timeout=connect, stream_deadline=total)
        with pytest.raises(TelegramApiError) as error:
            api.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)
        assert error.value.kind == "unreachable"
        assert armed and 0 < armed[0] <= ceiling and armed[0] > ceiling - 1, (armed, ceiling)


def wait_resolvers_done(seconds=5.0):
    """Wait for every abandoned resolver helper to return, so one test's blocked lookup never leaks into the next."""
    end = time.monotonic() + seconds
    while any(thread.name == "telegram-resolve" and thread.is_alive() for thread in threading.enumerate()) and time.monotonic() < end:
        time.sleep(0.02)


def resolver_helpers():
    return [thread for thread in threading.enumerate() if thread.name == "telegram-resolve" and thread.is_alive()]


def test_name_resolution_that_never_returns_is_abandoned_within_the_connect_budget(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(api_module, "socket", shim(getaddrinfo=lambda *args, **kwargs: gate.wait(20)))
    api = HttpBotApi(Secret(TOKEN), base_url="http://bot.invalid", connect_timeout=0.5, stream_deadline=5)
    started = time.monotonic()
    try:
        with pytest.raises(TelegramApiError) as error:
            api.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)
    finally:
        gate.set()
        wait_resolvers_done()
    assert error.value.kind == "unreachable" and 0.4 <= time.monotonic() - started < 2


def test_a_resolver_that_stays_blocked_never_has_more_than_two_helpers_and_lookups_recover(provider, monkeypatch):
    gate, lock, running, peak = threading.Event(), threading.Lock(), [0], [0]
    real = socket.getaddrinfo

    def blocked(host, port, *args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            gate.wait(30)
        finally:
            with lock:
                running[0] -= 1
        return real("127.0.0.1", port, type=socket.SOCK_STREAM)

    monkeypatch.setattr(api_module, "socket", shim(getaddrinfo=blocked))
    api = HttpBotApi(Secret(TOKEN), base_url="http://bot.invalid", connect_timeout=0.3, stream_deadline=5)
    source, durations = Virtual(100), []
    try:
        for _ in range(8):
            started = time.monotonic()
            with pytest.raises(TelegramApiError) as error:
                api.send_document_stream(GROUP_ID, "x.bin", source, 100, message_thread_id=TOPIC)
            durations.append(time.monotonic() - started)
            assert error.value.kind == "unreachable" and error.value.retryable_before_send
            assert len(resolver_helpers()) <= api_module.RESOLVER_LIMIT == 2
        assert peak[0] == 2 and max(durations) < 1.5 and min(durations) >= 0.25 and source.asked == []
    finally:
        gate.set()
        wait_resolvers_done()
    assert resolver_helpers() == [] and running[0] == 0
    monkeypatch.undo()
    server = provider()
    recovered = HttpBotApi(Secret(TOKEN), base_url=f"http://localhost:{server.port}", stream_deadline=5)
    assert recovered.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)["message_id"] == 900


def both_permits_free():
    taken = []
    try:
        while api_module._RESOLVER_PERMITS.acquire(blocking=False):
            taken.append(1)
        return len(taken) == api_module.RESOLVER_LIMIT
    finally:
        for _ in taken:
            api_module._RESOLVER_PERMITS.release()


def test_a_resolver_permit_is_released_when_the_lookup_fails_or_cannot_start(monkeypatch):
    def failing(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    def odd(*args, **kwargs):
        raise UnicodeError("label empty or too long")

    for resolver, expected in ((failing, socket.gaierror), (odd, OSError)):
        monkeypatch.setattr(api_module, "socket", shim(getaddrinfo=resolver))
        for _ in range(5):                                           # more calls than permits: none may leak
            with pytest.raises(expected):
                api_module._resolve("bot.invalid", 80, 1.0)
        wait_resolvers_done()
        assert both_permits_free()

    class NoThreads:
        class Thread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        BoundedSemaphore = threading.BoundedSemaphore

    monkeypatch.setattr(api_module, "threading", NoThreads)
    for _ in range(5):
        with pytest.raises(OSError):
            api_module._resolve("bot.invalid", 80, 1.0)
    assert both_permits_free()


def test_waiting_for_a_resolver_permit_shares_the_same_budget(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(api_module, "socket", shim(getaddrinfo=lambda *args, **kwargs: gate.wait(20)))
    try:
        for _ in range(api_module.RESOLVER_LIMIT):
            with pytest.raises(TimeoutError):
                api_module._resolve("bot.invalid", 80, 0.05)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            api_module._resolve("bot.invalid", 80, 0.4)               # no permit: the wait uses the whole 0.4 s once, not twice
        assert 0.3 <= time.monotonic() - started < 0.8 and len(resolver_helpers()) == 2
    finally:
        gate.set()
        wait_resolvers_done()
    assert both_permits_free()


def test_a_hostname_is_resolved_and_used(provider):
    server = provider()
    api = HttpBotApi(Secret(TOKEN), base_url=f"http://localhost:{server.port}", stream_deadline=5)
    assert api.send_document_stream(GROUP_ID, "x.bin", io.BytesIO(b"abc"), 3, message_thread_id=TOPIC)["message_id"] == 900
    assert server.requests[0]["headers"]["host"] == f"localhost:{server.port}"


# ===== TLS ============================================================================================================


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    directory = tmp_path_factory.mktemp("tls")
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    return directory


def tls_pair(certificate):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate / "cert.pem", certificate / "key.pem")
    client = ssl.create_default_context(cafile=str(certificate / "cert.pem"))
    return server, client


def test_an_https_upload_streams_over_tls_with_exact_bytes(provider, certificate):
    server_context, client_context = tls_pair(certificate)
    server = provider(tls=server_context)
    data = Virtual(6_000_000)
    api = HttpBotApi(Secret(TOKEN), base_url=server.url, context=client_context, stream_deadline=20)
    assert api.send_photo_stream(GROUP_ID, "p.png", data, 6_000_000, "cap", message_thread_id=TOPIC,
                                 content_type="image/png")["message_id"] == 900
    [request] = server.requests
    assert request["path"] == f"/bot{TOKEN}/sendPhoto" and request["received"] == request["length"]
    assert request["body"] is None and max(data.asked) <= STREAM_CHUNK_BYTES


def test_an_https_trickle_is_stopped_by_the_same_total_deadline(provider, certificate):
    server_context, client_context = tls_pair(certificate)
    server = provider(trickle_headers, tls=server_context)
    api = HttpBotApi(Secret(TOKEN), base_url=server.url, context=client_context, stream_deadline=0.9)
    started = time.monotonic()
    with pytest.raises(TelegramApiError) as error:
        send_stream(api)
    assert error.value.kind == "timeout" and 0.8 <= time.monotonic() - started < 3


def test_an_untrusted_certificate_is_a_pre_send_failure_and_the_request_is_never_written(provider, certificate):
    server_context, _ = tls_pair(certificate)
    server = provider(tls=server_context)
    source = Virtual(100)
    api = HttpBotApi(Secret(TOKEN), base_url=server.url, context=ssl.create_default_context(), stream_deadline=5)
    with pytest.raises(TelegramApiError) as error:
        api.send_document_stream(GROUP_ID, "x.bin", source, 100, message_thread_id=TOPIC)
    assert error.value.kind == "unreachable" and server.requests == [] and source.asked == []


def test_a_peer_that_never_completes_the_handshake_is_bounded_by_the_connect_allowance():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        api = HttpBotApi(Secret(TOKEN), base_url=f"https://localhost:{listener.getsockname()[1]}", connect_timeout=0.5,
                         stream_deadline=5)
        started = time.monotonic()
        with pytest.raises(TelegramApiError) as error:
            send_stream(api)
        assert error.value.kind == "unreachable" and 0.4 <= time.monotonic() - started < 2.5
    finally:
        listener.close()


# ===== the existing byte path is unchanged =========================================================================


def test_the_legacy_document_and_message_paths_keep_their_exact_wire_format(provider):
    server = provider()
    api = api_for(server)
    api.send_message(GROUP_ID, "hello", 5, TOPIC)
    api.send_document(GROUP_ID, 'we"ird\r\n.html', b"<p>x</p>", "Cap", TOPIC)
    api.send_document(-1001, "plain.html", b"<p>y</p>")
    message, document, whole_chat = server.requests
    assert message["path"].endswith("/sendMessage") and message["headers"]["content-type"] == "application/json"
    assert json.loads(message["body"]) == {"chat_id": GROUP_ID, "text": "hello", "link_preview_options": {"is_disabled": True},
                                           "message_thread_id": TOPIC, "reply_parameters": {"message_id": 5, "allow_sending_without_reply": True}}
    fields, order = fields_of(document)
    assert order == ["chat_id", "message_thread_id", "caption", "document"]
    assert (fields["chat_id"]["data"], fields["message_thread_id"]["data"], fields["caption"]["data"]) == (str(GROUP_ID).encode(), b"11", b"Cap")
    assert (fields["document"]["filename"], fields["document"]["content_type"], fields["document"]["data"]) == (
        "weird.html", "application/octet-stream", b"<p>x</p>")
    assert fields_of(whole_chat)[1] == ["chat_id", "document"]


def test_a_legacy_document_can_now_reply_through_normal_reply_parameters(provider):
    server = provider()
    api_for(server).send_document(GROUP_ID, "a.html", b"x", None, TOPIC, reply_to_message_id=41)
    fields, order = fields_of(server.requests[0])
    assert order == ["chat_id", "message_thread_id", "reply_parameters", "document"]
    assert json.loads(fields["reply_parameters"]["data"]) == {"message_id": 41, "allow_sending_without_reply": True}


def test_the_legacy_byte_limit_and_read_timeout_are_unchanged(provider, monkeypatch):
    monkeypatch.setattr(api_module, "MAX_DOCUMENT_BYTES", 10)
    server = provider(lambda request, conn: server.release.wait(10))
    api = api_for(server, read_timeout=0.4)
    with pytest.raises(TelegramApiError) as error:
        api.send_document(GROUP_ID, "x.html", b"x" * 11)
    assert (error.value.kind, error.value.error_code) == ("refused", 413) and error.value.local_code is None
    assert server.connections == 0
    with pytest.raises(TelegramApiError) as error:
        api.send_document(GROUP_ID, "x.html", b"x" * 10)
    assert error.value.kind == "timeout"


def test_the_port_declares_the_new_methods_and_the_existing_reply_parameter():
    port = BotApi()
    for call in (lambda: port.send_document_stream(1, "x", io.BytesIO(), 1), lambda: port.send_photo_stream(1, "x", io.BytesIO(), 1),
                 lambda: port.send_document(1, "x", b"", None, None, 5)):
        with pytest.raises(NotImplementedError):
            call()


# ===== Dispatcher._send over a fake bridge ==========================================================================


class RecordingApi(BotApi):
    def __init__(self):
        self.calls = []
        self.result = {"message_id": 901, "chat": {"id": GROUP_ID, "type": "supergroup"}, "message_thread_id": TOPIC,
                       "is_topic_message": True}
        self.error = None

    def _record(self, label, chat_id, filename, stream, size, caption, thread, reply, content_type):
        self.calls.append({"label": label, "chat_id": chat_id, "filename": filename, "data": stream.read(), "size": size, "caption": caption,
                           "message_thread_id": thread, "reply_to_message_id": reply, "content_type": content_type, "open": not stream.closed})
        if self.error is not None:
            raise self.error
        return self.result

    def send_document_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None, reply_to_message_id=None,
                             content_type=None):
        return self._record("document_stream", chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id, content_type)

    def send_photo_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None, reply_to_message_id=None,
                          content_type=None):
        return self._record("photo_stream", chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id, content_type)

    def send_document(self, chat_id, filename, data, caption=None, message_thread_id=None, reply_to_message_id=None):
        self.calls.append({"label": "document", "chat_id": chat_id, "filename": filename, "data": data, "caption": caption,
                           "message_thread_id": message_thread_id, "reply_to_message_id": reply_to_message_id})
        return self.result

    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        self.calls.append({"label": "message", "text": text, "reply_to": reply_to_message_id, "message_thread_id": message_thread_id})
        return self.result

    def edit_message_text(self, chat_id, message_id, text):
        self.calls.append({"label": "edit", "message_id": message_id, "text": text})
        return {"message_id": message_id}


class LoaderProbe:
    """A media loader factory that records its use and yields a stream over fixed bytes."""

    def __init__(self, data=PDF, *, name="report.pdf", caption="Cap", content_type="application/pdf", yields=None, enter_error=None,
                 exit_error=None):
        self.data, self.name, self.caption, self.content_type = data, name, caption, content_type
        self.yields, self.enter_error, self.exit_error = yields, enter_error, exit_error
        self.entered, self.exited, self.payloads, self.stream = 0, 0, [], None

    def __call__(self, payload):
        self.payloads.append(payload)
        probe = self

        @contextmanager
        def context():
            if probe.enter_error is not None:
                raise probe.enter_error
            probe.entered += 1
            probe.stream = io.BytesIO(probe.data)
            try:
                yield probe.yields if probe.yields is not None else (probe.name, probe.stream, len(probe.data), probe.caption,
                                                                     probe.content_type)
            finally:
                probe.exited += 1
                probe.stream.close()
                if probe.exit_error is not None:
                    raise probe.exit_error

        return context()


def description(kind="document", *, payload=None, source_kind="agent_file", thread=TOPIC, reply=None):
    value = {"kind": kind, "chat_id": GROUP_ID, "message_thread_id": thread, "chat_name": "ordubashy", "id": "outbox-1",
             "source_kind": source_kind, "binding": {},
             "payload": {"version": 1, "payload_id": PAYLOAD_ID} if payload is None else payload}
    if reply is not None:
        value["reply_to"] = reply
    return value


def fake_dispatcher(loader=None, api=None):
    bridge = SimpleNamespace(api=api or RecordingApi(), media_loaders={} if loader is None else {"agent_file": loader})
    return Dispatcher(bridge), bridge


def test_a_document_attachment_goes_through_the_loader_to_the_document_stream_with_the_frozen_destination():
    loader = LoaderProbe()
    dispatcher, bridge = fake_dispatcher(loader)
    result = dispatcher._send(description("document", reply=77))
    assert result is bridge.api.result
    [call] = bridge.api.calls
    assert call == {"label": "document_stream", "chat_id": GROUP_ID, "filename": "report.pdf", "data": PDF, "size": len(PDF), "caption": "Cap",
                    "message_thread_id": TOPIC, "reply_to_message_id": 77, "content_type": "application/pdf", "open": True}
    assert loader.payloads == [{"version": 1, "payload_id": PAYLOAD_ID}] and (loader.entered, loader.exited) == (1, 1)
    assert loader.stream.closed                                  # the source is released once the send returned


def test_a_photo_attachment_goes_to_the_photo_stream_and_never_the_document_one():
    loader = LoaderProbe(PNG, name="chart.png", caption=None, content_type="image/png")
    dispatcher, bridge = fake_dispatcher(loader)
    dispatcher._send(description("photo"))
    [call] = bridge.api.calls
    assert (call["label"], call["filename"], call["caption"], call["content_type"], call["reply_to_message_id"]) == (
        "photo_stream", "chart.png", None, "image/png", None)
    assert call["message_thread_id"] == TOPIC


def test_a_whole_chat_attachment_names_no_topic_and_an_empty_caption_is_not_sent():
    dispatcher, bridge = fake_dispatcher(LoaderProbe(caption=""))
    dispatcher._send(description("document", thread=None))
    assert bridge.api.calls[0]["message_thread_id"] is None and bridge.api.calls[0]["caption"] is None


@pytest.mark.parametrize("payload", [
    {"version": 2, "payload_id": PAYLOAD_ID}, {"version": "1", "payload_id": PAYLOAD_ID}, {"version": True, "payload_id": PAYLOAD_ID},
    {"version": 1.0, "payload_id": PAYLOAD_ID}, {"payload_id": PAYLOAD_ID}, {"version": 1}, {"version": 1, "payload_id": ""},
    {"version": 1, "payload_id": 5}, {"version": 1, "payload_id": "a b"}, {"version": 1, "payload_id": "../x"},
    {"version": 1, "payload_id": "x" * 129}, {"version": 1, "payload_id": PAYLOAD_ID, "path": "/etc/passwd"},
    {"version": 1, "payload_id": PAYLOAD_ID, "filename": "x"}, ["version", 1], "version 1", 7, {}], ids=repr)
def test_only_the_closed_version_one_envelope_reaches_the_loader(payload):
    loader = LoaderProbe()
    dispatcher, bridge = fake_dispatcher(loader)
    with pytest.raises(TelegramApiError) as refused:
        dispatcher._send(description("document", payload=payload if payload != {} else {}))
    assert refused.value.kind == "refused" and refused.value.local_code == "media_unsupported" and not refused.value.uncertain
    assert loader.payloads == [] and bridge.api.calls == []


@pytest.mark.parametrize("kind", ["text", "edit", "sticker", "album", "Document", ""])
def test_an_attachment_source_with_any_other_kind_is_refused_not_reinterpreted(kind):
    loader = LoaderProbe()
    dispatcher, bridge = fake_dispatcher(loader)
    with pytest.raises(TelegramApiError) as refused:
        dispatcher._send(description(kind))
    assert refused.value.local_code == "media_unsupported" and loader.payloads == [] and bridge.api.calls == []


def test_a_photo_from_any_other_source_is_refused_and_never_sent_as_a_document():
    dispatcher, bridge = fake_dispatcher(LoaderProbe())
    for source in ("report", "unknown", "notice"):
        with pytest.raises(TelegramApiError) as refused:
            dispatcher._send(description("photo", source_kind=source))
        assert refused.value.local_code == "media_unsupported"
    assert bridge.api.calls == []


def test_a_missing_loader_is_a_refusal_before_any_request():
    dispatcher, bridge = fake_dispatcher(None)
    with pytest.raises(TelegramApiError) as refused:
        dispatcher._send(description("document"))
    assert (refused.value.kind, refused.value.local_code) == ("refused", "media_unsupported") and bridge.api.calls == []


SECRET_PATH = "/private/state/outbound-media/0d6f1a.bin"


@pytest.mark.parametrize("error", [DomainError("snapshot_corrupt", f"The sealed snapshot {SECRET_PATH} does not match its hash"),
                                   FileNotFoundError(2, "No such file", SECRET_PATH), PermissionError(13, "denied", SECRET_PATH),
                                   OSError(f"io error on {SECRET_PATH}")])
def test_expected_local_loader_failures_are_pre_send_refusals_that_never_carry_a_path(error):
    loader = LoaderProbe(enter_error=error)
    dispatcher, bridge = fake_dispatcher(loader)
    with pytest.raises(TelegramApiError) as refused:
        dispatcher._send(description("document"))
    assert (refused.value.kind, refused.value.local_code) == ("refused", "media_unavailable")
    assert not refused.value.uncertain and not refused.value.retryable_before_send and bridge.api.calls == []
    assert SECRET_PATH not in refused.value.description and "outbound-media" not in refused.value.description
    assert ("snapshot_corrupt" in refused.value.description) is isinstance(error, DomainError)


@pytest.mark.parametrize("error", [RuntimeError("bug"), TypeError("bug"), KeyError("bug"), AttributeError("bug")])
def test_a_programming_error_in_the_loader_is_not_swallowed(error):
    dispatcher, bridge = fake_dispatcher(LoaderProbe(enter_error=error))
    with pytest.raises(type(error)):
        dispatcher._send(description("document"))
    assert bridge.api.calls == []


@pytest.mark.parametrize("kind, yields", [
    ("document", ("a.pdf", io.BytesIO(b"abc"), 3, None)),                                      # four values
    ("document", ("a.pdf", io.BytesIO(b"abc"), 3, None, None, None)),
    ("document", ["a.pdf", io.BytesIO(b"abc"), 3, None, None]),                                # not a tuple
    ("document", None.__class__),                                                            # not a sequence at all
    ("document", ("a/b.pdf", io.BytesIO(b"abc"), 3, None, None)),
    ("document", ("", io.BytesIO(b"abc"), 3, None, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), 0, None, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), 50_000_001, None, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), True, None, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), 3, "x" * 1025, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), 3, 5, None)),
    ("document", ("a.pdf", io.BytesIO(b"abc"), 3, None, "bad type")),
    ("photo", ("a.png", io.BytesIO(b"abc"), 3, None, None)),
    ("photo", ("a.png", io.BytesIO(b"abc"), 3, None, "application/pdf")),
    ("photo", ("a.png", io.BytesIO(b"abc"), 10_000_001, None, "image/png")),
], ids=lambda value: value if isinstance(value, str) else "")
def test_loader_output_that_is_not_valid_metadata_for_the_mode_fails_before_any_request(kind, yields):
    loader = LoaderProbe(yields=yields)
    dispatcher, bridge = fake_dispatcher(loader)
    with pytest.raises(TelegramApiError) as refused:
        dispatcher._send(description(kind))
    assert refused.value.kind == "refused" and refused.value.local_code == "media_invalid" and bridge.api.calls == []
    assert loader.exited == 1                                   # the context is always closed


@pytest.mark.parametrize("error, kind", [
    (TelegramApiError("timeout", "slow"), "timeout"), (TelegramApiError("uncertain", "odd"), "uncertain"),
    (TelegramApiError("unreachable", "down"), "unreachable"), (TelegramApiError("refused", "no", error_code=400), "refused"),
    (TelegramApiError("rate_limited", "wait", retry_after=3.0), "rate_limited")])
def test_the_providers_classification_passes_through_unchanged_and_the_source_is_closed(error, kind):
    loader = LoaderProbe()
    api = RecordingApi()
    api.error = error
    dispatcher, _ = fake_dispatcher(loader, api)
    with pytest.raises(TelegramApiError) as raised:
        dispatcher._send(description("document"))
    assert raised.value is error and raised.value.kind == kind and (loader.entered, loader.exited) == (1, 1)


def test_a_failure_to_close_the_source_never_turns_a_delivered_message_into_an_error():
    loader = LoaderProbe(exit_error=OSError("close failed"))
    dispatcher, bridge = fake_dispatcher(loader)
    assert dispatcher._send(description("document")) is bridge.api.result
    assert loader.exited == 1


def test_a_failure_to_close_the_source_does_not_mask_a_provider_refusal():
    api = RecordingApi()
    api.error = TelegramApiError("refused", "chat not found", error_code=400)
    dispatcher, _ = fake_dispatcher(LoaderProbe(exit_error=RuntimeError("close failed")), api)
    with pytest.raises(TelegramApiError) as raised:
        dispatcher._send(description("document"))
    assert raised.value.description == "chat not found"


def test_the_text_and_edit_senders_keep_their_exact_calls():
    dispatcher, bridge = fake_dispatcher(None)
    dispatcher._send(description("text", source_kind="agent", payload={"text": "hello"}, reply=9))
    dispatcher._send({**description("edit", source_kind="agent", payload={"text": "new"}), "provider_id": 33})
    assert bridge.api.calls == [
        {"label": "message", "text": "hello", "reply_to": 9, "message_thread_id": TOPIC},
        {"label": "edit", "message_id": 33, "text": "new"}]


def test_a_document_or_an_unknown_kind_from_a_source_with_no_sender_is_refused_and_never_uploaded():
    dispatcher, bridge = fake_dispatcher(LoaderProbe())
    for kind, source in (("document", "mystery"), ("document", "report"), ("video", "report")):
        with pytest.raises(TelegramApiError) as refused:
            dispatcher._send(description(kind, source_kind=source, payload={"x": 1}))
        assert refused.value.local_code == "media_unsupported"
    assert bridge.api.calls == []


@pytest.mark.parametrize("kind", ["document", "photo"])
@pytest.mark.parametrize("mutate, confirmed", [
    (lambda r: r, True), (lambda r: {**r, "message_thread_id": 99}, False),
    (lambda r: {k: v for k, v in r.items() if k != "message_thread_id"}, False),
    (lambda r: {k: v for k, v in r.items() if k != "chat"}, False), (lambda r: {**r, "chat": {"id": -5, "type": "supergroup"}}, False),
    (lambda r: {**r, "is_topic_message": False}, False)], ids=["good", "other_topic", "no_thread", "no_chat", "other_chat", "not_topic"])
def test_a_photo_or_document_receipt_must_name_the_intended_chat_and_topic(kind, mutate, confirmed):
    result = mutate({"message_id": 5, "chat": {"id": GROUP_ID, "type": "supergroup"}, "message_thread_id": TOPIC, "is_topic_message": True})
    assert Dispatcher._confirms_destination(description(kind), result) is confirmed


def test_a_local_refusal_code_is_what_the_outbox_records_and_the_provider_code_is_unchanged():
    local = TelegramApiError("refused", "x", error_code=400, local_code="media_unavailable")
    remote = TelegramApiError("refused", "Bad Request: wrong file", error_code=400)
    assert (local.local_code, remote.local_code) == ("media_unavailable", None)


def test_bridge_construction_registers_no_media_loader_and_has_no_document_loader_map():
    bridge = TelegramBridge(SimpleNamespace(database=None, application=SimpleNamespace(), root=None, workspace=None))
    assert bridge.media_loaders == {} and not hasattr(bridge, "document_loaders")


# ===== the real bridge on a synthetic store: fairness, receipts, recovery =========================================


class WorldApi(RecordingApi):
    """Every attempt is counted; only an attempt the script lets through is delivered (``sent``, in order, with the bytes read)."""

    def __init__(self):
        super().__init__()
        self.sent, self.attempts, self.script, self.next_id = [], [], [], 600

    def _next(self, entry, thread=TOPIC):
        self.attempts.append(entry[0])
        if self.script:
            error = self.script.pop(0)
            if error is not None:
                raise error
        self.next_id += 1
        self.sent.append(entry)
        return {"message_id": self.next_id, "chat": {"id": GROUP_ID, "type": "supergroup"}, "message_thread_id": thread,
                "is_topic_message": True}

    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        return self._next(("text", text), message_thread_id)

    def send_document_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None, reply_to_message_id=None,
                             content_type=None):
        return self._next(("file", filename, stream.read(), size, caption, message_thread_id, reply_to_message_id, content_type),
                          message_thread_id)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path):
    from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
    from bazarlyk.service.changes import ChangeFeed
    from instance_support import synthetic_paths

    paths = synthetic_paths(tmp_path / "instance")
    database = initialize_empty_target(paths, host_assembly(ChangeFeed()))
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": OWNER_PRINCIPAL, "kind": "human", "display_name": "Owner"})
        uow.repository("principals").insert({"id": AGENT, "kind": "agent", "display_name": "Ordubashy"})
        uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": OWNER_PRINCIPAL})
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "bot-token"
    token.write_text(TOKEN + "\n")
    os.chmod(token, 0o600)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config_path = config_dir / "telegram.json"
    config_path.write_text(json.dumps({
        "version": 1, "enabled": True, "bot_token_file": str(token), "owner_telegram_user_id": OWNER_TG, "expected_bot_id": BOT_ID,
        "expected_installation_id": INSTALLATION, "expected_owner_principal_id": OWNER_PRINCIPAL, "dashboard_origin": "http://qorqut:1071",
        "activation_at": (START - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "chats": {name: {"chat_id": GROUP_ID, "type": "supergroup", "message_thread_id": topic,
                         **({"agent_id": AGENT} if name == "ordubashy" else {})} for name, topic in CHAT.items()}}))
    os.chmod(config_path, 0o600)
    clock, api = Clock(), WorldApi()
    runtime = SimpleNamespace(database=database, application=SimpleNamespace(ensure_writes_open=lambda uow: None, writer_admission=None),
                              root=tmp_path / "root", workspace=tmp_path / "workspace")
    bridge = TelegramBridge(runtime, config_path=str(config_path), environ={}, api_factory=lambda config, secret: api, clock=clock,
                            sleep=lambda seconds: None, rng=random.Random(1), state_dir=tmp_path / "state")
    bridge.load_config()
    with database.transaction() as uow:
        store.ensure_state(uow, bridge.config, uow.repository("installation").get("service"), now=bridge.now())
    bridge.api = api
    bridge._ready.set()
    files = {}

    @contextmanager
    def loader(payload):
        data = files[payload["payload_id"]]
        yield (f"{payload['payload_id']}.bin", io.BytesIO(data), len(data), "cap", "application/octet-stream")

    bridge.media_loaders["agent_file"] = loader
    host = SimpleNamespace(bridge=bridge, api=api, clock=clock, database=database, files=files, runtime=runtime)
    yield host
    bridge.stop(timeout=5)
    database.close()


def rows(world, sql, *params):
    with world.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, params)]


def queue_file(world, key, *, kind="document", reply_to=None, data=None):
    bridge = world.bridge
    world.files[key] = data if data is not None else key.encode() * 3
    chat = bridge.config.chat("ordubashy")
    with bridge.write("test.file") as uow:
        message = store.insert_message(uow, direction="outbound", chat=chat, purpose="update", body="cap", now=bridge.now(), agent_id=AGENT,
                                       reply_to_id=reply_to["id"] if reply_to else None,
                                       reply_to_telegram_id=reply_to["telegram_message_id"] if reply_to else None)
        row, _ = store.enqueue(uow, chat, 1, bridge.config.binding_fingerprint(), kind=kind, source_kind="agent_file", source_key=key,
                               now=bridge.now(), message_id=message["id"], agent_id=AGENT, payload={"version": 1, "payload_id": key})
    world.clock.advance(1)
    return row


def queue_text(world, key):
    bridge = world.bridge
    with bridge.write("test.text") as uow:
        enqueue_text(bridge, uow, bridge.config.chat("ordubashy"), text=key, purpose="update", source_kind="agent", source_key=key,
                     agent_id=AGENT)
    world.clock.advance(1)


def outbox_states(world):
    return {r["source_key"]: (r["state"], r["error_code"], r["attempts"]) for r in rows(world, "SELECT * FROM telegram_outbox")}


def test_other_queues_are_never_starved_by_more_than_twenty_older_uploads(world):
    for index in range(25):
        queue_file(world, f"file{index:02d}")
    queue_text(world, "urgent text")
    queue_text(world, "middle text")
    queue_text(world, "second text")
    with world.database.read() as uow:
        now = world.bridge.now()
        assert [r["source_key"] for r in store.due_outbox(uow, now, limit=20)] == [f"file{i:02d}" for i in range(20)]    # the old behavior
        assert [r["source_key"] for r in store.due_outbox(uow, now, limit=20, uploads=False)] == ["urgent text", "middle text", "second text"]
        assert [r["source_key"] for r in store.due_outbox(uow, now, limit=3, uploads=True)] == ["file00", "file01", "file02"]
    assert world.bridge.dispatcher.run() is True
    kinds = [item[0] for item in world.api.sent]
    assert kinds == ["text", "text", "text", "file"], world.api.sent                # all other work first, then exactly one upload
    assert world.api.sent[3][1] == "file00.bin"
    assert outbox_states(world)["file01"][0] == "queued"


def test_each_pass_sends_at_most_one_upload_oldest_first_and_a_backlog_drains_one_by_one(world):
    for index in range(3):
        queue_file(world, f"f{index}")
    for expected in range(1, 4):
        assert world.bridge.dispatcher.run() is True
        assert [item[1] for item in world.api.sent if item[0] == "file"] == [f"f{i}.bin" for i in range(expected)]
    assert world.bridge.dispatcher.run() is False and len([i for i in world.api.sent if i[0] == "file"]) == 3


def test_text_queued_later_is_still_sent_first_in_the_same_pass_as_an_upload(world):
    queue_file(world, "early")
    queue_text(world, "later text")
    world.bridge.dispatcher.run()
    assert [item[0] for item in world.api.sent] == ["text", "file"]


def test_an_idle_pass_takes_no_write_unit_for_the_upload_step(world, monkeypatch):
    units = []
    original = world.bridge.write
    monkeypatch.setattr(world.bridge, "write", lambda operation: (units.append(operation), original(operation))[1])
    world.bridge.dispatcher.run()
    assert units.count("dispatch.claim") == 1                    # only the non-upload claim; the upload peek is read-only


def test_a_stop_in_progress_starts_no_new_upload(world):
    queue_file(world, "held")
    world.bridge._stop.set()
    assert world.bridge.dispatcher.run() is False and world.api.sent == [] and outbox_states(world)["held"][0] == "queued"
    world.bridge._stop.clear()
    assert world.bridge.dispatcher.run() is True and [i[0] for i in world.api.sent] == ["file"]


def test_a_delivered_attachment_records_its_receipt_and_message_like_any_other_send(world):
    queue_file(world, "doc1", data=b"payload-bytes")
    world.bridge.dispatcher.run()
    [sent] = world.api.sent
    assert sent == ("file", "doc1.bin", b"payload-bytes", 13, "cap", TOPIC, None, "application/octet-stream")
    [outbox] = rows(world, "SELECT * FROM telegram_outbox")
    assert (outbox["state"], outbox["provider_message_id"], outbox["attempts"]) == ("sent", world.api.next_id, 1)
    [message] = rows(world, "SELECT * FROM telegram_messages")
    assert message["telegram_message_id"] == world.api.next_id and message["outbox_id"] == outbox["id"]


def test_an_attachment_replies_only_to_the_exact_frozen_provider_message_in_its_own_topic(world):
    queue_text(world, "question")
    world.bridge.dispatcher.run()
    with world.database.read() as uow:
        parent = dict(uow.connection.execute("SELECT * FROM telegram_messages WHERE body='question'").fetchone())
    queue_file(world, "answer", reply_to=parent)
    world.bridge.dispatcher.run()
    assert world.api.sent[-1][5:7] == (TOPIC, parent["telegram_message_id"])
    other = dict(parent, id="other", chat_name="signoffs", message_thread_id=14)
    with world.bridge.write("test") as uow:
        uow.repository("telegram_messages").insert({k: other[k] for k in (
            "id", "direction", "chat_name", "chat_id", "message_thread_id", "purpose", "occurred_at", "created_at", "updated_at")}
            | {"telegram_message_id": 4242})
        foreign = store.message(uow, "other")
    row = queue_file(world, "crossed", reply_to=foreign)
    sent = len(world.api.sent)
    world.bridge.dispatcher.run()
    assert len(world.api.sent) == sent and outbox_states(world)["crossed"][:2] == ("failed", "reply_destination_mismatch") and row


def test_a_timeout_is_uncertain_and_the_upload_is_never_sent_again(world):
    queue_file(world, "t1")
    world.api.script = [TelegramApiError("timeout", "slow")]
    world.bridge.dispatcher.run()
    assert outbox_states(world)["t1"] == ("uncertain", "provider_timeout", 1)
    for _ in range(3):
        world.clock.advance(7200)
        world.bridge.dispatcher.run()
    assert world.api.attempts == ["file"] and outbox_states(world)["t1"][0] == "uncertain"
    assert [r["outbox_id"] for r in world.bridge.status()["uncertain_sends"]] == [rows(world, "SELECT id FROM telegram_outbox")[0]["id"]]


def test_an_uncertain_upload_does_not_block_the_next_attachment_or_any_text(world):
    queue_file(world, "stuck")
    queue_file(world, "next")
    queue_text(world, "text")
    world.api.script = [None, TelegramApiError("uncertain", "gateway")]   # the text goes first, then the first upload is uncertain
    world.bridge.dispatcher.run()
    world.bridge.dispatcher.run()
    states = outbox_states(world)
    assert (states["stuck"][0], states["next"][0], states["text"][0]) == ("uncertain", "sent", "sent")


@pytest.mark.parametrize("error, state, code", [
    (TelegramApiError("refused", "Bad Request: wrong type of the web page content", error_code=400), "failed", "refused_400"),
    (TelegramApiError("refused", "Request Entity Too Large", error_code=413), "failed", "refused_413"),
    (TelegramApiError("refused", "x", error_code=400, local_code="media_unavailable"), "failed", "media_unavailable"),
    (TelegramApiError("uncertain", "Bad Gateway", error_code=502), "uncertain", "provider_uncertain")])
def test_refusals_and_local_codes_close_an_attachment_without_a_resend(world, error, state, code):
    queue_file(world, "r1")
    world.api.script = [error]
    world.bridge.dispatcher.run()
    assert outbox_states(world)["r1"] == (state, code, 1)
    world.clock.advance(86400)
    world.bridge.dispatcher.run()
    assert world.api.attempts == ["file"] and world.api.sent == [] and outbox_states(world)["r1"][2] == 1


def test_only_a_proven_pre_send_failure_or_an_explicit_rate_limit_is_retried_later(world):
    queue_file(world, "retry")
    world.api.script = [TelegramApiError("unreachable", "ConnectionRefusedError"),
                        TelegramApiError("rate_limited", "Too Many Requests", error_code=429, retry_after=40.0)]
    world.bridge.dispatcher.run()
    assert outbox_states(world)["retry"][:2] == ("queued", "unreachable")
    world.bridge.dispatcher.run()
    assert world.api.attempts == ["file"] and outbox_states(world)["retry"][2] == 1     # backing off, not hammering
    world.clock.advance(3600)
    world.bridge.dispatcher.run()
    due = rows(world, "SELECT next_attempt_at FROM telegram_outbox")[0]["next_attempt_at"]
    assert outbox_states(world)["retry"][:2] == ("queued", "rate_limited") and due >= store.plus(world.bridge.now(), 39)
    world.bridge.dispatcher.run()
    assert world.api.attempts == ["file", "file"]                                       # the explicit delay is honored
    world.clock.advance(3600)
    world.bridge.dispatcher.run()
    assert outbox_states(world)["retry"][0] == "sent" and world.api.attempts == ["file"] * 3 and len(world.api.sent) == 1


@pytest.mark.parametrize("mutate", [
    lambda r: {**r, "message_thread_id": 99}, lambda r: {k: v for k, v in r.items() if k != "message_thread_id"},
    lambda r: {k: v for k, v in r.items() if k != "chat"}, lambda r: {"message_id": r["message_id"]}, lambda r: {"chat": r["chat"]},
    lambda r: {**r, "message_id": 0}, lambda r: {**r, "message_id": True}, lambda r: {**r, "message_id": "5"}], ids=[
        "other_topic", "no_thread", "no_chat", "minimal", "no_message_id", "zero_id", "bool_id", "string_id"])
def test_an_incomplete_or_misplaced_provider_message_is_uncertain_not_sent(world, mutate):
    queue_file(world, "m1")
    original = world.api._next
    world.api._next = lambda entry, thread=TOPIC: mutate(original(entry, thread))
    world.bridge.dispatcher.run()
    assert outbox_states(world)["m1"][0] == "uncertain"
    world.clock.advance(7200)
    world.bridge.dispatcher.run()
    assert world.api.attempts == ["file"]


def test_a_crash_while_an_upload_is_in_flight_recovers_as_uncertain_and_is_never_resent(world):
    queue_file(world, "crash")

    class Crash(BaseException):
        pass

    world.api.script = [Crash()]
    with pytest.raises(Crash):
        world.bridge.dispatcher.run()
    assert outbox_states(world)["crash"][0] == "dispatching"
    world.bridge.recover()
    assert outbox_states(world)["crash"][:2] == ("uncertain", "interrupted")
    world.bridge.dispatcher.run()
    assert world.api.attempts == ["file"]                                              # the one attempted send, no second


def test_a_sealed_attachment_that_cannot_be_loaded_fails_explicitly_and_other_work_continues(world):
    queue_file(world, "lost")
    queue_text(world, "still works")

    @contextmanager
    def missing(payload):
        raise DomainError("snapshot_missing", f"The sealed attachment {SECRET_PATH} is missing")
        yield

    world.bridge.media_loaders["agent_file"] = missing
    world.bridge.dispatcher.run()
    states = outbox_states(world)
    assert states["still works"][0] == "sent" and states["lost"] == ("failed", "media_unavailable", 1)
    assert world.api.attempts == ["text"]                                  # not a request was made for the lost attachment
    [stored] = rows(world, "SELECT error_message FROM telegram_outbox WHERE source_key='lost'")
    assert "snapshot_missing" in stored["error_message"] and SECRET_PATH not in stored["error_message"]


def test_a_queued_document_from_the_retired_report_source_is_refused_and_never_sent(world):
    with world.bridge.write("test.retired") as uow:
        store.enqueue(uow, world.bridge.config.chat("urgent"), 1, world.bridge.config.binding_fingerprint(), kind="document",
                      source_kind="report", source_key="p1", payload={"name": "probe"}, now=world.bridge.now())
    queue_text(world, "still works")
    world.bridge.dispatcher.run()
    states = outbox_states(world)
    assert states["p1"] == ("failed", "media_unsupported", 1) and states["still works"][0] == "sent"
    assert world.api.sent == [("text", "still works")]


def test_a_document_attachment_without_a_registered_loader_fails_before_any_request(world):
    del world.bridge.media_loaders["agent_file"]
    queue_file(world, "orphan")
    world.bridge.dispatcher.run()
    assert world.api.sent == [] and outbox_states(world)["orphan"][:2] == ("failed", "media_unsupported")


def test_an_attachment_outbox_row_with_an_open_envelope_is_refused_by_the_real_dispatcher(world):
    queue_file(world, "base")
    with world.bridge.write("test.open") as uow:
        store.enqueue(uow, world.bridge.config.chat("ordubashy"), 1, world.bridge.config.binding_fingerprint(), kind="document",
                      source_kind="agent_file", source_key="open", payload={"version": 1, "payload_id": "x", "path": "/etc/shadow"},
                      now=world.bridge.now())
    for _ in range(3):
        world.bridge.dispatcher.run()
    assert outbox_states(world)["open"][:2] == ("failed", "media_unsupported")
    assert not any("shadow" in str(item) for item in world.api.sent)


# ===== shutdown drains an in-flight upload within its deadline =====================================================


def test_stop_waits_out_an_in_flight_upload_by_its_deadline_and_starts_no_second_one(world, provider):
    server = provider(lambda request, conn: server.release.wait(10))
    queue_file(world, "inflight", data=b"x" * 200_000)
    queue_file(world, "second")
    real = HttpBotApi(Secret(TOKEN), base_url=server.url, stream_deadline=1.0)
    world.bridge.api = real
    thread = threading.Thread(target=world.bridge.dispatcher.run, name="telegram-control", daemon=True)
    world.bridge._threads = {"control": thread}
    started = time.monotonic()
    thread.start()
    while not server.requests and time.monotonic() - started < 5:
        time.sleep(0.02)
    assert server.requests and thread.is_alive()
    stop_started = time.monotonic()
    world.bridge.stop(timeout=DEFAULT_STOP_TIMEOUT)
    elapsed = time.monotonic() - started
    assert not thread.is_alive() and elapsed < 4, elapsed
    assert 0.2 < time.monotonic() - stop_started                       # stop did not cut the upload short: it drained it
    states = outbox_states(world)
    assert states["inflight"][:2] == ("uncertain", "provider_timeout") and states["second"][0] == "queued"
    assert server.connections == 1


# ===== a stop reaches every later step of the pass, through the real cycle and control loop =========================


class LoopbackApi(WorldApi):
    """Uploads cross a real loopback connection; a deletion is recorded and, until ``gate`` is set, blocks."""

    def __init__(self, real):
        super().__init__()
        self.real, self.deletes, self.gate = real, [], threading.Event()

    def send_document_stream(self, *args, **kwargs):
        return self.real.send_document_stream(*args, **kwargs)

    def delete_message(self, chat_id, message_id):
        self.deletes.append(message_id)
        self.gate.wait(3)
        return True


def with_a_pending_deletion(world, server, **timing):
    """A loopback API in place of the scripted one, and one old delivered message whose deletion is waiting."""
    api = LoopbackApi(HttpBotApi(Secret(TOKEN), base_url=server.url, **timing))
    world.bridge.api = api
    queue_text(world, "old")
    world.bridge.dispatcher.run()
    [message] = rows(world, "SELECT id FROM telegram_messages WHERE telegram_message_id IS NOT NULL")
    with world.bridge.write("test.cleanup") as uow:
        store.enqueue_cleanup(uow, message["id"], "test", now=world.bridge.now())
    return api, message["id"]


def cleanup_state(world):
    return rows(world, "SELECT state FROM telegram_cleanup_jobs")[0]["state"]


def test_without_a_stop_the_pass_goes_on_from_the_upload_to_the_cleanup(world, provider):
    server = provider()
    api, _ = with_a_pending_deletion(world, server, stream_deadline=5.0)
    api.gate.set()
    queue_file(world, "up")
    world.bridge.cycle()
    assert outbox_states(world)["up"][0] == "sent" and len(api.deletes) == 1 and cleanup_state(world) == "deleted"


def test_a_stop_during_the_upload_lets_it_finish_and_starts_no_later_step_of_the_same_pass(world, provider):
    def answer_while_stopping(request, conn):
        queue_text(world, "later text")                                  # arrives while the upload is in flight
        world.bridge._stop.set()
        ok_reply(request, conn)

    api, _ = with_a_pending_deletion(world, provider(answer_while_stopping), stream_deadline=5.0)
    queue_file(world, "up")
    queue_file(world, "later")
    world.bridge.cycle()
    states = outbox_states(world)
    assert states["up"] == ("sent", None, 1) and states["later"][0] == "queued" and states["later text"][0] == "queued"
    assert api.deletes == [] and cleanup_state(world) == "queued"


def test_a_stop_set_before_the_pass_reaches_its_first_step_sends_and_deletes_nothing(world, provider):
    server = provider()
    api, _ = with_a_pending_deletion(world, server, stream_deadline=5.0)
    queue_file(world, "up")
    queue_text(world, "text")
    world.bridge._stop.set()
    world.bridge.cycle()
    assert server.requests == [] and api.deletes == [] and outbox_states(world)["up"][0] == outbox_states(world)["text"][0] == "queued"


def test_a_stop_in_the_middle_of_the_plain_sends_finishes_the_current_one_and_leaves_the_rest_queued(world):
    for key in ("a", "b", "c"):
        queue_text(world, key)
    queue_file(world, "f")
    original = world.api.send_message

    def stopping(*args, **kwargs):
        result = original(*args, **kwargs)
        world.bridge._stop.set()
        return result

    world.api.send_message = stopping
    assert world.bridge.dispatcher.run() is True
    states = outbox_states(world)
    assert states["a"][0] == "sent" and {states[key][0] for key in ("b", "c", "f")} == {"queued"} and world.api.attempts == ["text"]
    world.bridge._stop.clear()
    world.api.send_message = original
    while world.bridge.dispatcher.run():
        pass
    assert {state for state, _, _ in outbox_states(world).values()} == {"sent"}


@pytest.mark.parametrize("answer, state, code", [
    (lambda request, conn: time.sleep(5), "uncertain", "provider_timeout"),
    (lambda request, conn: (time.sleep(0.6), ok_reply(request, conn)), "sent", None)], ids=["deadline", "slow-success"])
def test_shutdown_through_the_control_loop_records_the_active_upload_and_starts_nothing_after_it(world, provider, answer, state, code):
    server = provider(answer)
    api, _ = with_a_pending_deletion(world, server, stream_deadline=1.0)
    queue_file(world, "inflight", data=b"x" * 200_000)
    queue_file(world, "later")
    thread = threading.Thread(target=world.bridge._control_loop, name="telegram-control", daemon=True)
    world.bridge._threads = {"control": thread}
    world.bridge._stop.clear()
    started = time.monotonic()
    thread.start()
    while not server.requests and time.monotonic() - started < 5:
        time.sleep(0.02)
    assert server.requests and thread.is_alive()
    queue_text(world, "queued during the upload")
    stopping = time.monotonic()
    world.bridge.stop(timeout=DEFAULT_STOP_TIMEOUT)                     # raises TimeoutError if the loop would not end
    assert not thread.is_alive() and time.monotonic() - started < 4 and time.monotonic() - stopping > 0.1
    states = outbox_states(world)
    assert states["inflight"] == (state, code, 1)
    assert states["later"][0] == "queued" and states["queued during the upload"][0] == "queued"
    assert api.deletes == [] and cleanup_state(world) == "queued" and server.connections == 1
    assert api.attempts == ["text"]                                     # only the earlier text ever went through the scripted path
    api.gate.set()                                                      # the deferred work is intact and runs once the bridge runs again
    world.bridge._stop.clear()
    world.bridge._ready.set()                                           # stopping withdrew readiness; the bridge is "running" again
    world.bridge.cleanup.run()
    assert len(api.deletes) == 1 and cleanup_state(world) == "deleted"


def test_a_malformed_answer_to_a_real_upload_stays_with_the_row_and_the_dispatcher_never_resends(world, provider):
    server = provider(provider_error(200, {"ok": "true", "result": {"message_id": 17}}))
    world.bridge.api = HttpBotApi(Secret(TOKEN), base_url=server.url, stream_deadline=5.0)
    queue_file(world, "odd", data=b"payload" * 100)
    assert world.bridge.dispatcher.run() is True
    assert outbox_states(world)["odd"] == ("uncertain", "provider_uncertain", 1) and server.connections == 1
    for _ in range(3):
        world.clock.advance(7200)
        world.bridge.dispatcher.run()
    assert outbox_states(world)["odd"] == ("uncertain", "provider_uncertain", 1) and server.connections == 1
    [message] = rows(world, "SELECT telegram_message_id FROM telegram_messages WHERE outbox_id=(SELECT id FROM telegram_outbox WHERE source_key='odd')")
    assert message["telegram_message_id"] is None                      # the invented message_id 17 was never trusted


@pytest.mark.parametrize("status, body", [MALFORMED_ANSWERS[6], MALFORMED_ANSWERS[7], MALFORMED_ANSWERS[8], MALFORMED_ANSWERS[11]],
                         ids=["400-parameters-list", "429-parameters-text", "500-contradiction", "200-wrong-status-rate-limit"])
def test_every_kind_of_malformed_answer_leaves_the_dispatcher_running_and_the_row_uncertain(world, provider, status, body):
    server = provider(provider_error(status, body))
    world.bridge.api = HttpBotApi(Secret(TOKEN), base_url=server.url, stream_deadline=5.0)
    queue_file(world, "odd")
    world.bridge.dispatcher.run()
    world.clock.advance(7200)
    world.bridge.dispatcher.run()
    assert outbox_states(world)["odd"] == ("uncertain", "provider_uncertain", 1) and server.connections == 1
