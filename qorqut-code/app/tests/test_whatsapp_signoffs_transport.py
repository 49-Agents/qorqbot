"""The host's managed-send client against a scripted connector on a real Unix socket: signing, the private secret file, and
the one rule that matters after a send is on the wire: only an explicit ``"sent": false`` refusal proves nothing was sent."""

import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from bazarlyk.service.whatsapp_send import ManagedWhatsApp, UnknownOutcome, sign
from qorqut_operations.contracts import DomainError

SECRET = b"synthetic-managed-send-secret-0123456789"
ACCOUNT = "a" * 64


class Server:
    """Answers each request from ``script`` (a callable returning (status, body bytes) or None to drop the connection)."""

    def __init__(self, path, secret=SECRET):
        self.requests, self.script, self.secret = [], lambda route, payload: (200, b"{}"), secret
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stamp = self.headers["X-Qorqut-Timestamp"]
                valid = self.headers["X-Qorqut-Signature"] == sign(outer.secret, "POST", self.path, stamp, body)
                outer.requests.append((self.path, json.loads(body), valid, stamp))
                answer = outer.script(self.path, json.loads(body)) if valid else (401, b'{"error":"unauthorized","sent":false}')
                if answer is None:
                    self.connection.close()
                    return
                status, data = answer
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Unix(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def get_request(self):
                request, _ = super().get_request()
                return request, ("local", 0)

        self.server = Unix(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def connector(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    secret_file = tmp_path / "send.key"
    secret_file.write_bytes(SECRET + b"\n")
    os.chmod(secret_file, 0o600)
    server = Server(directory / "send.sock")
    client = ManagedWhatsApp({"socket": str(directory / "send.sock"), "secret_file": str(secret_file), "account_ref": ACCOUNT})
    client.secret_path = secret_file
    try:
        yield client, server
    finally:
        server.close()


def reply(status, **body):
    return status, json.dumps(body).encode()


def test_requests_are_signed_with_the_private_secret_and_carry_exact_bodies(connector):
    client, server = connector
    server.script = lambda route, payload: reply(200, account_ref=ACCOUNT, connected=True, managed_send=True)
    assert client.account() == {"account_ref": ACCOUNT, "connected": True}
    server.script = lambda route, payload: reply(200, found=True, receipt={"request_id": payload["request_id"]})
    assert client.send("qorqut-job-1", "friend@s.whatsapp.net", "Hi <b> &  ", ACCOUNT)["request_id"] == "qorqut-job-1"
    route, payload, valid, _ = server.requests[-1]
    assert valid and route == "/managed/send"
    assert payload == {"request_id": "qorqut-job-1", "chat_id": "friend@s.whatsapp.net", "text": "Hi <b> &  ", "account_ref": ACCOUNT}


def test_a_wrong_secret_is_refused_by_the_connector_before_anything_is_sent(connector):
    client, server = connector
    server.secret = b"another-secret-that-is-long-enough-123456"
    with pytest.raises(DomainError) as caught:
        client.send("qorqut-job-1", "friend@s.whatsapp.net", "text", ACCOUNT)
    assert caught.value.code == "operation_unavailable" and "credential" in caught.value.message


@pytest.mark.parametrize("answer", [
    reply(503, error="WhatsApp is disconnected; nothing was sent by this call", sent=False),
    reply(409, error="request_id already belongs to different text or recipient", sent=False),
    reply(400, error="invalid request", sent=False)])
def test_an_explicit_refusal_is_definitive_and_names_nothing_private(connector, answer):
    client, server = connector
    server.script = lambda route, payload: answer
    with pytest.raises(DomainError) as caught:
        client.send("qorqut-job-1", "friend@s.whatsapp.net", "secret text", ACCOUNT)
    assert "secret text" not in caught.value.message and "friend@" not in caught.value.message


@pytest.mark.parametrize("answer", [
    (503, b"send receipt unavailable\n"),                                  # the old ambiguous post-send answer
    reply(503, error="send receipt unavailable"),                          # a 503 that does not say "sent": false
    reply(500, error="send receipt unavailable", outcome="unknown"),       # the connector's post-send failure
    reply(503, error="x", sent=True), reply(503, error="x", sent=None),    # only a literal false is evidence
    (502, b""), (200, b"not json"), (200, b"[]"), reply(200, found=False),
    (503, b"{" + b"x" * 20000), None])
def test_every_other_answer_to_a_send_is_an_unknown_outcome(connector, answer):
    client, server = connector
    server.script = (lambda route, payload: answer)
    with pytest.raises(UnknownOutcome):
        client.send("qorqut-job-1", "friend@s.whatsapp.net", "text", ACCOUNT)


def test_the_same_503_on_a_read_route_is_just_unavailable(connector):
    client, server = connector
    server.script = lambda route, payload: (503, b"index query failed\n")
    with pytest.raises(DomainError) as caught:
        client.chat("friend@s.whatsapp.net")
    assert caught.value.code == "operation_unavailable"


def test_a_receipt_lookup_never_turns_into_a_send_and_distinguishes_unrecorded(connector):
    client, server = connector
    server.script = lambda route, payload: reply(200, found=False)
    assert client.receipt("qorqut-job-1", ACCOUNT) is None
    server.script = lambda route, payload: reply(200, found=True, receipt={"request_id": "qorqut-job-1", "state": "unknown"})
    assert client.receipt("qorqut-job-1", ACCOUNT)["state"] == "unknown"
    assert {route for route, *_ in server.requests} == {"/managed/receipt"}
    server.script = lambda route, payload: reply(200, found="maybe")
    with pytest.raises(DomainError):
        client.receipt("qorqut-job-1", ACCOUNT)


def test_no_connector_listening_means_nothing_was_sent(connector, tmp_path):
    client, server = connector
    server.close()
    os.unlink(client.socket)
    with pytest.raises(DomainError) as caught:
        client.send("qorqut-job-1", "friend@s.whatsapp.net", "text", ACCOUNT)
    assert caught.value.code == "operation_unavailable"
    server.thread = None


@pytest.mark.parametrize("prepare", ["loose", "short", "missing", "symlink", "group", "empty"])
def test_the_secret_file_must_be_a_private_real_file_of_sufficient_length(connector, prepare, tmp_path):
    client, server = connector
    path = client.secret_path
    if prepare == "loose":
        os.chmod(path, 0o644)
    elif prepare == "group":
        os.chmod(path, 0o640)
    elif prepare == "short":
        path.write_bytes(b"too short")
    elif prepare == "empty":
        path.write_bytes(b"")
    elif prepare == "missing":
        path.unlink()
    else:
        real = tmp_path / "real.key"
        real.write_bytes(SECRET)
        os.chmod(real, 0o600)
        path.unlink()
        path.symlink_to(real)
    with pytest.raises(DomainError) as caught:
        client.account()
    assert caught.value.code == "operation_unavailable" and "credential is unavailable" in caught.value.message
    assert str(path) not in caught.value.message and server.requests == []
