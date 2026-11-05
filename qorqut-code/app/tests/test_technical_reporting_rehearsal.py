"""Isolated rehearsal of recovering a legacy, full, collision-blocked outbox.

Everything here is a disposable fixture: generated identities, a temporary
database and temporary agent folders. It mirrors the shape of the confirmed
Marketing backlog (a few failures under thousands of duplicate recoveries, the
4 MiB cap reached, one native session registered for its rightful agent while
another agent also has activity for the same UUID) and shows that the corrected
reporting path preserves every failure, coalesces the recoveries and drains.
It never reads or changes production state and is not a cleanup tool.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from starlette.testclient import TestClient

from bazarlyk.bootstrap import technical_reporting as tr
from qorqut_operations.contracts import new_id
from test_service_transports import ServiceFixture
from test_technical_reporting_http import (
    _DomainRouteClient, _bind, _bind_session_registration, _principal_registration, _rows)


def _line(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _seed_legacy_backlog(registration, session, foreign_session):
    """Write the pre-coalescing shape: random recovery ids, repeated per poll."""
    ticks = (datetime.now(timezone.utc) - timedelta(hours=3) + timedelta(milliseconds=500 * n) for n in range(10 ** 6))

    def make(component, operation, call_site, *, recovery_for=None, sid=session):
        recovered = recovery_for is not None
        value = tr._base(registration, sid, component, operation, "success" if recovered else "internal_error",
            call_site=call_site, retryable=False, terminal=not recovered, recovered=recovered,
            observed_at=next(ticks).isoformat(timespec="microseconds"))
        if recovered:
            value["recovery_for"] = recovery_for
        return value

    # Three failure episodes (two of the same operation, the newer replacing the first's
    # marker), one of another operation, and a last one that fills the cap.
    episodes = [("inbox", "watch", "inbox.watcher.watch", 3000), ("inbox", "watch", "inbox.watcher.watch", 3000),
                ("bootstrap", "prepare", "bootstrap.handle_hook", 3000),
                ("activity_hook", "record", "activity_hook.record", None)]
    head = make("bootstrap", "prepare", "bootstrap.handle_hook", sid=foreign_session)  # can never be accepted
    lines, size, failures = [_line(head)], len(_line(head)), []
    for component, operation, call_site, quota in episodes:
        failure = make(component, operation, call_site)
        failures.append(failure)
        lines.append(_line(failure))
        size += len(lines[-1])
        added = 0
        while quota is None or added < quota:
            line = _line(make(component, operation, call_site, recovery_for=failure["observation_id"]))
            if size + len(line) + 1 > tr.MAX_OUTBOX_BYTES:
                break
            lines.append(line)
            size += len(line)
            added += 1
    path = tr._path(registration)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    with tr._lock(path):
        for failure in failures[1:]:  # one marker per operation keeps only its newest failure
            tr._mark_active_locked(path, failure)
    return failures, len(lines)


def test_legacy_full_outbox_with_a_session_collision_recovers_and_drains(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    try:
        agent, other = service.agent, service.manager
        registration = _principal_registration(service, agent)
        session, foreign_session, later_session = new_id(), new_id(), new_id()
        # The authentic shape: the rightful session_registrations row plus
        # activity for BOTH agents, all with the same native UUID.
        _bind_session_registration(service, agent, session)
        _bind(service, agent, session)
        _bind(service, other, session)
        _bind(service, other, foreign_session)
        _bind(service, agent, later_session)
        failures, total = _seed_legacy_backlog(registration, session, foreign_session)

        before = tr.diagnostics(registration)
        assert total > 9000 and before["pending"]["count"] == total
        assert before["status"] == "degraded" and "queue_full" in before["degraded"]
        assert before["pending"]["failures"] == 5 and before["pending"]["recovery_episodes"] == 4
        assert before["pending"]["duplicate_recoveries"] == total - 5 - 4

        with TestClient(service.web) as http:
            client = _DomainRouteClient(http, service.tokens[agent.id])
            # A brand-new failure arrives while the outbox is full and the service is healthy:
            # record() replays and compacts, then saves it instead of dropping it; the
            # replay loop below delivers it.
            assert tr.record(registration, later_session, "bootstrap", "prepare", "internal_error",
                             call_site="bootstrap.handle_hook", terminal=True, client=client)
            for _ in range(5):
                if not tr.pending(registration):
                    break
                tr.replay(client, registration)

            after = tr.diagnostics(registration)
            # Nothing is pending; the one retained rejected record still awaits reconciliation.
            assert after["pending"]["count"] == 0 and after["degraded"] == ["rejected_pending_review"]
            # The one record that could never be accepted is preserved, not dropped or blocking.
            assert after["rejected"]["count"] == 1
            [entry] = [json.loads(line) for line in (Path(registration["workspace"]) /
                ".bazarlyk/technical-reports/rejected.jsonl").read_text().splitlines()]
            assert entry["reason"] == "technical_session_mismatch" and entry["record"]["session_id"] == foreign_session
            # Four failures and their four coalesced recoveries, plus the new failure, reached the service.
            assert len(_rows(service, "SELECT observation_id FROM system_incident_ingested")) == 4 + 4 + 1
            assert _rows(service, f"SELECT count(*) FROM system_incident_impacts WHERE session_id='{session}' AND recovered_at IS NULL")[0][0] == 0
            assert _rows(service, f"SELECT count(*) FROM system_incident_impacts WHERE session_id='{session}'")[0][0] >= 1
            assert _rows(service, f"SELECT count(*) FROM system_incident_impacts WHERE session_id='{later_session}' AND recovered_at IS NULL")[0][0] == 1
            # Only the new session's failure remains active; recovered episodes are closed.
            assert [m["session_id"] for m in after["active_markers"]] == [later_session]
            assert after["history"]["coalesced"] == total - (1 + 4 + 4)
            # No SQL repair was needed: every authoritative row is untouched.
            assert _rows(service, f"SELECT count(*) FROM agent_activity WHERE session_id='{session}'")[0][0] == 2
            assert _rows(service, f"SELECT count(*) FROM session_registrations WHERE session_id='{session}'")[0][0] == 1
    finally:
        service.close()
