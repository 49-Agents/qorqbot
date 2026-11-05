"""A core-only host carries no mail: no table, trigger, view, history entry, operation, worker or import.

Every case is a real synthetic host over a disposable instance with no extension selected. Selecting the optional Fastmail
provider changes the operation set and nothing in the store, so a store prepared without it is served with it and back. A
generic Signoff stays whole, and the retained Email Task type reports a clean refusal instead of an import or SQL error. Nothing
leaves the process: no mail engine, mailbox, account, network or real address is used."""

import json
import re
import subprocess
import sys

import pytest
from qorqut_operations.contracts import InvocationOptions, Principal, new_id, utc_now

from bazarlyk.extensions import NO_EXTENSIONS, select
from bazarlyk.operations_history.catalog import catalog
from bazarlyk.service.runtime import build_runtime
from host_foundation_support import (build_instance, create_registered_global, register_sources, set_agent_overrides,
                                     synthetic_ordubashy)

MAIL = re.compile(r"email|fastmail|mailbox|masked_mail|vacation", re.IGNORECASE)


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("core"))
    register_sources(instance, instance.business, "first")
    synthetic_ordubashy(instance)
    agent = create_registered_global(instance, "ordubashy")
    set_agent_overrides(instance, agent, {"tasks_create": "signoff"}, [instance.business])
    instance.runtime.freeze_registry()
    return instance, agent


def objects(instance):
    with instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")]


def test_a_fresh_core_store_names_no_mail_object(core):
    instance, _ = core
    found = objects(instance)
    assert {"businesses", "tasks", "signoffs", "inbox_events", "jobs"} <= {row["name"] for row in found}
    mail = [(row["type"], row["name"]) for row in found if MAIL.search(f"{row['name']} {row['sql'] or ''}")]
    assert mail == []                                          # tables, indexes, triggers, views and every column or CHECK


def test_core_history_classifies_no_mail_table_or_owner(core):
    instance, _ = core
    exported = catalog(())
    names = {*exported.exports, *exported.classifications, *(item.owner for item in exported.contributions)}
    assert not [name for name in names if MAIL.search(name)]
    live = {row["name"] for row in objects(instance) if row["type"] == "table"}
    classified = {*exported.exports, *exported.classifications}
    assert not [name for name in live if name not in classified and MAIL.search(name)]


def test_no_mail_operation_or_extension_is_composed(core):
    instance, _ = core
    registry = instance.runtime.registry
    for name in ("fastmail_capabilities_get", "fastmail_send", "fastmail_receipt_get", "business_email_update",
                 "business_email_sync", "business_email_default_set", "email_send", "email_sync", "account_email_send"):
        assert not registry.known(name), name
    assert instance.runtime.extensions == {}
    assert not any(MAIL.search(step.name) for step in instance.runtime.lifetime().steps)
    assert tuple(instance.runtime.extension_writers()) == ()
    assert not hasattr(instance.runtime.domains, "email")


def test_the_core_package_imports_without_the_fastmail_distribution():
    """A fresh interpreter composes the core host with the Fastmail package made unimportable."""
    program = ("import sys\n"
               "sys.modules['qorqut_fastmail'] = None\n"
               "import bazarlyk.composition.assembly, bazarlyk.service.runtime, bazarlyk.calendar.operations\n"
               "loaded = sorted(name for name in sys.modules if name.split('.')[0] == 'qorqut_fastmail' and sys.modules[name] is not None)\n"
               "assert loaded == [], loaded\n")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_a_generic_signoff_still_proposes_and_applies_once(core):
    instance, agent = core
    runtime = instance.runtime
    context = runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=instance.business)
    proposed = runtime.application.invoke(context, "tasks_create", {"title": "Needs approval"}, InvocationOptions(idempotency_key=new_id()))
    assert proposed.status == "signoff_required", proposed.to_dict()
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM signoffs WHERE id=?", (proposed.signoff["id"],)).fetchone()[0] == 1


def test_an_email_task_is_refused_cleanly_without_a_mail_domain(core):
    instance, _ = core
    result = instance.invoke(instance.human(), "tasks_create", {
        "title": "Reply to a customer", "task_type": "email", "email_conversation_id": new_id()})
    assert result.status == "error", result.to_dict()
    assert result.error["code"] in {"not_found", "validation_error"}, result.error
    assert "no such table" not in str(result.error).lower() and "traceback" not in str(result.error).lower()


def test_a_task_that_needs_no_email_is_unaffected(core):
    instance, _ = core
    result = instance.invoke(instance.human(), "tasks_create", {"title": "Plain task"})
    assert result.status == "ok", result.to_dict()


def test_progress_reports_no_email_series(core):
    """The retired ``emails_sent`` series is gone from both progress reads; every other count stays."""
    from host_foundation_support import owner_global_context

    instance, _ = core
    business = instance.invoke(instance.human(), "business_progress_get", {"days": 30})
    assert business.status == "ok", business.to_dict()
    data = business.data
    assert set(data["totals"]) == set(data["undated"]) == {"tasks_done", "actions", "signoffs_approved"}
    assert all(set(row) == {"date", "tasks_done", "actions", "signoffs_approved"} for row in data["series"])
    assert "emails_sent" not in data["coverage"]
    owner = instance.invoke(owner_global_context(instance), "global_progress_get", {"days": 30})
    assert owner.status == "ok", owner.to_dict()
    assert all(set(row) == {"date", "tasks_done", "inbox_messages", "learnings_accepted", "new_tickets", "actions"}
               for row in owner.data["series"])


# -- historical email data stays read-only, never fetched ------------------------------------------------------------------

def historical_email_task(instance, title="Historical email Task"):
    """A Task row as an earlier version stored it: Email type bound to a conversation this host cannot resolve."""
    conversation, identity = new_id(), new_id()
    stored = {"$task": {"task_type": "email", "overview": "", "email_conversation_id": conversation}}
    with instance.database.transaction() as uow:
        uow.repository("tasks", business_id=instance.business).insert({
            "id": identity, "title": title, "status": "open", "created_by": instance.owner, "fields_json": stored})
    return identity, conversation


def edit(instance, identity, changes):
    with instance.database.read() as uow:
        revision = uow.connection.execute("SELECT revision FROM tasks WHERE id=?", (identity,)).fetchone()[0]
    return instance.runtime.application.invoke(instance.human(), "tasks_update", {"task_id": identity, "changes": changes},
                                               InvocationOptions(idempotency_key=new_id(), expected_revision=revision))


def binding(instance, identity):
    with instance.database.read() as uow:
        row = uow.connection.execute("SELECT fields_json FROM tasks WHERE id=?", (identity,)).fetchone()
    return json.loads(row[0]).get("$task", {})


def test_an_ordinary_edit_of_a_historical_email_task_needs_no_mail_lookup(core):
    instance, _ = core
    identity, conversation = historical_email_task(instance)
    result = edit(instance, identity, {"title": "Renamed", "overview": "Still the same thread"})
    assert result.status == "ok", result.to_dict()
    assert binding(instance, identity)["task_type"] == "email" and binding(instance, identity)["email_conversation_id"] == conversation
    read = instance.invoke(instance.human(), "tasks_get", {"task_id": identity})
    assert read.status == "ok" and read.data["task"]["email_conversation_id"] == conversation


def test_changing_a_historical_email_task_to_another_type_clears_its_binding_without_naming_it(core):
    instance, _ = core
    identity, _ = historical_email_task(instance)
    result = edit(instance, identity, {"task_type": "story"})                 # the field is omitted, as every ordinary write does
    assert result.status == "ok", result.to_dict()
    assert binding(instance, identity)["task_type"] == "story" and binding(instance, identity)["email_conversation_id"] is None
    assert instance.invoke(instance.human(), "tasks_get", {"task_id": identity}).data["task"]["email_conversation_id"] is None


def test_naming_a_binding_for_another_type_is_still_refused_and_a_new_binding_is_never_looked_up_successfully(core):
    instance, _ = core
    identity, _ = historical_email_task(instance)
    contradiction = edit(instance, identity, {"task_type": "story", "email_conversation_id": new_id()})
    assert contradiction.status == "error" and contradiction.error["code"] == "validation_error"
    rebinding = edit(instance, identity, {"email_conversation_id": new_id()})
    assert rebinding.status == "error" and rebinding.error["code"] in {"not_found", "validation_error"}
    unbound = instance.invoke(instance.human(), "tasks_create", {"title": "No conversation", "task_type": "email"})
    assert unbound.status == "error" and unbound.error["code"] == "validation_error"


def test_a_plain_task_write_omits_the_binding_entirely(core):
    instance, _ = core
    created = instance.invoke(instance.human(), "tasks_create", {"title": "Ordinary", "task_type": "story"})
    assert created.status == "ok", created.to_dict()
    identity = created.data["task"]["id"]
    assert instance.invoke(instance.human(), "tasks_get", {"task_id": identity}).data["task"]["email_conversation_id"] is None
    assert edit(instance, identity, {"overview": "Where it stands"}).status == "ok"
    assert binding(instance, identity)["email_conversation_id"] is None


def test_historical_email_links_and_note_references_read_without_any_fetch(core):
    """Relationships and note references that point at a conversation or message this host cannot resolve are not errors."""
    instance, _ = core
    created = instance.invoke(instance.human(), "tasks_create", {"title": "Has history", "task_type": "story"})
    identity = created.data["task"]["id"]
    now = utc_now()
    with instance.database.transaction() as uow:
        for _ in range(2):                                                    # a conversation link and a message link, both "material"
            uow.connection.execute(
                "INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (instance.business, new_id(), "task", identity, "material", new_id(), "concerns", "historical", now, now))
    commented = instance.runtime.application.invoke(
        instance.human(), "tasks_comment", {"task_id": identity, "text": "Plain note"},
        InvocationOptions(idempotency_key=new_id(), expected_revision=1))
    assert commented.status == "ok", commented.to_dict()
    with instance.database.transaction() as uow:
        row = dict(uow.connection.execute("SELECT * FROM thread_messages WHERE item_id=? AND kind='comment'", (identity,)).fetchone())
        row.update(id=new_id(), body="Historical note", refs_json=json.dumps([{"type": "material", "id": new_id()}]))
        uow.connection.execute(f"INSERT INTO thread_messages({','.join(row)}) VALUES({','.join('?' for _ in row)})", list(row.values()))
    for name, arguments in (("relationships_list", {}), ("relationships_list", {"source": {"type": "task", "id": identity}}),
                            ("tasks_get", {"task_id": identity}), ("tasks_notes_list", {"task_id": identity}),
                            ("tasks_thread_get", {"task_id": identity})):
        result = instance.invoke(instance.human(), name, arguments)
        assert result.status == "ok", (name, result.to_dict())
    notes = instance.invoke(instance.human(), "tasks_notes_list", {"task_id": identity}).data["notes"]
    assert sorted(note["body"] for note in notes) == ["Historical note", "Plain note"]
