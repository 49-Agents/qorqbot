"""Signoffs in Telegram against the real canonical SignoffService: posts, thumbs, replies, retirement and cleanup.

Only the Telegram API and the terminal are injected; decisions, notes, notifications, policy and the inbox are the real
shared service in an isolated database.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
from types import SimpleNamespace

import pytest

from bazarlyk.application.engine import Application
from qorqut_operations.contracts import new_id
from bazarlyk.telegram import store
from bazarlyk.telegram.bridge import TelegramBridge
from telegram_support import CHAT_IDS, GROUP_ID, OWNER_TG, ROLES, TOPIC_CHAT_IDS, Clock, Env, FakeBotApi, TOKEN, write_private
from test_signoff_inputs import FORM, create as create_input
from test_signoffs import Scenario
from test_telegram_owner_input import FakeTerminal

BLOCKING = {"notify": True, "blocking": True, "reason": "Need access"}
URGENT = {"notify": True, "urgent": True, "reason": "Deadline today"}
ORDINARY = {"notify": True}
BINARY = {"title": "Ship it?", "explanation": "Should the release ship today?", "kind": "question", "selection_mode": "single",
          "selection_required": False, "notes_required": False,
          "choices": [{"id": "yes", "label": "Yes", "explanation": "Ship it"}, {"id": "no", "label": "No", "explanation": "Hold it"}]}
FEEDBACK = "Owner reacted 👎 in Telegram"


class World(Env):
    """One installation: the real Signoff service plus a bridge that owns its Signoffs chat."""

    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.s = s = Scenario(tmp_path)
        for name in ("signoffs_input_create", "signoffs_input_cancel", "signoffs_attention_set"):
            s.change_override(name, "allow")
        from bazarlyk.transport.auth import human_global_capabilities
        s.policy.human_global_capabilities = human_global_capabilities
        self.database = s.db
        self.clock = Clock()
        self.api = FakeBotApi(TOPIC_CHAT_IDS, forum_chats={GROUP_ID}) if self.topics else FakeBotApi()
        self.agents = {}
        s.service.clock = lambda: store.iso(self.clock())
        self.installation_id, self.owner_id = new_id(), s.owner.id
        with s.db.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": self.installation_id, "owner_principal_id": s.owner.id})
            for name, role in ROLES.items():
                identity = new_id()
                self.agents[name] = identity
                uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": name.title()})
                uow.repository("agent_profiles", business_id=None).insert({
                    "agent_id": identity, "principal_id": identity, "namespace": "ecosystem", "name": name,
                    "workspace": str(self.tmp / "agents" / name), "status": "active",
                    "profile_json": {"role": {"id": role}, "display_name": name.title()}})
        self.token_file = write_private(self.tmp / "secrets" / "bot-token", TOKEN + "\n")
        self.config_path = self.tmp / "config" / "telegram.json"
        self.write_config()
        self.runtime = SimpleNamespace(database=s.db, application=s.app, signoffs=s.service, root=self.tmp / "root",
                                       workspace=self.tmp / "workspace", registry=s.registry, telegram=None)
        self.state_dir = self.tmp / "state" / "telegram"
        self.rng_seed, self.poll_timeout = 1, 0.05
        self.bridges = []
        self.bridge = self.runtime.telegram = self.make_bridge()
        self.bridge.owner_terminal = FakeTerminal()
        from bazarlyk.telegram.operations import TelegramOperations
        TelegramOperations(self.runtime).register()
        self.ready()

    def resolve(self, message_id, action, **arguments):
        outbox = self.one("SELECT id, revision FROM telegram_outbox WHERE message_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                          message_id)
        result = self.s.app.invoke(self.s.owner_context, "telegram_delivery_resolve",
                                   {"outbox_id": outbox["id"], "action": action, "expected_revision": outbox["revision"], **arguments},
                                   {"idempotency_key": new_id()})
        assert result.status == "ok", result.to_dict()

    def close(self):
        self.s.service.stop()
        super().close()

    # ----- driving ------------------------------------------------------------------------------------

    def tick(self):
        self.s.service.notifications.tick()

    def pump(self, limit=40):
        """Run control cycles until nothing is left to do."""
        for _ in range(limit):
            if self.bridge.cycle() <= self.bridge.tick:
                return
        raise AssertionError("the bridge never went idle")

    def settle(self):
        self.tick()
        self.pump()

    def say(self, update):
        self.api.push(update)
        self.bridge.poll_once()
        self.pump()

    def react(self, post, *emoji, old=(), **kwargs):
        self.say(self.reaction("signoffs", self.provider(post), list(emoji), old=old, **kwargs))

    def reply(self, post, text, **kwargs):
        self.say(self.message("signoffs", text, reply_to=self.provider(post), **kwargs))

    # ----- reads --------------------------------------------------------------------------------------

    def posts(self, signoff_id=None):
        rows = self.rows("SELECT * FROM telegram_signoff_posts ORDER BY created_at, generation, rowid")
        for row in rows:
            row["decision_json"] = json.loads(row["decision_json"]) if row["decision_json"] else None
        return [row for row in rows if signoff_id in (None, row["signoff_id"])]

    def post(self, signoff_id=None, **where):
        found = [p for p in self.posts(signoff_id) if all(p[key] == value for key, value in where.items())]
        assert found, self.posts()
        return found[-1]

    def provider(self, post):
        return self.one("SELECT telegram_message_id FROM telegram_messages WHERE id=?", post["message_id"])["telegram_message_id"]

    def signoff(self, signoff_id):
        return self.s.detail({"id": signoff_id})

    def owner_notes(self, signoff_id):
        return [row["body"] for row in self.rows("SELECT body FROM signoff_notes WHERE signoff_id=? ORDER BY sequence", signoff_id)]

    def notices(self):
        return [json.loads(r["payload_json"])["text"] for r in self.rows(
            "SELECT payload_json FROM telegram_outbox WHERE source_kind='notice' ORDER BY created_at, rowid")]

    def deleted_ids(self):
        return [message_id for _, message_id in self.api.deleted]

    def handoffs(self):
        return self.rows("SELECT * FROM telegram_owner_inputs WHERE purpose='signoff_reply' ORDER BY created_at, rowid")

    def set_attention(self, signoff, **attention):
        result = self.s.app.invoke(self.s.agent_context(), "signoffs_attention_set", {
            "signoff_id": signoff["id"], "attention": {**attention}}, {"idempotency_key": new_id()})
        assert result.status == "ok", result.to_dict()

    def action(self, title="Task", *, options=None, **attention):
        proposal = self.s.propose(title, options=options)
        self.set_attention(proposal, **(attention or URGENT))
        return self.signoff(proposal["id"])

    def decisions(self):
        return {row["id"]: (row["state"], json.loads(row["decision_json"]) if row["decision_json"] else None)
                for row in self.rows("SELECT id, state, decision_json FROM signoffs")}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def only_post(w, signoff_id=None):
    posts = [p for p in w.posts(signoff_id) if p["state"] in ("queued", "active") and p["input_form"] != "status"]
    assert len(posts) == 1, w.posts()
    return posts[0]


# ----- posts ----------------------------------------------------------------------------------------------------


def test_a_blocking_input_request_gets_one_exact_post_in_signoffs_and_never_a_duplicate(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    assert len(w.api.sent) == 1
    sent = w.api.sent[0]
    assert sent["chat_id"] == CHAT_IDS["signoffs"] and sent["reply_to"] is None
    for expected in ("Question: Choose a supplier", "one · requested by Business-Agent", "Which supplier should we use?",
                     "a: First supplier", "Available today", "Why now: Need access", "Open the Signoffs view: http://qorqut:1071/#one",
                     "needs a choice, which can only be selected in the dashboard"):
        assert expected in sent["text"], sent["text"]
    assert "Truncated" not in sent["text"]
    post = only_post(w)
    assert (post["state"], post["input_form"], post["generation"], post["category"]) == ("active", "input", 1, "input_request")
    assert (post["proposal_revision"], post["payload_hash"], post["expected_status_revision"]) == (
        row["proposal_revision"], row["payload_hash"], row["status_revision"])
    assert post["refresh_at"] == store.plus(w.bridge.now(), 24 * 3600)
    for _ in range(3):
        w.settle()
    assert len(w.api.sent) == 1 and len(w.posts()) == 1


def test_an_action_post_shows_the_exact_prepared_change_and_says_when_it_is_truncated(world):
    w = world
    proposal = w.action("Create the launch checklist")
    w.settle()
    text = w.api.sent[0]["text"]
    assert text.startswith("Approval: Create task") and "Create the launch checklist prepared" in text
    assert "Prepared task rationale" in text and "React 👍 to approve or 👎 to reject" in text
    assert "Deadline today" in text and "http://qorqut:1071/#one" in text and "Truncated" not in text
    assert only_post(w)["input_form"] == "action" and proposal["id"] == only_post(w)["signoff_id"]
    long = w.action("y" * 3000)
    w.settle()
    clipped = w.api.sent[-1]["text"]
    assert len(clipped) <= 3800 and "Truncated: the complete text is in the Signoffs view: http://qorqut:1071/#one" in clipped
    assert only_post(w, long["id"])["input_form"] == "action"


def test_ordinary_items_are_posted_at_summary_time_each_as_its_own_post(world):
    w = world
    w.clock.now = datetime(2026, 10, 1, 11, 59, tzinfo=timezone.utc)
    first = create_input(w.s, attention=ORDINARY)
    second = create_input(w.s, request_key="other", form={**FORM, "title": "Another question"}, attention=ORDINARY)
    w.settle()
    assert w.api.sent == [] and w.posts() == []
    w.clock.advance(60)
    w.settle()
    assert len(w.api.sent) == 2
    assert {p["signoff_id"] for p in w.posts()} == {first["id"], second["id"]}
    assert {p["notification_id"] for p in w.posts()} != {None} and len({p["message_id"] for p in w.posts()}) == 2


def test_items_without_notification_attention_or_before_activation_history_get_no_post(world):
    w = world
    create_input(w.s, request_key="quiet")
    create_input(w.s, request_key="not-urgent", attention={"notify": False})
    w.settle()
    assert w.api.sent == [] and w.posts() == []
    assert w.bridge.status()["signoffs"] == {"active_posts": 0, "queued_posts": 0, "pending_decisions": 0, "held_replies": 0}


# ----- thumbs on action approvals -------------------------------------------------------------------------------


def test_thumbs_up_approves_the_exact_frozen_proposal_once_then_retires_and_deletes_the_post(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    w.react(post, "👍")
    state, decision = w.decisions()[proposal["id"]]
    assert state == "approved" and decision["kind"] == "approve" and decision["by"] == w.s.owner.id
    assert decision["message_id"] is None  # no feedback was invented for an approval
    retired = w.post(proposal["id"])
    assert retired["state"] == "retired" and retired["retire_reason"] == "resolved"
    assert retired["decision_json"]["state"] == "done" and retired["decision_json"]["decision"] == "approve"
    assert (CHAT_IDS["signoffs"], provider) in w.api.deleted
    receipts = w.rows("SELECT idempotency_key FROM idempotency_receipts WHERE operation_name='signoffs_review'")
    assert [r["idempotency_key"] for r in receipts] == [f"telegram:{post['id']}:reaction"]
    # A repeated reaction update and a later stray reaction change nothing.
    before = w.decisions()
    w.react(post, "👍", old=["👍"])
    w.react(post, "👎")
    assert w.decisions() == before and len(w.rows("SELECT * FROM idempotency_receipts WHERE operation_name='signoffs_review'")) == 1


def test_thumbs_down_rejects_with_the_fixed_feedback_only_when_a_message_is_required(world):
    w = world
    proposal = w.action("First")
    w.settle()
    w.react(only_post(w), "👎")
    state, decision = w.decisions()[proposal["id"]]
    assert state == "rejected" and decision["kind"] == "reject"
    assert w.owner_notes(proposal["id"]) == [FEEDBACK]
    assert w.signoff(proposal["id"])["decision"]["feedback"] == FEEDBACK
    # With an owner note already present the rejection needs no invented feedback.
    second = w.action("Second")
    w.settle()
    post = only_post(w, second["id"])
    w.reply(post, "Please rework the scope first")
    assert w.owner_notes(second["id"]) == ["Please rework the scope first"]
    # The reply retired the post after the durable handoff; a fresh generation comes with the next reminder.
    assert w.post(second["id"])["state"] == "retired" and w.post(second["id"])["retire_reason"] == "replied"
    assert w.decisions()[second["id"]][0] == "pending"


def test_a_reaction_that_is_not_one_unambiguous_owner_thumb_on_a_known_active_post_is_ignored(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    provider = w.provider(post)

    def outcome():
        return w.one("SELECT state, reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")

    w.say(w.reaction("signoffs", provider, ["👍", "👎"]))
    assert outcome()["reason"] == "ambiguous_reaction"
    assert sum("nothing was decided" in text and "Both 👍 and 👎" in text for text in w.notices()) == 1
    assert w.decisions()[proposal["id"]][0] == "pending" and w.post(proposal["id"])["decision_json"] is None
    w.say(w.reaction("signoffs", provider, [], old=["👍"]))
    assert outcome()["reason"] == "not_a_decision_reaction"
    w.say(w.reaction("signoffs", provider, ["❤"]))
    assert outcome()["reason"] == "not_a_decision_reaction"
    w.say(w.reaction("signoffs", provider, ["👍"], actor_chat=True))
    assert outcome()["reason"] == "anonymous_actor"
    w.say(w.reaction("signoffs", provider, ["👍"], sender=OWNER_TG + 1))
    assert outcome()["reason"] == "not_owner"
    w.say({"update_id": w.next_update_id(), "message_reaction_count": {"chat": {"id": CHAT_IDS["signoffs"]}, "message_id": provider}})
    assert outcome()["reason"] == "message_reaction_count"
    w.say(w.reaction("signoffs", provider + 999, ["👍"]))
    assert outcome()["reason"] == "unknown_post"
    w.say(w.reaction("founder", provider, ["👍"]))
    assert outcome()["reason"] == "no_reaction_route"
    assert w.decisions()[proposal["id"]][0] == "pending" and only_post(w)["decision_json"] is None


def test_a_reaction_before_activation_is_ignored_and_a_reaction_on_a_bridge_notice_or_status_post_decides_nothing(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.say(w.reaction("signoffs", w.provider(post), ["👍"], at=w.clock.now - timedelta(hours=3)))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "before_activation"
    with w.bridge.write("test") as uow:
        from bazarlyk.telegram.outbox import enqueue_text
        enqueue_text(w.bridge, uow, w.bridge.config.chat("signoffs"), text="Just a note", purpose="notice", source_kind="notice", source_key="n1")
    w.pump()
    notice = w.api.sent[-1]["message_id"]
    w.say(w.reaction("signoffs", notice, ["👍"]))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "unknown_post"
    assert w.decisions()[proposal["id"]][0] == "pending"


def test_a_post_the_dashboard_already_decided_cannot_decide_again_and_is_retired_and_deleted(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    result = w.s.decide(proposal, "reject", feedback="Rejected in the dashboard")
    assert result.status == "ok"
    w.pump()
    retired = w.post(proposal["id"])
    assert retired["state"] == "retired" and retired["retire_reason"] == "resolved"
    assert (CHAT_IDS["signoffs"], provider) in w.api.deleted
    w.react(post, "👍")
    assert w.decisions()[proposal["id"]][0] == "rejected"
    stale = [text for text in w.notices() if "decided nothing" in text]
    assert len(stale) == 1 and "it is no longer pending" in stale[0] and "Open the Signoffs view (it lists what is current there): http://qorqut:1071" in stale[0]


def test_a_superseded_proposal_retires_its_post_and_the_replacement_gets_its_own(world):
    w = world
    first = w.action("Version one")
    w.settle()
    old = only_post(w, first["id"])
    second = w.action("Version two", options={"supersedes_signoff_id": first["id"],
                                              "expected_superseded_status_revision": first["status_revision"]})
    w.settle()
    assert w.post(first["id"])["retire_reason"] == "resolved" and (CHAT_IDS["signoffs"], w.provider(old)) in w.api.deleted
    assert only_post(w, second["id"])["payload_hash"] == second["payload_hash"]
    w.react(old, "👍")
    assert w.decisions()[first["id"]][0] == "conflicted" and w.decisions()[second["id"]][0] == "pending"
    assert w.signoff(first["id"])["superseded_by_id"] == second["id"]
    w.react(only_post(w, second["id"]), "👍")
    assert w.decisions()[second["id"]][0] == "approved"


def test_a_post_whose_signoff_resolved_before_it_was_sent_is_never_sent(world):
    w = world
    proposal = w.action()
    w.tick()
    w.bridge.signoff_bridge.run()
    assert w.api.sent == [] and len(w.posts()) == 1 and w.posts()[0]["state"] == "queued"
    w.s.decide(proposal, "reject", feedback="No")
    w.bridge.dispatcher.run()
    assert w.api.sent == []
    outbox = w.one("SELECT state, error_code FROM telegram_outbox WHERE source_kind='signoff'")
    assert (outbox["state"], outbox["error_code"]) == ("cancelled", "post_stale")
    w.pump()
    assert w.post(proposal["id"])["state"] == "retired" and w.api.sent == []


# ----- input requests -------------------------------------------------------------------------------------------


def test_thumbs_answer_only_the_unambiguous_yes_no_form_and_never_any_other_input_form(world):
    w = world
    binary = create_input(w.s, request_key="ship", form=BINARY, attention=BLOCKING)
    other = create_input(w.s, request_key="supplier", attention=BLOCKING)
    w.settle()
    binary_post, other_post = only_post(w, binary["id"]), only_post(w, other["id"])
    assert (binary_post["input_form"], other_post["input_form"]) == ("binary_input", "input")
    assert "React 👍 for Yes or 👎 for No" in w.one("SELECT body FROM telegram_messages WHERE id=?", binary_post["message_id"])["body"]
    assert "React 👍" not in w.one("SELECT body FROM telegram_messages WHERE id=?", other_post["message_id"])["body"]
    w.react(other_post, "👍")
    assert w.decisions()[other["id"]][0] == "pending" and any("needs a written answer" in n and "answer it in the Signoffs view of the dashboard: http://qorqut:1071" in n for n in w.notices())
    w.react(binary_post, "👎")
    state, decision = w.decisions()[binary["id"]]
    assert state == "answered" and decision["kind"] == "respond" and tuple(decision["selected_choices"]) == ("no",)
    assert not w.signoff(binary["id"])["decision"].get("notes")


NOTES_ONLY = {**FORM, "selection_required": False}


def test_a_reply_answers_an_input_request_with_the_exact_text_and_hands_it_to_the_proposer(world):
    w = world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    text = "Use the first supplier,\r\nbut only for the trial order.  "
    w.reply(post, text)
    state, decision = w.decisions()[row["id"]]
    assert state == "answered" and tuple(decision["selected_choices"]) == ()
    assert w.signoff(row["id"])["decision"]["notes"] == "Use the first supplier,\nbut only for the trial order.  "
    assert w.post(row["id"])["state"] == "retired" and w.post(row["id"])["retire_reason"] == "resolved"
    handoff = w.handoffs()[0]
    assert handoff["agent_id"] == w.s.agent.id and handoff["signoff_id"] == row["id"] and handoff["purpose"] == "signoff_reply"
    assert handoff["correlation_id"] == decision["message_id"] and handoff["expires_at"] is None
    # The canonical inbox event exists for the proposer, and the owner's reply and the bot post are queued for deletion.
    events = w.rows("SELECT * FROM inbox_events WHERE source_type='signoffs'")
    assert events
    reply = w.one("SELECT * FROM telegram_messages WHERE purpose='signoff_reply'")
    assert reply["reply_to_id"] == post["message_id"] and reply["body"].startswith("Use the first supplier,\nbut only")
    jobs = {r["message_id"]: r["reason"] for r in w.rows("SELECT message_id, reason FROM telegram_cleanup_jobs")}
    assert reply["id"] in jobs and post["message_id"] in jobs
    assert (CHAT_IDS["signoffs"], reply["telegram_message_id"]) in w.api.deleted
    # The direct path types the same saved text into the proposer's terminal once, guarded, with the Signoff preface.
    terminal = w.bridge.owner_terminal
    assert len(terminal.calls) == 1 and terminal.calls[0]["agent"] == w.s.agent.id
    typed = terminal.calls[0]["text"]
    assert typed.startswith("Qorqut owner reply via Telegram on your Signoff") and "Choose a supplier" in typed
    assert "Use the first supplier,\nbut only for the trial order.  " in typed and decision["message_id"] in typed
    assert w.handoffs()[0]["state"] == "submitted"


def test_a_reply_never_answers_a_question_whose_required_choice_is_unresolved(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    assert "needs a choice" in w.api.sent[0]["text"] and "leaves the question open" in w.api.sent[0]["text"]
    w.reply(post, "The second one, I think")
    assert w.decisions()[row["id"]][0] == "pending" and w.signoff(row["id"])["decision"] is None
    assert w.owner_notes(row["id"]) == ["The second one, I think"]
    assert w.post(row["id"])["retire_reason"] == "replied" and len(w.handoffs()) == 1


def test_a_written_reply_that_alone_cannot_answer_is_a_canonical_comment_and_the_request_stays_pending(world):
    w = world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    w.reply(post, "x" * 8001)
    assert w.decisions()[row["id"]][0] == "pending"
    assert w.owner_notes(row["id"]) == ["x" * 8001]
    assert w.post(row["id"])["state"] == "retired" and w.post(row["id"])["retire_reason"] == "replied"
    assert len(w.handoffs()) == 1 and w.handoffs()[0]["agent_id"] == w.s.agent.id
    # The same event never re-posts the item; the next hourly reminder posts a fresh generation.
    w.pump()
    w.pump()
    assert len(w.api.sent) == 1
    w.clock.advance(3600)
    w.settle()
    assert len(w.api.sent) == 2 and only_post(w)["generation"] == 2


def test_a_reply_to_an_action_approval_is_always_a_comment_and_never_a_decision(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.reply(post, "approve")
    assert w.decisions()[proposal["id"]][0] == "pending"
    assert w.owner_notes(proposal["id"]) == ["approve"]
    assert w.post(proposal["id"])["retire_reason"] == "replied" and len(w.handoffs()) == 1
    assert w.signoff(proposal["id"])["decision"] is None


def test_a_reply_to_a_stale_or_retired_post_is_recorded_as_a_comment_and_cannot_decide(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    w.s.app.invoke(w.s.agent_context(), "signoffs_input_cancel", {"signoff_id": row["id"]}, {"idempotency_key": new_id()})
    w.pump()
    assert w.post(row["id"])["state"] == "retired"
    w.say(w.message("signoffs", "Too late but noted", reply_to=provider))
    assert w.decisions()[row["id"]][0] == "cancelled"
    assert w.owner_notes(row["id"]) == ["Too late but noted"]
    assert len(w.handoffs()) == 1
    explanation = [text for text in w.notices() if "saved as a comment on the original Signoff" in text]
    assert len(explanation) == 1 and "decided nothing" in explanation[0] and "no longer pending" in explanation[0]
    assert "Open the Signoffs view (it lists what is current there): http://qorqut:1071/#one" in explanation[0]


def test_a_reply_for_an_inactive_proposer_is_saved_reported_and_nothing_is_deleted(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (w.s.agent.id,))
    w.reply(post, "x" * 8001)
    assert w.owner_notes(row["id"]) == ["x" * 8001]
    assert w.handoffs() == []
    assert any("proposer is not active" in n for n in w.notices())
    assert w.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 0
    assert w.post(row["id"])["state"] == "active"


def test_a_reply_to_a_stale_post_for_an_inactive_proposer_is_held_and_handed_off_once_the_proposer_returns(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    w.s.app.invoke(w.s.agent_context(), "signoffs_input_cancel", {"signoff_id": row["id"]}, {"idempotency_key": new_id()})
    w.pump()
    assert w.post(row["id"])["state"] == "retired"
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (w.s.agent.id,))
    update = w.message("signoffs", "Late but please note it", reply_to=provider)
    w.say(update)
    reply_id = update["message"]["message_id"]
    assert w.owner_notes(row["id"]) == ["Late but please note it"]
    held = json.loads(w.post(row["id"])["handoff_json"])
    assert len(held) == 1 and w.handoffs() == []
    assert any("proposer is not active" in n for n in w.notices())
    assert (CHAT_IDS["signoffs"], reply_id) not in w.api.deleted  # the owner's reply is kept while nobody can receive it
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (w.s.agent.id,))
    w.clock.advance(w.bridge.timings["sweep_interval"])
    for _ in range(4):
        w.pump()
    assert len(w.handoffs()) == 1 and w.post(row["id"])["handoff_json"] is None
    assert w.post(row["id"])["state"] == "retired" and w.post(row["id"])["retire_reason"] != "replied"
    assert (CHAT_IDS["signoffs"], reply_id) in w.api.deleted
    for _ in range(3):
        w.pump()
    assert len(w.handoffs()) == 1  # once


def test_replies_outside_signoff_posts_are_hinted_and_never_reach_any_agent(world):
    w = world
    create_input(w.s, attention=BLOCKING)
    w.settle()
    w.say(w.message("signoffs", "hello there"))
    assert any("Reply directly to a Signoff post" in n for n in w.notices())
    w.say(w.message("signoffs", "reply to the void", reply_to=123456))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "not_a_signoff_reply"
    assert w.rows("SELECT * FROM agent_messages") == [] and w.handoffs() == []


def test_unsafe_or_empty_signoff_replies_are_refused_with_a_notice_and_decide_nothing(world):
    w = world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    for text, marker in (("bad\x1b[0m", "control characters"), ("z" * 16001, "longer than 16000"), ("  \n ", "no text")):
        w.reply(post, text)
        assert any(marker in n for n in w.notices())
    assert w.decisions()[row["id"]][0] == "pending" and w.owner_notes(row["id"]) == [] and w.post(row["id"])["decision_json"] is None
    w.say(w.message("signoffs", "see photo", reply_to=w.provider(post), extra={"photo": [{"file_id": "F"}]}))
    assert w.decisions()[row["id"]][0] == "answered"
    assert "[Telegram attachment not delivered: photo]" in w.signoff(row["id"])["decision"]["notes"]
    assert sum("Attachments are not supported" in n for n in w.notices()) == 1


# ----- idempotency and recovery ---------------------------------------------------------------------------------


def test_a_duplicate_update_is_one_effect_and_a_decision_committed_before_a_crash_is_replayed_not_repeated(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    update = w.reaction("signoffs", w.provider(post), ["👍"])
    w.api.push(update)
    w.api.push(dict(update))
    w.bridge.poll_once()
    assert w.one("SELECT count(*) n FROM telegram_updates")["n"] == 1
    # The canonical decision commits, then the process dies before the intent is closed.
    intent = w.post(proposal["id"])["decision_json"]
    assert intent["state"] == "pending"
    result = w.bridge.signoff_bridge._call(w.post(proposal["id"]), intent, w.s.service.get(w.s.owner_context, proposal["id"]).data["signoff"],
                                           w.s.owner_context, "")
    assert result.status == "accepted"
    w.pump()
    assert w.post(proposal["id"])["decision_json"]["state"] == "done"
    assert w.decisions()[proposal["id"]][0] == "approved"
    assert len(w.rows("SELECT * FROM idempotency_receipts WHERE operation_name='signoffs_review'")) == 1


def test_a_reaction_that_loses_the_race_to_a_dashboard_decision_reports_and_never_overrides_it(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.api.push(w.reaction("signoffs", w.provider(post), ["👍"]))
    w.bridge.poll_once()
    assert w.post(proposal["id"])["decision_json"]["state"] == "pending"
    assert w.s.decide(proposal, "reject", feedback="Rejected first").status == "ok"
    w.pump()
    assert w.decisions()[proposal["id"]][0] == "rejected"
    assert w.post(proposal["id"])["decision_json"]["state"] == "stale"
    changed = [n for n in w.notices() if "changed before your reaction was applied" in n]
    assert len(changed) == 1 and "Open the Signoffs view (it lists what is current there): http://qorqut:1071" in changed[0]


def test_a_retryable_failure_keeps_the_intent_pending_and_a_hard_failure_reports_it(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    calls = []
    real = w.s.service.review

    def flaky(*args, **kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            from qorqut_operations.contracts import DomainError, OperationResult
            return OperationResult.failure(DomainError("busy", "Try again", retryable=True), request_id="r")
        return real(*args, **kwargs)

    w.s.service.review = flaky
    w.react(post, "👍")
    assert w.post(proposal["id"])["decision_json"]["state"] == "pending" and w.decisions()[proposal["id"]][0] == "pending"
    assert w.post(proposal["id"])["decision_json"]["attempts"] == 1
    w.clock.advance(120)
    w.pump()
    assert w.decisions()[proposal["id"]][0] == "approved" and calls[0] == calls[1]


def test_a_hard_failure_retains_the_post_and_the_owners_reply_and_deletes_nothing(world):
    from qorqut_operations.contracts import DomainError, OperationResult
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.s.service.review = lambda *args, **kwargs: OperationResult.failure(DomainError("policy_denied", "Not allowed"), request_id="r")
    w.react(post, "👍")
    stored = w.post(proposal["id"])
    assert stored["decision_json"]["state"] == "failed" and stored["retire_reason"] == "decision_failed"
    assert w.decisions()[proposal["id"]][0] == "pending"
    assert any("could not be applied (policy_denied)" in text for text in w.notices())
    assert w.rows("SELECT * FROM telegram_cleanup_jobs") == [] and w.api.deleted == []
    # The same holds for a written answer: the reply stays in the chat and no note or handoff is invented.
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    request_post = only_post(w, row["id"])
    w.s.service.respond = lambda *args, **kwargs: OperationResult.failure(DomainError("policy_denied", "Not allowed"), request_id="r")
    w.reply(request_post, "Use the first supplier")
    assert w.post(row["id"])["decision_json"]["state"] == "failed"
    assert w.rows("SELECT * FROM telegram_cleanup_jobs") == [] and w.api.deleted == []
    assert w.owner_notes(row["id"]) == [] and w.handoffs() == []
    assert w.one("SELECT body FROM telegram_messages WHERE purpose='signoff_reply'")["body"] == "Use the first supplier"


def test_an_owner_context_that_can_no_longer_be_built_fails_that_decision_without_stalling_the_bridge(world):
    import sys
    from qorqut_operations.contracts import DomainError
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.api.push(w.reaction("signoffs", w.provider(post), ["👍"]))
    w.bridge.poll_once()
    bridge = w.bridge.signoff_bridge
    real = bridge.context

    def context(uow, business_id=None, **kwargs):
        if sys._getframe(1).f_code.co_name == "_execute":
            raise DomainError("business_archived", "The business is archived")
        return real(uow, business_id, **kwargs)

    bridge.context = context
    with w.bridge.write("test") as uow:
        from bazarlyk.telegram.outbox import enqueue_notice
        enqueue_notice(w.bridge, uow, "founder", "unrelated and still sent", "unrelated")
    w.pump()
    assert w.post(proposal["id"])["decision_json"]["state"] == "failed" and w.decisions()[proposal["id"]][0] == "pending"
    assert any("could not be applied (business_archived)" in text for text in w.notices())
    assert "unrelated and still sent" in [m["text"] for m in w.api.sent] and w.bridge.health() == {}


def test_a_decision_saved_but_not_yet_applied_never_runs_after_the_owner_is_repinned(world):
    from test_telegram_binding import repin, second_owner
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.api.push(w.reaction("signoffs", w.provider(post), ["👍"]))
    w.bridge.poll_once()
    saved = w.post(proposal["id"])["decision_json"]
    assert saved["state"] == "pending" and saved["binding"]["generation"] == 1 and saved["binding"]["owner"] == w.owner_id
    repin(w, second_owner(w))
    w.clock.advance(2)
    assert w.bridge._ensure_ready() is True and w.bridge.status()["binding"]["generation"] == 2
    w.bridge.signoff_bridge.execute_decisions()
    after = w.post(proposal["id"])["decision_json"]
    assert (after["state"], after["code"]) == ("failed", "binding_changed")
    assert w.decisions()[proposal["id"]][0] == "pending"  # the proposal was never approved under the new owner
    assert any("could not be applied (binding_changed)" in text for text in w.notices())


def repin_to_new_owner(w):
    """An explicit operator repin to a second owner who holds, in current canonical permissions, the scope the first one did."""
    from test_telegram_binding import repin, second_owner
    other = second_owner(w)
    with w.database.transaction() as uow:
        for business in (w.s.business, w.s.other_business):
            uow.repository("business_memberships", business_id=business).insert({"principal_id": other, "is_owner": True})
    repin(w, other)
    assert w.bridge._ensure_ready() is True and w.bridge.status()["binding"]["generation"] == 2
    return other


def test_an_active_post_survives_an_owner_repin_and_the_new_owner_answers_it_after_the_new_effective_time(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    other = repin_to_new_owner(w)
    w.pump()
    # Not retired and not re-posted: it is an outstanding question, not queued authority.
    assert [(p["state"], p["generation"]) for p in w.posts(proposal["id"])] == [("active", 1)]
    # A thumb dated in the repin second (before the new effective time) decides nothing.
    w.api.push(w.reaction("signoffs", provider, ["👍"], at=w.clock.now))
    w.bridge.poll_once()
    w.pump()
    assert w.decisions()[proposal["id"]][0] == "pending" and w.post(proposal["id"])["decision_json"] is None
    # A later sweep under the new owner keeps it too, even though the new owner has no delivery row for it yet.
    w.clock.advance(w.bridge.timings["sweep_interval"])
    w.pump()
    assert [(p["state"], p["generation"]) for p in w.posts(proposal["id"])] == [("active", 1)], w.posts(proposal["id"])
    # A fresh thumb after it answers the question, frozen under the new generation and owner.
    w.clock.advance(1)
    w.api.push(w.reaction("signoffs", provider, ["👍"], at=w.clock.now, update_id=w.next_update_id()))
    w.bridge.poll_once()
    w.pump()
    decided = w.post(proposal["id"])["decision_json"]
    assert decided["binding"]["generation"] == 2 and decided["binding"]["owner"] == other
    assert w.decisions()[proposal["id"]][0] == "approved" and decided["state"] == "done"


def test_a_post_carried_across_a_repin_ends_at_its_refresh_time_and_the_new_owners_delivery_publishes_a_fresh_one(world):
    w = world
    proposal = w.action()
    w.settle()
    old = only_post(w)
    repin_to_new_owner(w)
    w.clock.advance(24 * 3600 + 1)
    w.pump()
    # Nothing of the new owner can refresh it, and Telegram stops allowing its deletion soon: it ends rather than lingers.
    assert w.post(proposal["id"], id=old["id"])["retire_reason"] == "owner_changed"
    assert (CHAT_IDS["signoffs"], w.provider(old)) in w.api.deleted
    w.settle()
    fresh = [p for p in w.posts(proposal["id"]) if p["id"] != old["id"]]
    assert len(fresh) == 1 and fresh[0]["state"] == "active" and fresh[0]["generation"] == 2
    assert fresh[0]["notification_id"] != old["notification_id"]


def test_clearing_notifications_after_a_repin_still_retires_the_carried_post(world):
    w = world
    proposal = w.action()
    w.settle()
    repin_to_new_owner(w)
    w.pump()
    assert w.post(proposal["id"])["state"] == "active"
    result = w.s.app.invoke(w.s.agent_context(), "signoffs_attention_set", {"signoff_id": proposal["id"], "attention": {"notify": False}},
                            {"idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    w.pump()
    assert w.post(proposal["id"])["state"] == "retired" and w.post(proposal["id"])["retire_reason"] == "notify_cleared"


def test_a_changed_signoffs_chat_never_inherits_the_posts_of_the_old_chat(world):
    w = world
    proposal = w.action()
    w.settle()
    old_post = only_post(w)
    old_provider = w.provider(old_post)
    new_chat = -2004
    w.api.chat_ids["signoffs_moved"] = new_chat
    w.write_config(mutate=lambda data: data["chats"]["signoffs"].update(chat_id=new_chat))
    w.clock.advance(2)
    assert w.bridge._ensure_ready() is True and w.bridge.status()["binding"]["generation"] == 2
    # A thumb in the old chat is not routed: that chat is no longer configured.
    w.clock.advance(1)
    stale = w.reaction("signoffs", old_provider, ["👍"], at=w.clock.now)
    w.api.push(stale)
    w.bridge.poll_once()
    w.pump()
    assert w.decisions()[proposal["id"]][0] == "pending" and w.post(proposal["id"], id=old_post["id"])["decision_json"] is None
    w.clock.advance(w.bridge.timings["sweep_interval"])
    w.pump()
    w.pump()
    # The old post is retired, not inherited, and the pending Signoff is published in the new chat as a fresh generation.
    old = w.post(proposal["id"], id=old_post["id"])
    assert old["state"] == "retired" and old["retire_reason"] == "chat_changed"
    fresh = [p for p in w.posts(proposal["id"]) if p["id"] != old_post["id"]]
    assert len(fresh) == 1 and fresh[0]["state"] == "active" and fresh[0]["generation"] == 2
    assert [m["chat_id"] for m in w.api.sent if m["message_id"] == w.provider(fresh[0])] == [new_chat]
    assert (CHAT_IDS["signoffs"], old_provider) not in w.api.deleted  # nothing is deleted in a chat the bridge is no longer bound to


def test_an_owner_mismatch_keeps_a_saved_decision_pending_until_the_pinned_owner_is_current_again(world):
    from qorqut_operations.contracts import DomainError
    from test_telegram_binding import second_owner, set_database_owner
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    w.api.push(w.reaction("signoffs", w.provider(post), ["👍"]))
    w.bridge.poll_once()
    set_database_owner(w, second_owner(w))
    with pytest.raises(DomainError) as refused:
        w.bridge.signoff_bridge.execute_decisions()
    assert refused.value.code == "owner_binding_mismatch"
    assert w.post(proposal["id"])["decision_json"]["state"] == "pending" and w.decisions()[proposal["id"]][0] == "pending"
    set_database_owner(w, w.owner_id)  # the database names the pinned owner again: no repin was ever inferred
    w.bridge.signoff_bridge.execute_decisions()
    assert w.decisions()[proposal["id"]][0] == "approved" and w.post(proposal["id"])["decision_json"]["state"] == "done"


def test_a_reply_held_for_an_inactive_proposer_is_never_upgraded_into_direct_input_by_a_repin(world):
    from test_telegram_binding import repin, second_owner
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (w.s.agent.id,))
    w.reply(post, "A comment for later")
    held = json.loads(w.post(row["id"])["handoff_json"])
    assert held and held[0]["binding"]["generation"] == 1
    repin(w, second_owner(w))
    w.clock.advance(2)
    assert w.bridge._ensure_ready() is True
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (w.s.agent.id,))
    with w.bridge.write("test") as uow:
        assert w.bridge.signoff_bridge._release_held(uow, w.bridge.now()) is True
    # The hold ends and the Telegram reply is cleaned up, but no terminal input is queued under the new generation:
    # the canonical note and the proposer's inbox event already hold the words.
    assert w.handoffs() == [] and w.post(row["id"])["handoff_json"] is None
    assert w.owner_notes(row["id"]) == ["A comment for later"]
    assert w.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] >= 1


def test_a_reply_that_cannot_be_saved_as_a_comment_is_reported_and_kept(world):
    from qorqut_operations.contracts import DomainError
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    real = w.s.service.notes.append

    def refuse(*args, **kwargs):
        raise DomainError("forbidden", "The owner may not comment")

    w.s.service.notes.append = refuse
    w.reply(post, "Please rework the scope")
    update = w.one("SELECT state, reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")
    assert (update["state"], update["reason"]) == ("done", "comment_failed")
    assert any("could not be saved on the Signoff" in text for text in w.notices())
    assert w.owner_notes(proposal["id"]) == [] and w.handoffs() == [] and w.api.deleted == []
    assert w.rows("SELECT * FROM telegram_cleanup_jobs") == [] and w.post(proposal["id"])["state"] == "active"
    w.s.service.notes.append = real
    w.reply(post, "Second try")
    assert w.owner_notes(proposal["id"]) == ["Second try"]


# ----- reminders, snooze, attention -----------------------------------------------------------------------------


def test_an_hourly_reminder_replaces_the_old_post_and_deletes_it_only_once_the_new_one_is_sent(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    old = only_post(w)
    w.clock.advance(3600)
    w.tick()
    w.bridge.signoff_bridge.run()
    assert w.post(row["id"], generation=1)["retire_reason"] == "replaced"
    assert w.api.deleted == []  # the replacement is queued, not yet visible
    w.pump()
    new = only_post(w)
    assert new["generation"] == 2 and new["notification_id"] != old["notification_id"]
    assert (CHAT_IDS["signoffs"], w.provider(old)) in w.api.deleted and len(w.api.sent) == 2


def test_snooze_retires_and_deletes_the_post_and_it_returns_when_due_and_notify_cleared_retires_it(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    old = only_post(w)
    until = store.plus(w.bridge.now(), 6 * 3600)
    w.s.service.notifications.snooze(w.s.owner_context, row["id"], until)
    w.pump()
    assert w.post(row["id"])["retire_reason"] == "snoozed" and (CHAT_IDS["signoffs"], w.provider(old)) in w.api.deleted
    w.clock.advance(6 * 3600)
    w.settle()
    assert only_post(w)["generation"] == 2 and len(w.api.sent) == 2
    result = w.s.app.invoke(w.s.agent_context(), "signoffs_attention_set", {"signoff_id": row["id"], "attention": {"notify": False}},
                            {"idempotency_key": new_id()})
    assert result.status == "ok"
    w.pump()
    assert w.post(row["id"], generation=2)["retire_reason"] == "notify_cleared"
    w.clock.advance(7200)
    w.settle()
    assert len(w.api.sent) == 2


def test_a_dismissed_owner_notification_retires_its_post(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    delivery = w.s.service.notifications.deliveries(w.s.owner_context)["items"][0]["id"]
    w.s.service.notifications.dismiss(w.s.owner_context, delivery)
    w.pump()
    assert w.post(row["id"])["state"] == "retired"


# ----- refresh, cleanup limits ----------------------------------------------------------------------------------


def test_a_pending_post_is_refreshed_with_a_new_generation_before_telegram_stops_allowing_deletion(world):
    w = world
    row = create_input(w.s, attention=ORDINARY)
    w.settle()
    old = only_post(w)
    w.clock.advance(24 * 3600 - 1)
    w.pump()
    assert only_post(w)["generation"] == 1
    w.clock.advance(2)
    w.bridge.signoff_bridge.run()
    assert w.post(row["id"], generation=1)["retire_reason"] == "refreshed" and only_post(w)["state"] == "queued"
    assert w.api.deleted == []
    w.pump()
    new = only_post(w)
    assert new["generation"] == 2 and new["state"] == "active" and new["message_id"] != old["message_id"]
    assert (CHAT_IDS["signoffs"], w.provider(old)) in w.api.deleted
    assert w.provider(new) not in w.deleted_ids()
    assert new["notification_id"] == old["notification_id"]


def test_a_refresh_that_races_a_decision_deletes_the_replacement_and_the_old_post(world):
    w = world
    proposal = w.action()
    w.settle()
    old = only_post(w)
    w.clock.advance(24 * 3600 + 1)
    w.bridge.signoff_bridge.run()
    w.bridge.dispatcher.run()
    replacement = only_post(w)
    assert replacement["generation"] == 2 and replacement["state"] == "active"
    w.s.decide(proposal, "reject", feedback="Rejected while refreshed")
    w.pump()
    assert w.post(proposal["id"])["state"] == "retired"
    assert {w.provider(old), w.provider(replacement)} <= set(w.deleted_ids())
    w.react(replacement, "👍")
    assert w.decisions()[proposal["id"]][0] == "rejected"


def test_a_post_older_than_the_deletion_limit_is_reported_for_manual_cleanup_never_silently_lost(world):
    w = world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE telegram_signoff_posts SET refresh_at=? WHERE id=?", ("9999-01-01T00:00:00.000000Z", post["id"]))
    w.clock.advance(49 * 3600)
    w.s.decide(proposal, "reject", feedback="Late")
    w.pump()
    job = w.one("SELECT state, error_code FROM telegram_cleanup_jobs WHERE message_id=?", post["message_id"])
    assert (job["state"], job["error_code"]) == ("failed", "too_old_48h")
    failure = w.bridge.status()["cleanup_failures"][0]
    assert failure["message_id"] == post["message_id"] and failure["error_code"] == "too_old_48h"
    assert w.api.deleted == []


# ----- failed or unconfirmed execution --------------------------------------------------------------------------


def test_an_approved_proposal_whose_application_fails_gets_one_non_actionable_status_notice_and_no_retry(world):
    w = world
    w.s.fail_apply = True
    proposal = w.action()
    w.settle()
    w.react(only_post(w), "👍")
    assert w.decisions()[proposal["id"]][0] == "approved"
    assert w.s.service.run_pending() == 1
    w.pump()
    assert w.signoff(proposal["id"])["state"] == "failed"
    status = w.post(proposal["id"], input_form="status")
    assert status["state"] == "active" and status["category"] == "status:failed"
    text = w.api.sent[-1]["text"]
    assert text.startswith("Status: Create task") and "approved but did not complete (failed)" in text
    assert "Nothing was retried automatically" in text and "http://qorqut:1071/#one" in text and "React" not in text
    w.pump()
    w.settle()
    assert len([p for p in w.posts() if p["input_form"] == "status"]) == 1
    # A reaction or reply on a status notice decides nothing; the notice itself is never deleted.
    w.react(status, "👍")
    w.reply(status, "retry please")
    assert w.signoff(proposal["id"])["state"] == "failed"
    assert w.provider(status) not in w.deleted_ids()
    assert len(w.api.sent) == 2 + len([n for n in w.notices()])


def test_a_conflicted_approval_reports_once_and_a_rejection_or_supersession_never_produces_a_status_notice(world):
    w = world
    rejected = w.action("Rejected one")
    w.settle()
    w.react(only_post(w, rejected["id"]), "👎")
    superseded = w.action("Old")
    w.action("New", options={"supersedes_signoff_id": superseded["id"], "expected_superseded_status_revision": superseded["status_revision"]})
    w.settle()
    assert [p for p in w.posts() if p["input_form"] == "status"] == []


# ----- routing --------------------------------------------------------------------------------------------------


def test_the_signoff_bridge_is_registered_for_source_kind_hooks_inbound_routes_and_status(world):
    w = world
    bridge = w.bridge
    assert bridge.signoff_bridge.bridge is bridge and "signoff" in bridge.admit_hooks and "signoff" in bridge.sent_hooks
    assert bridge.steps[0] == bridge.signoff_bridge.run
    create_input(w.s, attention=BLOCKING)
    w.settle()
    assert bridge.status()["signoffs"] == {"active_posts": 1, "queued_posts": 0, "pending_decisions": 0, "held_replies": 0}


def test_a_disabled_or_unready_bridge_never_reconciles_or_sends_signoffs(tmp_path):
    w = World(tmp_path)
    try:
        create_input(w.s, attention=BLOCKING)
        w.write_config(enabled=False)
        w.bridge._config_signature = None
        assert w.bridge.cycle() >= 30
        assert w.api.sent == [] and w.posts() == []
    finally:
        w.close()


# ----- global scope, failed posts, restart ----------------------------------------------------------------------


def global_input(w, **updates):
    agent = getattr(w, "global_agent", None)
    if agent is None:
        agent = w.global_agent = w.s.add_agent("ordubashy")
        w.s.change_override("signoffs_input_create", "allow", actor=agent)
    args = {"scope": "global", "request_key": "global-ship", "form": BINARY, "attention": BLOCKING, **updates}
    result = w.s.app.invoke(w.s.agent_context(agent), "signoffs_input_create", args, {"idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    return result.data["signoff"]


def test_a_global_signoff_is_posted_with_the_global_link_and_decided_through_the_global_owner_scope(world):
    w = world
    row = global_input(w)
    w.settle()
    post = only_post(w, row["id"])
    text = w.one("SELECT body FROM telegram_messages WHERE id=?", post["message_id"])["body"]
    assert "Global · requested by Ordubashy" in text and "Open the Signoffs view: http://qorqut:1071/#/global/signoffs" in text
    assert post["input_form"] == "binary_input"
    w.react(post, "👍")
    state, decision = w.decisions()[row["id"]]
    assert state == "answered" and tuple(decision["selected_choices"]) == ("yes",)
    assert w.post(row["id"])["state"] == "retired"


def test_a_refused_post_is_retired_not_retried_forever_and_the_next_reminder_posts_a_fresh_generation(world):
    from bazarlyk.telegram.api import TelegramApiError
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.api.script("send_message", TelegramApiError("refused", "Bad Request: something", error_code=400))
    w.settle()
    assert w.api.sent == [] and w.post(row["id"])["retire_reason"] == "send_failed"
    for _ in range(3):
        w.settle()
    assert w.api.sent == [] and len(w.posts()) == 1
    assert w.bridge.status()["failed_sends"][0]["error_code"] == "refused_400"
    w.clock.advance(3600)
    w.settle()
    assert len(w.api.sent) == 1 and only_post(w)["generation"] == 2


def test_a_restart_reconciles_from_saved_state_without_duplicating_posts_or_replaying_history(world):
    w = world
    live = w.action("Still pending")
    w.settle()
    resolved = w.action("Already done")
    w.s.decide(resolved, "reject", feedback="Done before the restart")
    w.bridge.stop(timeout=5)
    restarted = w.make_bridge()
    restarted.owner_terminal = w.bridge.owner_terminal
    w.ready(restarted)
    w.bridge = w.runtime.telegram = restarted
    for _ in range(3):
        w.pump()
    assert len(w.api.sent) == 1 and only_post(w, live["id"])["generation"] == 1
    assert w.posts(resolved["id"]) == []
    # The new process still honors the old post, and a decision is applied exactly once.
    w.react(only_post(w, live["id"]), "👍")
    assert w.decisions()[live["id"]][0] == "approved" and w.post(live["id"])["decision_json"]["state"] == "done"


# ----- the direct path for Signoff replies ----------------------------------------------------------------------


def test_a_signoff_reply_for_an_offline_agent_stays_queued_without_expiry_and_is_typed_once_when_it_returns(world):
    from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome
    w = world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    w.bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("deferred", "terminal_offline")] * 400
    w.reply(only_post(w), "Please use the first supplier")
    handoff = w.handoffs()[0]
    assert handoff["state"] == "queued" and handoff["deferral_reason"] == "terminal_offline" and handoff["notice_sent_at"] is None
    assert handoff["expires_at"] is None
    assert not any("queued for" in n for n in w.notices())
    for _ in range(3):  # far beyond the previous one-hour limit: nothing expires, nothing is discarded
        w.clock.advance(3601)
        w.pump()
        assert w.handoffs()[0]["state"] == "queued"
    assert w.owner_notes(row["id"]) == ["Please use the first supplier"]
    assert w.signoff(row["id"])["decision"]["notes"] == "Please use the first supplier"
    # The agent comes back: the exact saved reply is typed once, guarded, and the input closes.
    terminal = w.bridge.owner_terminal
    terminal.outcomes[:] = []
    tried = len(terminal.calls)
    w.clock.advance(60)
    for _ in range(4):
        w.pump()
    assert w.handoffs()[0]["state"] == "submitted"
    for _ in range(3):
        w.pump()
    assert len(terminal.calls) == tried + 1 and "Please use the first supplier" in terminal.calls[-1]["text"]
    assert not any("expired" in n for n in w.notices())


def test_a_saved_response_for_an_inactive_proposer_keeps_the_post_and_reply_until_a_valid_handoff_exists(world):
    w = world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (w.s.agent.id,))
    w.reply(post, "Use the first supplier")
    # The canonical response saved, but nobody can receive it: the post retired, yet nothing was deleted or handed off.
    assert w.decisions()[row["id"]][0] == "answered"
    assert w.post(row["id"])["state"] == "retired" and w.post(row["id"])["retire_reason"] == "resolved"
    assert w.handoffs() == [] and w.api.deleted == []
    assert w.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 0
    assert sum("proposer is not active" in n for n in w.notices()) == 1
    assert w.post(row["id"])["handoff_json"]
    # Neither the resolved sweep nor the periodic reconciliation bypasses the hold while the proposer is inactive.
    for _ in range(3):
        w.clock.advance(w.bridge.timings["sweep_interval"])
        w.pump()
    assert w.handoffs() == [] and w.api.deleted == [] and w.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 0
    with w.bridge.read() as uow:
        assert w.bridge.signoff_bridge.status_counts(uow)["held_replies"] == 1
    # The proposer becomes active again: one handoff is queued and both messages are cleaned up exactly once.
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (w.s.agent.id,))
    w.clock.advance(w.bridge.timings["sweep_interval"])
    for _ in range(4):
        w.pump()
    assert len(w.handoffs()) == 1 and w.handoffs()[0]["agent_id"] == w.s.agent.id
    assert w.post(row["id"])["handoff_json"] is None
    assert (CHAT_IDS["signoffs"], provider) in w.api.deleted
    reply = w.one("SELECT * FROM telegram_messages WHERE purpose='signoff_reply'")
    assert (CHAT_IDS["signoffs"], reply["telegram_message_id"]) in w.api.deleted
    for _ in range(3):
        w.clock.advance(w.bridge.timings["sweep_interval"])
        w.pump()
    assert len(w.handoffs()) == 1 and w.api.deleted.count((CHAT_IDS["signoffs"], provider)) == 1


def test_a_comment_reply_for_an_inactive_proposer_holds_the_live_post_and_replies_only_after_a_valid_handoff(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (w.s.agent.id,))
    w.reply(post, "A comment for later")
    assert w.owner_notes(row["id"]) == ["A comment for later"]
    assert w.post(row["id"])["state"] == "active" and w.post(row["id"])["handoff_json"]
    assert w.handoffs() == [] and w.api.deleted == []
    with w.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (w.s.agent.id,))
    w.clock.advance(w.bridge.timings["sweep_interval"])
    for _ in range(4):
        w.pump()
    assert w.post(row["id"])["state"] == "retired" and w.post(row["id"])["retire_reason"] == "replied"
    assert len(w.handoffs()) == 1 and (CHAT_IDS["signoffs"], provider) in w.api.deleted


def test_an_uncertain_signoff_reply_input_is_never_typed_again(world):
    from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome
    w = world
    create_input(w.s, attention=BLOCKING)
    w.settle()
    w.bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("uncertain", "submit_unconfirmed")]
    w.reply(only_post(w), "Go ahead")
    assert w.handoffs()[0]["state"] == "uncertain"
    w.clock.advance(7200)
    for _ in range(3):
        w.pump()
    assert len(w.bridge.owner_terminal.calls) == 1 and w.handoffs()[0]["state"] == "uncertain"


# ----- fake-clock reconciliation and uncertain sends ------------------------------------------------------------


def test_the_fifteen_minute_sweep_repairs_a_change_no_hook_reported(world):
    w = world
    row = create_input(w.s, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    with w.database.transaction() as uow:  # a write that bypasses every change signal
        uow.connection.execute("UPDATE signoff_attention SET snoozed_until=? WHERE signoff_id=?",
                               (store.plus(w.bridge.now(), 6 * 3600), row["id"]))
    for _ in range(3):
        w.pump()
    assert only_post(w)["id"] == post["id"]
    w.clock.advance(w.bridge.timings["sweep_interval"])
    w.pump()
    assert w.post(row["id"])["retire_reason"] == "snoozed" and (CHAT_IDS["signoffs"], w.provider(post)) in w.api.deleted


def test_an_uncertain_post_is_never_resent_or_replaced_and_the_owner_confirming_it_activates_the_post(world):
    from bazarlyk.telegram.api import TelegramApiError
    w = world
    row = create_input(w.s, attention=ORDINARY)
    w.api.script("send_message", TelegramApiError("timeout", "read timed out"))
    w.settle()
    post = w.post(row["id"])
    outbox = w.one("SELECT * FROM telegram_outbox WHERE source_kind='signoff'")
    assert outbox["state"] == "uncertain" and post["state"] == "queued"
    w.clock.advance(3 * 3600)
    w.settle()
    assert w.api.calls.count("send_message") == 1 and len(w.posts()) == 1  # no resend and no second post while unsure
    w.resolve(post["message_id"], "mark_delivered", provider_message_id=777)
    assert w.post(row["id"])["state"] == "active" and w.provider(w.post(row["id"])) == 777
    assert w.post(row["id"])["refresh_at"]


def test_an_uncertain_post_that_gets_resolved_meanwhile_is_deleted_only_after_the_owner_confirms_it(world):
    from bazarlyk.telegram.api import TelegramApiError
    w = world
    proposal = w.action()
    w.api.script("send_message", TelegramApiError("timeout", "read timed out"))
    w.settle()
    post = w.post(proposal["id"])
    w.s.decide(proposal, "reject", feedback="No")
    w.pump()
    assert w.post(proposal["id"])["state"] == "retired" and w.api.deleted == []
    assert w.one("SELECT state FROM telegram_outbox WHERE source_kind='signoff'")["state"] == "uncertain"
    w.clock.advance(60)
    w.pump()
    assert w.api.deleted == []  # a message that may not exist is never guessed at
    w.resolve(post["message_id"], "mark_delivered", provider_message_id=778)
    w.clock.advance(60)
    w.pump()
    assert (CHAT_IDS["signoffs"], 778) in w.api.deleted
