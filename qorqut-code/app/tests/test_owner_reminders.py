from datetime import timedelta

import pytest

from qorqut_operations.contracts import new_id
from telegram_service_support import TelegramService
from telegram_support import CHAT_IDS


@pytest.fixture
def service(tmp_path):
    s = TelegramService(tmp_path)
    try:
        yield s
    finally:
        s.close()


def schedule(s, who=None, **extra):
    return s.call(who or s.ordubashy, "owner_reminder", {
        "action": "schedule", "text": "Pay the accountant.",
        "at": (s.clock() + timedelta(minutes=5)).isoformat(), **extra})


def test_delayed_delivery_is_durable_deduplicated_and_plain(service):
    s = service
    args = {"action": "schedule", "text": "Pay the accountant.", "at": (s.clock()+timedelta(minutes=5)).isoformat()}
    key = new_id()
    saved = s.ok(s.call(s.ordubashy, "owner_reminder", args, key))
    assert saved == s.ok(s.call(s.ordubashy, "owner_reminder", args, key))
    assert set(saved) == {"reminder_id", "revision", "text", "at", "state"}
    s.dispatch()
    assert s.api.sent == []
    s.clock.advance(301)
    s.dispatch(); s.dispatch()
    assert len(s.api.sent) == 1
    assert s.api.sent[0]["chat_id"] == CHAT_IDS["urgent"]
    assert s.api.sent[0]["text"] == "Pay the accountant."


def test_list_update_cancel_and_caller_isolation(service):
    s = service
    first = s.ok(schedule(s))
    assert not s.ok(s.call(s.founder,"owner_reminder",{"action":"list"}))["reminders"]
    denied = s.call(s.founder,"owner_reminder",{"action":"cancel","reminder_id":first["reminder_id"],"revision":first["revision"]})
    assert denied.status == "error"
    replacement = s.ok(s.call(s.ordubashy,"owner_reminder",{"action":"update","reminder_id":first["reminder_id"],"revision":first["revision"],"text":"Call accountant.","at":(s.clock()+timedelta(minutes=10)).isoformat()}))
    assert replacement["reminder_id"] != first["reminder_id"]
    cancelled = s.ok(s.call(s.ordubashy,"owner_reminder",{"action":"cancel","reminder_id":replacement["reminder_id"],"revision":replacement["revision"]}))
    assert cancelled["state"] == "cancelled"
    s.clock.advance(601);s.dispatch()
    assert not s.api.sent


@pytest.mark.parametrize("extra", [{"text":" "},{"text":"x"*241},{"at":"2020-01-01T10:00:00Z"},{"at":"2039-01-01T10:00:00"},{"chat_id":123}])
def test_invalid_reminders_schedule_nothing(service, extra):
    assert schedule(service, **extra).status == "error"
    assert not service.rows("select id from telegram_outbox where source_kind='owner_reminder'")


def test_business_agent_can_schedule_and_disabled_delivery_does_not_promise(service):
    s=service
    assert s.ok(schedule(s,s.onbashy))["state"] == "scheduled"
    s.write_config(enabled=False);s.bridge.load_config()
    assert schedule(s,s.founder).status == "error"


def test_permission_is_rechecked_at_delivery(service):
    s=service
    saved=s.ok(schedule(s,s.founder))
    s.fixture.update(s.founder,{"tool_policy":{"overrides":{"owner_reminder":"deny"}}})
    s.clock.advance(301);s.dispatch()
    assert not s.api.sent
    row=s.rows('select state,error_code from telegram_outbox where id=?',saved['reminder_id'])[0]
    assert row == {"state":"cancelled","error_code":"permission_revoked"}
