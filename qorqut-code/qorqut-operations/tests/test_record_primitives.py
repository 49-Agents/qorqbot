"""The shared primitives relocated into Operations: structured YAML, source document framing, file-name and path
validation, and the Task associations of a new record (default and host-supplied item resolver)."""

from __future__ import annotations

import pytest

from operational_support import BUSINESS, OperationalHost
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.assets.validation import filename
from qorqut_operations.contracts import DomainError
from qorqut_operations.documents.serialization import MAX_TEXT_BYTES, document_bytes, slugify, split_document
from qorqut_operations.records import associations
from qorqut_operations.records.vocabulary import RESOURCE_TYPES, document_type
from qorqut_operations.storage.paths import registered_path
from qorqut_operations.storage.structured import parse_yaml


def code_of(call, *args, **kwargs):
    with pytest.raises(DomainError) as raised:
        call(*args, **kwargs)
    return raised.value.code, str(raised.value)


# -- structured YAML -------------------------------------------------------------------------------------------------

def test_yaml_keys_are_unique_strings_and_dates_stay_strings():
    assert parse_yaml("when: 2026-10-25\nat: 2026-10-25 10:00:00\nitems: [1, 2]\n") == {
        "when": "2026-10-25", "at": "2026-10-25 10:00:00", "items": [1, 2]}
    assert code_of(parse_yaml, "a: 1\na: 2\n") == ("invalid_configuration", "Invalid structured YAML: Duplicate or non-string YAML key: 'a'")
    assert code_of(parse_yaml, "1: one\n")[0] == "invalid_configuration"
    assert code_of(parse_yaml, "a: [unclosed\n")[0] == "invalid_configuration"
    # Python object tags are refused by the safe loader, not constructed.
    assert code_of(parse_yaml, "a: !!python/object/apply:os.getcwd []\n")[0] == "invalid_configuration"


# -- source document framing -----------------------------------------------------------------------------------------

def test_document_framing_round_trips_metadata_and_keeps_the_body_exact():
    body = "\n\nSecond line after blank lines.\r\nTrailing\n"
    data = document_bytes({"title": "Ünïcode", "tags": ["a"], "day": "2026-10-25"}, body)
    assert data.startswith(b"---\ntitle: \xc3\x9cn\xc3\xafcode\ntags:\n- a\nday: '2026-10-25'\n---\n\n")
    metadata, restored = split_document(data)
    assert metadata == {"title": "Ünïcode", "tags": ["a"], "day": "2026-10-25"} and restored == body
    assert document_bytes({}, body, frontmatter=False) == body.encode()
    assert split_document(b"plain text\n---\nnot frontmatter\n") == ({}, "plain text\n---\nnot frontmatter\n")
    # Only the one framing separator is removed (LF or CRLF); the source's own leading blank lines stay.
    assert split_document(b"---\r\ntitle: A\r\n---\r\n\r\nBody") == ({"title": "A"}, "Body")
    assert split_document(b"---\ntitle: A\n---\n\n\nBody") == ({"title": "A"}, "\nBody")
    assert split_document(b"---\n---\nBody") == ({}, "Body")


def test_document_framing_errors_and_bounds():
    assert code_of(split_document, b"---\ntitle: A\nBody") == ("invalid_document", "Document frontmatter is unterminated")
    assert code_of(split_document, b"---\n- a\n---\n") == ("invalid_document", "Document frontmatter must be a mapping")
    assert code_of(split_document, b"\xff\xfe") == ("invalid_document", "Document is not UTF-8 text")
    assert code_of(split_document, b"---\na: 1\na: 2\n---\n")[0] == "invalid_configuration"
    assert code_of(document_bytes, {}, "x" * (MAX_TEXT_BYTES + 1)) == ("validation_error", "Document exceeds the text size limit")
    assert code_of(document_bytes, {}, b"bytes")[0] == "validation_error"


def test_slugs_and_asset_file_names_normalize_deterministically():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert slugify("2026 plan") == "note-2026-plan" and slugify("!!!") == "note-untitled"
    assert len(slugify("a" * 100)) == 64
    assert filename("C:\\Users\\Me\\My Photo (1).JPEG") == "my-photo-1.jpeg"
    assert filename("archive.tar.GZ") == "archive-tar.gz" and filename("name") == "name"
    assert filename("x" * 200 + ".md") == "x" * 160 + ".md"
    for invalid in ("", ".hidden", "dir/", "***.png"):
        assert code_of(filename, invalid) == ("validation_error", "Invalid asset filename")


# -- registered paths ------------------------------------------------------------------------------------------------

def test_registered_paths_stay_under_their_root(tmp_path):
    root = tmp_path / "operations"
    (root / "data").mkdir(parents=True)
    expected = (root / "data" / "table.json").resolve()
    assert registered_path(root, "data/table.json") == expected
    assert registered_path(root, root / "data" / "table.json") == expected
    assert code_of(registered_path, root, tmp_path / "elsewhere.json") == (
        "invalid_configuration", "Registered data path is outside the operations root")
    assert code_of(registered_path, root, "../escape.json")[0] == "validation_error"
    with pytest.raises(FileNotFoundError):
        registered_path(tmp_path / "missing", "x.json")


# -- Task associations ----------------------------------------------------------------------------------------------

def test_vocabulary_is_neutral_operations_only():
    assert tuple(RESOURCE_TYPES) == ("tasks", "data_tables", "agent_profiles", "action_logs", "signoffs", "context_entities")
    assert document_type({"kind": "context_topic"}) == "topic" and document_type({"kind": "context_person"}) == "person"
    assert document_type({"kind": "note"}) == "material" and document_type({"kind": "email_message"}) == "material"
    assert associations.source_item(None, None, "tasks", "t-1") == {"type": "task", "id": "t-1"}
    with pytest.raises(KeyError):                      # a host's own resource tables are not in the neutral vocabulary
        associations.source_item(None, None, "email_messages", "m-1")


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def link(host, *, kind="tasks", identity, task_ids=(), inherit_from=None, item_resolver=None):
    """Prepare and apply one association exactly as a creating operation would; returns the stored relationships."""
    ctx = host.owner()
    with host.database.transaction() as uow:
        builder = PlanBuilder(ctx, "records_test", {}, "Associate", clock=host.clock)
        kwargs = {} if item_resolver is None else {"item_resolver": item_resolver}
        associations.link_new_resource(builder, uow, kind, identity, task_ids, inherit_from=inherit_from, **kwargs)
        change_set = builder.build()
        apply_records(ctx, change_set, uow)
    return host.query("SELECT * FROM relationships WHERE business_id IS ? ORDER BY id", BUSINESS), change_set


def test_the_default_resolver_relates_a_new_resource_to_existing_tasks(host):
    group, other, member = (host.create_task(title)["id"] for title in ("Group", "Other", "Member"))
    stored, change_set = link(host, identity=member, task_ids=[other, group])
    assert [(row["from_type"], row["from_id"], row["to_type"], row["to_id"], row["role"]) for row in sorted(stored, key=lambda row: row["to_id"])] == sorted(
        ("task", member, "task", task_id, "concerns") for task_id in (group, other))
    assert {change.operation_name for change in change_set.changes} == {"relationships_create"}
    assert link(host, identity=member)[1].changes == ()           # nothing to relate: no plan effect and no lookup


def test_an_association_needs_an_existing_task_in_scope(host):
    member = host.create_task("Member")["id"]
    ctx = host.owner()
    with host.database.read() as uow:
        builder = PlanBuilder(ctx, "records_test", {}, "Associate", clock=host.clock)
        assert code_of(associations.link_new_resource, builder, uow, "tasks", member, ["00000000-0000-4000-8000-0000000000ff"]) == (
            "not_found", "Task does not exist in this scope")


def test_an_injected_resolver_names_the_items_and_inherits_task_links(host):
    origin = "00000000-0000-4000-8000-0000000000a0"
    group = host.create_task("Group")["id"]
    calls = []

    def resolver(ctx, uow, kind, identity, builder=None):
        calls.append((kind, identity, builder is not None))
        return {"type": "topic", "id": identity}

    host.topic(origin)
    first, _ = link(host, kind="documents", identity=origin, task_ids=[group], item_resolver=resolver)
    assert calls == [("documents", origin, True)]
    assert [(row["from_type"], row["from_id"], row["to_id"]) for row in first] == [("topic", origin, group)]

    created = "00000000-0000-4000-8000-0000000000b0"
    host.topic(created)
    calls.clear()
    second, change_set = link(host, kind="documents", identity=created, inherit_from=origin, item_resolver=resolver)
    # The new resource is named with its builder, the inheritance origin without one; the origin's Task link is inherited.
    assert calls == [("documents", created, True), ("documents", origin, False)]
    inherited = [row for row in second if row["from_id"] == created]
    assert [(row["from_type"], row["to_type"], row["to_id"], row["role"]) for row in inherited] == [("topic", "task", group, "concerns")]
    assert any(target.resource.resource_type == "relationships" for target in change_set.expected_targets)
