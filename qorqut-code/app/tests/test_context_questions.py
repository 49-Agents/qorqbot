"""External Context review questions on a synthetic host: outbox delivery, the generic question tools and their authority.

Real: the stock host assembly and ``build_runtime``, the host's own ``ContextServices`` step and its question outbox drain, the
real ``Context`` (real Git) and the library's own ``Pipeline`` with its question outbox, the real ``QuestionStore`` file under a
pytest ``tmp_path``, the registered ``context_questions`` and ``context_question_edit`` operations with the real policy,
registry and applying transaction. Faked: only the model runner (a scripted one that stages one question while filing), the
injected clock, and the synthetic principals, roles and files. No provider, model, browser, network or live data is used."""

from contextlib import closing
import dataclasses
import json
import sqlite3
from unittest import mock

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id
from qorqut_operations.storage.receipts import IdempotencyReceipts

from qorqut_context.integration.questions import QuestionStore
from context_completion_support import Runner, build
from context_edit_support import build as build_edit

ASK = "Which Jesse is meant: the lawyer or the founder?"


def questioning_host(tmp_path):
    host = build(tmp_path)
    host.runner.during = lambda tools, payload: Runner.dispatch(
        tools, "emit_question", text=ASK, related=[{"kind": "event", "id": tools.event_id}])
    return host


def publish(host):
    host.upload(host.agent(), "Jesse appears in this note.")
    assert host.services.step()["status"] == "published"
    event_id = list(host.context.snapshot().events)[-1]
    return event_id, host.context.snapshot().event_commits[event_id], host.context.runtime / "questions" / f"{event_id}.json"


def owner_context(host):
    return dataclasses.replace(host.human(None), source="worker")


def interrupted(host, caller, arguments, key):
    with mock.patch.object(IdempotencyReceipts, "save", side_effect=RuntimeError("interrupted before the receipt")):
        with pytest.raises(RuntimeError):
            host.call(caller, "context_question_edit", arguments, key=key)


def test_a_published_filings_questions_are_delivered_once_then_acknowledged(tmp_path):
    host = questioning_host(tmp_path)
    store = QuestionStore(host.paths.context_questions)
    assert not store.exists()
    event_id, commit, outbox = publish(host)
    [question] = store.list()["items"]
    assert (question["text"], question["origin_event_id"]) == (ASK, event_id)
    assert question["related"] == [{"kind": "event", "id": event_id}]
    assert not outbox.exists() and store.delivered(event_id, commit)
    assert host.services.diagnostics()["questions"]["last"]["delivered"] == 1
    calls = list(host.runner.calls)
    assert host.services.step()["status"] != "published"
    assert store.list()["items"] == [question] and host.runner.calls == calls
    assert ASK.encode() not in b"\n".join(host.context.git.files(host.context.git.head()).values())


def test_a_failed_delivery_stays_in_the_outbox_and_is_retried_without_a_model_or_a_queue_outcome(tmp_path):
    host = questioning_host(tmp_path)
    path = host.paths.context_questions
    path.write_text("this is not a question store")
    event_id, commit, outbox = publish(host)
    failure = host.services.diagnostics()["questions"]["last"]
    assert failure["failed"] == 1 and failure["errors"][0]["event_id"] == event_id and failure["errors"][0]["stage"] == "deliver"
    assert outbox.is_file() and ASK not in json.dumps(failure)
    calls = list(host.runner.calls)
    path.unlink()
    with mock.patch.object(host.services.driver, "process_next", side_effect=RuntimeError("queue outcome bookkeeping failed")):
        with pytest.raises(RuntimeError):
            host.services.step()
    assert not outbox.exists() and host.services.diagnostics()["questions"]["last"]["delivered"] == 1
    assert [item["text"] for item in QuestionStore(path).list()["items"]] == [ASK]
    assert host.runner.calls == calls and host.context.snapshot().event_commits[event_id] == commit


def test_a_deleted_question_is_not_resurrected_by_a_delivered_but_unacknowledged_batch(tmp_path):
    host = questioning_host(tmp_path)
    store = QuestionStore(host.paths.context_questions)
    with mock.patch.object(host.pipeline, "acknowledge_question_output", side_effect=OSError("acknowledgement lost")):
        event_id, commit, outbox = publish(host)
    assert outbox.is_file() and store.delivered(event_id, commit)
    [question] = store.list()["items"]
    assert store.delete(question["id"]) is True
    host.services.step()
    assert not outbox.exists() and store.list()["items"] == []
    assert store.deliver(event_id, commit, [{"text": ASK, "related": []}]) == {"inserted": 0, "already_delivered": True}
    assert store.list()["items"] == []


def test_question_changes_apply_once_per_request_key_across_an_interrupted_receipt(tmp_path):
    host = build(tmp_path)
    learner = host.agent(None, "learning-applier")
    store = QuestionStore(host.paths.context_questions)
    create = {"action": "create", "text": ASK, "related": [{"kind": "node", "id": "aaaaa"}]}
    interrupted(host, learner, create, "create-key")
    [created] = store.list()["items"]
    assert created["related"] == [{"kind": "node", "id": "aaaaa"}]
    removed = host.data(learner, "context_question_edit", {"action": "delete", "question_id": created["id"]}, key="delete-key")
    assert removed["applied"] is True and store.list()["items"] == []
    retried = host.data(learner, "context_question_edit", create, key="create-key")
    assert retried["question_id"] == created["id"] and retried["replayed"] is True and "text" not in retried
    assert store.list()["items"] == []
    fresh = host.data(learner, "context_question_edit", {"action": "create", "text": ASK}, key="second-create")
    change = {"action": "update", "question_id": fresh["question_id"], "text": "Which Jesse, exactly?"}
    interrupted(host, learner, change, "update-key")
    assert store.read(fresh["question_id"])["text"] == "Which Jesse, exactly?"
    assert host.error(learner, "context_question_edit", {**change, "text": "A different change"}, key="update-key") == "conflict"
    assert host.data(learner, "context_question_edit", change, key="update-key")["replayed"] is True
    drop = {"action": "delete", "question_id": fresh["question_id"]}
    interrupted(host, learner, drop, "drop-key")
    assert store.list()["items"] == []
    replay = host.data(learner, "context_question_edit", drop, key="drop-key")
    assert (replay["action"], replay["question_id"], replay["applied"], replay["replayed"]) == (
        "delete", fresh["question_id"], True, True)
    with closing(sqlite3.connect(host.paths.context_questions)) as connection:
        receipts = connection.execute("SELECT * FROM mutations").fetchall()
    assert len(receipts) == 5 and "Which Jesse" not in json.dumps(receipts) and ASK not in json.dumps(receipts)


def test_only_the_owner_and_the_current_learning_applier_may_use_the_tools_under_their_context_edit_authority(tmp_path):
    host = build_edit(tmp_path)
    learner_id = host.agent_id("learning-applier")
    learner, coordinator = host.as_agent(learner_id, None), host.as_agent(host.agent_id("ordubashy"), None)
    for caller in (owner_context(host), learner):
        created = host.data(caller, "context_question_edit", {"action": "create", "text": ASK})
        assert host.data(caller, "context_questions", {"action": "get", "question_id": created["question_id"]})["question"]["text"] == ASK
        assert [item["id"] for item in host.data(caller, "context_questions", {"action": "list"})["items"]] == [created["question_id"]]
        host.data(caller, "context_question_edit", {"action": "delete", "question_id": created["question_id"]})
    stranger = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Member"})
        uow.repository("business_memberships", business_id=host.ids["first"]).insert({"principal_id": stranger, "is_owner": False})
    member = InvocationContext(Principal(stranger, "human"), allowed_business_ids=frozenset({host.ids["first"]}), source="worker",
                               global_capabilities=host.human(None).global_capabilities)
    for operation, arguments in (("context_questions", {"action": "list"}), ("context_question_edit", {"action": "create", "text": ASK})):
        assert host.error(coordinator, operation, arguments) == "policy_denied"
        assert host.error(member, operation, arguments) in {"forbidden", "policy_denied"}
    tools = {row["canonical_operation"]: row for row in host.runtime.resolver.resolve_agent_config(Principal(learner_id, "agent")).data["tools"]}
    assert all(tools[name]["available"] and tools[name]["effective_mode"] == "allow" for name in ("context_questions", "context_question_edit"))
    assert not any(row["available"] for row in host.runtime.resolver.resolve_agent_config(
        Principal(host.agent_id("ordubashy"), "agent")).data["tools"] if row["canonical_operation"].startswith("context_question"))
    both = {"context_questions": "allow", "context_question_edit": "allow"}
    host.set_overrides(learner_id, {"context_edit": "deny", **both})
    for operation, arguments in (("context_questions", {"action": "list"}), ("context_question_edit", {"action": "create", "text": ASK})):
        assert host.error(learner, operation, arguments) == "policy_denied"
    host.set_overrides(learner_id, {"context_question_edit": "deny"})
    assert host.error(learner, "context_question_edit", {"action": "create", "text": ASK}) == "policy_denied"
    assert host.data(learner, "context_questions", {"action": "list"})["items"] == []
    host.set_overrides(learner_id, {})
    assert host.data(learner, "context_question_edit", {"action": "create", "text": ASK})["applied"] is True


def test_composition_preview_and_reads_leave_the_question_store_uncreated(tmp_path):
    host = build(tmp_path, build_service=False)
    path, learner = host.paths.context_questions, host.agent(None, "learning-applier")
    assert path == host.paths.operations / ".runtime" / "context-questions.sqlite3" and not path.exists()
    assert host.services.diagnostics()["questions"] is None
    host.services.build()
    assert host.services.step()["status"] != "published" and host.services.diagnostics()["questions"]["last"]["examined"] == 0
    assert host.data(learner, "context_questions", {"action": "list"}) == {"items": [], "next_offset": None}
    assert host.error(learner, "context_questions", {"action": "get", "question_id": new_id()}) == "not_found"
    with host.database.read() as uow:
        host.runtime.domains.context_questions.prepare(
            host.runtime.application.current_context(learner, uow), {"action": "create", "text": ASK},
            InvocationOptions(idempotency_key="preview-key"), uow)
    assert not path.exists() and not host.context.runtime.joinpath("questions").exists()
    path.touch()
    assert QuestionStore(path).list() == {"items": [], "next_offset": None} and path.stat().st_size == 0
