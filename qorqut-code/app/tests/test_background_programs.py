from collections import deque
from dataclasses import replace
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.background.context import clean_summary, paragraphs, parse_outline
from bazarlyk.background.integration import attach_background
from qorqut_operations.background.programs import ProgramRegistry
from bazarlyk.background.providers import ProviderError
from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.domains import register_business_domains
from context_ports import context_ports
from bazarlyk.domains.learnings import analysis
from bazarlyk.policy import PolicyResolver
from qorqut_operations.signoffs import SignoffService
from qorqut_operations.storage.executor import PreparedFileExecutor
from bazarlyk.storage.files import FileApplier
from foundation_support import BUSINESS, HUMAN, OTHER, context, database_at


class Model:
    config = SimpleNamespace(model="deepseek-v4-flash")

    def __init__(self):
        self.answers, self.calls = deque(), []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        answer = self.answers.popleft()
        if callable(answer):
            answer = answer(messages)
        return {"content": json.dumps(answer) if isinstance(answer, dict) else answer}, {
            "prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 20}


@pytest.fixture
def runtime(tmp_path):
    db = database_at(tmp_path)
    for slug in ("first", "second"):
        (tmp_path / slug / "hq").mkdir(parents=True)
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
    registry, programs = builtin_registry(), ProgramRegistry()
    policy = PolicyResolver(registry)
    app = Application(db, registry, policy=policy, context_resolver=policy.refresh_context)
    domains = register_business_domains(registry, app, tmp_path, job_programs=programs, catalog=None, agent_resolver=None, **context_ports())
    files = FileApplier(db, domains.resolve_file_target)
    app.file_executor = PreparedFileExecutor(db, files)
    value = SimpleNamespace(database=db, registry=registry, policy=policy, application=app,
        domains=domains, file_applier=files, root=tmp_path, background=None, recovery_quiescer=None)
    model, sources = Model(), []
    attach_background(value, programs, model_client_factory=lambda biz: model,
        transcript_source=lambda biz, agent=None: iter(sources))
    with db.transaction() as uow:
        value.background.authority.register_workers(uow, [BUSINESS, OTHER])
    value.signoffs = SignoffService(app, file_applier=files).register_operations()
    value.model, value.sources = model, sources
    return value


def invoke(runtime, operation, args, revision=None):
    result = runtime.application.invoke(context(), operation, args,
        {"idempotency_key": new_id(), **({"expected_revision": revision} if revision is not None else {})})
    assert result.status in {"ok", "accepted"}, result.to_dict()
    return result.data


def job(runtime, kind):
    with runtime.database.read() as uow:
        return next(row for row in uow.repository("jobs", business_id=BUSINESS).list() if row["kind"] == kind)


def test_filer_keeps_upload_bytes_and_one_actual_log(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
    body = "Ada wants a pilot.\n\nSend the proposal on Friday.\n"
    runtime.model.answers.append({"notes": [{"title": "Pilot", "topic_id": topic["id"], "paragraph_ids": [0, 1],
        "summary": "Ada wants a pilot and proposal."}]})
    invoke(runtime, "context_upload", {"body": body, "title": "Pilot notes"})
    queued = job(runtime, "context.file")
    outcome = runtime.background.run_one(queued["id"])
    assert outcome.get("state") == "succeeded", outcome
    with runtime.database.read() as uow:
        notes = [row for row in uow.repository("documents", business_id=BUSINESS).list() if row["kind"] == "context_note"]
        assert len(notes) == 1
        text = runtime.domains.documents.store.view(notes[0], uow)
        assert text["body"] == body
        assert text["metadata"]["part"] == "1/1"
        assert len(uow.repository("action_logs", business_id=BUSINESS).list()) == 1
        assert not uow.repository("tasks", business_id=BUSINESS).list()
        assert not uow.repository("signoffs", business_id=BUSINESS).list()
    assert len(runtime.model.calls) == 1


def test_filer_rejects_missing_paragraph_and_records_failure(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
    runtime.model.answers.extend([{"notes": [{"title": "Incomplete", "topic_id": topic["id"], "paragraph_ids": [0]}]}] * 2)
    invoke(runtime, "context_upload", {"body": "First.\n\nDo not lose this.", "title": "Full source"})
    outcome = runtime.background.run_one(job(runtime, "context.file")["id"])
    assert outcome.get("state") == "failed", outcome
    with runtime.database.read() as uow:
        docs = uow.repository("documents", business_id=BUSINESS).list()
        assert not [row for row in docs if row["kind"] == "context_note"]
        source = next(row for row in docs if row["kind"] == "context_upload")
        assert "paragraph" in source["metadata_json"]["error"]
        assert not uow.repository("action_logs", business_id=BUSINESS).list()


def test_source_changed_during_model_call_never_applies_summary(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
    note = invoke(runtime, "context_notes_create", {"topic_id": topic["id"], "title": "Notes", "body": "Original fact."})["document"]
    def answer(messages):
        (runtime.root / "first/hq" / note["path"]).write_text("Owner changed this while the model ran.")
        return "Original fact."
    runtime.model.answers.append(answer)
    invoke(runtime, "context_summarize", {"document_id": note["id"]}, 1)
    outcome = runtime.background.run_one(job(runtime, "context.summarize")["id"])
    assert outcome["error"]["code"] in {"document_conflict", "revision_conflict"}, outcome
    assert (runtime.root / "first/hq" / note["path"]).read_text() == "Owner changed this while the model ran."
    with runtime.database.read() as uow:
        assert uow.repository("documents", business_id=BUSINESS).get(note["id"])["revision"] == 1
        assert job(runtime, "context.summarize")["result_json"]["calls"]


def test_summary_and_map_cleaning():
    assert clean_summary("Summary: The note says that\n- a useful fact exists.", "context_note") == "A useful fact exists."
    assert len(clean_summary("word " * 100, "context_note").split()) == 30
    outline = parse_outline("Central idea\n- Branch\n  - Detail")
    assert outline[0]["children"][0]["children"][0]["text"] == "Detail"
    with pytest.raises(DomainError):
        parse_outline("Root\n    - Skipped levels")
    source = "\n\nFirst\n\n\nSecond\n\n"
    assert "".join(paragraphs(source)) == source


def test_all_background_handlers_are_registered(runtime):
    for name in ("routines_list", "routines_get", "routines_preview", "routines_runs", "routines_costs", "routines_create",
            "routines_update", "routines_delete", "routines_pause", "routines_resume", "routines_run", "routines_stop",
            "learnings_list", "learnings_get", "learnings_extract", "learnings_review", "learnings_edit", "jobs_get"):
        descriptor = runtime.registry.get(name)
        assert descriptor.read or descriptor.prepare


def transcript(runtime, *, business_id=BUSINESS, owner="Always keep source quotes."):
    agent = new_id()
    with runtime.database.transaction() as uow:
        uow.repository("principals").insert({"id": agent, "kind": "agent", "display_name": "Business agent"})
        uow.repository("business_memberships", business_id=business_id).insert({"principal_id": agent})
        uow.repository("agent_profiles", business_id=business_id).insert({"agent_id": agent, "principal_id": agent,
            "home_business_id": business_id, "namespace": business_id, "name": "business-agent",
            "workspace": "first/hq/agents/business-agent", "profile_json": {"role": {"id": "business-agent"}}})
    session = new_id()
    content = "\n".join(json.dumps(value) for value in [
        {"type": "user", "sessionId": session, "uuid": new_id(), "timestamp": "2026-09-08T12:00:00Z",
         "message": {"content": owner}},
        {"type": "assistant", "sessionId": session, "uuid": new_id(), "timestamp": "2026-09-08T12:00:01Z",
         "message": {"content": [{"type": "text", "text": "I will retain the quotes."}]}},
    ])
    source = {"session_id": session, "source_id": "agents/business-agent/sessions/selected.jsonl", "agent_id": agent,
        "agent_kind": "business-agent", "content": content, "transcript_hash": hashlib.sha256(content.encode()).hexdigest()}
    runtime.sources.append(source)
    return source


def raw_learning(quote="Always keep source quotes."):
    return {"bucket": "delta", "tag": "rule", "scope": "biz", "scope_reason": "Owner's business workflow",
        "said_by": "owner", "text": "Retain source quotations.", "from_correction": False, "confidence": 0.9,
        "evidence": {"turns": ["T1"], "owner_quote": quote, "agent_quote": None}}


def test_learning_extraction_provenance_review_and_replay(runtime):
    source = transcript(runtime)
    runtime.model.answers.append({"items": [raw_learning()]})
    queued = invoke(runtime, "learnings_extract", {"passes": 1})
    outcome = runtime.background.run_one(queued["job_id"])
    assert outcome.get("state") == "succeeded", outcome
    with runtime.database.read() as uow:
        row = uow.repository("learning_candidates", business_id=BUSINESS).list()[0]
        assert row["candidate_json"]["evidence"]["transcript"] == source["source_id"]
        assert row["candidate_json"]["evidence"]["session"] == source["session_id"]
        assert uow.repository("learning_scan_state", business_id=BUSINESS).get(source["session_id"])["transcript_hash"] == source["transcript_hash"]
        assert not uow.repository("documents", business_id=BUSINESS).list()
    review = invoke(runtime, "learnings_review", {"learning_id": row["id"], "state": "accepted", "feedback": "  Keep exactly.\n"}, 1)
    assert review["item"]["decision"]["note"] == "  Keep exactly.\n"
    edited = invoke(runtime, "learnings_edit", {"learning_id": row["id"], "changes": {"text": "Keep verbatim source evidence."}}, 2)
    assert edited["item"]["state"] == "accepted"
    assert edited["item"]["text"] == "Keep verbatim source evidence."
    reopened = invoke(runtime, "learnings_review", {"learning_id": row["id"], "state": "open"}, 3)
    assert reopened["item"]["state"] == "open"
    second = invoke(runtime, "learnings_extract", {"passes": 1})
    assert runtime.background.run_one(second["job_id"])["state"] == "succeeded"
    assert len(runtime.model.calls) == 1


def test_active_transcript_growth_reprocesses_whole_snapshot_and_then_skips(runtime):
    source = transcript(runtime)
    original_hash = source["transcript_hash"]
    def grow_during_extraction(messages):
        source["content"] += "\n" + json.dumps({"type": "user", "sessionId": source["session_id"], "uuid": new_id(),
            "timestamp": "2026-09-08T12:01:00Z", "message": {"content": "Also preserve every final decision."}})
        source["transcript_hash"] = hashlib.sha256(source["content"].encode()).hexdigest()
        return {"items": []}
    runtime.model.answers.append(grow_during_extraction)
    runtime.background.is_live = lambda _: pytest.fail("Active agents must never delay extraction")
    first = invoke(runtime, "learnings_extract", {"passes": 1})
    assert runtime.background.run_one(first["job_id"])["state"] == "succeeded"
    with runtime.database.read() as uow:
        assert uow.repository("learning_scan_state", business_id=BUSINESS).get(source["session_id"])["transcript_hash"] == original_hash
    runtime.model.answers.append({"items": []})
    second = invoke(runtime, "learnings_extract", {"passes": 1})
    assert runtime.background.run_one(second["job_id"])["state"] == "succeeded"
    prompt = json.dumps(runtime.model.calls[-1][0])
    assert "Always keep source quotes." in prompt
    assert "Also preserve every final decision." in prompt
    third = invoke(runtime, "learnings_extract", {"passes": 1})
    assert runtime.background.run_one(third["job_id"])["state"] == "succeeded"
    assert len(runtime.model.calls) == 2


def test_learning_rejects_fabricated_owner_quote(runtime):
    transcript(runtime)
    runtime.model.answers.append({"items": [raw_learning("The owner never said this.")]})
    queued = invoke(runtime, "learnings_extract", {"passes": 1})
    outcome = runtime.background.run_one(queued["job_id"])
    assert outcome.get("state") == "failed", outcome
    with runtime.database.read() as uow:
        assert not uow.repository("learning_candidates", business_id=BUSINESS).list()
        run = uow.repository("learning_runs", business_id=BUSINESS).list()[0]
        assert "OWNER block" in run["result_json"]["errors"][0]["error"]


        assert not uow.repository("learning_scan_state", business_id=BUSINESS).list()

def test_transcript_adapter_cannot_return_other_business(runtime):
    transcript(runtime, business_id=OTHER)
    queued = invoke(runtime, "learnings_extract", {"passes": 1})
    outcome = runtime.background.run_one(queued["job_id"])
    assert outcome["error"]["code"] == "forbidden", outcome
    assert not runtime.model.calls


def test_late_provider_usage_is_retained_while_output_is_refused(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
    note = invoke(runtime, "context_notes_create", {"topic_id": topic["id"], "title": "Notes", "body": "Original fact."})["document"]
    def late(messages):
        raise ProviderError("provider_timeout", "Completion arrived after the budget", model="deepseek-v4-flash",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 20})
    runtime.model.answers.append(late)
    invoke(runtime, "context_summarize", {"document_id": note["id"]}, 1)
    queued = job(runtime, "context.summarize")
    outcome = runtime.background.run_one(queued["id"])
    assert outcome["error"]["code"] == "provider_timeout"
    with runtime.database.read() as uow:
        stored = uow.repository("jobs", business_id=BUSINESS).get(queued["id"])
        assert stored["result_json"]["calls"][0]["usage"]["out"] == 50
        assert stored["result_json"]["calls"][0]["usd"] > 0
        assert uow.repository("documents", business_id=BUSINESS).get(note["id"])["revision"] == 1


def test_learning_soft_budget_stops_additional_calls(runtime):
    transcript(runtime)
    runtime.model.answers.append({"items": [raw_learning()]})
    queued = invoke(runtime, "learnings_extract", {"passes": 3, "max_usd": 0.000000001})
    outcome = runtime.background.run_one(queued["job_id"])
    assert outcome.get("state") == "succeeded", outcome
    assert len(runtime.model.calls) == 1
    with runtime.database.read() as uow:
        run = uow.repository("learning_runs", business_id=BUSINESS).list()[0]
        assert run["result_json"]["cost_usd"] > run["result_json"]["max_usd"]
        assert "Soft model budget" in run["result_json"]["errors"][0]["error"]


def test_registered_repository_status_is_real_read_only_work(runtime):
    hq = runtime.root / "first/hq"
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=hq, check=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    (hq / "draft.txt").write_text("Uncommitted owner draft")
    with runtime.database.transaction() as uow:
        values = runtime.background.programs.job_values(context(), "repository.status", {}, uow)
        queued = uow.repository("jobs", business_id=BUSINESS).insert(values)
    result = runtime.background.run_one(queued["id"])
    assert result.get("state") == "succeeded", result
    with runtime.database.read() as uow:
        saved = uow.repository("jobs", business_id=BUSINESS).get(queued["id"])
        assert "?? draft.txt" in saved["result_json"]["data"]["stdout"]
        assert saved["result_json"]["outputs"] == []
    assert (hq / "draft.txt").read_text() == "Uncommitted owner draft"
def test_nightly_registration_is_explicit_root_only_and_has_no_assembly_effects():
    from types import SimpleNamespace
    from bazarlyk.background.integration import register_programs
    from qorqut_operations.background.programs import ProgramRegistry
    from qorqut_operations.contracts import DomainError

    def maintenance(control, arguments):
        raise AssertionError("Assembly must not start maintenance")

    assert register_programs(ProgramRegistry()).get("maintenance.nightly") is None
    programs = register_programs(ProgramRegistry(), maintenance=SimpleNamespace(run=maintenance))
    program = programs.require("maintenance.nightly")
    assert program.scope_kinds == ("global",) and program.timeout_seconds == 1800
    assert program.outputs == () and dict(program.permissions) == {}
    assert program.external and program.run is program.recover is maintenance
    program.validate({})
    with pytest.raises(DomainError):
        program.validate({"command": ["git", "push"]})


def test_notes_snapshot_stays_whole_through_filing_and_retry(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Field notes", "slug": "field-notes"})["document"]
    note = invoke(runtime, "notes_create", {"title": "Pilot", "body": "- Completed\u2028\u2028With details\nIndependent prose."})["document"]
    invoke(runtime, "notes_update", {"document_id": note["id"], "metadata": {"statuses": {"Completed\n\nWith details": "done"}}}, 1)
    invoke(runtime, "notes_upload", {"document_id": note["id"]}, 2)
    submitted = runtime.application.invoke(context(), "notes_get", {"document_id": note["id"]}).data["document"]
    upload_id = submitted["metadata"]["upload_id"]
    runtime.model.answers.extend([{"notes": []}] * 2)
    failed = runtime.background.run_one(job(runtime, "context.file")["id"])
    assert failed["state"] == "failed", failed
    upload = runtime.application.invoke(context(), "context_get", {"document_id": upload_id}).data["document"]
    assert upload["metadata"]["status"] == "failed"
    original = upload["body"]
    def answer(messages):
        data = json.loads(messages[-1]["content"])
        assert len(data["paragraphs"]) == 1
        assert data["paragraphs"][0]["text"] == original
        assert '<item status="done">Completed\n\nWith details</item>' in original
        return {"notes": [{"title": "Pilot", "paragraph_ids": [0], "topic_id": topic["id"]}]}
    runtime.model.answers.append(answer)
    invoke(runtime, "context_retry", {"upload_id": upload_id}, upload["revision"])
    with runtime.database.read() as uow:
        queued = next(row for row in uow.repository("jobs", business_id=BUSINESS).list() if row["kind"] == "context.file" and row["state"] == "queued")
    outcome = runtime.background.run_one(queued["id"])
    assert outcome["state"] == "succeeded", outcome
    processed = runtime.application.invoke(context(), "context_get", {"document_id": upload_id}).data["document"]
    assert processed["metadata"]["status"] == "processed"
    assert processed["body"] == original
    filed_id = processed["metadata"]["result"]["notes"][0]
    filed = runtime.application.invoke(context(), "context_get", {"document_id": filed_id}).data["document"]
    assert filed["body"] == original


@pytest.mark.parametrize("new_topic", [False, True])
def test_filer_can_file_into_an_existing_or_new_topic(runtime, new_topic):
    topic = invoke(runtime, "context_topics_create", {"title": "Launch", "slug": "launch"})["document"]
    destination = {"topic_key": "new"} if new_topic else {"topic_id": topic["id"]}
    answer = {"notes": [{"title": "Launch notes", "paragraph_ids": [0], **destination}]}
    if new_topic:
        answer["topics"] = [{"key": "new", "title": "Campaign", "slug": "campaign", "parent_id": topic["id"]}]
    runtime.model.answers.append(answer)
    invoke(runtime, "context_upload", {"body": "Launch research.", "title": "Research"})
    outcome = runtime.background.run_one(job(runtime, "context.file")["id"])
    assert outcome.get("state") == "succeeded", outcome
    with runtime.database.read() as uow:
        notes = [row for row in uow.repository("documents", business_id=BUSINESS).list() if row["kind"] == "context_note"]
        assert len(notes) == 1
        assert notes[0]["path"].startswith("context/launch/")
        assert runtime.domains.documents.store.view(notes[0], uow)["body"] == "Launch research."


def test_filer_corrects_person_as_topic_once(runtime):
    person = invoke(runtime, "context_people_create", {"name": "Murad"})["document"]
    topic = invoke(runtime, "context_topics_create", {"title": "Team", "slug": "team"})["document"]
    runtime.model.answers.append({"notes": [{"title": "Murad", "topic_id": person["id"], "paragraph_ids": [0]}]})
    def corrected(messages):
        correction = json.loads(messages[-1]["content"])
        assert "topic_id" in correction["validation_error"]
        return {"notes": [{"title": "Murad", "topic_id": topic["id"], "people_ids": [person["id"]], "paragraph_ids": [0]}]}
    runtime.model.answers.append(corrected)
    invoke(runtime, "context_upload", {"body": "Murad is a cofounder.", "title": "Team"})
    outcome = runtime.background.run_one(job(runtime, "context.file")["id"])
    assert outcome.get("state") == "succeeded", outcome
    assert len(runtime.model.calls) == 2


@pytest.mark.parametrize("field,value", [("topic_id", "person"), ("topic_key", "missing"), ("people_ids", ["topic"]), ("people_keys", ["missing"])])
def test_filer_rejects_invalid_references(field, value):
    from bazarlyk.background.context import ContextPrograms
    note = {"topic_id": "topic", field: value}
    if field == "topic_key":
        note.pop("topic_id")
    with pytest.raises(DomainError, match="") as error:
        ContextPrograms._validate_filing_references({"notes": [note]}, {"topics": [{"id": "topic"}], "people": [{"id": "person"}]})
    assert error.value.code == "invalid_model_output"


def test_filing_status_tracks_apply_failure_and_latest_retry(runtime):
    from bazarlyk.transport.compatibility import DashboardCompatibility
    topic = invoke(runtime, "context_topics_create", {"title": "Team", "slug": "team"})["document"]
    # Valid references but an invalid hierarchy: this fails during applying.
    answer = {"topics": [{"key": "duplicate", "title": "Team", "slug": "team"}],
        "notes": [{"title": "Team", "topic_id": topic["id"], "paragraph_ids": [0]}]}
    runtime.model.answers.append(answer)
    invoke(runtime, "context_upload", {"body": "Team notes.", "title": "Team"})
    first = job(runtime, "context.file")
    runtime.background.run_one(first["id"])
    upload_id = first["arguments_json"]["upload_id"]
    def state():
        with runtime.database.read() as uow:
            upload = uow.repository("documents", business_id=BUSINESS).get(upload_id)
            return upload, DashboardCompatibility.filing_status(upload, uow)
    upload, status = state()
    assert upload["metadata_json"]["status"] == "pending"
    assert status["state"] == "failed" and status["retryable"]
    assert "exists" in status["error"]
    invoke(runtime, "context_retry", {"upload_id": upload_id}, upload["revision"])
    _, status = state()
    assert status["state"] == "queued" and status["error"] is None and not status["retryable"]
    # Old attempt finishing later must not replace the newer attempt.
    with runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET updated_at='2099-01-01T00:00:00Z' WHERE id=?", (first["id"],))
    assert state()[1]["state"] == "queued"
    runtime.model.answers.append({"notes": [{"title": "Team", "topic_id": topic["id"], "paragraph_ids": [0]}]})
    outcome = runtime.background.run_one(status["job_id"])
    assert outcome.get("state") == "succeeded", outcome
    assert state()[1]["state"] == "processed"


def test_filing_schema_feedback_and_durable_rejected_response(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Team", "slug": "team"})["document"]
    bad = {"notes": [{"title": "Team", "topic_id": topic["id"], "paragraph_ids": [0]}],
        "tasks": [{"title": "Follow up", "source_quote": "Original facts.", "due": None}]}
    good = {"notes": [{"title": "Team", "topic_id": topic["id"], "paragraph_ids": [0]}]}
    runtime.model.answers.extend([bad, good])
    invoke(runtime, "context_upload", {"body": "Original facts.", "title": "Team"})
    queued = job(runtime, "context.file")
    assert runtime.background.run_one(queued["id"])["state"] == "succeeded"
    feedback = json.loads(runtime.model.calls[1][0][1]["content"])
    assert feedback["validation_error"] == "Filing cannot create tasks"
    assert feedback["previous_response"] == bad
    saved = job(runtime, "context.file")["result_json"]["data"]["validation_attempts"]
    assert saved[0]["response"] == bad
    assert saved[0]["error"]["field_errors"] == feedback["field_errors"]


def test_filing_invalid_output_retains_both_attempts_without_partial_notes(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Team", "slug": "team"})["document"]
    bad = {"notes": [{"title": "", "topic_id": topic["id"], "paragraph_ids": [0]}]}
    runtime.model.answers.extend([bad, bad])
    invoke(runtime, "context_upload", {"body": "Original facts.", "title": "Team"})
    queued = job(runtime, "context.file")
    assert runtime.background.run_one(queued["id"])["state"] == "failed"
    saved = job(runtime, "context.file")
    assert "notes.0.title" in saved["error_json"]["field_errors"]
    assert len(saved["result_json"]["data"]["validation_attempts"]) == 2
    with runtime.database.read() as uow:
        docs = uow.repository("documents", business_id=BUSINESS).list()
        assert not any(row["kind"] == "context_note" for row in docs)
        upload = next(row for row in docs if row["id"] == queued["arguments_json"]["upload_id"])
        assert runtime.domains.documents.store.view(upload, uow)["body"] == "Original facts."


@pytest.mark.parametrize("active_state", ["queued", "running", "applying", "waiting_signoff", "cancel_requested"])
def test_retry_reuses_active_filing_without_changing_upload(runtime, active_state):
    invoke(runtime, "context_upload", {"body": "Original facts.", "title": "Team"})
    queued = job(runtime, "context.file")
    upload_id = queued["arguments_json"]["upload_id"]
    with runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET state=? WHERE id=?", (active_state, queued["id"]))
        before = uow.repository("documents", business_id=BUSINESS).get(upload_id)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(runtime, "context_retry", {"upload_id": upload_id}, before["revision"]), range(2)))
    assert [r["job"]["id"] for r in results] == [queued["id"], queued["id"]]
    with runtime.database.read() as uow:
        assert uow.repository("documents", business_id=BUSINESS).get(upload_id) == before
        assert len(uow.repository("jobs", business_id=BUSINESS).list()) == 1


def test_simultaneous_retries_of_failed_upload_create_one_new_job(runtime):
    runtime.model.answers.extend([{"notes": []}, {"notes": []}])
    invoke(runtime, "context_upload", {"body": "Original facts.", "title": "Team"})
    first = job(runtime, "context.file")
    assert runtime.background.run_one(first["id"])["state"] == "failed"
    upload_id = first["arguments_json"]["upload_id"]
    with runtime.database.read() as uow:
        before = uow.repository("documents", business_id=BUSINESS).get(upload_id)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.application.invoke(context(), "context_retry",
            {"upload_id": upload_id}, {"idempotency_key": new_id(), "expected_revision": before["revision"]}), range(2)))
    accepted = [r for r in results if r.status == "accepted"]
    assert accepted
    for result in results:
        if result.status == "error":
            # The file-receipt transaction may still be completing. It must
            # refuse a second mutation; a subsequent request reuses the job.
            assert result.error["code"] in {"busy", "revision_conflict"}
        else:
            assert result.data["job"]["id"] == accepted[0].data["job"]["id"]
    again = invoke(runtime, "context_retry", {"upload_id": upload_id}, before["revision"])
    assert again["job"]["id"] == accepted[0].data["job"]["id"] != first["id"]
    with runtime.database.read() as uow:
        assert uow.repository("documents", business_id=BUSINESS).get(upload_id)["revision"] == before["revision"] + 1
        assert len(uow.repository("jobs", business_id=BUSINESS).list()) == 2


def test_story_upload_orients_filer_and_retains_link_after_filing(runtime):
    story = invoke(runtime, "stories_create", {"title": "PH launch", "purpose": "Reach developers", "situation": "Hunter confirmed"})["story"]
    invoke(runtime, "stories_create", {"title": "Unrelated private undertaking", "purpose": "Not part of this upload"})
    topic = invoke(runtime, "context_topics_create", {"title": "Launch", "slug": "launch"})["document"]
    body = "The launch page is ready."
    invoke(runtime, "context_upload", {"body": body, "story_ids": [story["id"]]})
    queued = job(runtime, "context.file")
    def respond(messages):
        data = json.loads(messages[-1]["content"])
        assert data["paragraphs"] == [{"id": 0, "text": body}]
        assert data["stories"] == [{"id": story["id"], "title": "PH launch", "state": "planned",
            "purpose": "Reach developers", "situation": "Hunter confirmed", "background": "", "revision": 1, "text_truncated": False}]
        assert not data["more_stories"]
        return {"notes": [{"title": "Launch ready", "topic_id": topic["id"], "paragraph_ids": [0], "summary": "The launch page is ready."}]}
    runtime.model.answers.append(respond)
    outcome = runtime.background.run_one(queued["id"])
    assert outcome["state"] == "succeeded", outcome
    with runtime.database.read() as uow:
        links = [edge for edge in uow.repository("relationships", business_id=BUSINESS).list() if edge["to_type"] == "story"]
        assert len(links) == 1 and links[0]["to_id"] == story["id"]
        source = uow.repository("documents", business_id=BUSINESS).get(links[0]["from_id"], required=True)
        assert source["kind"] == "context_dump"
        assert runtime.domains.documents.store.view(source, uow)["body"] == body
        assert not uow.repository("tasks", business_id=BUSINESS).list()
