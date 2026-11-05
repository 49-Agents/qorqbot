"""Neutral records (O7) and Project settings (O8) after their move into Operations, through the actual synthetic host.

The generic lookup, related-resource, Material/archive and settings algorithms live in ``qorqut_operations.records`` and
``qorqut_operations.projects.settings_operations``; the host keeps its configured-global identity, Context navigation and
settings authority behind required adapters (the core mail model and its tables are retired). ``observe_records`` and ``observe_settings`` record what the
host answers (values and errors) so the same assertions hold on the pre-move baseline and on the split code; the last
cases are about the split itself: declaration equality and Operations importing without the host, Context or extensions.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network or operational data."""

import contextlib
import dataclasses
import json
import re
import subprocess
import sys
from unittest import mock

from qorqut_operations.application.plans import PlanBuilder
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain
from qorqut_operations.documents.materials import classification

from bazarlyk import owner_sessions
from bazarlyk.agents import engineering
from bazarlyk.browser import config as browser_config
from bazarlyk.domains import business_settings
from host_foundation_support import historical_business_agent, set_agent_overrides
from test_host_neutral_items import agent_note, business_material, call, host, ok, reclassify, resolve, rows, task

NOW = "2026-10-11T12:00:00Z"
ENTITY = "e0000000-0000-4000-8000-000000000001"
LEGACY_ACTION = "f0000000-0000-4000-8000-000000000001"
TABLE = "d0000000-0000-4000-8000-000000000001"


def seed_records(host):
    """A Context entity, a data table and a business-stored (legacy) Action."""
    with host.instance.database.transaction() as uow:
        uow.repository("context_entities", business_id=host.first).insert({
            "id": ENTITY, "label": "Docs site", "url": "https://docs.example.test", "created_at": NOW, "updated_at": NOW})
        uow.repository("data_tables", business_id=host.first).insert({
            "id": TABLE, "title": "Pipeline", "columns_json": [], "created_by": host.instance.owner, "created_at": NOW, "updated_at": NOW})
        # A business-stored (legacy) Action, linked to its business.
        key = f"business/{host.first}/{LEGACY_ACTION}"
        uow.repository("action_logs", business_id=host.first).insert({
            "id": LEGACY_ACTION, "action_key": key, "category": "business-operational", "action_text": "Closed the books for September",
            "recorded_by": host.instance.owner, "origin": "legacy", "occurred_at": NOW, "created_at": NOW, "updated_at": NOW})
        uow.connection.execute("INSERT INTO action_businesses(action_key,business_id,created_at) VALUES (?,?,?)", (key, host.first, NOW))


def read(host, action):
    with host.instance.database.read() as uow:
        return action(uow)


def failure(result):
    assert result.status != "ok", result.data
    error = result.error or {}
    return result.status, error.get("code"), error.get("message")


def outcome(function, *arguments, **keywords):
    """The value of a lookup, or its error code (an error is part of what must not change)."""
    try:
        return function(*arguments, **keywords)
    except DomainError as error:
        return error.code


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def scrub(seen, host, names):
    """The observations as JSON with every generated identifier replaced by its role (the synthetic ones are fixed)."""
    names = {host.first: "first", host.second: "second", host.agent: "agent", host.other: "colleague", host.instance.owner: "owner", **names}
    text = json.dumps(seen, sort_keys=True, default=str)
    for identity, name in names.items():
        text = text.replace(identity, name)
    return json.loads(UUID.sub("<id>", text) if False else text)


def resolved(host, ctx, item, uow):
    builder = PlanBuilder(ctx, "probe", {}, "probe")
    host.runtime.domains.items.resolve(ctx, item, uow, builder)
    return builder


def pins(builder):
    return sorted((ref.resource_type, ref.business_id, ref.namespace) for ref in builder.targets)


def observe_records(host):
    """Everything the O7 move must leave unchanged, as plain data without generated identifiers."""
    seen = {}
    domains, owner, items = host.runtime.domains, host.owner, host.runtime.domains.items
    seed_records(host)
    work, note = task(host, "Plan launch"), business_material(host)
    private = agent_note(host)

    def labelled(ctx, kind=None):
        def listing(uow):
            return [(entry["type"], outcome(lambda: items.resolve(ctx, entry, uow)["label"])) for entry in items.candidates(ctx, uow, kind=kind)]
        return read(host, listing)

    # Candidate enumeration: resource tables in the retained order, then documents, then the human members.
    seen["candidates"] = labelled(owner)
    seen["material_candidates"] = labelled(owner, "material")
    seen["entity_candidates"] = labelled(owner, "entity")
    seen["global_candidates"] = labelled(host.owner_global)

    # The owner-only journal and excluded working-memory guards answer for a stored note.
    item = {"type": "material", "id": private["id"]}
    reclassify(host, private["id"], "business_note")
    seen["journal"] = [outcome(resolve, host, ctx, item) for ctx in (host.agent_global, host.global_context)]
    reclassify(host, private["id"], "working_memory")
    seen["working_memory"] = [outcome(resolve, host, ctx, item) for ctx in (host.owner_global, host.agent_global)]

    # Relationship identity: a Material is itself.
    seen["relationship_item"] = read(host, lambda uow: [
        items.relationship_item(owner, {"type": "material", "id": note["id"]}, uow)["id"] == note["id"]])
    seen["entity"] = {key: value for key, value in resolve(host, owner, {"type": "entity", "id": ENTITY}).items()
                      if key in ("label", "url", "resource_type", "archived", "revision")}

    # Canonical Action pins: the Action's true storage identity, and a Global-stored Action as a read-only Global reference.
    action = ok(call(host, host.agent_context, "actions_record", {
        "text": "Shipped the release candidate to staging", "category": "business-operational", "business_ids": [host.first]}))["action"]
    shared = ok(call(host, host.agent_global, "actions_record", {
        "text": "Rotated the shared mailbox credentials", "category": "global-operational"}))["action"]
    seen["action_pins"] = {name: read(host, lambda uow: pins(resolved(host, ctx, {"type": "action", "id": identity}, uow)))
                           for name, ctx, identity in (("canonical", owner, action["action_id"]), ("legacy", owner, LEGACY_ACTION),
                                                   ("global", host.owner_global, shared["action_id"]))}
    seen["action"] = {name: {key: value for key, value in resolve(host, ctx, {"type": "action", "id": identity}).items()
                             if key in ("resource_type", "archived")}
                      for name, ctx, identity in (("canonical", owner, action["action_id"]), ("legacy", owner, LEGACY_ACTION),
                                                  ("global", host.owner_global, shared["action_id"]))}

    # Current vs historical agent pins: a global is pinned with its principal (a read-only ecosystem profile inside a business),
    # a business-home profile only with its home; retired and historical ones read as archived.
    historical = historical_business_agent(host.instance, host.first, status="inactive")
    seen["agent_pins"] = {
        "global_in_business": read(host, lambda uow: pins(resolved(host, owner, {"type": "agent", "id": host.agent}, uow))),
        "global_in_global": read(host, lambda uow: pins(resolved(host, host.owner_global, {"type": "agent", "id": host.agent}, uow))),
        "historical": read(host, lambda uow: pins(resolved(host, owner, {"type": "agent", "id": historical}, uow))),
        "archived": [resolve(host, ctx, {"type": "agent", "id": identity})["archived"]
                     for ctx, identity in ((owner, host.agent), (owner, historical))],
        "historical_elsewhere": outcome(resolve, host, host.owner_second, {"type": "agent", "id": historical})}

    # Representative fingerprints: stable and content-sensitive.
    def fingerprint(item, ctx=owner):
        return read(host, lambda uow: items.fingerprint(ctx, item, uow))

    def stable(item):
        """An Action and an agent profile embed a wall-clock value: only a stable 64-hex token is comparable between runs."""
        first = fingerprint(item)
        return bool(re.fullmatch(r"[0-9a-f]{64}", first)) and first == fingerprint(item)

    seen["fingerprints"] = {
        "material": fingerprint({"type": "material", "id": note["id"]}), "task": fingerprint({"type": "task", "id": work["id"]}),
        "person": fingerprint({"type": "person", "id": host.instance.owner}), "action": stable({"type": "action", "id": action["action_id"]}),
        "entity": fingerprint({"type": "entity", "id": ENTITY}), "agent": stable({"type": "agent", "id": host.agent})}
    with host.instance.database.read() as uow:
        builder = PlanBuilder(owner, "probe", {}, "probe")
        items.fingerprint(owner, {"type": "material", "id": note["id"]}, uow, builder)
    seen["fingerprint_pins"] = pins(builder)

    # The related-resource resolver: the host's rows and agent reach come first, everything else is read as before.
    related = domains.related
    seen["related"] = read(host, lambda uow: {
        "agent": related.get(owner, uow, "agent_profiles", host.agent)["label"],
        "inactive_agent": outcome(related.get, owner, uow, "agent_profiles", historical),
        "task": {key: related.get(owner, uow, "tasks", work["id"])[key] for key in ("label", "task_type", "state")},
        "unknown": outcome(related.get, owner, uow, "tasks", new_id())})

    # Material classification: a historical email-message document and an agent note are not classified, the refusal sits
    # between the generic one and the revision check; the raw upload kinds have a fixed type.
    topic = ok(call(host, owner, "context_topics_create", {"title": "Launch", "slug": "launch"}))["document"]
    mail_copy, upload, material = (ok(call(host, owner, "context_notes_create", {
        "topic_id": topic["id"], "title": title, "body": "Plan.\n", "material_type": "note"}))["document"] for title in ("Mail copy", "Upload", "Launch notes"))
    historical_note = business_material(host, "Historical agent note")
    reclassify(host, mail_copy["id"], "email_message")
    reclassify(host, upload["id"], "context_upload")
    reclassify(host, historical_note["id"], "agent_note")
    wrong = {"material_type": "excerpt"}

    def revision_of(identity):
        return resolve(host, owner, {"type": "material", "id": identity})["revision"]

    seen["materials"] = {
        "message_document": failure(call(host, owner, "materials_update", {"material_id": mail_copy["id"], "changes": wrong}, revision=99)),
        "agent_note": failure(call(host, owner, "materials_update", {"material_id": historical_note["id"], "changes": wrong}, revision=99)),
        "stale_revision": failure(call(host, owner, "materials_update", {"material_id": material["id"], "changes": wrong}, revision=99)),
        "upload_type": failure(call(host, owner, "materials_update", {"material_id": upload["id"], "changes": wrong}, revision=revision_of(upload["id"]))),
        "recorded": ok(call(host, owner, "materials_update", {"material_id": material["id"], "changes": {"tags": ["draft"]}},
                            revision=material["revision"]))["item"]["tags"]}

    # Composed archive: a direct record table is archived by its flag; a Task or table archive is the owning operation's,
    # under its Signoff floor.
    def archive(item, archived, revision):
        return call(host, owner, "context_items_archive", {"item": item, "archived": archived}, revision=revision)

    entity_item = {"type": "entity", "id": ENTITY}
    entity_view = resolve(host, owner, entity_item)
    archived = ok(archive(entity_item, True, entity_view["revision"]))["item"]
    seen["archive"] = {
        "entity": [archived["archived"], ok(archive(entity_item, False, archived["revision"]))["item"]["archived"]],
        "stale": failure(archive(entity_item, True, 99))}
    set_agent_overrides(host.instance, host.agent, {"tasks_update": "signoff"}, [host.first])
    proposed = call(host, host.agent_context, "context_items_archive", {"item": {"type": "task", "id": work["id"]}, "archived": True},
                    revision=work["revision"])
    seen["archive"]["task_floor"] = [proposed.status, rows(host, "SELECT archived FROM tasks")[0]["archived"],
                                     rows(host, "SELECT COUNT(*) AS n FROM signoffs WHERE state='pending'")[0]["n"]]
    seen["archive"]["table"] = failure(archive({"type": "table", "id": TABLE}, True, 1))

    # Navigation: the person count is the published Context reader's alone; unknown is null, never zero. The reader is
    # looked up at call time (it is a late-bound host service).
    class Reader:
        def __init__(self, result):
            self.result, self.queries = result, []

        def count_nodes(self, query):
            self.queries.append(query)
            return self.result

    published = domains.documents
    original, seen["navigation"], readers = published.shared_context, {}, {}
    for name, answer in (("ready", {"status": "ready", "count": 4, "revision": "r7"}), ("empty", {"status": "ready", "count": 0, "revision": "r7"}),
                         ("not_initialized", {"status": "not_initialized", "reason": {"code": "not_published", "message": "No graph"}, "revision": None}),
                         ("unavailable", {"status": "unavailable", "reason": {"code": "unreadable", "message": "Unreadable"}, "revision": "r3"})):
        published.shared_context = readers[name] = Reader(answer)
        seen["navigation"][name] = ok(call(host, owner, "context_navigation_get", {}))
    published.shared_context = None
    seen["navigation"]["not_configured"] = ok(call(host, owner, "context_navigation_get", {}))
    published.shared_context = original
    seen["navigation_queries"] = {name: reader.queries for name, reader in readers.items()}
    return scrub(seen, host, {work["id"]: "task", note["id"]: "note", private["id"]: "private-note", historical: "historical-agent",
                              action["action_id"]: "canonical-action", shared["action_id"]: "global-action", mail_copy["id"]: "mail-copy",
                              upload["id"]: "upload", material["id"]: "launch-notes", topic["id"]: "topic"})


class Calls:
    """The host's settings guards, recorded in call order (each real one still runs; the browser ones are stubs, so nothing
    allocates a port or touches a browser)."""

    def __init__(self, host):
        self.host, self.log, self.stack = host, [], contextlib.ExitStack()

    def __enter__(self):
        host = self.host
        summaries = {
            "protect_owner_session_configuration": lambda a, k: {"owner_session": k["owner_session"], "deleted": sorted(k.get("explicit_deletions", ()))},
            "has_ordubashy_settings_authority": lambda a, k: {},
            "prepare_authority": lambda a, k: {"delegated": k["delegated"]},
            "prepare_owner_sessions": lambda a, k: {"principal_kind": k["principal_kind"]},
            "require_settings_authority": lambda a, k: {},
            "prepare_config": lambda a, k: {"paths": k["paths"] == host.instance.paths, "delegated": k["delegated"], "uow": k["uow"].connection.in_transaction},
            "validate_applied": lambda a, k: {"paths": k["paths"] == host.instance.paths, "in_transaction": k["uow"].connection.in_transaction,
                                              "uow": getattr(self, "writer", None) is k["uow"]}}
        stubs = {"prepare_config", "validate_applied"}
        for module, name in ((owner_sessions, "protect_owner_session_configuration"), (engineering, "has_ordubashy_settings_authority"),
                             (engineering, "prepare_authority"), (owner_sessions, "prepare_owner_sessions"),
                             (business_settings, "require_settings_authority"), (browser_config, "prepare_config"), (browser_config, "validate_applied")):
            real = getattr(module, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                self.log.append([_name, summaries[_name](args, kwargs)])
                return None if _name in stubs else _real(*args, **kwargs)
            self.stack.enter_context(mock.patch.object(module, name, spy))
        return self

    def __exit__(self, *error):
        self.stack.close()


def observe_settings(host):
    """What the O8 move must leave unchanged: the three operations' results and errors, the order of the host guards, and the
    authority and browser checks that run again in the applying transaction."""
    seen, owner, registry = {}, host.owner, host.runtime.application.registry
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET stage='validating'")
        uow.connection.execute("UPDATE businesses SET config_json=? WHERE id=?", (json.dumps({"tool_policy": {"mode": "ask"}}), host.first))
    session = dataclasses.replace(owner, source="mcp", owner_session_business_id=host.first)
    set_agent_overrides(host.instance, host.other, {"business_config_patch": "allow", "business_config_update": "allow"}, [host.first])
    worker = host.runtime.resolver.invocation_context(Principal(host.other, "agent"), business_id=host.first)
    delegated = host.agent_context

    def settings(ctx=owner):
        return plain(ok(call(host, ctx, "business_config_get", {})))

    def patch(changes, ctx=owner, revision=None):
        return call(host, ctx, "business_config_patch", {"changes": changes, "rationale": "synthetic"}, revision=revision or settings()["revision"])

    def update(config, ctx=owner, revision=None):
        return call(host, ctx, "business_config_update", {"config": config, "rationale": "synthetic"}, revision=revision or settings()["revision"])

    def row():
        return rows(host, "SELECT name,stage,timezone,revision,config_json FROM businesses WHERE id=?", host.first)[0]

    seen["get"] = settings()
    seen["patch"] = [ok(patch({"notes": {"a": 1, "b": 2}}))["config"], ok(patch({"notes": {"a": None}}))["config"]]
    seen["update"] = ok(update({"aliases": ["Alpha"], "tool_policy": {"mode": "ask"}, "name": "Renamed", "timezone": "Asia/Baku"}))
    seen["row"] = {key: row()[key] for key in ("name", "stage", "timezone", "revision")}
    seen["invalid"] = {name: failure(result) for name, result in (
        ("timezone", update({"timezone": "Nowhere/Land"})), ("blank_name", update({"name": " "})),
        ("aliases", update({"aliases": ["x"] * 21})), ("identity", update({"slug": "other"})),
        ("stale", patch({"aliases": ["Beta"]}, revision=1)), ("empty_patch", patch({})))}
    # A patch needs the project's settings authority before anything is merged or validated; a replacement does not.
    seen["authority"] = {"worker_patch": failure(patch({"timezone": "Nowhere/Land"}, worker)),
                         "worker_update": failure(update({"timezone": "Nowhere/Land", "name": "x"}, worker))}
    # An owner-session write cannot delete a protected setting: refused before the setting is normalized back (or the
    # other, invalid, change is looked at); the same patch by the owner outside a session reaches the generic validation.
    seen["protected"] = {"session_delete": failure(patch({"tool_policy": None, "timezone": "Nowhere/Land"}, session)),
                         "owner_delete": failure(patch({"tool_policy": None, "timezone": "Nowhere/Land"})),
                         "session_replacement": ok(update({"aliases": ["Gamma"]}, session))["config"]}
    with Calls(host) as calls:
        failed = patch({"tool_policy": None}, session)
    seen["protected"]["session_calls"] = [calls.log, failure(failed)]
    with Calls(host) as calls:
        ok(patch({"aliases": ["Delta"]}))
        patched = [entry for entry in calls.log]
        calls.log.clear()
        ok(update({"aliases": ["Delta"], "engineering": {"repositories": ["acme/web"]}}, delegated))
    seen["calls"] = {"owner_patch": patched, "delegated_update": calls.log}

    # Revoked delegated authority is refused when the plan is applied, whatever was true when it was prepared.
    args = {"config": {"engineering": {"repositories": ["acme/api"]}}, "rationale": "synthetic"}
    descriptor = registry.get("business_config_update")
    options = InvocationOptions(expected_revision=settings()["revision"])
    plan = read(host, lambda uow: descriptor.prepare(delegated, args, options, uow))
    revoked = dataclasses.replace(delegated, allowed_business_ids=frozenset())
    with host.instance.database.transaction() as uow:
        seen["revoked"] = {"still_delegated": outcome(descriptor.validate_targets, delegated, plan, uow),
                           "revoked": outcome(descriptor.validate_targets, revoked, plan, uow)}
    patch_descriptor = registry.get("business_config_patch")
    patch_plan = read(host, lambda uow: patch_descriptor.prepare(delegated, {"changes": {"aliases": ["Epsilon"]}, "rationale": "synthetic"}, InvocationOptions(
        expected_revision=settings()["revision"]), uow))
    with host.instance.database.transaction() as uow:
        seen["revoked"]["patch"] = [outcome(patch_descriptor.validate_targets, delegated, patch_plan, uow),
                                    outcome(patch_descriptor.validate_targets, revoked, patch_plan, uow)]

    # Browser settings are validated again in the applying transaction, by the host's browser rules, in that transaction's unit.
    browser_args = {"config": {"browser": {"enabled": True, "proxy_server": "http://127.0.0.1:9"}}, "rationale": "synthetic"}
    with Calls(host) as calls:
        browser_plan = read(host, lambda uow: descriptor.prepare(owner, browser_args, InvocationOptions(expected_revision=settings()["revision"]), uow))
        calls.log.clear()
        with host.instance.database.transaction() as uow:
            calls.writer = uow
            descriptor.validate_targets(owner, browser_plan, uow)
    seen["browser"] = calls.log
    return scrub(seen, host, {})



EXPECTED_OWNER_CANDIDATES_PREFIX = [("task", "Plan launch"), ("table", "Pipeline"), ("agent", "Ordubashy"), ("agent", "Colleague"),
                                    ("action", "Closed the books for September"), ("entity", "Docs site"), ("project", "First")]
DOCUMENTS = {"Business note", "Product", "Icp", "Brand", "Positioning"}
FIXED_TYPE = "This source has a fixed material type"
NOT_A_MATERIAL = "Use the item's dedicated tools to classify it"


def test_item_lookups_enumerate_guard_and_pin_as_the_host_did(host):
    seen = observe_records(host)
    candidates = seen["candidates"]
    # Enumeration: the resource tables in the retained order, then the documents, the Project candidate and the
    # human members; the retired mail tables are not candidates in any scope.
    assert candidates[:7] == [list(entry) for entry in EXPECTED_OWNER_CANDIDATES_PREFIX]
    assert {label for _, label in candidates[7:-1]} == DOCUMENTS and candidates[-1] == ["person", "Owner"]
    assert len(candidates) == 13
    assert [label for _, label in seen["material_candidates"]] and len(seen["material_candidates"]) == 5
    assert seen["entity_candidates"] == [["entity", "Docs site"]]
    assert seen["global_candidates"] == [["agent", "Ordubashy"], ["agent", "Colleague"], ["material", "Agent note"]]

    # The owner-only journal and excluded working-memory guards answer from a global or other scope as not found.
    assert seen["journal"] == seen["working_memory"] == ["not_found", "not_found"]
    assert seen["relationship_item"] == [True]
    assert seen["entity"] == {"label": "Docs site", "url": "https://docs.example.test", "revision": 1, "resource_type": "context_entities", "archived": False}

    # Pins: an Action keeps its true storage identity (Global storage is a read-only Global reference); a global agent is
    # pinned with its principal (a read-only ecosystem profile inside a business) and a historical one only with its home.
    assert seen["action_pins"] == {"canonical": [["action_logs", None, "global"]], "global": [["action_logs", None, "global"]],
                                   "legacy": [["action_logs", "first", None]]}
    assert seen["agent_pins"] == {
        "global_in_business": [["agent_profiles", None, "ecosystem"], ["principals", None, None]],
        "global_in_global": [["agent_profiles", None, None], ["principals", None, None]],
        "historical": [["agent_profiles", "first", None]], "archived": [False, True], "historical_elsewhere": "not_found"}
    assert seen["fingerprint_pins"] == [["documents", "first", None], ["managed_document_files", "first", None]]

    # Fingerprints are stable and content-sensitive.
    prints = seen["fingerprints"]
    assert prints["action"] is True and prints["agent"] is True
    assert len({prints[name] for name in ("material", "task", "person", "entity")}) == 4

    # The related-resource resolver: the host's agent reach first, everything else read as before.
    assert seen["related"] == {
        "agent": "Ordubashy", "inactive_agent": "not_found",
        "task": {"label": "Plan launch", "state": "open", "task_type": "task"}, "unknown": "not_found"}


def test_material_archive_and_navigation_keep_their_host_rules(host):
    seen = observe_records(host)
    # The refusal of a historical email-message document and of an agent note sits between the generic one and the revision
    # check (the stale revision never matters); a raw upload's type is fixed; a plain stale revision still conflicts.
    refused = ["error", "workflow_required", NOT_A_MATERIAL]
    assert seen["materials"]["message_document"] == seen["materials"]["agent_note"] == refused
    assert seen["materials"]["upload_type"] == ["error", "validation_error", FIXED_TYPE]
    assert seen["materials"]["stale_revision"] == ["error", "revision_conflict", "Current resource revision is required"]
    assert seen["materials"]["recorded"] == ["draft"]
    with host.instance.database.read() as uow:
        row = uow.repository("documents", business_id=host.first).get(
            next(entry["id"] for entry in uow.connection.execute("SELECT id FROM documents WHERE kind='email_message'")))
        assert classification(row, "documents")          # not the generic refusal: the host's rule is what stopped it

    # Composed archive: the Context entity is archived by its flag, a Task waits for its owning operation's Signoff floor and
    # a table reaches its owning operation.
    assert seen["archive"] == {
        "entity": [True, False], "stale": ["error", "revision_conflict", "Current resource revision is required"],
        "task_floor": ["signoff_required", 0, 1],
        "table": ["error", "operation_unavailable", "Named background program is not installed"]}

    # Navigation: the published reader's count only; unknown is null, a ready empty graph is a real zero.
    navigation = seen["navigation"]
    assert {name: value["counts"]["person"] for name, value in navigation.items()} == {
        "ready": 4, "empty": 0, "not_initialized": None, "unavailable": None, "not_configured": None}
    assert navigation["not_initialized"]["context"] == {"status": "not_initialized", "revision": None, "reason": {"code": "not_published", "message": "No graph"}}
    assert navigation["not_configured"]["context"]["reason"]["code"] == "not_configured"
    assert all(value["counts"]["table"] == 1 and value["navigation_only"] is True for value in navigation.values())
    assert seen["navigation_queries"] == {name: [{"basic_entity_type": ["person"]}] for name in ("ready", "empty", "not_initialized", "unavailable")}


def test_project_settings_keep_their_guards_in_order_and_recheck_at_apply(host):
    seen = observe_settings(host)
    assert seen["get"] == {"config": {"tool_policy": {"mode": "ask"}}, "revision": 1, "environment": {"mode": "host"}}
    assert [entry["notes"] for entry in seen["patch"]] == [{"a": 1, "b": 2}, {"b": 2}]       # nested merge, null removes
    assert seen["update"]["config"] == {"aliases": ["Alpha"], "name": "Renamed", "timezone": "Asia/Baku", "tool_policy": {"mode": "ask"}}
    assert seen["row"] == {"name": "Renamed", "stage": "validating", "timezone": "Asia/Baku", "revision": 4}
    assert seen["invalid"] == {
        "timezone": ["error", "validation_error", "Unknown project timezone"],
        "blank_name": ["error", "validation_error", "Project name, stage, and timezone must be nonempty text"],
        "aliases": ["error", "validation_error", "Project aliases must be a list of at most 20 names of 1-60 characters"],
        "identity": ["error", "validation_error", "Registered identity, owner, and paths are not editable project settings"],
        "stale": ["error", "revision_conflict", "Current resource revision is required"],
        "empty_patch": ["error", "validation_error", "Operation arguments are invalid"]}
    # A patch needs the settings authority before anything is merged or validated; a replacement is not a patch.
    assert seen["authority"] == {"worker_patch": ["error", "forbidden", "Project settings require its owner or scoped Ordubashy"],
                                 "worker_update": ["error", "validation_error", "Unknown project timezone"]}
    # An owner-session patch that deletes a protected setting is refused first, even beside an invalid change; outside a
    # session the same patch reaches the generic validation, and a replacement that omits the setting keeps it.
    gone = ["error", "forbidden", "Owner sessions cannot delete protected project setting: tool_policy"]
    assert seen["protected"]["session_delete"] == gone and seen["protected"]["owner_delete"] == [
        "error", "validation_error", "Unknown project timezone"]
    assert seen["protected"]["session_calls"] == [[["require_settings_authority", {}], ["protect_owner_session_configuration", {
        "owner_session": True, "deleted": ["tool_policy"]}]], gone]
    assert seen["protected"]["session_replacement"] == {"aliases": ["Gamma"], "tool_policy": {"mode": "ask"}}

    # The host guards run in their fixed order, with the actual instance paths, before the generic validation.
    guards = [name for name, _ in seen["calls"]["owner_patch"]][:6]
    assert guards == ["require_settings_authority", "protect_owner_session_configuration", "has_ordubashy_settings_authority",
                      "prepare_authority", "prepare_owner_sessions", "prepare_config"]
    assert [entry for entry in seen["calls"]["owner_patch"] if entry[0] == "prepare_config"][0][1] == {"paths": True, "delegated": False, "uow": True}
    assert [name for name, _ in seen["calls"]["delegated_update"]][:5] == guards[1:]
    # Authority revoked after preparation is refused when the plan is applied (patch and replacement); still held, it passes.
    assert seen["revoked"] == {"still_delegated": None, "revoked": "forbidden", "patch": [None, "forbidden"]}
    # The applying transaction validates the browser section with the host's rules, in that very unit of work.
    assert seen["browser"] == [["protect_owner_session_configuration", {"owner_session": False, "deleted": []}],
                               ["validate_applied", {"paths": True, "in_transaction": True, "uow": True}]]


def test_split_declarations_equal_the_committed_host_declarations():
    from bazarlyk.composition import schema_ddl, tables
    from bazarlyk.operations_history.catalog import catalog
    from qorqut_operations.records import exports as record_exports, schema as record_schema
    from qorqut_operations.relationships import exports as relationship_exports

    assert (*record_schema.SCHEMA, *record_schema.CHANGE_CAPTURE) == tuple(schema_ddl.OWNER_SCHEMA["legacy-context"])
    assert tuple(record_schema.TABLES) == schema_ddl.OWNER_TABLES["legacy-context"]
    assert record_schema.TABLES == {"context_entities": tables.TABLE_SPECS["context_entities"]} and record_schema.IMMUTABLE_FIELDS == {}
    assert tuple(record_schema.storage_contribution().tables) == ("context_entities",)
    owned = {contribution.owner: contribution for contribution in catalog().contributions}["host.relationships"]
    assert tuple(owned.exports) == (*relationship_exports.EDGE_EXPORTS, *record_exports.EXPORTS, *relationship_exports.REVIEW_EXPORTS)
    assert [export.table for export in owned.exports] == ["relationships", "context_entities", "context_reviews", "context_review_states"]


def test_operations_records_and_settings_import_without_the_host_context_or_extensions():
    probe = """
import sys
class Blocked:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"bazarlyk", "qorqut_context", "qorqut_paper", "qorqut_content", "biz_dashboard", "agent_roles"}:
            raise ImportError("blocked: " + name)
sys.meta_path.insert(0, Blocked())
import qorqut_operations.records.lookup, qorqut_operations.records.resources, qorqut_operations.records.adapters
import qorqut_operations.records.compose, qorqut_operations.records.schema, qorqut_operations.records.exports
import qorqut_operations.relationships.exports, qorqut_operations.projects.settings_operations
print(sorted(name for name in sys.modules if name.split(".")[0] in {"bazarlyk", "qorqut_context", "qorqut_paper", "qorqut_content"}))
"""
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stderr


def test_the_host_composes_exactly_one_item_contribution(host):
    from qorqut_operations.records.lookup import RecordCandidates, RecordItems
    from qorqut_operations.records.vocabulary import ITEM_TYPES

    domains = host.runtime.domains
    contributions = domains.items.contributions
    assert [(entry.owner, entry.kinds) for entry in contributions] == [("host-items", ITEM_TYPES)]
    assert isinstance(contributions[0].lookup, RecordItems) and isinstance(domains.items._candidates, RecordCandidates)
    assert domains.relationships.items is domains.items and domains.reviews is domains.relationships.reviews
    assert contributions[0].lookup.related is domains.related and domains.related.host.application is host.runtime.application
