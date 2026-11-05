"""Project notes are ordinary documents: no category, no author restriction, no automatic injection (Manager decision 3031).

Real: a built host instance, the actual published roles and registered UUID agents, the configured Ordubashy, the real
``notes_*`` operations, the real permission policy and the real resolver. Synthetic: the tmp instance and its two projects. No model,
provider, browser or network is used.

Contract under test: ``target_business_id`` selects a project note on create and list and is required, equal to the selected and the
owning project, on every mutation; it is ambiguous beside an agent-note selector. Authorization is the ordinary ``notes_*`` grant plus
the selected-project access check, membership is no grant and a tag grants nothing. ``author_id`` is immutable attribution. Documents
carry ordinary ``tags``; private agent notes and owner journals keep their semantics. Nothing is pinned into agent context."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain

from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context, register_sources,
                                     select_coordinator, set_agent_overrides)

NOTES = ["notes_create", "notes_update", "notes_delete", "notes_list", "notes_get"]
SENTINEL = "PINNED-PROJECT-DIRECTION-SENTINEL"


@pytest.fixture
def team(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    register_sources(instance, ids["first"], "first")
    register_sources(instance, ids["second"], "second")
    agents = {"ordubashy": select_coordinator(instance, create_registered_global(instance, "ordubashy")),
              "worker": create_worker(instance, "worker-a"), "other": create_worker(instance, "worker-b"),
              "plain": create_worker(instance, "worker-c")}
    instance.runtime.freeze_registry()
    try:
        yield Team(instance, ids, agents)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


class Team:
    def __init__(self, instance, ids, agents):
        self.instance, self.ids, self.agents = instance, ids, agents
        self.first, self.second = ids["first"], ids["second"]
        for who in ("ordubashy", "worker", "other"):                       # "plain" is granted nothing
            self.grant(who, NOTES)

    def grant(self, who, operations):
        set_agent_overrides(self.instance, self.agents[who], {name: "allow" for name in operations}, list(self.ids.values()))

    def agent(self, who, business=None):
        return self.instance.runtime.resolver.invocation_context(Principal(self.agents[who], "agent"), business_id=business or self.first)

    def owner(self, business=None):
        return self.instance.human(business or self.first)

    def call(self, ctx, name, arguments, revision=None):
        return self.instance.runtime.application.invoke(
            ctx, name, arguments, InvocationOptions(idempotency_key=new_id(), expected_revision=revision))

    def create(self, ctx, title="Direction", business=None, **arguments):
        return self.call(ctx, "notes_create", {"title": title, "body": f"{title} body.\n", "target_business_id": business or self.first,
                                               **arguments})

    def query(self, sql, *parameters):
        with self.instance.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def row(self, document_id):
        return self.query("SELECT * FROM documents WHERE id=?", document_id)[0]


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def refusal(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"], result.error.get("message", "")


# ----- an ordinary project document: creation, path, metadata, tags -----------------------------------------------------


def test_the_owner_and_a_granted_agent_create_ordinary_project_notes(team):
    owned = ok(team.create(team.owner(), "Pricing direction"))["document"]
    assert owned["kind"] == "business_owned_note" and owned["business_id"] == team.first
    assert owned["metadata"]["author_id"] == team.instance.owner and "category" not in owned["metadata"]
    # New files are plain dated notes; there is no category directory.
    assert owned["path"].startswith("notes/") and owned["path"].count("/") == 1 and owned["path"].endswith("-pricing-direction.md")
    granted = ok(team.create(team.agent("worker"), "Launch plan", tags=["launch", "plan"], pinned=True))["document"]
    assert granted["metadata"]["author_id"] == team.agents["worker"] and granted["metadata"]["tags"] == ["launch", "plan"]
    assert granted["metadata"]["pinned"] is True and granted["path"].startswith("notes/") and granted["path"].count("/") == 1
    # The configured Ordubashy writes with the same ordinary grant and no special authority.
    assert ok(team.create(team.agent("ordubashy"), "Roadmap", business=team.first))["document"]["business_id"] == team.first
    stored = team.query("SELECT metadata_json FROM documents WHERE kind='business_owned_note'")
    assert len(stored) == 3 and all("category" not in json.loads(row["metadata_json"]) for row in stored)


def test_removed_category_and_strategy_authoring_inputs_are_not_accepted(team):
    for extra in ({"category": "strategy-notes"}, {"category": "marketing-notes"}, {"category": "engineering-decisions"}):
        assert refusal(team.create(team.owner(), "Typed", **extra))[0] == "validation_error", extra
    assert refusal(team.call(team.owner(), "notes_list", {"target_business_id": team.first, "category": "strategy-notes"}))[0] \
        == "validation_error"
    assert team.query("SELECT count(*) AS n FROM documents WHERE kind='business_owned_note'")[0]["n"] == 0


def test_tags_label_project_notes_and_filter_the_list(team):
    one = ok(team.create(team.owner(), "One", tags=["launch"]))["document"]
    two = ok(team.create(team.owner(), "Two", tags=["pricing", "launch"]))["document"]
    three = ok(team.create(team.owner(), "Three"))["document"]
    listed = lambda **arguments: {item["id"] for item in ok(team.call(team.agent("worker"), "notes_list", {
        "target_business_id": team.first, **arguments}))["items"]}
    assert listed() == {one["id"], two["id"], three["id"]}
    assert listed(tag="launch") == {one["id"], two["id"]} and listed(tag="pricing") == {two["id"]} and listed(tag="absent") == set()
    # A tag is ordinary metadata that grants nothing: the ungranted worker is refused whatever a note is tagged.
    assert refusal(team.call(team.agent("plain"), "notes_list", {"target_business_id": team.first, "tag": "launch"}))[0] \
        == "policy_denied"
    edited = ok(team.call(team.owner(), "notes_update", {"document_id": three["id"], "target_business_id": team.first,
                                                         "metadata": {"tags": ["launch"]}}, three["revision"]))["document"]
    assert edited["metadata"]["tags"] == ["launch"] and listed(tag="launch") == {one["id"], two["id"], three["id"]}


def test_listing_project_notes_requires_the_explicit_target_even_for_the_owner(team):
    ours = ok(team.create(team.owner(), "Ours"))["document"]
    other = ok(team.create(team.owner(team.second), "Theirs", business=team.second))["document"]
    assert refusal(team.call(team.owner(), "notes_list", {}))[0] == "validation_error"
    assert {item["id"] for item in ok(team.call(team.owner(), "notes_list", {"target_business_id": team.first}))["items"]} == {ours["id"]}
    assert {item["id"] for item in ok(team.call(team.owner(team.second), "notes_list", {"target_business_id": team.second}))["items"]} \
        == {other["id"]}


def refused_over_http(method, *arguments, **query):
    """True when the route answers with an error status or the typed domain error the HTTP layer would turn into one."""
    try:
        return method(*arguments, **query)[0] >= 400
    except DomainError:
        return True


def test_the_dashboard_http_adapter_carries_the_project_uuid_distinct_from_the_slug(team):
    from types import SimpleNamespace
    from context_source_status_support import Routes

    routes = Routes(SimpleNamespace(runtime=team.instance.runtime, background=None))
    owner, uuid = team.owner(), team.first
    assert uuid != "first"                                      # the route names the Project by slug, the payload by UUID
    # create: a matching UUID is accepted; a missing, mismatched or slug target is refused and nothing is written
    status, created = routes.post(owner, "biz/first/notes", {"title": "Direction", "content": "Direction.\n", "target_business_id": uuid})
    assert status == 200 and created["title"] == "Direction"
    stored = team.row(created["id"])
    assert stored["kind"] == "business_owned_note" and stored["business_id"] == uuid
    for target in (None, team.second, "first"):
        body = {"title": "Refused", "content": "x\n", **({} if target is None else {"target_business_id": target})}
        assert refused_over_http(routes.post, owner, "biz/first/notes", body), target
    assert team.query("SELECT count(*) AS n FROM documents WHERE kind='business_owned_note'")[0]["n"] == 1
    # list
    status, listed = routes.get(owner, "biz/first/notes", target_business_id=uuid)
    assert status == 200 and [item["id"] for item in listed] == [created["id"]]
    for query in ({}, {"target_business_id": team.second}, {"target_business_id": "first"}):
        assert refused_over_http(routes.get, owner, "biz/first/notes", **query), query
    # edit and archive use the same captured target against the selected Project and the stored owner
    path = "biz/first/notes/" + created["id"]
    for target in (None, team.second, "first"):
        body = {"content": "Changed.\n", **({} if target is None else {"target_business_id": target})}
        assert refused_over_http(routes.post, owner, path, body), target
        assert refused_over_http(routes.post, owner, path + "/delete", {} if target is None else {"target_business_id": target}), target
    assert team.row(created["id"])["revision"] == stored["revision"] and not team.row(created["id"])["archived"]
    status, edited = routes.post(owner, path, {"content": "Changed.\n", "target_business_id": uuid})
    assert status == 200 and edited["content"].strip() == "Changed."
    assert routes.post(owner, path + "/delete", {"target_business_id": uuid})[0] == 200 and team.row(created["id"])["archived"]
    # a Project requested through the other Project's route is the mismatch, however valid the UUID
    assert refused_over_http(routes.get, team.owner(team.second), "biz/second/notes", target_business_id=uuid)
    # the owner's root journal takes no Project target and keeps working without one
    root = replace(team.owner(), business_id=None)
    status, journal = routes.post(root, "root/notes", {"title": "Daily", "content": "Today.\n"})
    assert status == 200 and team.row(journal["id"])["business_id"] is None and team.row(journal["id"])["kind"] != "business_owned_note"
    assert routes.get(root, "root/notes")[0] == 200


# ----- authorization: the ordinary grant plus the selected project, never a membership, tag or author rule -----------------


def test_a_project_note_needs_the_ordinary_grant_and_membership_alone_grants_nothing(team):
    assert refusal(team.create(team.agent("plain")))[0] == "policy_denied"
    with team.instance.database.transaction() as uow:
        uow.repository("business_memberships", business_id=team.first).insert({"principal_id": team.agents["plain"], "is_owner": False})
    assert refusal(team.create(team.agent("plain")))[0] == "policy_denied"                  # membership is no grant
    note = ok(team.create(team.owner(), "Existing"))["document"]
    for name, arguments in (("notes_update", {"body": "x\n"}), ("notes_delete", {})):
        denied = team.call(team.agent("plain"), name, {"document_id": note["id"], "target_business_id": team.first, **arguments},
                           note["revision"])
        assert refusal(denied)[0] == "policy_denied", name
    assert refusal(team.call(team.agent("plain"), "notes_get", {"document_id": note["id"]}))[0] == "policy_denied"
    assert team.row(note["id"])["revision"] == note["revision"] and not team.row(note["id"])["archived"]
    team.grant("plain", ["notes_create"])                                                # the ordinary grant is exactly what admits
    assert ok(team.create(team.agent("plain"), "Now allowed"))["document"]["metadata"]["author_id"] == team.agents["plain"]


def test_a_project_note_never_crosses_the_selected_project(team):
    note = ok(team.create(team.owner(), "First only"))["document"]
    # A target that is not the selected project is refused on create, list and every mutation.
    assert refusal(team.create(team.agent("worker", team.first), business=team.second))[0] == "forbidden"
    assert refusal(team.call(team.agent("worker", team.first), "notes_list", {"target_business_id": team.second}))[0] == "forbidden"
    for name, arguments in (("notes_update", {"body": "x\n"}), ("notes_delete", {})):
        crossing = team.call(team.agent("worker", team.second), name, {"document_id": note["id"], "target_business_id": team.second,
                                                                       **arguments}, note["revision"])
        assert crossing.status == "error" and refusal(crossing)[0] in {"forbidden", "not_found"}, name
        misdirected = team.call(team.agent("worker", team.second), name, {"document_id": note["id"], "target_business_id": team.first,
                                                                          **arguments}, note["revision"])
        assert refusal(misdirected)[0] in {"forbidden", "not_found"}, name
    assert refusal(team.call(team.agent("worker", team.second), "notes_get", {"document_id": note["id"]}))[0] in {"forbidden", "not_found"}
    # A principal whose reach excludes the project cannot write to it even when its selection names it.
    narrowed = replace(team.owner(team.first), allowed_business_ids=frozenset({team.second}))
    assert team.create(narrowed).status == "error"
    assert team.call(narrowed, "notes_update", {"document_id": note["id"], "target_business_id": team.first, "body": "x\n"},
                     note["revision"]).status == "error"
    unchanged = team.row(note["id"])
    assert unchanged["revision"] == note["revision"] and not unchanged["archived"]
    assert team.query("SELECT count(*) AS n FROM documents WHERE kind='business_owned_note'")[0]["n"] == 1


def test_every_mutation_path_names_the_project(team):
    note = ok(team.create(team.agent("worker"), "Named"))["document"]
    for name, arguments in (("notes_update", {"body": "x\n"}), ("notes_delete", {})):
        missing = team.call(team.agent("other"), name, {"document_id": note["id"], **arguments}, note["revision"])
        assert refusal(missing)[0] == "validation_error", name
    assert refusal(team.call(team.agent("other"), "notes_create", {"title": "x", "body": "x\n", "target_business_id": None}))[0] \
        == "validation_error"
    assert team.row(note["id"])["revision"] == note["revision"]


def test_the_target_is_the_project_uuid_never_its_slug(team):
    note = ok(team.create(team.agent("worker"), "Named"))["document"]
    assert note["business_id"] == team.first and team.first != "first"
    # The URL slug of the selected project is not a target: it is refused, never remapped to the selected project.
    assert refusal(team.create(team.agent("worker"), business="first"))[0] == "validation_error"
    assert refusal(team.call(team.agent("worker"), "notes_list", {"target_business_id": "first"}))[0] == "validation_error"
    stale = team.call(team.agent("worker"), "notes_update",
                      {"document_id": note["id"], "target_business_id": "first", "body": "x\n"}, note["revision"])
    assert refusal(stale)[0] == "validation_error"
    assert team.row(note["id"])["revision"] == note["revision"]
    assert team.query("SELECT count(*) AS n FROM documents WHERE kind='business_owned_note'")[0]["n"] == 1


def test_any_granted_agent_edits_and_archives_and_attribution_stays_with_the_author(team):
    created = ok(team.create(team.agent("worker"), "Shared direction", tags=["plan"]))["document"]
    author, path = team.agents["worker"], created["path"]
    # No author-only rule: another granted agent edits, then the owner edits, and the author is still the first writer.
    edited = ok(team.call(team.agent("other"), "notes_update", {"document_id": created["id"], "target_business_id": team.first,
                                                                "body": "Revised by another.\n"}, created["revision"]))["document"]
    assert edited["metadata"]["author_id"] == author and edited["path"] == path and edited["revision"] == created["revision"] + 1
    by_owner = ok(team.call(team.owner(), "notes_update", {"document_id": created["id"], "target_business_id": team.first,
                                                           "metadata": {"pinned": True}}, edited["revision"]))["document"]
    assert by_owner["metadata"]["author_id"] == author and by_owner["metadata"]["pinned"] is True
    stored = team.row(created["id"])
    assert json.loads(stored["metadata_json"])["author_id"] == author and stored["path"] == path and stored["id"] == created["id"]
    # An edit that tries to rewrite the attribution (or the owning project) changes neither.
    attempt = team.call(team.agent("other"), "notes_update", {
        "document_id": created["id"], "target_business_id": team.first, "metadata": {"author_id": team.agents["other"]}},
        by_owner["revision"])
    if attempt.status == "ok":
        assert plain(attempt.data)["document"]["metadata"]["author_id"] == author
    assert json.loads(team.row(created["id"])["metadata_json"])["author_id"] == author
    assert team.row(created["id"])["business_id"] == team.first
    archived = ok(team.call(team.agent("other"), "notes_delete", {"document_id": created["id"], "target_business_id": team.first},
                            team.row(created["id"])["revision"]))
    assert archived and team.row(created["id"])["archived"]
    assert json.loads(team.row(created["id"])["metadata_json"])["author_id"] == author


def test_revision_and_hash_conflicts_still_refuse_a_stale_project_note_edit(team):
    created = ok(team.create(team.agent("worker"), "Contended"))["document"]
    newer = ok(team.call(team.agent("other"), "notes_update", {"document_id": created["id"], "target_business_id": team.first,
                                                               "body": "Newer.\n"}, created["revision"]))["document"]
    for name, arguments in (("notes_update", {"body": "Stale.\n"}), ("notes_delete", {})):
        stale = team.call(team.agent("worker"), name, {"document_id": created["id"], "target_business_id": team.first, **arguments},
                          created["revision"])
        assert refusal(stale)[0] == "revision_conflict", name
    wrong_hash = team.call(team.agent("worker"), "notes_update", {
        "document_id": created["id"], "target_business_id": team.first, "body": "Hash.\n", "expected_content_hash": "0" * 64},
        newer["revision"])
    assert refusal(wrong_hash)[0] == "revision_conflict"
    current = team.row(created["id"])
    assert current["revision"] == newer["revision"] and current["content_hash"] == newer["content_hash"]
    good = team.call(team.agent("worker"), "notes_update", {"document_id": created["id"], "target_business_id": team.first,
                                                            "body": "Fresh.\n", "expected_content_hash": newer["content_hash"]},
                     newer["revision"])
    assert good.status == "ok", good.error


# ----- ambiguous selectors ------------------------------------------------------------------------------------------------


def test_a_project_target_beside_an_agent_note_selector_is_ambiguous_and_rejected(team):
    worker = team.agents["worker"]
    assert refusal(team.create(team.agent("worker"), target_agent_id=worker))[0] == "validation_error"
    assert refusal(team.call(team.agent("worker"), "notes_list", {"target_business_id": team.first, "agent_id": worker}))[0] \
        == "validation_error"
    private = ok(team.call(team.agent("worker"), "notes_create", {"title": "Mine", "body": "private\n"}))["document"]
    assert private["kind"] == "agent_note"
    crossing = team.call(team.agent("worker"), "notes_update", {"document_id": private["id"], "target_business_id": team.first,
                                                                "body": "x\n"}, private["revision"])
    assert crossing.status == "error"                                                    # a project target never reaches a private note
    assert team.row(private["id"])["revision"] == private["revision"]
    project = ok(team.create(team.agent("worker"), "Project"))["document"]
    assert team.call(team.agent("worker"), "notes_update", {"document_id": project["id"], "body": "x\n"}, project["revision"]).status == "error"
    assert team.query("SELECT count(*) AS n FROM documents WHERE kind='business_owned_note'")[0]["n"] == 1


# ----- unchanged behavior and no injection --------------------------------------------------------------------------------


def test_private_agent_notes_and_owner_journals_keep_their_semantics(team):
    worker = team.agent("worker")
    private = ok(team.call(worker, "notes_create", {"title": "Scratch", "body": "mine\n", "tags": ["scratch"]}))["document"]
    assert private["kind"] == "agent_note" and "author_id" not in private["metadata"] and private["metadata"]["tags"] == ["scratch"]
    project = ok(team.create(team.agent("other"), "Shared"))["document"]
    own = {item["id"] for item in ok(team.call(worker, "notes_list", {}))["items"]}
    assert private["id"] in own and project["id"] not in own                              # no target: the agent's own private notes
    assert {item["id"] for item in ok(team.call(worker, "notes_list", {"tag": "scratch"}))["items"]} == {private["id"]}
    assert {item["id"] for item in ok(team.call(worker, "notes_list", {"target_business_id": team.first}))["items"]} == {project["id"]}
    # A private note is writable only by its own agent (global notes stay readable under the unchanged shared-context grant), and
    # the project listing never carries it.
    assert refusal(team.call(team.agent("other"), "notes_update", {"document_id": private["id"], "body": "theirs\n"},
                             private["revision"]))[0] == "forbidden"
    assert refusal(team.call(team.agent("other"), "notes_delete", {"document_id": private["id"]}, private["revision"]))[0] == "forbidden"
    # The owner's Global journal keeps its owner-only semantics beside the project notes.
    journal_ctx = owner_global_context(team.instance)
    entry = ok(team.call(journal_ctx, "notes_create", {"title": "Journal", "body": "owner journal\n"}))["document"]
    assert entry["kind"] in {"global_note", "journal"} and entry["business_id"] is None
    assert {item["id"] for item in ok(team.call(journal_ctx, "notes_list", {}))["items"]} == {entry["id"]}
    assert refusal(team.call(team.agent("worker", None), "notes_get", {"document_id": entry["id"]}))[0] in {"forbidden", "not_found"}
    assert refusal(team.call(journal_ctx, "notes_create", {"title": "Mixed", "body": "x\n", "target_business_id": team.first}))[0] \
        in {"validation_error", "forbidden", "scope_required"}


def test_a_pinned_project_note_is_never_injected_into_agent_context(team):
    pinned = ok(team.create(team.owner(), "Strategy", pinned=True, tags=["strategy"]))["document"]
    body = f"{SENTINEL} strategy narrative.\n"
    ok(team.call(team.owner(), "notes_update", {"document_id": pinned["id"], "target_business_id": team.first, "body": body},
                 pinned["revision"]))
    assert team.row(pinned["id"])["metadata_json"]
    resolver = team.instance.runtime.resolver
    for who in ("ordubashy", "worker"):
        config = resolver.resolve_agent_config(Principal(team.agents[who], "agent"), business_id=team.first).to_dict()
        rendered = json.dumps(config)
        # Neither the text, the identity, the path nor the hash of the pinned note reaches the delivered context.
        assert SENTINEL not in rendered and "strategy narrative" not in rendered
        assert pinned["id"] not in rendered and pinned["path"] not in rendered and team.row(pinned["id"])["content_hash"] not in rendered
        assert "Business Strategy" not in rendered and "owner-controlled business strategy" not in rendered
    # The pinned flag is ordinary metadata: the note is still listed and read under the ordinary grant, nothing more.
    assert [item["id"] for item in ok(team.call(team.agent("worker"), "notes_list", {"target_business_id": team.first}))["items"]] \
        == [pinned["id"]]


# ----- the explicit conversion of legacy categories into ordinary tags -----------------------------------------------------


def legacy_shape(team, document_id, category, tags=None):
    """Rewrite a created note into its stored pre-v1.3 shape: a ``category`` in the frontmatter and the registered metadata."""
    from qorqut_operations.documents.serialization import document_bytes, split_document
    store = team.instance.runtime.domains.documents.store
    with team.instance.database.transaction() as uow:
        row = uow.repository("documents", business_id=team.first).get(document_id)
        target = store._file_target(store.file_ref(row["id"], row["path"], row["business_id"]), uow).resolve()
        metadata, body = split_document(target.read_bytes())
        metadata = {**metadata, "category": category, **({"tags": tags} if tags is not None else {})}
        data = document_bytes(metadata, body)
        target.write_bytes(data)
        uow.connection.execute("UPDATE documents SET metadata_json=?, content_hash=? WHERE id=?",
                               (json.dumps({**row["metadata_json"], **metadata}), hashlib.sha256(data).hexdigest(), document_id))
    return hashlib.sha256(data).hexdigest()


def test_legacy_categories_convert_to_tags_only_by_the_explicit_isolated_tool(team):
    from bazarlyk.migration.project_note_tags import migrate
    runtime = team.instance.runtime
    strategy = ok(team.create(team.agent("worker"), "Positioning"))["document"]
    marketing = ok(team.create(team.owner(), "Campaign", tags=["launch"]))["document"]
    tagged = ok(team.create(team.owner(), "Already"))["document"]
    plain_note = ok(team.create(team.owner(), "Plain", tags=["x"]))["document"]
    hashes = {strategy["id"]: legacy_shape(team, strategy["id"], "strategy-notes"),
              marketing["id"]: legacy_shape(team, marketing["id"], "marketing-notes", ["launch"]),
              tagged["id"]: legacy_shape(team, tagged["id"], "strategy-notes", ["strategy-notes"])}
    before = {item["id"]: team.row(item["id"]) for item in (strategy, marketing, tagged, plain_note)}
    # Nothing converts at run time: a read leaves the legacy label uninterpreted and untagged.
    assert {item["id"] for item in ok(team.call(team.agent("worker"), "notes_list", {"target_business_id": team.first,
            "tag": "strategy-notes"}))["items"]} == {tagged["id"]}

    inspected = migrate(runtime)
    assert inspected["apply"] is False and inspected["notes"] == 2 and not inspected["converted"]
    assert [item["reason"] for item in inspected["skipped"]] == ["already tagged"]
    assert {item["document_id"]: item["tags"] for item in inspected["pending"]} == {
        strategy["id"]: ["strategy-notes"], marketing["id"]: ["launch", "marketing-notes"]}
    assert all(team.row(key) == value for key, value in before.items())                  # a dry run changes nothing

    applied = migrate(runtime, apply=True)
    assert applied["notes"] == 2 and len(applied["converted"]) == 2 and Path(applied["backup"]).is_dir()
    by_id = {item["document_id"]: item for item in applied["converted"]}
    for note, label, tags in ((strategy, "strategy-notes", ["strategy-notes"]), (marketing, "marketing-notes", ["launch", "marketing-notes"])):
        after, report = team.row(note["id"]), by_id[note["id"]]
        stored = json.loads(after["metadata_json"])
        assert (after["id"], after["path"], after["business_id"], after["kind"]) == (
            before[note["id"]]["id"], before[note["id"]]["path"], before[note["id"]]["business_id"], "business_owned_note")
        assert stored["tags"] == tags and stored["author_id"] == json.loads(before[note["id"]]["metadata_json"])["author_id"]
        assert stored["category"] == label                                              # uninterpreted legacy metadata
        view = ok(team.call(team.owner(), "notes_get", {"document_id": note["id"]}))["document"]
        assert view["body"] == f"{note['metadata']['title']} body.\n"
        # The frontmatter changed, so the hash was recomputed honestly from the real bytes and is reported before and after.
        assert report["content_hash_before"] == hashes[note["id"]] and report["content_hash_after"] == after["content_hash"]
        assert report["content_hash_before"] != report["content_hash_after"] and after["revision"] == before[note["id"]]["revision"] + 1
        assert (Path(applied["backup"]) / (note["id"] + ".md")).is_file()
    # Notes that needed no conversion keep their exact bytes, hash and revision.
    for untouched in (tagged, plain_note):
        assert team.row(untouched["id"]) == before[untouched["id"]]
    assert {item["id"] for item in ok(team.call(team.agent("worker"), "notes_list", {"target_business_id": team.first,
            "tag": "strategy-notes"}))["items"]} == {strategy["id"], tagged["id"]}
    again = migrate(runtime, apply=True)
    assert again["notes"] == 0 and not again["converted"] and "backup" not in again
