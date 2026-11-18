"""``ManagedAdapter`` (native_chat/adapters.py) against a stand-in host client, and the guards that keep every tmux operation away
from a managed agent (``TerminalService``, ``AgentTerminalBackend``, terminal control).

Nothing here starts a host, a socket or ``claude``: the client records each call in one shared log, so "the checkpoint ran once
and before the send" is an assertion about that log. The outcome codes are the managed side of the table in
docs/MANAGED-RUNTIME-STEP3-DESIGN.md, section 2.
"""
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.agents import terminal_control
from qorqut.managed_runtime import manager as hosts
from qorqut.managed_runtime import mode as modes
from qorqut.managed_runtime.client import HostUnavailable
from qorqut.native_chat.adapters import ManagedAdapter, TerminalAdapter
from qorqut.native_chat.arbiter import InputArbiter
from qorqut.native_chat.contracts import TYPE_CHAT, TYPE_NUDGE, TYPE_TELEGRAM
from qorqut.transport.agent_terminal_control import AgentTerminalBackend, OwnerInputOutcome
from qorqut.transport.terminals import TerminalService

AGENT = "11111111-1111-4111-8111-111111111111"
SESSION = "33333333-3333-4333-8333-333333333333"
PROFILE = {"agent_id": AGENT, "workspace": "/w", "profile_json": json.dumps({"runtime": "claude-code"})}
SESSION_ROW = {"transport": "managed", "provider": "claude", "conversation_id": SESSION, "process_pid": 4343,
               "process_started": "777", "owner": "host:4242:555"}


def status(**fields):
    """A host ``status`` reply for the session in ``SESSION_ROW``."""
    return {"status": "ok", "agent_id": AGENT, "instance": "inst-1", "host_pid": 4242, "host_identity": "boot:555", "state": "ready",
            "phase": "idle", "session_id": SESSION, "claude_pid": 4343, "claude_identity": "boot:777", "activity": None,
            "turn_started_at": None, "pending": [], **fields}


class Client:
    """A host client whose every call goes into ``log``. ``send_reply`` may be a reply dict or an exception to raise."""

    def __init__(self, log, *, status_reply=None, send_reply=None, lookup_reply=None, close_reply=None):
        self.log = log
        self.status_reply = status() if status_reply is None else status_reply
        self.send_reply = {"status": "ok", "mode": "started", "duplicate": False, "turn_started": True} if send_reply is None else send_reply
        self.lookup_reply = lookup_reply
        self.close_reply = {"status": "ok", "state": "closing"} if close_reply is None else close_reply
        self.sent, self.lookups, self.closes = [], [], []

    @staticmethod
    def _result(value):
        if isinstance(value, Exception):
            raise value
        return value

    def status(self):
        self.log.append("status")
        return self._result(self.status_reply)

    def send(self, text, message_uuid, steer=False):
        self.log.append("send")
        self.sent.append((text, message_uuid, steer))
        return self._result(self.send_reply)

    def lookup(self, message_uuid, fence=False):
        self.log.append("lookup")
        self.lookups.append((message_uuid, fence))
        return self._result(self.lookup_reply)

    def close(self, mode="graceful"):
        self.log.append("close")
        self.closes.append(mode)
        return self._result(self.close_reply)


class Managed:
    """The ``ManagedRuntime`` seen by the adapter: ``handle`` returns the next prepared host (the last one repeats)."""

    def __init__(self, *hosts_):
        self.hosts = list(hosts_)
        self.asked = 0

    def handle(self, agent_id):
        self.asked += 1
        item = self.hosts[min(self.asked, len(self.hosts)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def host(client, **record):
    return hosts.Host(AGENT, {"host_pid": 4242, "host_identity": "boot:555", "state": "ready", "session_id": SESSION,
                              "claude_pid": 4343, "claude_identity": "boot:777", "instance": "inst-1", **record}, client)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.uuid = str(uuid.uuid4())

    def client(self, **kwargs):
        return Client(self.log, **kwargs)

    def checkpoint(self, *, fail=False):
        def run():
            self.log.append("checkpoint")
            if fail:
                raise RuntimeError("claim refused")
        return run

    def submit(self, managed, *, text="hello", steer=True, checkpoint=None, profile=PROFILE, session=SESSION_ROW, **options):
        adapter = ManagedAdapter(managed, new_uuid=lambda: self.uuid)
        return adapter.submit(profile, session, text, steer=steer, echo=False, checkpoint=checkpoint or self.checkpoint(), **options)

    def assertOutcome(self, outcome, status_, reason):
        self.assertEqual((outcome.status, outcome.reason), (status_, reason))


class Deferrals(Fixture):
    """Nothing was sent, and the claim was not made."""

    def assertUntouched(self, client=None):
        self.assertNotIn("checkpoint", self.log)
        self.assertNotIn("send", self.log)

    def test_no_verified_host_is_a_closed_session(self):
        self.assertOutcome(self.submit(Managed(None)), "deferred", "session_closed")
        self.assertOutcome(self.submit(None), "deferred", "session_closed")
        self.assertOutcome(self.submit(lambda: None), "deferred", "session_closed")
        self.assertUntouched()

    def test_a_host_that_cannot_be_verified_is_never_written_to(self):
        self.assertOutcome(self.submit(Managed(hosts.HostUnverified(AGENT))), "deferred", "host_unverified")
        for reason in ("unverified", "unreachable", "no_token", "refused", "instance_changed", "timeout", "protocol"):
            with self.subTest(reason=reason):
                managed = Managed(host(self.client(status_reply=HostUnavailable(reason))))
                self.assertOutcome(self.submit(managed), "deferred", "host_unverified")
        self.assertOutcome(self.submit(Managed(host(self.client(status_reply={"status": "rejected", "reason": "x"})))), "deferred",
                           "host_unverified")
        self.assertOutcome(self.submit(Managed(host(self.client(status_reply=OSError("socket"))))), "deferred", "host_unverified")
        self.assertUntouched()

    def test_a_host_that_went_away_between_looking_and_asking_is_a_closed_session(self):
        for reason in ("no_host", "not_running"):
            managed = Managed(host(self.client(status_reply=HostUnavailable(reason))))
            self.assertOutcome(self.submit(managed), "deferred", "session_closed")
        self.assertUntouched()

    def test_another_session_or_host_is_a_stale_session(self):
        for change in ({"session_id": "other"}, {"claude_pid": 9999}, {"claude_identity": "boot:888"}, {"host_pid": 4243},
                       {"host_identity": "boot:556"}):
            with self.subTest(change=change):
                self.assertOutcome(self.submit(Managed(host(self.client(status_reply=status(**change))))), "deferred", "stale_session")
        self.assertUntouched()

    def test_a_session_still_starting_or_closing_or_closed_takes_nothing(self):
        for state, reason in (("starting", "session_starting"), ("closing", "session_closing"), ("closed", "session_closed")):
            with self.subTest(state=state):
                self.assertOutcome(self.submit(Managed(host(self.client(status_reply=status(state=state))))), "deferred", reason)
        self.assertOutcome(self.submit(Managed(hosts.Host(AGENT, {"state": "starting"}, None))), "deferred", "session_starting")
        self.assertUntouched()

    def test_a_host_whose_driver_never_started_is_a_failure(self):
        self.assertOutcome(self.submit(Managed(host(self.client(status_reply=status(state="failed"))))), "failed", "driver_unavailable")
        self.assertUntouched()

    def test_only_claude_code_is_managed(self):
        for runtime in ("codex", None):
            with self.subTest(runtime=runtime):
                profile = {**PROFILE, "profile_json": json.dumps({"runtime": runtime})}
                self.assertOutcome(self.submit(Managed(host(self.client())), profile=profile), "failed", "unsupported_runtime")
        self.assertOutcome(self.submit(Managed(host(self.client())), session={**SESSION_ROW, "provider": "codex"}),
                           "failed", "unsupported_runtime")
        self.assertOutcome(self.submit(Managed(host(self.client())), profile={"agent_id": AGENT, "profile_json": "not json"}),
                           "failed", "unsupported_runtime")
        self.assertUntouched()


class Phase(Fixture):
    def test_a_running_turn_defers_a_producer_that_does_not_steer(self):
        for phase in ("working", "blocked"):
            with self.subTest(phase=phase):
                self.log.clear()
                client = self.client(status_reply=status(phase=phase))
                self.assertOutcome(self.submit(Managed(host(client)), steer=False), "deferred", "turn_running")
                self.assertEqual(self.log, ["status"], "no checkpoint, no send")

    def test_a_steering_producer_sends_into_a_running_turn_and_it_is_queued(self):
        client = self.client(status_reply=status(phase="working"), send_reply={"status": "ok", "mode": "queued", "duplicate": False})
        outcome = self.submit(Managed(host(client)), steer=True)
        self.assertOutcome(outcome, "submitted", "queued_mid_turn")
        self.assertEqual(client.sent, [("hello", self.uuid, True)])
        self.assertTrue(outcome.receipt["queued"])

    def test_an_idle_session_takes_a_non_steering_input_and_starts_a_turn(self):
        client = self.client()
        outcome = self.submit(Managed(host(client)), steer=False)
        self.assertOutcome(outcome, "submitted", "processing_confirmed")
        self.assertEqual(client.sent, [("hello", self.uuid, False)])
        self.assertEqual(outcome.receipt, {"client": "claude", "transport": "managed", "message_uuid": self.uuid, "queued": False})

    def test_a_turn_that_was_not_seen_to_start_is_submitted_not_confirmed(self):
        client = self.client(send_reply={"status": "ok", "mode": "started", "duplicate": False, "turn_started": False})
        self.assertOutcome(self.submit(Managed(host(client))), "submitted", "processing_not_confirmed")

    def test_a_repeated_uuid_is_accepted_by_the_host(self):
        client = self.client(send_reply={"status": "ok", "mode": "started", "duplicate": True, "turn_started": True})
        self.assertOutcome(self.submit(Managed(host(client))), "submitted", "accepted_by_host")

    def test_the_composer_options_mean_nothing_to_a_headless_session(self):
        client = self.client()
        outcome = ManagedAdapter(Managed(host(client)), new_uuid=lambda: self.uuid).submit(
            PROFILE, SESSION_ROW, "text", steer=True, echo=True, checkpoint=self.checkpoint(), replace_composer=True)
        self.assertOutcome(outcome, "submitted", "processing_confirmed")
        self.assertEqual(client.sent, [("text", self.uuid, True)])

    def test_a_real_uuid_is_made_when_none_is_given(self):
        client = self.client()
        ManagedAdapter(Managed(host(client))).submit(PROFILE, SESSION_ROW, "text", steer=True, echo=False, checkpoint=self.checkpoint())
        self.assertEqual(str(uuid.UUID(client.sent[0][1])), client.sent[0][1])


class Checkpoint(Fixture):
    def test_the_checkpoint_runs_once_after_the_last_check_and_before_the_send(self):
        client = self.client()
        self.submit(Managed(host(client)))
        self.assertEqual(self.log, ["status", "checkpoint", "send"])

    def test_a_checkpoint_that_raises_means_nothing_was_sent(self):
        client = self.client()
        outcome = self.submit(Managed(host(client)), checkpoint=self.checkpoint(fail=True))
        self.assertOutcome(outcome, "deferred", "checkpoint_failed")
        self.assertEqual(self.log, ["status", "checkpoint"])
        self.assertEqual(client.sent, [])

    def test_no_checkpoint_is_made_for_an_input_that_cannot_go(self):
        self.submit(Managed(host(self.client(status_reply=status(phase="working")))), steer=False)
        self.submit(Managed(None))
        self.assertNotIn("checkpoint", self.log)


class Rejections(Fixture):
    """The host refused before it wrote anything to Claude: deferred, and the receipt names the message."""

    def test_each_host_rejection_is_a_deferral(self):
        for reason, code in (("session_starting", "session_starting"), ("session_closing", "session_closing"),
                             ("session_closed", "session_closed"), ("turn_running", "turn_running"), ("host_busy", "host_busy"),
                             ("fenced", "host_never_received"), ("something_new", "host_rejected"), (None, "host_rejected")):
            with self.subTest(reason=reason):
                client = self.client(send_reply={"status": "rejected", "reason": reason})
                outcome = self.submit(Managed(host(client)))
                self.assertOutcome(outcome, "deferred", code)
                self.assertEqual(outcome.receipt["message_uuid"], self.uuid)

    def test_a_reply_that_is_not_a_reply_is_uncertain(self):
        for reply in ({"status": "weird"}, {}):
            outcome = self.submit(Managed(host(self.client(send_reply=reply))))
            self.assertOutcome(outcome, "uncertain", "host_connection_lost")


class Unanswered(Fixture):
    """The send may have been written but was not answered: the host's ledger decides, once, with a fence."""

    def managed(self, send_error, *, lookup=None, later=None):
        first = self.client(send_reply=send_error, lookup_reply=lookup)
        return Managed(host(first), *( [later] if later is not None else [host(first)] )), first

    def test_a_timeout_followed_by_a_host_that_has_the_message_is_submitted(self):
        managed, client = self.managed(HostUnavailable("timeout", sent=True), lookup={"status": "ok", "seen": True, "fenced": False, "mode": "started"})
        outcome = self.submit(managed)
        self.assertOutcome(outcome, "submitted", "accepted_by_host")
        self.assertEqual(client.lookups, [(self.uuid, True)], "one lookup, with the same uuid, fenced")
        self.assertEqual(self.log, ["status", "checkpoint", "send", "lookup"])

    def test_a_lost_connection_followed_by_a_queued_message_is_submitted_queued(self):
        managed, _ = self.managed(HostUnavailable("connection_lost", sent=True), lookup={"status": "ok", "seen": True, "mode": "queued"})
        outcome = self.submit(managed)
        self.assertOutcome(outcome, "submitted", "accepted_by_host")
        self.assertTrue(outcome.receipt["queued"])

    def test_a_message_the_host_never_saw_is_fenced_and_deferred(self):
        managed, client = self.managed(HostUnavailable("timeout", sent=True), lookup={"status": "ok", "seen": False, "fenced": True})
        outcome = self.submit(managed)
        self.assertOutcome(outcome, "deferred", "host_never_received")
        self.assertEqual(client.lookups, [(self.uuid, True)])

    def test_an_unfenced_unseen_answer_proves_nothing(self):
        managed, _ = self.managed(HostUnavailable("timeout", sent=True), lookup={"status": "ok", "seen": False, "fenced": False})
        self.assertOutcome(self.submit(managed), "uncertain", "host_timeout")

    def test_when_the_lookup_fails_too_the_outcome_is_uncertain_and_says_why(self):
        for send_reason, code in (("timeout", "host_timeout"), ("connection_lost", "host_connection_lost"),
                                  ("protocol", "host_connection_lost")):
            with self.subTest(reason=send_reason):
                managed, client = self.managed(HostUnavailable(send_reason, sent=True), lookup=HostUnavailable("timeout", sent=True))
                self.assertOutcome(self.submit(managed), "uncertain", code)
                self.assertEqual(len(client.lookups), 1, "asked once, never repeated")
        managed, _ = self.managed(TimeoutError("slow"), lookup=OSError("gone"))
        self.assertOutcome(self.submit(managed), "uncertain", "host_timeout")

    def test_a_host_that_is_gone_or_unverifiable_afterwards_is_uncertain(self):
        first = self.client(send_reply=HostUnavailable("connection_lost", sent=True))
        for later in (None, hosts.HostUnverified(AGENT), hosts.Host(AGENT, {"state": "starting"}, None)):
            with self.subTest(later=type(later).__name__):
                self.assertOutcome(self.submit(Managed(host(first), later)), "uncertain", "host_connection_lost")

    def test_a_different_host_afterwards_says_nothing_about_this_message(self):
        first = self.client(send_reply=HostUnavailable("timeout", sent=True))
        successor = self.client(lookup_reply={"status": "ok", "seen": False, "fenced": True})
        outcome = self.submit(Managed(host(first), host(successor, host_pid=5000, host_identity="boot:999")))
        self.assertOutcome(outcome, "uncertain", "session_changed")
        self.assertEqual(successor.lookups, [], "a fence on another host would prove nothing")

    def test_a_request_that_cannot_have_been_written_is_a_deferral(self):
        for reason, code in (("no_host", "session_closed"), ("not_running", "session_closed"), ("unreachable", "host_unverified"),
                             ("unverified", "host_unverified"), ("instance_changed", "host_unverified")):
            with self.subTest(reason=reason):
                managed, client = self.managed(HostUnavailable(reason, sent=False))
                self.assertOutcome(self.submit(managed), "deferred", code)
                self.assertEqual(client.lookups, [])

    def test_a_host_that_refused_the_token_did_nothing(self):
        managed, client = self.managed(HostUnavailable("refused", sent=True))
        self.assertOutcome(self.submit(managed), "deferred", "host_unverified")
        self.assertEqual(client.lookups, [])

    def test_a_bug_in_the_client_is_not_hidden(self):
        managed, _ = self.managed(ValueError("bug"))
        with self.assertRaises(ValueError):
            self.submit(managed)


class StagingManaged(Managed):
    """The managed runtime with the owner's first message remembered (``send_first``) and a ledger for a host that is gone."""

    def __init__(self, *hosts_, staged=None, ledger=None):
        super().__init__(*hosts_)
        self.staged, self.unstaged, self.ledger, self.asked_ledger = dict(staged or {}), [], ledger, []

    def staged_uuid(self, agent_id, text):
        return self.staged.get(text)

    def unstage(self, agent_id, text):
        self.unstaged.append(text)
        self.staged.pop(text, None)

    def sent_before_exit(self, agent_id, message_uuid):
        self.asked_ledger.append(message_uuid)
        return self.ledger


class FirstMessage(Fixture):
    """The owner's message that opened the session was already handed to the host, with a uuid derived from the request: the
    input admitted for it reuses that uuid, which the host recognises, so the message is typed once."""

    def test_the_staged_uuid_is_the_one_the_host_is_sent_and_it_is_recognised(self):
        staged = str(uuid.uuid4())
        client = self.client(send_reply={"status": "ok", "mode": "started", "duplicate": True})
        managed = StagingManaged(host(client), staged={"hello": staged})
        outcome = self.submit(managed)
        self.assertOutcome(outcome, "submitted", "accepted_by_host")
        self.assertEqual(client.sent, [("hello", staged, True)])
        self.assertEqual(outcome.receipt["message_uuid"], staged)
        self.assertEqual(managed.unstaged, ["hello"], "forgotten once it is an input, so a later identical message is a new one")

    def test_another_text_or_nothing_staged_gets_a_fresh_uuid(self):
        client = self.client()
        managed = StagingManaged(host(client), staged={"something else": str(uuid.uuid4())})
        self.assertOutcome(self.submit(managed), "submitted", "processing_confirmed")
        self.assertEqual(client.sent, [("hello", self.uuid, True)])
        self.assertEqual(managed.unstaged, ["hello"])

    def test_a_deferred_input_keeps_the_staged_uuid_for_the_next_attempt(self):
        staged = str(uuid.uuid4())
        client = self.client(send_reply={"status": "rejected", "reason": "session_starting"})
        managed = StagingManaged(host(client), staged={"hello": staged})
        self.assertOutcome(self.submit(managed), "deferred", "session_starting")
        self.assertEqual((managed.unstaged, managed.staged), ([], {"hello": staged}))

    def test_a_runtime_without_staging_is_used_as_before(self):
        client = self.client()
        self.assertOutcome(self.submit(Managed(host(client))), "submitted", "processing_confirmed")
        self.assertEqual(client.sent, [("hello", self.uuid, True)])


class RacingAClose(Fixture):
    """A send that raced the host's idle close or a close request must end as a retryable deferral, never as a lost message."""

    def race(self, ledger):
        first = self.client(send_reply=HostUnavailable("connection_lost", sent=True))
        managed = StagingManaged(host(first), None, ledger=ledger)
        return self.submit(managed), managed

    def test_a_host_that_exited_without_recording_the_message_is_a_closed_session_to_retry(self):
        for ledger in ("absent", "fenced"):
            with self.subTest(ledger=ledger):
                outcome, managed = self.race(ledger)
                self.assertOutcome(outcome, "deferred", "session_closed")
                self.assertEqual(managed.asked_ledger, [self.uuid])

    def test_a_host_that_recorded_the_message_before_exiting_is_not_called_unsent(self):
        for ledger in ("sent", None):
            with self.subTest(ledger=ledger):
                self.assertOutcome(self.race(ledger)[0], "uncertain", "host_connection_lost")

    def test_a_host_that_says_it_is_closing_or_starting_is_always_a_deferral_with_the_receipt(self):
        for reason in ("session_closing", "session_starting", "session_closed"):
            with self.subTest(reason=reason):
                outcome = self.submit(Managed(host(self.client(send_reply={"status": "rejected", "reason": reason}))))
                self.assertOutcome(outcome, "deferred", reason)
        for state, code in (("closing", "session_closing"), ("starting", "session_starting"), ("closed", "session_closed")):
            with self.subTest(state=state):
                outcome = self.submit(Managed(host(self.client(status_reply=status(state=state)))))
                self.assertOutcome(outcome, "deferred", code)


class Exit(Fixture):
    def test_exit_closes_the_session_and_is_not_sent(self):
        client = self.client()
        outcome = self.submit(Managed(host(client)), text="/exit")
        self.assertOutcome(outcome, "submitted", "session_closing")
        self.assertEqual((client.closes, client.sent), (["graceful"], []))
        self.assertEqual(self.log, ["status", "checkpoint", "close"], "claimed once, then closed")

    def test_exit_works_mid_turn_and_while_closing(self):
        for state, phase in (("ready", "working"), ("ready", "blocked"), ("closing", "closing")):
            with self.subTest(state=state, phase=phase):
                client = self.client(status_reply=status(state=state, phase=phase))
                self.assertOutcome(self.submit(Managed(host(client)), text=" /exit\n", steer=False), "submitted", "session_closing")
                self.assertEqual(client.closes, ["graceful"])

    def test_only_exactly_exit_closes(self):
        client = self.client()
        self.submit(Managed(host(client)), text="/exit now")
        self.assertEqual((client.closes, len(client.sent)), ([], 1))

    def test_exit_without_a_session_defers_and_makes_no_claim(self):
        self.assertOutcome(self.submit(Managed(None), text="/exit"), "deferred", "session_closed")
        self.assertNotIn("checkpoint", self.log)

    def test_exit_that_the_host_does_not_answer_is_uncertain(self):
        for reason, code in (("timeout", "host_timeout"), ("connection_lost", "host_connection_lost")):
            client = self.client(close_reply=HostUnavailable(reason, sent=True))
            self.assertOutcome(self.submit(Managed(host(client)), text="/exit"), "uncertain", code)
        client = self.client(close_reply=HostUnavailable("no_host", sent=False))
        self.assertOutcome(self.submit(Managed(host(client)), text="/exit"), "deferred", "session_closed")


class ThroughTheArbiter(Fixture):
    """The adapter is chosen by the binding's transport, and Chat, Telegram and a nudge reach it as before."""

    def run_input(self, input_type, steer, client, **session):
        runtime = SimpleNamespace()
        arbiter = InputArbiter(runtime, None, None, {"managed": ManagedAdapter(Managed(host(client)), new_uuid=lambda: self.uuid)})
        claimed = {}

        def checkpoint():
            claimed["row"] = {"id": "input"}
        outcome, name = arbiter._run(PROFILE, {**SESSION_ROW, **session}, "words", steer, input_type, checkpoint, claimed)
        return outcome, name, claimed

    def test_chat_steers_into_the_session(self):
        client = self.client(status_reply=status(phase="working"), send_reply={"status": "ok", "mode": "queued"})
        outcome, name, claimed = self.run_input(TYPE_CHAT, True, client)
        self.assertEqual((name, outcome.status, outcome.reason), ("managed", "submitted", "queued_mid_turn"))
        self.assertTrue(claimed)

    def test_telegram_waits_for_an_idle_session(self):
        client = self.client(status_reply=status(phase="working"))
        outcome, name, claimed = self.run_input(TYPE_TELEGRAM, False, client)
        self.assertEqual((name, outcome.status, outcome.reason, claimed), ("managed", "deferred", "turn_running", {}))
        self.assertEqual(client.sent, [])

    def test_a_nudge_is_delivered_when_the_session_is_idle(self):
        outcome, name, _ = self.run_input(TYPE_NUDGE, True, self.client())
        self.assertEqual((name, outcome.status), ("managed", "submitted"))

    def test_a_terminal_binding_still_uses_the_terminal_adapter(self):
        arbiter = InputArbiter(SimpleNamespace(), None, None, {"terminal": TerminalAdapter(lambda: None),
                                                              "managed": ManagedAdapter(Managed(None))})
        outcome, name = arbiter._run(PROFILE, {"transport": "terminal", "provider": "claude"}, "x", True, TYPE_CHAT, lambda: None, {})
        self.assertEqual((name, outcome.status, outcome.reason), ("terminal", "deferred", "terminal_unavailable"))


# ----- tmux operations never reach a managed agent ----------------------------------------------------------------------------

class Guards(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="managed guards ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.paths = SimpleNamespace(runtime=self.base / "runtime")
        self.paths.runtime.mkdir()
        self.identity = None
        self.closed = []
        self.managed = SimpleNamespace(identity=lambda agent: self.identity, close=lambda agent, mode="graceful": self.closed.append((agent, mode)))
        self.runtime = SimpleNamespace(paths=self.paths, managed_runtime=self.managed, native_chat=None)
        self.row = {"agent_id": AGENT, "profile_json": json.dumps({"runtime": "claude-code"}), "home_business_id": None}

    def switch(self):
        modes.set_mode(self.paths, AGENT, "managed")

    def backend(self):
        terminals = SimpleNamespace(runtime=self.runtime)
        backend = AgentTerminalBackend(terminals, sleep=lambda seconds: None)
        backend.tmux_touched = []
        for name in ("execution", "binding"):
            setattr(terminals, name, lambda *a, **k: backend.tmux_touched.append(name))
        return backend

    def present(self):
        self.identity = {"conversation_id": SESSION, "pid": 4343, "started": "777", "owner": "host:4242:555"}

    # the synthetic observation
    def test_a_terminal_agent_is_not_affected(self):
        backend = self.backend()
        self.assertFalse(backend.is_managed(self.row))
        self.assertFalse(backend.is_managed({}))

    def test_a_managed_agent_observes_as_absent_until_a_verified_session_exists(self):
        self.switch()
        backend = self.backend()
        self.assertEqual(backend.snapshot(self.row), {"state": "absent", "managed": True, "client": "claude", "selection_revision": None})
        self.present()
        snapshot = backend.snapshot(self.row)
        self.assertEqual((snapshot["state"], snapshot["command"], snapshot["client"], snapshot["managed"]), ("present", "claude", "claude", True))
        self.assertEqual((snapshot["session_id"], snapshot["owner"]), (SESSION, "host:4242:555"))
        self.assertEqual(backend.snapshot(self.row, client="claude"), snapshot, "stable, so a prepared command stays valid")
        self.assertEqual(backend.tmux_touched, [], "no terminal was looked at")

    def test_without_a_managed_runtime_the_agent_is_absent(self):
        self.switch()
        self.runtime.managed_runtime = None
        self.assertEqual(self.backend().snapshot(self.row)["state"], "absent")

    def test_a_direct_message_passes_validation_only_for_an_open_session(self):
        self.switch()
        backend = self.backend()
        with self.assertRaises(DomainError):
            backend.validate("ping", backend.snapshot(self.row))
        self.present()
        backend.validate("ping", backend.snapshot(self.row))

    # start and restart are refused, kill closes
    def test_start_and_restart_are_refused_whatever_the_session_state(self):
        self.switch()
        backend = self.backend()
        for present in (False, True):
            if present:
                self.present()
            for action in ("start", "restart"):
                with self.subTest(action=action, present=present):
                    with self.assertRaises(DomainError) as raised:
                        backend.validate(action, backend.snapshot(self.row))
                    self.assertEqual(raised.exception.code, "managed_agent")

    def test_a_terminal_snapshot_validates_exactly_as_before(self):
        AgentTerminalBackend.validate("start", {"state": "absent"})
        AgentTerminalBackend.validate("restart", {"state": "present", "command": "claude", "client": "claude"})
        with self.assertRaises(DomainError) as raised:
            AgentTerminalBackend.validate("start", {"state": "present", "command": "claude"})
        self.assertEqual(raised.exception.code, "invalid_transition")

    def test_a_start_that_reaches_the_executor_never_opens_a_terminal(self):
        self.switch()
        self.present()
        backend = self.backend()
        expected = backend.snapshot(self.row)
        opened = []
        backend.terminals.open = lambda *a, **k: opened.append(a)
        for action in ("start", "restart"):
            with self.assertRaises(DomainError) as raised:
                backend._execute(self.row, action, None, expected, None, None)
            self.assertEqual(raised.exception.code, "managed_agent")
        self.assertEqual((opened, backend.tmux_touched), ([], []))

    def test_a_command_prepared_for_a_terminal_agent_is_stale_once_it_is_switched(self):
        backend = self.backend()
        terminal_snapshot = {"state": "absent", "client": "claude", "selection_revision": None}
        self.switch()
        with self.assertRaises(DomainError) as raised:
            backend._execute(self.row, "start", None, terminal_snapshot, None, None)
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(backend.tmux_touched, [])

    def test_kill_closes_the_managed_session_without_touching_tmux(self):
        self.switch()
        self.present()
        backend = self.backend()
        detail = backend._execute(self.row, "kill", None, backend.snapshot(self.row), None, None)
        self.assertIn("close requested", detail)
        self.assertEqual(self.closed, [(AGENT, "now")])
        self.assertEqual(backend.tmux_touched, [])

    def test_kill_without_a_managed_runtime_fails_cleanly(self):
        self.switch()
        self.present()
        backend = self.backend()
        expected = backend.snapshot(self.row)
        self.runtime.managed_runtime = None
        self.identity = None
        with self.assertRaises(DomainError):
            backend._execute(self.row, "kill", None, {**expected, "state": "absent"}, None, None)

    def test_ping_goes_through_the_shared_arbiter(self):
        self.switch()
        self.present()
        backend = self.backend()
        sent = []
        backend._ping = lambda row, message, key, client: sent.append((message, key, client)) or "sent"
        self.assertEqual(backend._execute(self.row, "ping", "hi", backend.snapshot(self.row), "key-1", None), "sent")
        self.assertEqual(sent, [("hi", "key-1", "claude")])

    def test_no_tmux_pane_is_selected_observed_or_typed_into_for_a_managed_agent(self):
        self.switch()
        backend = self.backend()
        with self.assertRaises(DomainError) as raised:
            backend.selected(self.row)
        self.assertEqual(raised.exception.code, "managed_agent")
        self.assertEqual(backend.pane_observation(self.row, "claude"), {"state": "absent", "managed": True})
        backend.selected_client = lambda row: "claude"
        outcome = backend.owner_input(self.row, "text", workdir=self.base, checkpoint=lambda: self.fail("claimed"))
        self.assertIsInstance(outcome, OwnerInputOutcome)
        self.assertEqual(outcome.status, "deferred")

    def test_a_failed_start_is_recorded_as_failed_not_uncertain(self):
        self.assertEqual(terminal_control.execution_outcome(DomainError("managed_agent", "No"))[0], "failed")
        self.assertEqual(terminal_control.execution_outcome(DomainError("not_delivered", "No"))[0], "failed")
        self.assertEqual(terminal_control.execution_outcome(DomainError("operation_failed", "No"))[0], "uncertain")
        self.assertEqual(terminal_control.execution_outcome(RuntimeError())[0], "uncertain")

    # opening a terminal
    def service(self):
        service = TerminalService.__new__(TerminalService)
        service.runtime = self.runtime
        service.agent = lambda context, selector: (SimpleNamespace(agent_id=AGENT), {"agent_id": AGENT})
        service.execution = lambda business: self.fail("a managed agent's terminal was looked up")
        service.binding = lambda *a: self.fail("a managed agent's terminal was bound")
        service._prepare = lambda *a: self.fail("a managed agent was prepared for a terminal")
        return service

    def test_no_terminal_is_opened_for_a_managed_agent(self):
        self.switch()
        service = self.service()
        for reconnect in (False, True):
            with self.assertRaises(DomainError) as raised:
                service._open_admitted(None, None, "agent", cols=80, rows=24, execution=None, reconnect=reconnect)
            self.assertEqual(raised.exception.code, "managed_agent")

    def test_a_reconnect_is_refused_before_any_attachment_is_inspected(self):
        self.switch()
        service = self.service()
        service.attachment = lambda: SimpleNamespace(state=lambda agent: self.fail("inspected"))
        with self.assertRaises(DomainError) as raised:
            service.reconnect_selected(None, None, "agent", None, "1")
        self.assertEqual(raised.exception.code, "managed_agent")

    def test_a_broadcast_reaches_a_managed_agent_only_through_the_arbiter(self):
        self.switch()
        service = self.service()
        row = {"agent_id": AGENT, "home_business_id": None, "status": "active"}
        uow = SimpleNamespace(repository=lambda name: SimpleNamespace(get=lambda key: None),
                              connection=SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: row)))
        service.runtime = SimpleNamespace(paths=self.paths, database=SimpleNamespace(read=lambda: _Context(uow)))
        from qorqut.transport import terminals
        original = terminals.active_global
        terminals.active_global = lambda connection, agent: {"role": {"id": "x"}}
        self.addCleanup(setattr, terminals, "active_global", original)
        service._native_input = lambda agent, data, key: {"state": "delivered", "via": "arbiter"}
        self.assertEqual(service.input_existing(None, AGENT, "hi", "key-1"), {"state": "delivered", "via": "arbiter"})
        result = service.input_existing(None, AGENT, "hi")
        self.assertEqual(result["state"], "unavailable", "no keyless tmux fallback")

    def test_a_terminal_agent_is_opened_as_before(self):
        service = self.service()
        self.assertIsNone(service.refuse_managed({"agent_id": AGENT}))
        self.assertIsNone(service.refuse_managed({}))
        self.assertFalse(service.is_managed(AGENT))


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
