"""v1.3: the daily report, the separate milestones and the separate personal context are gone from the composed host, and what
stays next to them (the journal, owner reminders, Learning and the general Telegram outbox) still works.

The composed host, its real Telegram bridge and the real dispatcher are used; only the Bot API is a recording fake. Every path
lives under ``tmp_path``; no provider, model, network or operational data is involved."""

from datetime import datetime, timedelta, timezone

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.background.integration import attach_background
from bazarlyk.telegram.config import CHAT_NAMES, parse_config
from host_foundation_support import build_instance
from telegram_media_operation_support import CHAT_ID, URGENT, build_host, invoke, outbox, register_document, sql

RETIRED_PREFIXES = ("milestones_", "personal_context_", "daily_report_")
RETIRED_TABLES = ("daily_report_runs", "daily_report_owner_attention", "milestones", "milestone_events")


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    host = build_host(tmp_path_factory.mktemp("retired-personal-features"))
    with host.database.transaction() as uow:
        installation = uow.repository("installation").get("service")
        uow.repository("installation").update("service", {"management_coordinator_id": host.ordubashy},
                                              expected_revision=installation["revision"])
    return host


# ----- what is gone --------------------------------------------------------------------------------------------------


def test_no_report_milestone_or_personal_context_operation_is_declared_or_installed(host):
    registry = host.runtime.registry
    declared = {entry.name for entry in registry.entries()}
    installed = {descriptor.name for descriptor in registry.installed()}
    assert declared and installed
    assert not [name for name in declared | installed if name.startswith(RETIRED_PREFIXES)]
    for name in ("milestones_list", "milestones_create", "personal_context_get", "personal_context_create",
                 "daily_report_generate", "daily_report_get", "daily_report_owner_attention_get"):
        assert not registry.known(name)


def test_no_daily_report_program_or_operation_is_attached_with_the_background_service(tmp_path):
    """The background service of a composed host registers every program; the fixed ``reports.daily`` one is no longer among them."""
    instance, _ = build_instance(tmp_path, businesses=("first",))
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    identities = {program.id for program in runtime.programs}
    assert not [name for name in identities if name.startswith("reports.")]
    assert {"learnings.extract", "learnings.extract_global"} <= identities
    assert not [entry.name for entry in runtime.registry.entries() if entry.name.startswith(RETIRED_PREFIXES)]
    assert not [entry.name for entry in runtime.registry.installed() if entry.name.startswith(RETIRED_PREFIXES)]


def test_the_retired_tables_do_not_exist_in_a_new_store(host):
    present = {row["name"] for row in sql(host, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not present & set(RETIRED_TABLES)
    assert {"documents", "telegram_outbox", "learning_candidates"} <= present


def test_the_retired_operations_cannot_be_invoked_by_the_registered_ordubashy(host):
    for name in ("milestones_list", "personal_context_list", "daily_report_list"):
        with pytest.raises(DomainError):
            host.runtime.registry.get(name)
        assert invoke(host, {}, name=name).status == "error"


def test_the_daily_reports_telegram_chat_is_no_longer_configurable():
    assert CHAT_NAMES == ("ordubashy", "signoffs", "urgent")
    chats = {name: {"chat_id": CHAT_ID, "type": "supergroup", "message_thread_id": 10 + index}
             for index, name in enumerate((*CHAT_NAMES, "daily_reports"))}
    chats["ordubashy"]["agent_id"] = "11111111-1111-4111-8111-111111111111"
    data = {"version": 1, "enabled": False, "chats": chats}
    with pytest.raises(DomainError) as error:
        parse_config(data)
    assert error.value.code == "invalid_configuration"


# ----- what stays ----------------------------------------------------------------------------------------------------


def test_the_journal_still_lists_and_returns_a_visible_entry_to_the_registered_ordubashy(host):
    entry = register_document(host, kind="global_note", path="notes/visible.md", data=b"The complete journal entry.",
                              metadata={"title": "Daily", "date": "2026-09-22"})
    hidden = register_document(host, kind="global_note", path="notes/hidden.md", data=b"Not for Ordubashy.",
                               metadata={"title": "Private", "date": "2026-09-23", "ordubashy_visible": False})
    listed = invoke(host, {}, name="journal_entries_list")
    assert listed.status == "ok", listed.error
    listed_ids = [item["document_id"] for item in listed.data["items"]]
    assert entry.id in listed_ids and hidden.id not in listed_ids
    read = invoke(host, {"document_id": entry.id}, name="journal_entry_get")
    assert read.status == "ok", read.error
    assert read.data["entry"]["body"] == "The complete journal entry." and read.data["entry"]["title"] == "Daily"
    # An entry the owner hid from Ordubashy stays unavailable, and the refusal does not reveal that it exists.
    refused = invoke(host, {"document_id": hidden.id}, name="journal_entry_get")
    assert refused.status == "error" and refused.error["code"] == "not_found"


def test_the_journal_is_not_readable_by_another_global_agent(host):
    entry = register_document(host, kind="global_note", path="notes/other.md", data=b"Body", metadata={"title": "Daily"})
    assert invoke(host, {}, name="journal_entries_list", agent=host.other).status == "error"
    assert invoke(host, {"document_id": entry.id}, name="journal_entry_get", agent=host.other).status == "error"


def test_an_owner_reminder_is_scheduled_and_delivered_to_the_urgent_topic_as_plain_text(host):
    start = datetime.now(timezone.utc)
    host.bridge.clock = lambda: start
    at = (start + timedelta(minutes=5)).isoformat()
    saved = invoke(host, {"action": "schedule", "text": "Pay the accountant.", "at": at}, name="owner_reminder")
    assert saved.status == "ok", saved.error
    assert saved.data["state"] == "scheduled" and saved.data["text"] == "Pay the accountant."
    assert [row["source_kind"] for row in outbox(host)] == ["owner_reminder"]
    assert host.bridge.dispatcher.dispatch_one() is False and not host.api.texts          # not due yet
    host.bridge.clock = lambda: start + timedelta(minutes=6)
    assert host.bridge.dispatcher.dispatch_one() is True
    assert [(sent.chat_id, sent.thread, sent.text) for sent in host.api.texts] == [(CHAT_ID, URGENT, "Pay the accountant.")]
    assert [row["state"] for row in outbox(host)] == ["sent"]
    listed = invoke(host, {"action": "list"}, name="owner_reminder")
    assert listed.status == "ok" and [item["state"] for item in listed.data["reminders"]] == ["sent"]


def test_learning_reads_and_the_general_telegram_operations_are_still_composed(host):
    listed = invoke(host, {}, name="learnings_list")
    assert listed.status == "ok", listed.error
    assert list(listed.data["items"]) == []
    for name in ("telegram_send_file", "learnings_review_list", "learnings_extract", "owner_reminder", "journal_entry_get"):
        assert host.runtime.registry.known(name), name
    assert "daily_reports" not in host.bridge.status()
