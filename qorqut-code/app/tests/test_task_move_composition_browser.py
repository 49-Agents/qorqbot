"""Move to project, composed: the integrated dashboard's real UI against the real composed host, with storage as the judge.

``task_move_composition_support`` builds one small synthetic host through real operations and serves the integrated dashboard
on a loopback ephemeral port (``create_app``, ``manage_lifecycle=False``, no provider, model or live data). The browser driver
in the dashboard repository talks only to that server, never mocks a Move operation or DTO, and blocks every other origin.
After it finishes, this module reads the SQLite database directly and proves what the UI claimed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from task_move_composition_support import BROWSER_TEST, build_move_composed, rows, write_fixture

PLAYWRIGHT = "/tmp/node_modules/playwright"


def browser_available():
    return shutil.which("node") is not None and Path(PLAYWRIGHT).is_dir() and BROWSER_TEST.is_file()


def capture(host, fixture):
    """Everything the Move may change, as it is stored before the browser opens anything."""
    story = fixture["story"]["ids"]
    holder = [fixture["holder"]["id"], fixture["holder"]["follower"]]
    ids = [*story, *holder, fixture["blocked"]["id"], fixture["blocked"]["mail"], fixture["stale"]["id"], fixture["stale"]["child"]]
    marks = ",".join("?" * len(ids))
    return {
        "tasks": {(row["business_id"], row["id"]): row for row in rows(host, f"SELECT * FROM tasks")},
        "messages": rows(host, f"SELECT * FROM thread_messages WHERE item_type='task' AND item_id IN ({marks}) ORDER BY id", *ids),
        "all_messages": rows(host, "SELECT * FROM thread_messages ORDER BY id"),
        "follows": rows(host, f"SELECT * FROM thread_follows WHERE item_type='task' AND item_id IN ({marks}) ORDER BY item_id, principal_id", *ids),
        "tags": rows(host, f"SELECT * FROM item_tags WHERE item_type='task' AND item_id IN ({marks}) ORDER BY item_id, tag_id", *ids),
        "edges": rows(host, "SELECT * FROM relationships ORDER BY id"),
        "conversations": rows(host, "SELECT * FROM owner_conversations ORDER BY id"),
        "chat": rows(host, "SELECT * FROM owner_conversation_messages ORDER BY sequence"),
        "reads": rows(host, "SELECT * FROM owner_conversation_reads ORDER BY rowid"),
        "cards": rows(host, "SELECT * FROM focus_cards ORDER BY id"),
        "links": rows(host, "SELECT * FROM project_item_links ORDER BY id"),
        "wakes": rows(host, "SELECT * FROM task_snooze_wakes ORDER BY rowid"),
        "completions": rows(host, "SELECT * FROM task_completions ORDER BY rowid"),
        "records": {name: rows(host, f"SELECT * FROM {name} ORDER BY rowid") for name in ("action_logs", "action_revisions", "signoffs")},
    }


def keyed(found, *columns):
    index = {(row[columns[0]] if len(columns) == 1 else tuple(row[column] for column in columns)): row for row in found}
    assert len(index) == len(found), f"duplicate key over {columns}"
    return index


def without(row, *names):
    return {key: value for key, value in row.items() if key not in names}


def scrubbed(found, *names):
    return sorted(json.dumps(without(row, *names), sort_keys=True, default=str) for row in found)


def verify_storage(host, fixture, before, after, ui_moves):
    """The browser's claims, judged by the stored rows: nothing here reads a product operation."""
    alpha, beta, gamma = (fixture["projects"][name] for name in ("alpha", "beta", "gamma"))
    story, holder, blocked, stale = fixture["story"], fixture["holder"], fixture["blocked"], fixture["stale"]
    launch, chat = fixture["launch"], fixture["chat"]
    destinations = {**{identity: (None, beta) for identity in story["ids"]},
                    **{identity: (alpha, gamma) for identity in (holder["id"], holder["follower"])},
                    **{identity: (alpha, beta) for identity in (stale["id"], stale["child"], launch["id"], launch["child"], chat["live"]["id"])}}
    roots = {story["id"]: story["outside"], holder["id"]: None, stale["id"]: None, launch["id"]: None, chat["live"]["id"]: None}
    move_ids = {move["label"]: move["move_id"] for move in ui_moves}

    # -- Tasks: same ids, new scope, one revision more (two for the Task a concurrent editor also changed), roots detached.
    old_tasks, new_tasks = before["tasks"], after["tasks"]
    assert len(new_tasks) == len(old_tasks), "a Move neither creates nor deletes a Task"
    for identity, (source, destination) in destinations.items():
        assert (source, identity) not in new_tasks, "the Task no longer exists in its old scope"
        old, new = old_tasks[(source, identity)], new_tasks[(destination, identity)]
        assert new["revision"] == old["revision"] + (2 if identity == stale["child"] else 1), (identity, new["revision"], old["revision"])
        ignored = ["business_id", "revision", "updated_at"] + (["parent_id"] if identity in roots else []) + (["title"] if identity == stale["child"] else [])
        assert without(new, *ignored) == without(old, *ignored), f"{identity} kept every field but its scope"
        if identity in roots:
            assert new["parent_id"] is None and old["parent_id"] == roots[identity], "a selected root is detached; the outside parent is untouched"
    assert new_tasks[(beta, stale["child"])]["title"] == stale["edited_title"], "the concurrent edit survived the Move"
    for identity, row in old_tasks.items():
        if identity[1] not in destinations:
            assert new_tasks[identity] == row, f"{identity} is not part of any Move and must be byte-identical"
    assert new_tasks[(None, story["outside"])] == old_tasks[(None, story["outside"])]
    assert new_tasks[(gamma, chat["live"]["id"])] == old_tasks[(gamma, chat["live"]["id"])], "the Gamma Task that only shares the chat Task's id is untouched"
    assert new_tasks[(alpha, chat["reused"]["id"])] == old_tasks[(alpha, chat["reused"]["id"])] and \
        new_tasks[(beta, chat["reused"]["id"])] == old_tasks[(beta, chat["reused"]["id"])], "both lifetimes of the reused id are as they were"
    assert new_tasks[(None, story["unrelated_child"])]["parent_id"] == story["outside"]

    # -- Discussion: every message keeps id, author, time, body and references; only scope changes and the origin is pinned.
    old_messages, new_messages = keyed(before["all_messages"], "id"), keyed(after["all_messages"], "id")
    assert set(old_messages) <= set(new_messages), "no message is deleted"
    extra = set(new_messages) - set(old_messages)
    assert {new_messages[identity]["item_id"] for identity in extra} == {stale["child"]}, "only the concurrent edit wrote a note; a Move writes none"
    for identity, old in old_messages.items():
        new, target = new_messages[identity], destinations.get(old["item_id"])
        if target and old["business_id"] == target[0]:
            assert new["business_id"] == target[1]
            assert without(new, "business_id", "reference_namespace") == without(old, "business_id", "reference_namespace"), "the payload is never rewritten"
            assert new["reference_namespace"] == ("global" if target[0] is None else f"business:{target[0]}"), "the original reference scope is pinned"
        else:
            assert new == old, "an unmoved note (including a same-id decoy's) is untouched"
    refs = {note["body"]: json.loads(note["refs_json"]) for note in after["all_messages"]}
    assert refs["Agreed next step is the follow-up"] == [{"id": holder["follower"], "type": "task"}], "stored refs stay relative"
    assert refs["Also see the task we left behind"] == [{"id": holder["behind"], "type": "task"}]

    # -- Actions and Signoffs are immutable records: the Story's Move changed none of them, and the pre-move ones are still Alpha's.
    assert after["records"] == before["records"], "no Action, Action revision or Signoff was written, changed or moved"
    action = next(row for row in after["records"]["action_logs"] if row["action_key"] == launch["action"]["id"])
    assert action["business_id"] == alpha and json.loads(action["refs_json"]).get("task") == launch["child"]
    signoff = next(row for row in after["records"]["signoffs"] if row["id"] == launch["signoff"]["id"])
    assert signoff["review_business_id"] == alpha and signoff["state"] == "rejected"

    # -- Follows, tags, edges, wakes and completions: the same rows under the destination scope.
    for name, key in (("follows", "item_id"), ("tags", "item_id")):
        for identity, (source, destination) in destinations.items():
            old = [row for row in before[name] if row[key] == identity and row["business_id"] == source]
            new = [row for row in after[name] if row[key] == identity and row["business_id"] == destination]
            assert scrubbed(new, "business_id") == scrubbed(old, "business_id"), f"{name} of {identity} follow it"
    assert before["follows"] and before["tags"] and before["edges"], "the fixture really carries follows, tags and relationships to preserve"
    old_edges, new_edges = keyed(before["edges"], "id"), keyed(after["edges"], "id")
    assert set(old_edges) == set(new_edges), "no relationship is created or deleted"
    for identity, old in old_edges.items():
        target = destinations.get(old["from_id"]) if old["from_type"] == "task" else None
        if target and old["business_id"] == target[0]:
            assert new_edges[identity]["business_id"] == target[1] and without(new_edges[identity], "business_id") == without(old, "business_id")
        else:
            assert new_edges[identity] == old
    for name in ("wakes", "completions"):
        for identity, (source, destination) in destinations.items():
            old = [row for row in before[name] if row["task_id"] == identity and row["business_id"] == source]
            new = [row for row in after[name] if row["task_id"] == identity and row["business_id"] == destination]
            assert scrubbed(new, "business_id") == scrubbed(old, "business_id"), f"{name} of {identity} follow it"
    assert len(after["completions"]) == len(before["completions"]) and len(after["wakes"]) == len(before["wakes"])

    # -- Private owner conversations: same conversation, messages and read state; scope and source key follow the Task.
    old_conversations, new_conversations = keyed(before["conversations"], "id"), keyed(after["conversations"], "id")
    assert set(old_conversations) == set(new_conversations)
    moved_chats = 0
    for identity, old in old_conversations.items():
        new, target = new_conversations[identity], destinations.get(old["source_id"]) if old["source_type"] == "task" else None
        if target and old["business_id"] == target[0]:
            moved_chats += 1
            assert new["business_id"] == target[1] and new["source_key"] == f"source:{target[1]}:task:{old['source_id']}"
            assert without(new, "business_id", "source_key", "revision", "updated_at") == without(old, "business_id", "source_key", "revision", "updated_at")
        else:
            assert new == old
    assert moved_chats == 2, "both private story conversations moved"
    assert after["chat"] == before["chat"], "every private message keeps its id, sequence, author and body"
    old_reads = keyed(before["reads"], "conversation_id", "principal_id")
    new_reads = keyed(after["reads"], "conversation_id", "principal_id")
    assert all(key in new_reads and new_reads[key]["last_read_sequence"] >= row["last_read_sequence"] for key, row in old_reads.items()), "read state is kept"

    # -- Focus cards: the same card and order, rewritten to the destination; unrelated cards untouched.
    old_cards, new_cards = keyed(before["cards"], "id"), keyed(after["cards"], "id")
    assert set(old_cards) == set(new_cards), "no card is created or deleted"
    card_destinations = {story["card"]: (story["id"], beta), story["grandchild_card"]: (story["grandchild"], beta),
                         holder["card"]: (holder["id"], gamma), stale["card"]: (stale["id"], beta), launch["card"]: (launch["id"], beta)}
    for identity, old in old_cards.items():
        new = new_cards[identity]
        if identity in card_destinations:
            task, destination = card_destinations[identity]
            assert new["source_key"] == f"source:{destination}:task:{task}"
            assert json.loads(new["source_json"]) == {"type": "task", "id": task, "business_id": destination}
            assert new["position"] == old["position"] and new["reason"] == old["reason"] and new["created_at"] == old["created_at"]
        else:
            assert new == old, "a card of a Task that stayed (the blocked Story's, a project's) is untouched"

    # -- Project links: the destination's own link is redundant and removed; the other follows the Story.
    new_links = keyed(after["links"], "id")
    assert story["owned_link"] not in new_links, "a link owned by the destination became redundant"
    outside = new_links[story["outside_link"]]
    assert (outside["target_business_id"], outside["target_namespace"], outside["target_id"]) == (beta, f"business:{beta}", story["id"])
    assert len(new_links) == len(before["links"]) - 1

    # -- Audit and receipts: one append-only group per UI Move, one receipt per submission, nothing for conflicts or retries.
    audit = rows(host, "SELECT * FROM task_moves ORDER BY seq")
    seeded = chat["seed_move"]
    assert len(audit) == len(destinations) + len(seeded["ids"]) == 13
    assert len({row["move_id"] for row in audit}) == len(ui_moves) + 1 == 6, "five browser Moves plus the history the Chat fixture started from"
    groups = {"global-story": story["ids"], "project-task": [holder["id"], holder["follower"]], "stale-story": [stale["id"], stale["child"]],
              "launch-story": [launch["id"], launch["child"]], "chat-task": [chat["live"]["id"]]}
    owner = audit[0]["actor_id"]
    for label, identities in groups.items():
        found = [row for row in audit if row["move_id"] == move_ids[label]]
        assert sorted(row["task_id"] for row in found) == sorted(identities), label
        for row in found:
            source, destination = destinations[row["task_id"]]
            assert (row["source_business_id"], row["destination_business_id"], row["actor_id"]) == (source, destination, owner)
            assert row["revision_after"] > row["revision_before"]
            assert row["task_created_at"] == old_tasks[(source, row["task_id"])]["created_at"]
            assert row["previous_parent_id"] == old_tasks[(source, row["task_id"])]["parent_id"], "the audit remembers the old parent"
    history = [row for row in audit if row["move_id"] == seeded["move_id"]]
    assert sorted(row["task_id"] for row in history) == sorted(seeded["ids"]) and {(row["source_business_id"], row["destination_business_id"]) for row in history} == {(alpha, beta)}
    receipts = rows(host, "SELECT * FROM idempotency_receipts WHERE operation_name='tasks_move_to_project' ORDER BY created_at")
    started = [row for row in receipts if seeded["move_id"] in row["result_json"]]
    assert len(started) == 1, "the fixture's own history left one receipt"
    receipts = [row for row in receipts if row not in started]
    assert sorted(row["idempotency_key"] for row in receipts) == sorted(move["key"] for move in ui_moves), "one receipt per executed Move"
    assert all(move["move_id"] in row["result_json"] for move in ui_moves for row in receipts if row["idempotency_key"] == move["key"])
    assert not [row for row in receipts if row["idempotency_key"] == next(m for m in ui_moves if m["label"] == "stale-story")["conflictedKey"]], \
        "the conflicted submission left no receipt"
    untouched = {blocked["id"], blocked["mail"]}
    assert not [row for row in audit if row["task_id"] in untouched], "the blocked Story never moved"


@pytest.mark.skipif(not browser_available(), reason="node/playwright or the dashboard's composed driver is unavailable")
def test_the_real_dashboard_moves_records_through_the_real_host(tmp_path):
    host = build_move_composed(tmp_path)
    try:
        fixture = host.fixture
        before = capture(host, fixture)
        document = tmp_path / "fixture.json"
        write_fixture(host, document)
        shots = Path(os.environ.get("TASK_MOVE_COMPOSITION_SHOTS", tmp_path / "shots"))
        shots.mkdir(parents=True, exist_ok=True)
        result = Path(os.environ.get("TASK_MOVE_COMPOSITION_RESULT", tmp_path / "result.json"))
        environment = {
            **os.environ,
            "TASK_MOVE_COMPOSITION_BASE": host.origin,
            "TASK_MOVE_COMPOSITION_FIXTURE": str(document),
            "TASK_MOVE_COMPOSITION_SHOTS": str(shots),
            "TASK_MOVE_COMPOSITION_RESULT": str(result),
            "PLAYWRIGHT_MODULE": os.environ.get("PLAYWRIGHT_MODULE", PLAYWRIGHT),
        }
        completed = subprocess.run(["node", str(BROWSER_TEST)], cwd=str(BROWSER_TEST.parent), env=environment,
                                   capture_output=True, text=True, timeout=900)
        print(completed.stdout)
        print(completed.stderr)
        assert completed.returncode == 0, "the composed Move to project browser run failed"
        outcome = json.loads(result.read_text())
        assert [move["label"] for move in outcome["uiMoves"]] == ["global-story", "project-task", "stale-story", "launch-story", "chat-task"], "the browser performed exactly these Moves (the last through the owner session API)"
        assert all(urlparse(url).hostname not in ("127.0.0.1", "localhost") for url in outcome["blockedExternal"]), "only external requests were denied"
        verify_storage(host, fixture, before, capture(host, fixture), outcome["uiMoves"])
        expected = {"scenario-1-preview.png", "scenario-1-moved-record.png", "scenario-2-linked-activity.png", "scenario-2-left-behind-record.png",
                    "scenario-3-blocked-preview.png", "scenario-3-stale-conflict.png", "scenario-4-moved-story-history.png",
                    "scenario-4-source-signoff.png", "scenario-5-moved-notice.png", "scenario-5-destination-record.png", "final.png"}
        assert expected <= {path.name for path in shots.iterdir()}, "the synthetic evidence screenshots were captured"
    finally:
        host.close()
