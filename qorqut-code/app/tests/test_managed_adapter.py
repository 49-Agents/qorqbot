"""``ManagedAdapter`` (native_chat/adapters.py) against a stand-in host client.

Nothing here starts a host, a socket or ``claude``: the client records each call in one shared log, so "the checkpoint ran once
and before the send" is an assertion about that log. The outcome codes are the managed side of the table in
docs/MANAGED-RUNTIME-STEP3-DESIGN.md, section 2.
"""
import json
from types import SimpleNamespace
import unittest

import uuid

from qorqut.managed_runtime import manager as hosts
from qorqut.managed_runtime.client import HostUnavailable
from qorqut.native_chat.adapters import ManagedAdapter
from qorqut.native_chat.arbiter import InputArbiter
from qorqut.native_chat.contracts import TYPE_CHAT, TYPE_NUDGE, TYPE_TELEGRAM, OwnerInputOutcome

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

    def test_the_profile_runtime_does_not_decide_the_client_does(self):
        # The profile says codex (or nothing, or garbage) but the bound session is Claude's: the input is delivered.
        for profile in ({**PROFILE, "profile_json": json.dumps({"runtime": "codex"})}, {**PROFILE, "profile_json": json.dumps({})},
                        {"agent_id": AGENT, "profile_json": "not json"}):
            with self.subTest(profile=profile["profile_json"]):
                outcome = self.submit(Managed(host(self.client())), profile=profile)
                self.assertEqual((outcome.status, outcome.receipt["client"]), ("submitted", "claude"))

    def test_an_unknown_client_is_unsupported(self):
        self.assertOutcome(self.submit(Managed(host(self.client())), session={**SESSION_ROW, "provider": "gemini"}),
                           "failed", "unsupported_runtime")
        self.assertUntouched()

    def test_a_client_with_a_driver_is_delivered_with_its_own_receipt(self):
        outcome = self.submit(Managed(host(self.client())), session={**SESSION_ROW, "provider": "codex"},
                              profile={**PROFILE, "profile_json": json.dumps({"runtime": "codex"})})
        self.assertEqual((outcome.status, outcome.receipt["client"], outcome.receipt["transport"]), ("submitted", "codex", "managed"))


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
    """Chat, Telegram and a nudge reach the one adapter."""

    def run_input(self, input_type, steer, client, **session):
        runtime = SimpleNamespace()
        arbiter = InputArbiter(runtime, None, None, ManagedAdapter(Managed(host(client)), new_uuid=lambda: self.uuid))
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


class RowThatIsNotManaged(Fixture):
    def test_a_row_that_is_not_a_managed_binding_is_deferred_unsupported(self):
        arbiter = InputArbiter(SimpleNamespace(), None, None, ManagedAdapter(Managed(None)))
        outcome, name = arbiter._run(PROFILE, {"transport": "terminal", "provider": "claude"}, "x", True, TYPE_CHAT, lambda: None, {})
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "unsupported_transport"))


class CodexCommands(Fixture):
    """Codex's slash commands are real driver operations (``compact``, ``model``), never text typed into the thread."""

    CODEX = {**SESSION_ROW, "provider": "codex"}

    class CommandClient(Client):
        def __init__(self, log, reply=None):
            super().__init__(log)
            self.commands = []
            self.command_reply = {"status": "ok", "name": "compact", "accepted": True} if reply is None else reply

        def command(self, name, **arguments):
            self.log.append("command")
            self.commands.append((name, arguments))
            return self._result(self.command_reply)

    def run_text(self, text, client):
        return self.submit(Managed(host(client)), text=text, session=self.CODEX)

    def test_compact_is_the_drivers_compact_and_is_not_sent_as_text(self):
        client = self.CommandClient(self.log)
        outcome = self.run_text("/compact", client)
        self.assertOutcome(outcome, "submitted", "command_accepted")
        self.assertEqual((client.commands, client.sent), ([("compact", {})], []))

    def test_model_carries_model_and_effort_together(self):
        client = self.CommandClient(self.log, {"status": "ok", "name": "model", "accepted": True})
        self.assertOutcome(self.run_text("/model gpt-x high", client), "submitted", "command_accepted")
        self.assertEqual(client.commands, [("model", {"model": "gpt-x", "effort": "high"})])

    def test_a_running_turn_defers_a_command(self):
        client = self.CommandClient(self.log, {"status": "ok", "name": "compact", "accepted": False})
        self.assertOutcome(self.run_text("/compact", client), "deferred", "turn_running")

    def test_a_refused_command_fails_and_a_lost_connection_is_uncertain(self):
        refused = self.CommandClient(self.log, {"status": "error", "code": "unsupported_command"})
        self.assertOutcome(self.run_text("/compact", refused), "failed", "command_refused")
        lost = self.CommandClient(self.log, HostUnavailable("lost", sent=True))
        self.assertEqual(self.run_text("/compact", lost).status, "uncertain")

    def test_ordinary_text_and_claude_commands_are_not_commands(self):
        client = self.CommandClient(self.log)
        self.assertEqual(self.run_text("compact the notes please", client).status, "submitted")
        self.assertEqual(client.commands, [])
        claude = self.CommandClient(self.log)
        self.submit(Managed(host(claude)), text="/compact")
        self.assertEqual(claude.commands, [], "Claude takes its slash commands as the text it understands")


if __name__ == "__main__":
    unittest.main()
