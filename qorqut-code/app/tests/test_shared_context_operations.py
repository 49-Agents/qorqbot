"""P4A: the shared Context read contribution inside the real host catalog, policy resolver and Application.

The instance is the synthetic host-foundation one (two businesses, an owner, and the registered global Ordubashy and a worker
agent, each case's agent selecting one business; businesses have no resident agents). The
product host registration installs the shared read contribution directly over ``InstancePaths.context`` and
``context_runtime`` and registers none of the five per-business Context reads; the tests initialize the graph at those
final locations with the engine's own writer API. Every call still goes through ``Application.invoke`` so the policy
resolver, scope rules and output-schema validation are the real ones."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from qorqut_context import Context
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

plain_json = plain

import shared_context_support as support
from bazarlyk.composition.catalog import builtin_registry
from qorqut_context.integration.reading import (PublishedContextText, SharedContextOperations, SharedContextReader,
                                     shared_context_contribution)
from host_foundation_support import (agent_context, build_instance, create_registered_global, create_worker, global_agent_without_capability,
                                     historical_business_agent, register_sources, snapshot, WORKER_ROLE)

SHARED_READS = frozenset({"context_home", "context_object", "context_expand", "context_find", "context_changes"})
AGENT_READS = ("context_home", "context_object", "context_find", "context_expand")


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("shared-context-host"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Each business is read by a global agent selecting it; the historical business-home identity is only ever refused.
    agent_first = create_registered_global(instance, "ordubashy")
    agent_second = create_worker(instance, "colleague")
    historical = historical_business_agent(instance, first)
    # A business task the published sources point at: the pointer must never open it for the other business.
    foreign = instance.invoke(instance.human(second), "tasks_create", {"scope": "business", "title": "Second-only task"})
    assert foreign.status == "ok", foreign.error
    task = foreign.data["task"]
    paths = instance.paths
    graph = support.Synthetic(paths.context, paths.context_runtime).initialize()
    graph = graph.populate(origin_ref="task:" + task["id"]).draft()
    reader = instance.runtime.domains.shared_context
    instance.runtime.freeze_registry()
    yield SimpleNamespace(instance=instance, first=first, second=second, agents={first: agent_first, second: agent_second},
                          historical=historical, task=task, graph=graph, reader=reader)
    reader.close()


def call(host, ctx, name, arguments=None):
    """The Application result with its frozen data and error as plain JSON values."""
    result = host.instance.runtime.application.invoke(ctx, name, arguments or {}, InvocationOptions())
    return SimpleNamespace(status=result.status, data=None if result.data is None else plain(result.data),
                           error=None if result.error is None else plain(result.error))


def agent(host, business):
    return agent_context(host.agents[business], business)


def keys(value):
    """Every mapping key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {name for item in value.values() for name in keys(item)}
    return {name for item in value for name in keys(item)} if isinstance(value, list) else set()


def fingerprint(*roots):
    return sorted((str(path), path.stat().st_size, path.stat().st_mtime_ns)
                  for root in roots for path in root.rglob("*") if path.is_file())


def test_the_host_registers_the_shared_contribution_directly_and_the_old_reads_are_gone(host):
    contribution = shared_context_contribution(host.reader)
    assert contribution.owner == "shared-context" and {entry.name for entry in contribution.entries} == SHARED_READS
    retained = builtin_registry()
    for entry in contribution.entries:
        assert retained.metadata(entry.name) == entry
    registry = host.instance.runtime.registry
    assert registry.frozen
    for name in SHARED_READS:
        descriptor = registry.get(name)
        assert descriptor.effect_kind == "read" and descriptor.supports_signoff is False
        assert descriptor.scope_kinds == {"business", "global"} and descriptor.global_capability == "read_global_context"
        assert "published" in descriptor.description and "living brief" not in descriptor.description
    assert registry.get("context_changes").owner_only and registry.get("context_changes").exposure == {"dashboard", "worker"}
    for name in AGENT_READS:
        assert not registry.get(name).owner_only and registry.get(name).exposure == {"mcp", "dashboard", "worker"}
    # The old arguments are not accepted: callers use limit/cursor and the published IDs.
    assert set(registry.get("context_home").input_schema["properties"]) == {"limit", "cursor"}
    assert "action_kind" not in registry.get("context_changes").input_schema["properties"]
    # The retained owner reads and the old writers stay registered (their replacement is the next assignment).
    for name in ("context_filings", "context_upload_map", "context_filing_costs", "context_instruct", "context_edit"):
        assert registry.get(name).name == name
    # The registered reader is the one trusted assembly built over the instance's final graph locations.
    assert (host.reader._repository, host.reader._runtime) == (host.instance.paths.context, host.instance.paths.context_runtime)
    assert host.instance.runtime.domains.shared_context is host.reader
    assert [type(item).__name__ for item in contribution.descriptors] == ["OperationDescriptor"] * 5
    with pytest.raises(TypeError):
        SharedContextOperations(object())


def test_two_global_agents_in_different_businesses_get_identical_permitted_content(host):
    first, second = agent(host, host.first), agent(host, host.second)
    published = host.reader.home(limit=60)
    calls = [("context_home", {"limit": 60}), ("context_find", {"query": "atlas"}), ("context_find", {"query": ""}),
             ("context_object", {"ref": support.ATLAS}), ("context_object", {"ref": "edge:" + support.MANAGES}),
             ("context_expand", {"ref": support.ALICE, "depth": 2})]
    for name, arguments in calls:
        left, right = call(host, first, name, arguments), call(host, second, name, arguments)
        assert left.status == right.status == "ok", (name, left.error, right.error)
        assert left.data == right.data, name
        assert left.data["status"] == "ready" and left.data["revision"] == published["revision"]
    home = call(host, first, "context_home", {"limit": 60}).data
    assert home == published and {item["id"] for item in home["nodes"]} == {
        support.ALICE, support.ACME, support.ATLAS, support.BOBBY}
    owner = host.instance.human(host.first)
    assert call(host, owner, "context_home", {"limit": 60}).data == published


def test_every_read_is_validated_against_its_explicit_output_schema(host):
    registry = host.instance.runtime.registry
    sample = {"context_home": {}, "context_object": {"ref": support.ALICE}, "context_find": {"query": "alice"},
              "context_expand": {"ref": support.ALICE}}
    for name, arguments in sample.items():
        result = call(host, agent(host, host.first), name, arguments)
        assert result.status == "ok"
        assert registry.get(name).validate_output(result) is result
        spoiled = SimpleNamespace(status="ok", data={**result.data, "unreviewed_field": 1})
        with pytest.raises(DomainError) as error:
            registry.get(name).validate_output(spoiled)
        assert error.value.code == "invalid_result"


def test_scoped_operational_reads_stay_denied_and_a_source_pointer_opens_nothing(host):
    first = agent(host, host.first)
    source = next(item for item in call(host, first, "context_find", {"query": "", "kind": "source", "limit": 100}).data["items"]
                  if item["title"] == "Atlas note")
    pointer = call(host, first, "context_object", {"ref": source["ref"]}).data
    assert pointer["object"]["origin_ref"] == "task:" + host.task["id"]
    assert "Second-only task" not in json.dumps(pointer)
    # Operational reads stay scoped to the selected business, and the historical business-home identity reads nothing.
    with host.instance.database.read() as uow:
        before = snapshot(uow.connection, ("tasks", "idempotency_receipts", "apply_receipts"))
    denied = [call(host, first, "tasks_get", {"scope": "business", "task_id": host.task["id"]}),
              call(host, agent_context(host.historical, host.first), "tasks_get", {"scope": "business", "task_id": host.task["id"]}),
              call(host, agent_context(host.historical, host.second), "tasks_get",
                   {"scope": "business", "task_id": host.task["id"]})]
    assert [item.status for item in denied] == ["error"] * 3
    assert [item.error["code"] for item in denied] == ["not_found", "inactive_agent", "inactive_agent"]
    # A global agent reaches the Task only by selecting its business under its own permissions, never through the pointer.
    assert call(host, agent_context(host.agents[host.first], host.second), "tasks_get",
                {"scope": "business", "task_id": host.task["id"]}).status == "ok"
    assert call(host, agent(host, host.second), "tasks_get", {"scope": "business", "task_id": host.task["id"]}).status == "ok"
    with host.instance.database.read() as uow:
        assert snapshot(uow.connection, ("tasks", "idempotency_receipts", "apply_receipts")) == before


def test_the_caller_identity_comes_from_the_application_never_from_arguments(host):
    first = agent(host, host.first)
    baseline = call(host, first, "context_home", {}).data
    for forged in ({"agent_id": host.agents[host.second]}, {"business_id": host.second}, {"principal_id": host.instance.owner},
                   {"scope": "global"}, {"revision": baseline["revision"]}):
        refused = call(host, first, "context_home", forged)
        assert refused.status == "error" and refused.error["code"] == "validation_error", forged
    # A historical business-home identity, a selection of no active business and an unregistered principal read nothing.
    assert call(host, agent_context(host.historical, host.first), "context_home").error["code"] == "inactive_agent"
    assert call(host, agent_context(host.agents[host.first], new_id()), "context_home").status == "error"
    stranger = InvocationContext(Principal(new_id(), "agent"), business_id=host.first, source="mcp")
    assert call(host, stranger, "context_home").status == "error"
    assert call(host, InvocationContext(Principal(new_id(), "human"), business_id=host.first, source="dashboard"),
                "context_home").status == "error"
    assert call(host, first, "context_home", {}).data == baseline


def test_the_host_resolver_not_the_supplied_context_decides_the_agents_scope(host):
    registered = host.agents[host.first]
    baseline = call(host, agent(host, host.first), "context_home", {"limit": 60}).data
    # The Application re-derives a registered agent's scope and capabilities, so a caller-supplied business or capability
    # claim changes nothing: every variant, in either active business, reads the one published graph under its registration.
    for ctx in (InvocationContext(Principal(registered, "agent"), source="mcp"),
                InvocationContext(Principal(registered, "agent"), source="mcp", global_capabilities=frozenset({"read_global_context"})),
                InvocationContext(Principal(registered, "agent"), business_id=host.first, source="worker"),
                InvocationContext(Principal(registered, "agent"), business_id=host.second, source="mcp")):
        result = call(host, ctx, "context_home", {"limit": 60})
        assert result.status == "ok" and result.data == baseline
    # The same resolver keeps a global whose stored scope lost the capability out of Global operational scope, whatever it claims.
    revoked = global_agent_without_capability(host.instance, WORKER_ROLE)
    claim = InvocationContext(Principal(revoked, "agent"), source="mcp", global_capabilities=frozenset({"read_global_context"}))
    assert call(host, claim, "tasks_list", {"scope": "global"}).error["code"] == "forbidden"


def test_a_registered_global_agent_reads_the_same_graph_under_its_global_capability(host):
    global_agent = create_registered_global(host.instance, "learning-applier")      # another global, with no business selected
    ctx = InvocationContext(Principal(global_agent, "agent"), source="mcp")
    expected = call(host, agent(host, host.first), "context_home", {"limit": 60}).data
    result = call(host, ctx, "context_home", {"limit": 60})
    assert result.status == "ok" and result.data == expected
    for name, arguments in (("context_find", {"query": "atlas"}), ("context_object", {"ref": support.ATLAS}),
                            ("context_expand", {"ref": support.ALICE})):
        assert call(host, ctx, name, arguments).data == call(host, agent(host, host.first), name, arguments).data
    assert call(host, ctx, "context_changes", {}).error["code"] == "forbidden"


def test_changes_remain_owner_only(host):
    owner = host.instance.human(host.first)
    listed = call(host, owner, "context_changes", {"limit": 2})
    assert listed.status == "ok" and listed.data["total"] == 4 and listed.data["items"][0]["event_id"] == support.ACTION_EVENT
    assert listed.data["items"][0]["action"]["tags"][0]["business_id"] == "business:north"
    for ctx in (agent(host, host.first), agent(host, host.second),
                InvocationContext(Principal(host.agents[host.first], "agent"), business_id=host.first, source="dashboard"),
                InvocationContext(Principal(host.agents[host.first], "agent"), business_id=host.first, source="worker")):
        refused = call(host, ctx, "context_changes", {})
        assert refused.status == "error" and refused.error["code"] == "forbidden", ctx.source
    member = new_id()
    with host.instance.database.transaction() as uow:
        uow.repository("principals").insert({"id": member, "kind": "human", "display_name": "Member"})
        uow.repository("business_memberships", business_id=host.first).insert({"principal_id": member, "is_owner": False})
    plain = InvocationContext(Principal(member, "human"), business_id=host.first, allowed_business_ids=frozenset({host.first}),
                              source="dashboard")
    assert call(host, plain, "context_changes", {}).error["code"] == "forbidden"
    outsider = host.instance.human(host.second)
    assert call(host, InvocationContext(Principal(member, "human"), business_id=host.second,
                                        allowed_business_ids=frozenset({host.second}), source="dashboard"),
                "context_changes", {}).error["code"] == "forbidden"
    assert outsider.principal.id == host.instance.owner
    # The agent-facing reads carry none of the change feed's attribution fields (submitter, source path, Action envelope).
    action_source = next(item for item in call(host, agent(host, host.first), "context_find",
                                               {"query": "", "kind": "source", "limit": 100}).data["items"]
                         if item["source_kind"] == "accepted_action")
    reads = [call(host, agent(host, host.first), name, arguments).data for name, arguments in (
        ("context_home", {"limit": 60}), ("context_find", {"query": "", "limit": 100}),
        ("context_object", {"ref": support.ATLAS}), ("context_object", {"ref": action_source["ref"]}),
        ("context_expand", {"ref": support.ALICE, "depth": 3}))]
    assert not {"submitted_by", "action", "handoff_disposition", "path", "changes"} & keys(reads)
    assert "synthetic-recorder" not in json.dumps(reads)
    # Accepted Action evidence is committed source text; reading it is reading published evidence, not the owner feed.
    assert "Routine completed" in reads[3]["content"]["text"]


def test_draft_data_is_absent_and_reads_leave_the_context_unchanged(host):
    graph = host.graph
    before = fingerprint(graph.repository, graph.runtime)
    ctx = agent(host, host.first)
    results = [call(host, ctx, "context_home", {"limit": 60}), call(host, ctx, "context_find", {"query": "", "limit": 100}),
               call(host, ctx, "context_find", {"query": "", "kind": "source", "limit": 100}),
               call(host, ctx, "context_object", {"ref": support.ALICE}),
               call(host, host.instance.human(host.first), "context_changes", {"limit": 100})]
    assert [item.status for item in results] == ["ok"] * 5
    text = json.dumps([item.data for item in results])
    assert "drft1" not in text and support.DRAFT_MARKER not in text and "draft-only" not in text.lower()
    assert call(host, ctx, "context_object", {"ref": "drft1"}).error["code"] == "not_found"
    for query in (support.DRAFT_MARKER, "drft1"):
        assert call(host, ctx, "context_find", {"query": query}).data["items"] == []
    assert fingerprint(graph.repository, graph.runtime) == before


def test_pinned_pagination_survives_a_later_publication_and_crosses_agents(host):
    first, second = agent(host, host.first), agent(host, host.second)
    page = call(host, first, "context_home", {"limit": 2}).data
    found = call(host, first, "context_find", {"query": "", "limit": 3}).data
    assert page["page"]["has_more"] and found["page"]["has_more"]
    published = host.graph.publish_draft()
    assert published != page["revision"]
    continued = call(host, second, "context_home", {"limit": 2, "cursor": page["page"]["next_cursor"]})
    assert continued.status == "ok" and continued.data["revision"] == page["revision"]
    assert "drft1" not in json.dumps(continued.data)
    more = call(host, second, "context_find", {"query": "", "limit": 3, "cursor": found["page"]["next_cursor"]}).data
    assert more["revision"] == found["revision"] and "drft1" not in json.dumps(more)
    fresh = call(host, first, "context_home", {"limit": 60}).data
    assert fresh["revision"] == published and fresh["totals"]["nodes"] == 5
    mismatch = call(host, first, "context_find", {"query": "other", "limit": 3, "cursor": found["page"]["next_cursor"]})
    assert mismatch.status == "error" and mismatch.error["code"] == "validation_error"
    tampered = call(host, first, "context_home", {"cursor": page["page"]["next_cursor"][:-3] + "AAA"})
    assert tampered.error["code"] == "validation_error"


def test_input_bounds_are_enforced_by_the_operation_schemas(host):
    ctx = agent(host, host.first)
    for name, arguments in (("context_home", {"limit": 0}), ("context_home", {"limit": 61}), ("context_object", {}),
                            ("context_object", {"ref": "ATLAS"}), ("context_object", {"ref": "node:atlas", "text_limit": 70000}),
                            ("context_find", {"limit": 101}), ("context_find", {"kind": "event"}),
                            ("context_find", {"query": "x" * 2001}), ("context_expand", {"ref": "alice", "depth": 4}),
                            ("context_expand", {"ref": "alice", "direction": "up"}), ("context_expand", {}),
                            ("context_expand", {"ref": "alice", "limit": 201}), ("context_home", {"cursor": ""})):
        result = call(host, ctx, name, arguments)
        assert result.status == "error" and result.error["code"] == "validation_error", (name, arguments)
    assert call(host, ctx, "context_object", {"ref": "node:zzzzz"}).error["code"] == "not_found"
    assert call(host, ctx, "context_expand", {"ref": "edge:" + support.LEADS}).error["code"] == "validation_error"


def test_a_missing_repository_differs_from_an_empty_initialized_graph(tmp_path):
    instance, ids = build_instance(tmp_path)
    business = ids["first"]
    register_sources(instance, business, "first")
    agent_id = create_registered_global(instance, "ordubashy")
    instance.runtime.freeze_registry()
    repository, runtime = instance.paths.context, instance.paths.context_runtime
    ctx = agent_context(agent_id, business)
    application = instance.runtime.application
    texts = PublishedContextText(instance.runtime.domains.shared_context)
    assert not (repository / ".git").exists()
    missing = application.invoke(ctx, "context_home", {}, InvocationOptions())
    missing = SimpleNamespace(status=missing.status, data=plain(missing.data))
    assert missing.status == "ok" and missing.data["status"] == "not_initialized" and "empty" not in missing.data
    assert application.invoke(ctx, "context_find", {"query": "x"}, InvocationOptions()).data["status"] == "not_initialized"
    assert application.invoke(ctx, "context_object", {"ref": "alice"}, InvocationOptions()).data["revision"] is None
    assert "Not initialized" in texts.home_text() and "not an empty graph" in texts.home_text()
    assert not (repository / ".git").exists() and not runtime.exists()
    Context.initialize(repository, runtime, basic_entity_types=["person"])
    empty = application.invoke(ctx, "context_home", {}, InvocationOptions())
    empty = SimpleNamespace(status=empty.status, data=plain(empty.data))
    assert empty.status == "ok" and empty.data["status"] == "ready" and empty.data["empty"] is True
    assert empty.data["nodes"] == [] and len(empty.data["revision"]) == 40
    assert "Initialized and empty" in texts.home_text() and "Not initialized" not in texts.home_text()
    instance.runtime.domains.shared_context.close()


# ---- the host read consumers: bootstrap text, owner bootstrap, briefing, navigation and history


@pytest.fixture
def bound_text(host):
    """The provider binding ``service.main.assemble`` makes, applied to this instance's resolver and owner bootstrap."""
    runtime = host.instance.runtime
    published = PublishedContextText(host.reader)
    runtime.resolver.context_home = runtime.owner_bootstrap.context_home = published.home_text
    runtime.owner_bootstrap.context_status = published.status
    yield published
    del runtime.resolver.context_home
    runtime.owner_bootstrap.context_home = runtime.owner_bootstrap.context_status = None


def section(rendered, title):
    return rendered.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def pillar_documents(host, business):
    """The business's registered authored Product/ICP/Brand documents: ordinary scoped sources, not Context briefs."""
    with host.instance.database.read() as uow:
        found = {row["id"]: row for row in uow.connection.execute(
            "SELECT * FROM documents WHERE business_id=?", (business,))
            if json.loads(row["metadata_json"]).get("bootstrap_key") in ("product", "icp", "brand")}
    assert len(found) == 3
    return found


def test_managed_agent_bootstrap_carries_the_same_published_graph_text_and_no_brief(host, bound_text):
    resolver, rendered, entries = host.instance.runtime.resolver, {}, {}
    for business in (host.first, host.second):
        config = resolver.resolve_agent_config(Principal(host.agents[business], "agent"), business_id=business).to_dict()
        rendered[business] = config["mandatory_context"]["rendered"]
        entries[business] = config["mandatory_context"]["entries"]
    home = {business: section(text, "Context Home") for business, text in rendered.items()}
    published = host.reader.home(limit=12)
    assert (home[host.first].split("\n", 1)[1].strip() == home[host.second].split("\n", 1)[1].strip()
            == bound_text.home_text())
    text = bound_text.home_text()
    assert f"Revision {published['revision']}" in text and "node:" + support.ALICE in text
    assert "edge:" + support.LEADS in text and "Alice leads the Atlas program" in text
    assert "Initialized and empty" not in text and "Not initialized" not in text
    for business in (host.first, host.second):
        assert "published Context graph" in rendered[business] and "They read one shared published graph" in rendered[business]
        # The authored pillar documents are neither injected beside the graph nor required: the graph is the orientation.
        for pillar in ("Core Product", "Core Icp", "Core Brand"):
            assert f"## {pillar}" not in rendered[business]
        for document_id in pillar_documents(host, business):
            assert document_id not in rendered[business]
            assert document_id not in {entry["document_id"] for entry in entries[business]}
        for retired in ("living brief", "About The Owner", "Latest:", "context-base:", "room and recent impacts"):
            assert retired not in rendered[business], retired
        assert "context_edit for explicit structured changes" not in rendered[business]


def test_a_business_without_registered_pillar_documents_still_bootstraps_from_the_graph(tmp_path):
    instance, ids = build_instance(tmp_path)
    business = ids["first"]
    register_sources(instance, business, "first")
    agent_id = create_registered_global(instance, "ordubashy")
    with instance.database.transaction() as uow:         # the business's authored pillar documents are simply not registered
        uow.connection.execute("DELETE FROM documents WHERE business_id=? AND json_extract(metadata_json,'$.bootstrap_key') "
                               "IN ('product','icp','brand')", (business,))
        assert uow.connection.execute("SELECT count(*) FROM documents WHERE business_id=? AND json_extract(metadata_json,"
                                      "'$.bootstrap_key') IN ('product','icp','brand')", (business,)).fetchone()[0] == 0
    paths = instance.paths
    support.Synthetic(paths.context, paths.context_runtime).initialize().populate().draft()
    published = PublishedContextText(instance.runtime.domains.shared_context)
    instance.runtime.resolver.context_home = published.home_text
    try:
        config = instance.runtime.resolver.resolve_agent_config(Principal(agent_id, "agent"), business_id=business).to_dict()
    finally:
        del instance.runtime.resolver.context_home
    rendered = config["mandatory_context"]["rendered"]
    assert "## Context Home" in rendered and "node:" + support.ALICE in rendered and "## Core " not in rendered
    instance.runtime.domains.shared_context.close()


def test_the_graph_text_is_bounded_and_keeps_the_references_it_shows(tmp_path):
    graph = support.Synthetic(tmp_path / "context", tmp_path / "context.runtime").initialize().populate_large()
    reader = SharedContextReader(graph.repository, graph.runtime)
    try:
        text = PublishedContextText(reader).home_text("any-business")
        assert len(text) <= 6000 and text.count("\n- node:") <= 12
        assert "node:bigxx" in text or "node:hubxx" in text
        assert "relationship" in text and text.startswith("Shared Context (published graph)\n")
        assert text == PublishedContextText(reader).home_text("another-business")
    finally:
        reader.close()


def test_owner_bootstrap_reads_only_the_published_graph_as_its_brief(host, bound_text):
    runtime, owner = host.instance.runtime, host.instance.human(host.first)
    with host.instance.database.read() as uow:
        result = plain(runtime.owner_bootstrap.read(owner, uow))
    assert [brief["key"] for brief in result["briefs"]] == ["context_home"]
    brief = result["briefs"][0]
    assert brief["source"] == "context:published" and "document_id" not in brief and brief["text"] == bound_text.home_text()
    assert not [item for item in result["missing"] if item["source"] == "context_home" or item["source"].startswith("brief:")]
    assert result["character_count"] == len(brief["text"]) <= result["character_limit"]
    # The authored pillar documents are ordinary scoped sources: only the registered-metadata index may list them.
    pillars = pillar_documents(host, host.first)
    assert not {item for item in pillars if item in json.dumps(result["briefs"])}
    assert {item["id"] for item in result["context_index"]["items"]} >= set(pillars)
    text = json.dumps(result)
    assert "context-base:" not in text and "Maintain canonical context" not in text and "context_edit" not in text


def test_owner_bootstrap_reports_a_graph_that_is_not_readable_instead_of_an_empty_one_or_authored_briefs(host, tmp_path):
    reader = SharedContextReader(tmp_path / "absent", tmp_path / "absent.runtime")
    published = PublishedContextText(reader)
    owner = host.instance.human(host.first)
    documents = host.instance.runtime.domains.documents
    from bazarlyk.owner_bootstrap import OwnerSessionBootstrap
    try:
        for provider, status in ((published.home_text, published.status), (None, None)):
            bootstrap = OwnerSessionBootstrap(documents, provider, status)
            with host.instance.database.read() as uow:
                result = plain(bootstrap.read(owner, uow))
            reasons = [item["reason"] for item in result["missing"] if item["source"] == "context_home"]
            assert reasons == (["not_initialized"] if provider else ["not_configured"])
            # An unreadable graph is not replaced by the authored briefs.
            assert [brief["key"] for brief in result["briefs"]] == (["context_home"] if provider else [])
            assert not [item for item in result["missing"] if item["source"].startswith("brief:")]
            if provider:
                assert "Not initialized" in result["briefs"][0]["text"] and "not an empty graph" in result["briefs"][0]["text"]
    finally:
        reader.close()


def test_business_briefing_shows_the_one_shared_graph_to_every_scoped_agent(host):
    briefs = {business: call(host, agent(host, business), "business_briefing") for business in (host.first, host.second)}
    assert {item.status for item in briefs.values()} == {"ok"}, [item.error for item in briefs.values()]
    left, right = briefs[host.first].data, briefs[host.second].data
    assert "brief" not in left and "brief" not in right and left["context"] == right["context"]
    published = host.reader.home(limit=6)
    context = left["context"]
    assert context["status"] == "ready" and context["revision"] == published["revision"]
    assert context["totals"] == published["totals"] and context["empty"] is False
    assert [item["ref"] for item in context["nodes"]] == [node["ref"] for node in published["nodes"]]
    assert all(set(item) == {"ref", "summary"} for item in context["nodes"])
    assert "context_home" in context["more"] and left["business"]["id"] != right["business"]["id"]
    assert "living brief" not in json.dumps([left, right]) and "context" not in left.get("unavailable", {})


def test_business_briefing_reports_an_uninitialized_graph_explicitly(tmp_path):
    instance, ids = build_instance(tmp_path)
    business = ids["first"]
    register_sources(instance, business, "first")
    agent_id = create_registered_global(instance, "ordubashy")
    instance.runtime.freeze_registry()
    result = instance.runtime.application.invoke(agent_context(agent_id, business), "business_briefing", {}, InvocationOptions())
    assert result.status == "ok"
    context = plain(result.data)["context"]
    assert context["status"] == "not_initialized" and context["reason"] == "repository_missing"
    assert "nodes" not in context and "empty" not in context
    instance.runtime.domains.shared_context.close()


def test_the_people_count_comes_from_the_published_graph_for_any_business(host):
    expected = sum("person" in node["attributes"].get("basic_entity_type", [])
                   for node in host.reader.find("", kind="node", limit=100)["items"])
    counts = {}
    for business in (host.first, host.second):
        result = call(host, host.instance.human(business), "context_navigation_get", {})
        assert result.status == "ok", result.error
        counts[business] = result.data["counts"]
    assert counts[host.first]["person"] == counts[host.second]["person"] == expected >= 2
    assert counts[host.first] == counts[host.second]
    assert host.reader.count_nodes({"basic_entity_type": ["person"]})["count"] == expected
    assert host.reader.count_nodes({"basic_entity_type": ["nothing"]})["count"] == 0
    with pytest.raises(DomainError):
        host.reader.count_nodes({"basic_entity_type": [{"bad": 1}]})


def navigation(instance, business):
    result = instance.runtime.application.invoke(instance.human(business), "context_navigation_get", {}, InvocationOptions())
    assert result.status == "ok", result.error
    return plain(result.data)


def operational_people(instance, business):
    """The old people count: the business's human members. It must never stand in for the graph's person nodes."""
    with instance.database.read() as uow:
        return sum(uow.repository("principals").get(member["principal_id"])["kind"] == "human"
                   for member in uow.connection.execute(
                       "SELECT principal_id FROM business_memberships WHERE business_id=?", (business,)))


def test_navigation_reports_the_published_revision_beside_a_ready_person_count(host):
    result = navigation(host.instance, host.first)
    assert result["navigation_only"] is True and set(result["counts"]) == {"table", "person"}
    assert result["context"] == {"status": "ready", "reason": None, "revision": host.reader.home(limit=1)["revision"]}
    assert result["counts"]["person"] == host.reader.count_nodes({"basic_entity_type": ["person"]})["count"] >= 2


def test_navigation_person_count_is_unknown_for_a_missing_graph_and_zero_only_for_a_ready_empty_one(tmp_path):
    instance, ids = build_instance(tmp_path)
    business = ids["first"]
    assert operational_people(instance, business) >= 1          # the old fallback would have reported a nonzero count
    try:
        missing = navigation(instance, business)
        assert missing["counts"]["person"] is None
        assert missing["context"]["status"] == "not_initialized" and missing["context"]["revision"] is None
        assert missing["context"]["reason"]["code"] == "repository_missing"
        assert missing["counts"]["table"] == 0
        paths = instance.paths
        support.Synthetic(paths.context, paths.context_runtime).initialize()
        empty = navigation(instance, business)
        assert empty["counts"]["person"] == 0 and empty["counts"]["person"] is not None
        assert empty["context"]["status"] == "ready" and empty["context"]["reason"] is None
        assert empty["context"]["revision"] == instance.runtime.domains.shared_context.home(limit=1)["revision"]
        assert empty["counts"]["table"] == 0
    finally:
        instance.runtime.domains.shared_context.close()


def test_navigation_keeps_an_unreadable_graph_and_an_absent_reader_unknown_not_zero(tmp_path):
    from bazarlyk.domains.items.adapters import HostNavigation

    instance, ids = build_instance(tmp_path)
    business, items = ids["first"], instance.runtime.domains.items
    plain_file = tmp_path / "not-a-repository"
    plain_file.write_text("not a repository")
    reader = SharedContextReader(plain_file, tmp_path / "unreadable.runtime")
    ctx = instance.human(business)
    try:
        for shared, status, code in ((reader, "unavailable", "not_a_context_repository"), (None, "unavailable", "not_configured")):
            with instance.database.read() as uow:
                result = plain_json(HostNavigation(items, SimpleNamespace(shared_context=shared)).read(ctx, {}, uow))
            assert result["counts"]["person"] is None and result["context"]["revision"] is None
            assert result["context"]["status"] == status and result["context"]["reason"]["code"] == code
    finally:
        reader.close()
        instance.runtime.domains.shared_context.close()


def test_the_old_context_history_sources_are_no_longer_offered(host):
    owner = host.instance.human(host.first)
    for source in ("context_object", "context_brief"):
        refused = call(host, owner, "document_history_get", {"source_kind": source, "source_id": support.ALICE})
        assert refused.status == "error" and refused.error["code"] == "validation_error", source
    assert call(host, owner, "business_history_list", {"source": "context"}).error["code"] == "validation_error"
    listed = call(host, owner, "business_history_list", {"source": "core"})
    assert listed.status == "ok" and listed.data["source"] == "core"
    # The Context's history is the published graph's: the object read and the owner change feed.
    assert call(host, agent(host, host.first), "context_object", {"ref": support.ATLAS}).data["history"]["items"]
    assert call(host, owner, "context_changes", {"ref": support.ATLAS}).data["total"] == 2
