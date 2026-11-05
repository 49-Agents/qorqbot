"""The local program gateway, its Python helper and the operator commands."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading

import pytest

from bazarlyk.telegram import __main__ as cli
from bazarlyk.telegram import gateway as gateway_module
from bazarlyk.telegram.client import GatewayError, notification_status, notify
from bazarlyk.telegram.config import load_config, redacted_example
from telegram_support import CHAT_IDS, Env, write_private

SECRET = "s3cret-value-for-monitor-producer-0123456789"
OTHER = "another-producer-secret-9876543210-abcdefgh"
NOTE = {"event_key": "db-down", "title": "Database unreachable", "text": "Writes fail. Fail over now.", "reason": "Data loss risk"}


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


@pytest.fixture
def world(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="tg-"))
    secret_file = write_private(tmp_path / "producer" / "monitor.secret", SECRET + "\n")
    other_file = write_private(tmp_path / "producer" / "other.secret", OTHER + "\n")
    path = sockets / "notify.sock"

    def gateway_config(producers=None, enabled=True):
        return {"program_gateway": {"enabled": enabled, "socket_path": str(path), "producers": producers if producers is not None else [
            {"producer_id": "monitor", "secret_sha256": digest(SECRET)}, {"producer_id": "other", "secret_sha256": digest(OTHER)}]}}

    env = Env(tmp_path, extra=gateway_config())
    bridge = env.ready()
    bridge._ensure_gateway()
    assert bridge.gateway.running, bridge.health()
    env.socket, env.secret_file, env.other_file, env.gateway_config = path, secret_file, other_file, gateway_config
    try:
        yield env
    finally:
        env.close()
        shutil.rmtree(sockets, ignore_errors=True)


def raw(path, payload, *, read=True, close_write=False):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(payload if isinstance(payload, bytes) else json.dumps(payload).encode() + b"\n")
        if close_write:
            connection.shutdown(socket.SHUT_WR)
        if not read:
            return None
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b"\n", 1)[0]) if data else None


def request(path, **fields):
    return raw(path, {"version": 1, "action": "notify", "secret": SECRET, **NOTE, **fields})


def test_the_socket_is_private_unix_only_and_serves_notifications_to_the_urgent_chat(world):
    env = world
    info = os.lstat(env.socket)
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env.socket.parent).st_mode) == 0o700
    assert env.bridge.gateway._server.family == socket.AF_UNIX
    first = request(env.socket)
    assert first["ok"] is True and first["result"]["action"] == "posted"
    assert request(env.socket)["result"]["action"] == "suppressed"
    env.bridge.dispatcher.run()
    assert [(m["chat_id"], m["text"].split("\n")[0]) for m in env.api.sent] == [(CHAT_IDS["urgent"], "URGENT: Database unreachable")]
    row = env.one("SELECT producer_id, source_kind FROM telegram_outbox")
    assert row == {"producer_id": "monitor", "source_kind": "notify"}
    changed = request(env.socket, state="resolved", title="Recovered", text="Writes succeed.")
    assert changed["result"]["action"] == "updated"
    env.bridge.dispatcher.run()
    assert env.api.edited[-1]["text"].startswith("RESOLVED: Recovered")


def test_status_reports_only_the_producers_own_notifications(world):
    env = world
    request(env.socket)
    env.bridge.dispatcher.run()
    status = raw(env.socket, {"action": "status", "secret": SECRET, "event_key": "db-down"})
    assert status["ok"] and status["result"]["delivery_state"] == "sent" and status["result"]["state"] == "firing"
    assert "message_id" not in json.dumps(status) and str(CHAT_IDS["urgent"]) not in json.dumps(status)
    other = raw(env.socket, {"action": "status", "secret": OTHER, "event_key": "db-down"})
    assert other["ok"] is False and other["error"]["code"] == "not_found"
    assert raw(env.socket, {"action": "status", "secret": SECRET, "event_key": "db-down", "extra": 1})["error"]["code"] == "validation_error"


@pytest.mark.parametrize("payload", [
    {"action": "notify", "secret": "wrong-secret-that-is-long-enough-1234"},
    {"action": "notify"}, {"action": "notify", "secret": 5}, {"action": "notify", "secret": "short"},
    {"action": "notify", "secret": ""},
])
def test_wrong_or_missing_credentials_are_refused_without_effect(world, payload):
    response = raw(world.socket, {"version": 1, **NOTE, **payload})
    assert response == {"ok": False, "error": {"code": "unauthenticated", "message": "The credential or peer is not accepted", "retryable": False}}
    assert world.rows("SELECT * FROM telegram_outbox") == [] and world.rows("SELECT * FROM telegram_events") == []


def test_a_peer_running_as_a_different_user_is_refused_even_with_the_right_secret(world):
    env = world
    env.write_config(extra=env.gateway_config([{"producer_id": "monitor", "secret_sha256": digest(SECRET), "peer_uid": os.geteuid() + 1}]))
    env.bridge.cycle()
    assert request(env.socket)["error"]["code"] == "unauthenticated"
    env.write_config(extra=env.gateway_config([{"producer_id": "monitor", "secret_sha256": digest(SECRET), "peer_uid": os.geteuid()}]))
    env.bridge.cycle()
    assert request(env.socket)["ok"] is True


@pytest.mark.parametrize("field,value", [
    ("producer", "other"), ("producer_id", "other"), ("chat_id", -1001), ("chat", "founder"), ("target", "signoffs"),
    ("agent_id", "x"), ("token", "x"),
])
def test_the_producer_and_the_target_can_never_be_chosen_by_the_caller(world, field, value):
    response = request(world.socket, **{field: value})
    assert response["ok"] is False and response["error"]["code"] == "validation_error"
    assert world.rows("SELECT * FROM telegram_outbox") == []


def test_malformed_oversized_and_unknown_requests_are_bounded_errors(world, monkeypatch):
    env = world
    assert raw(env.socket, b"not json\n")["error"]["code"] == "invalid_request"
    assert raw(env.socket, b"[1,2,3]\n")["error"]["code"] == "invalid_request"
    assert raw(env.socket, {"action": "delete", "secret": SECRET})["error"]["code"] == "invalid_request"
    assert request(env.socket, version=2)["error"]["code"] == "invalid_request"
    assert request(env.socket, surprise=1)["error"]["code"] == "validation_error"
    assert raw(env.socket, b"x" * (65 * 1024) + b"\n")["error"]["code"] == "payload_too_large"
    assert raw(env.socket, b"x" * (70 * 1024), close_write=True)["error"]["code"] == "payload_too_large"
    monkeypatch.setattr(gateway_module, "REQUEST_DEADLINE_SECONDS", 0.3)
    assert raw(env.socket, b'{"action":"notify"')["error"]["code"] == "timeout"  # never completed: bounded, not hung
    assert env.rows("SELECT * FROM telegram_outbox") == []
    for bad in ({"title": "t" * 121}, {"link": "https://user:pw@example.com"}, {"state": "paused"}):
        assert request(env.socket, **bad)["error"]["code"] == "validation_error"
    assert request(env.socket)["ok"] is True  # the gateway survived all of it


def test_revoking_a_credential_blocks_new_requests_and_cancels_its_queued_sends(world):
    env = world
    assert request(env.socket)["ok"] is True
    env.write_config(extra=env.gateway_config([{"producer_id": "other", "secret_sha256": digest(OTHER)}]))
    env.bridge.cycle()
    assert request(env.socket)["error"]["code"] == "unauthenticated"
    row = env.one("SELECT state, error_code FROM telegram_outbox")
    assert (row["state"], row["error_code"]) == ("cancelled", "producer_revoked") and env.api.sent == []


def test_disabling_the_gateway_removes_the_socket_and_stopping_the_bridge_removes_it_too(world):
    env = world
    env.write_config(extra=env.gateway_config(enabled=False))
    env.bridge.cycle()
    assert not env.socket.exists() and not env.bridge.gateway.running
    env.write_config(extra=env.gateway_config())
    env.bridge.cycle()
    assert env.socket.exists()
    env.bridge.stop(timeout=5)
    assert not env.socket.exists()


def test_a_stale_socket_file_is_replaced_and_a_foreign_file_degrades_only_the_gateway(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="tg-"))
    path = sockets / "notify.sock"
    config = {"program_gateway": {"enabled": True, "socket_path": str(path), "producers": [
        {"producer_id": "monitor", "secret_sha256": digest(SECRET)}]}}
    env = Env(tmp_path, extra=config)
    try:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path)); stale.close()
        bridge = env.ready()
        bridge._ensure_gateway()
        assert bridge.gateway.running and bridge.health() == {}
        bridge.gateway.stop()
        path.write_text("not a socket")
        bridge._ensure_gateway()
        assert bridge.health() == {"gateway": "invalid_configuration"} and path.read_text() == "not a socket"
        assert bridge.status()["state"] in ("degraded", "stopped")
    finally:
        env.close()
        shutil.rmtree(sockets, ignore_errors=True)


def test_the_gateway_backlog_is_bounded_and_concurrent_requests_are_serialised_safely(world):
    env = world
    results = []

    def post(n):
        results.append(request(env.socket, event_key=f"parallel-{n}"))

    threads = [threading.Thread(target=post, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(results) == 6 and all(r["ok"] for r in results)
    assert env.one("SELECT count(*) n FROM telegram_events")["n"] == 6
    for n in range(94):
        assert request(env.socket, event_key=f"fill-{n}")["ok"]
    full = request(env.socket, event_key="overflow")
    assert full["ok"] is False and full["error"] == {"code": "busy", "message": full["error"]["message"], "retryable": True}


def test_the_python_helper_and_command_line_use_the_socket_and_never_print_the_secret(world, capsys, monkeypatch):
    env = world
    result = notify(env.socket, env.secret_file, **NOTE)
    assert result["action"] == "posted"
    assert notification_status(env.socket, env.secret_file, event_key="db-down")["state"] == "firing"
    with pytest.raises(GatewayError) as failure:
        notify(env.socket, env.other_file, **{**NOTE, "title": "t" * 200})
    assert failure.value.code == "validation_error" and not failure.value.retryable
    with pytest.raises(GatewayError) as gone:
        notify(env.socket.with_name("missing.sock"), env.secret_file, **NOTE)
    assert gone.value.code == "unavailable" and gone.value.retryable
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Failover complete.\n"))
    code = cli.main(["notify", "--socket", str(env.socket), "--secret-file", str(env.secret_file), "--event-key", "cli-event",
                     "--title", "From the CLI", "--reason", "Testing", "--text-file", "-"])
    out = capsys.readouterr().out
    assert code == 0 and json.loads(out)["result"]["action"] == "posted" and SECRET not in out
    code = cli.main(["notify", "--socket", str(env.socket), "--secret-file", str(env.other_file), "--event-key", "e", "--title", "t" * 300,
                     "--reason", "r", "--text-file", "-"])
    assert code == 1 and OTHER not in capsys.readouterr().out
    wide = write_private(env.tmp / "wide" / "s", SECRET, mode=0o644)
    code = cli.main(["notify", "--socket", str(env.socket), "--secret-file", str(wide), "--event-key", "e", "--title", "t", "--reason", "r",
                     "--text-file", "-"])
    assert code == 2 and SECRET not in capsys.readouterr().out


def test_operator_commands_validate_example_and_enroll_without_revealing_secrets(world, tmp_path, capsys):
    env = world
    assert cli.main(["validate", "--config", str(env.config_path)]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["valid"] is True and report["configuration"]["program_gateway"] == {"enabled": True, "producers": 2}
    for hidden in (str(env.token_file), str(CHAT_IDS["urgent"]), SECRET, list(env.agents.values())[0]):
        assert hidden not in out
    env.token_file.write_text("garbage\n")
    assert cli.main(["validate", "--config", str(env.config_path)]) == 1
    assert "garbage" not in capsys.readouterr().out
    assert cli.main(["validate", "--config", str(tmp_path / "missing.json")]) == 1
    capsys.readouterr()
    assert cli.main(["example"]) == 0
    example = json.loads(capsys.readouterr().out)
    assert example == redacted_example() and example["enabled"] is False
    secret_file = tmp_path / "enroll" / "producer.secret"
    secret_file.parent.mkdir(mode=0o700)
    os.chmod(secret_file.parent, 0o700)
    assert cli.main(["enroll", "--producer-id", "backup", "--secret-file", str(secret_file)]) == 0
    enrolled = json.loads(capsys.readouterr().out)
    secret = secret_file.read_text().strip()
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600 and enrolled["secret_sha256"] == digest(secret)
    assert secret not in json.dumps(enrolled)
    assert cli.main(["enroll", "--producer-id", "backup", "--secret-file", str(secret_file)]) == 1  # never replaces a secret
    assert secret_file.read_text().strip() == secret and cli.main(["enroll", "--producer-id", "x", "--secret-file", "relative"]) == 2


def test_no_tcp_listener_or_proxy_surface_exists_in_the_gateway_module():
    source = Path(gateway_module.__file__).read_text()
    assert "AF_INET" not in source and "urlopen" not in source and "subprocess" not in source and "eval(" not in source


def test_a_disabled_producer_is_refused_at_the_socket_and_its_queued_sends_are_cancelled(world):
    from dataclasses import replace
    env = world
    assert request(env.socket)["ok"] is True
    config = env.bridge.config
    producers = tuple(replace(p, enabled=False) if p.producer_id == "monitor" else p for p in config.gateway.producers)
    env.bridge.config = replace(config, gateway=replace(config.gateway, producers=producers))
    assert request(env.socket, event_key="after-disable")["error"]["code"] == "unauthenticated"
    env.bridge.dispatcher.run()
    row = env.one("SELECT state, error_code FROM telegram_outbox")
    assert (row["state"], row["error_code"]) == ("cancelled", "producer_revoked") and env.api.sent == []
    assert raw(env.socket, {"version": 1, "action": "notify", "secret": OTHER, **NOTE, "event_key": "still-fine"})["ok"] is True


def test_one_producers_full_backlog_does_not_block_another(world):
    env = world
    for n in range(100):
        assert request(env.socket, event_key=f"fill-{n}")["ok"]
    assert request(env.socket, event_key="overflow")["error"]["code"] == "busy"
    other = raw(env.socket, {"version": 1, "action": "notify", "secret": OTHER, **NOTE, "event_key": "independent"})
    assert other["ok"] is True and other["result"]["action"] == "posted"


def test_the_command_line_takes_the_whole_payload_from_stdin_or_a_file_and_refuses_mixed_input(world, capsys, monkeypatch, tmp_path):
    import io
    env = world
    payload = {"event_key": "json-event", "title": "From a payload", "text": "Failover complete.", "reason": "Testing"}
    base = ["notify", "--socket", str(env.socket), "--secret-file", str(env.secret_file)]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main([*base, "--payload-file", "-"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["result"]["action"] == "posted" and SECRET not in out
    path = tmp_path / "payload.json"
    path.write_text(json.dumps({**payload, "event_key": "file-event", "state": "resolved"}))
    assert cli.main([*base, "--payload-file", str(path)]) == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    assert env.one("SELECT count(*) n FROM telegram_events WHERE event_key IN ('json-event','file-event')")["n"] == 2
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "producer": "other"})))
    assert cli.main([*base, "--payload-file", "-"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main([*base, "--payload-file", "-"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main([*base, "--payload-file", "-", "--title", "Extra flag"]) == 2
    assert cli.main([*base]) == 2
    assert OTHER not in capsys.readouterr().out


def test_an_owner_mismatch_refuses_gateway_notifications_and_a_cycle_closes_the_socket(world):
    from qorqut_operations.contracts import new_id
    env = world
    assert request(env.socket)["ok"] is True
    other = new_id()
    with env.database.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Second owner"})
        uow.connection.execute("UPDATE installation SET owner_principal_id=?", (other,))
    queued = env.one("SELECT count(*) n FROM telegram_outbox")["n"]
    refused = request(env.socket, event_key="another-event")
    assert refused["ok"] is False and refused["error"]["code"] == "owner_binding_mismatch"
    assert env.one("SELECT count(*) n FROM telegram_outbox")["n"] == queued
    env.bridge.cycle()
    assert not env.bridge._ready.is_set() and not env.bridge.gateway.running
    assert env.bridge.health() == {"binding": "owner_binding_mismatch"}
