"""Context completion for uploads, notes, instructions and registered jobs, end to end on a synthetic composed host.

Real: the host assembly and runtime (policy, application, file effects), the actual ``DocumentOperations`` with its
``ContextFiling``, the registered ``context.file`` / ``context.instruct`` programs and worker authority, the real
``BackgroundService`` executor, the Context integration's queue and ``context_filing`` storage, the terminal-outcome
contribution, the Context integration's ``ContextQueueDriver``, and a disposable real Context (real Git, the library's ``Pipeline``). Faked:
only the model runner injected into the Pipeline, the injected clock and the synthetic principals and files under a pytest
``tmp_path`` (see ``context_completion_support``). No model, provider, network or operational data is used.

Every case counts what must happen exactly once: model (propagation) calls, completion receipts, source versions/queue
inputs and Context events. Nothing here is skipped by design; the module is skipped only when the pinned Context library
cannot be imported."""

import hashlib
import json
import sqlite3

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_context.integration import filing as context_filing
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id

import context_completion_support as support
from context_completion_support import Runner, build

FILE_PATH = "foundation/documents/{}.txt"
INSTRUCTION_PATH = "foundation/instructions/{}.txt"


class ProcessStopped(BaseException):
    """Simulates losing the process without any normal error bookkeeping."""


class NoOutcomes:
    """A terminal-outcome contribution that records nothing: the gap mechanical recovery must close."""

    def record(self, uow, input_row):
        return None


@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


# -- helpers -----------------------------------------------------------------------------------------------------------

def shown(host, ctx, document_id):
    """The document as an authorized reader sees it: its metadata status and its applicable current filing."""
    return host.read(ctx, "context_get", {"document_id": document_id})["document"]


def source_file(host, document_id):
    return host.context.snapshot().files.get(FILE_PATH.format(document_id))


def envelope_of(raw):
    value = json.loads(raw.decode())
    assert value["format"] == "qorqut-source-v1" and set(value) == {"body", "format", "metadata"}
    return value


def settle(host, job_id=None):
    """Run the registered job once after its 15 second deferral has elapsed."""
    host.clock.advance(seconds=16)
    return host.run_job(job_id)


def job_of(host, document_id):
    return host.query("SELECT j.* FROM jobs j JOIN context_filings f ON f.job_id = j.id JOIN context_source_versions v "
                      "ON v.id = f.source_version_id WHERE v.source_id=? ORDER BY v.source_revision DESC", document_id)[0]


def counts(host):
    return {"versions": host.count("context_source_versions"), "filings": host.count("context_filings"),
            "inputs": host.count("context_inputs"), "jobs": host.count("jobs", "kind LIKE 'context.%'"),
            "receipts": host.count("context_completions"), "applied": host.count("context_completions", "state='applied'"),
            "model_calls": host.runner.propagations, "events": len([p for p in host.context.snapshot().events])}


def upload(host, body="Met Alice about the pilot.", *, agent=None, **arguments):
    data = host.upload(agent or host.agent(), body, **arguments)
    return data["document"], data["job"]["id"]


# -- the whole path ----------------------------------------------------------------------------------------------------

def test_an_upload_files_through_the_queue_and_its_job_settles_with_exactly_one_receipt(host):
    owner = host.human()
    document, job_id = upload(host, title="Pilot call")
    # Admission: one version, one filing, one registered job and one GENERAL queue input, created in the same operation.
    assert counts(host) | {"model_calls": 0, "events": 0} == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 0,
                                                              "applied": 0, "model_calls": 0, "events": 0}
    version = host.query("SELECT * FROM context_source_versions")[0]
    filing = host.query("SELECT * FROM context_filings")[0]
    queued = host.query("SELECT * FROM context_inputs")[0]
    assert (version["source_kind"], version["source_id"], version["source_revision"], version["operation"]) == ("document", document["id"], 1, "add")
    assert version["source_path"] == FILE_PATH.format(document["id"]) and queued["payload_json"]["path"] == version["source_path"]
    assert (filing["job_id"], filing["input_id"], filing["source_version_id"]) == (job_id, queued["id"], version["id"])
    assert host.job(job_id)["arguments_json"] == {"filing_id": filing["id"]} and queued["state"] == "queued"
    waiting = shown(host, owner, document["id"])
    assert waiting["metadata"]["status"] == "pending"
    assert waiting["filing"] == {"filing_id": filing["id"], "source_revision": 1, "input_id": queued["id"], "job_id": job_id, "state": "queued",
                                 "published_commit": None, "completion_state": None, "source_current": True, "error": None,
                                 "queue_position": 0, "document_revision": document["revision"], "operation": "add", "status": "pending",
                                 "attempts": 0, "job_state": "queued", "retryable": False}
    # The job calls no model and reads no body: while the input is queued it only defers.
    assert host.run_job(job_id) == {"job_id": job_id, "state": "queued"}
    deferred = host.job(job_id)
    assert deferred["state"] == "queued" and deferred["next_retry_at"] == "2026-10-12T12:00:15Z" and "outputs" not in (deferred["result_json"] or {})
    assert host.run_job() is None and host.runner.calls == []                           # not due, and no model has been called
    # The backend driver alone propagates the input; the bytes are the canonical envelope with the allowlisted metadata.
    published = host.step()
    assert published["status"] == "published" and host.runner.propagations == 1
    value = envelope_of(source_file(host, document["id"]))
    assert value["body"] == "Met Alice about the pilot."
    assert value["metadata"] == {"date": document["metadata"]["date"], "document_kind": "context_upload", "origin": "upload", "source": "upload",
                                 "title": "Pilot call", "uploaded_by": document["metadata"]["by"]}
    outcome = host.query("SELECT * FROM context_completions")[0]
    assert (outcome["outcome"], outcome["state"], outcome["filing_id"], outcome["published_commit"]) == ("published", "pending", filing["id"], published["published_commit"])
    between = shown(host, owner, document["id"])
    assert between["filing"]["state"] == "published" and between["filing"]["completion_state"] == "pending" and between["metadata"]["status"] == "pending"
    assert host.run_job() is None                                                         # still inside the 15 second deferral
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed"
    assert done["filing"] == {**between["filing"], "completion_state": "applied", "queue_position": None, "status": "processed",
                              "job_state": "succeeded", "document_revision": between["filing"]["document_revision"] + 1}
    marker = {key: value for key, value in done["metadata"]["context_filing"].items() if key != "completed_at"}
    assert marker == {"filing_id": filing["id"], "source_revision": 1, "published_commit": published["published_commit"],
                      "context_event_id": published["context_event_id"], "outcome": "published"}
    result = host.job(job_id)["result_json"]
    assert result["data"] == {"filed": True, "filing_id": filing["id"], "source_revision": 1, "context_event_id": published["context_event_id"],
                              "published_commit": published["published_commit"], "document_id": document["id"]}
    assert [item["operation"] for item in result["outputs"]] == ["context_filing_receipt"] and set(result["applied"]) == {"0"}
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 1, "events": 1}
    # Idempotent from here: nothing runs again and the recorded facts do not change.
    assert host.run_job() is None and host.run_job(job_id) is None and host.step() == {"status": "idle"}
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 1, "events": 1}
    # The job read carries the same filing to an authorized reader, and the owner's filing history is a projection of it.
    job_filing = host.read(owner, "jobs_get", {"job_id": job_id})["filing"]         # the job read keeps the completion fields only
    assert job_filing == {key: done["filing"][key] for key in job_filing} and "status" not in job_filing
    history = host.read(owner, "context_filings", {"upload_id": document["id"]})
    assert [item["filing_id"] for item in history["filings"]] == [filing["id"]]
    fact = history["filings"][0]
    assert fact["source"]["revision"] == 1 and fact["job"] == {"id": job_id, "program": "context.file", "version": 2, "state": "succeeded"}
    assert fact["publication"] == {"context_event_id": published["context_event_id"], "published_commit": published["published_commit"]}
    assert fact["completion"]["state"] == "applied" and fact["input"]["state"] == "published" and [a["outcome"] for a in fact["attempts"]] == ["published"]
    chain = host.read(owner, "context_upload_map", {"upload_id": document["id"]})
    assert chain["upload_id"] == document["id"] and chain["versions"] == [fact]
    assert chain["published"] == [{"context_event_id": published["context_event_id"], "published_commit": published["published_commit"],
                                   "source_revision": 1}]


# -- blocked, retried and discarded inputs -------------------------------------------------------------------------------

def test_a_blocked_input_keeps_the_job_pending_and_an_explicit_retry_completes_the_same_filing_once(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    blocked = host.step()
    assert blocked["status"] == "input_blocked" and host.runner.propagations == 1
    failed = shown(host, owner, document["id"])
    # A blocked input is still pending (the exact state and error say what is wrong); the retry hint is offered.
    assert failed["metadata"]["status"] == "pending" and failed["filing"]["state"] == "blocked" and failed["filing"]["completion_state"] is None
    assert failed["filing"]["status"] == "pending" and failed["filing"]["retryable"] is True and failed["filing"]["attempts"] == 1
    assert failed["filing"]["error"]["code"] == "propagation_failed" and failed["filing"]["queue_position"] == 0
    # A blocked attempt is not terminal: the job only defers, no receipt exists and no output is frozen.
    assert settle(host) == {"job_id": job_id, "state": "queued"}
    assert host.job(job_id)["state"] == "queued" and host.count("context_completions") == 0 and "outputs" not in (host.job(job_id)["result_json"] or {})
    assert host.step()["status"] == "blocked"                                              # nothing is retried without a request
    # The originating agent asks for a retry: the same filing, job and version, with the queue's own control.
    host.runner.fail = None
    retried = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert retried["filing"]["filing_id"] == failed["filing"]["filing_id"] and retried["filing"]["state"] == "blocked"
    assert host.query("SELECT intent, status FROM context_input_controls") == [{"intent": "retry", "status": "pending"}]
    assert host.step()["status"] == "published" and host.runner.propagations == 2
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["state"] == "published" and done["filing"]["completion_state"] == "applied"
    attempts = host.query("SELECT attempt, outcome FROM context_input_attempts ORDER BY attempt")
    assert attempts == [{"attempt": 1, "outcome": "blocked"}, {"attempt": 2, "outcome": "published"}]
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 2, "events": 1}


def test_a_discard_before_any_event_fails_the_job_with_the_discard_and_never_publishes(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    input_id = host.query("SELECT id FROM context_inputs")[0]["id"]
    assert host.data(owner, "context_queue_discard", {"input_id": input_id, "expected_state": "queued"})["result"]["state"] == "discarded"
    receipt = host.query("SELECT * FROM context_completions")[0]
    assert (receipt["outcome"], receipt["state"], receipt["context_event_id"], receipt["published_commit"]) == ("discarded", "pending", None, None)
    assert host.step() == {"status": "idle"} and host.runner.calls == [] and source_file(host, document["id"]) is None
    assert settle(host) == {"job_id": job_id, "state": "failed"}
    job = host.job(job_id)
    assert job["state"] == "failed" and job["error_json"]["code"] == "context_discarded" and job["error_json"]["retryable"] is False
    after = shown(host, owner, document["id"])
    assert after["metadata"]["status"] == "failed" and after["filing"]["state"] == "discarded" and after["filing"]["completion_state"] == "applied"
    assert after["filing"]["published_commit"] is None and after["filing"]["error"] is not None
    # A discarded filing is final: a retry is refused and only a changed source is filed again.
    assert host.error(agent, "context_retry", {"upload_id": document["id"]}) == "invalid_transition"
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 0, "events": 0}


def test_a_source_edited_while_waiting_publishes_the_frozen_version_and_files_the_edit_as_the_next_version(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, "First body.", agent=agent, title="Draft")
    edited = host.data(owner, "context_document_update", {"document_id": document["id"], "body": "Second body.",
                                                          "expected_content_hash": document["content_hash"]},
                       expected_revision=document["revision"])["document"]
    # The edited source has no applicable filing yet; the superseded version's own filing says it is not current.
    stale = shown(host, owner, document["id"])
    assert stale["filing"]["state"] == "unfiled" and stale["filing"]["filing_id"] is None and stale["metadata"]["status"] == "pending"
    superseded = host.read(owner, "jobs_get", {"job_id": first_job})["filing"]
    assert superseded["state"] == "queued" and superseded["source_current"] is False and superseded["source_revision"] == 1
    # The queue froze the first bytes, so that is exactly what is published.
    assert host.step()["status"] == "published" and envelope_of(source_file(host, document["id"]))["body"] == "First body."
    assert settle(host) == {"job_id": first_job, "state": "succeeded"}
    after_first = shown(host, owner, document["id"])
    # The receipt belongs to version 1; it must not mark the edited document processed.
    assert after_first["metadata"]["status"] == "pending" and after_first["filing"]["state"] == "unfiled"
    assert host.query("SELECT state FROM context_completions") == [{"state": "applied"}]
    # The edit is filed explicitly as the next version of the same source, with its own job and input.
    retried = host.data(agent, "context_retry", {"upload_id": document["id"]})
    second_job = retried["job"]["id"]
    assert second_job != first_job and retried["filing"]["source_revision"] == 2
    assert host.query("SELECT source_revision, operation FROM context_source_versions ORDER BY source_revision") == [
        {"source_revision": 1, "operation": "add"}, {"source_revision": 2, "operation": "edit"}]
    assert host.step()["status"] == "published" and envelope_of(source_file(host, document["id"]))["body"] == "Second body."
    assert settle(host) == {"job_id": second_job, "state": "succeeded"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["source_revision"] == 2 and done["filing"]["source_current"] is True
    assert counts(host) == {"versions": 2, "filings": 2, "inputs": 2, "jobs": 2, "receipts": 2, "applied": 2, "model_calls": 2, "events": 2}
    # A replay of the retry while the edit is already filed creates nothing.
    host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert counts(host) == {"versions": 2, "filings": 2, "inputs": 2, "jobs": 2, "receipts": 2, "applied": 2, "model_calls": 2, "events": 2}
    assert edited["revision"] == document["revision"] + 1


# -- what is, and is not, a change of source -------------------------------------------------------------------------------

def edit_stored(host, document_id, **changes):
    """A trusted writer edits stored document metadata directly (there is no operation that edits an upload's metadata)."""
    with host.database.transaction() as uow:
        for key, value in changes.items():
            uow.connection.execute("UPDATE documents SET metadata_json = json_set(metadata_json, '$.' || ?, json(?)) WHERE id=?",
                                   (key, json.dumps(value), document_id))


def test_bookkeeping_never_changes_a_source_but_a_title_or_participant_change_files_a_new_version(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, "Same body.", agent=agent, title="Original")
    host.step()
    assert settle(host) == {"job_id": first_job, "state": "succeeded"}
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed"
    baseline = counts(host)
    # Status, attempts, errors and timestamps are bookkeeping: the digest, the current flag and the version count stand still.
    edit_stored(host, document["id"], status="failed", attempts=3, error="boom", last_attempt="yesterday", processed="no")
    same = shown(host, owner, document["id"])
    assert same["filing"]["source_current"] is True and same["filing"]["state"] == "published"
    retry = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert retry["job"] == {"id": first_job, "state": "succeeded"} and counts(host) == baseline
    # A metadata-only change of an allowlisted field is a different source with the same body: the next version.
    edit_stored(host, document["id"], title="Renamed")
    renamed = shown(host, owner, document["id"])
    assert renamed["filing"]["state"] == "unfiled" and renamed["metadata"]["status"] == "pending"
    second = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert second["filing"]["source_revision"] == 2 and second["job"]["id"] != first_job
    assert host.step()["status"] == "published" and settle(host) == {"job_id": second["job"]["id"], "state": "succeeded"}
    value = envelope_of(source_file(host, document["id"]))
    assert value["body"] == "Same body." and value["metadata"]["title"] == "Renamed" and "participants" not in value["metadata"]
    # A participant change is a change too; the stated name is frozen exactly as normalized.
    edit_stored(host, document["id"], participants=[{"name": "  Alice "}, {"name": "Bob"}])
    assert shown(host, owner, document["id"])["filing"]["state"] == "unfiled"
    third = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert third["filing"]["source_revision"] == 3
    assert host.step()["status"] == "published" and settle(host) == {"job_id": third["job"]["id"], "state": "succeeded"}
    assert envelope_of(source_file(host, document["id"]))["metadata"]["participants"] == [{"name": "Alice"}, {"name": "Bob"}]
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed"
    revisions = host.query("SELECT source_revision, operation FROM context_source_versions ORDER BY source_revision")
    assert revisions == [{"source_revision": 1, "operation": "add"}, {"source_revision": 2, "operation": "edit"},
                         {"source_revision": 3, "operation": "edit"}]
    assert counts(host) == {"versions": 3, "filings": 3, "inputs": 3, "jobs": 3, "receipts": 3, "applied": 3, "model_calls": 3, "events": 3}


def test_the_envelope_allowlist_is_closed_and_the_source_limit_has_a_useful_error(tmp_path):
    from qorqut_context.integration.filing import filing as module

    stored = {key: "x" for key in (*module.DOCUMENT_FIELDS, *module.EXCLUDED_BOOKKEEPING)}
    produced = module.document_metadata("context_upload", stored, [{"resource_type": "documents", "resource_id": "d"}], [{"name": "A"}])
    assert set(produced) == {"document_kind", "participants", "references", *module.DOCUMENT_FIELDS.values()}
    assert set(produced).isdisjoint(set(module.EXCLUDED_BOOKKEEPING) - set(module.DOCUMENT_FIELDS))
    assert set(module.DOCUMENT_METADATA) == set(produced) and set(module.INSTRUCTION_METADATA) >= {"instructed_by", "scope"}
    small = build(tmp_path, source_bytes=400)
    agent = small.agent()
    refused = small.call(agent, "context_upload", {"body": "x" * 1000, "title": "Large"})
    assert refused.status == "error" and refused.error["code"] == "source_too_large"
    assert "400" in refused.error["message"] and "bytes" in refused.error["message"]
    assert small.count("documents", "kind='context_upload'") == 0 and small.count("jobs", "kind LIKE 'context.%'") == 0
    assert small.count("context_inputs") == 0 and small.count("context_source_versions") == 0
    assert small.data(agent, "context_upload", {"body": "short"})["job"]["state"] == "queued"


# -- authority -----------------------------------------------------------------------------------------------------------

def worker_context(host, job_id):
    with host.database.read() as uow:
        return host.background.context_for(host.job(job_id), uow)


def test_the_receipt_operation_is_worker_only_and_applies_only_the_receipt_of_its_own_filing(host):
    owner, agent = host.human(), host.agent()
    first, first_job = upload(host, "One.", agent=agent)
    second, second_job = upload(host, "Two.", agent=agent)
    assert host.step()["status"] == "published" and host.step()["status"] == "published"
    receipts = {row["job_id"]: row["id"] for row in host.query("SELECT id, job_id FROM context_completions")}
    assert set(receipts) == {first_job, second_job}
    # Neither the originating agent nor the owner can apply a receipt; the operation is exposed to workers only.
    for caller in (agent, owner):
        result = host.call(caller, "context_filing_receipt", {"receipt_id": receipts[first_job]})
        assert result.status == "error" and result.error["code"] in {"operation_unavailable", "forbidden", "policy_denied"}
    # The other filing's worker is refused with the first receipt, and nothing about either document changes.
    wrong = host.call(worker_context(host, second_job), "context_filing_receipt", {"receipt_id": receipts[first_job]})
    assert wrong.status == "error" and wrong.error["code"] in {"forbidden", "policy_denied"}
    with host.database.read() as uow, pytest.raises(DomainError) as refused:
        context_filing.completion.authorize(uow, worker_context(host, second_job), receipts[first_job])
    assert refused.value.code == "forbidden"                                     # the receipt belongs to another job's filing
    assert host.query("SELECT state FROM context_completions") == [{"state": "pending"}, {"state": "pending"}]
    assert shown(host, owner, first["id"])["metadata"]["status"] == "pending"
    # A worker of the other business cannot apply it either, even for a receipt of the right job.
    elsewhere = InvocationContext(Principal(host.job(first_job)["principal_id"], "system"), business_id=host.ids["second"], source="worker",
                                  job_id=first_job, initiated_by_principal_id=host.job(first_job)["initiated_by_principal_id"])
    assert host.call(elsewhere, "context_filing_receipt", {"receipt_id": receipts[first_job]}).status == "error"
    assert shown(host, owner, first["id"])["metadata"]["status"] == "pending"
    # Applying a receipt is the job's own frozen output: a worker whose job has not frozen it yet is not authorized at all.
    early = host.call(worker_context(host, first_job), "context_filing_receipt", {"receipt_id": receipts[first_job]})
    assert early.status == "error" and early.error["code"] == "policy_denied"
    assert host.run_job(first_job) == {"job_id": first_job, "state": "succeeded"}
    assert shown(host, owner, first["id"])["metadata"]["status"] == "processed" and shown(host, owner, second["id"])["metadata"]["status"] == "pending"
    revision = shown(host, owner, first["id"])["revision"]
    # A second application of the same receipt, even by its own worker, changes nothing.
    host.call(worker_context(host, first_job), "context_filing_receipt", {"receipt_id": receipts[first_job]})
    assert shown(host, owner, first["id"])["revision"] == revision and counts(host)["applied"] == 1


def test_revoked_authority_cannot_file_retry_or_complete_and_a_restored_worker_finishes_the_same_job(host):
    owner, agent = host.human(), host.agent()
    other = host.agent("second")                                  # the same global agent, selecting another business
    document, job_id = upload(host, agent=agent)
    host.step()
    worker = host.job(job_id)["principal_id"]
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (worker,))
    outcome = settle(host, job_id)
    assert outcome["error"]["code"] and host.job(job_id)["state"] == "failed"
    pending = host.query("SELECT state FROM context_completions")
    assert pending == [{"state": "pending"}] and shown(host, owner, document["id"])["metadata"]["status"] == "pending"
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (worker,))
    # The failed job holds an unapplied receipt: the explicit retry requeues that same job, it makes no second filing.
    requeued = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert requeued["job"] == {"id": job_id, "state": "queued"} and counts(host)["jobs"] == 1
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed" and counts(host)["receipts"] == 1
    # A selection of another business has no authority over this upload or its retraction; the owner alone retracts.
    assert host.call(other, "context_retry", {"upload_id": document["id"]}).status == "error"
    assert host.call(agent, "context_retract", {"upload_id": document["id"]}).status == "error"
    assert counts(host)["versions"] == 1
    # A revoked agent can neither upload nor retry.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.agent_id(),))
    for operation, arguments in (("context_upload", {"body": "Late."}), ("context_retry", {"upload_id": document["id"]})):
        assert host.call(agent, operation, arguments).status == "error"
    assert counts(host)["versions"] == 1 and counts(host)["jobs"] == 1


# -- crashes -------------------------------------------------------------------------------------------------------------

def test_a_crash_after_publication_is_recorded_once_by_recovery_with_one_receipt_and_no_second_model_call(host):
    owner = host.human()
    document, job_id = upload(host)
    write, failures = host.driver._write, []

    def flaky(function, **arguments):
        if function is support.transitions.record_published and not failures:
            failures.append(1)
            raise sqlite3.OperationalError("disk I/O error")
        return write(function, **arguments)

    host.driver._write = flaky
    first = host.step()
    assert first["status"] == "unrecorded" and first["reason"] == "record_failed" and host.runner.propagations == 1
    assert host.query("SELECT state FROM context_inputs") == [{"state": "processing"}] and host.count("context_completions") == 0
    assert envelope_of(source_file(host, document["id"]))["body"] == "Met Alice about the pilot."          # published, not yet recorded
    assert settle(host) == {"job_id": job_id, "state": "queued"}                                           # nothing to complete yet
    old_runner = host.runner
    restarted = host.restart_driver()                                                                      # a fresh process
    recovered = restarted.recover()
    assert recovered["status"] == "published" and recovered["recovered"] is True
    assert host.runner.calls == [] and old_runner.propagations == 1 and host.count("context_completions") == 1
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed"
    assert host.restart_driver().recover() == {"status": "idle"} and host.count("context_completions") == 1


def test_a_missing_receipt_is_recorded_mechanically_atomically_and_idempotently(host):
    owner = host.human()
    document, job_id = upload(host)
    host.driver.outcomes = NoOutcomes()                                       # the outcome recording is lost with the process
    assert host.step()["status"] == "published"
    assert host.query("SELECT state FROM context_inputs") == [{"state": "published"}] and host.count("context_completions") == 0
    assert settle(host) == {"job_id": job_id, "state": "queued"}              # no receipt: the job still only defers
    restarted = host.restart_driver()
    assert restarted.reconcile() == {"recorded": 1, "discarded_for_cancelled_job": 0, "conflicts": 0}
    assert restarted.reconcile() == {"recorded": 0, "discarded_for_cancelled_job": 0, "conflicts": 0}
    assert host.count("context_completions") == 1 and host.runner.calls == []
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed"
    assert counts(host)["receipts"] == 1 and counts(host)["model_calls"] == 0


def test_a_crash_after_the_receipt_is_applied_finishes_the_job_without_applying_it_again(host):
    owner = host.human()
    document, job_id = upload(host)
    host.step()
    flaky = {"armed": True}
    finish = host.background._routine_finish

    def crash(row, uow):
        if flaky["armed"]:
            flaky["armed"] = False
            raise ProcessStopped()
        return finish(row, uow)

    host.background._routine_finish = crash
    with pytest.raises(ProcessStopped):
        settle(host)
    # The output was applied in its own transaction before the job could be marked finished.
    crashed = host.job(job_id)
    assert crashed["state"] == "applying" and set(crashed["result_json"]["applied"]) == {"0"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["completion_state"] == "applied"
    receipt = host.query("SELECT * FROM context_completions")[0]
    assert host.run_job(job_id) == {"job_id": job_id, "state": "succeeded"}
    after = shown(host, owner, document["id"])
    assert after["revision"] == done["revision"] and after["metadata"] == done["metadata"]               # not applied a second time
    assert host.query("SELECT * FROM context_completions")[0] == receipt and host.job(job_id)["state"] == "succeeded"
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 1, "events": 1}


# -- cancellation ------------------------------------------------------------------------------------------------------

def cancel(host, job_id):
    """The job is cancelled by a trusted path (no existing API cancels a non-routine job): only its stored state changes."""
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET state='cancelled', error_json=? WHERE id=?",
                               (json.dumps({"code": "cancelled", "message": "Owner stopped this job", "retryable": False}), job_id))


def test_a_cancelled_job_discards_its_unstarted_input_through_the_queue_and_never_runs_a_model(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    cancel(host, job_id)
    assert host.step() == {"status": "idle"}                                  # reconciliation discarded it before any Context work
    row = host.query("SELECT state, attempt, context_event_id, result_json FROM context_inputs")[0]
    assert row["state"] == "discarded" and row["attempt"] == 0 and row["context_event_id"] is None
    assert row["result_json"]["reason"] == "job_cancelled" and row["result_json"]["job_state"] == "cancelled"
    assert host.query("SELECT intent, status, requested_by FROM context_input_controls") == [
        {"intent": "discard", "status": "applied", "requested_by": host.driver_id}]
    receipt = host.query("SELECT outcome, state, application_json FROM context_completions")[0]
    assert receipt["outcome"] == "discarded" and receipt["state"] == "conflict" and receipt["application_json"]["code"] == "job_cancelled"
    after = shown(host, owner, document["id"])
    assert after["filing"]["state"] == "discarded" and after["filing"]["completion_state"] == "conflict"
    assert after["metadata"]["status"] == "failed" and source_file(host, document["id"]) is None and host.runner.calls == []
    assert host.run_job(job_id) is None and host.error(agent, "context_retry", {"upload_id": document["id"]}) == "invalid_transition"


def test_cancelling_after_publication_never_conceals_the_publication(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    published = host.step()
    assert published["status"] == "published"
    cancel(host, job_id)
    assert host.step() == {"status": "idle"}                                  # the receipt can never be applied by this job
    assert host.query("SELECT state FROM context_inputs") == [{"state": "published"}]
    receipt = host.query("SELECT outcome, state, application_json FROM context_completions")[0]
    assert receipt["outcome"] == "published" and receipt["state"] == "conflict" and receipt["application_json"]["code"] == "job_cancelled"
    after = shown(host, owner, document["id"])
    # The publication stays visible: its commit and Context file exist, whatever happened to the job.
    assert after["filing"]["state"] == "published" and after["filing"]["published_commit"] == published["published_commit"]
    assert after["filing"]["completion_state"] == "conflict" and after["metadata"]["status"] == "failed"
    assert envelope_of(source_file(host, document["id"]))["body"] == "Met Alice about the pilot."
    assert host.run_job(job_id) is None and host.error(agent, "context_retry", {"upload_id": document["id"]}) == "invalid_transition"
    assert host.query("SELECT state FROM context_completions") == [{"state": "conflict"}]
    assert counts(host)["model_calls"] == 1 and counts(host)["events"] == 1 and counts(host)["applied"] == 0


def test_a_cancelled_job_with_a_blocked_head_requests_the_discard_and_the_driver_completes_it(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked"
    cancel(host, job_id)
    result = host.step()                                                      # reconciliation requests it, the driver finishes it
    assert result["status"] == "discarded" and host.query("SELECT state FROM context_inputs") == [{"state": "discarded"}]
    assert host.query("SELECT outcome, state FROM context_completions") == [{"outcome": "discarded", "state": "pending"}]
    assert host.step() == {"status": "idle"}                                  # the next reconciliation marks the cancelled job's receipt
    assert host.query("SELECT outcome, state FROM context_completions") == [{"outcome": "discarded", "state": "conflict"}]
    assert shown(host, owner, document["id"])["filing"]["state"] == "discarded" and source_file(host, document["id"]) is None
    assert counts(host)["model_calls"] == 1 and host.step() == {"status": "idle"}


def requested_controls(host):
    return host.query("SELECT id, intent, status, requested_by, result_json FROM context_input_controls ORDER BY sequence")


def test_a_blocked_cancellation_keeps_its_evidence_across_a_restart_and_replays_without_a_model_call(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked" and host.runner.propagations == 1
    event_id = host.query("SELECT context_event_id FROM context_inputs")[0]["context_event_id"]
    cancel(host, job_id)
    assert host.driver.reconcile()["discarded_for_cancelled_job"] == 1                  # requested, not yet resolved
    (control,) = requested_controls(host)
    evidence = control["result_json"]["request_evidence"]
    assert (control["intent"], control["status"], control["requested_by"]) == ("discard", "pending", host.driver_id)
    assert evidence == {"reason": "job_cancelled", "job_id": job_id, "job_state": "cancelled", "requested_by": host.driver_id,
                        "job_error": {"code": "cancelled", "message": "Owner stopped this job", "retryable": False}}
    assert host.query("SELECT state FROM context_inputs") == [{"state": "discard_requested"}]
    assert host.query("SELECT COUNT(*) AS n FROM context_completions") == [{"n": 0}]
    host.restart_driver()                                                               # the process is lost after the request
    assert host.driver.reconcile()["discarded_for_cancelled_job"] == 0 and requested_controls(host) == [control]
    assert host.step() == {"status": "discarded", "input_id": host.query("SELECT id FROM context_inputs")[0]["id"],
                           "context": "draft_discarded"}
    resolved = {"context_event_id": event_id, "context": "draft_discarded", "mechanical": True, "outcome": "discarded",
                "request_evidence": evidence, "control_id": control["id"]}
    (final,) = requested_controls(host)
    receipt = host.query("SELECT * FROM context_completions")[0]
    assert final["status"] == "applied" and final["result_json"] == resolved
    assert host.query("SELECT result_json FROM context_inputs")[0]["result_json"] == resolved and receipt["reason_json"] == resolved
    shown_to_owner = shown(host, owner, document["id"])["filing"]
    assert shown_to_owner["state"] == "discarded" and shown_to_owner["error"] == {"code": "discarded", "message": "job_cancelled"}
    assert host.step() == {"status": "idle"} and host.restart_driver().process_next() == {"status": "idle"}   # replay changes nothing
    assert requested_controls(host) == [final] and host.query("SELECT * FROM context_completions")[0]["reason_json"] == resolved
    assert host.runner.propagations == 0 and host.runner.calls == []                    # the restarted runner was never used
    assert counts(host)["receipts"] == 1 and host.query("SELECT COUNT(*) AS n FROM context_input_attempts") == [{"n": 1}]


def test_publication_winning_the_discard_race_keeps_the_request_evidence_and_never_hides_the_commit(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked"
    event_id = host.query("SELECT context_event_id FROM context_inputs")[0]["context_event_id"]
    cancel(host, job_id)
    assert host.driver.reconcile()["discarded_for_cancelled_job"] == 1
    (pending,) = requested_controls(host)
    host.runner.fail = None
    host.pipeline.retry(event_id)                                                       # Context publishes it before the driver discards
    calls = host.runner.propagations
    result = host.step()
    assert result["status"] == "published" and result["discard"] == "rejected" and host.runner.propagations == calls
    (rejected,) = requested_controls(host)
    assert rejected["status"] == "rejected" and rejected["result_json"] == {
        "outcome": "already_published", "context_event_id": event_id, "control_id": pending["id"],
        "request_evidence": pending["result_json"]["request_evidence"]}
    row = host.query("SELECT state, result_json FROM context_inputs")[0]
    assert row["state"] == "published" and "request_evidence" not in row["result_json"]
    receipt = host.query("SELECT outcome, state, published_commit FROM context_completions")[0]
    assert receipt["outcome"] == "published" and receipt["published_commit"] == row["result_json"]["published_commit"]
    assert host.step() == {"status": "idle"}                                            # the cancelled job cannot apply the receipt
    assert host.query("SELECT state FROM context_completions") == [{"state": "conflict"}]
    after = shown(host, owner, document["id"])["filing"]
    assert after["state"] == "published" and after["published_commit"] == receipt["published_commit"]
    assert envelope_of(source_file(host, document["id"]))["body"] == "Met Alice about the pilot."
    restarted = host.restart_driver()
    assert restarted.process_next() == {"status": "idle"} and requested_controls(host) == [rejected]
    assert host.query("SELECT outcome, state, published_commit FROM context_completions")[0]["state"] == "conflict"
    assert host.runner.calls == [] and counts(host)["receipts"] == 1


def test_a_cancelled_job_supersedes_a_pending_retry_and_no_new_attempt_or_model_call_starts(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked" and host.runner.propagations == 1
    retried = host.data(agent, "context_retry", {"upload_id": document["id"]})
    assert retried["filing"]["state"] == "blocked"
    (retry,) = requested_controls(host)
    assert (retry["intent"], retry["status"], retry["requested_by"]) == ("retry", "pending", host.agent_id())
    cancel(host, job_id)
    host.runner.fail = None                                                             # the runner would now succeed
    result = host.step()
    assert result["status"] == "discarded" and host.runner.propagations == 1            # cancelled, not retried
    rejected, discard = requested_controls(host)
    evidence = discard["result_json"]["request_evidence"]
    assert (rejected["id"], rejected["status"], rejected["requested_by"]) == (retry["id"], "rejected", host.agent_id())
    assert rejected["result_json"] == {"outcome": "superseded_by_cancellation", "superseded_by": discard["id"], "request_evidence": evidence}
    assert evidence["reason"] == "job_cancelled" and evidence["job_id"] == job_id and evidence["requested_by"] == host.driver_id
    assert (discard["intent"], discard["status"], discard["requested_by"]) == ("discard", "applied", host.driver_id)
    assert discard["result_json"]["control_id"] == discard["id"] and discard["result_json"]["request_evidence"] == evidence
    assert host.query("SELECT state, attempt FROM context_inputs") == [{"state": "discarded", "attempt": 1}]
    assert host.query("SELECT outcome FROM context_input_attempts") == [{"outcome": "blocked"}]
    assert host.job(job_id)["state"] == "cancelled" and source_file(host, document["id"]) is None
    receipt = host.query("SELECT * FROM context_completions")[0]
    assert receipt["outcome"] == "discarded" and receipt["reason_json"]["request_evidence"] == evidence
    # Replay and a restart add no control, receipt, attempt or model call.
    for driver_step in (host.step, host.restart_driver().process_next, host.step):
        assert driver_step() == {"status": "idle"}
    assert requested_controls(host) == [rejected, discard] and host.runner.propagations == 0
    assert host.query("SELECT outcome, reason_json FROM context_completions")[0]["reason_json"] == receipt["reason_json"]
    assert host.error(agent, "context_retry", {"upload_id": document["id"]}) == "invalid_transition"
    assert counts(host)["receipts"] == 1 and counts(host)["events"] == 0


def retract_published(host):
    """A published and settled upload whose owner has just retracted it (the removal is queued, not yet processed)."""
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, "Original body.", agent=agent, title="Original")
    assert host.step()["status"] == "published" and settle(host)["state"] == "succeeded"
    settled = shown(host, owner, document["id"])
    assert settled["metadata"]["status"] == "processed" and settled["filing"]["source_current"] is True
    retracted = host.data(owner, "context_retract", {"upload_id": document["id"]})
    assert retracted["filing"]["source_revision"] == 2 and retracted["filing"]["source_current"] is True      # unchanged: current
    return owner, agent, document, first_job, retracted["job"]["id"]


def stored_document(host, document_id):
    return host.query("SELECT revision, archived, metadata_json, content_hash FROM documents WHERE id=?", document_id)[0]


def change_body(host, owner, document):
    current = shown(host, owner, document["id"])
    host.data(owner, "context_document_update", {"document_id": document["id"], "body": "Changed body.",
                                                 "expected_content_hash": current["content_hash"]}, expected_revision=current["revision"])
    return "Changed body."


def change_title(host, owner, document):
    edit_stored(host, document["id"], title="Retitled")
    return "Original body."


@pytest.mark.parametrize("change", [change_body, change_title], ids=["body", "selected-metadata"])
def test_changed_content_during_a_removal_is_not_current_is_not_rewritten_and_refiles_only_on_retry(host, change):
    owner, agent, document, first_job, removal_job = retract_published(host)
    body = change(host, owner, document)
    stale = shown(host, owner, document["id"])
    assert stale["filing"]["state"] == "unfiled" and stale["filing"]["filing_id"] is None and stale["metadata"]["status"] == "pending"
    removal = host.read(owner, "jobs_get", {"job_id": removal_job})["filing"]
    assert removal["state"] == "queued" and removal["source_current"] is False and removal["source_revision"] == 2
    assert host.step()["status"] == "published" and source_file(host, document["id"]) is None            # the removal still publishes
    before = stored_document(host, document["id"])
    assert settle(host) == {"job_id": removal_job, "state": "succeeded"}
    assert stored_document(host, document["id"]) == before                               # nothing of the newer content is rewritten
    receipt = host.query("SELECT * FROM context_completions WHERE job_id=?", removal_job)[0]
    assert receipt["outcome"] == "published" and receipt["state"] == "applied"
    assert receipt["application_json"]["source_current"] is False and receipt["application_json"]["reason"] == "superseded"
    historical = host.read(owner, "jobs_get", {"job_id": removal_job})["filing"]
    assert historical["state"] == "published" and historical["completion_state"] == "applied" and historical["source_current"] is False
    unfiled = shown(host, owner, document["id"])
    assert unfiled["filing"]["state"] == "unfiled" and unfiled["metadata"]["status"] == "pending" and "retracted" not in json.dumps(unfiled["metadata"])
    assert counts(host)["versions"] == 2 and counts(host)["model_calls"] == 2            # nothing was refiled automatically
    refiled = host.data(agent, "context_retry", {"upload_id": document["id"]})            # an explicit retry admits the next version
    new_job = refiled["job"]["id"]
    assert new_job not in {first_job, removal_job} and refiled["filing"]["source_revision"] == 3
    assert host.query("SELECT source_revision, operation FROM context_source_versions ORDER BY source_revision") == [
        {"source_revision": 1, "operation": "add"}, {"source_revision": 2, "operation": "remove"},
        {"source_revision": 3, "operation": "add"}]
    assert host.step()["status"] == "published" and envelope_of(source_file(host, document["id"]))["body"] == body
    assert settle(host) == {"job_id": new_job, "state": "succeeded"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["source_revision"] == 3 and done["filing"]["source_current"] is True
    assert counts(host) == {"versions": 3, "filings": 3, "inputs": 3, "jobs": 3, "receipts": 3, "applied": 3, "model_calls": 3, "events": 3}
    host.data(agent, "context_retry", {"upload_id": document["id"]})                       # a replay creates nothing
    assert counts(host)["versions"] == 3 and counts(host)["jobs"] == 3


def test_an_archived_source_is_not_current_for_its_removal_and_the_completion_rewrites_nothing(host):
    owner, agent, document, first_job, removal_job = retract_published(host)
    with host.database.transaction() as uow:                                             # a trusted writer archives it
        uow.connection.execute("UPDATE documents SET archived=1 WHERE id=?", (document["id"],))
    archived = host.read(owner, "jobs_get", {"job_id": removal_job})["filing"]
    assert archived["state"] == "queued" and archived["source_current"] is False
    assert host.step()["status"] == "published" and source_file(host, document["id"]) is None
    before = stored_document(host, document["id"])
    assert settle(host) == {"job_id": removal_job, "state": "succeeded"}
    assert stored_document(host, document["id"]) == before and before["archived"] == 1
    receipt = host.query("SELECT * FROM context_completions WHERE job_id=?", removal_job)[0]
    assert receipt["outcome"] == "published" and receipt["state"] == "applied"
    assert receipt["application_json"]["source_current"] is False and receipt["application_json"]["reason"] == "source_missing"
    settled = host.read(owner, "jobs_get", {"job_id": removal_job})["filing"]
    assert settled["state"] == "published" and settled["completion_state"] == "applied" and settled["source_current"] is False
    assert counts(host)["versions"] == 2 and counts(host)["receipts"] == 2 and counts(host)["model_calls"] == 2


# -- registered executor behaviour -----------------------------------------------------------------------------------------

def test_an_unrelated_program_runs_while_the_context_job_defers_and_neither_is_repeated(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    with host.database.transaction() as uow:
        values = host.runtime.programs.job_values(owner, "test.unrelated", {}, uow)
        uow.repository("jobs", business_id=host.ids["first"]).insert(values)
        other_id = values["id"]
    assert host.run_job() == {"job_id": job_id, "state": "queued"}            # the oldest job runs first and defers
    assert host.run_job() == {"job_id": other_id, "state": "succeeded"}       # the executor is released: the other program runs
    assert host.unrelated.runs == 1 and host.runner.calls == [] and host.run_job() is None
    assert host.job(job_id)["state"] == "queued" and host.job(job_id)["attempts"] == 1
    host.step()
    host.clock.advance(seconds=16)
    assert host.run_job() == {"job_id": job_id, "state": "succeeded"}
    assert host.unrelated.runs == 1 and host.job(other_id)["state"] == "succeeded" and host.job(job_id)["attempts"] == 2
    assert shown(host, owner, document["id"])["metadata"]["status"] == "processed" and counts(host)["receipts"] == 1


def test_a_retired_old_definition_never_runs_and_the_registered_definition_is_the_new_one(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    frozen = host.job(job_id)["definition_ref_json"]
    current = host.runtime.programs.get("context.file")
    assert frozen["digest"] == current["digest"] and current["version"] == 2 and current["outputs"] == ["context_filing_receipt"]
    assert host.runtime.programs.get("context.summarize") is None and host.runtime.programs.get("context.map") is None
    assert host.runtime.programs.get("context.base_file") is None and host.runtime.programs.get("context.base_instruct") is None
    # A job frozen under an older definition is refused at dispatch rather than run by the new program.
    filing_id = host.query("SELECT id FROM context_filings")[0]["id"]
    with host.database.transaction() as uow:
        values = host.runtime.programs.job_values(owner, "context.file", {"filing_id": filing_id}, uow)
        values["definition_ref_json"] = {**values["definition_ref_json"], "digest": "0" * 64}
        uow.repository("jobs", business_id=host.ids["first"]).insert(values)
        old_id = values["id"]
    stopped = host.run_job(old_id)
    assert stopped["error"]["code"] == "policy_denied" and host.job(old_id)["state"] == "failed" and host.runner.calls == []
    assert shown(host, owner, document["id"])["metadata"]["status"] == "pending" and host.job(job_id)["state"] == "queued"


# -- instructions, notes and retraction ----------------------------------------------------------------------------------

def test_an_instruction_is_its_own_frozen_source_and_completes_through_the_same_queue_and_receipt_path(host):
    owner, agent = host.human(), host.agent()
    data = host.data(agent, "context_instruct", {"text": "Alice leads the pilot.", "people": ["Alice"], "rationale": "Owner said so."})
    job_id = data["job"]["id"]
    assert data["job"]["kind"] == "context.instruct" and host.job(job_id)["definition_ref_json"]["id"] == "context.instruct"
    version = host.query("SELECT * FROM context_source_versions")[0]
    assert (version["source_kind"], version["source_id"], version["source_path"]) == ("instruction", job_id, INSTRUCTION_PATH.format(job_id))
    assert host.count("documents", "kind='context_upload'") == 0                            # an instruction creates no document
    assert host.run_job(job_id) == {"job_id": job_id, "state": "queued"} and host.runner.calls == []
    published = host.step()
    assert published["status"] == "published" and host.runner.propagations == 1
    value = envelope_of(host.context.snapshot().files[INSTRUCTION_PATH.format(job_id)])
    assert value["body"] == "Alice leads the pilot."
    assert set(value["metadata"]) == {"title", "document_kind", "source", "instructed_by", "scope", "rationale", "people"}
    assert value["metadata"]["document_kind"] == "instruction" and value["metadata"]["scope"] == "business" and value["metadata"]["people"] == ["Alice"]
    assert value["metadata"]["instructed_by"]["principal_id"] == host.agent_id() and value["metadata"]["rationale"] == "Owner said so."
    assert settle(host) == {"job_id": job_id, "state": "succeeded"}
    result = host.job(job_id)["result_json"]["data"]
    assert result["filed"] is True and result["document_id"] is None and result["published_commit"] == published["published_commit"]
    shown_filing = host.read(owner, "jobs_get", {"job_id": job_id})["filing"]
    assert shown_filing["state"] == "published" and shown_filing["completion_state"] == "applied" and shown_filing["source_current"] is True
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 1, "events": 1}
    # The owner's Global instruction is a separate source in the Global scope with its own job.
    global_data = host.data(host.human(None), "context_instruct", {"text": "The company name is Qorqut."})
    global_job = host.job(global_data["job"]["id"])
    assert global_job["business_id"] is None and global_job["kind"] == "context.instruct"
    assert host.step()["status"] == "published" and settle(host)["state"] == "succeeded"
    assert envelope_of(host.context.snapshot().files[INSTRUCTION_PATH.format(global_job["id"])])["metadata"]["scope"] == "global"
    assert counts(host)["receipts"] == 2 and counts(host)["applied"] == 2 and counts(host)["model_calls"] == 2


def test_a_note_snapshot_keeps_its_provenance_frozen_while_the_note_changes_afterwards(host):
    owner, business = host.human(), host.ids["first"]
    note = host.data(owner, "notes_create", {"title": "Strategy", "body": "Plan the launch.", "target_business_id": business})["document"]
    uploaded = host.data(owner, "notes_upload", {"document_id": note["id"], "expected_content_hash": note["content_hash"],
                                                 "target_business_id": business}, expected_revision=note["revision"])["document"]
    snapshot = host.query("SELECT * FROM documents WHERE kind='context_upload'")[0]
    assert snapshot["metadata_json"]["source"] == "notes" and snapshot["source_refs_json"] == [{"resource_type": "documents", "resource_id": note["id"]}]
    changed = host.data(owner, "notes_update", {"document_id": note["id"], "body": "Changed plan.", "expected_content_hash": uploaded["content_hash"],
                                                "target_business_id": business}, expected_revision=uploaded["revision"])["document"]
    assert changed["revision"] == uploaded["revision"] + 1
    assert host.step()["status"] == "published"
    value = envelope_of(source_file(host, snapshot["id"]))
    # The published source is the frozen snapshot: the later edit of the note is not in it, and its provenance is.
    assert "Plan the launch." in value["body"] and "Changed plan." not in value["body"]
    metadata = value["metadata"]
    assert metadata["source"] == "notes" and metadata["format"] == "notes-v1" and metadata["document_kind"] == "context_upload"
    assert metadata["source_snapshot_hash"] == snapshot["metadata_json"]["source_snapshot_hash"]
    assert metadata["references"] == [{"resource_type": "documents", "resource_id": note["id"]}]
    assert not {"source_revision", "upload_id", "status", "uploaded", "upload_snapshot_hash"} & set(metadata)
    assert settle(host)["state"] == "succeeded"
    done = shown(host, owner, snapshot["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["source_current"] is True
    retry = host.data(owner, "context_retry", {"upload_id": snapshot["id"]})
    assert retry["job"]["state"] == "succeeded" and counts(host)["versions"] == 1 and counts(host)["receipts"] == 1


def test_the_owner_retracts_a_published_source_with_a_distinct_removal_and_an_agent_cannot(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, agent=agent)
    assert host.error(owner, "context_retract", {"upload_id": document["id"]}) == "invalid_transition"      # nothing published yet
    host.step()
    assert settle(host)["state"] == "succeeded"
    assert source_file(host, document["id"]) is not None
    assert host.call(agent, "context_retract", {"upload_id": document["id"]}).status == "error"
    retracted = host.data(owner, "context_retract", {"upload_id": document["id"]})
    removal_job = retracted["job"]["id"]
    assert removal_job != first_job and retracted["filing"]["source_revision"] == 2
    versions = host.query("SELECT source_revision, operation FROM context_source_versions ORDER BY source_revision")
    assert versions == [{"source_revision": 1, "operation": "add"}, {"source_revision": 2, "operation": "remove"}]
    assert host.query("SELECT operation FROM context_inputs ORDER BY sequence") == [{"operation": "add"}, {"operation": "remove"}]
    assert host.step()["status"] == "published" and source_file(host, document["id"]) is None                  # exactly that source is removed
    assert settle(host) == {"job_id": removal_job, "state": "succeeded"}
    after = shown(host, owner, document["id"])
    assert after["metadata"]["status"] == "retracted" and after["filing"]["source_revision"] == 2 and after["filing"]["state"] == "published"
    assert counts(host)["receipts"] == 2 and counts(host)["applied"] == 2 and counts(host)["versions"] == 2
    again = host.error(owner, "context_retract", {"upload_id": document["id"]})
    assert again == "invalid_transition" and counts(host)["versions"] == 2


# -- reads ---------------------------------------------------------------------------------------------------------------

def test_filing_reads_are_authorized_redacted_and_owner_views_stay_owner_only(host):
    owner, agent, outsider = host.human(), host.agent(), host.agent("second")
    # An owner-initiated upload blocks: the agent coordinates on the code, only the initiator reads the reason.
    document, job_id = upload(host, agent=owner)
    host.runner.fail = RuntimeError("a private provider detail")
    assert host.step()["status"] == "input_blocked"
    by_owner = host.read(owner, "jobs_get", {"job_id": job_id})["filing"]
    by_agent = host.read(agent, "jobs_get", {"job_id": job_id})["filing"]
    assert by_owner["state"] == "blocked" and by_owner["error"]["code"] == "propagation_failed" and "message" in by_owner["error"]
    assert by_agent["error"] == {"code": "propagation_failed"}
    assert {key: value for key, value in by_agent.items() if key != "error"} == {key: value for key, value in by_owner.items() if key != "error"}
    assert host.call(outsider, "jobs_get", {"job_id": job_id}).status == "error"
    assert shown(host, owner, document["id"])["filing"]["error"] == by_owner["error"]
    assert host.call(outsider, "context_get", {"document_id": document["id"]}).status == "error"
    # Owner-only projections of the recorded facts.
    for operation, arguments in (("context_filings", {"upload_id": document["id"]}), ("context_upload_map", {"upload_id": document["id"]})):
        assert host.call(agent, operation, arguments).status == "error" and host.call(outsider, operation, arguments).status == "error"
    facts = host.read(owner, "context_filings", {"upload_id": document["id"]})["filings"][0]
    assert facts["input"]["state"] == "blocked" and facts["input"]["attempt"] == 1 and facts["publication"] is None and facts["completion"] is None
    assert facts["attempts"][0]["outcome"] == "blocked" and facts["job"]["state"] == "queued"
    # An agent-initiated blocked upload shows the reason to the agent that started it.
    host.runner.fail = None
    host.data(owner, "context_retry", {"upload_id": document["id"]})
    assert host.step()["status"] == "published" and settle(host)["state"] == "succeeded"
    own, own_job = upload(host, "Mine.", agent=agent)
    host.runner.fail = RuntimeError("another private detail")
    assert host.step()["status"] == "input_blocked"
    initiator = host.read(agent, "jobs_get", {"job_id": own_job})["filing"]
    assert initiator["error"]["code"] == "propagation_failed" and "message" in initiator["error"]
    blocked = shown(host, owner, own["id"])
    assert blocked["filing"]["state"] == "blocked" and blocked["metadata"]["status"] == "pending"      # blocked is pending, not failed


def test_a_global_upload_files_and_settles_in_the_global_scope(host):
    owner = host.human(None)
    data = host.upload(owner, "Global strategy meeting.", title="Strategy")
    document, job_id = data["document"], data["job"]["id"]
    assert document["business_id"] is None and host.job(job_id)["business_id"] is None
    assert host.query("SELECT business_id FROM context_inputs") == [{"business_id": None}]
    waiting = shown(host, owner, document["id"])
    assert waiting["filing"]["state"] == "queued" and waiting["metadata"]["status"] == "pending"
    assert host.step()["status"] == "published" and settle(host) == {"job_id": job_id, "state": "succeeded"}
    done = shown(host, owner, document["id"])
    assert done["metadata"]["status"] == "processed" and done["filing"]["completion_state"] == "applied"
    assert envelope_of(source_file(host, document["id"]))["body"] == "Global strategy meeting."
    assert counts(host) == {"versions": 1, "filings": 1, "inputs": 1, "jobs": 1, "receipts": 1, "applied": 1, "model_calls": 1, "events": 1}
