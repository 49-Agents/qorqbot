"""``transport/managed_launch.py``: the broadcast port into a session, and the preparation a managed session is opened with.

The broadcast reaches an agent only through the shared input arbiter and never opens a session or falls back to anything else.
Stand-ins replace the database, the arbiter and the preparation; nothing here starts a host or a client.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.native_chat.contracts import (InputResult, NativeChatError, RESULT_DEFERRED, STATE_FAILED, STATE_SUBMITTED,
                                          STATE_UNCERTAIN)
from qorqut.transport import managed_launch
from qorqut.transport.managed_launch import ManagedLaunch

AGENT = "11111111-1111-4111-8111-111111111111"


class Arbiter:
    def __init__(self, result):
        self.result, self.requests = result, []

    def submit(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Case(unittest.TestCase):
    def setUp(self):
        self.row = {"agent_id": AGENT, "home_business_id": None, "status": "active"}
        self.arbiter = Arbiter(InputResult(state=STATE_SUBMITTED, evidence={"transport": "managed"}))

        @contextmanager
        def read():
            yield SimpleNamespace(connection=SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: self.row)))
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=read), native_chat=SimpleNamespace(arbiter=self.arbiter),
                                       paths=SimpleNamespace(runtime="/r"), application=SimpleNamespace(writer_admission=None),
                                       resolver=SimpleNamespace(profile=lambda principal, connection=None: (SimpleNamespace(agent_id=AGENT), {})))
        self.launch = ManagedLaunch(self.runtime, service_url="http://x/mcp")
        patch = mock.patch.object(managed_launch, "active_global", lambda connection, agent: {"role": {"id": "x"}})
        patch.start()
        self.addCleanup(patch.stop)


class BroadcastPortTests(Case):
    def test_a_keyed_broadcast_is_one_input_through_the_arbiter(self):
        result = self.launch.input_existing(None, AGENT, "hello", key="k1")
        self.assertEqual(result, {"state": "delivered", "evidence": {"transport": "managed"}})
        (request,) = self.arbiter.requests
        self.assertEqual((request.agent_id, request.input_type, request.source_key, request.text), (AGENT, "broadcast", "k1", "hello"))

    def test_a_keyless_call_has_no_fallback(self):
        self.assertEqual(self.launch.input_existing(None, AGENT, "hello")["state"], "unavailable")
        self.assertEqual(self.arbiter.requests, [])

    def test_the_arbiters_answers_in_the_dispatchers_words(self):
        for result, state in ((InputResult(state=STATE_UNCERTAIN), "uncertain"), (InputResult(state=STATE_FAILED, reason="x"), "failed"),
                              (InputResult(state=RESULT_DEFERRED, reason="session_closed"), "unavailable"),
                              (NativeChatError("validation_error", "too long"), "failed")):
            with self.subTest(state=state):
                self.arbiter.result = result
                found = self.launch.input_existing(None, AGENT, "hi", key="k")
                self.assertEqual(found["state"], state)
                self.assertNotIn("terminal", str(found).lower())

    def test_a_business_home_or_inactive_agent_is_never_dispatched_to(self):
        for row in (None, {**self.row, "home_business_id": "b1"}):
            self.row = row
            self.assertEqual(self.launch.input_existing(None, AGENT, "hi", key="k")["state"], "unavailable")
        self.assertEqual(self.arbiter.requests, [])

    def test_the_text_is_bounded(self):
        for text in (None, 7, "x" * 65537):
            with self.assertRaises(DomainError):
                self.launch.input_existing(None, AGENT, text, key="k")


class PreparationTests(Case):
    def test_prepare_managed_prepares_the_agent_and_returns_its_workspace_and_environment(self):
        calls = []
        self.launch.preparation = lambda resolver, principal, **kw: calls.append((principal.id, kw)) or {"ok": True}
        self.launch.version_provider = lambda: "2.1.0"
        self.launch.agent_environment = lambda business, profile: {"TOKEN": "t"}
        with mock.patch.object(managed_launch, "agent_workspace", lambda paths, agent_id, business: "/w/" + agent_id), \
                mock.patch.object(managed_launch.agent_cli_launch, "session_environment", lambda env: {**env, "PATH": "/ours"}):
            result = self.launch.prepare_managed(AGENT)
        self.assertEqual(result, {"workspace": "/w/" + AGENT, "env": {"TOKEN": "t", "PATH": "/ours"}})
        self.assertEqual(calls, [(AGENT, {"endpoint": "http://x/mcp", "versions": {"claude-code": "2.1.0"}, "capture_transcripts": True})])

    def test_an_agent_that_is_not_an_active_global_is_not_prepared(self):
        self.row = None
        self.launch.preparation = lambda *a, **k: self.fail("prepared")
        with self.assertRaises(DomainError) as refused:
            self.launch.prepare_managed(AGENT)
        self.assertEqual(refused.exception.code, "not_found")

    def test_a_project_home_agent_is_not_prepared_whatever_its_status(self):
        """Agents with a home project are retired identities (none is created any more): the preparation's own query leaves them
        out, so no managed session can be opened for one even if every other check said yes."""
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE agent_profiles(agent_id TEXT, home_business_id TEXT, status TEXT)")
        connection.execute("INSERT INTO agent_profiles VALUES(?,?,?)", (AGENT, "business-1", "active"))

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        self.runtime.database = SimpleNamespace(read=read)
        self.launch.preparation = lambda *a, **k: self.fail("prepared")
        with self.assertRaises(DomainError) as refused:
            self.launch.prepare_managed(AGENT)
        self.assertEqual(refused.exception.code, "not_found")
        connection.execute("UPDATE agent_profiles SET home_business_id=NULL")
        self.launch.preparation = lambda *a, **k: {"ok": True}
        with mock.patch.object(managed_launch, "agent_workspace", lambda paths, agent_id, business: "/w"), \
                mock.patch.object(managed_launch.agent_cli_launch, "session_environment", lambda env: env):
            self.assertEqual(self.launch.prepare_managed(AGENT)["workspace"], "/w")

    def test_the_capture_selection_must_be_a_boolean(self):
        with self.assertRaises(ValueError):
            ManagedLaunch(self.runtime, service_url="u", capture_transcripts="yes")


if __name__ == "__main__":
    unittest.main()
