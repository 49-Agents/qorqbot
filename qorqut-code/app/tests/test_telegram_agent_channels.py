"""Acceptance case: Ordubashy is the only agent with a Telegram channel.

Founder, Marketing and every worker have none. The private configuration names exactly three destinations (Ordubashy, Signoffs
and Urgent Notifications) and a configuration that still names Founder, Marketing or the retired Daily Reports chat is refused,
never ignored.

The first group checks the configuration alone (strict validation, the redacted example, the documented conversion of the
earlier six-destination shape and the operator ``validate`` command). The second runs the real bridge on one synthetic v1.2
host with a fake Bot API and a fake clock: Ordubashy's two-way flow and the non-agent Urgent post work, every other agent is
refused and unrouted and an owner message in a former Founder or Marketing topic is ignored with no fallback. The store itself
accepts only the three retained chat names.

Synthetic only: tmp files and rows the test writes, invented identifiers and a bot token that belongs to no bot. No network,
Telegram service, model, native process or live configuration is used."""

import copy
import json
import os
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.telegram import store
from bazarlyk.telegram.__main__ import cmd_validate
from bazarlyk.telegram.api import BotApi
from bazarlyk.telegram.bridge import TelegramBridge
from bazarlyk.telegram.config import AGENT_CHATS, CHAT_NAMES, parse_config, redacted_example
from bazarlyk.telegram.outbox import enqueue_notice

OWNER_TG, BOT_ID = 1001, 200200200
TOKEN = f"{BOT_ID}:" + "A" * 35
GROUP_ID = -100900100
TOPICS = {"ordubashy": 11, "founder": 12, "marketing": 13, "signoffs": 14, "daily_reports": 15, "urgent": 16}
FORMER_AGENT_CHATS = ("founder", "marketing")
RETIRED_CHATS = (*FORMER_AGENT_CHATS, "daily_reports")
INSTALLATION, OWNER_PRINCIPAL = "00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2"
AGENT_IDS = {"ordubashy": "11111111-1111-4111-8111-111111111111", "founder": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
             "marketing": "33333333-3333-4333-8333-333333333333"}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def earlier_six_chat_shape(agents=AGENT_IDS, *, installation=INSTALLATION, owner=OWNER_PRINCIPAL, enabled=False):
    """The configuration shape that existed before: three agent topics and three outbound topics in one forum group."""
    return {
        "version": 1, "enabled": enabled, "bot_token_file": "/synthetic/private/bot-token", "owner_telegram_user_id": OWNER_TG,
        "expected_bot_id": BOT_ID, "expected_installation_id": installation, "expected_owner_principal_id": owner,
        "dashboard_origin": "http://qorqut:1071", "activation_at": (START - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "chats": {name: {"chat_id": GROUP_ID, "type": "supergroup", "message_thread_id": topic,
                         **({"agent_id": agents[name]} if name in agents else {})} for name, topic in TOPICS.items()}}


def final_shape(**kwargs):
    """The same configuration with the Founder, Marketing and Daily Reports entries removed: the one edit the conversion needs."""
    data = earlier_six_chat_shape(**kwargs)
    for name in RETIRED_CHATS:
        del data["chats"][name]
    return data


def refused(data, **kwargs):
    with pytest.raises(DomainError) as error:
        parse_config(data, **kwargs)
    assert error.value.code == "invalid_configuration"
    return error.value.message


# ----- the configuration alone ----------------------------------------------------------------------------------


def test_only_ordubashy_is_an_agent_chat():
    assert AGENT_CHATS.keys() == {"ordubashy"} and CHAT_NAMES == ("ordubashy", "signoffs", "urgent")


def test_the_final_shape_and_the_redacted_example_are_accepted_complete():
    for data in (final_shape(), redacted_example()):
        config = parse_config(data, require_complete=True)
        assert [chat.name for chat in config.chats] == list(CHAT_NAMES)
        assert [chat.name for chat in config.chats if chat.agent_id] == ["ordubashy"]
    assert not any(name in json.dumps(redacted_example()) for name in FORMER_AGENT_CHATS)


@pytest.mark.parametrize("enabled", [True, False])
def test_a_configuration_that_names_founder_or_marketing_is_refused_not_ignored(enabled):
    old = earlier_six_chat_shape(enabled=enabled)
    assert "only Ordubashy has an agent chat" in refused(old, require_complete=True)
    assert "only Ordubashy has an agent chat" in refused(old)       # a staged draft is refused as well
    for name in FORMER_AGENT_CHATS:
        extra = final_shape(enabled=enabled)
        extra["chats"][name] = old["chats"][name]
        refused(extra)
        del extra["chats"][name]["agent_id"]                         # a plain topic under the former name is no way around it
        refused(extra)


@pytest.mark.parametrize("enabled", [True, False])
def test_a_configuration_that_still_names_the_retired_daily_reports_chat_is_refused_not_ignored(enabled):
    data = final_shape(enabled=enabled)
    data["chats"]["daily_reports"] = earlier_six_chat_shape(enabled=enabled)["chats"]["daily_reports"]
    assert "exactly Ordubashy, Signoffs and Urgent Notifications" in refused(data, require_complete=True)
    refused(data)                                                    # a staged draft is refused as well
    assert "daily_reports" not in json.dumps(redacted_example())


def test_every_destination_is_required_and_only_ordubashy_binds_an_agent():
    for name in CHAT_NAMES:
        missing = final_shape()
        del missing["chats"][name]
        refused(missing, require_complete=True)
    for name in ("signoffs", "urgent"):
        bound = final_shape()
        bound["chats"][name]["agent_id"] = AGENT_IDS["founder"]
        assert "Only the Ordubashy chat binds an agent" in refused(bound)
    for agent in (None, "not-a-uuid", AGENT_IDS["founder"].upper()):
        data = final_shape()
        data["chats"]["ordubashy"]["agent_id"] = agent
        refused(data)
    data = final_shape()
    del data["chats"]["ordubashy"]["agent_id"]
    refused(data)


def test_the_conversion_only_removes_the_three_retired_entries_and_starts_a_new_generation():
    old, new = earlier_six_chat_shape(), final_shape()
    assert set(old["chats"]) - set(new["chats"]) == set(RETIRED_CHATS)
    assert {key: value for key, value in old.items() if key != "chats"} == {key: value for key, value in new.items() if key != "chats"}
    assert all(new["chats"][name] == old["chats"][name] for name in new["chats"])         # retained entries are byte-identical
    config = parse_config(new, require_complete=True)
    assert config.agent_chat(AGENT_IDS["ordubashy"]).name == "ordubashy"
    # Founder's and Marketing's former identifiers resolve to nothing: no disabled-agent fallback.
    assert config.agent_chat(AGENT_IDS["founder"]) is None and config.agent_chat(AGENT_IDS["marketing"]) is None
    with pytest.raises(DomainError) as unavailable:
        config.chat("founder")
    assert unavailable.value.code == "operation_unavailable"
    assert config.redacted()["configured_chats"] == list(CHAT_NAMES)


def test_the_operator_validate_command_refuses_the_earlier_shape_and_accepts_the_final_one(tmp_path, capsys):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    results = {}
    for label, data in (("earlier", earlier_six_chat_shape()), ("final", final_shape())):
        path = directory / f"{label}.json"
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        code = cmd_validate(SimpleNamespace(config=str(path)))
        results[label] = (code, json.loads(capsys.readouterr().out))
    assert results["earlier"][0] == 1 and results["earlier"][1]["valid"] is False
    assert "only Ordubashy has an agent chat" in results["earlier"][1]["problems"][0]
    assert results["final"][0] == 0 and results["final"][1]["valid"] is True
    assert results["final"][1]["configuration"]["configured_chats"] == list(CHAT_NAMES)


# ----- the bridge on a synthetic host ---------------------------------------------------------------------------


class FakeBotApi(BotApi):
    """Implements exactly the port over invented forum-group answers; it can never reach a network."""

    def __init__(self):
        self.sent, self.updates, self.next_message_id = [], [], 500

    def get_me(self):
        return {"id": BOT_ID, "is_bot": True}

    def get_webhook_info(self):
        return {"url": ""}

    def get_chat(self, chat_id):
        return {"id": chat_id, "type": "supergroup", "is_forum": True}

    def get_chat_member(self, chat_id, user_id):
        if user_id == BOT_ID:
            return {"status": "administrator", "can_delete_messages": True, "can_post_messages": True}
        return {"status": "member"}

    def get_updates(self, offset, timeout):
        return [update for update in self.updates if update["update_id"] >= offset]

    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        self.next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_thread_id": message_thread_id, "message_id": self.next_message_id})
        return {"message_id": self.next_message_id, "chat": {"id": chat_id, "type": "supergroup"},
                "message_thread_id": message_thread_id, "is_topic_message": True}


def owner_message(topic_name, text, update_id, at):
    topic = TOPICS[topic_name]
    return {"update_id": update_id, "message": {
        "message_id": update_id + 1000, "date": int(at.timestamp()), "chat": {"id": GROUP_ID, "type": "supergroup"},
        "from": {"id": OWNER_TG, "is_bot": False}, "message_thread_id": topic, "is_topic_message": True, "text": text,
        "reply_to_message": {"message_id": topic, "forum_topic_created": {"name": topic_name}}}}


@pytest.fixture
def world(tmp_path):
    from host_foundation_support import (build_instance, create_registered_global, historical_founder, historical_marketing,
                                         owner_global_context, register_sources)

    instance, businesses = build_instance(tmp_path)
    register_sources(instance, businesses["first"], "first")
    ids = {"ordubashy": create_registered_global(instance, "ordubashy"), "founder": historical_founder(instance),
           "marketing": historical_marketing(instance), "learning-applier": create_registered_global(instance, "learning-applier")}
    instance.runtime.freeze_registry()
    with instance.database.read() as uow:
        installation = uow.repository("installation").get("service")
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "bot-token"
    token.write_text(TOKEN + "\n")
    os.chmod(token, 0o600)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config_path = config_dir / "telegram.json"
    data = final_shape(agents={"ordubashy": ids["ordubashy"]}, installation=installation["installation_id"],
                       owner=installation["owner_principal_id"], enabled=True)
    data["bot_token_file"] = str(token)
    config_path.write_text(json.dumps(data))
    os.chmod(config_path, 0o600)

    clock, api = SimpleNamespace(now=START), FakeBotApi()
    bridge = instance.runtime.telegram
    bridge.config_path, bridge.environ, bridge.api_factory = str(config_path), {}, lambda config, bot_token: api
    bridge.clock, bridge.sleep, bridge.rng = (lambda: clock.now), (lambda seconds: None), random.Random(1)
    bridge.state_dir_override, bridge.authority_check = tmp_path / "state" / "telegram", None
    bridge.poll_timeout, bridge.tick = 0.05, 0.05
    bridge.load_config()
    assert bridge._ensure_ready(), bridge.health()
    host = SimpleNamespace(instance=instance, runtime=instance.runtime, bridge=bridge, api=api, clock=clock, ids=ids,
                           config_path=config_path, data=data, owner=owner_global_context(instance))
    yield host
    bridge.stop(timeout=5)
    if bridge.lock is not None:
        bridge.lock.release()
    instance.database.close()


READS = {"telegram_history", "telegram_delivery_get"}


def call(host, agent, operation, arguments=None):
    context = host.runtime.resolver.invocation_context(Principal(agent, "agent"))
    if operation in READS:
        return host.runtime.application.invoke(context, operation, arguments or {}, {})
    return host.instance.invoke(context, operation, arguments or {})


def rows(host, sql, *params):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, params)]


def test_ordubashy_sends_and_reads_in_the_final_shape(world):
    sent = call(world, world.ids["ordubashy"], "telegram_send", {"text": "Deploy finished", "purpose": "update"})
    assert sent.status == "ok", sent.to_dict()
    while world.bridge.dispatcher.run():
        pass
    assert [(m["chat_id"], m["message_thread_id"], m["text"]) for m in world.api.sent] == [(GROUP_ID, TOPICS["ordubashy"], "Deploy finished")]
    history = call(world, world.ids["ordubashy"], "telegram_history")
    assert history.status == "ok" and [item["text"] for item in history.data["items"]] == ["Deploy finished"]
    receipt = call(world, world.ids["ordubashy"], "telegram_delivery_get", {"message_id": sent.data["message_id"]})
    assert receipt.data["items"][0]["state"] == "sent"


def test_no_other_agent_has_a_telegram_channel(world):
    # A retired Founder or Marketing cannot even build a context: its stored profile is never authenticated.
    for name in ("founder", "marketing"):
        with pytest.raises(DomainError) as retired:
            call(world, world.ids[name], "telegram_history")
        assert retired.value.code == "inactive_agent", name
    for name in ("learning-applier",):
        agent = world.ids[name]
        assert call(world, agent, "telegram_send", {"text": f"From {name}", "purpose": "update"}).status == "error", name
        assert call(world, agent, "telegram_history").status == "error", name
        assert call(world, agent, "telegram_delivery_get").status == "error", name
    while world.bridge.dispatcher.run():
        pass
    assert world.api.sent == [] and rows(world, "SELECT id FROM telegram_messages") == []
    with world.instance.database.read() as uow:
        for name in ("founder", "marketing", "learning-applier"):
            assert world.bridge.config.agent_chat(world.ids[name]) is None
        # A chat binds the exact role: Founder's identity cannot stand in the Ordubashy chat.
        with pytest.raises(DomainError) as binding:
            TelegramBridge.check_agent_chat(uow, SimpleNamespace(name="ordubashy", agent_id=world.ids["founder"]))
        assert binding.value.code == "binding_invalid"
        TelegramBridge.check_agent_chat(uow, SimpleNamespace(name="ordubashy", agent_id=world.ids["ordubashy"]))


def test_the_owner_reaches_ordubashy_and_the_former_agent_topics_route_nowhere(world):
    updates = [owner_message("ordubashy", "Please look at the report", 101, START),
               owner_message("founder", "Founder, are you there?", 102, START),
               owner_message("marketing", "Marketing, are you there?", 103, START)]
    world.api.updates.extend(updates)
    world.clock.now = START + timedelta(seconds=5)
    assert world.bridge.poll_once() == 0.0
    stored = {row["update_id"]: row for row in rows(world, "SELECT update_id, state, reason, normalized_json FROM telegram_updates")}
    assert (stored[101]["state"], stored[101]["reason"]) == ("done", None)
    for update_id in (102, 103):
        assert (stored[update_id]["state"], stored[update_id]["reason"]) == ("ignored", "unknown_topic")
        assert "are you there" not in (stored[update_id]["normalized_json"] or "")        # no text is kept for an unrouted topic
    inbound = rows(world, "SELECT chat_name, agent_id, body FROM telegram_messages WHERE direction='inbound'")
    assert [(m["chat_name"], m["agent_id"]) for m in inbound] == [("ordubashy", world.ids["ordubashy"])]
    assert "Please look at the report" in inbound[0]["body"]
    assert {r["recipient_id"] for r in rows(world, "SELECT recipient_id FROM agent_messages")} == {world.ids["ordubashy"]}
    assert [r["agent_id"] for r in rows(world, "SELECT agent_id FROM telegram_owner_inputs")] == [world.ids["ordubashy"]]
    assert world.api.sent == []                      # an ignored topic gets no reply either, so nothing falls back to Ordubashy


def test_urgent_notifications_still_post_to_their_own_topic(world):
    posted = world.instance.invoke(world.owner, "telegram_notify", {
        "event_key": "db-down", "title": "Database unreachable", "text": "Writes fail. Fail over.", "reason": "Data-loss risk"})
    assert posted.status == "ok", posted.to_dict()
    while world.bridge.dispatcher.run():
        pass
    assert [(m["chat_id"], m["message_thread_id"]) for m in world.api.sent] == [(GROUP_ID, TOPICS["urgent"])]


def test_a_notice_for_a_removed_chat_cannot_be_queued_anywhere_else(world):
    with pytest.raises(DomainError) as unavailable:
        with world.instance.database.transaction() as uow:
            enqueue_notice(world.bridge, uow, "founder", "Hello", "synthetic")
    assert unavailable.value.code == "operation_unavailable"
    assert rows(world, "SELECT id FROM telegram_outbox") == []


def test_the_converted_file_over_state_saved_under_the_earlier_shape_is_a_new_generation(world):
    """What the release does: the saved binding fingerprint covered six chats, the converted file covers three. The bridge starts a
    new generation, closes what was queued under the old one, and Ordubashy's own flow continues under the new one."""
    bridge = world.bridge
    queued = call(world, world.ids["ordubashy"], "telegram_send", {"text": "Queued before the conversion", "purpose": "update"})
    assert queued.status == "ok", queued.to_dict()
    with bridge.write("synthetic.earlier-shape") as uow:
        state = store.get_state(uow)
        assert state["config_generation"] == 1
        uow.repository("telegram_state").update("telegram", {"binding_fingerprint": "0" * 64}, expected_revision=state["revision"])
    world.clock.now = START + timedelta(seconds=30)
    bridge._ready.clear()
    assert bridge._ensure_ready() is True
    with bridge.read() as uow:
        state = store.get_state(uow)
    assert state["config_generation"] == 2 and state["binding_fingerprint"] == bridge.config.binding_fingerprint()
    while bridge.dispatcher.run():
        pass
    assert world.api.sent == []
    assert [(r["state"], r["error_code"]) for r in rows(world, "SELECT state, error_code FROM telegram_outbox")] == [("failed", "binding_changed")]
    again = call(world, world.ids["ordubashy"], "telegram_send", {"text": "After the conversion", "purpose": "update"})
    assert again.status == "ok", again.to_dict()
    world.clock.now = START + timedelta(seconds=40)
    while bridge.dispatcher.run():
        pass
    assert [(m["message_thread_id"], m["text"]) for m in world.api.sent] == [(TOPICS["ordubashy"], "After the conversion")]


def test_the_earlier_shape_on_disk_stops_the_bridge_instead_of_routing_anything(world):
    queued = call(world, world.ids["ordubashy"], "telegram_send", {"text": "Held back", "purpose": "update"})
    assert queued.status == "ok", queued.to_dict()
    world.api.updates.append(owner_message("founder", "Founder, are you there?", 101, START))
    older = copy.deepcopy(world.data)
    older["chats"].update({name: {"chat_id": GROUP_ID, "type": "supergroup", "message_thread_id": TOPICS[name],
                                  "agent_id": world.ids[name]} for name in FORMER_AGENT_CHATS})
    world.config_path.write_text(json.dumps(older))
    os.chmod(world.config_path, 0o600)
    world.clock.now = START + timedelta(seconds=30)
    world.bridge.cycle()
    assert "only Ordubashy has an agent chat" in world.bridge.health()["config"]
    assert world.bridge.poll_once() == 1.0                         # nothing is polled or routed while the file is refused
    assert world.api.sent == [] and rows(world, "SELECT update_id FROM telegram_updates") == []
    assert [r["state"] for r in rows(world, "SELECT state FROM telegram_outbox")] == ["queued"]    # kept, never sent elsewhere
    # Restoring the converted file recovers the same bridge: the held send goes to Ordubashy's own topic.
    world.config_path.write_text(json.dumps(world.data))
    os.chmod(world.config_path, 0o600)
    world.clock.now = START + timedelta(seconds=60)
    assert world.bridge._ensure_ready() and world.bridge.health() == {}
    while world.bridge.dispatcher.run():
        pass
    assert [(m["message_thread_id"], m["text"]) for m in world.api.sent] == [(TOPICS["ordubashy"], "Held back")]
