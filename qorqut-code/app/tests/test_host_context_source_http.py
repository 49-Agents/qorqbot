"""H2-host: the Global operation surface of the source status UI, through the real ``HTTPAPI`` dispatcher.

Real: ``HTTPAPI.__call__`` (origin/CSRF checks, JSON reading, the ``/root/operations`` and ``/biz/{slug}/operations`` dispatch, option
splitting, scope-selector refusal, ``failure`` mapping), the single-owner principal resolution (``Authentication.installation_owner``),
``OriginPolicy``, ``policy.context_for``, ``Application.invoke`` and the composed host of ``context_completion_support`` (the Operations
Context queue and filing, ``DocumentOperations``/``ContextLibrary``, the registered ``context.file`` job, the queue driver, a disposable
Context). Faked: the injected model runner and clock, and the socket (requests are made in process). No server, provider, session or
operational data. Nothing here routes around the dispatcher or stands in for it."""

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from context_completion_support import build
from context_source_status_support import Http, cancel, settle

SOURCE_OPERATIONS = ("context_list", "context_get", "context_search", "context_retry", "context_retract",
                     "context_queue_get", "context_queue_retry", "context_queue_discard")


@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


@pytest.fixture
def http(host):
    return Http(host)


def code(payload):
    return payload["error"]["code"]


def global_upload(host, http, body="Met Alice about the pilot.", **arguments):
    """A Global upload accepted through the dispatcher's retained route: ``({id, revision}, job_id)``."""
    status, payload, _ = http.call("POST", "root/context/upload", {"text": body, "topic": arguments.get("title", "Context upload")})
    assert status == 202, payload
    return {"id": payload["document_id"], "revision": 1}, payload["job"]["id"]


def read(http, document_id, **arguments):
    status, payload = http.operation("context_get", {"scope": "shared", "document_id": document_id, **arguments})
    assert status == 200, payload
    return payload["data"]


def effects(host):
    return {table: host.count(table) for table in ("context_source_versions", "context_filings", "context_inputs", "jobs",
                                                    "idempotency_receipts", "context_input_controls")}


# -- the exposed surface --------------------------------------------------------------------------------------------------------------

def test_exactly_the_eight_source_operations_are_dispatched_at_the_root_and_neighbours_are_not(host, http):
    for name in SOURCE_OPERATIONS:
        status, payload = http.operation(name, {})
        assert status == 200 or code(payload) in ("validation_error",), (name, status, payload)     # reached the application's own schema
    for name in ("context_queue_list", "context_queue_submit", "context_filing_receipt", "context_summarize"):
        status, payload = http.operation(name, {})
        assert (status, code(payload)) == (404, "not_found"), (name, payload)                         # not widened to the catalog
    status, payload, _ = http.call("GET", "root/operations/context_get")
    assert (status, code(payload)) == (404, "not_found")                                               # the dispatcher is POST only
    assert (http.operation("context_queue_get", {}, business="first")[0], http.operation("context_get", {}, business="first")[0]) == (422, 422)


@pytest.mark.parametrize("name", SOURCE_OPERATIONS)
def test_a_root_source_operation_cannot_select_a_business_or_review_business(host, http, name):
    document, _ = global_upload(host, http)
    for selector in ({"business_id": host.ids["first"]}, {"review_business_id": host.ids["first"]}):
        before = effects(host)
        status, payload = http.operation(name, {"scope": "shared", "upload_id": document["id"], "document_id": document["id"],
                                                "input_id": document["id"], "query": "Alice", **selector})
        assert (status, code(payload)) == (403, "forbidden"), (name, selector, payload)
        assert effects(host) == before


# -- Global raw reads ----------------------------------------------------------------------------------------------------------------

def test_global_list_get_and_search_carry_the_same_filing_through_the_dispatcher(host, http):
    document, job_id = global_upload(host, http, title="Global pilot")
    body = {"scope": "shared", "kind": "context_upload", "limit": 5}
    filings = {}
    for stage in ("queued", "blocked", "published", "applied"):
        if stage == "blocked":
            host.runner.fail = RuntimeError("model unavailable")
            host.step()
            host.runner.fail = None
        elif stage == "published":
            host.data(host.human(None), "context_retry", {"upload_id": document["id"]})
            host.step()
        elif stage == "applied":
            settle(host, job_id)
        listed = http.operation("context_list", body)[1]["data"]
        searched = http.operation("context_search", {"scope": "shared", "query": "Alice", "limit": 5})[1]["data"]
        (from_list,) = [item for item in listed["items"] if item["id"] == document["id"]]
        (from_search,) = [item for item in searched["items"] if item["id"] == document["id"]]
        from_get = read(http, document["id"])["document"]
        assert from_list["filing"] == from_search["filing"] == from_get["filing"], stage
        assert from_list["filing_status"] == from_get["filing_status"] == from_get["filing"]["status"]
        filings[stage] = from_get["filing"]
    assert [filings[stage]["state"] for stage in filings] == ["queued", "blocked", "published", "published"]
    assert filings["blocked"]["retryable"] is True and filings["applied"]["completion_state"] == "applied"
    assert filings["applied"]["status"] == "processed" and filings["applied"]["document_revision"] == filings["queued"]["document_revision"] + 1
    # A business scope is not available to the Global caller; another business's source is not found.
    status, payload = http.operation("context_get", {"scope": "business", "document_id": document["id"]})
    assert status >= 400 and code(payload) in ("scope_required", "validation_error", "forbidden"), payload
    other = host.upload(host.human("first"), "First business note.", title="Mine")["document"]
    assert code(http.operation("context_get", {"scope": "shared", "document_id": other["id"]})[1]) == "not_found"
    assert other["id"] not in [item["id"] for item in http.operation("context_list", {"scope": "shared"})[1]["data"]["items"]]
    assert other["id"] not in [item["id"] for item in http.operation("context_search", {"scope": "shared", "query": "First"})[1]["data"]["items"]]


def test_global_list_is_bounded_and_pages_with_its_own_cursor(host, http):
    for number in range(3):
        global_upload(host, http, f"Note number {number} about Alice.", title=f"Note {number}")
    first = http.operation("context_list", {"scope": "shared", "kind": "context_upload", "limit": 2})[1]["data"]
    assert len(first["items"]) == 2 and first["next_cursor"] and first["total"] == 3
    second = http.operation("context_list", {"scope": "shared", "kind": "context_upload", "limit": 2, "cursor": first["next_cursor"]})[1]["data"]
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}


def test_a_global_body_cursor_survives_completion_and_belongs_to_its_document(host, http):
    text = "".join(f"Line {number:03d} of the long Global source body.\n" for number in range(20))
    document, job_id = global_upload(host, http, text, title="Long")
    first = read(http, document["id"], max_chars=100)
    assert first["document"]["body"] == text[:100] and first["next_cursor"]
    host.step()
    settle(host, job_id)                                                              # the completion rewrites the file hash and metadata
    second = read(http, document["id"], max_chars=100, cursor=first["next_cursor"])
    assert second["document"]["content_hash"] != first["document"]["content_hash"]
    assert second["document"]["body"] == text[100:200] and second["document"]["character_offset"] == 100
    other, _ = global_upload(host, http, text, title="Other")
    status, payload = http.operation("context_get", {"scope": "shared", "document_id": other["id"], "max_chars": 100,
                                                     "cursor": second["next_cursor"]})
    assert (status, code(payload)) == (422, "validation_error")


def test_a_business_body_cursor_dies_with_a_real_body_change_through_the_dispatcher(host, http):
    text = "".join(f"Line {number:03d} of the long business source body.\n" for number in range(20))
    document = host.upload(host.human("first"), text, title="Long")["document"]
    arguments = {"scope": "business", "document_id": document["id"], "max_chars": 100}
    first = http.operation("context_get", arguments, business="first")[1]["data"]
    assert first["next_cursor"]
    changed = http.operation("context_document_update", {"document_id": document["id"], "body": text + "One more line.\n",
                                                         "expected_content_hash": first["document"]["content_hash"],
                                                         "expected_revision": first["document"]["revision"], "idempotency_key": "edit-1"},
                             business="first")
    assert changed[0] in (200, 202), changed
    status, payload = http.operation("context_get", {**arguments, "cursor": first["next_cursor"]}, business="first")
    assert (status, code(payload)) == (422, "validation_error")


# -- source commands: supplied revision, replay, no effects ------------------------------------------------------------------------------

def test_global_retry_and_retract_enforce_the_supplied_revision_and_replay_through_the_dispatcher(host, http):
    document, job_id = global_upload(host, http, title="Revisions")
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    current = read(http, document["id"])["document"]["revision"]
    before = effects(host)
    status, payload = http.operation("context_retry", {"upload_id": document["id"], "expected_revision": current + 5, "idempotency_key": "stale-1"})
    assert (status, code(payload)) == (409, "revision_conflict") and payload["error"]["current_revision"] == current
    assert effects(host) == before
    first = http.operation("context_retry", {"upload_id": document["id"], "expected_revision": current, "idempotency_key": "retry-1"})
    assert first[0] in (200, 202) and first[1]["data"]["filing"]["state"] == "blocked" and first[1]["data"]["filing"]["retryable"] is False
    after = effects(host)
    host.step()
    settle(host, job_id)
    settled = read(http, document["id"])["document"]
    assert settled["revision"] != current and settled["filing"]["completion_state"] == "applied"
    replay = http.operation("context_retry", {"upload_id": document["id"], "expected_revision": current, "idempotency_key": "retry-1"})
    assert replay[0] == first[0] and replay[1]["data"] == first[1]["data"]                 # the receipt, although the source moved on
    # Retraction: stale -> conflict without effects; correct -> accepted; identical key -> the same receipt.
    before = effects(host)
    status, payload = http.operation("context_retract", {"upload_id": document["id"], "expected_revision": current, "idempotency_key": "stale-2"})
    assert (status, code(payload)) == (409, "revision_conflict") and payload["error"]["current_revision"] == settled["revision"]
    assert effects(host) == before
    removal = http.operation("context_retract", {"upload_id": document["id"], "expected_revision": settled["revision"], "idempotency_key": "retract-1"})
    assert removal[0] in (200, 202) and removal[1]["data"]["filing"]["operation"] == "remove"
    assert effects(host)["context_source_versions"] == before["context_source_versions"] + 1
    again = http.operation("context_retract", {"upload_id": document["id"], "expected_revision": settled["revision"], "idempotency_key": "retract-1"})
    assert again[1]["data"] == removal[1]["data"] and effects(host)["context_source_versions"] == before["context_source_versions"] + 1
    del after


def test_global_source_commands_refuse_another_namespaces_source_and_unknown_fields(host, http):
    mine, mine_job = global_upload(host, http)
    host.step()
    settle(host, mine_job)                                                            # published, so a retraction is otherwise possible
    foreign = host.upload(host.human("first"), "Business source.", title="Foreign")["document"]
    before = effects(host)
    for name in ("context_retry", "context_retract"):
        status, payload = http.operation(name, {"upload_id": foreign["id"], "expected_revision": foreign["revision"], "idempotency_key": f"foreign-{name}"})
        assert (status, code(payload)) == (404, "not_found"), (name, payload)
        status, payload = http.operation(name, {"upload_id": mine["id"], "force": True})
        assert (status, code(payload)) == (422, "validation_error"), (name, payload)
    assert effects(host) == before
    # The business dispatcher refuses the Global source too, and changes nothing. The existing read access to Global sources reaches
    # the prepare step, which refuses it by its own check: retry as a prepared-target revision_conflict, retract as forbidden.
    refusals = {}
    for name in ("context_retry", "context_retract"):
        status, payload = http.operation(name, {"upload_id": mine["id"], "idempotency_key": f"cross-{name}"}, business="first")
        refusals[name] = (status, code(payload))
    assert refusals == {"context_retry": (409, "revision_conflict"), "context_retract": (403, "forbidden")}
    assert effects(host) == before


# -- exact-input inspection, retry and discard ----------------------------------------------------------------------------------------------

def test_exact_input_get_retry_and_discard_through_the_dispatcher(host, http):
    document, job_id = global_upload(host, http, title="Controls")
    input_id = read(http, document["id"])["document"]["filing"]["input_id"]
    queued = http.operation("context_queue_get", {"input_id": input_id})[1]["data"]["input"]
    assert queued["id"] == input_id and queued["state"] == "queued" and queued["attempt"] == 0 and queued["controls"] == []
    # A never-started input is discarded at once: the control is applied, no mechanical discard is pending.
    discard = http.operation("context_queue_discard", {"input_id": input_id, "expected_state": "queued", "idempotency_key": "discard-queued"})
    assert discard[0] in (200, 202) and discard[1]["data"]["result"]["status"] == "applied"
    assert http.operation("context_queue_get", {"input_id": input_id})[1]["data"]["input"]["state"] == "discarded"
    assert read(http, document["id"])["document"]["filing"]["state"] == "discarded"
    # A blocked head: retry is requested (pending), the control is visible, a second command is refused, the driver then acts.
    second, second_job = global_upload(host, http, "Second note about Bob.", title="Blocked")
    blocked_input = read(http, second["id"])["document"]["filing"]["input_id"]
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    assert http.operation("context_queue_get", {"input_id": blocked_input})[1]["data"]["input"]["state"] == "blocked"
    status, payload = http.operation("context_queue_retry", {"input_id": blocked_input, "expected_state": "queued", "idempotency_key": "wrong-state"})
    assert (status, code(payload)) == (422, "state_conflict")
    retry = http.operation("context_queue_retry", {"input_id": blocked_input, "expected_state": "blocked", "idempotency_key": "retry-blocked"})
    assert retry[0] in (200, 202) and retry[1]["data"]["result"]["status"] == "pending"
    pending = http.operation("context_queue_get", {"input_id": blocked_input})[1]["data"]["input"]
    assert pending["state"] == "blocked" and [control["status"] for control in pending["controls"]] == ["pending"]
    assert pending["controls"][0]["intent"] == "retry"
    for name, key in (("context_queue_retry", "again"), ("context_queue_discard", "discard-while-pending")):
        status, payload = http.operation(name, {"input_id": blocked_input, "expected_state": "blocked", "idempotency_key": key})
        assert (status, code(payload)) == (422, "control_pending"), (name, payload)
    replay = http.operation("context_queue_retry", {"input_id": blocked_input, "expected_state": "blocked", "idempotency_key": "retry-blocked"})
    assert replay[1]["data"] == retry[1]["data"]
    host.step()                                                                         # the driver acts on the request
    done = http.operation("context_queue_get", {"input_id": blocked_input})[1]["data"]["input"]
    assert done["state"] == "published" and [control["status"] for control in done["controls"]] == ["applied"]
    settle(host, second_job)
    # A blocked discard is a pending request until the driver completes it.
    third, third_job = global_upload(host, http, "Third note about Carol.", title="Discarded")
    third_input = read(http, third["id"])["document"]["filing"]["input_id"]
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    requested = http.operation("context_queue_discard", {"input_id": third_input, "expected_state": "blocked", "idempotency_key": "discard-blocked"})
    assert requested[1]["data"]["result"]["status"] == "pending"
    assert http.operation("context_queue_get", {"input_id": third_input})[1]["data"]["input"]["state"] == "discard_requested"
    host.step()
    assert http.operation("context_queue_get", {"input_id": third_input})[1]["data"]["input"]["state"] == "discarded"
    del job_id, third_job


def test_exact_input_commands_refuse_another_namespaces_input_through_the_dispatcher(host, http):
    own, _ = global_upload(host, http)
    own_input = read(http, own["id"])["document"]["filing"]["input_id"]
    foreign_document = host.upload(host.human("first"), "Business source.", title="Foreign")["document"]
    foreign_input = host.query("SELECT id FROM context_inputs WHERE business_id=?", host.ids["first"])[0]["id"]
    before = effects(host)
    for name, body in (("context_queue_get", {}), ("context_queue_retry", {"expected_state": "blocked", "idempotency_key": "k-retry"}),
                       ("context_queue_discard", {"expected_state": "queued", "idempotency_key": "k-discard"})):
        status, payload = http.operation(name, {"input_id": foreign_input, **body})
        assert (status, code(payload)) == (404, "not_found"), (name, payload)
        # ... and the Global input is not reachable from a business dispatcher either.
        status, payload = http.operation(name, {"input_id": own_input, **body}, business="first")
        assert (status, code(payload)) == (404, "not_found"), (name, payload)
    assert effects(host) == before
    assert http.operation("context_queue_get", {"input_id": own_input})[1]["data"]["input"]["state"] == "queued"
    del foreign_document


def test_a_stopped_job_before_publication_still_reads_through_the_dispatcher_without_inventing_a_publication(host, http):
    document, job_id = global_upload(host, http, title="Stopped")
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    cancel(host, job_id)
    shown = read(http, document["id"])["document"]["filing"]
    assert (shown["state"], shown["job_state"], shown["retryable"], shown["published_commit"]) == ("blocked", "cancelled", False, None)
    status, payload = http.operation("context_retry", {"upload_id": document["id"], "idempotency_key": "stopped"})
    assert (status, code(payload)) == (422, "invalid_transition")
