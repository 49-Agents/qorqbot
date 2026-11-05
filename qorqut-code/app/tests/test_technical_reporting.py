"""Durable prepared-hook reporting stays bounded and exact across retries."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from dataclasses import replace

from bazarlyk.bootstrap import technical_reporting
from bazarlyk.bootstrap.technical_reporting import active, pending, record, replay
from bazarlyk.incidents import IncidentReporter, drain_spool
from test_incidents import system


def registration(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".bazarlyk").mkdir(parents=True)
    return {"registration_id": "registration-1", "workspace": str(workspace),
            "service_url": "http://testserver", "agent_id": "agent-1"}


class Client:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def post(self, route, value):
        self.calls.append((route, dict(value)))
        if self.fail:
            raise OSError("offline")
        return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}


def test_failure_survives_restart_and_recovery_clears_marker(tmp_path):
    reg = registration(tmp_path)
    offline = Client(fail=True)
    assert record(reg, "session-1", "bootstrap", "prepare", "bootstrap_unavailable",
                  retryable=True, terminal=True, client=offline)
    assert pending(reg) == 1
    assert active(reg, "bootstrap", "prepare", "session-1")

    online = Client()
    assert replay(online, reg) == 1
    assert pending(reg) == 0  # The accepted failure remains as an active marker.
    assert record(reg, "session-1", "bootstrap", "prepare", "success", recovered=True, client=online)
    assert pending(reg) == 0
    assert not active(reg, "bootstrap", "prepare", "session-1")
    assert len(online.calls) == 2
    assert online.calls[0][1]["observation"]["observation_id"] != online.calls[1][1]["observation"]["observation_id"]


def test_marker_write_failure_keeps_accepted_failure_durable(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    monkeypatch.setattr(technical_reporting, '_mark_active_locked',
                        lambda path, value: (_ for _ in ()).throw(OSError('marker unavailable')))
    assert record(reg, 'session-1', 'bootstrap', 'prepare', 'bootstrap_unavailable',
                  retryable=True, terminal=True)
    assert replay(Client(), reg) == 0
    assert pending(reg) == 1
    monkeypatch.undo()
    assert replay(Client(), reg) == 1
    assert pending(reg) == 0


def test_recovery_is_versioned_and_cannot_clear_a_newer_failure(tmp_path):
    reg = registration(tmp_path)
    offline = Client(fail=True)
    assert record(reg, "session-1", "bootstrap", "prepare", "bootstrap_unavailable",
                  retryable=True, terminal=True, client=offline)
    online = Client()
    assert record(reg, "session-1", "bootstrap", "prepare", "success",
                  recovered=True, client=None)
    # A later failure supersedes the marker while the earlier recovery waits.
    assert record(reg, "session-1", "bootstrap", "prepare", "bootstrap_unavailable",
                  retryable=True, terminal=True, client=None)
    assert replay(online, reg) == 3
    assert active(reg, "bootstrap", "prepare", "session-1")
    assert pending(reg) == 0


def test_concurrent_writes_and_malformed_head_are_bounded(tmp_path):
    reg = registration(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: record(reg, "session-1", "inbox", "watch", "inbox_unavailable",
                                       retryable=True, observed_at="2026-09-16T00:00:00+00:00"), range(32)))
    path = Path(reg["workspace"]) / ".bazarlyk/technical-reports/pending.jsonl"
    path.write_bytes(b"{malformed}\n" + path.read_bytes())
    client = Client()
    while pending(reg):
        replay(client, reg)
    assert pending(reg) == 0
    health = json.loads(path.with_name("health.json").read_text())
    assert health["malformed"] == 1


def test_expected_busy_and_validation_are_not_spooled(tmp_path):
    reg = registration(tmp_path)
    assert not record(reg, "session-1", "inbox", "watch", "busy")
    assert not record(reg, "session-1", "bootstrap", "prepare", "forbidden")
    assert pending(reg) == 0


def test_auth_failure_preserves_pending_record(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, 'session-1', 'bootstrap', 'prepare', 'bootstrap_unavailable')
    class AuthFailure(Client):
        def post(self, route, value):
            from qorqut_operations.contracts import DomainError
            raise DomainError('invalid_credential', 'credential repair required')
    assert replay(AuthFailure(), reg) == 0
    assert pending(reg) == 1


def test_watcher_attempt_count_reaches_reportable_threshold_after_reconnects(system):
    reporter = system.reporter
    first = reporter.observation(component='inbox', operation='watch', error_code='bootstrap_unavailable',
        call_site='inbox.watcher.watch', retryable=True, attempts=1, agent_id=system.contexts[0].principal_id,
        business_id=system.business, session_id='session-1')
    second = replace(first, observation_id='00000000-0000-4000-8000-000000000005',
        observed_at='2026-09-15T00:20:00+00:00', attempts=5)
    reporter.observe(first); system.drain()
    reporter.observe(second); system.drain()
    with system.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM dev_tickets').fetchone()[0] == 1


def test_restart_recovery_is_limited_to_the_native_session(system):
    for session_id in ("session-a", "session-b"):
        failure = system.reporter.observation(component="bootstrap", operation="prepare",
            error_code="internal_error", call_site="bootstrap.handle_hook", terminal=True,
            agent_id=system.contexts[0].principal_id, business_id=system.business,
            session_id=session_id)
        system.reporter.observe(failure)
    system.drain()
    replacement = IncidentReporter(system.reporter.spool_dir, clock=system.reporter.clock)
    replacement.load_active(system.db)
    recovery = replacement.observation(component="bootstrap", operation="prepare",
        error_code="success", call_site="bootstrap.handle_hook", recovered=True,
        agent_id=system.contexts[0].principal_id, business_id=system.business,
        session_id="session-a")
    assert replacement.observe(recovery)["status"] == "queued"
    drain_spool(replacement, system.db, reporter_id=system.reporter_id)
    with system.db.read() as uow:
        rows = uow.connection.execute("SELECT session_id,recovered_at FROM system_incident_impacts ORDER BY session_id").fetchall()
    assert rows[0][0] == "session-a" and rows[0][1] is not None
    assert rows[1][0] == "session-b" and rows[1][1] is None


def test_standalone_replay_quarantines_validation_head_and_delivers_later(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    reg = registration(tmp_path)
    credential = Path(reg['workspace']) / '.bazarlyk' / 'credential'
    credential.write_text('a' * 48)
    credential.chmod(0o600)
    assert record(reg, 'session-1', 'bootstrap', 'prepare', 'bootstrap_unavailable')
    assert record(reg, 'session-1', 'inbox', 'watch', 'inbox_unavailable')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            self.send_response(422 if len(calls) == 1 else 200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if len(calls) == 1:
                self.wfile.write(b'{"status":"error","error":{"code":"validation_error"}}')
            else:
                self.wfile.write(json.dumps({'status':'ok', 'data': {
                    'status':'accepted', 'observation_id': payload['observation']['observation_id']}}).encode())
        def log_message(self, *_):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reg['service_url'] = 'http://127.0.0.1:' + str(server.server_port)
        from bazarlyk.bootstrap.technical_reporting import replay_http
        assert replay_http(reg) == 2
        assert pending(reg) == 0
        assert len(calls) == 2
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()


# --- Durable, coalesced recovery and progress despite rejected/full outboxes ---

import threading
import uuid
from datetime import datetime, timezone

import pytest

from bazarlyk.bootstrap.technical_reporting import diagnostics
from qorqut_operations.contracts import DomainError


def outbox(reg):
    return Path(reg["workspace"]) / ".bazarlyk/technical-reports/pending.jsonl"


def rows(reg, name="pending.jsonl"):
    path = outbox(reg).with_name(name)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def health(reg):
    path = outbox(reg).with_name("health.json")
    return json.loads(path.read_text()) if path.exists() else {}


def legacy(reg, session, component, operation, *, recovered=False, recovery_for=None, observed=None):
    """A row shaped exactly like the pre-coalescing writer produced it."""
    value = technical_reporting._base(reg, session, component, operation, "success" if recovered else "internal_error",
        call_site=f"{component}.{operation}" if component != "inbox" else "inbox.watcher." + operation,
        terminal=not recovered, recovered=recovered, observed_at=observed)
    if recovery_for is not None:
        value["recovery_for"] = recovery_for
    return value


def seed(reg, values):
    path = outbox(reg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(v, sort_keys=True, separators=(",", ":")).encode() + b"\n" for v in values))
    return path


class Rejecting:
    """Service double answering with one fixed domain error for chosen sessions."""

    def __init__(self, error, *, sessions=None):
        self.error, self.sessions, self.calls = error, sessions, []

    def post(self, route, value):
        self.calls.append(value)
        if self.sessions is None or value["session_id"] in self.sessions:
            raise self.error
        return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}


FOREIGN = DomainError("forbidden", "x", details={"reason": "technical_session_mismatch"})


def test_recovery_identity_is_deterministic_and_one_per_failure_episode(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    failure = rows(reg)[0]
    down = Rejecting(FOREIGN)  # the service keeps refusing, so the marker stays active
    for _ in range(6):
        assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True,
                      client=Client(fail=True))
    queued = [row for row in rows(reg) if row["recovered"]]
    assert len(queued) == 1 and queued[0]["recovery_for"] == failure["observation_id"]
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "qorqut/technical-recovery/" + json.dumps(
        [reg["registration_id"], "s", "inbox", "watch", failure["observation_id"]], separators=(",", ":"))))
    assert queued[0]["observation_id"] == expected
    assert active(reg, "inbox", "watch", "s")
    online = Client()
    assert replay(online, reg) == 2 and pending(reg) == 0
    assert not active(reg, "inbox", "watch", "s")
    assert [call["observation"]["observation_id"] for _, call in online.calls][1] == expected


def test_retrying_a_queued_recovery_still_attempts_bounded_replay(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    online = Client()
    # The recovery is already queued: this retry neither duplicates nor skips delivery.
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True, client=online)
    assert len(online.calls) == 2 and pending(reg) == 0
    assert not active(reg, "inbox", "watch", "s")


def test_concurrent_hook_and_watcher_recoveries_cannot_duplicate(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: record(reg, "s", "inbox", "watch", "success",
            call_site="inbox.watcher.watch", recovered=True), range(32)))
    assert all(results)
    assert len([row for row in rows(reg) if row["recovered"]]) == 1


def test_restart_lost_ack_and_crash_before_marker_clear_converge_on_one_receipt(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert record(reg, "s", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    assert record(reg, "s", "bootstrap", "prepare", "success", recovered=True)
    seen = []

    class Tracking(Client):
        def post(self, route, value):
            seen.append(value["observation"]["observation_id"])
            return super().post(route, value)

    class LostAck(Tracking):
        def post(self, route, value):
            result = super().post(route, value)
            if len(seen) == 2:
                raise OSError("recovery acknowledgement lost")
            return result

    # All state is on disk, so each call below models a fresh helper process.
    assert replay(LostAck(), reg) == 1  # the failure is accepted, the recovery's ack is lost
    assert pending(reg) == 1 and active(reg, "bootstrap", "prepare", "s")
    monkeypatch.setattr(technical_reporting, "_clear_active", lambda *args: None)
    assert replay(Tracking(), reg) == 1 and pending(reg) == 0  # crash after the ack, before the marker clear
    assert active(reg, "bootstrap", "prepare", "s")
    monkeypatch.undo()
    assert record(reg, "s", "bootstrap", "prepare", "success", recovered=True, client=Tracking())
    assert not active(reg, "bootstrap", "prepare", "s") and pending(reg) == 0
    # One failure receipt and one recovery receipt, re-presented under the same identity each time.
    assert len(seen) == 4 and len(set(seen)) == 2 and seen[1] == seen[2] == seen[3]


def test_recovery_still_coalesces_to_the_queued_failure_when_marker_storage_is_down(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    real = technical_reporting._mark_active_locked
    monkeypatch.setattr(technical_reporting, "_mark_active_locked",
                        lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert active(reg, "inbox", "watch", "s")  # found through the queued failure, not a marker
    for _ in range(5):
        assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    queued = rows(reg)
    assert [row["recovered"] for row in queued] == [False, True]
    assert queued[1]["recovery_for"] == queued[0]["observation_id"]
    monkeypatch.setattr(technical_reporting, "_mark_active_locked", real)
    assert replay(Client(), reg) == 2 and pending(reg) == 0
    assert not active(reg, "inbox", "watch", "s")


def test_recovery_is_queued_when_the_outbox_file_is_absent_and_invalid_twins_do_not_shadow_it(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    failure = rows(reg)[0]
    outbox(reg).unlink()  # the marker outlives an externally removed outbox
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    [queued] = rows(reg)
    assert queued["recovered"] and queued["recovery_for"] == failure["observation_id"]
    # An invalid row for the same episode is discarded by replay, so it cannot count as queued.
    bad = dict(queued, observation_id=str(uuid.uuid4()), attempts="many")
    seed(reg, [bad])
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    assert [row["observation_id"] for row in rows(reg)] == [bad["observation_id"], queued["observation_id"]]


def test_failure_replays_even_when_its_marker_cannot_be_written(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    monkeypatch.setattr(technical_reporting, "_mark_active_locked",
                        lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
    online = Client()
    assert record(reg, "s", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True, client=online)
    assert online.calls, "a marker failure must not skip the bounded replay of the backlog"
    # The earlier backlog item (its marker exists) drained; the new failure stays queued until a marker can be kept.
    assert [(row["component"], row["operation"]) for row in rows(reg)] == [("bootstrap", "prepare")]


def test_bootstrap_client_keeps_only_the_safe_reason_of_a_service_error(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from bazarlyk.bootstrap.client import BootstrapClient
    credential = tmp_path / "credential"
    credential.write_text("a" * 48)
    credential.chmod(0o600)
    answers = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "error": {
                "code": "forbidden", "message": "refused", "details": answers[0]}}).encode())
        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = BootstrapClient({"workspace": str(tmp_path), "credential_file": "credential",
                                  "service_url": "http://127.0.0.1:" + str(server.server_port)})
        for details, expected in [({"reason": "technical_session_mismatch", "principal": "other", "url": "http://x"},
                                   {"reason": "technical_session_mismatch"}),
                                  ({"reason": "Not A Safe Reason"}, None), ({"principal": "other"}, None), ("text", None)]:
            answers[:] = [details]
            with pytest.raises(DomainError) as caught:
                client.post("/api/agent/technical-observation", {})
            assert caught.value.code == "forbidden"
            assert (dict(caught.value.details) if caught.value.details is not None else None) == expected
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()


def test_old_recovery_ack_cannot_clear_a_newer_failure_and_episodes_stay_distinct(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    queued = rows(reg)
    assert [row["recovered"] for row in queued] == [False, True, False, True]
    assert queued[1]["recovery_for"] == queued[0]["observation_id"]
    assert queued[3]["recovery_for"] == queued[2]["observation_id"]
    assert queued[1]["observation_id"] != queued[3]["observation_id"]


def test_legacy_duplicate_recoveries_collapse_only_for_the_exact_episode(tmp_path):
    reg = registration(tmp_path)
    f1 = legacy(reg, "a", "inbox", "watch")
    f2 = legacy(reg, "a", "inbox", "watch")  # a newer failure of the same operation
    g = legacy(reg, "b", "inbox", "watch")   # a different session
    h = legacy(reg, "a", "bootstrap", "prepare")
    r1 = [legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for=f1["observation_id"]) for _ in range(4)]
    r2 = [legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for=f2["observation_id"]) for _ in range(3)]
    rg = [legacy(reg, "b", "inbox", "watch", recovered=True, recovery_for=g["observation_id"]) for _ in range(2)]
    rh = [legacy(reg, "a", "bootstrap", "prepare", recovered=True, recovery_for=h["observation_id"]) for _ in range(2)]
    unknown = [legacy(reg, "a", "inbox", "watch", recovered=True) for _ in range(2)]
    empty = [legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for="") for _ in range(2)]
    order = [f1, r1[0], r1[1], f2, r2[0], g, r1[2], rg[0], r2[1], h, rh[0], unknown[0], rg[1], r1[3], r2[2],
             rh[1], unknown[1], empty[0], empty[1]]
    seed(reg, order)
    keep = [f1, r1[0], f2, r2[0], g, rg[0], h, rh[0], unknown[0], unknown[1], empty[0], empty[1]]
    assert technical_reporting._compact(outbox(reg)) == len(order) - len(keep)
    assert [row["observation_id"] for row in rows(reg)] == [row["observation_id"] for row in keep]
    assert technical_reporting._compact(outbox(reg)) == 0
    assert health(reg)["coalesced"] == len(order) - len(keep)
    assert oct(outbox(reg).stat().st_mode & 0o777) == "0o600"


def test_compaction_keeps_the_only_valid_recovery_and_ignores_malformed_rows(tmp_path):
    reg = registration(tmp_path)
    failure = legacy(reg, "a", "inbox", "watch")
    bad = legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for=failure["observation_id"])
    bad["attempts"] = "many"  # invalid: replay would discard it, so it must not shadow a valid twin
    good = legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for=failure["observation_id"])
    seed(reg, [failure, bad, good])
    assert technical_reporting._compact(outbox(reg)) == 0
    assert [row["observation_id"] for row in rows(reg)] == [failure["observation_id"], bad["observation_id"], good["observation_id"]]


def test_full_outbox_replays_compacts_and_retries_the_same_observation_once(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    failure = legacy(reg, "a", "inbox", "watch")
    duplicates = [legacy(reg, "a", "inbox", "watch", recovered=True, recovery_for=failure["observation_id"])
                  for _ in range(40)]
    path = seed(reg, [failure, *duplicates])
    monkeypatch.setattr(technical_reporting, "MAX_OUTBOX_BYTES", path.stat().st_size)
    attempts = []
    real = technical_reporting._append
    monkeypatch.setattr(technical_reporting, "_append", lambda target, value: attempts.append(dict(value)) or real(target, value))
    healthy = Client()
    assert record(reg, "s", "bootstrap", "prepare", "internal_error", terminal=True, client=healthy,
                  call_site="bootstrap.handle_hook", observed_at="2026-09-30T01:00:00+00:00")
    assert len(attempts) == 2 and attempts[0] == attempts[1]  # identical identity and timestamp, exactly one retry
    assert healthy.calls, "a full queue must not stop replay"
    assert any(row["session_id"] == "s" for row in rows(reg))
    assert health(reg).get("dropped", 0) == 0


def test_unrecoverable_full_outbox_is_reported_as_queue_full_not_a_raw_count(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    failures = [legacy(reg, f"s{n}", "inbox", "watch") for n in range(20)]
    path = seed(reg, failures)
    monkeypatch.setattr(technical_reporting, "MAX_OUTBOX_BYTES", path.stat().st_size)
    offline = Client(fail=True)
    assert not record(reg, "new", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch",
                      retryable=True, client=offline)
    assert offline.calls, "bounded replay is attempted even though the append was refused"
    assert len(rows(reg)) == 20
    assert health(reg)["dropped"] == 1 and health(reg)["queue_full"] == 1
    report = diagnostics(reg)
    assert report["status"] == "degraded" and "queue_full" in report["degraded"]
    assert report["pending"]["full"] is True and report["history"]["queue_full"] == 1


def test_rejected_head_is_quarantined_with_evidence_and_later_records_progress(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "foreign", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    assert record(reg, "healthy", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    original = rows(reg)[0]
    client = Rejecting(FOREIGN, sessions={"foreign"})
    assert replay(client, reg) == 2 and pending(reg) == 0
    assert len(client.calls) == 2
    [entry] = rows(reg, "rejected.jsonl")
    assert entry["record"] == original and entry["reason"] == "technical_session_mismatch" and entry["code"] == "forbidden"
    assert entry["record_sha256"] and health(reg)["rejected"] == 1
    # Quarantine retires the rejected failure's local marker; nothing is resolved on the service.
    assert not active(reg, "bootstrap", "prepare", "foreign") and active(reg, "bootstrap", "prepare", "healthy")
    assert replay(client, reg) == 0 and len(rows(reg, "rejected.jsonl")) == 1
    assert oct(outbox(reg).with_name("rejected.jsonl").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("error", [
    DomainError("forbidden", "generic"),
    DomainError("forbidden", "other", details={"reason": "something_else"}),
    DomainError("unauthenticated", "expired"), DomainError("invalid_credential", "bad"),
    DomainError("missing_credential", "none"), DomainError("maintenance", "later", retryable=True),
    DomainError("service_unavailable", "later", retryable=True), OSError("offline"),
])
def test_endpoint_wide_or_transient_failures_keep_every_record_pending(tmp_path, error):
    reg = registration(tmp_path)
    for session in ("one", "two"):
        assert record(reg, session, "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    client = Rejecting(error)
    assert replay(client, reg) == 0 and pending(reg) == 2
    assert len(client.calls) == 1, "the attempt stops at the first endpoint-wide failure"
    assert not outbox(reg).with_name("rejected.jsonl").exists()
    assert health(reg).get("rejected", 0) == 0


def test_schema_rejection_is_quarantined_not_dropped(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    original = rows(reg)[0]
    assert replay(Rejecting(DomainError("validation_error", "bad")), reg) == 1 and pending(reg) == 0
    [entry] = rows(reg, "rejected.jsonl")
    assert entry["record"] == original and entry["reason"] == "validation_error"


def test_quarantine_failure_keeps_the_record_and_surfaces_local_degradation(tmp_path):
    reg = registration(tmp_path)
    for session in ("foreign", "healthy"):
        assert record(reg, session, "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    outbox(reg).with_name("rejected.jsonl").mkdir()  # durable quarantine is impossible
    client = Rejecting(FOREIGN, sessions={"foreign"})
    assert replay(client, reg) == 1  # only the healthy later record progressed
    assert [row["session_id"] for row in rows(reg)] == ["foreign"]
    assert health(reg)["quarantine_failed"] == 1 and health(reg)["quarantine_blocked"] == 1
    report = diagnostics(reg)
    assert report["status"] == "degraded" and "quarantine_failed" in report["degraded"]
    assert active(reg, "bootstrap", "prepare", "foreign")  # never claimed to be handled
    # Once quarantine works again the same record is retained exactly once and the state clears.
    outbox(reg).with_name("rejected.jsonl").rmdir()
    assert replay(client, reg) == 1 and pending(reg) == 0
    assert health(reg)["quarantine_blocked"] == 0 and len(rows(reg, "rejected.jsonl")) == 1


def test_full_rejected_file_retains_pending_and_never_overwrites(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    for session in ("first", "second"):
        assert record(reg, session, "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    client = Rejecting(FOREIGN)
    monkeypatch.setattr(technical_reporting, "MAX_REJECTED_BYTES", 700)
    assert replay(client, reg) == 1  # one fits; the second would exceed the cap
    kept = rows(reg, "rejected.jsonl")
    assert len(kept) == 1 and pending(reg) == 1
    assert health(reg)["quarantine_failed"] == 1
    assert diagnostics(reg)["rejected"]["full"] is True
    # The record that could not be quarantined keeps its marker; only the durable one retired its own.
    assert active(reg, "bootstrap", "prepare", "second") and not active(reg, "bootstrap", "prepare", "first")
    assert replay(client, reg) == 0 and rows(reg, "rejected.jsonl") == kept  # no overwrite, no silent drop


def test_lost_pending_ack_after_quarantine_is_not_quarantined_twice(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert record(reg, "foreign", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    real = technical_reporting._ack
    monkeypatch.setattr(technical_reporting, "_ack", lambda *args: None)  # crash after the evidence was stored
    assert replay(Rejecting(FOREIGN), reg) == 1 and pending(reg) == 1
    monkeypatch.setattr(technical_reporting, "_ack", real)
    assert replay(Rejecting(FOREIGN), reg) == 1 and pending(reg) == 0
    assert len(rows(reg, "rejected.jsonl")) == 1 and health(reg)["rejected"] == 1


def test_diagnostics_are_read_only_bounded_and_carry_no_body_or_secret(tmp_path):
    reg = registration(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    empty = diagnostics(reg)
    assert sorted(tmp_path.rglob("*")) == before, "an absent outbox must stay absent"
    assert empty["status"] == "ok" and empty["pending"]["count"] == 0
    (Path(reg["workspace"]) / ".bazarlyk" / "credential").write_text("s3cret-" * 8)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    snapshot = {p: p.read_bytes() for p in Path(reg["workspace"]).rglob("*") if p.is_file() and p.name != "pending.jsonl.lock"}
    report = diagnostics(reg)
    assert {p: p.read_bytes() for p in Path(reg["workspace"]).rglob("*") if p.is_file() and p.name != "pending.jsonl.lock"} == snapshot
    assert report["pending"]["count"] == 2 and report["pending"]["failures"] == 1
    assert report["pending"]["recoveries"] == 1 and report["pending"]["recovery_episodes"] == 1
    assert report["pending"]["max_bytes"] == technical_reporting.MAX_OUTBOX_BYTES
    assert report["rejected"] == {"count": 0, "pending_review": False, "bytes": 0,
                                  "max_bytes": technical_reporting.MAX_REJECTED_BYTES, "full": False}
    assert [marker["operation"] for marker in report["active_markers"]] == ["watch"]
    text = json.dumps(report)
    assert "s3cret" not in text and "inbox_unavailable" not in text and reg["service_url"] not in text


def test_diagnostic_cli_runs_from_the_prepared_standalone_helper(tmp_path):
    import subprocess, sys
    reg = registration(tmp_path)
    registration_file = Path(reg["workspace"]) / ".bazarlyk/registration.json"
    registration_file.write_text(json.dumps(reg))
    helper = Path(reg["workspace"]) / ".bazarlyk/technical_reporting.py"
    helper.write_bytes(Path(technical_reporting.__file__).read_bytes())
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    done = subprocess.run([sys.executable, "-I", str(helper), "--registration", str(registration_file)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["pending"]["failures"] == 1
    missing = subprocess.run([sys.executable, "-I", str(helper), "--registration", str(tmp_path / "none.json")],
                             capture_output=True, text=True, timeout=30)
    assert missing.returncode == 1 and "Traceback" not in missing.stderr


def test_standalone_replay_distinguishes_per_record_reasons_from_endpoint_failures(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    reg = registration(tmp_path)
    credential = Path(reg['workspace']) / '.bazarlyk' / 'credential'
    credential.write_text('a' * 48)
    credential.chmod(0o600)
    answers = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            status, body = answers.pop(0)(payload)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        def log_message(self, *_):
            pass
    accept = lambda payload: (200, {'status': 'ok', 'data': {'status': 'accepted', 'observation_id': payload['observation']['observation_id']}})
    refuse = lambda code, status, **extra: (lambda payload: (status, {'status': 'error', 'error': {'code': code, 'message': 'https://internal/secret', **extra}}))
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reg['service_url'] = 'http://127.0.0.1:' + str(server.server_port)
        from bazarlyk.bootstrap.technical_reporting import replay_http
        for session in ('a', 'b', 'c'):
            assert record(reg, session, 'bootstrap', 'prepare', 'bootstrap_unavailable', terminal=True)
        # Exact binding reason: quarantined and the batch continues.
        answers[:] = [refuse('forbidden', 403, details={'reason': 'technical_session_mismatch', 'principal': 'x'}), accept, accept]
        assert replay_http(reg) == 3 and pending(reg) == 0
        [entry] = rows(reg, 'rejected.jsonl')
        assert entry['reason'] == 'technical_session_mismatch' and 'secret' not in json.dumps(entry)
        # Generic forbidden, unsafe reason text, credential and server failures keep the record pending.
        for index, (code, status, extra) in enumerate([('forbidden', 403, {}),
                ('forbidden', 403, {'details': {'reason': 'Not A Safe Reason'}}),
                ('unauthenticated', 401, {}), ('service_unavailable', 503, {})]):
            session = f'keep-{index}'
            assert record(reg, session, 'bootstrap', 'prepare', 'bootstrap_unavailable', terminal=True)
            answers[:] = [refuse(code, status, **extra)]
            assert replay_http(reg) == 0, code
            assert [row['session_id'] for row in rows(reg)] == [session]
            answers[:] = [accept]
            assert replay_http(reg) == 1 and pending(reg) == 0
        assert len(rows(reg, 'rejected.jsonl')) == 1
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()


# --- Marker retirement after durable quarantine and the review-pending state ---

class RejectIds:
    """Rejects exactly the listed observation ids as per-record, accepts the rest."""

    def __init__(self, ids, error=FOREIGN):
        self.ids, self.error, self.calls = set(ids), error, []

    def post(self, route, value):
        self.calls.append(value["observation"]["observation_id"])
        if value["observation"]["observation_id"] in self.ids:
            raise self.error
        return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}


def marker_ids(reg):
    directory = outbox(reg).parent / "active"
    return [json.loads(path.read_text())["observation_id"] for path in directory.glob("*.json")] if directory.exists() else []


def test_quarantine_retires_only_its_own_marker_and_never_a_newer_failure_or_preparation_state(tmp_path):
    from bazarlyk.bootstrap import recovery
    reg = registration(tmp_path)
    recovery.record(reg, "s", "claude-code", "busy")  # an unrelated preparation blocker
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    first, old_recovery, newer = rows(reg)
    assert marker_ids(reg) == [newer["observation_id"]]
    # The older failure and its recovery are rejected per record; the newer episode is not.
    client = RejectIds({first["observation_id"], old_recovery["observation_id"]})
    assert replay(client, reg) == 3 and pending(reg) == 0
    assert [entry["record"]["observation_id"] for entry in rows(reg, "rejected.jsonl")] == [
        first["observation_id"], old_recovery["observation_id"]]
    assert marker_ids(reg) == [newer["observation_id"]] and active(reg, "inbox", "watch", "s")
    assert recovery.read(reg, "s") is not None  # preparation-recovery state is never touched


def test_quarantined_failure_and_quarantined_recovery_each_retire_their_exact_marker(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "a", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    [failure] = rows(reg)
    assert replay(RejectIds({failure["observation_id"]}), reg) == 1
    assert not active(reg, "bootstrap", "prepare", "a") and marker_ids(reg) == []
    # A failure the service accepted, then a recovery the service rejects for itself.
    assert record(reg, "b", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True, client=Client())
    assert active(reg, "bootstrap", "prepare", "b")
    assert record(reg, "b", "bootstrap", "prepare", "success", recovered=True)
    [recovery_row] = rows(reg)
    assert replay(RejectIds({recovery_row["observation_id"]}, DomainError("validation_error", "bad")), reg) == 1
    assert not active(reg, "bootstrap", "prepare", "b")
    assert [entry["reason"] for entry in rows(reg, "rejected.jsonl")] == ["technical_session_mismatch", "validation_error"]


def test_retained_rejected_evidence_stays_degraded_with_empty_pending_and_room_left(tmp_path):
    reg = registration(tmp_path)
    assert diagnostics(reg)["status"] == "ok"
    assert record(reg, "foreign", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    assert replay(Rejecting(FOREIGN), reg) == 1
    report = diagnostics(reg)
    # Nothing is pending and the rejected file is nowhere near full, yet the evidence is unreconciled.
    assert report["pending"]["count"] == 0 and report["rejected"]["full"] is False
    assert report["rejected"]["count"] == 1 and report["rejected"]["pending_review"] is True
    assert report["status"] == "degraded" and report["degraded"] == ["rejected_pending_review"]
    assert report["history"]["rejected"] == 1  # the cumulative counter is not what drives the state
    path = outbox(reg).with_name("rejected.jsonl")
    path.unlink()  # reconciliation is an operator action outside this helper
    cleared = diagnostics(reg)
    assert cleared["status"] == "ok" and cleared["rejected"]["pending_review"] is False and cleared["history"]["rejected"] == 1


# --- Episode fallback over the whole bounded outbox ---

def queue_interleaved(reg, *, marker):
    """Failures of session a at index 0 and 11 with session b between them; no readable marker."""
    older = legacy(reg, "a", "inbox", "watch")
    others = [legacy(reg, "b", "inbox", "watch") for _ in range(9)]
    wrong_part = legacy(reg, "a", "bootstrap", "prepare")  # same session, another operation
    newest = legacy(reg, "a", "inbox", "watch")
    seed(reg, [older, *others, wrong_part, newest])
    assert len(rows(reg)) > technical_reporting.MAX_REPLAY
    if marker == "corrupt":
        path = technical_reporting._marker(outbox(reg), newest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
    return older, others, newest


@pytest.mark.parametrize("marker", ["missing", "corrupt"])
def test_recovery_binds_to_the_newest_matching_failure_beyond_the_first_batch(tmp_path, marker):
    reg = registration(tmp_path)
    older, others, newest = queue_interleaved(reg, marker=marker)
    assert active(reg, "inbox", "watch", "a")
    assert record(reg, "a", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    queued = rows(reg)[-1]
    assert queued["recovered"] and queued["recovery_for"] == newest["observation_id"]
    assert queued["recovery_for"] not in (older["observation_id"], *[o["observation_id"] for o in others])
    # Repeating it coalesces into the same episode rather than binding anywhere else.
    assert record(reg, "a", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    assert len([row for row in rows(reg) if row["recovered"]]) == 1


def test_no_valid_matching_failure_leaves_the_recovery_target_unknown(tmp_path):
    reg = registration(tmp_path)
    other_session = legacy(reg, "b", "inbox", "watch")
    invalid = legacy(reg, "a", "inbox", "watch")
    invalid["attempts"] = "many"
    seed(reg, [other_session, invalid])
    assert not active(reg, "inbox", "watch", "a")
    for _ in range(2):
        assert record(reg, "a", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    recoveries = [row for row in rows(reg) if row["recovered"]]
    assert len(recoveries) == 2 and all("recovery_for" not in row for row in recoveries)
    assert len({row["observation_id"] for row in recoveries}) == 2  # unknown targets never coalesce


def test_a_concurrent_newer_failure_keeps_its_marker_through_an_older_recovery(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    real = technical_reporting._append
    raced = []
    def append(path, value):
        if value["recovered"] and not raced:
            raced.append(True)  # another process records a newer failure just before this append
            assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
        return real(path, value)
    monkeypatch.setattr(technical_reporting, "_append", append)
    assert record(reg, "s", "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    monkeypatch.setattr(technical_reporting, "_append", real)
    first, newer, recovery_row = rows(reg)
    assert recovery_row["recovery_for"] == first["observation_id"]
    assert replay(Client(), reg) == 3
    assert marker_ids(reg) == [newer["observation_id"]] and active(reg, "inbox", "watch", "s")


def test_concurrent_failures_and_recoveries_across_sessions_stay_valid_and_coalesced(tmp_path):
    reg = registration(tmp_path)
    def cycle(n):
        session = f"session-{n % 4}"
        record(reg, session, "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
        for _ in range(3):
            record(reg, session, "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(cycle, range(16)))
    queued = rows(reg)
    assert all(technical_reporting._valid(row) for row in queued)
    episodes = [technical_reporting._episode(row) for row in queued if row["recovered"]]
    assert len(episodes) == len(set(episodes))  # never two recoveries for one exact episode
    assert all(episode is not None for episode in episodes)


def test_unreadable_marker_is_exposed_by_the_diagnostics(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)
    (outbox(reg).parent / "active" / "garbage.json").write_text("{not json")
    report = diagnostics(reg)
    assert report["unreadable_markers"] == 1 and "active_marker_unreadable" in report["degraded"]
    assert len(report["active_markers"]) == 1


# --- One outbox lock orders every marker write and delete against the failure append ---

import fcntl
import os


class Gate:
    """Pauses one chosen thread, once, at an instrumented point; every other thread passes."""

    def __init__(self):
        self.thread = None
        self.used = False
        self.inside, self.release = threading.Event(), threading.Event()

    def hold(self):
        if threading.current_thread() is self.thread and not self.used:
            self.used = True
            self.inside.set()
            assert self.release.wait(10), "the gated participant was never released"


def run(function, *args, gate=None, **kwargs):
    """Run one participant on its own thread; returns (thread, box) where box gets result/error."""
    box = {}

    def target():
        try:
            box["result"] = function(*args, **kwargs)
        except BaseException as exc:  # surfaced by settle()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    if gate is not None:
        gate.thread = thread
    thread.start()
    return thread, box


def settle(*participants):
    for thread, box in participants:
        thread.join(10)
        assert not thread.is_alive(), "participant is stuck (deadlock?)"
        assert "error" not in box, box.get("error")


def fail(reg, session="s"):
    return record(reg, session, "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True)


def succeed(reg, session="s", **kwargs):
    return record(reg, session, "inbox", "watch", "success", call_site="inbox.watcher.watch", recovered=True, **kwargs)


def assert_lock_free(reg):
    """The outbox lock is not held (so no service request or rejected/health write runs inside it)."""
    descriptor = os.open(str(outbox(reg)) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # BlockingIOError if held
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("mode", ["accepted_recovery", "quarantine_retirement"])
def test_marker_retirement_is_one_locked_step_so_a_newer_failure_keeps_its_marker(tmp_path, monkeypatch, mode):
    reg = registration(tmp_path)
    assert fail(reg)
    [older] = rows(reg)
    if mode == "accepted_recovery":
        assert replay(Client(), reg) == 1  # the failure is accepted; its marker waits for the recovery
        assert succeed(reg)
        client = Client()
    else:
        client = RejectIds({older["observation_id"]})
    marker = technical_reporting._marker(outbox(reg), older)
    gate, real_unlink = Gate(), Path.unlink

    def unlink(self, *args, **kwargs):
        if self == marker:
            gate.hold()  # the old marker compared equal; the delete is next
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    retiring = run(replay, client, reg, gate=gate)
    assert gate.inside.wait(10)
    newer_failure = run(fail, reg)
    newer_failure[0].join(0.3)
    # The newer failure cannot append or install its marker inside the compare-to-unlink window.
    assert newer_failure[0].is_alive()
    gate.release.set()
    settle(retiring, newer_failure)
    monkeypatch.undo()
    assert retiring[1]["result"] == 1 and newer_failure[1]["result"] is True
    [newer] = rows(reg)
    assert newer["observation_id"] != older["observation_id"]
    assert marker_ids(reg) == [newer["observation_id"]] and active(reg, "inbox", "watch", "s")
    # The next recovery binds to the latest queued episode, not the retired one.
    assert succeed(reg)
    assert rows(reg)[-1]["recovery_for"] == newer["observation_id"]


def test_delayed_marker_install_of_an_older_failure_cannot_overwrite_a_newer_append(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    gate, real = Gate(), technical_reporting._mark_active_locked

    def mark(path, value):
        gate.hold()  # appended, marker not yet installed, outbox lock held
        real(path, value)

    monkeypatch.setattr(technical_reporting, "_mark_active_locked", mark)
    older_run = run(fail, reg, gate=gate)
    assert gate.inside.wait(10)
    newer_run = run(fail, reg)
    newer_run[0].join(0.3)
    assert newer_run[0].is_alive() and len(rows(reg)) == 1  # its append waits for the older acquisition
    gate.release.set()
    settle(older_run, newer_run)
    monkeypatch.undo()
    older, newer = rows(reg)
    assert marker_ids(reg) == [newer["observation_id"]]
    assert succeed(reg)
    assert rows(reg)[-1]["recovery_for"] == newer["observation_id"]


def test_replay_marker_repair_is_serialized_with_a_concurrent_newer_failure(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    with monkeypatch.context() as broken:
        broken.setattr(technical_reporting, "_mark_active_locked",
                       lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
        assert fail(reg)
    [first] = rows(reg)
    assert marker_ids(reg) == []
    gate, real = Gate(), technical_reporting._marker_active

    def marker_active(path, value):
        gate.hold()  # the repair is rechecking under the outbox lock
        return real(path, value)

    monkeypatch.setattr(technical_reporting, "_marker_active", marker_active)
    repairing = run(replay, Client(), reg, gate=gate)
    assert gate.inside.wait(10)
    newer_run = run(fail, reg)
    newer_run[0].join(0.3)
    assert newer_run[0].is_alive()
    gate.release.set()
    settle(repairing, newer_run)
    monkeypatch.undo()
    assert repairing[1]["result"] == 1
    [newer] = rows(reg)  # the repaired failure was accepted and acknowledged; the newer one is queued
    assert newer["observation_id"] != first["observation_id"]
    assert marker_ids(reg) == [newer["observation_id"]]


def test_replay_waits_behind_a_newer_append_and_keeps_its_marker(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    with monkeypatch.context() as broken:
        broken.setattr(technical_reporting, "_mark_active_locked",
                       lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
        assert fail(reg)
    [first] = rows(reg)
    gate, real = Gate(), technical_reporting._mark_active_locked

    def mark(path, value):
        gate.hold()  # the newer failure is appended, its marker pending, outbox lock held
        real(path, value)

    monkeypatch.setattr(technical_reporting, "_mark_active_locked", mark)
    newer_run = run(fail, reg, gate=gate)
    assert gate.inside.wait(10)
    online = Client()
    repairing = run(replay, online, reg)
    repairing[0].join(0.3)
    assert repairing[0].is_alive() and online.calls == []  # nothing is sent or repaired inside that section
    gate.release.set()
    settle(newer_run, repairing)
    monkeypatch.undo()
    ids = [call[1]["observation"]["observation_id"] for call in online.calls]
    assert ids[0] == first["observation_id"] and len(ids) == 2
    assert marker_ids(reg) == [ids[1]]  # the repair saw the newer failure's marker and left it alone


def test_replay_marker_repair_does_not_overwrite_a_newer_failure_installed_while_the_request_was_in_flight(tmp_path):
    reg = registration(tmp_path)
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(technical_reporting, "_mark_active_locked",
                       lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
        assert fail(reg)
    [first] = rows(reg)
    assert marker_ids(reg) == []

    class Interleaved:
        """While the first failure's request is in flight, another participant records a newer one."""
        calls = 0

        def post(self, route, value):
            type(self).calls += 1
            if self.calls == 1:
                assert_lock_free(reg)
                other = run(fail, reg)
                settle(other)
                assert other[1]["result"] is True
            return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}

    assert replay(Interleaved(), reg) == 1  # only the stale snapshot's failure is acknowledged
    [newer] = rows(reg)
    assert newer["observation_id"] != first["observation_id"]
    assert marker_ids(reg) == [newer["observation_id"]]  # the repair rechecked and did not overwrite
    assert succeed(reg) and rows(reg)[-1]["recovery_for"] == newer["observation_id"]


def test_stale_replay_snapshot_cannot_resurrect_a_retired_failure_marker(tmp_path):
    reg = registration(tmp_path)
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(technical_reporting, "_mark_active_locked",
                       lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
        assert fail(reg)

    class Retiring:
        """While the first request is in flight another participant fully retires the episode."""
        calls = 0

        def post(self, route, value):
            type(self).calls += 1
            assert_lock_free(reg)  # no service request runs while the outbox lock is held
            if self.calls == 1:
                other = run(lambda: (replay(Client(), reg), succeed(reg, client=Client())))
                settle(other)
                assert pending(reg) == 0 and marker_ids(reg) == [] and not active(reg, "inbox", "watch", "s")
            return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}

    assert replay(Retiring(), reg) == 1  # the stale snapshot's failure was accepted again (same receipt)
    assert pending(reg) == 0
    assert marker_ids(reg) == [] and not active(reg, "inbox", "watch", "s")


def test_no_service_request_runs_while_the_outbox_lock_is_held(tmp_path):
    reg = registration(tmp_path)

    class Checking:
        def __init__(self, error=None):
            self.error, self.calls = error, 0

        def post(self, route, value):
            assert_lock_free(reg)
            self.calls += 1
            if self.error and self.calls == 1:
                raise self.error
            return {"status": "accepted", "observation_id": value["observation"]["observation_id"]}

    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch",
                  retryable=True, client=Checking())
    assert succeed(reg, client=Checking())
    assert record(reg, "t", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    checking = Checking(FOREIGN)
    assert replay(checking, reg) == 1 and checking.calls == 1 and pending(reg) == 0


def test_rejected_file_and_health_writes_stay_outside_the_outbox_lock(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    real_quarantine, real_health = technical_reporting._quarantine, technical_reporting._health
    observed = []

    def quarantine(*args, **kwargs):
        assert_lock_free(reg)
        observed.append("quarantine")
        return real_quarantine(*args, **kwargs)

    def health_write(*args, **kwargs):
        assert_lock_free(reg)
        observed.append("health")
        return real_health(*args, **kwargs)

    monkeypatch.setattr(technical_reporting, "_quarantine", quarantine)
    monkeypatch.setattr(technical_reporting, "_health", health_write)
    older = legacy(reg, "s", "inbox", "watch")
    seed(reg, [older, *[legacy(reg, "s", "inbox", "watch", recovered=True, recovery_for=older["observation_id"])
                        for _ in range(3)]])
    assert replay(Rejecting(FOREIGN), reg) == 2  # three duplicate recoveries coalesce; the rest is quarantined
    assert "quarantine" in observed and "health" in observed


def test_marker_retirement_fsyncs_the_containing_directory_after_the_delete(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert fail(reg)
    [failure] = rows(reg)
    marker = technical_reporting._marker(outbox(reg), failure)
    events, real_unlink, real_fsync = [], Path.unlink, technical_reporting._fsync_dir

    def unlink(self, *args, **kwargs):
        events.append(("unlink", self))
        return real_unlink(self, *args, **kwargs)

    def fsync_dir(directory):
        events.append(("fsync", directory))
        return real_fsync(directory)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(technical_reporting, "_fsync_dir", fsync_dir)
    technical_reporting._clear_active(outbox(reg), dict(failure, recovered=True, recovery_for=failure["observation_id"]))
    assert events == [("unlink", marker), ("fsync", marker.parent)]
    assert marker_ids(reg) == []


def test_concurrent_failures_always_leave_the_marker_on_the_last_appended_failure(tmp_path):
    reg = registration(tmp_path)
    barrier = threading.Barrier(8)

    def writer(n):
        barrier.wait(10)
        for _ in range(8):
            assert fail(reg)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))
    failures = rows(reg)
    assert len(failures) == 64
    assert marker_ids(reg) == [failures[-1]["observation_id"]]
    assert succeed(reg) and rows(reg)[-1]["recovery_for"] == failures[-1]["observation_id"]


# --- Unreadable markers fall back to the queued failure; quarantine state is the present state ---

@pytest.mark.parametrize("content", ["{not json", "[]", "null", '"text"', '{"observation_id": 5}',
                                     '{"observation_id": ""}', '{"observation_id": null}', "{}"])
def test_any_unreadable_marker_falls_back_to_the_newest_queued_failure(tmp_path, content):
    reg = registration(tmp_path)
    assert fail(reg)
    [failure] = rows(reg)
    technical_reporting._marker(outbox(reg), failure).write_text(content)
    report = diagnostics(reg)
    assert report["unreadable_markers"] == 1 and "active_marker_unreadable" in report["degraded"]
    assert active(reg, "inbox", "watch", "s")
    assert succeed(reg)  # never raises, never queues a row the replay would discard
    recovery_row = rows(reg)[-1]
    assert technical_reporting._valid(recovery_row)
    assert recovery_row["recovered"] and recovery_row["recovery_for"] == failure["observation_id"]
    assert succeed(reg) and len([row for row in rows(reg) if row["recovered"]]) == 1
    assert replay(Client(), reg) == 2 and pending(reg) == 0


def test_replay_repairs_an_unreadable_marker_from_the_still_queued_failure(tmp_path):
    reg = registration(tmp_path)
    assert fail(reg)
    [failure] = rows(reg)
    technical_reporting._marker(outbox(reg), failure).write_text("[]")
    assert replay(Client(), reg) == 1 and pending(reg) == 0
    assert marker_ids(reg) == [failure["observation_id"]] and diagnostics(reg)["unreadable_markers"] == 0


def test_quarantine_blocked_clears_when_the_stuck_record_leaves_pending_another_way(tmp_path, monkeypatch):
    reg = registration(tmp_path)
    assert record(reg, "stuck", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    rejected = outbox(reg).with_name("rejected.jsonl")
    rejected.mkdir()  # durable quarantine is impossible
    assert replay(Rejecting(FOREIGN, sessions={"stuck"}), reg) == 0
    assert health(reg)["quarantine_blocked"] == 1 and "quarantine_failed" in diagnostics(reg)["degraded"]
    # The service now accepts that record. Another accepted record stays queued (its marker
    # cannot be kept), so the queue is not empty and the stale flag would otherwise show.
    with monkeypatch.context() as broken:
        broken.setattr(technical_reporting, "_mark_active_locked",
                       lambda path, value: (_ for _ in ()).throw(OSError("marker storage down")))
        assert record(reg, "later", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
        assert replay(Client(), reg) == 1
    assert [row["session_id"] for row in rows(reg)] == ["later"]
    assert health(reg)["quarantine_blocked"] == 0
    assert "quarantine_failed" not in diagnostics(reg)["degraded"]


def test_quarantine_blocked_is_kept_while_the_pass_could_not_examine_the_stuck_record(tmp_path):
    reg = registration(tmp_path)
    assert record(reg, "stuck", "bootstrap", "prepare", "bootstrap_unavailable", retryable=True, terminal=True)
    outbox(reg).with_name("rejected.jsonl").mkdir()
    assert replay(Rejecting(FOREIGN), reg) == 0 and health(reg)["quarantine_blocked"] == 1
    assert replay(Client(fail=True), reg) == 0  # the service is unreachable: nothing was examined
    assert health(reg)["quarantine_blocked"] == 1 and "quarantine_failed" in diagnostics(reg)["degraded"]
    assert replay(Rejecting(FOREIGN), reg) == 0  # still stuck, still reported
    assert health(reg)["quarantine_blocked"] == 1


def test_a_clean_replay_does_not_create_or_rewrite_the_health_file(tmp_path):
    reg = registration(tmp_path)
    assert replay(Client(), reg) == 0
    assert not outbox(reg).with_name("health.json").exists()
    assert record(reg, "s", "inbox", "watch", "inbox_unavailable", call_site="inbox.watcher.watch", retryable=True, client=Client())
    assert not outbox(reg).with_name("health.json").exists()
