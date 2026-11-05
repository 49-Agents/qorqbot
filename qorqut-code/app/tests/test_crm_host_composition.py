"""CRM composition, storage, native Table Focus and private conversations.

Runs against the real CRM Domain and Operations engine over a disposable SQLite store. Covers catalog/exposure, storage
ownership, capture, history/export classification, Focus, private conversations and Project can_thread. Full membership
writes and approval application are exercised in test_crm_integration_flows."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qorqut_operations import crm
from qorqut_operations.contracts import InvocationOptions, Principal, canonical_hash, new_id, plain

from bazarlyk.background.integration import attach_background
from bazarlyk.composition import capture
from bazarlyk.composition.catalog import BUILTIN_CATALOG, ENGINE_ENTRIES
from bazarlyk.inbox.private_conversations import private_index, private_state, source_key
from bazarlyk.service.changes import ITEM_KIND_BY_TYPE, KIND_BY_TABLE
from bazarlyk.storage import exports
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context, register_sources,
                                     select_coordinator, set_agent_overrides)

TABLE_GRANTS = {"focus_present": "allow", "focus_get": "allow", "focus_reply": "allow", "focus_set_archived": "allow",
                "tables_comment": "allow", "tables_rows_write": "allow", "owner_conversation_get": "allow",
                "owner_conversation_send": "allow", "tables_thread_get": "allow", "tables_get": "allow"}
COLUMNS = [{"id": "name", "name": "Name", "type": "text"}]


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("crm-host"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    worker = create_worker(instance, "worker-a")
    for identity in (coordinator, worker):
        set_agent_overrides(instance, identity, TABLE_GRANTS, (first, second))
    attach_background(instance.runtime, instance.runtime.programs)    # Table history is a background program
    with instance.database.transaction() as uow:
        instance.runtime.authority.register_workers(uow, [first, second])
    instance.runtime.freeze_registry()
    state = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first, second=second,
                            coordinator=coordinator, worker=worker)
    try:
        yield state
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def call(host, ctx, name, arguments, key=None):
    return host.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key or new_id()))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def owner(host, business):
    return host.instance.human(business) if business else owner_global_context(host.instance)


def agent(host, identity, business):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def scoped(business, **arguments):
    return {"scope": "business" if business else "global", **arguments}


def make_table(host, business, title="Synthetic table"):
    return ok(call(host, owner(host, business), "tables_create", scoped(business, title=title, columns=COLUMNS)))["table"]


def make_crm(host, business, title="Synthetic CRM"):
    return ok(call(host, owner(host, business), "tables_create",
                   scoped(business, title=title, table_kind="crm")))["table"]


def make_task(host, business, title="Synthetic record"):
    return ok(call(host, owner(host, business), "tasks_create", scoped(business, title=title)))["task"]


def query(host, sql, *parameters):
    with host.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(sql, parameters)]


def run(host, sql, *parameters):
    with host.database.transaction() as uow:
        uow.connection.execute(sql, parameters)


def table_source(table, business):
    return {"type": "table", "id": table["id"], "business_id": business}


# ----- composition and catalog ------------------------------------------------------------------------------------------


def test_the_crm_domain_is_composed_into_the_runtime_with_its_table_service_bound(host):
    domains = host.runtime.domains
    assert isinstance(domains.crm, crm.CRMOperations)
    assert domains.tables.crm is domains.crm                      # bound before the registry froze: the Domain reads Tables through it
    assert host.runtime.crm_activity.crm is domains.crm and host.runtime.crm_activity.application is host.runtime.application


def test_every_crm_operation_is_declared_once_registered_and_discoverable_with_its_effect(host):
    declared = [entry.name for entry in (*BUILTIN_CATALOG, *ENGINE_ENTRIES)]
    registered = [entry.name for entry in host.runtime.registry.entries()]
    expected = [entry.name for entry in crm.CATALOG_ENTRIES] + ["crm_activity_list"]
    for name in expected:
        assert declared.count(name) == 1 and registered.count(name) == 1, name
        assert host.runtime.registry.get(name).name == name        # a handler is installed, not only metadata
    effects = {entry.name: (entry.effect_kind, entry.supports_signoff) for entry in host.runtime.registry.entries()}
    assert effects["crm_activity_list"] == ("read", False)
    assert all(effects[name] == ("read", False) for name in crm.READ_NAMES)
    assert all(effects[name][0] == "records" for name in crm.MUTATION_NAMES)
    descriptor = host.runtime.registry.get("crm_activity_list")
    assert descriptor.scope_kinds == frozenset({"business", "global"}) and descriptor.global_capability == "read_global_context"


def test_the_crm_tables_are_owned_by_the_domain_contribution_and_declared_for_capture_history_and_feed(host):
    assert set(crm.TABLE_NAMES) == {"crm_entries", "crm_activity_events"}
    assert set(crm.TABLE_NAMES) <= set(host.database.assembly.tables)
    assert set(crm.TABLE_NAMES) <= {owner for owner, _ in host.database.assembly.schema} | set(host.database.assembly.tables)
    # Capture: the Domain declares the memberships' own feed triggers in its DDL beside the guards reading the same rows, so the
    # host attaches only the immutable recorded observations (a second declaration would build the trigger twice).
    assert "crm_entries" not in capture.CAPTURED and capture.CAPTURED["crm_activity_events"][1] == ("insert",)
    identity = capture.CAPTURED["crm_activity_events"][0]
    assert set(identity) <= {"business_id", "id"} and identity
    assert KIND_BY_TABLE["crm_entries"] == "tables" and KIND_BY_TABLE["crm_activity_events"] == "tasks"
    assert ITEM_KIND_BY_TYPE["table"] == "tables"
    # History: each new table is explicitly classified, none by wildcard.
    from bazarlyk.operations_history.catalog import catalog as history_catalog
    classified = history_catalog().classifications
    assert {name: (classified[name][0], classified[name][1].kind) for name in crm.TABLE_NAMES} == {
        "crm_entries": ("host.separate_history", "separate_history"),
        "crm_activity_events": ("host.separate_history", "private_excluded")}


def test_broad_exports_carry_membership_rows_but_never_the_recorded_external_bodies():
    assert "crm_entries" in exports.BUSINESS_TABLES and "crm_entries" in exports.GLOBAL_TABLES
    assert "crm_activity_events" not in (*exports.BUSINESS_TABLES, *exports.GLOBAL_TABLES)
    for table in ("owner_conversations", "owner_conversation_messages"):
        assert table not in (*exports.BUSINESS_TABLES, *exports.GLOBAL_TABLES)


def test_a_crm_row_write_publishes_only_a_kind_and_its_capture_triggers_carry_identity_columns_alone(host):
    for name in ("inbox_capture_crm_entries_insert", "inbox_capture_crm_entries_update", "inbox_capture_crm_activity_events_insert"):
        definition = query(host, "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", name)[0][0]
        assert "json_object('business_id',NEW.business_id,'id',NEW.id" in definition.replace(" ", ""), name
        assert not any(column in definition for column in ("body", "summary", "source_json")), name
    # The recorded observations' own trigger is the host's: identity alone (the body stays in its table).
    feed_only = query(host, "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='inbox_capture_crm_activity_events_insert'")[0][0]
    assert not any(column in feed_only for column in ("cells_json", "stage_id", "kind", "occurred_at"))
    table = make_crm(host, host.first, "Capture table")
    task = make_task(host, host.first)
    feed = host.instance.feed
    tables_before, tasks_before = feed.version("tables", host.first), feed.version("tasks", host.first)
    now = "2026-10-28T12:00:00Z"
    run(host, "INSERT INTO crm_entries(business_id,id,table_id,record_task_id,record_business_id,record_namespace,position,cells_json,"
              "created_by,updated_by,created_at,updated_at) VALUES (?,?,?,?,?,?,0,?,?,?,?,?)",
        host.first, "entry-one", table["id"], task["id"], host.first, f"business:{host.first}",
        json.dumps({"name": "Secret local cell"}), host.instance.owner, host.instance.owner, now, now)
    assert feed.version("tables", host.first) > tables_before and feed.version("tasks", host.first) == tasks_before
    source, body = {"kind": "email", "title": "Synthetic mailbox"}, "Synthetic external body"
    run(host, "INSERT INTO crm_activity_events(business_id,id,task_id,kind,occurred_at,recorded_at,recorded_by,summary,body,source_json,"
              "event_key,content_hash) VALUES (?,?,?,'email',?,?,?,'Synthetic summary',?,?,?,?)",
        host.first, "event-one", task["id"], now, now, host.instance.owner, body, json.dumps(source), "key-1",
        canonical_hash({"kind": "email", "summary": "Synthetic summary", "body": body, "occurred_at": now, "source": source,
                        "corrects_event_id": None}))
    assert feed.version("tasks", host.first) > tasks_before


# ----- native Table Focus -----------------------------------------------------------------------------------------------


def present(host, source, **fields):
    ctx = agent(host, host.coordinator, None)
    return ok(call(host, ctx, "focus_present", {"scope": "global", "source": source, **fields}))["item"]


def card(host, item_id, **arguments):
    ctx = agent(host, host.coordinator, None)
    return ok(call(host, ctx, "focus_get", {"scope": "global", "item_id": item_id, "include_archived": True, **arguments}))


def test_a_table_is_presented_as_a_native_focus_source_with_its_kind_and_a_general_and_a_crm_table_share_one_identity(host):
    for business in (host.first, None):
        table = make_table(host, business, "Pipeline")
        item = present(host, table_source(table, business))
        assert item["source"] == table_source(table, business) and item["label"] == "Pipeline"
        assert item["resolved_source"]["table_kind"] == "general" and "parent_id" not in item
        # Renaming the Table keeps the card: one source identity, the current label, the same surfaced kind.
        ok(host.runtime.application.invoke(
            owner(host, business), "tables_update", scoped(business, table_id=table["id"], changes={"title": "Pipeline renamed"}),
            InvocationOptions(idempotency_key=new_id(), expected_revision=table["revision"])))
        renamed = present(host, table_source(table, business))
        assert renamed["id"] == item["id"] and renamed["label"] == "Pipeline renamed"
        assert renamed["resolved_source"]["table_kind"] == "general"
        # A CRM is the same native Focus source kind with its own kind surfaced (the kind is fixed at creation). Pinning it
        # writes no parent edge and one card is still one card.
        crm_table = make_crm(host, business, "Pipeline CRM")
        crm_item = present(host, table_source(crm_table, business))
        assert crm_item["source"] == table_source(crm_table, business) and crm_item["label"] == "Pipeline CRM"
        assert crm_item["resolved_source"]["table_kind"] == "crm" and "parent_id" not in crm_item
        again = present(host, table_source(crm_table, business))
        assert again["id"] == crm_item["id"] and again["resolved_source"]["table_kind"] == "crm"
        assert query(host, "SELECT COUNT(*) FROM tasks WHERE parent_id IS NOT NULL")[0][0] == 0
        assert query(host, "SELECT COUNT(*) FROM focus_cards WHERE source_json LIKE ?", f'%"{crm_table["id"]}"%')[0][0] == 1


def test_focus_counts_only_a_tables_team_comments_and_routes_its_thread_through_the_table_discussion(host):
    table = make_table(host, host.first, "Discussed")
    item = present(host, table_source(table, host.first))
    shown = card(host, item["id"])["item"]
    assert shown["note_count"] == 0
    for index, (kind, body) in enumerate((("comment", "Team note one"), ("comment", "Team note two"), ("system", "Entry added"))):
        run(host, "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            host.first, f"table-note-{table['id']}-{index}", "table", table["id"], kind, body, None if kind == "system" else host.instance.owner,
            host.instance.owner, f"2026-10-28T12:0{index}:00Z")
    shown = card(host, item["id"], conversation=True)["item"]
    assert shown["note_count"] == 2
    thread = shown["thread"]
    assert [note["body"] for note in thread["messages"]] == ["Team note one", "Team note two", "Entry added"]
    assert thread["thread_id"] and thread["next_cursor"] is None


def test_a_denied_table_discussion_read_leaves_the_card_readable_but_without_a_discussion_count(host):
    table = make_table(host, host.first, "Count is a discussion read")
    item = present(host, table_source(table, host.first))
    for index in range(2):
        run(host, "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            host.first, f"count-note-{table['id']}-{index}", "table", table["id"], "comment", f"Counted note {index}", host.instance.owner,
            host.instance.owner, f"2026-10-28T12:0{index}:00Z")
    allowed = card(host, item["id"])["item"]
    assert allowed["note_count"] == 2
    try:
        # The Table read stays allowed; only its discussion read (tables_thread_get) is denied.
        set_agent_overrides(host.instance, host.coordinator, {**TABLE_GRANTS, "tables_thread_get": "deny"}, (host.first, host.second))
        shown = card(host, item["id"])["item"]
        assert "note_count" not in shown and not shown.get("unavailable")
        assert shown["label"] == "Count is a discussion read" and shown["resolved_source"]["table_kind"] == "general"
        # The independent private channel keeps its own presentation, unchanged by the discussion denial.
        assert {key: value for key, value in shown.items() if key.startswith(("private", "owner_conversation"))} == \
            {key: value for key, value in allowed.items() if key.startswith(("private", "owner_conversation"))}
        assert "Counted note" not in json.dumps(shown)
        # The count is also a per-call decision: other scopes' denial does not matter, restoring the grant restores it.
    finally:
        set_agent_overrides(host.instance, host.coordinator, TABLE_GRANTS, (host.first, host.second))
    assert card(host, item["id"])["item"]["note_count"] == 2
    # A Global Table is judged in its own scope: its discussion read is a separate decision and is denied the same way.
    global_table = make_table(host, None, "Global count")
    global_item = present(host, table_source(global_table, None))
    run(host, "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) VALUES (NULL,?,?,?,?,?,?,?,?)",
        f"gcount-{global_table['id']}", "table", global_table["id"], "comment", "Global counted note", host.instance.owner, host.instance.owner,
        "2026-10-28T12:30:00Z")
    assert card(host, global_item["id"])["item"]["note_count"] == 1
    try:
        set_agent_overrides(host.instance, host.coordinator, {**TABLE_GRANTS, "tables_thread_get": "deny"}, (host.first, host.second))
        assert "note_count" not in card(host, global_item["id"])["item"]
    finally:
        set_agent_overrides(host.instance, host.coordinator, TABLE_GRANTS, (host.first, host.second))
    assert card(host, global_item["id"])["item"]["note_count"] == 1


def test_a_card_archive_is_presentation_only_and_presenting_the_table_again_restores_the_same_card(host):
    table = make_table(host, host.first, "Archive me")
    item = present(host, table_source(table, host.first))
    revision = query(host, "SELECT revision, archived FROM data_tables WHERE id=?", table["id"])[0]
    ctx = agent(host, host.coordinator, None)
    archived = ok(call(host, ctx, "focus_set_archived", {"scope": "global", "item_id": item["id"], "expected_card_revision": item["revision"],
                                                        "archived": True}))["item"]
    assert archived["archived"]
    assert query(host, "SELECT revision, archived FROM data_tables WHERE id=?", table["id"])[0] == revision      # the Table is untouched
    restored = present(host, table_source(table, host.first))
    assert restored["id"] == item["id"] and not restored["archived"]
    assert query(host, "SELECT revision, archived FROM data_tables WHERE id=?", table["id"])[0] == revision


def test_a_focus_reply_to_a_table_is_prepared_as_a_table_comment_and_never_as_a_task_comment(host):
    table = make_table(host, host.first, "Reply target")
    task = make_task(host, host.first, "Not the reply target")
    item = present(host, table_source(table, host.first))
    present(host, {"type": "task", "id": task["id"], "business_id": host.first})
    ctx = agent(host, host.coordinator, None)
    task_notes = query(host, "SELECT COUNT(*) FROM thread_messages WHERE item_type='task'")[0][0]
    # The route reaches the real Table comment handler: one comment on the Table's own thread, none on any Task's.
    answer = ok(call(host, ctx, "focus_reply", {"scope": "global", "item_id": item["id"], "text": "Reply from Ordubashy"}))
    assert answer["message"]["body"] == "Reply from Ordubashy"
    stored = query(host, "SELECT item_type,item_id,kind,body FROM thread_messages WHERE item_type='table' AND item_id=?", table["id"])
    assert stored == [("table", table["id"], "comment", "Reply from Ordubashy")]
    assert query(host, "SELECT COUNT(*) FROM thread_messages WHERE item_type='task'")[0][0] == task_notes
    # Without the Table comment grant the same reply is refused by policy before any preparation.
    set_agent_overrides(host.instance, host.coordinator, {**TABLE_GRANTS, "tables_comment": "deny"}, (host.first, host.second))
    assert code(call(host, ctx, "focus_reply", {"scope": "global", "item_id": item["id"], "text": "Reply from Ordubashy"})) == "policy_denied"
    set_agent_overrides(host.instance, host.coordinator, TABLE_GRANTS, (host.first, host.second))
    # The installation owner keeps the existing channel rule: shared Table discussion is not the owner's channel.
    assert code(call(host, owner(host, None), "focus_reply", {"scope": "global", "item_id": item["id"], "text": "Owner text"})) == "channel_required"


def test_ordinary_task_signoff_and_reference_focus_cards_are_unaffected(host):
    task = make_task(host, host.first)
    item = present(host, {"type": "task", "id": task["id"], "business_id": host.first})
    assert "table_kind" not in item["resolved_source"] and item["source"]["type"] == "task"
    shown = card(host, item["id"], conversation=True)["item"]
    assert shown["note_count"] == 0 and "thread" in shown
    ctx = agent(host, host.coordinator, None)
    reference = ok(call(host, ctx, "focus_present", {"scope": "global", "reference": {
        "key": "synthetic-reference", "title": "Synthetic reference", "target": {"type": "url", "url": "https://example.com/synthetic"}}}))["item"]
    assert reference["source"] is None and "table_kind" not in reference and not reference.get("resolved_source")


def test_an_unreadable_table_cannot_be_presented(host):
    ctx = agent(host, host.coordinator, None)
    absent = call(host, ctx, "focus_present", {"scope": "global", "source": {"type": "table", "id": "no-such-table", "business_id": host.first}})
    assert code(absent) in {"not_found", "forbidden"}
    foreign = make_table(host, host.second, "Other business")
    # A different business is a different namespace: the same id with the wrong business never resolves.
    assert code(call(host, ctx, "focus_present", {"scope": "global", "source": {"type": "table", "id": foreign["id"], "business_id": host.first}})) in {"not_found", "forbidden"}


# ----- owner-private Table conversations -------------------------------------------------------------------------------


def deliveries(host, identity):
    return query(host, "SELECT count(*) FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                       "WHERE d.agent_id=? AND e.event_type='private_message'", identity)[0][0]


def send(host, ctx, source, text, key=None):
    return call(host, ctx, "owner_conversation_send", {**({"scope": "global"} if source["business_id"] is None else {}), "source": source, "text": text}, key)


def get(host, ctx, source, **arguments):
    return call(host, ctx, "owner_conversation_get", {**({"scope": "global"} if source["business_id"] is None else {}), "source": source, **arguments})


@pytest.mark.parametrize("business", ["first", None])
def test_the_owner_and_ordubashy_converse_privately_on_a_table_and_nobody_else_is_a_party(host, business):
    business = host.first if business else None
    source = table_source(make_table(host, business, "Private table"), business)
    before = {identity: deliveries(host, identity) for identity in (host.coordinator, host.worker)}
    ok(send(host, owner(host, business), source, "Owner to Ordubashy"))
    key = new_id()
    sent = ok(send(host, agent(host, host.coordinator, business), source, "Ordubashy answers", key))
    assert {identity: deliveries(host, identity) - before[identity] for identity in before} == {host.coordinator: 1, host.worker: 0}
    # Identity: the one owner_ordubashy channel, a null peer, bound to the Table source key.
    stored = query(host, "SELECT source_type, source_id, audience, peer_agent_id, source_key FROM owner_conversations WHERE source_id=?", source["id"])
    assert stored == [("table", source["id"], "owner_ordubashy", None, source_key("table", business, source["id"]))]
    for ctx in (owner(host, business), agent(host, host.coordinator, business)):
        assert [m["body"] for m in ok(get(host, ctx, source))["messages"]] == ["Owner to Ordubashy", "Ordubashy answers"]
    # Replay returns the saved receipt and appends nothing.
    again = ok(send(host, agent(host, host.coordinator, business), source, "Ordubashy answers", key))
    assert again["message"]["id"] == sent["message"]["id"]
    assert len(ok(get(host, owner(host, business), source))["messages"]) == 2
    # No worker channel exists: a worker cannot read the conversation or write into it, and no peer other than Ordubashy is addressable.
    assert code(get(host, agent(host, host.worker, business), source)) in {"forbidden", "policy_denied"}
    assert code(send(host, agent(host, host.worker, business), source, "Worker text")) in {"forbidden", "policy_denied"}
    assert code(get(host, owner(host, business), source, agent_id=host.worker, peer_role="ordubashy")) in {"forbidden", "validation_error"}
    peers = ok(call(host, owner(host, business), "owner_conversation_peers", {**({"scope": "global"} if business is None else {}), "source": source}))["peers"]
    assert [(peer["agent_id"], peer["channel"]) for peer in peers] == [(host.coordinator, "private")]
    # The private text is not a shared Table comment, and the shared thread is untouched.
    assert query(host, "SELECT COUNT(*) FROM thread_messages WHERE item_type='table' AND item_id=?", source["id"])[0][0] == 0


def test_unread_state_and_the_card_counters_follow_the_private_table_conversation(host):
    source = table_source(make_table(host, host.first, "Unread table"), host.first)
    ok(send(host, owner(host, host.first), source, "Owner question"))
    ok(send(host, agent(host, host.coordinator, host.first), source, "Ordubashy reply"))
    with host.database.read() as uow:
        context = host.runtime.application.current_context(owner(host, host.first), uow)
        state = private_state(context, source, uow)
        index = private_index(context, [source], uow)
    assert state["private_message_count"] == 2 and state["private_unread"] is True
    assert list(index) == [source_key("table", host.first, source["id"])]
    assert index[source_key("table", host.first, source["id"])] == state
    # A worker (not the owner, not Ordubashy) reads no counters at all.
    with host.database.read() as uow:
        assert private_index(host.runtime.application.current_context(agent(host, host.worker, host.first), uow), [source], uow) == {}
    latest = ok(get(host, owner(host, host.first), source))["messages"][-1]["sequence"]
    ok(call(host, owner(host, host.first), "owner_conversation_mark_read", {"source": source, "sequence": latest}))
    with host.database.read() as uow:
        context = host.runtime.application.current_context(owner(host, host.first), uow)
        assert private_state(context, source, uow)["private_unread"] is False
    # The Focus card carries the same counters; they are presentation and write nothing.
    item = present(host, source)
    before = query(host, "SELECT COUNT(*) FROM owner_conversation_messages")[0][0]
    shown = card(host, item["id"])["item"]
    assert shown["private_message_count"] == 2 and shown["private_unread"] is False
    assert query(host, "SELECT COUNT(*) FROM owner_conversation_messages")[0][0] == before


def test_an_archived_or_foreign_table_has_no_private_conversation(host):
    ctx = owner(host, host.first)
    absent = {"type": "table", "id": "no-such-table", "business_id": host.first}
    assert code(send(host, ctx, absent, "Nobody home")) in {"not_found", "forbidden"}
    foreign = make_table(host, host.second, "Elsewhere")
    assert code(send(host, ctx, {"type": "table", "id": foreign["id"], "business_id": host.first}, "Wrong business")) in {"not_found", "forbidden"}
    assert query(host, "SELECT COUNT(*) FROM owner_conversations WHERE source_type='table' AND source_id IN (?, 'no-such-table')", foreign["id"])[0][0] == 0


def test_private_table_messages_stay_out_of_shared_activity_and_broad_history(host):
    source = table_source(make_table(host, host.first, "Hidden talk"), host.first)
    ok(send(host, owner(host, host.first), source, "Private owner sentence"))
    assert "Private owner sentence" not in json.dumps(query(host, "SELECT * FROM thread_messages"))
    assert "owner_conversation_messages" not in exports.BUSINESS_TABLES


# ----- Project inventory ------------------------------------------------------------------------------------------------


def test_a_table_linked_into_a_project_can_open_a_conversation(host):
    table = make_table(host, host.first, "Project table")
    linked = ok(call(host, owner(host, host.first), "project_items_link",
                     {"project_id": host.first, "item": table_source(table, host.first)}))
    assert linked["item"]["type"] == "table" and linked["item"]["can_thread"] is True
    listed = ok(call(host, owner(host, host.first), "project_items_list", {"project_id": host.first, "limit": 100}))["items"]
    assert [item["can_thread"] for item in listed if item["type"] == "table" and item["id"] == table["id"]] == [True]
