"""Notes in the shared published Context reads: format 2 and 3 histories, note search and objects, source supports and
change entries, an unchanged node/edge graph, and format rejection (synthetic repositories under a pytest ``tmp_path``)."""

import json
import subprocess

import pytest
from jsonschema import Draft202012Validator
from qorqut_context import Context
from qorqut_operations.contracts import DomainError

import shared_context_support as support
from qorqut_context.integration.reading import SharedContextReader, schemas


def conforms(schema, result):
    errors = list(Draft202012Validator(schema).iter_errors(result))
    assert not errors, [(list(error.absolute_path), error.message[:160]) for error in errors[:3]]
    return result


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.invalid",
                           *arguments], check=True, capture_output=True, text=True).stdout.strip()


def drain(call, key, **arguments):
    items, revisions, cursor = [], set(), None
    while True:
        result = call(**arguments, cursor=cursor)
        assert result["status"] == "ready"
        items += result[key]
        revisions.add(result["revision"])
        cursor = result["page"]["next_cursor"]
        if cursor is None:
            return items, revisions


@pytest.fixture(scope="module")
def noted(tmp_path_factory):
    return support.published_graph(tmp_path_factory.mktemp("noted-graph")).populate_notes()


@pytest.fixture(scope="module")
def reader(noted):
    shared = SharedContextReader(noted.repository, noted.runtime)
    yield shared
    shared.close()


def test_a_format_two_context_reads_as_it_is_and_simply_has_no_notes(tmp_path):
    source = support.published_graph(tmp_path)
    legacy, runtime = tmp_path / "legacy", tmp_path / "legacy.runtime"
    support.legacy_copy(source, legacy, runtime, through=source.commits[support.EDIT_EVENT])
    shared = SharedContextReader(legacy, runtime)
    status = shared.status()
    assert status["status"] == "ready" and status["reason"] is None
    assert json.loads(git(legacy, "show", f"{status['revision']}:graph/config.json"))["format_version"] == 2
    home = conforms(schemas.HOME_OUTPUT, shared.home())
    assert home["empty"] is False and home["totals"] == {"nodes": 4, "edges": 3, "sources": 2, "events": 3}
    everything = conforms(schemas.FIND_OUTPUT, shared.find("", limit=100))
    assert sorted(item["kind"] for item in everything["items"]) == ["edge"] * 3 + ["node"] * 4 + ["source"] * 2
    assert shared.find("", kind="note")["items"] == []
    atlas = conforms(schemas.OBJECT_OUTPUT, shared.object(support.ATLAS))
    assert [entry["event_id"] for entry in atlas["history"]["items"]] == [support.EDIT_EVENT, support.FIRST_EVENT]
    source_read = conforms(schemas.OBJECT_OUTPUT, shared.object(source.source_id("foundation/atlas.md")))
    assert {item["id"] for item in source_read["supports"]["items"]} >= {support.ALICE, support.ATLAS}
    assert conforms(schemas.CHANGES_OUTPUT, shared.changes())["total"] == 3
    for missing in ("note:zzzzz", "zzzzz"):
        with pytest.raises(DomainError) as error:
            shared.object(missing)
        assert error.value.code == "not_found"
    shared.close()


def test_a_format_three_context_answers_unfiltered_and_note_searches_with_note_entries(reader, noted):
    everything, revisions = drain(reader.find, "items", query="", limit=4)
    assert revisions == {reader.status()["revision"]} and len(everything) == len({(item["kind"], item["id"]) for item in everything})
    assert {kind: sum(item["kind"] == kind for item in everything) for kind in ("node", "edge", "note", "source")} == \
        {"node": 4, "edge": 3, "note": 3, "source": 5}
    assert len(conforms(schemas.FIND_OUTPUT, reader.find("", limit=100))["items"]) == 15
    notes = conforms(schemas.FIND_OUTPUT, reader.find("", kind="note"))["items"]
    assert [item["id"] for item in notes] == [support.NOTE_LINKED, support.NOTE_FREE, support.NOTE_MIXED]
    assert notes[0] == {"kind": "note", "id": "notea", "ref": "note:notea", "text": support.NOTE_REVISED_TEXT,
                        "truncated": False, "subject_count": 3}
    assert [item["subject_count"] for item in notes] == [3, 0, 2]
    assert [item["id"] for item in reader.find("budget decision", kind="note")["items"]] == [support.NOTE_FREE]
    assert [(item["kind"], item["id"]) for item in reader.find("follow-up")["items"]][0] == ("note", support.NOTE_LINKED)
    assert [item["id"] for item in reader.find("follow-up", kind="note")["items"]] == [support.NOTE_LINKED]
    mentioned = {(item["kind"], item["id"]) for item in reader.find("alice")["items"]}
    assert {("note", support.NOTE_LINKED), ("note", support.NOTE_MIXED), ("node", support.ALICE)} <= mentioned
    assert {item["kind"] for item in reader.find("alice", kind="node")["items"]} == {"node"}
    assert [item["id"] for item in reader.find("", attributes={"status": ["shipping"]})["items"]] == [support.ATLAS]
    with pytest.raises(DomainError) as error:
        reader.find("", kind="event")
    assert error.value.code == "validation_error"


def test_a_note_object_has_exact_text_subjects_evidence_and_pinned_history(tmp_path):
    graph = support.published_graph(tmp_path).populate_notes()
    shared = SharedContextReader(graph.repository, graph.runtime)
    linked = conforms(schemas.OBJECT_OUTPUT, shared.object("note:notea", history_limit=1))
    assert linked["object"] == {"kind": "note", "id": "notea", "ref": "note:notea", "text": support.NOTE_REVISED_TEXT}
    assert linked["subjects"]["count"] == 3 and linked["subjects"]["truncated"] is False
    assert [(item["ref"], item["status"], item["summary"]) for item in linked["subjects"]["items"]] == [
        ("node:alice", "current", "Alice Rivera"), ("edge:lead1", "current", "Alice leads the Atlas program"),
        ("node:atlas", "current", "Atlas program (April)")]
    [evidence] = linked["support"]["items"]
    assert linked["support"]["count"] == 1 and evidence["event_id"] == support.NOTE_REVISION_EVENT
    assert evidence["source"]["title"] == "Follow-up notes"
    assert linked["history"]["total"] == 2 and linked["history"]["page"]["has_more"] is True
    [revision] = linked["history"]["items"]
    assert revision["operation"] == "edit" and revision["after"]["text"] == support.NOTE_REVISED_TEXT
    assert revision["before"]["text"] == support.NOTE_FIRST_TEXT
    assert [link["ref"] for link in revision["before"]["objects"]] == ["node:alice", "edge:lead1"]
    pinned = linked["revision"]
    graph.publish_later_note()
    assert shared.status()["revision"] != pinned
    older = shared.object("notea", history_limit=1, history_cursor=linked["history"]["page"]["next_cursor"])
    assert older["revision"] == pinned and conforms(schemas.OBJECT_OUTPUT, older)["history"]["total"] == 2
    [first] = older["history"]["items"]
    assert first["event_id"] == support.NOTE_EVENT and first["operation"] == "add" and first["before"] is None
    assert first["after"]["text"] == support.NOTE_FIRST_TEXT and older["history"]["page"]["has_more"] is False
    assert shared.history("note:notea")["object"] == {"kind": "note", "id": "notea", "ref": "note:notea"}

    mixed = conforms(schemas.OBJECT_OUTPUT, shared.object(support.NOTE_MIXED))
    assert [(item["ref"], item["status"], item["summary"]) for item in mixed["subjects"]["items"]] == [
        ("node:tempx", "removed", None), ("node:alice", "current", "Alice Rivera")]
    with pytest.raises(DomainError) as error:
        shared.object(support.TEMPO)
    assert error.value.code == "not_found"

    free = conforms(schemas.OBJECT_OUTPUT, shared.object("note:noteb"))
    assert free["object"]["text"] == support.NOTE_FREE_TEXT + " The decision is due in June."
    assert [(item["ref"], item["status"]) for item in free["subjects"]["items"]] == [("node:alice", "current")]
    assert [entry["event_id"] for entry in free["history"]["items"]] == [support.NOTE_LATER_EVENT, support.NOTE_EVENT]
    unlinked = shared.object("note:noteb", revision=graph.commits[support.NOTE_EVENT])
    assert conforms(schemas.OBJECT_OUTPUT, unlinked)["subjects"] == {"count": 0, "items": [], "truncated": False}
    assert unlinked["object"]["text"] == support.NOTE_FREE_TEXT and unlinked["history"]["total"] == 1

    assert shared.reference("note:notea")["ref"] == "note:notea"
    for call in (lambda: shared.source("note:notea"), lambda: shared.object("note:zzzzz"), lambda: shared.object("node:notea")):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == "not_found"
    with pytest.raises(DomainError) as error:
        shared.expand("note:notea")
    assert error.value.code == "validation_error" and "ref" in error.value.field_errors
    shared.close()


def test_source_supports_and_change_entries_carry_notes_without_breaking_node_and_edge_reads(reader, noted):
    notes_source = conforms(schemas.OBJECT_OUTPUT, reader.object(noted.source_id("foundation/notes.md")))
    assert notes_source["supports"]["count"] == 2
    assert [(item["kind"], item["id"]) for item in notes_source["supports"]["items"]] == [("note", "noteb"), ("note", "notec")]
    assert notes_source["supports"]["items"][0]["text"] == support.NOTE_FREE_TEXT
    update_source = conforms(schemas.OBJECT_OUTPUT, reader.object("source:" + noted.source_id("foundation/notes-update.md")))
    assert [(item["kind"], item["id"]) for item in update_source["supports"]["items"]] == [("note", "notea")]
    assert [entry["event_id"] for entry in update_source["history"]["items"]] == [support.NOTE_REVISION_EVENT]
    atlas_source = conforms(schemas.OBJECT_OUTPUT, reader.object(noted.source_id("foundation/atlas.md")))
    assert {item["kind"] for item in atlas_source["supports"]["items"]} == {"node", "edge"}
    changes = conforms(schemas.CHANGES_OUTPUT, reader.changes(limit=100))
    by_event = {item["event_id"]: item for item in changes["items"]}
    assert {(change["kind"], change["ref"], change["operation"]) for change in by_event[support.NOTE_EVENT]["changes"]} == {
        ("note", "note:notea", "add"), ("note", "note:noteb", "add"), ("note", "note:notec", "add")}
    scoped = conforms(schemas.CHANGES_OUTPUT, reader.changes(ref="note:notea"))
    assert [item["event_id"] for item in scoped["items"]] == [support.NOTE_REVISION_EVENT, support.NOTE_EVENT]
    node = conforms(schemas.OBJECT_OUTPUT, reader.object(support.ALICE))
    assert "subjects" not in node and node["history"]["total"] == 1
    assert [item["event_id"] for item in conforms(schemas.CHANGES_OUTPUT, reader.changes(ref="node:" + support.ATLAS))["items"]] == \
        [support.EDIT_EVENT, support.FIRST_EVENT]
    assert reader.source("source:" + noted.source_id("foundation/notes.md"))["object"]["kind"] == "source"


def test_notes_are_not_graph_vertices_and_leave_counts_and_traversal_unchanged(reader, tmp_path):
    plain = support.published_graph(tmp_path, name="plain")
    bare = SharedContextReader(plain.repository, plain.runtime)
    with_notes, without = conforms(schemas.HOME_OUTPUT, reader.home(limit=60)), conforms(schemas.HOME_OUTPUT, bare.home(limit=60))
    assert with_notes["totals"]["nodes"] == without["totals"]["nodes"] == 4
    assert with_notes["totals"]["edges"] == without["totals"]["edges"] == 3
    assert [node["id"] for node in with_notes["nodes"]] == [node["id"] for node in without["nodes"]]
    assert [node["relationship_count"] for node in with_notes["nodes"]] == [node["relationship_count"] for node in without["nodes"]]
    assert {node["kind"] for node in with_notes["nodes"]} == {"node"}
    for origin in (support.ALICE, support.ATLAS):
        expanded = conforms(schemas.EXPAND_OUTPUT, reader.expand(origin, depth=3))
        reference = bare.expand(origin, depth=3)
        assert expanded["relationships"] == reference["relationships"] and expanded["total"] == reference["total"]
        assert {item["kind"] for item in expanded["relationships"]} == {"edge"}
    assert reader.count_nodes({"basic_entity_type": ["person"]})["count"] == bare.count_nodes({"basic_entity_type": ["person"]})["count"] == 2
    assert reader.object(support.ALICE)["relationship_count"] == bare.object(support.ALICE)["relationship_count"] == 3
    bare.close()


@pytest.mark.parametrize("config, code", [
    ({"format_version": 4, "published_ref": "refs/heads/main"}, "unsupported_format"),
    ({"format_version": "3", "published_ref": "refs/heads/main"}, "unsupported_format"),
    ({"format_version": 1, "published_ref": "refs/heads/main", "extra": True}, "unsupported_format"),
    ({"format_version": 3, "published_ref": "refs/heads/elsewhere"}, "invalid_repository"),
    ({"format_version": 2, "published_ref": "refs/heads/main", "extra": True}, "invalid_repository"),
    (["format_version", 3], "unsupported_format"),
    ("not json", "invalid_repository")])
def test_an_unreadable_format_is_unavailable_with_the_matching_reason_and_a_good_one_is_ready(tmp_path, config, code):
    repository, runtime = tmp_path / "context", tmp_path / "context.runtime"
    Context.initialize(repository, runtime, basic_entity_types=["person"])
    good = SharedContextReader(repository, runtime)
    assert good.status()["status"] == "ready"
    (repository / "graph" / "config.json").write_text(config if isinstance(config, str) else json.dumps(config))
    git(repository, "commit", "-aqm", "A bad configuration")
    shared = SharedContextReader(repository, runtime)
    status = shared.status()
    assert status["status"] == "unavailable" and status["reason"]["code"] == code and status["revision"] is None
    for result in (conforms(schemas.HOME_OUTPUT, shared.home()), conforms(schemas.FIND_OUTPUT, shared.find("x", kind="note"))):
        assert result["status"] == "unavailable" and result["reason"]["code"] == code
    good.close()
    shared.close()
