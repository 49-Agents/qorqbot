"""Private forum topics: one supergroup, six topics, and a destination that is always the exact
``(chat, topic)`` pair. Only the Telegram API and the terminal are injected; nothing here reaches a network or a real terminal."""
import copy
import hashlib
import json
import sqlite3

import pytest

from qorqut_operations.contracts import DomainError, canonical_hash, new_id
from bazarlyk.storage import Database
from bazarlyk.telegram import store
from bazarlyk.telegram.api import TelegramApiError
from bazarlyk.telegram.config import parse_config, redacted_example
from bazarlyk.telegram.normalize import normalize_update
from bazarlyk.telegram.notify import notify
from bazarlyk.telegram.outbox import enqueue_text
from foundation_support import HUMAN
from telegram_service_support import TelegramService
from telegram_support import BOT_ID, CHAT_IDS, GROUP_ID, OWNER_TG, START, SYNTHETIC_PINS, TOPIC_IDS, Env, config_data
from test_telegram_owner_input import FakeTerminal, every_stored_text, inputs, notices, owner_says, secret_request
from test_telegram_config import agents, rejects
from test_signoff_inputs import create as create_input

# The fingerprint of a configuration without topics, as it was computed before topics existed.
LEGACY_FINGERPRINT = "3d75faec996e1dd92636087af42510a13771dc6a980ec684b90b19da88b0c850"
MIGRATION_106_SHA256 = "4891446c65f2d6cc146aa5a731cf6fd83ec9111e324f0eefe8197be681f1f65c"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, topics=True)
    e.bridge.owner_terminal = FakeTerminal()
    yield e
    e.close()


@pytest.fixture
def legacy(tmp_path):
    e = Env(tmp_path)
    e.bridge.owner_terminal = FakeTerminal()
    yield e
    e.close()


def ready(env):
    bridge = env.ready()
    bridge.owner_terminal = env.bridge.owner_terminal
    return bridge


def rebind(env, bridge, **topics):
    """The operator moves destinations in the private file; the running bridge verifies it as a new generation."""
    def change(data):
        for name, thread in topics.items():
            data["chats"][name]["message_thread_id"] = thread
    env.write_config(mutate=change)
    assert bridge._ensure_ready(), bridge.health()
    return env.one("SELECT * FROM telegram_state")


def valid(tmp_path, **kwargs):
    return config_data(tmp_path, agents(), topics=True, **kwargs)


def ignored(env, update_id=None):
    sql = "SELECT state, reason, normalized_json FROM telegram_updates"
    row = env.one(sql + (" WHERE update_id=?" if update_id else " ORDER BY update_id DESC LIMIT 1"), *([update_id] if update_id else []))
    return row["state"], row["reason"]


def sent_at(env, text):
    return [item for item in env.api.sent if item["text"] == text]


# ----- configuration --------------------------------------------------------------------------------------------


def test_a_six_topic_forum_configuration_is_valid_and_resolves_every_destination_exactly(tmp_path):
    config = parse_config(valid(tmp_path))
    assert [(chat.name, chat.chat_id, chat.message_thread_id) for chat in config.chats] == [
        (name, GROUP_ID, TOPIC_IDS[name]) for name in TOPIC_IDS]
    for name, thread in TOPIC_IDS.items():
        assert config.destination(GROUP_ID, thread).name == name and config.chat(name).destination == (GROUP_ID, thread)
    # Only the exact pair resolves: the chat alone, General, another chat and an unbound topic never do.
    assert config.destination(GROUP_ID) is None and config.destination(GROUP_ID, 1) is None
    assert config.destination(GROUP_ID, 99) is None and config.destination(-5, TOPIC_IDS["founder"]) is None
    assert len(config.chats_in(GROUP_ID)) == 6 and config.group_ids() == (GROUP_ID,)
    assert config.redacted()["forum_topics"] == 6 and str(GROUP_ID) not in json.dumps(config.redacted())


def test_the_example_is_one_synthetic_private_forum_group_with_six_different_topics():
    example = redacted_example()
    config = parse_config(example, require_complete=True)
    assert len({chat["chat_id"] for chat in example["chats"].values()}) == 1
    assert len({chat["message_thread_id"] for chat in example["chats"].values()}) == 6
    assert all(chat["type"] == "supergroup" and chat["message_thread_id"] > 1 for chat in example["chats"].values())
    assert config.group_ids() == (-1001000000001,) and example["enabled"] is False


@pytest.mark.parametrize("thread", [True, False, "12", 12.0, 0, -5, 1, None, [12]])
def test_a_topic_id_must_be_an_exact_integer_greater_than_one(tmp_path, thread):
    data = valid(tmp_path)
    data["chats"]["founder"]["message_thread_id"] = thread
    rejects(data, "message_thread_id must be an exact integer")


@pytest.mark.parametrize("kind,chat_id", [("private", OWNER_TG), ("group", -5), ("channel", -1009)])
def test_a_topic_is_only_valid_in_a_supergroup(tmp_path, kind, chat_id):
    data = valid(tmp_path)
    data["chats"]["urgent"].update(type=kind, chat_id=chat_id)
    rejects(data, "Only a supergroup may name a forum topic")


def test_bindings_may_share_a_chat_only_as_distinct_topics_and_never_beside_a_whole_chat_binding(tmp_path):
    duplicate = valid(tmp_path)
    duplicate["chats"]["founder"]["message_thread_id"] = TOPIC_IDS["ordubashy"]
    rejects(duplicate, "forum topic destination must be distinct")
    mixed = valid(tmp_path)
    del mixed["chats"]["urgent"]["message_thread_id"]  # a whole-chat binding in the very group the others use as topics
    rejects(mixed, "whole-chat binding cannot share its chat")
    legacy = config_data(tmp_path, agents())
    legacy["chats"]["urgent"]["chat_id"] = legacy["chats"]["signoffs"]["chat_id"]  # the old rule: every whole chat is distinct
    rejects(legacy, "whole-chat binding cannot share its chat")
    # The agent binding stays unique even inside one group.
    twice = valid(tmp_path)
    twice["chats"]["marketing"]["agent_id"] = twice["chats"]["founder"]["agent_id"]
    rejects(twice, "agent binding must be unique")


def test_topic_bindings_coexist_with_a_legacy_whole_chat_binding_in_a_different_chat(tmp_path):
    data = valid(tmp_path)
    data["chats"]["urgent"] = {"chat_id": -1003333, "type": "channel"}
    config = parse_config(data)
    assert config.chat("urgent").message_thread_id is None and config.destination(-1003333).name == "urgent"
    assert config.group_ids() == (GROUP_ID, -1003333)


def test_a_configuration_without_topics_keeps_its_legacy_fingerprint_and_a_topic_changes_it(tmp_path):
    config = parse_config(config_data(tmp_path, agents()))
    assert config.binding_fingerprint() == canonical_hash({
        "bot_id": config.bot_id, "owner": config.owner_telegram_user_id, "installation": config.installation_id,
        "owner_principal": config.owner_principal_id, "chats": [[c.name, c.chat_id, c.chat_type, c.agent_id] for c in config.chats]})
    fixed = redacted_example()
    fixed["enabled"] = True
    for name, chat in fixed["chats"].items():
        chat["chat_id"] = CHAT_IDS[name]
        del chat["message_thread_id"]
    assert parse_config(fixed).binding_fingerprint() == LEGACY_FINGERPRINT  # a code upgrade alone resets no generation
    topics = parse_config(valid(tmp_path)).binding_fingerprint()
    assert topics != config.binding_fingerprint()
    moved = valid(tmp_path)
    moved["chats"]["marketing"]["message_thread_id"] = 77
    assert parse_config(moved).binding_fingerprint() not in (topics, config.binding_fingerprint())
    # Only the numeric ID counts: the same topics in the same order fingerprint the same however Telegram titles them.
    assert parse_config(valid(tmp_path)).binding_fingerprint() == topics


# ----- normalization --------------------------------------------------------------------------------------------


def raw(chat_id=GROUP_ID, **fields):
    message = {"message_id": 5, "date": 1_700_000_000, "chat": {"id": chat_id, "type": "supergroup"},
               "from": {"id": OWNER_TG, "is_bot": False}, "text": "hi", **fields}
    return {"update_id": 1, "message": message}


def test_topic_metadata_is_normalized_and_the_topic_anchor_is_not_a_reply():
    plain_topic = normalize_update(raw(message_thread_id=12, is_topic_message=True,
                                       reply_to_message={"message_id": 12, "forum_topic_created": {"name": "Founder"}}))
    assert (plain_topic["message_thread_id"], plain_topic["topic_invalid"], plain_topic["reply"]) == (12, False, None)
    by_id = normalize_update(raw(message_thread_id=12, is_topic_message=True, reply_to_message={"message_id": 12}))
    assert by_id["reply"] is None
    real = normalize_update(raw(message_thread_id=12, is_topic_message=True, reply_to_message={"message_id": 4321}))
    assert real["reply"] == {"message_id": 4321} and real["message_thread_id"] == 12
    general = normalize_update(raw())
    assert (general["message_thread_id"], general["topic_invalid"]) == (None, False)
    reply_thread = normalize_update(raw(message_thread_id=12))  # an ordinary supergroup reply thread, not a topic message
    assert (reply_thread["message_thread_id"], reply_thread["topic_invalid"]) == (None, False)
    assert normalize_update({"update_id": 2, "message_reaction": {"chat": {"id": GROUP_ID, "type": "supergroup"}, "message_id": 5,
                                                                   "user": {"id": OWNER_TG}, "new_reaction": [], "old_reaction": []}}
                            )["kind"] == "message_reaction"


@pytest.mark.parametrize("fields", [
    {"message_thread_id": "12", "is_topic_message": True}, {"message_thread_id": True, "is_topic_message": True},
    {"message_thread_id": 0, "is_topic_message": True}, {"message_thread_id": -3, "is_topic_message": True},
    {"message_thread_id": 12.0, "is_topic_message": True}, {"message_thread_id": 12, "is_topic_message": "yes"},
    {"is_topic_message": True}, {"message_thread_id": None, "is_topic_message": True},
])
def test_malformed_topic_metadata_is_invalid_and_never_read_as_general_or_a_whole_chat(fields):
    item = normalize_update(raw(**fields))
    assert item["topic_invalid"] is True and item["message_thread_id"] is None


# ----- migration 107 --------------------------------------------------------------------------------------------


def test_a_database_at_migration_106_upgrades_without_changing_its_legacy_receipts(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    assert Database.migrations()[105][1] == MIGRATION_106_SHA256  # migration 106 (the Telegram bridge) is retained byte for byte
    database = Database(path)
    database.initialize(through=106)
    now = "2026-09-30T12:00:00.000000Z"
    fingerprint = "f" * 64
    owner = new_id()
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
    db = database._connect()
    db.execute("INSERT INTO telegram_state(id,bot_id,installation_id,owner_principal_id,owner_telegram_user_id,config_generation,"
               "binding_fingerprint,binding_effective_at,activation_at,poll_offset,created_at,updated_at) "
               "VALUES ('telegram',7,?,?,9,3,?,?,?,4321,?,?)", ("0" * 36, owner, fingerprint, now, now, now, now))
    db.execute("INSERT INTO telegram_updates(bot_id,update_id,kind,chat_id,message_id,actor_id,sent_at,normalized_json,state,"
               "received_at,processed_at,created_at,updated_at) VALUES (7,4320,'message',-5,77,9,?,?,'done',?,?,?,?)",
               (now, json.dumps({"update_id": 4320, "kind": "message", "chat_id": -5, "text": "kept words"}), now, now, now, now))
    db.execute("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,telegram_message_id,purpose,body,occurred_at,"
               "created_at,updated_at) VALUES ('m1','outbound','founder',-5,901,'reply','legacy body',?,?,?)", (now, now, now))
    db.execute("INSERT INTO telegram_outbox(id,chat_name,chat_id,kind,source_kind,source_key,message_id,payload_json,state,"
               "provider_message_id,sent_at,binding_fingerprint,config_generation,created_at,updated_at) "
               "VALUES ('o1','founder',-5,'text','agent','k1','m1','{\"text\":\"legacy body\"}','sent',901,?,?,3,?,?)",
               (now, fingerprint, now, now))
    db.close()
    database.initialize()
    db = database._connect()
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(Database.migrations()) >= 107
    message = dict(db.execute("SELECT * FROM telegram_messages").fetchone())
    outbox = dict(db.execute("SELECT * FROM telegram_outbox").fetchone())
    state = dict(db.execute("SELECT * FROM telegram_state").fetchone())
    update = dict(db.execute("SELECT * FROM telegram_updates").fetchone())
    # Provider IDs, destinations, the cursor, the generation and the stored words are untouched; nothing became a topic.
    assert (message["telegram_message_id"], message["chat_id"], message["chat_name"], message["body"]) == (901, -5, "founder", "legacy body")
    assert (outbox["provider_message_id"], outbox["chat_id"], outbox["state"]) == (901, -5, "sent")
    assert message["message_thread_id"] is None and outbox["message_thread_id"] is None
    assert (state["poll_offset"], state["config_generation"], state["binding_fingerprint"]) == (4321, 3, fingerprint)
    assert json.loads(update["normalized_json"])["text"] == "kept words"
    # A legacy row is never backfilled to a topic, a saved destination never changes, and a topic must be a real one.
    for table, key in (("telegram_messages", "m1"), ("telegram_outbox", "o1")):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"UPDATE {table} SET message_thread_id=12 WHERE id=?", (key,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,message_thread_id,purpose,body,occurred_at,created_at,"
                   "updated_at) VALUES ('m2','outbound','founder',-5,1,'reply','x',?,?,?)", (now, now, now))
    db.execute("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,message_thread_id,purpose,body,occurred_at,created_at,"
               "updated_at) VALUES ('m3','outbound','founder',-5,12,'reply','x',?,?,?)", (now, now, now))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE telegram_messages SET message_thread_id=13 WHERE id='m3'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE telegram_messages SET message_thread_id=NULL WHERE id='m3'")
    # The provider identity stays unique group-wide, whatever the topic.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,message_thread_id,telegram_message_id,purpose,body,"
                   "occurred_at,created_at,updated_at) VALUES ('m4','outbound','founder',-5,12,901,'reply','x',?,?,?)", (now, now, now))
    db.close()


def test_repository_metadata_declares_the_new_destination_columns_immutable(tmp_path):
    from bazarlyk.storage.repositories import IMMUTABLE_FIELDS
    assert "message_thread_id" in IMMUTABLE_FIELDS["telegram_messages"] and "message_thread_id" in IMMUTABLE_FIELDS["telegram_outbox"]


# ----- verification ---------------------------------------------------------------------------------------------


def test_the_forum_group_is_verified_once_for_all_six_topics(env):
    bridge = ready(env)
    assert env.api.calls.count("get_chat") == 1 and env.api.calls.count("get_chat_member") == 2  # the bot and the owner, once
    assert env.one("SELECT config_generation FROM telegram_state")["config_generation"] == 1
    assert bridge.config.group_ids() == (GROUP_ID,)


@pytest.mark.parametrize("arrange,needle", [
    (lambda e: e.api.chat_overrides.update({GROUP_ID: {"is_forum": False}}), "does not have topics enabled"),
    (lambda e: e.api.forum_chats.clear(), "does not have topics enabled"),
    (lambda e: e.api.chat_overrides.update({GROUP_ID: {"is_forum": "true"}}), "does not have topics enabled"),
    (lambda e: e.api.chat_overrides.update({GROUP_ID: {"username": "public_group"}}), "public"),
    (lambda e: e.api.chat_overrides.update({GROUP_ID: {"active_usernames": ["x"]}}), "public"),
    (lambda e: e.api.chat_overrides.update({GROUP_ID: {"type": "group"}}), "wrong type"),
    (lambda e: e.api.member_overrides.update({(GROUP_ID, BOT_ID): {"status": "member"}}), "lacks administrator rights"),
    (lambda e: e.api.member_overrides.update({(GROUP_ID, BOT_ID): {"status": "administrator", "can_delete_messages": False}}),
     "lacks administrator rights"),
    (lambda e: e.api.member_overrides.update({(GROUP_ID, OWNER_TG): {"status": "left"}}), "owner is not a member"),
])
def test_a_group_that_is_not_a_private_forum_with_the_right_members_is_refused(env, arrange, needle):
    arrange(env)
    env.bridge.load_config()
    assert env.bridge._ensure_ready() is False
    assert env.bridge.health() == {"verification": "verification_failed"}
    with pytest.raises(DomainError, match=needle):
        env.bridge.verify_remote(env.api, env.bridge.config)


def test_a_whole_chat_configuration_needs_no_forum(legacy):
    assert legacy.ready()  # legacy groups are not forums and are verified exactly as before, one call per chat
    assert legacy.api.calls.count("get_chat") == 6


# ----- routing --------------------------------------------------------------------------------------------------


def queue(env, text="hi", chat="founder", source_key=None, purpose="reply", **kwargs):
    bridge = env.bridge
    with bridge.write("test") as uow:
        return enqueue_text(bridge, uow, bridge.config.chat(chat), text=text, purpose=purpose, source_kind="agent",
                            source_key=source_key or new_id(), agent_id=env.agents.get(chat), **kwargs)


def in_topic(update, thread):
    """The same message, as sent into a different topic of the group (``None``: General, which Telegram sends with no thread)."""
    message = update["message"]
    for key in ("message_thread_id", "is_topic_message", "reply_to_message"):
        message.pop(key, None)
    if thread is not None:
        message.update(message_thread_id=thread, is_topic_message=True,
                       reply_to_message={"message_id": thread, "forum_topic_created": {"name": "t"}})
    return update


def drain(bridge):
    while bridge.owner_inputs.run():
        pass


def test_each_topic_routes_owner_text_only_to_its_own_agent_and_saves_the_topic(env):
    bridge = ready(env)
    for name in ("ordubashy", "founder", "marketing"):
        owner_says(env, name, f"for {name}")
    drain(bridge)
    calls = bridge.owner_terminal.calls
    assert [c["agent"] for c in calls] == [env.agents[n] for n in ("ordubashy", "founder", "marketing")]
    for call, name in zip(calls, ("ordubashy", "founder", "marketing")):
        assert f"for {name}" in call["text"] and "In reply to" not in call["text"]  # the topic anchor is no reply
    stored = env.rows("SELECT chat_name, chat_id, message_thread_id, agent_id, body FROM telegram_messages WHERE direction='inbound' ORDER BY rowid")
    assert [(r["chat_name"], r["chat_id"], r["message_thread_id"]) for r in stored] == [
        (n, GROUP_ID, TOPIC_IDS[n]) for n in ("ordubashy", "founder", "marketing")]
    assert [r["body"] for r in stored] == ["for ordubashy", "for founder", "for marketing"]


@pytest.mark.parametrize("build,reason", [
    (lambda e: in_topic(e.message("founder", "from general"), None), "unknown_topic"),
    (lambda e: in_topic(e.message("founder", "from an unbound topic"), 99), "unknown_topic"),
    (lambda e: in_topic(e.message("founder", "from the anchor id"), 1), "unknown_topic"),
    (lambda e: {**e.message("founder", "from elsewhere"), "message": {**e.message("founder", "from elsewhere")["message"],
                "chat": {"id": -5, "type": "supergroup"}}}, "unknown_chat"),
    (lambda e: (lambda u: (u["message"].update(message_thread_id="12"), u)[1])(e.message("founder", "string topic")), "invalid_topic"),
    (lambda e: (lambda u: (u["message"].update(message_thread_id=True), u)[1])(e.message("founder", "boolean topic")), "invalid_topic"),
    (lambda e: (lambda u: (u["message"].pop("message_thread_id"), u)[1])(e.message("founder", "marker without a topic")), "invalid_topic"),
])
def test_a_message_outside_an_exact_configured_topic_is_ignored_and_never_falls_back_to_another_topic(env, build, reason):
    bridge = ready(env)
    update = build(env)
    body = update["message"]["text"]
    env.api.push(update)
    bridge.poll_once()
    assert ignored(env) == ("ignored", reason)
    assert inputs(env) == [] and env.rows("SELECT * FROM agent_messages") == [] and list(bridge.spool.directory.glob("*.spool")) == []
    assert body not in every_stored_text(env) and bridge.owner_terminal.calls == []


def test_a_secret_shaped_message_in_an_unrouted_topic_is_never_stored_or_spooled(env):
    bridge = ready(env)
    secret_request(env, "marketing")
    update = in_topic(env.message("marketing", "482913"), 99)  # the unbound topic of the same group, inside the request's interval
    env.api.push(update)
    bridge.poll_once()
    assert ignored(env) == ("ignored", "unknown_topic") and "482913" not in every_stored_text(env)
    assert inputs(env) == [] and list(bridge.spool.directory.glob("*.spool")) == []
    assert env.one("SELECT request_state FROM telegram_messages WHERE input_mode='secret'")["request_state"] == "pending"


def test_a_reply_inside_its_own_topic_keeps_its_context_and_a_reply_across_topics_is_refused_unread(env):
    bridge = ready(env)
    _, _, _ = queue(env, "Founder draft is ready", chat="founder", purpose="update")
    queue(env, "Marketing draft is ready", chat="marketing", purpose="update")
    bridge.dispatcher.run()
    provider = {m["text"]: m["message_id"] for m in env.api.sent}
    owner_says(env, "founder", "looks good", reply_to=provider["Founder draft is ready"])
    bridge.owner_inputs.run()
    assert "In reply to your earlier message" in bridge.owner_terminal.calls[0]["text"]
    before = len(inputs(env))
    env.api.push(env.message("founder", "wrong topic words", reply_to=provider["Marketing draft is ready"]))
    bridge.poll_once()
    assert ignored(env) == ("ignored", "cross_topic_reply") and len(inputs(env)) == before
    assert "wrong topic words" not in every_stored_text(env)
    bridge.dispatcher.run()
    notice = env.api.sent[-1]
    assert "another topic" in notice["text"] and (notice["chat_id"], notice["message_thread_id"]) == (GROUP_ID, TOPIC_IDS["founder"])
    # Replying to a message that is not in the bridge's history stays as before: a note, never a cross-topic refusal.
    assert len(bridge.owner_terminal.calls) == 1


def test_an_unknown_reply_parent_is_not_a_cross_topic_reply(env):
    bridge = ready(env)
    row = owner_says(env, "founder", "about something old", reply_to=424242)
    assert row["state"] == "queued" and row["purpose"] == "owner_message"


def test_secret_requests_govern_only_their_own_topic(env):
    bridge = ready(env)
    marketing = secret_request(env, "marketing")
    founder = secret_request(env, "founder")  # the open-secret slot is per logical topic, so both can be pending at once
    assert (marketing["message_thread_id"], founder["message_thread_id"]) == (TOPIC_IDS["marketing"], TOPIC_IDS["founder"])
    row = owner_says(env, "founder", "654321")
    assert row["sensitive"] == 1 and row["spool_id"] and row["agent_id"] == env.agents["founder"]
    assert "654321" not in every_stored_text(env)
    states = {r["chat_name"]: r["request_state"] for r in env.rows("SELECT chat_name, request_state FROM telegram_messages WHERE request_key='sms'")}
    assert states == {"founder": "answered", "marketing": "pending"}
    # A secret prompt in one topic is never answered from another topic, even by reply: it is refused without being read.
    before = len(inputs(env))
    env.api.push(env.message("ordubashy", "111222", reply_to=marketing["telegram_message_id"]))
    bridge.poll_once()
    assert ignored(env) == ("ignored", "cross_topic_reply") and len(inputs(env)) == before and "111222" not in every_stored_text(env)
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", marketing["id"])["request_state"] == "pending"
    # Ordinary text in an unrelated topic during the open marketing request is ordinary, not secret.
    plain = owner_says(env, "ordubashy", "plain words")
    assert plain["sensitive"] == 0 and "plain words" in every_stored_text(env)


def test_a_pending_ordinary_request_is_answered_only_from_its_own_topic(env):
    bridge = ready(env)
    request = {"request_key": "q1", "input_mode": "text", "reason": "Need a number", "expires_at": store.plus(bridge.now(), 600)}
    with bridge.write("test") as uow:
        message, _, _ = enqueue_text(bridge, uow, bridge.config.chat("founder"), text="How many?", purpose="blocking_input",
                                     source_kind="agent", source_key=new_id(), agent_id=env.agents["founder"], request=request)
    bridge.dispatcher.run()
    provider = env.one("SELECT telegram_message_id FROM telegram_messages WHERE id=?", message["id"])["telegram_message_id"]
    env.api.push(env.message("marketing", "seven", reply_to=provider))  # another topic answering it: refused unread
    bridge.poll_once()
    assert ignored(env) == ("ignored", "cross_topic_reply") and "seven" not in every_stored_text(env)
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "pending"
    owner_says(env, "founder", "twelve", reply_to=provider)
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "answered"


# ----- sends ----------------------------------------------------------------------------------------------------


def test_every_send_names_its_topic_group_wide(env):
    bridge = ready(env)
    for name in TOPIC_IDS:
        queue(env, f"to {name}", chat=name, purpose="update")
    bridge.dispatcher.run()
    while bridge.dispatcher.run():
        pass
    assert [(m["chat_id"], m["message_thread_id"], m["text"]) for m in env.api.sent] == [
        (GROUP_ID, TOPIC_IDS[name], f"to {name}") for name in TOPIC_IDS]
    stored = env.rows("SELECT chat_name, chat_id, message_thread_id FROM telegram_outbox ORDER BY rowid")
    assert [(r["chat_name"], r["message_thread_id"]) for r in stored] == [(name, TOPIC_IDS[name]) for name in TOPIC_IDS]
    assert {r["chat_id"] for r in stored} == {GROUP_ID}


def test_a_whole_chat_send_names_no_topic(legacy):
    old = legacy.ready()
    with old.write("test") as uow:
        enqueue_text(old, uow, old.config.chat("founder"), text="legacy", purpose="update", source_kind="agent", source_key="l1",
                     agent_id=legacy.agents["founder"])
    old.dispatcher.run()
    assert legacy.api.sent == [{"chat_id": CHAT_IDS["founder"], "text": "legacy", "reply_to": None, "message_id": legacy.api.sent[0]["message_id"]}]
    assert legacy.one("SELECT message_thread_id FROM telegram_outbox")["message_thread_id"] is None


def test_a_reply_send_threads_inside_its_own_topic_only(env):
    bridge = ready(env)
    first, _, _ = queue(env, "first", chat="founder", purpose="update")
    other, _, _ = queue(env, "elsewhere", chat="marketing", purpose="update")
    bridge.dispatcher.run()
    while bridge.dispatcher.run():
        pass
    with bridge.write("test") as uow:
        first_row = store.message(uow, first["id"])
        other_row = store.message(uow, other["id"])
    queue(env, "second", chat="founder", source_key="s2", reply_to=first_row)
    bridge.dispatcher.run()
    assert env.api.sent[-1]["reply_to"] == first_row["telegram_message_id"] and env.api.sent[-1]["message_thread_id"] == TOPIC_IDS["founder"]
    # A reply whose parent sits in another topic is refused when it is queued and again at dispatch, never sent into the wrong one.
    from bazarlyk.telegram.operations import TelegramOperations  # noqa: F401 - the native tool validates through the same helper
    message, row, _ = queue(env, "crossed", chat="founder", source_key="s3", reply_to=other_row)
    sent = len(env.api.sent)
    bridge.dispatcher.run()
    assert len(env.api.sent) == sent
    assert env.one("SELECT state, error_code FROM telegram_outbox WHERE id=?", row["id"]) == {
        "state": "failed", "error_code": "reply_destination_mismatch"}


@pytest.mark.parametrize("mutate", [
    lambda r: {**r, "message_thread_id": 99},
    lambda r: {k: v for k, v in r.items() if k != "message_thread_id"},
    lambda r: {k: v for k, v in r.items() if k != "chat"},
    lambda r: {**r, "chat": {"id": -5, "type": "supergroup"}},
    lambda r: {**r, "is_topic_message": False},
    lambda r: {"message_id": r["message_id"]},
], ids=["other_topic", "no_thread", "no_chat", "other_chat", "not_a_topic_message", "minimal"])
def test_a_topic_send_whose_response_does_not_confirm_the_destination_is_uncertain_and_never_repeated(env, mutate):
    bridge = ready(env)
    env.api.response_mutator = mutate
    message, row, _ = queue(env, "careful", chat="marketing", purpose="update")
    bridge.dispatcher.run()
    stored = env.one("SELECT * FROM telegram_outbox WHERE id=?", row["id"])
    assert stored["state"] == "uncertain" and stored["provider_message_id"] is None
    assert env.one("SELECT telegram_message_id FROM telegram_messages WHERE id=?", message["id"])["telegram_message_id"] is None
    env.api.response_mutator = None
    env.clock.advance(3600)
    for _ in range(3):
        bridge.dispatcher.run()
    assert len(env.api.sent) == 1  # one attempt only: the message may already be somewhere, so it is never sent again
    listed = bridge.status()["uncertain_sends"]
    assert [(u["outbox_id"], u["chat"], u["message_thread_id"]) for u in listed] == [(row["id"], "marketing", TOPIC_IDS["marketing"])]


def test_a_send_is_closed_when_its_destination_moved_between_the_intent_and_the_dispatch(env):
    bridge = ready(env)
    _, row, _ = queue(env, "before the move", chat="marketing", purpose="update")
    original = bridge.config
    from dataclasses import replace
    moved = replace(original, chats=tuple(replace(c, message_thread_id=77) if c.name == "marketing" else c for c in original.chats))
    # The generation and fingerprint are unchanged here, so only the destination guard can stop it.
    bridge.config = moved
    bridge.binding_fingerprint = original.binding_fingerprint()
    try:
        bridge.check_binding = lambda uow: {"generation": row["config_generation"], "fingerprint": row["binding_fingerprint"]}
        bridge.dispatcher.run()
    finally:
        bridge.config = original
    failed = env.one("SELECT state, error_code FROM telegram_outbox WHERE id=?", row["id"])
    assert failed == {"state": "failed", "error_code": "destination_changed"} and env.api.sent == []


# ----- edits, urgent events and rebinds -------------------------------------------------------------------------


def test_an_edit_addresses_only_the_exact_destination_of_its_message(env):
    from bazarlyk.telegram.outbox import enqueue_edit
    bridge = ready(env)
    message, _, _ = queue(env, "status: firing", chat="urgent", purpose="update")
    bridge.dispatcher.run()
    with bridge.write("t") as uow:
        target = store.message(uow, message["id"])
    with pytest.raises(DomainError, match="not at the currently configured destination"):
        with bridge.write("t") as uow:
            enqueue_edit(bridge, uow, bridge.config.chat("founder"), target, "x", source_kind="agent", source_key="wrong-topic")
    with bridge.write("t") as uow:
        enqueue_edit(bridge, uow, bridge.config.chat("urgent"), target, "status: resolved", source_kind="agent", source_key="right-topic")
        # A queued edit that reaches dispatch aimed at another topic's message is failed there too.
        store.enqueue(uow, bridge.config.chat("founder"), 1, bridge.config.binding_fingerprint(), kind="edit", source_kind="agent",
                      source_key="forged", payload={"text": "x", "target_message_id": target["id"]}, now=bridge.now(), message_id=target["id"])
    bridge.dispatcher.run()
    rows = env.rows("SELECT source_key, state, error_code FROM telegram_outbox WHERE kind='edit' ORDER BY source_key")
    assert rows == [{"source_key": "forged", "state": "failed", "error_code": "target_destination_mismatch"},
                    {"source_key": "right-topic", "state": "sent", "error_code": None}]
    assert env.api.edited == [{"chat_id": GROUP_ID, "message_id": env.api.sent[0]["message_id"], "text": "status: resolved"}]


def test_moving_a_topic_is_a_new_generation_that_keeps_the_cursor_and_closes_what_was_queued(env):
    bridge = ready(env)
    env.api.push(env.message("founder", "one"))
    bridge.poll_once()
    before = env.one("SELECT * FROM telegram_state")
    _, queued, _ = queue(env, "queued before the move", chat="founder", purpose="update")
    env.clock.advance(1)
    state = rebind(env, bridge, founder=77)
    assert state["config_generation"] == before["config_generation"] + 1 and state["poll_offset"] == before["poll_offset"]
    assert state["activation_at"] == before["activation_at"] and state["binding_effective_at"] > before["binding_effective_at"]
    assert state["binding_fingerprint"] != before["binding_fingerprint"]
    bridge.dispatcher.run()
    failed = env.one("SELECT state, error_code FROM telegram_outbox WHERE id=?", queued["id"])
    assert failed == {"state": "failed", "error_code": "binding_changed"} and env.api.sent == []
    # New sends use the new topic; the old topic is no longer routed, and what was said there keeps no text.
    queue(env, "after the move", chat="founder", purpose="update", source_key="after")
    bridge.dispatcher.run()
    assert (env.api.sent[-1]["message_thread_id"], env.api.sent[-1]["chat_id"]) == (77, GROUP_ID)
    env.clock.advance(2)
    env.api.push(env.message("founder", "said in the old topic"))
    bridge.poll_once()
    assert ignored(env) == ("ignored", "unknown_topic") and "said in the old topic" not in every_stored_text(env)
    env.api.push(in_topic(env.message("founder", "said in the new topic"), 77))
    bridge.poll_once()
    assert inputs(env)[-1]["agent_id"] == env.agents["founder"] and inputs(env)[-1]["state"] == "queued"


def test_a_rebind_ends_an_open_request_at_the_old_destination_and_frees_the_slot(env):
    bridge = ready(env)
    request = {"request_key": "q1", "input_mode": "text", "reason": "Need a number", "expires_at": store.plus(bridge.now(), 3000)}
    with bridge.write("test") as uow:
        message, _, _ = enqueue_text(bridge, uow, bridge.config.chat("founder"), text="How many?", purpose="blocking_input",
                                     source_kind="agent", source_key=new_id(), agent_id=env.agents["founder"], request=request)
    bridge.dispatcher.run()
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "pending"
    env.clock.advance(1)
    rebind(env, bridge, founder=77)
    closed = env.one("SELECT request_state, message_thread_id, request_closed_at, body FROM telegram_messages WHERE id=?", message["id"])
    assert closed["request_state"] == "cancelled" and closed["message_thread_id"] == TOPIC_IDS["founder"]  # its own destination is kept
    assert closed["request_closed_at"] and closed["body"] == "How many?"
    with bridge.write("test") as uow:  # the new topic can ask the same question right away
        _, row, _ = enqueue_text(bridge, uow, bridge.config.chat("founder"), text="How many now?", purpose="blocking_input",
                                 source_kind="agent", source_key=new_id(), agent_id=env.agents["founder"], request=request)
    bridge.dispatcher.run()
    assert env.api.sent[-1]["message_thread_id"] == 77


def test_converting_separate_chats_to_one_forum_group_starts_a_generation_and_routes_by_topic(tmp_path):
    e = Env(tmp_path)
    e.bridge.owner_terminal = FakeTerminal()
    try:
        bridge = e.ready()
        bridge.owner_terminal = e.bridge.owner_terminal
        e.api.push(e.message("founder", "old chat"))
        bridge.poll_once()
        before = e.one("SELECT * FROM telegram_state")
        e.api.chat_ids.update({name: GROUP_ID for name in TOPIC_IDS})
        e.api.forum_chats.add(GROUP_ID)

        def convert(data):
            for name, chat in data["chats"].items():
                chat.update(chat_id=GROUP_ID, message_thread_id=TOPIC_IDS[name])
        e.write_config(mutate=convert)
        e.clock.advance(1)
        assert bridge._ensure_ready(), bridge.health()
        state = e.one("SELECT * FROM telegram_state")
        assert state["config_generation"] == 2 and state["poll_offset"] == before["poll_offset"] and state["activation_at"] == before["activation_at"]
        # The old chat history is retained, at its own destination, and is no longer a routing target.
        old = e.one("SELECT chat_id, message_thread_id, body FROM telegram_messages WHERE body='old chat'")
        assert (old["chat_id"], old["message_thread_id"]) == (CHAT_IDS["founder"], None)
        e.clock.advance(2)
        e.topics = True
        e.api.push(e.message("founder", "new topic"))
        e.api.push({"update_id": e.next_update_id(), "message": {**e.message("founder", "old chat again")["message"],
                    "chat": {"id": CHAT_IDS["founder"], "type": "supergroup"}}})
        bridge.poll_once()
        assert inputs(e)[-1]["agent_id"] == e.agents["founder"]
        assert ignored(e) == ("ignored", "unknown_chat")
    finally:
        e.close()


# ----- cleanup --------------------------------------------------------------------------------------------------


def sent_message(env, text="delete me", chat="signoffs"):
    message, _, _ = queue(env, text, chat=chat, purpose="update")
    env.bridge.dispatcher.run()
    return env.one("SELECT * FROM telegram_messages WHERE id=?", message["id"])


def enqueue_cleanup(env, message_id, reason="test"):
    with env.bridge.write("t") as uow:
        return store.enqueue_cleanup(uow, message_id, reason, now=env.bridge.now())


def test_cleanup_deletes_only_at_the_exact_saved_destination_and_reports_a_moved_one_with_its_original_link(env):
    bridge = ready(env)
    known = sent_message(env, chat="signoffs")
    moved = sent_message(env, "in a topic that will move", chat="founder")
    enqueue_cleanup(env, known["id"])
    bridge.cleanup.run()
    assert env.api.deleted == [(GROUP_ID, known["telegram_message_id"])]
    enqueue_cleanup(env, moved["id"], "will_move")
    env.clock.advance(1)
    rebind(env, bridge, founder=77)
    bridge.cleanup.run()
    job = env.one("SELECT state, error_code FROM telegram_cleanup_jobs WHERE message_id=?", moved["id"])
    assert (job["state"], job["error_code"]) == ("failed", "unknown_chat")
    assert env.api.deleted == [(GROUP_ID, known["telegram_message_id"])]  # never deleted in a destination it was not saved for
    failure = bridge.status()["cleanup_failures"][0]
    assert failure["message_id"] == moved["id"] and failure["chat"] == "founder" and failure["message_thread_id"] == TOPIC_IDS["founder"]
    assert failure["still_configured"] is False
    assert failure["link"] == f"https://t.me/c/{str(GROUP_ID)[4:]}/{TOPIC_IDS['founder']}/{moved['telegram_message_id']}"
    assert failure["link"] in failure["instructions"] and "founder topic" in failure["instructions"]
    assert "chat_id" not in failure


def test_manual_cleanup_links_name_the_topic_the_message_was_saved_in():
    from types import SimpleNamespace
    from bazarlyk.telegram.bridge import TelegramBridge
    config = SimpleNamespace(destination=lambda chat_id, thread=None: (
        SimpleNamespace(name="signoffs") if (chat_id, thread) == (GROUP_ID, 14) else None))
    row = {"message_id": "m1", "error_code": "too_old_48h", "reason": "signoff_retired", "chat_name": "signoffs", "chat_id": GROUP_ID,
           "message_thread_id": 14, "telegram_message_id": 77}
    entry = TelegramBridge._manual_cleanup(row, config)
    assert entry["link"] == "https://t.me/c/900100/14/77" and entry["message_thread_id"] == 14 and entry["still_configured"] is True
    assert entry["link"] in entry["instructions"] and "signoffs topic" in entry["instructions"]
    moved = TelegramBridge._manual_cleanup({**row, "message_thread_id": 15}, config)
    assert moved["link"] == "https://t.me/c/900100/15/77" and moved["still_configured"] is False
    unsent = TelegramBridge._manual_cleanup({**row, "telegram_message_id": None}, config)
    assert unsent["link"] is None and "by hand" in unsent["instructions"]


def test_status_names_the_destination_of_an_uncertain_send_without_identifiers_or_text(env):
    bridge = ready(env)
    env.api.script("send_message", TelegramApiError("timeout", "slow"))
    queue(env, "secret-ish words", chat="urgent", purpose="update")
    bridge.dispatcher.run()
    status = bridge.status()
    rendered = json.dumps(status)
    assert "secret-ish words" not in rendered and str(GROUP_ID) not in rendered and "AAAAAAAAAA" not in rendered
    assert status["binding"]["state"] == "matched"


# ----- native tools and the owner's routes ----------------------------------------------------------------------

NOTE = {"event_key": "db-down", "title": "Database unreachable", "text": "Writes fail; customers cannot log in. Restore or fail over.",
        "reason": "Data loss risk"}


@pytest.fixture
def service(tmp_path):
    s = TelegramService(tmp_path, topics=True)
    try:
        yield s
    finally:
        s.close()


def send(s, agent, text="Hello owner", purpose="update", **extra):
    return s.call(agent, "telegram_send", {"text": text, "purpose": purpose, **extra})


def test_each_agent_sends_only_into_its_own_topic_and_history_never_crosses_topics(service):
    s = service
    for agent, name in ((s.ordubashy, "ordubashy"), (s.founder, "founder"), (s.marketing, "marketing")):
        s.ok(send(s, agent, f"From {name}"))
    s.dispatch()
    assert {(m["chat_id"], m["message_thread_id"], m["text"]) for m in s.api.sent} == {
        (GROUP_ID, TOPIC_IDS[n], f"From {n}") for n in ("ordubashy", "founder", "marketing")}
    # A saved message of the same agent and logical chat, but from before topics (no topic), is kept and never shown.
    from dataclasses import replace
    old_layout = replace(s.bridge.config.chat("founder"), message_thread_id=None)
    with s.bridge.write("test") as uow:
        store.insert_message(uow, direction="outbound", chat=old_layout, purpose="update", body="old layout words", now=s.bridge.now(),
                             agent_id=s.founder.id)
    assert [i["text"] for i in s.ok(s.call(s.founder, "telegram_history"))["items"]] == ["From founder"]
    assert [i["text"] for i in s.ok(s.call(s.marketing, "telegram_history"))["items"]] == ["From marketing"]
    assert s.rows("SELECT count(*) n FROM telegram_messages WHERE body='old layout words'")[0]["n"] == 1
    # Callers still cannot name a destination: the closed schema has no chat or topic field.
    for field, value in (("message_thread_id", 13), ("chat_id", GROUP_ID), ("topic", "marketing")):
        assert send(s, s.founder, **{field: value}).status == "error"


def test_a_reply_to_a_message_of_another_topic_is_refused_before_anything_is_stored(service):
    s = service
    theirs = s.ok(send(s, s.marketing, "elsewhere"))
    before = s.rows("SELECT count(*) n FROM telegram_messages")[0]["n"]
    foreign = send(s, s.founder, "cross-topic", "reply", reply_to=theirs["message_id"])
    assert foreign.status == "error" and foreign.error["code"] == "not_found"
    assert s.rows("SELECT count(*) n FROM telegram_messages")[0]["n"] == before


def test_urgent_events_post_in_the_urgent_topic_and_a_moved_topic_gets_a_fresh_post_never_an_edit_elsewhere(service):
    s = service
    s.ok(s.owner("telegram_notify", NOTE))
    s.dispatch()
    assert (s.api.sent[0]["chat_id"], s.api.sent[0]["message_thread_id"]) == (GROUP_ID, TOPIC_IDS["urgent"])
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "Still failing; fail over started."}))
    s.dispatch()
    assert len(s.api.sent) == 1 and [e["chat_id"] for e in s.api.edited] == [GROUP_ID]
    s.clock.advance(2)
    s.write_config(mutate=lambda data: data["chats"]["urgent"].update(message_thread_id=77))
    assert s.bridge._ensure_ready(), s.bridge.health()
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "Fail over finished, still degraded."}))
    s.dispatch()
    assert len(s.api.sent) == 2 and (s.api.sent[1]["chat_id"], s.api.sent[1]["message_thread_id"]) == (GROUP_ID, 77)
    assert len(s.api.edited) == 1  # the post in the old topic was not edited again


def test_the_owner_sees_the_topic_of_an_uncertain_send_and_resolves_it_by_receipt(service):
    s = service
    s.api.response_mutator = lambda r: {**r, "message_thread_id": 99}  # lands somewhere else, or so the response says
    sent = s.ok(send(s, s.marketing, "where did it go"))
    s.dispatch()
    listed = s.ok(s.owner("telegram_status"))["uncertain_sends"]
    assert [(u["chat"], u["message_thread_id"], u["message_id"]) for u in listed] == [("marketing", TOPIC_IDS["marketing"], sent["message_id"])]
    view = s.ok(s.owner("telegram_delivery_get", {"message_id": sent["message_id"]}))["items"][0]
    assert (view["chat"], view["message_thread_id"], view["state"]) == ("marketing", TOPIC_IDS["marketing"], "uncertain")
    s.api.response_mutator = None
    delivery = view["deliveries"][-1]
    # The owner found the message where it belonged and confirms its exact ID; nothing is sent again.
    s.ok(s.owner("telegram_delivery_resolve", {"outbox_id": delivery["outbox_id"], "action": "mark_delivered",
                                               "provider_message_id": s.api.sent[0]["message_id"],
                                               "expected_revision": delivery["receipt_revision"]}))
    assert s.rows("SELECT state FROM telegram_outbox")[0]["state"] == "sent" and len(s.api.sent) == 1
    assert not s.ok(s.owner("telegram_status"))["uncertain_sends"]


def test_a_retry_of_an_uncertain_send_after_its_topic_moved_fails_closed_instead_of_reaching_the_new_topic(service):
    s = service
    s.api.response_mutator = lambda r: {**r, "chat": {"id": -5, "type": "supergroup"}}
    sent = s.ok(send(s, s.marketing, "will not be retried elsewhere"))
    s.dispatch()
    s.api.response_mutator = None
    delivery = s.ok(s.owner("telegram_delivery_get", {"message_id": sent["message_id"]}))["items"][0]["deliveries"][-1]
    s.clock.advance(2)
    s.write_config(mutate=lambda data: data["chats"]["marketing"].update(message_thread_id=77))
    assert s.bridge._ensure_ready(), s.bridge.health()
    result = s.owner("telegram_delivery_resolve", {"outbox_id": delivery["outbox_id"], "action": "retry",
                                                   "expected_revision": delivery["receipt_revision"]})
    s.dispatch()
    final = s.rows("SELECT state, error_code FROM telegram_outbox")[0]
    assert final == {"state": "failed", "error_code": "binding_changed"}  # the new generation already closes the old intent
    assert len(s.api.sent) == 1  # the one original attempt; the retry never reached Telegram in any topic
    assert result.status == "ok"  # the owner's retry is accepted as a receipt change; dispatch is what refuses it


# ----- Signoffs in their topic ----------------------------------------------------------------------------------

from test_telegram_signoffs import BLOCKING, NOTES_ONLY, World, only_post  # noqa: E402  (the real Signoff scenario, in topic mode)


class TopicWorld(World):
    """The real Signoff service with a bridge whose six destinations are topics of one forum group."""

    topics = True


@pytest.fixture
def topic_world(tmp_path):
    w = TopicWorld(tmp_path)
    yield w
    w.close()


def test_a_post_goes_to_the_signoffs_topic_and_a_thumb_that_names_no_topic_decides_through_its_stored_post(topic_world):
    w = topic_world
    proposal = w.action()
    w.settle()
    sent = w.api.sent[0]
    assert (sent["chat_id"], sent["message_thread_id"], sent["reply_to"]) == (GROUP_ID, TOPIC_IDS["signoffs"], None)
    post = only_post(w)
    assert w.one("SELECT message_thread_id FROM telegram_messages WHERE id=?", post["message_id"])["message_thread_id"] == TOPIC_IDS["signoffs"]
    provider = w.provider(post)
    update = w.reaction("signoffs", provider, ["👍"])
    assert "message_thread_id" not in update["message_reaction"]  # Telegram's reaction updates carry no topic
    w.say(update)
    state, decision = w.decisions()[proposal["id"]]
    assert state == "approved" and decision["kind"] == "approve"
    assert (GROUP_ID, provider) in w.api.deleted  # the retired post is deleted at its own saved destination


def test_a_thumb_on_a_message_of_another_topic_decides_nothing_and_says_nothing_there(topic_world):
    w = topic_world
    proposal = w.action()
    w.settle()
    with w.bridge.write("test") as uow:
        enqueue_text(w.bridge, uow, w.bridge.config.chat("founder"), text="A founder note", purpose="notice", source_kind="notice",
                     source_key="u1")
    w.pump()
    founder_message = w.api.sent[-1]
    assert founder_message["message_thread_id"] == TOPIC_IDS["founder"]
    notices_before = w.notices()
    w.say(w.reaction("founder", founder_message["message_id"], ["👍"]))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "other_destination"
    assert w.decisions()[proposal["id"]][0] == "pending" and w.notices() == notices_before
    # A reaction on a message this bridge never sent is the unknown-post case, exactly as before.
    w.say(w.reaction("signoffs", 987654, ["👍"]))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "unknown_post"


def test_a_reply_in_the_signoffs_topic_answers_its_post_and_a_plain_or_cross_topic_message_never_does(topic_world):
    w = topic_world
    row = create_input(w.s, form=NOTES_ONLY, attention=BLOCKING)
    w.settle()
    post = only_post(w)
    w.say(w.message("signoffs", "just chatting"))  # the topic's own anchor is not a reply
    assert any("Reply directly to a Signoff post" in n for n in w.notices())
    assert w.decisions()[row["id"]][0] == "pending" and w.handoffs() == []
    with w.bridge.write("test") as uow:
        enqueue_text(w.bridge, uow, w.bridge.config.chat("marketing"), text="A marketing note", purpose="notice", source_kind="notice",
                     source_key="m1")
    w.pump()
    foreign = w.api.sent[-1]["message_id"]
    w.say(w.message("signoffs", "crossed words", reply_to=foreign))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "cross_topic_reply"
    assert "crossed words" not in every_stored_text(w) and w.decisions()[row["id"]][0] == "pending" and w.handoffs() == []
    w.reply(post, "Use the first supplier for the trial order.")
    state, _ = w.decisions()[row["id"]]
    assert state == "answered" and len(w.handoffs()) == 1
    reply = w.one("SELECT * FROM telegram_messages WHERE purpose='signoff_reply'")
    assert (reply["chat_id"], reply["message_thread_id"]) == (GROUP_ID, TOPIC_IDS["signoffs"])
    assert (GROUP_ID, reply["telegram_message_id"]) in w.api.deleted


def test_moving_the_signoffs_topic_retires_the_old_post_without_deleting_it_and_publishes_in_the_new_topic(topic_world):
    w = topic_world
    proposal = w.action()
    w.settle()
    old_post = only_post(w)
    old_provider = w.provider(old_post)
    w.write_config(mutate=lambda data: data["chats"]["signoffs"].update(message_thread_id=77))
    w.clock.advance(2)
    assert w.bridge._ensure_ready() is True and w.bridge.status()["binding"]["generation"] == 2
    # A thumb on the old post no longer decides: the post is at a destination that is not the current Signoffs topic.
    w.clock.advance(1)
    w.say(w.reaction("signoffs", old_provider, ["👍"], at=w.clock.now))
    assert w.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "other_destination"
    assert w.decisions()[proposal["id"]][0] == "pending"
    w.clock.advance(w.bridge.timings["sweep_interval"])
    w.pump()
    w.pump()
    old = w.post(proposal["id"], id=old_post["id"])
    assert old["state"] == "retired" and old["retire_reason"] == "chat_changed"
    fresh = [p for p in w.posts(proposal["id"]) if p["id"] != old_post["id"]]
    assert len(fresh) == 1 and fresh[0]["state"] == "active" and fresh[0]["generation"] == 2
    republished = [m for m in w.api.sent if m["message_id"] == w.provider(fresh[0])]
    assert [(m["chat_id"], m["message_thread_id"]) for m in republished] == [(GROUP_ID, 77)]
    assert (GROUP_ID, old_provider) not in w.api.deleted  # never deleted as though it were in the new topic
    # Whatever cleanup was asked for the old post is a failed job with a manual link to the topic it really sits in.
    failures = [f for f in w.bridge.status()["cleanup_failures"] if f["message_id"] == old_post["message_id"]]
    assert failures and failures[0]["message_thread_id"] == TOPIC_IDS["signoffs"] and failures[0]["still_configured"] is False
    assert failures[0]["link"] == f"https://t.me/c/{str(GROUP_ID)[4:]}/{TOPIC_IDS['signoffs']}/{old_provider}"
    # The new owner-facing post decides normally.
    w.react(fresh[0], "👍")
    assert w.decisions()[proposal["id"]][0] == "approved"


def test_an_owner_repin_to_an_unchanged_topic_keeps_and_answers_its_outstanding_post(topic_world):
    # The approved repin behavior, unchanged, with the Signoffs destination a topic of the shared group.
    from test_telegram_signoffs import test_an_active_post_survives_an_owner_repin_and_the_new_owner_answers_it_after_the_new_effective_time as case
    case(topic_world)


def test_a_restart_in_topic_mode_keeps_its_posts_and_their_exact_destinations(topic_world):
    from test_telegram_signoffs import test_a_restart_reconciles_from_saved_state_without_duplicating_posts_or_replaying_history as case
    case(topic_world)
    assert {(m["chat_id"], m["message_thread_id"]) for m in topic_world.api.sent} == {(GROUP_ID, TOPIC_IDS["signoffs"])}


def test_an_external_resolution_deletes_only_the_mapped_post_in_its_own_topic(topic_world):
    w = topic_world
    proposal = w.action()
    w.settle()
    post = only_post(w)
    provider = w.provider(post)
    with w.bridge.write("test") as uow:
        enqueue_text(w.bridge, uow, w.bridge.config.chat("founder"), text="A founder note", purpose="notice", source_kind="notice",
                     source_key="n1")
    w.pump()
    note = next(m for m in w.api.sent if m["text"] == "A founder note")
    assert w.s.decide(proposal, "reject", feedback="Rejected in the dashboard").status == "ok"
    w.pump()
    retired = w.post(proposal["id"])
    assert retired["state"] == "retired" and retired["retire_reason"] == "resolved"
    assert w.api.deleted == [(GROUP_ID, provider)]  # the neighbouring topic's message, in the same group, is untouched
    assert note["message_id"] not in w.deleted_ids()


def test_a_send_waiting_to_retry_keeps_its_exact_destination(env):
    bridge = ready(env)
    env.api.script("send_message", TelegramApiError("unreachable", "network down"))
    _, row, _ = queue(env, "retry me", chat="marketing", purpose="update")
    bridge.dispatcher.run()
    waiting = env.one("SELECT state, message_thread_id FROM telegram_outbox WHERE id=?", row["id"])
    assert (waiting["state"], waiting["message_thread_id"]) == ("queued", TOPIC_IDS["marketing"]) and env.api.sent == []
    env.clock.advance(3600)
    bridge.dispatcher.run()
    assert [(m["chat_id"], m["message_thread_id"], m["text"]) for m in env.api.sent] == [(GROUP_ID, TOPIC_IDS["marketing"], "retry me")]
    # A restarted process resumes from saved state and sends nothing twice.
    bridge.lock.release()
    bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    env.clock.advance(3600)
    restarted.dispatcher.run()
    assert len(env.api.sent) == 1 and env.one("SELECT state FROM telegram_outbox WHERE id=?", row["id"])["state"] == "sent"


def test_a_queued_owner_input_is_fenced_when_its_topic_is_reassigned_in_the_same_group(env):
    bridge = ready(env)
    queued = owner_says(env, "founder", "typed before the move")
    assert queued["state"] == "queued"
    env.clock.advance(1)
    rebind(env, bridge, founder=77)
    bridge.owner_inputs.run()
    failed = env.one("SELECT state, error_code FROM telegram_owner_inputs WHERE id=?", queued["id"])
    assert (failed["state"], failed["error_code"]) == ("failed", "binding_changed") and bridge.owner_terminal.calls == []
    # The canonical inbox message stays, exactly as for any other binding change.
    assert env.one("SELECT id FROM agent_messages WHERE id=?", queued["correlation_id"]) is not None
