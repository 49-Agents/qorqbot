"""End-to-end durable hook reporting through the authenticated HTTP port."""

import importlib
import json
from pathlib import Path

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.bootstrap import technical_reporting
from test_service_transports import ServiceFixture


class _Offline:
    def post(self, route, value):
        raise OSError("service is offline")


class _RouteClient:
    """Adapter used by the client helper while the app remains in-process."""

    def __init__(self, http, token, *, lose_first_ack=False):
        self.http = http
        self.headers = {
            "Authorization": "Bearer " + token,
            "Origin": "http://testserver",
        }
        self.lose_first_ack = lose_first_ack
        self.calls = []

    def post(self, route, value):
        self.calls.append((route, dict(value)))
        response = self.http.post(route, headers=self.headers, json=value)
        if response.status_code != 200:
            raise AssertionError((response.status_code, response.text))
        payload = response.json()
        if self.lose_first_ack:
            self.lose_first_ack = False
            raise OSError("response acknowledgement was lost")
        # BootstrapClient/replay_http expose the route's data object to the
        # helper, while this adapter still performs the real HTTP request.
        return payload.get("data", payload)


def _registration(service):
    config = service.fixture.resolver.resolve_agent_config(service.agent).data
    path = Path(config["workspace"]) / ".bazarlyk/registration.json"
    return json.loads(path.read_text())


def _failure(registration, session_id):
    return technical_reporting.record(
        registration, session_id, "bootstrap", "prepare", "internal_error",
        call_site="bootstrap.handle_hook", retryable=False, terminal=True,
        client=_Offline())


def _rows(service, sql):
    with service.fixture.db.read() as uow:
        return uow.connection.execute(sql).fetchall()


def test_client_helper_http_ingestion_is_idempotent_and_session_scoped(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    registration = _registration(service)
    session_a, session_b = new_id(), new_id()

    # The standalone helper durably queues both failures while the service is down.
    assert _failure(registration, session_a)
    assert _failure(registration, session_b)
    assert technical_reporting.pending(registration) == 2
    assert technical_reporting.active(registration, "bootstrap", "prepare", session_a)
    assert technical_reporting.active(registration, "bootstrap", "prepare", session_b)

    with TestClient(service.web) as http:
        online = _RouteClient(http, service.tokens[service.agent.id], lose_first_ack=True)
        # The route commits before this simulated lost acknowledgement. Replay
        # must retry the same receipt without creating another ticket.
        assert technical_reporting.replay(online, registration) == 0
        assert technical_reporting.pending(registration) == 2
        assert technical_reporting.replay(online, registration) == 2
        assert technical_reporting.pending(registration) == 0

        incidents = _rows(service, "SELECT id,ticket_id,occurrence_count FROM system_incidents")
        assert len(incidents) == 1 and incidents[0][2] == 2
        assert len(_rows(service, "SELECT id FROM dev_tickets")) == 1
        impacts = _rows(service, "SELECT session_id,recovered_at FROM system_incident_impacts ORDER BY session_id")
        assert [row[0] for row in impacts] == sorted([session_a, session_b])
        assert all(row[1] is None for row in impacts)

        # Reload the helper to model a client restart. Its durable active marker
        # still limits recovery to the native session that actually recovered.
        restarted = importlib.reload(technical_reporting)
        assert restarted.active(registration, "bootstrap", "prepare", session_a)
        assert restarted.record(registration, session_a, "bootstrap", "prepare", "success",
                                call_site="bootstrap.handle_hook", recovered=True, client=online)
        assert restarted.pending(registration) == 0
        assert not restarted.active(registration, "bootstrap", "prepare", session_a)
        assert restarted.active(registration, "bootstrap", "prepare", session_b)

        impacts = _rows(service, "SELECT session_id,recovered_at FROM system_incident_impacts ORDER BY session_id")
        by_session = {row[0]: row[1] for row in impacts}
        assert by_session[session_a] is not None and by_session[session_b] is None
        assert len(_rows(service, "SELECT id FROM dev_tickets")) == 1

        # Registration binding and the external observation schema are enforced
        # before ingestion, so forged identity or malformed payloads cannot file
        # another platform incident.
        valid = next(iter(online.calls))[1]["observation"]
        forged = {"registration_id": new_id(), "session_id": session_a, "observation": valid}
        denied = online.http.post("/api/agent/technical-observation", headers=online.headers, json=forged)
        assert denied.status_code == 403
        foreign = {"registration_id": registration["registration_id"], "session_id": session_a,
                   "observation": {**valid, "agent_id": new_id()}}
        rejected = online.http.post("/api/agent/technical-observation", headers=online.headers, json=foreign)
        assert rejected.status_code == 422
        malformed = {"registration_id": registration["registration_id"], "session_id": session_a,
                     "observation": {**valid, "error_code": "contains spaces"}}
        rejected = online.http.post("/api/agent/technical-observation", headers=online.headers, json=malformed)
        assert rejected.status_code == 422
        assert len(_rows(service, "SELECT id FROM dev_tickets")) == 1


# --- Composite session identity over the real endpoint -------------------------

from bazarlyk.bootstrap.client import _safe_details
from qorqut_operations.contracts import DomainError


class _DomainRouteClient(_RouteClient):
    """Real HTTP requests with BootstrapClient's error mapping (code plus safe reason only)."""

    def post(self, route, value):
        self.calls.append((route, dict(value)))
        response = self.http.post(route, headers=self.headers, json=value)
        payload = response.json()
        if payload.get("status") == "error":
            error = payload["error"]
            raise DomainError(error["code"], error["message"], details=_safe_details(error.get("details")))
        return payload.get("data", payload)


def _principal_registration(service, principal):
    config = service.fixture.resolver.resolve_agent_config(principal).data
    return json.loads((Path(config["workspace"]) / ".bazarlyk/registration.json").read_text())


def _bind(service, principal, session_id):
    """The same registration path an agent's bootstrap uses (agent_activity row)."""
    ctx = service.runtime.resolver.invocation_context(principal)
    service.runtime.activity.register_session(ctx, service.registrations[principal.id], session_id, "claude-code")


def _bind_session_registration(service, principal, session_id, *, registration_id=None):
    with service.fixture.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO session_registrations(id,principal_id,registration_id,session_id,workspace,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'2026-09-30T00:00:00Z','2026-09-30T00:00:00Z')",
            (new_id(), principal.id, registration_id or service.registrations[principal.id], session_id, "/workspace"))


def _wire(registration, session_id, *, observation_id=None, registration_id=None):
    value = technical_reporting._base(registration, session_id, "bootstrap", "prepare", "internal_error",
                                      call_site="bootstrap.handle_hook", terminal=True)
    if observation_id:
        value["observation_id"] = observation_id
    observation = {key: item for key, item in value.items() if key not in technical_reporting._LOCAL_ONLY}
    return {"registration_id": registration_id or registration["registration_id"], "session_id": session_id,
            "observation": observation}


def _post(http, service, principal, wire):
    return http.post("/api/agent/technical-observation",
                     headers={"Authorization": "Bearer " + service.tokens[principal.id], "Origin": "http://testserver"},
                     json=wire)


def test_native_uuid_alone_is_not_identity_but_principal_and_registration_together_are(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    try:
        agent, other = service.agent, service.manager
        mine, theirs = _principal_registration(service, agent), _principal_registration(service, other)
        shared = new_id()
        _bind(service, agent, shared)
        _bind(service, other, shared)  # a legitimate second principal with the same native UUID
        with TestClient(service.web) as http:
            # Each principal, with its own registration, may report its own session,
            # even though another principal has a separate row for the same UUID.
            same_receipt = new_id()
            first = _post(http, service, agent, _wire(mine, shared, observation_id=same_receipt))
            second = _post(http, service, other, _wire(theirs, shared, observation_id=same_receipt))
            assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
            # Receipts are namespaced per agent: the same client UUID is two records,
            # and an exact retry by either principal remains one.
            assert len(_rows(service, "SELECT observation_id FROM system_incident_ingested")) == 2
            assert _post(http, service, agent, _wire(mine, shared, observation_id=same_receipt)).status_code == 200
            assert len(_rows(service, "SELECT observation_id FROM system_incident_ingested")) == 2
            attributed = {row[0] for row in _rows(service, f"SELECT agent_id FROM system_incident_impacts WHERE session_id='{shared}'")}
            assert attributed == {agent.id, other.id}

            # The authentic Marketing shape: the rightful session_registrations row
            # sits beside foreign activity for the same UUID.
            legit = new_id()
            _bind_session_registration(service, agent, legit)
            _bind(service, other, legit)
            assert _post(http, service, agent, _wire(mine, legit)).status_code == 200
            # ... and the mirror: matching activity beside a foreign session registration.
            mirror = new_id()
            _bind(service, agent, mirror)
            _bind_session_registration(service, other, mirror)
            assert _post(http, service, agent, _wire(mine, mirror)).status_code == 200

            # A session known only to other principals is refused, without disclosing them.
            foreign_activity, foreign_registration = new_id(), new_id()
            _bind(service, other, foreign_activity)
            _bind_session_registration(service, other, foreign_registration)
            tickets = len(_rows(service, "SELECT id FROM dev_tickets"))
            for session in (foreign_activity, foreign_registration):
                denied = _post(http, service, agent, _wire(mine, session))
                assert denied.status_code == 403
                error = denied.json()["error"]
                assert error["code"] == "forbidden" and error["details"] == {"reason": "technical_session_mismatch"}
                for secret in (other.id, theirs["registration_id"], service.tokens[other.id]):
                    assert secret not in denied.text
            assert len(_rows(service, "SELECT id FROM dev_tickets")) == tickets

            # A stale registration: this principal's row exists, but under another registration.
            stale = new_id()
            with service.fixture.db.transaction() as uow:
                uow.connection.execute(
                    "INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime,activity_at) VALUES(?,?,?,'claude-code','2026-09-30T00:00:00Z')",
                    (agent.id, stale, service.registrations[other.id]))
            refused = _post(http, service, agent, _wire(mine, stale))
            assert refused.status_code == 403 and refused.json()["error"]["details"] == {"reason": "technical_session_mismatch"}

            # The body cannot choose another registration.
            wrong = _post(http, service, agent, _wire(mine, new_id(), registration_id=theirs["registration_id"]))
            assert wrong.status_code == 403
            assert wrong.json()["error"]["details"] == {"reason": "technical_registration_mismatch"}
            assert theirs["registration_id"] not in json.dumps(wrong.json()["error"])

            # A failed FIRST bootstrap (no row anywhere yet) is still reportable.
            assert _post(http, service, agent, _wire(mine, new_id())).status_code == 200
            # Schema and timestamp checks still apply.
            invalid = _wire(mine, new_id())
            invalid["observation"]["error_code"] = "contains spaces"
            assert _post(http, service, agent, invalid).status_code == 422
    finally:
        service.close()


def test_rejected_per_record_report_no_longer_blocks_a_healthy_session_over_http(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    try:
        agent, other = service.agent, service.manager
        registration = _principal_registration(service, agent)
        foreign, healthy = new_id(), new_id()
        _bind(service, other, foreign)
        assert _failure(registration, foreign)  # queued first, so it heads the outbox
        assert _failure(registration, healthy)
        assert technical_reporting.pending(registration) == 2
        with TestClient(service.web) as http:
            client = _DomainRouteClient(http, service.tokens[agent.id])
            assert technical_reporting.replay(client, registration) == 2
            assert technical_reporting.pending(registration) == 0
            [entry] = [json.loads(line) for line in (Path(registration["workspace"]) /
                ".bazarlyk/technical-reports/rejected.jsonl").read_text().splitlines()]
            assert entry["reason"] == "technical_session_mismatch" and entry["record"]["session_id"] == foreign
            assert {row[0] for row in _rows(service, "SELECT session_id FROM system_incident_impacts")} == {healthy}
            assert technical_reporting.active(registration, "bootstrap", "prepare", healthy)
            assert not technical_reporting.active(registration, "bootstrap", "prepare", foreign)

            # A bad credential is endpoint-wide: nothing is quarantined or discarded.
            assert _failure(registration, new_id())
            bad = _DomainRouteClient(http, "x" * 48)
            assert technical_reporting.replay(bad, registration) == 0
            assert technical_reporting.pending(registration) == 1
            assert len(entry) and len((Path(registration["workspace"]) /
                ".bazarlyk/technical-reports/rejected.jsonl").read_text().splitlines()) == 1
    finally:
        service.close()


def test_quarantined_recovery_retires_the_local_marker_but_leaves_the_remote_incident_unresolved(tmp_path):
    from bazarlyk.bootstrap import recovery
    service = ServiceFixture(tmp_path, full=True)
    try:
        agent = service.agent
        registration = _principal_registration(service, agent)
        session = new_id()
        _bind(service, agent, session)
        recovery.record(registration, session, "claude-code", "busy")  # an unrelated preparation blocker
        assert _failure(registration, session)
        with TestClient(service.web) as http:
            client = _DomainRouteClient(http, service.tokens[agent.id])
            assert technical_reporting.replay(client, registration) == 1
            assert _rows(service, f"SELECT count(*) FROM system_incident_impacts WHERE session_id='{session}' AND recovered_at IS NULL")[0][0] == 1
            assert technical_reporting.active(registration, "bootstrap", "prepare", session)
            # The service accepted the failure but refuses this recovery for itself (timestamp bound).
            stale = "2026-01-01T00:00:00+00:00"
            assert technical_reporting.record(registration, session, "bootstrap", "prepare", "success",
                call_site="bootstrap.handle_hook", recovered=True, observed_at=stale)
            assert technical_reporting.replay(client, registration) == 1
            assert technical_reporting.pending(registration) == 0
            # The local attempt is retired, but nothing recovered remotely and the evidence is retained for review.
            assert not technical_reporting.active(registration, "bootstrap", "prepare", session)
            assert _rows(service, f"SELECT count(*) FROM system_incident_impacts WHERE session_id='{session}' AND recovered_at IS NULL")[0][0] == 1
            assert _rows(service, f"SELECT count(*) FROM system_incident_recoveries")[0][0] == 0
            report = technical_reporting.diagnostics(registration)
            assert report["pending"]["count"] == 0 and report["degraded"] == ["rejected_pending_review"]
            [entry] = [json.loads(line) for line in (Path(registration["workspace"]) /
                ".bazarlyk/technical-reports/rejected.jsonl").read_text().splitlines()]
            assert entry["reason"] == "validation_error" and entry["record"]["recovered"] is True
            assert recovery.read(registration, session) is not None  # preparation state is untouched
    finally:
        service.close()
