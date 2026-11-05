"""H2-host: the one current-source filing projection, exact status mapping, retained adapters and the namespace queue summary.

Real: the composed host of ``context_completion_support`` (assembly, policy, application, ``DocumentOperations`` with its
``ContextFiling``, the registered ``context.file`` job, the Context integration's queue/filing storage, the queue driver and a disposable
Context with real Git), and the retained dashboard compatibility routes called with real requests (``context_source_status_support``).
Faked: only the injected model runner, the injected clock and synthetic principals and files under ``tmp_path``. No model, provider,
network, native session or operational data is used, and nothing here is skipped."""

from dataclasses import replace

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_operations.contracts import DomainError, plain

from qorqut_context.integration.filing.queue_status import context_queue_summary
from bazarlyk.transport.compatibility import DashboardCompatibility
from context_completion_support import build
from context_source_status_support import Routes, brief, cancel, filings, scoped, settle, shown, upload


@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


# -- the same projection on every read path, through the whole life of an upload ------------------------------------------------

def test_every_upload_read_presents_the_same_current_source_filing_from_queued_to_applied(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent, title="Pilot call")
    seen = []
    host.runner.during = lambda tools, payload: seen.append(filings(host, owner, document["id"]))

    def agree(expected):
        reads = filings(host, owner, document["id"])
        assert len({str(sorted(value.items())) for value in reads.values()}) == 1, reads         # all six paths: identical object
        assert brief(reads["get"]) == expected, reads["get"]
        return reads["get"]

    queued = agree({"state": "queued", "status": "pending", "completion_state": None, "retryable": False,
                    "job_state": "queued", "operation": "add", "attempts": 0})
    assert queued["document_revision"] == document["revision"] and queued["queue_position"] == 0
    # The model is running while the driver holds the input as processing: pending, not retryable, one attempt recorded.
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked"
    (during,) = seen
    assert len({str(sorted(value.items())) for value in during.values()}) == 1
    assert brief(during["get"]) == {"state": "processing", "status": "pending", "completion_state": None, "retryable": False,
                                    "job_state": "queued", "operation": "add", "attempts": 1}
    blocked = agree({"state": "blocked", "status": "pending", "completion_state": None, "retryable": True,
                     "job_state": "queued", "operation": "add", "attempts": 1})
    assert blocked["error"]["code"] == "propagation_failed" and blocked["error"]["message"]
    # An explicit retry makes a control pending: the offer is suppressed until the driver resolves it.
    host.runner.fail = None
    host.data(agent, "context_retry", {"upload_id": document["id"]})
    agree({"state": "blocked", "status": "pending", "completion_state": None, "retryable": False,
           "job_state": "queued", "operation": "add", "attempts": 1})
    host.step()
    published = agree({"state": "published", "status": "pending", "completion_state": "pending", "retryable": False,
                       "job_state": "queued", "operation": "add", "attempts": 2})
    assert published["published_commit"] and published["error"] is None
    settle(host, job_id)
    applied = agree({"state": "published", "status": "processed", "completion_state": "applied", "retryable": False,
                     "job_state": "succeeded", "operation": "add", "attempts": 2})
    assert applied["published_commit"] == published["published_commit"] and applied["queue_position"] is None
    assert applied["document_revision"] == document["revision"] + 1                  # the completion rewrote the document row
    assert scoped(host, owner, document["id"])["filing_status"] == "processed"


# -- the exact mapping of queue, publication and completion states --------------------------------------------------------------

def test_a_discard_request_then_the_discard_is_pending_then_failed_and_never_retryable(host):
    owner, agent = host.human(), host.agent()
    document, job_id = upload(host, agent=agent)
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    input_id = shown(host, owner, document["id"])["filing"]["input_id"]
    host.data(owner, "context_queue_discard", {"input_id": input_id, "expected_state": "blocked"})
    requested = shown(host, owner, document["id"])["filing"]
    assert brief(requested) == {"state": "discard_requested", "status": "pending", "completion_state": None, "retryable": False,
                                "job_state": "queued", "operation": "add", "attempts": 1}
    assert requested["error"]["code"] == "propagation_failed"                       # the blocked reason stays visible
    assert host.step()["status"] == "discarded"
    settle(host, job_id)
    discarded = shown(host, owner, document["id"])
    assert brief(discarded["filing"]) == {"state": "discarded", "status": "failed", "completion_state": "applied", "retryable": False,
                                          "job_state": "failed", "operation": "add", "attempts": 1}
    assert discarded["filing"]["published_commit"] is None and discarded["metadata"]["status"] == "failed"
    assert discarded["filing"]["error"]["code"] == "discarded"


def test_a_removal_is_retracted_and_changed_content_after_an_older_publication_reads_unfiled_not_processed(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, "Original body.", agent=agent, title="Original")
    host.step()
    settle(host, first_job)
    processed = shown(host, owner, document["id"])["filing"]
    assert brief(processed)["status"] == "processed" and processed["source_current"] is True
    removal = host.data(owner, "context_retract", {"upload_id": document["id"]})
    pending = shown(host, owner, document["id"])["filing"]
    assert brief(pending) == {"state": "queued", "status": "pending", "completion_state": None, "retryable": False,
                              "job_state": "queued", "operation": "remove", "attempts": 0}
    assert pending["source_revision"] == 2 and pending["filing_id"] == removal["filing"]["filing_id"]
    host.step()
    settle(host, removal["job"]["id"])
    retracted = shown(host, owner, document["id"])
    assert brief(retracted["filing"])["status"] == "retracted" and retracted["metadata"]["status"] == "retracted"
    assert retracted["filing"]["operation"] == "remove" and retracted["filing"]["completion_state"] == "applied"
    # Newer content: the older publication (a removal here, and add before it) is history, never the current state.
    changed = host.data(owner, "context_document_update", {"document_id": document["id"], "body": "Newer body.",
                                                          "expected_content_hash": retracted["content_hash"]},
                        expected_revision=retracted["revision"])["document"]
    unfiled = filings(host, owner, document["id"], query="Newer")
    for read in unfiled.values():
        assert read == {"filing_id": None, "source_revision": None, "input_id": None, "job_id": None, "state": "unfiled",
                        "published_commit": None, "completion_state": None, "source_current": None, "error": None,
                        "queue_position": None, "document_revision": changed["revision"], "operation": None, "status": "pending",
                        "attempts": 0, "job_state": None, "retryable": True}
    historical = host.read(owner, "jobs_get", {"job_id": removal["job"]["id"]})["filing"]
    assert historical["state"] == "published" and historical["source_current"] is False       # history keeps its own outcome
    assert historical["published_commit"] and shown(host, owner, document["id"])["metadata"]["status"] == "pending"
    # An explicit retry admits the newer content as the next version; the offer disappears while it is queued.
    refiled = host.data(agent, "context_retry", {"upload_id": document["id"]})
    queued = shown(host, owner, document["id"])["filing"]
    assert refiled["filing"]["source_revision"] == 3 and queued["operation"] == "add" and queued["retryable"] is False


def test_a_completion_conflict_after_publication_is_failed_with_the_commit_and_a_stopped_job_infers_nothing(host):
    owner, agent = host.human(), host.agent()
    published_doc, published_job = upload(host, agent=agent, title="Published")
    published = host.step()
    cancel(host, published_job)                                                    # the job is stopped after publication
    host.step()
    conflict = shown(host, owner, published_doc["id"])
    assert brief(conflict["filing"]) == {"state": "published", "status": "failed", "completion_state": "conflict", "retryable": False,
                                         "job_state": "cancelled", "operation": "add", "attempts": 1}
    assert conflict["filing"]["published_commit"] == published["published_commit"] and conflict["metadata"]["status"] == "failed"
    assert conflict["filing"]["error"]["code"] == "job_cancelled"
    # A stopped job whose input is still queued: pending, the stop is visible, nothing is inferred, never retryable.
    other, other_job = upload(host, "Second note about Bob.", agent=agent, title="Stopped")
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    cancel(host, other_job)
    stopped = shown(host, owner, other["id"])["filing"]
    assert brief(stopped) == {"state": "blocked", "status": "pending", "completion_state": None, "retryable": False,
                              "job_state": "cancelled", "operation": "add", "attempts": 1}
    assert stopped["published_commit"] is None and stopped["error"]["code"] == "propagation_failed"
    assert host.error(agent, "context_retry", {"upload_id": other["id"]}) == "invalid_transition"


# -- note and journal snapshots ------------------------------------------------------------------------------------------------

def test_a_note_reports_its_submitted_snapshot_filing_and_marks_later_changes_without_changing_that_filing(host):
    owner, business, routes = host.human(), host.ids["first"], Routes(host)
    note = host.data(owner, "notes_create", {"title": "Strategy", "body": "Plan the launch.", "target_business_id": business})["document"]
    uploaded = host.data(owner, "notes_upload", {"document_id": note["id"], "expected_content_hash": note["content_hash"],
                                                 "target_business_id": business}, expected_revision=note["revision"])["document"]
    snapshot = host.query("SELECT id FROM documents WHERE kind='context_upload'")[0]["id"]
    status, notes = routes.get(owner, "biz/first/notes", target_business_id=business)
    assert status == 200
    (entry,) = [item for item in notes if item["id"] == note["id"]]
    assert entry["filing"] == shown(host, owner, snapshot)["filing"] and entry["filing"]["state"] == "queued"
    assert entry["changedSinceUpload"] is False and entry["uploadId"] == snapshot and entry["filerSubmissions"] == []
    host.data(owner, "notes_update", {"document_id": note["id"], "body": "Changed plan.", "expected_content_hash": uploaded["content_hash"],
                                      "target_business_id": business}, expected_revision=uploaded["revision"])
    host.step()
    settle(host)
    status, notes = routes.get(owner, "biz/first/notes", target_business_id=business)
    (entry,) = [item for item in notes if item["id"] == note["id"]]
    assert entry["changedSinceUpload"] is True                                       # the note moved on; the snapshot did not
    assert entry["filing"] == shown(host, owner, snapshot)["filing"] and entry["filing"]["status"] == "processed"
    assert entry["filing"]["source_current"] is True


def test_journal_submissions_keep_their_source_identity_and_each_filing_is_the_same_projection(host):
    owner = host.human(None)
    journal = host.data(owner, "notes_create", {"title": "Daily", "body": "Today I met Alice."})["document"]
    submitted = host.data(owner, "journal_upload_to_filer", {"document_id": journal["id"], "expected_content_hash": journal["content_hash"]},
                          expected_revision=journal["revision"])
    snapshot = submitted["document"]["id"]
    routes = Routes(host)
    status, notes = routes.get(owner, "root/notes")
    assert status == 200
    (entry,) = [item for item in notes if item["id"] == journal["id"]]
    (submission,) = entry["filerSubmissions"]
    # Source identity, date, hash, revision and the changed-since-submission flag stay; the filing is the shared projection and
    # no model-summary, partial-filing or previous-receipt interpretation remains beside it.
    assert set(submission) == {"uploadId", "sourceRevision", "sourceHash", "sourceDate", "createdAt", "changedSinceSubmission", "filing"}
    assert submission["uploadId"] == snapshot and submission["changedSinceSubmission"] is False
    assert submission["sourceRevision"] == journal["revision"] and submission["sourceHash"]
    assert submission["filing"] == shown(host, owner, snapshot)["filing"] and submission["filing"]["state"] == "queued"
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    blocked = routes.get(owner, "root/notes")[1][0]["filerSubmissions"][0]["filing"]
    assert brief(blocked)["state"] == "blocked" and blocked["status"] == "pending" and blocked["retryable"] is True
    # Editing the journal afterwards is flagged on the submission; the snapshot's own filing is untouched.
    host.data(owner, "notes_update", {"document_id": journal["id"], "body": "Edited later.", "expected_content_hash": journal["content_hash"]},
              expected_revision=journal["revision"])
    changed = routes.get(owner, "root/notes")[1][0]["filerSubmissions"][0]
    assert changed["changedSinceSubmission"] is True and changed["filing"] == blocked


# -- redaction and scope ------------------------------------------------------------------------------------------------------

def test_other_agents_read_error_codes_only_and_another_business_cannot_read_the_source(host):
    owner, agent, outsider, other_owner = host.human(), host.agent(), host.agent("second"), host.human("second")
    document, _ = upload(host, agent=owner, title="Private source")                     # the owner started this filing
    host.runner.fail = RuntimeError("a private provider detail")
    host.step()
    for reader, sees_reason in ((owner, True), (agent, False)):
        for name, read in filings(host, reader, document["id"]).items():
            if sees_reason:
                assert read["error"]["code"] == "propagation_failed" and read["error"]["message"], (name, read)
            else:
                assert read["error"] == {"code": "propagation_failed"}, (name, read)      # no frozen reason text for another agent
            assert read["state"] == "blocked" and read["retryable"] is True              # the hint is not a secret, only a hint
    # An agent-initiated filing shows the reason to the agent that started it, and the owner reads it too.
    host.runner.fail = None
    host.data(owner, "context_retry", {"upload_id": document["id"]})
    host.step()
    own, _ = upload(host, "Mine.", agent=agent, title="Own source")
    host.runner.fail = RuntimeError("another private detail")
    host.step()
    for name, read in filings(host, agent, own["id"], query="Mine").items():
        assert read["error"]["code"] == "propagation_failed" and read["error"]["message"], (name, read)
    # Another business's selection reads and retries nothing of this one, scoped or not.
    for reader in (outsider, other_owner):
        assert host.error(reader, "context_get", {"document_id": own["id"]}) == "not_found"
        assert host.error(reader, "context_get", {"document_id": own["id"], "scope": "business"}) == "not_found"
        for operation, arguments in (("context_list", {}), ("context_list", {"scope": "business"}),
                                     ("context_search", {"query": "Mine"}), ("context_search", {"scope": "business", "query": "Mine"})):
            assert own["id"] not in [item["id"] for item in host.read(reader, operation, arguments)["items"]]
        assert host.error(reader, "context_retry", {"upload_id": own["id"]}) == "not_found"


# -- the namespace-limited queue summary -----------------------------------------------------------------------------------------

def queue_of(routes, ctx, slug="first"):
    status, body = routes.get(ctx, f"biz/{slug}/context", view="processes") if slug else routes.get(ctx, "root/context", view="processes")
    assert status == 200 and set(body) == {"context_queue"}, body
    return body["context_queue"]


def test_the_queue_summary_counts_this_namespace_only_and_never_invents_a_pending_completion(host):
    routes = Routes(host)
    first, second, root = host.human(), host.human("second"), host.human(None)
    a, a_job = upload(host, agent=host.agent(), title="First business")
    b, b_job = upload(host, "Second business note.", agent=host.agent("second"), title="Second business")
    g = host.data(root, "context_upload", {"body": "A Global note.", "title": "Global"})["document"]
    zero = {"queued": 0, "processing": 0, "blocked": 0, "discard_requested": 0, "publication_pending_completion": 0,
            "completion_conflict": 0, "queue_blocked": False, "blocked_input_id": None}
    assert queue_of(routes, first) == {**zero, "queued": 1}
    assert queue_of(routes, second) == {**zero, "queued": 1} and queue_of(routes, root, None) == {**zero, "queued": 1}
    # The blocked head is shared as a fact; its identity is named to its own namespace only.
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    head = shown(host, first, a["id"])["filing"]["input_id"]
    assert queue_of(routes, first) == {**zero, "blocked": 1, "queue_blocked": True, "blocked_input_id": head}
    assert queue_of(routes, second) == {**zero, "queued": 1, "queue_blocked": True}
    assert queue_of(routes, root, None) == {**zero, "queued": 1, "queue_blocked": True}
    # A pending retry resolves, the head publishes and its completion waits on the job; the other namespaces are untouched.
    host.runner.fail = None
    host.data(host.agent(), "context_retry", {"upload_id": a["id"]})
    host.step()
    assert queue_of(routes, first) == {**zero, "publication_pending_completion": 1}
    assert queue_of(routes, second) == {**zero, "queued": 1}
    # A GENERAL input with no filing publishes without a filing receipt: it is not a pending completion.
    host.data(first, "context_queue_submit", {"operation": "add", "path": "foundation/notes/plain.md", "content": "Plain.",
                                              "metadata": {"kind": "note", "title": "plain", "origin": "agent"}})
    for _ in range(3):
        assert host.step()["status"] == "published"
    assert host.count("context_filings") == 3 and host.count("context_completions") == 3
    assert queue_of(routes, first) == {**zero, "publication_pending_completion": 1}
    assert queue_of(routes, second) == {**zero, "publication_pending_completion": 1}
    assert queue_of(routes, root, None) == {**zero, "publication_pending_completion": 1}
    settle(host, a_job)
    assert queue_of(routes, first) == zero
    cancel(host, b_job)                                                           # a stopped job cannot apply its receipt
    host.step()
    assert queue_of(routes, second) == {**zero, "completion_conflict": 1}
    assert queue_of(routes, root, None) == {**zero, "publication_pending_completion": 1}
    del g


def test_the_global_process_read_is_an_owner_read_and_a_missing_queue_is_an_explicit_error(host, tmp_path):
    routes = Routes(host)
    root = host.human(None)
    # The harness calls the registered route handlers directly, so a refusal is the raised domain error the HTTP layer maps to a status.
    agent_in_global = replace(host.agent(), business_id=None)
    for ctx, path, query, code in ((agent_in_global, "root/context", {"view": "processes"}, "forbidden"),
                                   (root, "root/context", {"view": "summary"}, "validation_error"),
                                   (root, "root/context", {}, "validation_error"),
                                   (host.human(), "root/context", {"view": "processes"}, "scope_required")):    # a selected business is not Global
        with pytest.raises(DomainError) as refused:
            routes.get(ctx, path, **query)
        assert refused.value.code == code, (path, query, refused.value)
    # A host whose database has no filing storage (a deliberately incomplete custom assembly; the stock one composes it) answers with an
    # explicit error, never an empty summary: the missing tables are the filing ones.
    from qorqut_context.integration import filing as context_filing

    from host_foundation_support import build_instance

    bare, _ = build_instance(tmp_path / "bare", without={context_filing.storage_contribution().owner})
    with bare.database.read() as uow, pytest.raises(DomainError) as missing:
        context_queue_summary(uow.connection, None)
    assert missing.value.code == "operation_unavailable" and "context_filings" in missing.value.message
    with pytest.raises(DomainError) as unavailable:
        DashboardCompatibility(bare.runtime).context_processes_view(bare.human())
    assert unavailable.value.code == "operation_unavailable"


# -- the supplied source revision is enforced on retry and retraction --------------------------------------------------------------

def effects(host):
    """Every table a retry or retraction could write: nothing here may move on a refused request."""
    return {table: host.count(table) for table in ("context_source_versions", "context_filings", "context_inputs", "jobs",
                                                    "idempotency_receipts", "context_input_controls")}


def test_a_supplied_revision_is_enforced_on_retry_and_retract_and_a_conflict_has_no_effects(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, "Original body.", agent=agent, title="Original")
    host.step()
    settle(host, first_job)
    processed = shown(host, owner, document["id"])
    stale = processed["revision"]
    host.data(owner, "context_document_update", {"document_id": document["id"], "body": "Newer body.",
                                                 "expected_content_hash": processed["content_hash"]}, expected_revision=stale)
    current = shown(host, owner, document["id"])["revision"]
    assert current != stale
    before = effects(host)
    # A wrong revision is the existing revision_conflict with the server's current revision, and writes nothing.
    wrong = host.call(owner, "context_retry", {"upload_id": document["id"]}, expected_revision=stale)
    assert wrong.status == "error" and wrong.error["code"] == "revision_conflict" and wrong.error["current_revision"] == current
    assert effects(host) == before
    # The correct revision admits the changed content as the next version.
    retried = host.data(owner, "context_retry", {"upload_id": document["id"]}, key="retry-1", expected_revision=current)
    assert retried["filing"]["source_revision"] == 2 and effects(host)["context_source_versions"] == before["context_source_versions"] + 1
    host.step()
    settle(host, retried["job"]["id"])
    settled = shown(host, owner, document["id"])
    assert settled["filing"]["completion_state"] == "applied" and settled["revision"] != current       # the source moved on after the request
    # An identical successful request replays its receipt although the source revision has since changed.
    replay_before = effects(host)
    replay = host.call(owner, "context_retry", {"upload_id": document["id"]}, key="retry-1", expected_revision=current)
    assert replay.status in ("ok", "accepted") and plain(replay.data)["filing"]["filing_id"] == retried["filing"]["filing_id"]
    assert effects(host) == replay_before
    # Retraction takes the same precondition, before it admits anything.
    refused = host.call(owner, "context_retract", {"upload_id": document["id"]}, expected_revision=current)
    assert refused.status == "error" and refused.error["code"] == "revision_conflict"
    assert refused.error["current_revision"] == settled["revision"] and effects(host) == replay_before
    removal = host.data(owner, "context_retract", {"upload_id": document["id"]}, expected_revision=settled["revision"])
    assert removal["filing"]["source_revision"] == 3 and effects(host)["context_source_versions"] == replay_before["context_source_versions"] + 1


def test_an_omitted_revision_keeps_the_existing_retry_and_retract_contract(host):
    owner, agent = host.human(), host.agent()
    document, first_job = upload(host, agent=agent, title="Blocked")
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    retried = host.data(agent, "context_retry", {"upload_id": document["id"]})                  # no expected_revision
    assert retried["filing"]["state"] == "blocked" or retried["filing"]["input_id"]
    host.step()
    settle(host, first_job)
    removal = host.data(owner, "context_retract", {"upload_id": document["id"]})                # no expected_revision
    assert removal["filing"]["source_revision"] == 2


def test_the_global_adapter_refuses_force_and_passes_the_revision_through(host):
    routes = Routes(host)
    root = host.human(None)
    data = host.upload(root, "A Global note.", title="Global")
    document, job_id = data["document"], data["job"]["id"]
    host.step()
    settle(host, job_id)
    revision = shown(host, root, document["id"])["revision"]
    retry, retract = (f"root/context-library/{document['id']}/{action}" for action in ("retry", "retract"))
    for path in (retry, retract):
        for body in ({"force": True}, {"expected_revision": revision, "force": False}, {"upload_id": document["id"]}):
            with pytest.raises(DomainError) as refused:
                routes.post(root, path, body)
            assert refused.value.code == "validation_error", (path, body)
    before = effects(host)
    for path in (retry, retract):                  # the retained adapter raises the operation's own error for the HTTP layer to map
        with pytest.raises(DomainError) as conflict:
            routes.post(root, path, {"expected_revision": revision + 1})
        assert conflict.value.code == "revision_conflict" and conflict.value.current_revision == revision, path
    assert effects(host) == before
    status, body = routes.post(root, retract, {"expected_revision": revision, "idempotency_key": "global-retract"})
    assert status in (200, 202) and body["status"] in ("ok", "accepted"), body
    status, replay = routes.post(root, retract, {"expected_revision": revision, "idempotency_key": "global-retract"})
    assert status in (200, 202) and replay["data"]["filing"]["filing_id"] == body["data"]["filing"]["filing_id"]


# -- change-feed invalidation of the queue status -------------------------------------------------------------------------------------

DRIVER_TAGS = {"context_queue_driver_write", "context_queue_driver_reconcile", "context_queue_driver_start"}


def mark(host):
    events = host.runtime.changes.replay_since(0)
    return events[-1]["seq"] if events else 0


def since(host, seq):
    return host.runtime.changes.replay_since(seq)


def test_driver_transitions_publish_the_additive_global_queue_kind_and_idle_ticks_publish_nothing(host):
    agent, owner = host.agent(), host.human()
    document, job_id = upload(host, agent=agent, title="Feed")
    first = since(host, 0)
    assert any("context_queue" in event["kinds"] for event in first)                       # admission: the context_upload family
    seq = mark(host)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked"
    blocked = since(host, seq)
    assert blocked and {event["operation"] for event in blocked} <= DRIVER_TAGS             # only the three accepted driver sites
    assert {kind for event in blocked for kind in event["kinds"]} == {"context_queue"}      # a queue-only transition
    seq = mark(host)
    host.runner.fail = None
    host.data(owner, "context_retry", {"upload_id": document["id"]})
    assert any("context_queue" in event["kinds"] for event in since(host, seq))             # retry: the context_ family
    seq = mark(host)
    assert host.step()["status"] == "published"
    published = since(host, seq)
    assert published and {event["operation"] for event in published} <= DRIVER_TAGS
    assert {kind for event in published for kind in event["kinds"]} == {"context_queue"}
    seq = mark(host)
    settle(host, job_id)                                                                    # the job's own writes keep their kinds too
    kinds = {kind for event in since(host, seq) for kind in event["kinds"]}
    assert {"context_queue", "jobs_routines"} <= kinds
    seq = mark(host)
    assert host.step() == {"status": "idle"} and host.step() == {"status": "idle"}          # empty ticks, reconcile included
    assert since(host, seq) == []
    # Recovery: a stopped job's unfinished input is discarded by the reconciling tick.
    stopped, stopped_job = upload(host, "Second.", agent=agent, title="Stopped")
    cancel(host, stopped_job)
    seq = mark(host)
    host.step()
    recovered = since(host, seq)
    assert any(event["operation"] == "context_queue_driver_reconcile" for event in recovered)
    assert {kind for event in recovered for kind in event["kinds"]} == {"context_queue"}
    assert host.runtime.changes.route_versions({"context_queue"}, host.ids["first"]).keys() == {("context_queue", "global")}


def test_a_rolled_back_or_empty_tagged_transaction_does_not_advance_the_queue_kind(host):
    changes = host.runtime.changes
    seq, version = mark(host), changes.version("context_queue", "global")
    with pytest.raises(RuntimeError):
        with host.database.transaction(operation="context_queue_driver_write") as uow:
            uow.connection.execute("UPDATE principals SET display_name = display_name || 'x' WHERE id = ?", (host.owner,))
            raise RuntimeError("rolled back")
    with host.database.transaction(operation="context_queue_driver_write"):
        pass                                                                                # nothing written
    with host.database.read() as uow:
        uow.connection.execute("SELECT 1").fetchone()
    assert since(host, seq) == [] and changes.version("context_queue", "global") == version
    with host.database.transaction(operation="context_queue_driver_write") as uow:          # a real write does
        uow.connection.execute("UPDATE principals SET display_name = display_name WHERE id = ?", (host.owner,))
    assert changes.version("context_queue", "global") > version


@pytest.mark.parametrize("operation,existing", [
    ("jobs_run", "jobs_routines"), ("job_cancel", "jobs_routines"), ("jobs_routines_tick", "jobs_routines"),
    ("learnings_update", "learnings"), ("learning_record", "learnings"),
    ("signoffs_notification_tick", "signoffs"),
    ("context_queue_driver_write", None), ("context_queue_driver_start", None), ("context_queue_driver_reconcile", None),
    ("context_retry", None), ("context_retract", None), ("context_upload", None),
    ("notes_upload", None), ("journal_upload_to_filer", None), ("actions_submit", None), ("signoffs_apply", None)])
def test_producer_operation_names_map_additively_to_the_global_queue_kind(operation, existing):
    # Mapping-level evidence only: operation NAMES, not a composed producer. It is not installed-integration proof for the families
    # (actions, Signoff apply, routines) that this H2 host composition does not run.
    from bazarlyk.service.changes import GLOBAL_SCOPE, ChangeFeed, _coarse_kinds_for_operation

    expected = {"context_queue"} | ({existing} if existing else set())
    assert _coarse_kinds_for_operation(operation) >= expected
    feed = ChangeFeed()
    feed.publish_commit(captured=[], operation=operation, business_id=None, wrote=False)
    assert feed.version("context_queue", GLOBAL_SCOPE) == 0                               # no row changed, nothing published
    feed.publish_commit(captured=[], operation=operation, business_id="business-a", wrote=True)
    assert feed.version("context_queue", GLOBAL_SCOPE) > 0
    assert feed.version("context_queue", "business-a") == 0                               # global only, whatever the transaction names
    assert feed.route_versions({"context_queue"}, "business-b").keys() == {("context_queue", GLOBAL_SCOPE)}
    assert existing is None or feed.version(existing, GLOBAL_SCOPE) > 0                   # the existing kind is kept, not replaced


def test_unrelated_operations_do_not_publish_the_queue_kind_and_trigger_kinds_keep_their_scope():
    from bazarlyk.service.changes import GLOBAL_SCOPE, ChangeFeed, _coarse_kinds_for_operation

    for operation in ("tasks_update", "agents_list", "documents_get", "routines_runs"):
        assert "context_queue" not in _coarse_kinds_for_operation(operation), operation
    assert _coarse_kinds_for_operation("business_setup_update") == set()
    feed = ChangeFeed()
    feed.publish_commit(captured=[{"table": "documents", "scope": "business-a", "kinds": {"documents"}}],
                        operation="notes_upload", business_id="business-a", wrote=True)
    assert feed.version("documents", "business-a") > 0 and feed.version("documents", GLOBAL_SCOPE) == 0
    assert feed.version("context_queue", GLOBAL_SCOPE) > 0 and feed.version("context_queue", "business-a") == 0
    (event,) = feed.replay_since(0)
    assert event["kinds"] == ["context_queue", "documents"] and event["tables"] == ["documents"]


def test_a_queue_only_transition_invalidates_cached_status_in_every_namespace_without_leaking_the_input(host):
    routes = Routes(host)
    owner, owner_b, root = host.human(), host.human("second"), host.human(None)
    note = host.data(owner, "notes_create", {"title": "Plan", "body": "Plan the pilot.", "target_business_id": host.ids["first"]})["document"]
    host.data(owner, "notes_upload", {"document_id": note["id"], "expected_content_hash": note["content_hash"],
                                      "target_business_id": host.ids["first"]}, expected_revision=note["revision"])
    snapshot = host.query("SELECT id FROM documents WHERE kind='context_upload'")[0]["id"]
    views = {"a_processes": (owner, "biz/first/context", {"view": "processes"}), "b_processes": (owner_b, "biz/second/context", {"view": "processes"}),
             "a_full": (owner, "biz/first/context", {}), "a_notes": (owner, "biz/first/notes", {"target_business_id": host.ids["first"]})}
    tags = {}
    for name, (ctx, path, query) in views.items():
        status, body, tags[name] = routes.revalidate(ctx, path, **query)
        assert status == 200 and tags[name], name
        assert routes.revalidate(ctx, path, tags[name], **query)[0] == 304, name                  # cached while nothing changed
    assert routes.revalidate(owner, "biz/first/notes", tags["a_notes"], target_business_id=host.ids["first"])[2] == tags["a_notes"]
    # A queue-only transition: the head blocks. Every namespace's cached tag is stale, though no document, task or job changed.
    seq = mark(host)
    host.runner.fail = RuntimeError("model unavailable")
    assert host.step()["status"] == "input_blocked"
    assert {kind for event in since(host, seq) for kind in event["kinds"]} == {"context_queue"}
    fresh = {}
    for name, (ctx, path, query) in views.items():
        status, body, fresh[name] = routes.revalidate(ctx, path, tags[name], **query)
        assert status == 200 and fresh[name] != tags[name], name
        fresh[name] = (fresh[name], body)
    head = shown(host, owner, snapshot)["filing"]["input_id"]
    assert fresh["a_processes"][1]["context_queue"]["blocked"] == 1 and fresh["a_processes"][1]["context_queue"]["blocked_input_id"] == head
    assert fresh["a_full"][1]["context_queue"]["blocked_input_id"] == head
    other = fresh["b_processes"][1]["context_queue"]                                              # the other namespace learns it waits
    assert other["queue_blocked"] is True and other["blocked_input_id"] is None and other["blocked"] == 0
    (entry,) = [item for item in fresh["a_notes"][1] if item["id"] == note["id"]]
    assert entry["filing"]["state"] == "blocked" and entry["filing"]["input_id"] == head
    # The next transitions invalidate again: retry, publication, completion.
    seen = {name: value[0] for name, value in fresh.items()}
    host.runner.fail = None
    host.data(owner, "context_retry", {"upload_id": snapshot})
    for step in ("retry", "publish", "completion"):
        if step == "publish":
            assert host.step()["status"] == "published"
        elif step == "completion":
            settle(host)
        for name, (ctx, path, query) in views.items():
            status, body, tag = routes.revalidate(ctx, path, seen[name], **query)
            assert status == 200 and tag != seen[name], (step, name)
            seen[name] = tag
    assert routes.revalidate(owner, "biz/first/context", seen["a_full"], view="processes")[0] in (200, 304)
    _, final, _ = routes.revalidate(owner, "biz/first/context", None, view="processes")
    assert final["context_queue"]["publication_pending_completion"] == 0 and final["context_queue"]["queue_blocked"] is False
    # The Global process read is uncached by construction, so it can never serve a stale 304.
    response = routes.send(root, "GET", "root/context", {"view": "processes"}, None, {"if-none-match": 'W/"anything"'})
    assert response.status_code == 200 and "etag" not in response.headers and response.headers["cache-control"] == "no-store"


# -- the file response, the scoped library cursor and an unavailable filing ----------------------------------------------------------

def test_the_context_file_response_carries_the_same_filing_as_the_raw_reads(host):
    routes, owner = Routes(host), host.human()
    document, job_id = upload(host, "Met Alice about the pilot.", agent=host.agent(), title="File")
    path = host.data(owner, "context_get", {"document_id": document["id"]})["document"]["path"]
    for stage in ("queued", "published", "applied"):
        if stage == "published":
            host.step()
        elif stage == "applied":
            settle(host, job_id)
        status, body = routes.get(owner, "biz/first/context/file", p=path.removeprefix("context/"))
        assert status == 200 and body["document_id"] == document["id"] and body["text"] == "Met Alice about the pilot."
        assert body["filing"] == shown(host, owner, document["id"])["filing"]
        assert (body["filing"]["state"], body["filing"]["completion_state"]) == {
            "queued": ("queued", None), "published": ("published", "pending"), "applied": ("published", "applied")}[stage]


def test_a_scoped_body_cursor_survives_the_filing_completion_but_not_a_body_change(host):
    owner, text = host.human(), "".join(f"Line {number:03d} of the long source body.\n" for number in range(20))
    document, job_id = upload(host, text, agent=host.agent(), title="Long")
    first = host.read(owner, "context_get", {"document_id": document["id"], "scope": "business", "max_chars": 100})
    assert first["document"]["body"] == text[:100] and first["next_cursor"] and first["document"]["filing"]["state"] == "queued"
    before = first["document"]["revision"]
    host.step()
    settle(host, job_id)                                                       # the completion rewrites the stored source and metadata
    second = host.read(owner, "context_get", {"document_id": document["id"], "scope": "business", "max_chars": 100,
                                              "cursor": first["next_cursor"]})
    assert second["document"]["revision"] != before and second["document"]["body"] == text[100:200]
    assert second["document"]["character_offset"] == 100
    assert second["document"]["filing"]["completion_state"] == "applied" and second["next_cursor"]
    # A cursor is not valid for another document, and not after the body itself changed.
    other, _ = upload(host, text, agent=host.agent(), title="Other")
    assert host.error(owner, "context_get", {"document_id": other["id"], "scope": "business", "cursor": second["next_cursor"]}) == "validation_error"
    changed = host.data(owner, "context_document_update", {"document_id": document["id"], "body": text + "One more line.\n",
                                                           "expected_content_hash": second["document"]["content_hash"]},
                        expected_revision=second["document"]["revision"])["document"]
    assert changed["revision"] != second["document"]["revision"]
    assert host.error(owner, "context_get", {"document_id": document["id"], "scope": "business", "cursor": second["next_cursor"]}) == "validation_error"


def test_an_unreadable_source_has_no_filing_instead_of_an_unfiled_state_and_hides_no_other_content(host, monkeypatch):
    owner = host.human()
    document, _ = upload(host, agent=host.agent(), title="Unreadable")
    listing = {"scope": "business", "kind": "context_upload"}
    composed = scoped(host, owner, document["id"])
    assert composed["filing"]["state"] == "queued" and composed["filing_status"] == "pending"
    filing = host.runtime.domains.documents.filing

    def unreadable(uow, row):
        raise DomainError("document_unavailable", "Registered context source is unavailable")
    monkeypatch.setattr(filing, "current_digest", unreadable)           # a stored source file that cannot be read right now
    (listed,) = [item for item in host.read(owner, "context_list", listing)["items"] if item["id"] == document["id"]]
    (found,) = [item for item in host.read(owner, "context_search", {"scope": "business", "query": "Alice"})["items"]
                if item["id"] == document["id"]]
    for read in (listed, found):
        assert read["filing"] is None and read["filing_status"] is None            # unavailable: neither unfiled nor any other state
        assert read["id"] == document["id"] and read["title"] == composed["title"]  # the permitted summary stays listed
    monkeypatch.undo()
    (back,) = [item for item in host.read(owner, "context_list", listing)["items"] if item["id"] == document["id"]]
    assert back["filing"]["state"] == "queued"                                      # nothing was recorded about the failure


# -- the checked-in captures are what the committed handlers produce ----------------------------------------------------------------

def test_the_checked_in_captures_are_exactly_what_the_registered_handlers_produce(tmp_path):
    import json

    from context_source_status_capture import FIXTURE, generate

    stored = json.loads(FIXTURE.read_text())
    produced = generate(tmp_path)
    assert produced == stored, "Regenerate with: PYTHONPATH=.:tests python tests/context_source_status_capture.py --write"
    states = {(entry["name"].split("/")[0], entry["name"].split("/")[1]) for entry in stored["captures"] if "/" in entry["name"]}
    assert {("pilot", "queued"), ("pilot", "processing"), ("pilot", "blocked"), ("pilot", "published-completion-pending"),
            ("pilot", "applied"), ("pilot", "retracted"), ("pilot", "changed-after-publication"), ("private", "discard-requested"),
            ("private", "discarded"), ("conflict", "completion-conflict"), ("stopped", "job-cancelled-before-publication"),
            ("global", "retract-with-force-refused"), ("global-ops", "retry-pending"), ("global-ops", "selector-refused"),
            ("global-ops", "foreign")} <= states


def test_the_captures_keep_real_types_exact_provenance_and_request_response_relationships():
    import json
    import re
    import uuid

    from context_source_status_capture import FIXTURE

    text = FIXTURE.read_text()
    stored = json.loads(text)
    # The exact dependency commits are provenance, outside every normalization rule.
    assert stored["provenance"]["operations_dependency"] == "2d697fa3973e929f7f2be5afa5e9a731db634aaa"
    assert stored["provenance"]["context_dependency"] == "32431e125b1982ea10fce4db1993ab22c6a241be"
    assert stored["provenance"]["host_base"] == "b9ce256aee2d2564683ff491023b9e7d62822974"
    assert any("not HTTP evidence" in line for line in stored["limitations"]) and any("injected" in line for line in stored["limitations"])
    assert not re.search(r"<[a-z]+:\d+>", text)                                  # no placeholder tokens: values keep their types
    identifiers = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert all(uuid.UUID(value).version in (4, 5) for value in set(identifiers.findall(text)))
    # Every entry names its transport, and an HTTP entry is a real dispatcher path.
    captures = stored["captures"]
    assert {entry["transport"] for entry in captures} == {"http-dispatcher", "application-invoke", "route-handler"}
    assert all(entry["request"]["path"].startswith("/api/") for entry in captures if entry["transport"] == "http-dispatcher")
    operations = {entry["request"]["path"].rsplit("/", 1)[1] for entry in captures
                  if entry["transport"] == "http-dispatcher" and entry["request"]["path"].startswith("/api/root/operations/")}
    assert operations == {"context_list", "context_get", "context_search", "context_retry", "context_retract", "context_queue_get",
                          "context_queue_retry", "context_queue_discard"}
    # Request ids and cursors are the ones an earlier response produced (opaque cursors are never invented).
    seen_ids, seen_cursors = set(), set()

    def cursors(value, found):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("cursor", "next_cursor", "refresh_cursor") and isinstance(item, str):
                    found.add(item)
                cursors(item, found)
        elif isinstance(value, list):
            for item in value:
                cursors(item, found)
        return found

    for entry in captures:
        assert set(identifiers.findall(json.dumps(entry["request"]))) <= seen_ids, entry["name"]
        assert cursors(entry["request"], set()) <= seen_cursors, entry["name"]
        seen_ids |= set(identifiers.findall(json.dumps(entry["response"])))
        seen_cursors |= cursors(entry["response"], set())
    # The exact input read carries its pending control.
    (pending,) = [entry for entry in captures if entry["name"] == "global-ops/retry-pending/queue-get"]
    control = pending["response"]["body"]["data"]["input"]["controls"][0]
    assert (control["intent"], control["status"], control["expected_state"], control["result"]) == ("retry", "pending", "blocked", None)


def test_the_capture_normalizer_keeps_value_shapes_and_cursor_relationships():
    import base64
    import json

    from qorqut_operations.documents.library import token
    from context_source_status_capture import Normalizer

    digest, commit = "a1" * 32, "b2" * 20
    cursor = token({"binding": digest, "offset": 100})
    data = [{"cursor": cursor, "at": "2026-10-12T11:13:57.846424Z", "again": "2026-10-12T11:13:57.846900Z", "later": "2026-10-12 11:14:01",
             "injected": "2026-01-01T12:00:16+00:00", "digest": "sha256:" + digest, "commit": commit, "path": "context/_inbox/2026-10-12-x.md"},
            {"next_cursor": cursor, "event_id": "k3x9q", "source_id": "m4z8p"}]
    first = Normalizer().normalized(data)
    assert first == Normalizer().normalized(data)
    row, other = first
    assert row["at"] == row["again"] == "2026-01-01T00:00:00.000000Z" and row["later"] == "2026-01-01 00:00:01"      # one instant, then the next
    assert row["injected"] == "2026-01-01T12:00:16+00:00" and row["path"] == "context/_inbox/2026-01-01-x.md"        # the injected clock is verbatim
    assert row["digest"].startswith("sha256:") and len(row["digest"]) == 7 + 64 and row["digest"] != "sha256:" + digest
    assert len(row["commit"]) == 40 and row["commit"] != commit and (other["event_id"], other["source_id"]) == ("ev001", "so001")
    assert row["cursor"] == other["next_cursor"] != cursor                                                              # still the same cursor
    assert json.loads(base64.urlsafe_b64decode(row["cursor"])) == {"binding": row["digest"].removeprefix("sha256:"), "offset": 100}
