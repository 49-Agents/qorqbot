"""The supervised variant of ``context_edit`` on a synthetic composed host (PRODUCTION-CONTRACT decisions 2 and 3).

Real: the stock host assembly, resolver, policy and application, the actual ``context_edit`` operation with the Context integration's ``GraphEdits``
and its ``SupervisedEdits`` adapter, the Context integration's queue and ``context_graph_edits`` binding (which these cases prove is never
used by the supervised variant), a disposable real Context (real Git) and the library's own ``Pipeline``. Faked: only the model
runner, which fails the test if it is ever called, the injected clock and the synthetic principals, roles and files under a pytest
``tmp_path``. No operational data, provider, model or network is used."""

import hashlib

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.graph_edits")

from qorqut_context import Conflict, Pipeline, graph_edits as graph_library, supervised_edits as library
from qorqut_operations.contracts import plain

from qorqut_context.integration.graph_edits import supervised_edit
from qorqut_context.integration.graph_edits.graph_edit import GraphEditPorts
from context_edit_support import NoModel, build, put_node
from test_context_edit_acceptance import archive, graph, ordubashy, settled
from test_context_edit_delegation import delegate

DOCUMENT = "# Owner-approved correction\r\n\r\nAlice — café notes.  \n\tTrailing spaces kept.  \n"
DOCUMENT_BYTES = DOCUMENT.encode("utf-8")


@pytest.fixture
def host(tmp_path):
    built = build(tmp_path)
    composed = built.runtime.context_services.composition.pipeline
    assert built.edits.ports.supervised_pipeline is composed
    built.pipeline = composed
    yield built
    built.no_model()


def put_note(identity, text, *subjects):
    return {"op": "put_note", "record": {"id": identity, "text": text,
                                         "objects": [{"kind": kind, "id": target} for kind, target in subjects]},
            "explanation": "Record the note.", "citations": ["self"], "support_after": ["self"]}


def plan_of(host, *operations, revision=None):
    return {"format": supervised_edit.FORMAT, "expected_revision": revision or host.head, "rationale": "Owner-approved correction.",
            "operations": list(operations)}


def arguments_of(plan, event_id, **extra):
    return {"edit": plan, "document": DOCUMENT, "event_id": event_id, **extra}


def seed_note(host, event_id, operation, scope):
    record_context = None if scope is None else {"scope": scope, "provenance": {"fixture": event_id}}
    Pipeline(host.context, runner=NoModel()).submit_supervised(
        library.normalize(plan_of(host, operation)), content=b"Seed note\n", submitted_by="fixture", event_id=event_id,
        record_context=record_context)


def before_publication(host, monkeypatch, action):
    supervised = host.runtime.domains.context_sources.supervised
    original, done = supervised.prepared, []

    def prepared(plan):
        if not done:
            done.append(True)
            action()
        return original(plan)

    monkeypatch.setattr(supervised, "prepared", prepared)


def test_a_preview_is_the_unchanged_pure_engine_preview_and_changes_nothing(host, monkeypatch):
    graph(host)
    owner = host.human("first")
    plan = plan_of(host, put_node("n1111", "New person"), put_note("t1111", "Alice prefers email.", ("node", "aaaaa"), ("node", "n1111")))

    def forbidden(*args, **kwargs):
        raise AssertionError("a preview never publishes")

    monkeypatch.setattr(Pipeline, "submit_supervised", forbidden)
    before, files = host.fingerprint(), dict(host.context.snapshot().files)
    preview = host.data(owner, "context_edit", arguments_of(plan, "ev001", preview_only=True))
    with host.database.read() as uow:
        attribution = host.edits.attribution(owner, uow, host.ids["first"])
    expected = host.pipeline.preview_supervised(
        library.normalize(plan), content=DOCUMENT_BYTES, submitted_by=host.owner, event_id="ev001",
        record_context=supervised_edit.record_context_of(attribution, host.ids["first"]))
    assert preview == plain(expected) and preview["status"] == "ready"
    assert [item["op"] for item in preview["operations"]] == ["put_node", "put_note"]
    assert preview["source_path"] == "foundation/supervised-edits/ev001.md"
    assert preview["document_digest"] == hashlib.sha256(DOCUMENT_BYTES).hexdigest()
    assert host.fingerprint() == before and dict(host.context.snapshot().files) == files and host.context.pending() is None
    assert host.inputs() == [] and host.bindings() == []


def test_a_note_and_node_edit_publishes_directly_with_the_exact_document_and_no_model_or_queue_binding(host):
    graph(host)
    agent_id, agent = ordubashy(host)
    plan = plan_of(host, put_node("n1111", "New person"), put_node("aaaaa", "Alice Smith"),
                   put_note("t1111", "Alice prefers email.", ("node", "aaaaa"), ("node", "n1111")))
    base = host.head
    preview = host.data(agent, "context_edit", arguments_of(plan, "ev002", preview_only=True))
    assert preview["status"] == "ready" and host.head == base
    assert host.inputs() == [] and host.context.pending() is None
    receipt = host.data(agent, "context_edit", arguments_of(plan, "ev002"))
    snapshot = host.context.snapshot()
    marker = library.marker(library.normalize(plan), DOCUMENT_BYTES)
    assert receipt["status"] == "published" and receipt["event_id"] == "ev002" and receipt["model"] is None
    assert receipt["execution"] == marker and receipt["base_revision"] == base
    assert receipt["published_commit"] == snapshot.revision == snapshot.event_commits["ev002"]
    assert snapshot.files["foundation/supervised-edits/ev002.md"] == DOCUMENT_BYTES
    event = snapshot.events["ev002"]
    assert event["execution"] == marker and event["submitted_by"] == agent_id and event["record_context"]["scope"] == host.scope("first")
    attribution = event["record_context"]["provenance"]["attribution"]
    assert attribution["principal_id"] == agent_id and attribution["role_id"] == "ordubashy" and attribution["delegation"] is None
    assert {(change["kind"], change["id"], change["operation"]) for change in event["changes"]} == {
        ("node", "n1111", "add"), ("node", "aaaaa", "edit"), ("note", "t1111", "add")}
    assert snapshot.notes["t1111"] == {"id": "t1111", "text": "Alice prefers email.",
                                       "objects": [{"kind": "node", "id": "aaaaa"}, {"kind": "node", "id": "n1111"}]}
    assert [item["event_id"] for item in snapshot.history("t1111", kind="note")] == ["ev002"]
    assert host.inputs() == [] and host.bindings() == [] and host.context.pending() is None
    assert "foundation/edits/ev002.txt" not in snapshot.files


def test_the_same_event_document_and_plan_is_reconciled_once_and_any_difference_is_refused(host):
    graph(host)
    _, agent = ordubashy(host)
    arguments = arguments_of(plan_of(host, put_node("n1111", "New person")), "ev003")
    first = host.data(agent, "context_edit", arguments, key="first-call")
    published = host.events()
    assert host.data(agent, "context_edit", arguments, key="first-call") == first
    host.seed([("zzzzz", "Later")], scope=host.scope("first"), event_id="s0099")
    moved, head = host.events(), host.head
    again = host.data(agent, "context_edit", arguments, key="second-call")
    assert again["published_commit"] == first["published_commit"] and again["execution"] == first["execution"]
    assert again["model"] is None and host.events() == moved == [*published, "s0099"] and host.head == head
    assert host.error(agent, "context_edit", {**arguments, "document": DOCUMENT + "More\n"}, key="third-call") == "conflict"
    changed = {**arguments, "edit": plan_of(host, put_node("n2222", "Other person"), revision=arguments["edit"]["expected_revision"])}
    assert host.error(agent, "context_edit", changed, key="fourth-call") == "conflict"
    assert host.events() == moved and host.head == head


def test_a_stale_base_a_busy_writer_and_a_foreign_pending_job_publish_nothing(host, monkeypatch):
    graph(host)
    _, agent = ordubashy(host)
    stale = plan_of(host, put_node("n1111", "New person"))
    host.seed([("zzzzz", "Later")], scope=host.scope("first"), event_id="s0099")
    assert host.error(agent, "context_edit", arguments_of(stale, "ev004")) == "conflict"
    fresh = plan_of(host, put_node("n1111", "New person"))
    host.context.begin_edit(graph_library.normalize(host.plan(put_node("p1111", "Pending person"))), submitted_by="fixture",
                            event_id="pend1")
    before = host.fingerprint()
    foreign = host.call(agent, "context_edit", arguments_of(fresh, "ev005"))
    assert foreign.status == "error" and foreign.error["code"] == "busy" and foreign.error["retryable"] is True
    assert host.fingerprint() == before and host.context.pending()["id"] == "pend1"
    assert "foundation/supervised-edits/ev005.md" not in host.context.snapshot().files

    def writer_busy(*args, **kwargs):
        raise Conflict("Another writer/query is using this shared context")

    monkeypatch.setattr(host.pipeline, "preview_supervised", writer_busy)
    contended = host.call(agent, "context_edit", arguments_of(fresh, "ev006"))
    assert contended.status == "error" and contended.error["code"] == "busy" and contended.error["retryable"] is True
    assert host.fingerprint() == before


def test_foreign_scope_note_authority_and_a_revoked_or_delegated_caller_are_refused_before_publication(host, monkeypatch):
    graph(host)
    agent_id, agent = ordubashy(host)
    seed_note(host, "sn001", put_note("t2222", "Bob likes tea.", ("node", "bbbbb")), host.scope("second"))
    seed_note(host, "sn002", put_note("t3333", "Unrecorded note.", ("node", "aaaaa")), None)
    snapshot = host.context.snapshot()
    first = host.ids["first"]

    def registered(business):
        return business in host.ids.values()

    def needed(plan, admission=first):
        targets = supervised_edit.analyze(library.normalize(plan), host.context.snapshot(), admission, registered)
        return supervised_edit.needed_namespaces(targets, admission)

    assert needed(plan_of(host, put_note("t2222", "Bob likes coffee.", ("node", "aaaaa")))) == {first, host.ids["second"]}
    assert needed(plan_of(host, put_note("t3333", "Changed.", ("node", "aaaaa")))) == {first, None}
    assert needed(plan_of(host, put_note("t4444", "Floating note."))) == {first, None}
    assert needed(plan_of(host, put_note("t4444", "Alice note.", ("node", "aaaaa")))) == {first}
    assert snapshot.notes["t2222"]["objects"] == [{"kind": "node", "id": "bbbbb"}]

    archive(host, "second")
    before = host.fingerprint()
    foreign = plan_of(host, put_note("t2222", "Bob likes coffee.", ("node", "aaaaa")))
    assert host.error(agent, "context_edit", arguments_of(foreign, "ev007")) == "business_archived"
    assert host.fingerprint() == before

    own = plan_of(host, put_node("n1111", "New person"))
    before_publication(host, monkeypatch, lambda: host.set_overrides(agent_id, {"context_edit": "deny"}))
    assert host.error(agent, "context_edit", arguments_of(own, "ev008")) == "policy_denied"
    assert host.fingerprint() == before and "ev008" not in host.events()
    host.set_overrides(agent_id, {})

    live = delegate(host)
    worker = live.context()
    preview = host.data(worker, "context_edit", arguments_of(own, "ev009", preview_only=True))
    assert preview["status"] == "ready"
    before_publication(host, monkeypatch, live.revoke_binding)
    result = host.call(worker, "context_edit", arguments_of(own, "ev009"))
    assert result.status == "error"
    assert host.fingerprint() == before and "ev009" not in host.events()


def test_the_queued_graph_edit_and_its_default_ports_are_unchanged_without_a_supervised_pipeline(tmp_path):
    host = build(tmp_path)
    graph(host)
    assert host.edits.ports.supervised_pipeline is host.runtime.context_services.composition.pipeline
    host.edits.ports = GraphEditPorts(host.edits.ports.context, source_bytes=host.edits.ports.source_bytes)
    assert host.edits.ports.supervised_pipeline is None
    owner = host.human("first")
    queued = host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    assert queued["input"]["state"] == "queued" and queued["execution"]["kind"] == "deterministic_graph_edit"
    assert len(host.inputs()) == 1 and len(host.bindings()) == 1
    assert [result["status"] for result in settled(host)][0] == "published"
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith"
    assert host.error(owner, "context_edit", {"edit": host.plan(put_node("aaaaa", "Alice Smith")), "event_id": "ev010"}) == "validation_error"
    supervised = arguments_of(plan_of(host, put_node("n1111", "New person")), "ev011")
    assert host.error(owner, "context_edit", supervised) == "operation_unavailable"
    assert "ev011" not in host.events()
    host.no_model()
