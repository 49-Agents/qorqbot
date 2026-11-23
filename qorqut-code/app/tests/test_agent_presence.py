"""The navigation's working dots read each agent's state alone. It is the state the full activity list reports, under the same
permission, and its cost does not grow with the agents' sessions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from qorqut_operations.contracts import DomainError, Principal, new_id

from qorqut.agents.activity import AgentActivity

from private_thread_runtime import build_runtime, invoke

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


class AgentPresenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.activity = self.activity = AgentActivity(runtime, clock=lambda: NOW).register()
        self.lead, self.worker = runtime.coordinator.principal_id, runtime.worker.principal_id

    def write(self, sql, values):
        # A plain connection: the fixture has no machine registration for a session row to reference.
        connection = sqlite3.connect(self.runtime.database.path)
        try:
            connection.execute(sql, values)
            connection.commit()
        finally:
            connection.close()

    def session(self, agent, state, seconds_ago):
        seen = (NOW - timedelta(seconds=seconds_ago)).isoformat()
        self.write("INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime,state,last_seen_at) "
                   "VALUES(?,?,'registration','claude-code',?,?)", (agent, new_id(), state, seen))

    def session_owner(self, agent, desired):
        self.write("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) "
                   "VALUES(?,?,'{\"runtime\":\"claude-code\"}',1,?)", (agent, new_id(), desired))

    def managed(self):
        self.runtime.native_chat = SimpleNamespace(opener=SimpleNamespace(managed=object()))

    def listed(self):
        return {agent['agent_id']: agent['state'] for agent in invoke(self.runtime, 'agents_activity_list', {'limit': 200})['agents']}

    def dots(self, ctx=None):
        return {agent['agent_id']: agent['state'] for agent in self.activity.presence(ctx or self.runtime.owner)['agents']}

    def test_the_dots_show_the_state_the_activity_list_reports(self):
        self.session(self.lead, 'offline', 5)
        self.session(self.lead, 'running', 20)
        self.session(self.worker, 'running', 600)  # not seen for ten minutes: no longer known to be running
        self.assertEqual(self.dots(), {self.lead: 'running', self.worker: 'unknown'})
        self.assertEqual(self.dots(), self.listed())

    def test_a_managed_agent_without_an_open_session_is_asleep_or_off(self):
        self.managed()
        self.session(self.lead, 'offline', 30)
        self.session_owner(self.lead, 'running')
        self.session_owner(self.worker, 'stopped')
        self.assertEqual(self.dots(), {self.lead: 'asleep', self.worker: 'off'})
        self.assertEqual(self.dots(), self.listed())

    def test_only_the_newest_twenty_sessions_of_an_agent_count(self):
        for seconds in range(1, 21):
            self.session(self.lead, 'offline', seconds)
        self.session(self.lead, 'idle', 60)  # the twenty-first newest: the full list does not read it either
        self.assertEqual(self.dots()[self.lead], 'offline')
        self.assertEqual(self.dots(), self.listed())

    def test_only_the_installation_owner_reads_it(self):
        stranger = replace(self.runtime.owner, principal=Principal(new_id(), 'human'))
        with self.assertRaises(DomainError):
            self.dots(stranger)

    def test_more_sessions_do_not_add_statements(self):
        def statements():
            seen = []
            with self.runtime.database.read() as uow:
                uow.connection.set_trace_callback(seen.append)  # the pooled connection the next unit takes
            self.dots()
            with self.runtime.database.read() as uow:
                uow.connection.set_trace_callback(None)
            return len(seen)
        self.session(self.lead, 'running', 5)
        few = statements()
        for seconds in range(10, 40):
            self.session(self.lead, 'idle', seconds)
            self.session(self.worker, 'idle', seconds)
        self.assertEqual(statements(), few)


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class PresenceRouteTests(unittest.TestCase):
    """``GET /api/me/agents/presence`` through the real ``HTTPAPI`` dispatch, with a stand-in for the activity service."""

    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        self.calls, self.error = [], None

        def presence(ctx):
            self.calls.append(ctx)
            if self.error is not None:
                raise self.error
            return {'agents': [{'agent_id': 'a1', 'state': 'running'}], 'as_of': NOW.isoformat()}

        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(activity=SimpleNamespace(presence=presence), require_authority=lambda: None,
                                      paths=SimpleNamespace(runtime="/nonexistent"))
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id="owner", display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        self.context = SimpleNamespace(principal_kind="human", principal_id="owner")
        api.context = lambda actor, slug=None, owner=False: (self.context, None)

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)

    def test_the_route_answers_with_the_states_for_the_callers_own_context(self):
        response = self.client.get("/api/me/agents/presence")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["agents"], [{'agent_id': 'a1', 'state': 'running'}])
        self.assertEqual(self.calls, [self.context])

    def test_a_refusal_reaches_the_caller(self):
        self.error = DomainError("forbidden", "Agent activity requires the installation owner")
        response = self.client.get("/api/me/agents/presence")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))


if __name__ == '__main__':
    unittest.main()
