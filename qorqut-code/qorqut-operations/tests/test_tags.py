"""The shared tag catalog: labels only, one entry per normalized label, never an access grant."""

from __future__ import annotations

import pytest

from operational_support import AGENT, AGENT2, BUSINESS, OTHER, OTHER_AGENT, OperationalHost
from qorqut_operations import tags


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def labels(host, ctx=None, **arguments):
    return [item["label"] for item in host.read("tags_list", arguments, ctx or host.agent())["tags"]]


def test_creating_a_label_is_idempotent_by_normalized_text(host):
    first = host.data("tags_create", {"label": "  Launch   Plan "}, host.agent())["tag"]
    again = host.data("tags_create", {"label": "launch plan"}, host.agent(AGENT2))["tag"]
    assert first["label"] == "Launch Plan" and again["id"] == first["id"] and again["label"] == "Launch Plan"
    assert host.count("tags") == 1
    assert host.data("tags_create", {"label": "two\nlines"}, host.agent())["tag"]["label"] == "two lines"      # whitespace is one space
    for invalid in ("", "   ", "x" * 65, "bell\x07label", "nul\x00label"):
        assert host.error("tags_create", {"label": invalid}, host.agent()) == "validation_error", invalid


def test_the_catalog_lists_labels_only_with_no_counts_or_item_references(host):
    host.create_task("First", tags=["alpha"])
    host.create_task("Second", tags=["Alpha", "beta"])
    host.data("tags_create", {"label": "unused"}, host.agent())
    listed = host.read("tags_list", {}, host.agent())["tags"]
    assert [item["label"] for item in listed] == ["alpha", "beta", "unused"]
    assert all(set(item) == {"id", "label", "created_at"} for item in listed)
    assert labels(host, query="ALP") == ["alpha"]
    page = host.read("tags_list", {"limit": 2}, host.agent())
    assert [item["label"] for item in page["tags"]] == ["alpha", "beta"] and page["next_cursor"]
    rest = host.read("tags_list", {"limit": 2, "cursor": page["next_cursor"]}, host.agent())
    assert [item["label"] for item in rest["tags"]] == ["unused"] and rest["next_cursor"] is None


def test_the_catalog_is_shared_across_namespaces_without_showing_other_businesses_items(host):
    host.create_task("Private plan", tags=["customer-escalation"])
    other = host.agent(OTHER_AGENT, OTHER)
    assert labels(host, other) == ["customer-escalation"]            # a label name is shared vocabulary
    assert host.read("tasks_list", {}, other)["tasks"] == []          # but it shows nothing of the owning business's records
    assert host.read("tasks_list", {"tags": ["customer-escalation"]}, other)["tasks"] == []


def test_a_tag_never_grants_access_or_creates_ownership(host):
    task = host.create_task("Escalation", tags=[BUSINESS, OTHER, "business:" + OTHER])
    other = host.agent(OTHER_AGENT, OTHER)
    assert host.error("tasks_get", {"task_id": task["id"]}, other) in {"not_found", "forbidden"}
    stored = host.task(task["id"])
    assert stored["business_id"] == BUSINESS                          # the owner is the namespace, whatever the labels say
    assert host.count("item_tags", "item_id=?", task["id"]) == 3
    assert host.count("businesses") == 2                             # naming a business in a label creates nothing


def test_labels_are_immutable_and_permanent(host):
    created = host.data("tags_create", {"label": "keep"}, host.agent())["tag"]
    for statement in ("UPDATE tags SET label='renamed' WHERE id=?", "DELETE FROM tags WHERE id=?"):
        with pytest.raises(Exception):
            with host.database.transaction() as uow:
                uow.connection.execute(statement, (created["id"],))
    assert labels(host) == ["keep"]


def test_item_tag_normalization_helpers_match_the_catalog():
    assert tags.label_key("  Mixed   CASE ") == "mixed case"
    assert tags.distinct_labels(["A", "a", " b"]) == ["A", "b"]
    assert tags.distinct_labels(["Research", "research", "Plan"])[1:] == ["Plan"]
    assert not hasattr(tags, "canonical_labels")
