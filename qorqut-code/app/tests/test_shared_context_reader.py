"""P4A: the shared published Context reader over synthetic Context repositories (no host, model or operational data).

Every repository is written with the pinned engine's own API under a pytest ``tmp_path``. The cases cover the explicit
not-initialized/empty/unavailable states, one published revision per response, cursors that keep their revision across a
later publication, absence of draft data, bounded pages and the shapes of node, edge and source reads."""

import base64
import json
import subprocess

import pytest
from jsonschema import Draft202012Validator
from qorqut_context import Context
from qorqut_operations.contracts import DomainError

import shared_context_support as support
from qorqut_context.integration.reading import SharedContextReader, schemas
from qorqut_context.integration.reading import cursor as cursors
from qorqut_context.integration.reading import reader as reader_module
from instance_support import synthetic_paths

NODES = {support.ALICE, support.ACME, support.ATLAS, support.BOBBY}
EDGES = {support.LEADS, support.WORKS, support.MANAGES}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    """One published synthetic graph shared by the read-only cases (cases that publish or draft build their own)."""
    return support.published_graph(tmp_path_factory.mktemp("shared-graph"))


@pytest.fixture(scope="module")
def reader(graph):
    shared = SharedContextReader(graph.repository, graph.runtime)
    yield shared
    shared.close()


def conforms(schema, result):
    errors = list(Draft202012Validator(schema).iter_errors(result))
    assert not errors, [(list(error.absolute_path), error.message[:160]) for error in errors[:3]]
    return result


def fingerprint(*roots):
    """Every file below the roots with its size and modification time, to prove a read wrote nothing."""
    return sorted((str(path), path.stat().st_size, path.stat().st_mtime_ns)
                  for root in roots if root.exists() for path in root.rglob("*") if path.is_file())


def drain(call, key, **arguments):
    """All items of a paged read, with the revisions its pages reported."""
    items, revisions, cursor = [], set(), None
    while True:
        result = call(**arguments, cursor=cursor)
        assert result["status"] == "ready"
        items += result[key]
        revisions.add(result["revision"])
        cursor = result["page"]["next_cursor"]
        if cursor is None:
            return items, revisions


def forged(operation, scope, revision, offset):
    return cursors.encode(operation, scope, revision, offset)


# ---- readiness: missing, empty, uninitialized and empty-initialized are different answers

def test_a_missing_repository_is_not_initialized_and_nothing_is_created(tmp_path):
    shared = SharedContextReader(tmp_path / "context", tmp_path / "runtime")
    expected = {"status": "not_initialized", "revision": None,
                "reason": {"code": "repository_missing", "message": "The shared Context has not been initialized"}}
    assert shared.status() == expected
    for result in (shared.home(), shared.find("anything"), shared.changes()):
        assert conforms(schemas.HOME_OUTPUT, result) == expected
    assert not (tmp_path / "context").exists() and not (tmp_path / "runtime").exists()


def test_an_empty_directory_and_a_bare_git_repository_are_not_initialized_and_stay_untouched(tmp_path):
    repository, runtime = tmp_path / "context", tmp_path / "runtime"
    repository.mkdir()
    shared = SharedContextReader(repository, runtime)
    assert shared.home()["reason"]["code"] == "repository_empty" and shared.home()["status"] == "not_initialized"
    assert list(repository.iterdir()) == []
    subprocess.run(["git", "-C", str(repository), "init", "-q", "-b", "main"], check=True)
    before = fingerprint(repository)
    result = shared.home()
    assert result["status"] == "not_initialized" and result["reason"]["code"] == "no_published_revision"
    assert shared.object("alice")["status"] == "not_initialized"
    assert fingerprint(repository) == before and not runtime.exists()


def test_an_initialized_empty_graph_is_explicitly_empty_and_differs_from_a_missing_repository(tmp_path):
    Context.initialize(tmp_path / "context", tmp_path / "runtime", basic_entity_types=["person"])
    shared = SharedContextReader(tmp_path / "context", tmp_path / "runtime")
    home = conforms(schemas.HOME_OUTPUT, shared.home())
    assert home["status"] == "ready" and home["empty"] is True and home["nodes"] == []
    assert home["totals"] == {"nodes": 0, "edges": 0, "sources": 0, "events": 0}
    assert home["page"] == {"limit": 30, "returned": 0, "has_more": False, "next_cursor": None}
    assert len(home["revision"]) == 40
    assert conforms(schemas.FIND_OUTPUT, shared.find("anyone"))["items"] == []
    missing = SharedContextReader(tmp_path / "absent", tmp_path / "absent-runtime").home()
    assert missing["status"] == "not_initialized" and "empty" not in missing


def test_unusable_locations_are_unavailable_not_initialized_and_not_repaired(tmp_path, graph):
    plain = tmp_path / "plain"
    plain.write_text("not a repository")
    assert SharedContextReader(plain, tmp_path / "r1").home()["reason"]["code"] == "not_a_context_repository"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_text("files but no repository")
    result = SharedContextReader(unrelated, tmp_path / "r2").home()
    assert result["status"] == "unavailable" and result["reason"]["code"] == "not_a_context_repository"
    link = tmp_path / "link"
    link.symlink_to(graph.repository)
    result = SharedContextReader(link, graph.runtime).home()
    assert result["status"] == "unavailable" and result["reason"]["code"] == "path_not_trusted"
    assert conforms(schemas.HOME_OUTPUT, result)
    other = tmp_path / "other-runtime"
    result = SharedContextReader(graph.repository, other).home()
    assert result["status"] == "unavailable" and result["reason"]["code"] == "runtime_binding_mismatch"
    assert not other.exists()


def test_locations_must_be_separate_absolute_paths(tmp_path):
    for repository, runtime in (("relative", tmp_path), (tmp_path, "relative"), (tmp_path / "a", tmp_path / "a"),
                                (tmp_path / "a", tmp_path / "a" / "b"), (tmp_path / "a" / "b", tmp_path / "a"),
                                (tmp_path / ".." / "x", tmp_path / "y"), (None, tmp_path)):
        with pytest.raises(ValueError):
            SharedContextReader(repository, runtime)


def test_the_reader_reads_the_explicit_instance_context_locations(tmp_path):
    paths = synthetic_paths(tmp_path / "instance")
    built = support.Synthetic(paths.context, paths.context_runtime).initialize().populate()
    shared = SharedContextReader(paths.context, paths.context_runtime)
    assert shared.status()["revision"] == built.commits[support.ACTION_EVENT]
    assert shared.home()["totals"]["nodes"] == len(NODES)


# ---- home

def test_home_is_a_bounded_pinned_page_of_node_summaries_attributes_and_relationships(reader, graph):
    first = conforms(schemas.HOME_OUTPUT, reader.home(limit=2))
    assert first["status"] == "ready" and first["empty"] is False and first["revision"] == graph.commits[support.ACTION_EVENT]
    assert first["totals"] == {"nodes": 4, "edges": 3, "sources": 3, "events": 4}
    assert [item["id"] for item in first["nodes"]][0] == support.ALICE  # most connected first
    assert first["page"]["returned"] == 2 and first["page"]["has_more"] is True
    alice = first["nodes"][0]
    assert alice["summary"] == "Alice Rivera" and alice["attributes"]["role"] == ["lead", "founder"]
    assert alice["relationship_count"] == 3 and len(alice["relationships"]) == 3
    assert {item["id"] for item in alice["relationships"]} == EDGES
    by_edge = {item["id"]: item for item in alice["relationships"]}
    assert by_edge[support.LEADS]["direction"] == "out" and by_edge[support.MANAGES]["direction"] == "in"
    assert by_edge[support.LEADS]["to"] == {"kind": "node", "id": support.ATLAS, "ref": "node:atlas",
                                            "summary": "Atlas program (April)", "truncated": False}
    items, revisions = drain(reader.home, "nodes", limit=1)
    assert [item["id"] for item in items][0] == support.ALICE
    assert {item["id"] for item in items} == NODES and len(items) == len(NODES)
    assert revisions == {first["revision"]}


def test_home_defaults_and_bounds(reader):
    assert reader.home()["page"]["limit"] == 30
    for bad in (0, 61, -1, True, "5", 1.5):
        with pytest.raises(DomainError) as error:
            reader.home(limit=bad)
        assert error.value.code == "validation_error" and "limit" in error.value.field_errors


# ---- pinning across publication

def test_cursors_keep_their_revision_across_a_later_publication(tmp_path):
    graph = support.published_graph(tmp_path, draft=True)
    shared = SharedContextReader(graph.repository, graph.runtime)
    before = shared.home(limit=2)
    found = shared.find("", limit=2)
    expand = shared.expand(support.ALICE, limit=1)
    obj = shared.object(support.ATLAS, history_limit=1)
    published = graph.publish_draft()
    assert published != before["revision"] and shared.status()["revision"] == published

    later = shared.home(limit=2, cursor=before["page"]["next_cursor"])
    assert later["revision"] == before["revision"]
    drained, revisions = [], set()
    cursor = before["page"]["next_cursor"]
    while cursor:
        page = shared.home(limit=2, cursor=cursor)
        drained += [item["id"] for item in page["nodes"]]
        revisions.add(page["revision"])
        cursor = page["page"]["next_cursor"]
    assert revisions == {before["revision"]} and "drft1" not in drained
    pinned_find = shared.find("", limit=2, cursor=found["page"]["next_cursor"])
    assert pinned_find["revision"] == before["revision"]
    assert "drft1" not in {item["id"] for item in pinned_find["items"]}
    assert shared.expand(support.ALICE, limit=1, cursor=expand["page"]["next_cursor"])["revision"] == before["revision"]
    older = shared.object(support.ATLAS, history_limit=1, history_cursor=obj["history"]["page"]["next_cursor"])
    assert older["revision"] == before["revision"] and older["history"]["total"] == obj["history"]["total"]

    fresh = shared.home(limit=60)
    assert fresh["revision"] == published and "drft1" in {item["id"] for item in fresh["nodes"]}
    assert fresh["totals"]["nodes"] == 5
    assert shared.home(limit=60, cursor=None)["revision"] == published


def test_an_explicit_revision_reads_that_published_state(reader, graph):
    old = graph.commits[support.FIRST_EVENT]
    home = reader.home(revision=old)
    assert home["revision"] == old and home["totals"]["nodes"] == 3 and support.BOBBY not in \
        {item["id"] for item in home["nodes"]}
    with pytest.raises(DomainError) as error:
        reader.home(revision=old[:10])
    assert error.value.code == "validation_error"
    with pytest.raises(DomainError):
        reader.home(revision="0" * 40)


# ---- cursor integrity

def test_a_cursor_is_bound_to_its_operation_arguments_and_revision(reader, graph):
    page = reader.find("atlas", limit=1)
    assert page["page"]["next_cursor"]
    token = page["page"]["next_cursor"]
    invalid = [
        ("find", dict(query="atlas", kind="node", limit=1, cursor=token)),
        ("find", dict(query="alice", limit=1, cursor=token)),
        ("find", dict(query="atlas", limit=1, cursor=token + "x")),
        ("find", dict(query="atlas", limit=1, cursor="not-a-cursor")),
        ("find", dict(query="atlas", limit=1, cursor="")),
        ("home", dict(limit=1, cursor=token)),
        ("find", dict(query="atlas", limit=1, cursor=token, revision=graph.commits[support.FIRST_EVENT])),
    ]
    for name, arguments in invalid:
        with pytest.raises(DomainError) as error:
            getattr(reader, name)(**arguments)
        assert error.value.code == "validation_error", (name, arguments)
    scope = {"query": "atlas", "kind": None, "attributes": {}}
    for offset, revision in ((-1, graph.commits[support.ACTION_EVENT]), (10 ** 7, graph.commits[support.ACTION_EVENT])):
        with pytest.raises(DomainError):
            reader.find("atlas", limit=1, cursor=forged("context_find", scope, revision, offset))
    body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    for change in ({"r": "z" * 40}, {"v": 2}, {"extra": 1}, {"o": "1"}):
        mangled = base64.urlsafe_b64encode(json.dumps({**body, **change}).encode()).decode().rstrip("=")
        with pytest.raises(DomainError):
            reader.find("atlas", limit=1, cursor=mangled)
    unknown = forged("context_find", scope, "0" * 40, 1)
    with pytest.raises(DomainError) as error:
        reader.find("atlas", limit=1, cursor=unknown)
    assert error.value.code == "validation_error" and "cursor" in error.value.field_errors


def test_an_object_cursor_cannot_move_to_another_object_or_part(reader):
    atlas = reader.object(support.ATLAS, history_limit=1)
    token = atlas["history"]["page"]["next_cursor"]
    assert token
    for kwargs in ({"ref": support.ALICE, "history_cursor": token}, {"ref": support.ATLAS, "text_cursor": token}):
        with pytest.raises(DomainError):
            reader.object(history_limit=1, **kwargs)
    assert reader.object("node:" + support.ATLAS, history_limit=1, history_cursor=token)["history"]["page"]["limit"] == 1


# ---- drafts and private runtime state are never read

def test_pending_draft_data_is_absent_and_reads_write_nothing(tmp_path):
    graph = support.published_graph(tmp_path, draft=True)
    shared = SharedContextReader(graph.repository, graph.runtime)
    before = fingerprint(graph.repository, graph.runtime)
    results = [shared.home(limit=60), shared.find("", limit=100), shared.changes(limit=100),
               shared.find("", kind="source", limit=100)]
    text = json.dumps(results)
    assert "drft1" not in text and support.DRAFT_MARKER not in text and "draft-only" not in text.lower()
    assert "Draft source" not in text and support.DRAFT_EVENT not in text
    for query in (support.DRAFT_MARKER, "draft", "drft1"):  # a query is echoed back, so these are checked apart
        assert shared.find(query)["items"] == []
    for ref in ("drft1", "node:drft1", "source:" + "abcde"):
        with pytest.raises(DomainError) as error:
            shared.object(ref)
        assert error.value.code == "not_found"
    assert results[0]["totals"]["nodes"] == 4 and results[0]["revision"] == graph.commits[support.ACTION_EVENT]
    assert fingerprint(graph.repository, graph.runtime) == before
    status = subprocess.run(["git", "-C", str(graph.repository), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""


def test_a_reader_does_not_block_or_collide_with_the_writer(tmp_path):
    graph = support.published_graph(tmp_path, draft=True)
    shared = SharedContextReader(graph.repository, graph.runtime)
    shared.find("atlas")
    published = graph.publish_draft()  # the engine's writer lock and index are free while the reader holds a snapshot
    assert shared.home()["revision"] == published
    assert shared.find("atlas")["revision"] == published


# ---- objects

def test_a_node_read_carries_relationships_evidence_and_before_after_history(reader, graph):
    result = conforms(schemas.OBJECT_OUTPUT, reader.object(support.ATLAS))
    assert result["revision"] == graph.commits[support.ACTION_EVENT]
    node = result["object"]
    assert node == {"kind": "node", "id": support.ATLAS, "ref": "node:atlas", "summary": "Atlas program (April)",
                    "attributes": {"basic_entity_type": ["program"], "name": ["Atlas program (April)"],
                                   "status": ["shipping"]}}
    assert result["relationship_count"] == 1 and result["relationships"][0]["id"] == support.LEADS
    assert result["relationships"][0]["direction"] == "in" and result["relationships_truncated"] is False
    # The edit replaced the node's support with its own commit; the superseded event stays in the history below.
    assert result["support"]["count"] == 1 and result["support"]["truncated"] is False
    [evidence] = result["support"]["items"]
    assert evidence["event_id"] == support.EDIT_EVENT and evidence["commit"] == graph.commits[support.EDIT_EVENT]
    assert evidence["source"]["title"] == "Atlas note" and evidence["source"]["origin_ref"] == "task:foreign-business-task"
    assert evidence["input_mode"] == "GENERAL"
    history = result["history"]
    assert history["total"] == 2 and [entry["event_id"] for entry in history["items"]] == [support.EDIT_EVENT, support.FIRST_EVENT]
    edit, add = history["items"]
    assert edit["operation"] == "edit" and edit["before"]["summary"] == "Atlas program"
    assert edit["after"]["summary"] == "Atlas program (April)" and edit["citations"] == [graph.commits[support.EDIT_EVENT]]
    assert add["operation"] == "add" and add["before"] is None and add["after"]["attributes"]["budget"] == [40000, True]
    assert add["input_mode"] == "GENERAL" and add["recorded_at"].endswith("Z")


def test_an_edge_read_names_both_endpoints_and_its_evidence(reader):
    result = conforms(schemas.OBJECT_OUTPUT, reader.object("edge:" + support.MANAGES))
    edge = result["object"]
    assert edge["kind"] == "edge" and edge["from"]["id"] == support.BOBBY and edge["to"]["summary"] == "Alice Rivera"
    assert edge["attributes"] == {"relation": ["manager"]} and result["support"]["count"] == 1
    assert [entry["event_id"] for entry in result["history"]["items"]] == [support.SECOND_EVENT]
    assert "relationships" not in result


def test_a_source_read_pages_its_committed_text_and_never_opens_the_operational_origin(reader, graph):
    source_id = graph.source_id("foundation/survey.md")
    first = conforms(schemas.OBJECT_OUTPUT, reader.object("source:" + source_id, text_limit=500))
    source = first["object"]
    assert source["kind"] == "source" and source["title"] == "Survey" and source["origin"] == "synthetic-intake"
    assert source["origin_ref"] is None and source["withdrawn"] is False
    content = first["content"]
    assert content["limit"] == 500 and content["has_more"] is True and content["total_characters"] == len(support.LONG_TEXT)
    text, cursor, pages = content["text"], content["next_cursor"], 1
    while cursor:
        page = reader.object(source_id, text_limit=500, text_cursor=cursor)["content"]
        text, cursor, pages = text + page["text"], page["next_cursor"], pages + 1
    assert text == support.LONG_TEXT.decode() and pages > 3
    atlas = reader.object("source:" + graph.source_id("foundation/atlas.md"))
    assert atlas["object"]["origin_ref"] == "task:foreign-business-task"  # a plain string, never dereferenced
    assert {item["id"] for item in atlas["supports"]["items"]} >= {support.ALICE, support.ATLAS}
    assert [entry["event_id"] for entry in atlas["history"]["items"]] == [support.EDIT_EVENT, support.FIRST_EVENT]
    assert atlas["history"]["items"][0]["operation"] == "edit"
    with pytest.raises(DomainError) as error:
        reader.source(support.ALICE)
    assert error.value.code == "not_found"
    assert reader.source(source_id)["object"]["id"] == source_id


def test_object_history_pages_are_bounded_and_ordered_newest_first(reader):
    first = reader.object(support.ALICE, history_limit=1)
    assert first["history"]["total"] == 1 and first["history"]["page"]["has_more"] is False
    atlas = reader.object(support.ATLAS, history_limit=1)
    assert atlas["history"]["page"] == {"limit": 1, "returned": 1, "has_more": True,
                                        "next_cursor": atlas["history"]["page"]["next_cursor"]}
    second = reader.object(support.ATLAS, history_limit=1, history_cursor=atlas["history"]["page"]["next_cursor"])
    assert [entry["event_id"] for entry in atlas["history"]["items"] + second["history"]["items"]] == \
        [support.EDIT_EVENT, support.FIRST_EVENT]
    assert reader.history(support.ATLAS)["history"]["total"] == 2
    for bad in (0, 26, "x"):
        with pytest.raises(DomainError):
            reader.object(support.ATLAS, history_limit=bad)
    for bad in (0, 64001):
        with pytest.raises(DomainError):
            reader.object(support.ATLAS, text_limit=bad)


def test_references_resolve_by_kind_or_bare_id_and_unknown_objects_are_not_found(reader, graph):
    assert reader.reference(support.ALICE)["ref"] == "node:alice"
    assert reader.reference("edge:" + support.LEADS) == {"status": "ready", "revision": reader.status()["revision"],
                                                        "kind": "edge", "id": support.LEADS, "ref": "edge:lead1"}
    source_id = graph.source_id("foundation/atlas.md")
    assert reader.reference(source_id)["ref"] == "source:" + source_id
    for bad in ("", "ALICE", "node:", "alice1", "nodes:alice", None, 5, "node:alice ", "../x"):
        with pytest.raises(DomainError) as error:
            reader.object(bad)
        assert error.value.code == "validation_error"
    for missing in ("zzzzz", "node:zzzzz", "edge:alice", "node:lead1"):
        with pytest.raises(DomainError) as error:
            reader.object(missing)
        assert error.value.code == "not_found"


# ---- find

def test_find_searches_published_text_with_filters_and_stable_paging(reader, graph):
    result = conforms(schemas.FIND_OUTPUT, reader.find("atlas"))
    ids = [item["id"] for item in result["items"]]
    assert support.ATLAS in ids and support.LEADS in ids and result["revision"] == graph.commits[support.ACTION_EVENT]
    assert conforms(schemas.FIND_OUTPUT, reader.find("atlas", kind="node"))["items"][0]["kind"] == "node"
    assert {item["kind"] for item in reader.find("atlas", kind="source")["items"]} == {"source"}
    shipping = reader.find("", attributes={"status": ["shipping"]})
    assert [item["id"] for item in shipping["items"]] == [support.ATLAS]
    assert reader.find("quartzite", kind="source")["items"][0]["title"] == "Survey"
    assert reader.find("zyxwvut")["items"] == [] and reader.find("zyxwvut")["page"]["has_more"] is False
    everything, revisions = drain(reader.find, "items", query="", limit=3)
    assert len(everything) == len({(item["kind"], item["id"]) for item in everything}) == 4 + 3 + 3
    assert revisions == {result["revision"]}
    ranked, _ = drain(reader.find, "items", query="atlas", limit=1)
    assert [item["id"] for item in ranked] == ids


def test_find_bounds_and_search_window(reader, monkeypatch):
    for bad in ({"limit": 0}, {"limit": 101}, {"kind": "event"}, {"query": "x" * 2001}, {"query": 5},
                {"attributes": {"status": "shipping"}}, {"attributes": {"status": [None]}}):
        with pytest.raises(DomainError) as error:
            reader.find(**{"query": "", **bad})
        assert error.value.code == "validation_error"
    monkeypatch.setattr(reader_module, "MAX_SEARCH_WINDOW", 3)
    last = reader.find("", limit=2)
    assert last["page"]["has_more"] is True
    window = reader.find("", limit=2, cursor=last["page"]["next_cursor"])
    assert window["page"]["window_exhausted"] is True and window["page"]["has_more"] is False
    assert window["page"]["next_cursor"] is None and conforms(schemas.FIND_OUTPUT, window)


# ---- expand

def test_expand_returns_pinned_bounded_relationships_by_depth_and_direction(reader, graph):
    one = conforms(schemas.EXPAND_OUTPUT, reader.expand(support.BOBBY))
    assert one["origin"]["id"] == support.BOBBY and one["depth"] == 1 and one["direction"] == "both"
    assert [item["id"] for item in one["relationships"]] == [support.MANAGES]
    assert one["relationships"][0]["direction"] == "out" and one["relationships"][0]["depth"] == 1
    two = conforms(schemas.EXPAND_OUTPUT, reader.expand(support.BOBBY, depth=2))
    assert {item["id"]: item["depth"] for item in two["relationships"]} == {support.MANAGES: 1, support.LEADS: 2,
                                                                          support.WORKS: 2}
    assert [item["depth"] for item in two["relationships"]] == sorted(item["depth"] for item in two["relationships"])
    assert reader.expand(support.BOBBY, depth=2, direction="in")["relationships"] == []
    assert [item["id"] for item in reader.expand(support.ALICE, direction="in")["relationships"]] == [support.MANAGES]
    assert {item["id"] for item in reader.expand(support.ALICE, direction="out")["relationships"]} == \
        {support.LEADS, support.WORKS}
    paged, revisions = drain(reader.expand, "relationships", ref=support.ALICE, limit=1)
    assert {item["id"] for item in paged} == EDGES and revisions == {two["revision"]}
    assert reader.expand(support.ALICE, limit=1)["total"] == 3


def test_expand_rejects_non_nodes_bad_options_and_missing_objects(reader):
    with pytest.raises(DomainError) as error:
        reader.expand("edge:" + support.LEADS)
    assert error.value.code == "validation_error" and "ref" in error.value.field_errors
    for bad in ({"depth": 0}, {"depth": 4}, {"limit": 0}, {"limit": 201}, {"direction": "sideways"}):
        with pytest.raises(DomainError) as error:
            reader.expand(support.ALICE, **bad)
        assert error.value.code == "validation_error"
    with pytest.raises(DomainError) as error:
        reader.expand("zzzzz")
    assert error.value.code == "not_found"


# ---- changes (the owner diagnostic feed)

def test_changes_list_published_events_newest_first_with_attribution(reader, graph):
    result = conforms(schemas.CHANGES_OUTPUT, reader.changes())
    assert [item["event_id"] for item in result["items"]] == [support.ACTION_EVENT, support.EDIT_EVENT,
                                                              support.SECOND_EVENT, support.FIRST_EVENT]
    action, edit = result["items"][0], result["items"][1]
    assert action["input_mode"] == "OPERATIONAL_ACTION" and action["action"]["tags"][0]["business_id"] == "business:north"
    assert edit["submitted_by"] == support.OWNER_SUBMITTER and edit["action"] is None
    assert edit["changes"][0]["id"] == support.ATLAS and edit["changes"][0]["operation"] == "edit"
    scoped = reader.changes(ref="node:" + support.ATLAS)
    assert [item["event_id"] for item in scoped["items"]] == [support.EDIT_EVENT, support.FIRST_EVENT]
    paged, revisions = drain(reader.changes, "items", limit=1)
    assert len(paged) == 4 and revisions == {result["revision"]}


def test_reader_closing_removes_the_private_search_scratch_and_blocks_further_reads(graph):
    own = SharedContextReader(graph.repository, graph.runtime)
    own.find("atlas")
    scratch = own._scratch
    assert scratch is not None and scratch.exists() and list(scratch.glob("*.sqlite3"))
    own.close()
    assert not scratch.exists()
    assert own.home()["reason"]["code"] == "reader_closed"


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.invalid",
                           *arguments], check=True, capture_output=True, text=True).stdout.strip()


def test_a_published_repository_in_another_format_or_without_a_graph_is_unavailable_and_unchanged(tmp_path):
    older, foreign = tmp_path / "older", tmp_path / "foreign"
    Context.initialize(older, tmp_path / "older.runtime", basic_entity_types=["person"])
    (older / "graph" / "config.json").write_text(json.dumps({"format_version": 1, "published_ref": "refs/heads/main"}))
    git(older, "commit", "-aqm", "An older format")
    foreign.mkdir()
    git(foreign, "init", "-q", "-b", "main")
    (foreign / "README.md").write_text("not a context graph\n")
    git(foreign, "add", "README.md")
    git(foreign, "commit", "-qm", "Unrelated content")
    for repository, code in ((older, "unsupported_format"), (foreign, "invalid_repository")):
        before = fingerprint(repository)
        shared = SharedContextReader(repository, tmp_path / (repository.name + ".runtime"))
        result = conforms(schemas.HOME_OUTPUT, shared.home())
        assert result["status"] == "unavailable" and result["reason"]["code"] == code
        assert shared.find("x")["status"] == "unavailable"
        status = shared.status()
        assert status["status"] == "unavailable" and status["reason"]["code"] == code and status["revision"] is None
        assert fingerprint(repository) == before


def test_compact_views_are_bounded_and_flag_truncation_while_the_object_record_stays_whole(tmp_path):
    graph = support.Synthetic(tmp_path / "context", tmp_path / "runtime").initialize().populate_large()
    shared = SharedContextReader(graph.repository, graph.runtime)
    big = next(item for item in shared.find("", limit=100)["items"] if item["id"] == "bigxx")
    assert big["truncated"] is True and len(big["summary"]) <= 600 and len(big["attributes"]) <= 40
    assert all(len(members) <= 20 for members in big["attributes"].values())
    assert all(len(member) <= 200 for members in big["attributes"].values() for member in members if isinstance(member, str))
    whole = conforms(schemas.OBJECT_OUTPUT, shared.object("bigxx"))["object"]
    assert whole["summary"] == "B" * 2000 and len(whole["attributes"]) == 51
    assert len(whole["attributes"]["field00"]) == 25
    home = conforms(schemas.HOME_OUTPUT, shared.home(limit=60))
    hub = next(item for item in home["nodes"] if item["id"] == "hubxx")
    assert home["nodes"][0]["id"] == "hubxx" and hub["relationship_count"] == 30
    assert len(hub["relationships"]) == 5 and hub["relationships_truncated"] is True
    detail = conforms(schemas.OBJECT_OUTPUT, shared.object("hubxx"))
    assert detail["relationship_count"] == 30 and len(detail["relationships"]) == 25 and detail["relationships_truncated"]
    expanded, _ = drain(shared.expand, "relationships", ref="hubxx", limit=7)
    assert len(expanded) == 30 and len({item["id"] for item in expanded}) == 30
    assert shared.home(limit=60)["page"]["next_cursor"] is None and home["totals"]["nodes"] == 32


def test_a_withdrawn_source_keeps_its_history_but_has_no_content_and_is_not_counted(tmp_path):
    graph = support.Synthetic(tmp_path / "context", tmp_path / "runtime").initialize().populate_withdrawn()
    shared = SharedContextReader(graph.repository, graph.runtime)
    home = conforms(schemas.HOME_OUTPUT, shared.home())
    assert home["empty"] is True and home["totals"] == {"nodes": 0, "edges": 0, "sources": 0, "events": 2}
    assert shared.find("tempo")["items"] == [] and shared.find("", kind="source")["items"] == []
    result = conforms(schemas.OBJECT_OUTPUT, shared.object(graph.source_id("foundation/temp.md")))
    assert result["object"]["withdrawn"] is True and result["object"]["operation"] == "remove"
    assert result["content"] is None and result["supports"]["count"] == 0
    assert [(entry["event_id"], entry["operation"]) for entry in result["history"]["items"]] == \
        [("tmp02", "remove"), ("tmp01", "add")]
    with pytest.raises(DomainError) as error:
        shared.object("tempx")
    assert error.value.code == "not_found"
    removed = conforms(schemas.CHANGES_OUTPUT, shared.changes(ref="source:" + result["object"]["id"]))
    assert [item["event_id"] for item in removed["items"]] == ["tmp02", "tmp01"]


def test_a_valid_format_commit_outside_the_published_history_is_never_read(tmp_path):
    graph = support.published_graph(tmp_path)
    shared = SharedContextReader(graph.repository, graph.runtime)
    head = shared.status()["revision"]
    # An orphan commit with a perfectly valid published tree, reachable by hash only: no ref publishes it.
    orphan = git(graph.repository, "commit-tree", f"{head}^{{tree}}", "-m", "Unpublished orphan")
    assert orphan != head and git(graph.repository, "cat-file", "-t", orphan) == "commit"
    assert git(graph.repository, "branch", "--contains", orphan) == ""
    scope = {"query": "atlas", "kind": None, "attributes": {}}
    forged_cursors = {
        "home": ("context_home", {}), "object": ("context_object", {"id": support.ATLAS, "part": "history"}),
        "expand": ("context_expand", {"id": support.ALICE, "depth": 1, "direction": "both"}),
        "changes": ("context_changes", {"id": None}), "find": ("context_find", scope)}
    calls = {"home": lambda token: shared.home(cursor=token), "object": lambda token: shared.object(support.ATLAS, history_cursor=token),
             "expand": lambda token: shared.expand(support.ALICE, cursor=token), "changes": lambda token: shared.changes(cursor=token),
             "find": lambda token: shared.find("atlas", cursor=token)}
    for name, (operation, operation_scope) in forged_cursors.items():
        with pytest.raises(DomainError) as error:
            calls[name](forged(operation, operation_scope, orphan, 0))
        assert error.value.code == "validation_error", name
        assert "cursor" in error.value.field_errors or "history_cursor" in error.value.field_errors, name
    with pytest.raises(DomainError) as error:
        shared.home(revision=orphan)
    assert error.value.code == "validation_error" and "revision" in error.value.field_errors
    with pytest.raises(DomainError):
        shared.reference(support.ALICE, revision=orphan)
    # The published head and its ancestors still read, and the orphan was never cached as a graph.
    assert shared.home()["revision"] == head and shared.home(revision=graph.commits[support.FIRST_EVENT])["status"] == "ready"
    assert orphan not in shared._graphs


def test_a_cached_revision_that_the_published_ref_no_longer_reaches_is_rejected(tmp_path):
    graph = support.published_graph(tmp_path)
    shared = SharedContextReader(graph.repository, graph.runtime)
    old = graph.commits[support.SECOND_EVENT]
    page = shared.home(limit=1, revision=old)
    assert page["revision"] == old and old in shared._graphs  # cached while it was published
    token = shared.home(limit=1, revision=old)["page"]["next_cursor"]
    assert shared.home(limit=1, cursor=token)["revision"] == old
    # Move the published ref to an unrelated commit: the old revision is cached but no longer in the published ancestry.
    head = shared.status()["revision"]
    rewritten = git(graph.repository, "commit-tree", f"{head}^{{tree}}", "-m", "Rewritten history")
    git(graph.repository, "update-ref", "refs/heads/main", rewritten)
    with pytest.raises(DomainError) as error:
        shared.home(limit=1, cursor=token)
    assert error.value.code == "validation_error" and "cursor" in error.value.field_errors
    with pytest.raises(DomainError):
        shared.object(support.ATLAS, revision=old)
    assert shared.home()["revision"] == rewritten and shared.status()["revision"] == rewritten
