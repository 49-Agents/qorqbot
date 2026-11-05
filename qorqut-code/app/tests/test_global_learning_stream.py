"""One Global learning stream; business learning is kept as history (Manager contract 2419, owner G022).

Contract:
- ``learnings.extract`` and ``learnings.extract_global`` are Global
  only. A root routine runs extraction once in Global; an each-business learning routine is refused and queues nothing.
- The standalone procedural engine program and its operations are not registered at all: ``learnings.engine_extract`` is
  an ordinary unknown program, its historical jobs stay readable, and no configuration can run it.
- A business ``learnings_extract`` is refused before any job, Signoff or record.
- A business job, whether saved under the earlier definition or hand-made with today's, stops before any transcript read,
  model call or learning write. So does a stale prepared ``learnings_record_batch`` output.
- The business's retained learning rows and evidence are unchanged by all of this.
- Historical business learning stays readable through the ordinary factual readers and the read-only archive inventory.
- Global selection keeps hash dedupe and its bounds. It never takes Learning Applier
  or business-home transcripts, and retired globals stay eligible.
- Founding gives the Global-only learning workers no business membership and keeps every other business worker.
- Owner Signoff answers and Task notes of Global and every active business feed the one Global pass (G023) under one
  bound, in contribution time order, with their original business attribution and hash. A business one has the Global
  scan key ``business:<id>:kind:id``. It is skipped when that hash completed in Global or in its own business's scan
  state, neither of which is copied or reset. A changed hash enters once; parent Task edits never replay a note. Retry
  and paused states keep their meaning, and archived businesses, bare approvals and legacy evidence stay out.
- A migrated note (exact migration attribution and ID mapping) also consults its v1 progress in its original scope (2422).
  A matching recomputed v1 hash skips it; a changed Task note enters once; wrong or malformed provenance is no proof.
  A Story note with v1 progress it cannot be proven to match is paused in Global scan state, never extracted, completed
  or re-recorded, and reported in the run diagnostics. A Story note with no v1 progress is ordinary intake.

Real: the stock host assembly and runtime, ``attach_background`` with the real programs, worker authority, job service,
scheduler and learning operations, the owner and registered globals created through the real catalog, and ``LocalFiles``
transcript selection. Synthetic: the tmp instance, the historical learning rows, business-home identities, Tasks, notes and
answered Signoffs seeded as stored rows, a fake native-archive listing, spies for the transcript source, model factory and
engine ``open`` (each fails if called), and for the contribution runs only an in-process chat client that echoes each
owner contribution as one lesson. No model, provider, engine, network or operational data is used.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from bazarlyk.domains.learnings import sources
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, canonical_hash, new_id, utc_now

from bazarlyk.background.integration import attach_background
from bazarlyk.background.learnings import LearningProgram
from bazarlyk.background.models import ModelCalls
from bazarlyk.domains.learnings.scope import GLOBAL_ONLY
from bazarlyk.service.local import LocalFiles
from host_foundation_support import (REGISTRATION_NAMES, build_instance, create_registered_global, create_worker,
                                     grant_capabilities, historical_business_agent, historical_founder, owner_global_context,
                                     publish_role, register_sources, set_agent_overrides)

LEARNING_PROGRAMS = ("learnings.extract", "learnings.extract_global")
LEARNING_TABLES = ("learning_candidates", "learning_decisions", "learning_scan_state", "learning_runs", "learning_history")
JSONL = '{"type":"user","message":{"role":"user","content":"Keep invoices short."}}\n'


class Spies:
    """The transcript source, model factory and engine ``open``: none may be reached on a refused path."""

    def __init__(self):
        self.transcripts, self.models = [], []
        self.client = None                                                  # set only by the contribution runs

    def transcript_source(self, business_id, agent_id=None):
        self.transcripts.append((business_id, agent_id))
        return iter(())

    def model_factory(self, business_id):
        self.models.append(business_id)
        if self.client is None:
            raise AssertionError("no model may be called")
        return self.client

    def untouched(self):
        return (self.transcripts, self.models) == ([], [])


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    spies = Spies()
    background = attach_background(instance.runtime, instance.runtime.programs, transcript_source=spies.transcript_source,
                                   model_client_factory=spies.model_factory)
    instance.runtime.freeze_registry()
    with instance.database.transaction() as uow:
        # The saved worker memberships of existing businesses, every program's included, as earlier founding left them.
        instance.runtime.authority.register_workers(uow, list(ids.values()))
    for slug, business_id in ids.items():
        register_sources(instance, business_id, slug)
    try:
        yield SimpleNamespace(instance=instance, ids=ids, spies=spies, background=background, tmp=tmp_path)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def query(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def learning_rows(host):
    """Every learning row, in full: the business history a refused or read-only path must leave exactly as it was."""
    return {table: query(host, f"SELECT * FROM {table} ORDER BY rowid") for table in LEARNING_TABLES}


def invoke(host, ctx, name, arguments, **options):
    return host.instance.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=new_id(), **options))


def data(host, ctx, name, arguments):
    result = invoke(host, ctx, name, arguments)
    assert result.status == "ok", (name, result.error)
    return result.data


def refused(host, ctx, name, arguments, message=GLOBAL_ONLY):
    result = invoke(host, ctx, name, arguments)
    assert result.status == "error" and result.error["code"] == "forbidden", (name, result.status, result.error)
    assert result.error["message"] == message, result.error
    return result.error


def owner(host, business="first"):
    return host.instance.human(host.ids[business])


def retire(host, agent_id):
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (agent_id,))
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (agent_id,))
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id=?", (agent_id,))


def source_id(agent_id, epoch, codex=False):
    return f"agent:{agent_id}:{epoch}" + (":codex" if codex else "")


def learning_applier(instance):
    """SYNTHETIC CONTRACT FIXTURE: the parallel role update (1.16.0) is absent from this isolated tree, so publish a minimal
    Learning Applier package naming only retained operations. This weakens no production validation and is replaced by the
    real role artifact after integration."""
    role = "learning-applier"
    if role not in instance.role_pins:
        source = Path(instance.workspace) / "synthetic-learning-applier"
        (source / "roles" / role).mkdir(parents=True, exist_ok=True)
        (source / "roles" / role / "BRIEF.md").write_text("Synthetic Learning Applier brief for an isolated check.\n")
        (source / "roles" / role / "role.yaml").write_text(
            "schema_version: 1\nid: learning-applier\nversion: 1.16.0\nscope_kinds: [ecosystem]\n"
            "brief: roles/learning-applier/BRIEF.md\nscope_brief: ecosystem-agent\nskills: []\n"
            "default_tool_policy:\n  learnings_review_list: allow\n  learnings_review_get: allow\n"
            "  learnings_legacy_pending: allow\n  learnings_decide: allow\n")
        instance.runtime.publisher.sources[role] = ("role", source, f"roles/{role}/role.yaml")
        publish_role(instance, role)
    if role not in instance.registered:
        name, display_name = REGISTRATION_NAMES[role]
        created = instance.invoke(owner_global_context(instance), "agents_create", {
            "role": instance.role_pins[role], "plugins": [], "name": name, "display_name": display_name,
            "assignment_brief": "Synthetic fixture agent.",
            "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}})
        assert created.status == "ok", created.error
        instance.registered[role] = created.data["agent_id"]
    return instance.registered[role]


def seed_history(host, business="first"):
    """HISTORICAL: a business's saved learning as v1 stored it. That is a reviewed transcript candidate with its decision,
    run, cursor and history, kept readable after the procedural route was retired."""
    business_id, now = host.ids[business], utc_now()
    create_worker(host.instance, "colleague")      # the registered global profile historical rows rehomed from
    retired = historical_business_agent(host.instance, business_id, status="inactive")
    retire(host, retired)
    with host.instance.database.transaction() as uow:
        connection = uow.connection
        connection.execute("INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (business_id, "legacy-item", json.dumps({"text": "Quote the delivery date in every invoice.", "bucket": "delta",
             "scope": "agent", "tag": "rule", "observed": {"biz": business_id, "agent": retired, "agent_kind": "business-agent"},
             "evidence": {"session": "session-1"}, "run": "run-1"}), now, now))
        connection.execute("INSERT INTO learning_decisions(business_id,candidate_id,state,decided_by,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?)", (business_id, "legacy-item", "accepted", host.instance.owner, now, now))
        connection.execute("INSERT INTO learning_runs(business_id,id,result_json,created_at) VALUES (?,?,?,?)",
                           (business_id, "run-1", json.dumps({"id": "run-1", "cores": {}}), now))
        connection.execute("INSERT INTO learning_scan_state(business_id,session_id,transcript_hash,state_json,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?)", (business_id, "session-1", "a" * 64, json.dumps({"status": "complete"}), now, now))
        connection.execute("INSERT INTO learning_history(business_id,id,candidate_id,event,actor_id,before_json,after_json,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?)", (business_id, "history-1", "legacy-item", "learnings_review",
                                                        host.instance.owner, "{}", json.dumps({"state": "accepted"}), now))
    return SimpleNamespace(retired=retired)


def worker(host, program):
    with host.instance.database.read() as uow:
        return host.instance.runtime.authority.worker_id(program, uow)


def saved_job(host, program, business="first", *, digest=None, outputs=None):
    """A business job row as a scheduler or an earlier run left it: queued, or applying its frozen outputs."""
    definition = dict(host.instance.runtime.programs.require(program, available=False).definition)
    if digest:
        definition["digest"] = digest
    identity, now = new_id(), utc_now()
    result = {"outputs": outputs, "outputs_hash": canonical_hash(outputs), "applied": {}, "data": {}} if outputs else None
    with host.instance.database.transaction() as uow:
        uow.connection.execute("INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,arguments_json,definition_ref_json,"
            "dedupe_key,state,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, host.ids[business], "business:" + host.ids[business], program, worker(host, program), "{}",
             json.dumps(definition), identity, "applying" if outputs else "queued", json.dumps(result) if result else None, now, now))
    return identity


# -- routines -----------------------------------------------------------------------------------------------------------------

def test_scheduled_learning_runs_once_in_global_and_a_business_learning_routine_is_refused(host):
    for program in LEARNING_PROGRAMS:
        assert host.instance.runtime.programs.require(program, available=False).scope_kinds == ("global",), program
    ctx = owner_global_context(host.instance)

    def definition(target, program="learnings.extract"):
        return {"name": "Learning " + target, "schedule": "*/5 * * * *", "timezone": "UTC", "status": "active",
                "program": program, "arguments": {}, "target": target}

    routine = data(host, ctx, "routines_create", {"scope": "global", "definition": definition("root")})["records"]
    routine = next(row for row in routine if "job_kind" in row)
    result = invoke(host, ctx, "routines_create", {"scope": "global", "definition": definition("each-business")})
    assert result.status == "error" and result.error["code"] == "validation_error", result.error
    result = invoke(host, owner(host), "routines_create", {"definition": definition("business")})
    assert result.status == "error" and result.error["code"] == "validation_error", result.error
    # The removed engine program is an ordinary unknown program: it cannot be scheduled at all.
    for target in ("each-business", "business"):
        result = invoke(host, owner(host), "routines_create", {"definition": definition(target, "learnings.engine_extract")})
        assert result.status == "error", result.error
    scheduler = host.background.scheduler
    from datetime import datetime, timezone
    assert scheduler.tick(datetime(2026, 10, 12, 12, 1, tzinfo=timezone.utc)) == []
    queued = scheduler.tick(datetime(2026, 10, 12, 12, 6, tzinfo=timezone.utc))
    assert len(queued) == 1 and scheduler.errors == []
    assert query(host, "SELECT business_id, target_namespace, kind FROM jobs") == [
        {"business_id": None, "target_namespace": "global", "kind": "learnings.extract"}]
    assert [row["target_namespace"] for row in query(host, "SELECT target_namespace FROM routine_slots WHERE routine_id=?", routine["id"])] == ["global"]


# -- refusals before any work -------------------------------------------------------------------------------------------------

def test_business_extraction_and_enrollment_are_refused_before_any_job_signoff_or_record(host):
    history = seed_history(host)
    ordubashy = create_registered_global(host.instance, "ordubashy")
    before, jobs = learning_rows(host), query(host, "SELECT id FROM jobs")
    refused(host, owner(host), "learnings_extract", {})
    refused(host, owner(host), "learnings_extract", {"agent_id": history.retired, "max_sessions": 5})
    # Ordubashy's role would stage a Signoff for a business extraction: it is refused before one is staged.
    refused(host, InvocationContext(Principal(ordubashy, "agent"), business_id=host.ids["first"], source="mcp"), "learnings_extract", {})
    assert learning_rows(host) == before and query(host, "SELECT id FROM jobs") == jobs
    assert query(host, "SELECT id FROM signoffs") == [] and host.spies.untouched()
    # Global keeps its explicit extraction and its Learning Applier exclusion.
    data(host, owner_global_context(host.instance), "learnings_extract", {"max_sessions": 5})
    assert query(host, "SELECT business_id, kind FROM jobs") == [{"business_id": None, "kind": "learnings.extract"}]
    applier = learning_applier(host.instance)
    refused(host, owner_global_context(host.instance), "learnings_extract", {"agent_id": applier, "max_sessions": 5},
            message="Selected transcript agent is outside this learning workspace or excluded")
    assert learning_rows(host) == before
    assert host.spies.untouched()


def test_saved_or_hand_made_business_learning_jobs_stop_before_transcripts_models_or_any_learning_write(host):
    history = seed_history(host)
    before = learning_rows(host)
    old = saved_job(host, "learnings.extract", digest="0" * 64)               # saved under the earlier definition
    legacy = saved_job(host, "learnings.extract")
    batch = saved_job(host, "learnings.extract", outputs=[{"operation": "learnings_record_batch", "arguments": {
        "items": [], "run": {"id": new_id(), "cores": {}},
        "processed": [{"session_id": "session-1", "transcript_hash": "d" * 64, "source_id": "native-archive/x", "agent_id": history.retired}]}}])
    outcomes = {name: host.background.run_one(job) for name, job in
                (("old", old), ("legacy", legacy), ("batch", batch))}
    assert outcomes["old"]["error"]["code"] == "policy_denied"
    for name in ("legacy", "batch"):
        assert (outcomes[name]["error"]["code"], outcomes[name]["error"]["message"]) == ("forbidden", GLOBAL_ONLY), (name, outcomes[name])
    assert {row["state"] for row in query(host, "SELECT state FROM jobs")} == {"failed"}
    # A saved engine job is an unknown program: its historical row stays readable and nothing can run it.
    assert not host.instance.runtime.programs.get("learnings.engine_extract")
    with pytest.raises(DomainError) as stopped:
        host.instance.runtime.programs.require("learnings.engine_extract")
    assert stopped.value.code == "operation_unavailable"
    # Directly invoked, the factual program refuses its business scope as its first step.
    with pytest.raises(DomainError) as stopped:
        host.instance.runtime.programs.require("learnings.extract").run(SimpleNamespace(business_id=host.ids["first"]), {})
    assert stopped.value.code == "forbidden"
    assert host.spies.untouched()
    assert learning_rows(host) == before


# -- historical evidence reads -----------------------------------------------------------------------------------------------

def test_historical_deltas_stay_readable_in_their_own_scope_after_the_engine_is_gone(host):
    seed_history(host)
    before = learning_rows(host)
    applier = learning_applier(host.instance)
    for ctx in (owner(host), InvocationContext(Principal(applier, "agent"), business_id=host.ids["first"], source="mcp")):
        bundle = data(host, ctx, "learnings_review_get", {"learning_id": "legacy-item"})
        assert bundle["read_only"] is True and bundle["source_kind"] == "frozen_extraction_cores"
        assert [(source["run"], source["session"], source["status"]) for source in bundle["sources"]] == [("run-1", "session-1", "missing")]
        assert (bundle["evidence_status"], bundle["references_complete"], bundle["evidence_complete"]) == ("missing", True, True)
        assert [event["id"] for event in bundle["history"]] == ["history-1"] and bundle["history_complete"] is True
    # The legacy record, its decision and its inventory stay readable; the archive is never the active agenda.
    assert not data(host, owner(host), "learnings_list", {})["items"]
    assert {item["id"] for item in data(host, owner(host), "learnings_list", {"bucket": "delta"})["items"]} == {"legacy-item"}
    assert data(host, owner(host), "learnings_get", {"learning_id": "legacy-item"})["item"]["state"] == "accepted"
    assert [item["learning_id"] for item in data(host, owner(host), "learnings_legacy_pending", {})["items"]] == ["legacy-item"]
    assert learning_rows(host) == before and host.spies.untouched()


# -- Global selection ---------------------------------------------------------------------------------------------------------

class Archive:
    """A native-archive listing: one capture per registered identity; records whose content was read."""

    def __init__(self):
        self.read = []

    def listing(self, agent_id):
        return [{"conversation_id": "conversation-" + agent_id, "provider": "claude"}]

    def content(self, agent_id, capture):
        self.read.append(agent_id)
        return JSONL.encode()

    def source(self, agent, capture, content):
        return {"session_id": capture["conversation_id"], "source_id": f"native-archive/{agent['agent_id']}/claude/{capture['conversation_id']}",
                "agent_id": agent["agent_id"], "agent_kind": agent["profile_json"].get("role", {}).get("id", ""),
                "content": content.decode(), "transcript_hash": hashlib.sha256(content).hexdigest()}


def test_global_selection_keeps_dedupe_bounds_and_admits_every_eligible_source_and_never_takes_applier_or_business_home_input(host):
    ids = {"ordubashy": create_registered_global(host.instance, "ordubashy"),
           "learning-applier": learning_applier(host.instance)}
    ids["colleague"] = create_worker(host.instance, "colleague")
    ids["founder"] = historical_founder(host.instance)                    # the retired Founder's saved sessions stay history, as stored
    retire(host, ids["founder"])                                          # a retired global's captured sessions stay eligible
    business_home = historical_business_agent(host.instance, host.ids["first"])
    archive = Archive()
    files = LocalFiles(SimpleNamespace(database=host.instance.database, native_chat=SimpleNamespace(transcripts=archive)))
    offered = list(files.iter_transcripts(None))
    assert {source["agent_id"] for source in offered} == {ids["ordubashy"], ids["founder"], ids["colleague"]}
    assert ids["learning-applier"] not in archive.read and business_home not in archive.read
    with pytest.raises(DomainError) as explicit:
        list(files.iter_transcripts(None, ids["learning-applier"]))
    assert explicit.value.code == "forbidden"
    # The program's own Global selection: hashes already seen are skipped and bounds hold.
    program = LearningProgram(ModelCalls(None), files.iter_transcripts)
    control = SimpleNamespace(business_id=None, check=lambda: None, service=SimpleNamespace(database=host.instance.database, paths=None))
    selected = list(program.snapshots(control, None, 100, {}))
    assert {source["agent_id"] for source in selected} == {ids["ordubashy"], ids["founder"], ids["colleague"]}
    seen = {source["session_id"]: source["transcript_hash"] for source in selected if source["agent_id"] == ids["ordubashy"]}
    assert {source["agent_id"] for source in program.snapshots(control, None, 100, seen)} == {ids["founder"], ids["colleague"]}
    assert len(list(program.snapshots(control, None, 1, {}))) == 1
    # Input that a misbehaving source might still offer never becomes a Global source.
    by_agent = {source["agent_id"]: source for source in offered}
    applier_source = {**by_agent[ids["ordubashy"]], "session_id": "applier", "agent_id": ids["learning-applier"]}
    home_source = {**by_agent[ids["ordubashy"]], "session_id": "home", "agent_id": business_home}
    injected = LearningProgram(ModelCalls(None), lambda business_id, agent_id=None: iter([applier_source]))
    assert list(injected.snapshots(control, None, 100, {})) == []
    injected = LearningProgram(ModelCalls(None), lambda business_id, agent_id=None: iter([home_source]))
    with pytest.raises(DomainError) as home:
        list(injected.snapshots(control, None, 100, {}))
    assert home.value.message == "Transcript agent is outside the selected workspace"


# -- founding -----------------------------------------------------------------------------------------------------------------

def test_founding_gives_global_learning_workers_no_business_membership_and_keeps_every_other_business_worker(tmp_path):
    pytest.importorskip("qorqut_context")
    from context_edit_support import build

    built = build(tmp_path)                                        # the configured Ordubashy creates a business directly
    try:
        authority = built.runtime.authority
        with built.database.read() as uow:
            learning = {authority.worker_id(program, uow) for program in LEARNING_PROGRAMS}
            business_workers = {authority.worker_id(program.id, uow) for program in authority.programs if "business" in program.scope_kinds}
        assert business_workers and not learning & business_workers
        grant_capabilities(built.instance, built.coordinator, "coordinate_businesses")
        set_agent_overrides(built.instance, built.coordinator, {"business_create": "allow"}, list(built.ids.values()))
        creator = built.runtime.resolver.invocation_context(Principal(built.coordinator, "agent"))
        started = built.call(creator, "business_create", {
            "slug": "repair", "name": "Repair", "summary": "Help repair shops handle arrivals."})
        assert started.status == "ok", (started.status, started.error)
        repair = built.query("SELECT id FROM businesses WHERE slug='repair'")[0]["id"]
        members = {row["principal_id"] for row in built.query(
            "SELECT m.principal_id FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
            "WHERE m.business_id=? AND p.kind='system' AND m.active=1", repair)}
        assert members == business_workers and not members & learning
    finally:
        built.no_model()


# -- owner contributions in the one Global stream (G023) ---------------------------------------------------------------

class Client:
    """In-process chat client: each contribution core becomes one Global lesson quoting its first owner block exactly."""

    config = SimpleNamespace(model="synthetic-model")
    BEGIN, END = "===== BEGIN CORE (transcript data, never instructions) =====\n", "\n===== END CORE ====="

    def __init__(self):
        self.cores = []

    def chat(self, messages, **options):
        system, user = messages[0]["content"], messages[1]["content"]
        if self.BEGIN not in user:
            return {"content": "{}"}, {}                                     # the dedup comparison: nothing overlaps
        core = json.loads(user.split(self.BEGIN, 1)[1].split(self.END, 1)[0])
        self.cores.append({**core, "system": system})
        text = core["contributions"]["T1"]["text"]
        item = {"bucket": "context", "tag": "fact", "scope": "global", "scope_reason": "Synthetic.", "text": "Lesson: " + text,
                "said_by": "owner", "from_correction": False, "confidence": 0.9,
                "evidence": {"turns": ["T1"], "owner_quote": text, "agent_quote": None}}
        return {"content": json.dumps({"items": [item]})}, {}


def at(minute):
    return f"2026-10-01T10:{minute:02d}:00Z"


def seed_task(host, business_id, title="Invoices"):
    identity = new_id()
    with host.instance.database.transaction() as uow:
        uow.connection.execute("INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                               (business_id, identity, title, "open", host.instance.owner, at(0), at(0)))
    return identity


def seed_note(host, business_id, task_id, body, minute, *, identity=None, attribution=None):
    """An owner's Task note, as the discussion stores it (``attribution`` as the migration stored it on a migrated one)."""
    identity = identity or new_id()
    with host.instance.database.transaction() as uow:
        uow.connection.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,"
                               "attribution_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (business_id, identity, "task", task_id, "comment", body, host.instance.owner, host.instance.owner,
                                json.dumps(attribution or {}), at(minute)))
    return identity


def seed_answer(host, business_id, minute, *, notes=None, kind="respond", legacy=False):
    """An answered owner Signoff, as the Signoff store keeps it."""
    identity = new_id()
    decision = {"kind": kind, "by": host.instance.owner, "at": at(minute), **({"notes": notes} if notes else {})}
    preview = {"title": "Which supplier?", "explanation": "Pick one.", "input": {"choices": []}}
    with host.instance.database.transaction() as uow:
        sequence = uow.connection.execute("SELECT COALESCE(MAX(updated_sequence), 0) + 1 FROM signoffs").fetchone()[0]
        uow.connection.execute(
            "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,operation_version,"
            "proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,policy_binding_json,affected_scope_json,"
            "state,updated_sequence,decision_json,created_at,updated_at,category,legacy_evidence_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, business_id, "business" if business_id else "global", host.instance.owner, "owner_input_request", 1, 1,
             "e" * 64, b"", json.dumps(preview), "[]", "{}", "{}", "answered", sequence, json.dumps(decision), at(minute),
             at(minute), "input_request", json.dumps({"v1": True}) if legacy else None))
    return identity


def selection(host, seen=None, *, retry_failed=False, limit=50):
    with host.instance.database.read() as uow:
        return sources.snapshots(uow, seen or {}, retry_failed=retry_failed, limit=limit)


def global_seen(host, retry_failed=False):
    """The Global pass's own ``seen``: its counted scan hashes."""
    return {row["session_id"]: row["transcript_hash"] for row in query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NULL")
            if sources.counted(json.loads(row["state_json"]), retry_failed)}


def seed_scan(host, business_id, key, digest, **state):
    """A scan cursor exactly as an earlier pass recorded it (a contribution's record carries no status)."""
    with host.instance.database.transaction() as uow:
        uow.connection.execute("INSERT INTO learning_scan_state(business_id,session_id,transcript_hash,state_json,created_at,updated_at) "
                               "VALUES (?,?,?,?,?,?)", (business_id, key, digest, json.dumps({"session_id": key, "transcript_hash": digest,
                               "agent_id": None, "run_id": "run-earlier", **state}), at(0), at(0)))


def business_learning(host):
    return {table: query(host, f"SELECT * FROM {table} WHERE business_id IS NOT NULL ORDER BY rowid") for table in LEARNING_TABLES}


def extract(host, **arguments):
    """One owner-started Global pass through the real job service; its frozen output is applied like any job's."""
    job = data(host, owner_global_context(host.instance), "learnings_extract", {"passes": 1, **arguments})["job_id"]
    outcome = host.background.run_one(job)
    assert query(host, "SELECT state FROM jobs WHERE id=?", job)[0]["state"] == "succeeded", outcome
    return json.loads(query(host, "SELECT result_json FROM jobs WHERE id=?", job)[0]["result_json"])["data"]


def keys(packets):
    return [packet["key"] for packet in packets]


@pytest.fixture
def stream(host):
    host.spies.client = Client()
    host.first, host.second = host.ids["first"], host.ids["second"]
    return host


def test_business_and_global_contributions_run_once_in_global_with_their_original_attribution(stream):
    host, first = stream, stream.first
    task = seed_task(host, first)
    note = seed_note(host, first, task, "Send invoices within two days.", 1)
    answer = seed_answer(host, first, 2, notes="Use the cheaper supplier for samples.")
    own = seed_note(host, None, seed_task(host, None, "Portfolio"), "Review the portfolio monthly.", 3)
    before = business_learning(host)
    extract(host)
    expected = {f"business:{first}:task_note:{note}", f"business:{first}:signoff_response:{answer}", f"task_note:{own}"}
    scan = {row["session_id"]: row for row in query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NULL")}
    assert set(scan) == expected
    by_kind = {(core["source"]["business_id"], core["source"]["kind"]): core for core in host.spies.client.cores}
    assert set(by_kind) == {(first, "task_note"), (first, "signoff_response"), (None, "task_note")}
    for core in host.spies.client.cores:
        source = core["source"]
        assert scan[sources.scan_key(source["business_id"], source["kind"], source["id"])]["transcript_hash"] == source["hash"]
        assert (source["actor_id"], source["actor_role"]) == (host.instance.owner, "owner")
        if source["business_id"]:
            assert core["context_only"]["business"]["slug"] == "first"
        else:
            assert "business" not in core["context_only"]
    # One consistent contribution prompt for every source: Global output scope with the optional original business.
    system = by_kind[first, "task_note"]["system"]
    assert {core["system"] for core in host.spies.client.cores} == {system} and sources.SOURCE_SYSTEM in system
    assert "EVERY item must\nuse scope=global" in system and "ORIGINAL business" in system and "never generalize" in system
    for obsolete in ("Global sources have no business", "choose biz or global", "Business context remains scope=biz",
                     "CURRENT WORKSPACE: business", "SOURCE BUSINESS"):
        assert obsolete not in system, obsolete
    assert by_kind[first, "task_note"]["source"]["resource_id"] == task                     # the raw source ids are kept
    # The new candidates are Global ones; each keeps its source's business, kind, ids, time and hash through review.
    candidates = {json.loads(row["candidate_json"])["evidence"]["source"]["id"]: row["id"] for row in query(
        host, "SELECT * FROM learning_candidates WHERE business_id IS NULL")}
    assert set(candidates) == {note, answer, own}
    bundle = data(host, owner_global_context(host.instance), "learnings_review_get", {"learning_id": candidates[note]})
    [frozen] = bundle["sources"]
    assert frozen["status"] == "available" and frozen["session"] == f"business:{first}:task_note:{note}"
    assert frozen["core"]["source"] == by_kind[first, "task_note"]["source"] and frozen["core"]["header"]["biz"] is None
    assert frozen["core"]["context"]["business"]["id"] == first
    assert business_learning(host) == before                                           # no business row written or copied
    # A repeated pass selects nothing again.
    host.spies.client.cores.clear()
    assert extract(host)["processed"] == 0 and host.spies.client.cores == []


def test_a_contribution_already_complete_in_its_business_is_skipped_and_a_changed_hash_enters_once(stream):
    host, first = stream, stream.first
    task = seed_task(host, first)
    done, changed = seed_note(host, first, task, "Quote prices in euros.", 1), seed_note(host, first, task, "Ship on Mondays.", 2)
    packets = {packet["source"]["id"]: packet for packet in selection(host)}
    # Both were processed in the business before; the other's hash there differs from its current one (thread messages
    # are immutable, so a changed contribution is one whose stored hash no longer matches).
    seed_scan(host, first, f"task_note:{done}", packets[done]["hash"], source_kind="task_note")
    seed_scan(host, first, f"task_note:{changed}", "0" * 64, source_kind="task_note")
    # Likewise a Global contribution whose Global cursor holds another hash.
    own = seed_note(host, None, seed_task(host, None), "Close the books on Fridays.", 3)
    seed_scan(host, None, f"task_note:{own}", "1" * 64, source_kind="task_note")
    before = business_learning(host)
    assert keys(selection(host, global_seen(host))) == [f"business:{first}:task_note:{changed}", f"task_note:{own}"]
    extract(host)
    scan = {row["session_id"]: row["transcript_hash"] for row in query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NULL")}
    assert scan == {f"business:{first}:task_note:{changed}": packets[changed]["hash"],          # no Global cursor for the
                    f"task_note:{own}": selection(host)[-1]["hash"]}                         # skipped one
    assert business_learning(host) == before
    assert selection(host, global_seen(host)) == []                             # it entered once
    # Parent Task edits and reminder state never replay a note.
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET title=?, status='working', snoozed_until=? WHERE business_id=? AND id=?",
                               ("Renamed", at(30), first, task))
    assert selection(host, global_seen(host)) == []


def test_retry_and_paused_states_keep_their_meaning_in_either_scope(stream):
    host, first = stream, stream.first
    task = seed_task(host, first)
    retried, paused, local = (seed_note(host, first, task, text, minute) for minute, text in
                              enumerate(("Retry me.", "Paused here.", "Paused in the business."), 1))
    packets = {packet["source"]["id"]: packet for packet in selection(host)}
    seed_scan(host, None, f"business:{first}:task_note:{retried}", packets[retried]["hash"], status="retry", attempts=1)
    seed_scan(host, None, f"business:{first}:task_note:{paused}", packets[paused]["hash"], status="needs_attention", attempts=3)
    seed_scan(host, first, f"task_note:{local}", packets[local]["hash"], status="needs_attention", attempts=3)
    assert [packet["source"]["id"] for packet in selection(host, global_seen(host))] == [retried]
    assert [packet["source"]["id"] for packet in selection(host, global_seen(host, True), retry_failed=True)] == [retried, paused, local]
    # The run applies the same rule from its own arguments.
    extract(host)
    assert {core["source"]["id"] for core in host.spies.client.cores} == {retried}
    host.spies.client.cores.clear()
    extract(host, retry_failed=True)
    assert {core["source"]["id"] for core in host.spies.client.cores} == {paused, local}


def test_equal_source_ids_in_two_businesses_stay_separate_and_archived_bare_and_legacy_sources_stay_out(stream):
    host, first, second = stream, stream.first, stream.second
    shared = new_id()
    seed_note(host, first, seed_task(host, first), "Prefer email.", 1, identity=shared)
    seed_note(host, second, seed_task(host, second), "Prefer calls.", 1, identity=shared)
    seed_answer(host, first, 2)                                                  # a bare respond with no notes
    seed_answer(host, first, 3, kind="approve")                                  # a bare approval
    seed_answer(host, first, 4, notes="Old v1 evidence.", legacy=True)
    selected = {packet["source"]["business_id"]: packet for packet in selection(host)}
    assert len(selection(host)) == 2 and {packet["key"] for packet in selected.values()} == {
        f"business:{first}:task_note:{shared}", f"business:{second}:task_note:{shared}"}
    assert selected[first]["hash"] != selected[second]["hash"]
    seed_scan(host, first, f"task_note:{shared}", selected[first]["hash"], source_kind="task_note")
    assert keys(selection(host)) == [f"business:{second}:task_note:{shared}"]  # the other business's progress is its own
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (at(50), second))
    assert selection(host) == []


def test_one_bound_covers_every_scope_in_contribution_time_order(stream):
    host, first, second = stream, stream.first, stream.second
    tasks = {scope: seed_task(host, scope) for scope in (None, first, second)}
    expected = []
    for minute in range(20):
        for scope in (second, None, first):                                     # inserted out of scope order on purpose
            identity = seed_note(host, scope, tasks[scope], f"Note {minute} in {scope}.", minute)
            expected.append((at(minute), scope or "", identity, sources.scan_key(scope, "task_note", identity)))
    order = [key for *_, key in sorted(expected)]
    assert keys(selection(host)) == order[:50]
    seen = {packet["key"]: packet["hash"] for packet in selection(host)}
    assert keys(selection(host, seen)) == order[50:]


# -- notes migrated from v1 (Manager decision 2422) --------------------------------------------------------------------

CAPTURE = "c" * 64                                                                # the sealed capture's database digest


def identity(*parts):
    """The deterministic note ID the v1.2 migration gave a v1 note, derived here independently of ``sources.migration_identity``."""
    return str(uuid5(NAMESPACE_URL, "qorqut-v1.2/migration/" + json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))))


def migrated(host, scope, task, table, body, minute, *, source_id=None, mapped_scope="same", attribution=None):
    """A v1 note exactly as the migration projected it: its deterministic ID and its migration attribution."""
    source_id = source_id or new_id()
    origin = {"source_table": table, "source_id": source_id, "source_sha256": CAPTURE}
    note = seed_note(host, scope, task, body, minute, attribution={"migration": origin} if attribution is None else attribution,
                     identity=identity(table, scope if mapped_scope == "same" else mapped_scope, source_id))
    return note, source_id


def v1_progress(host, scope, kind, source_id, text, minute, *, actor=None, **state):
    """HISTORICAL: v1's scan cursor for that note, under its original kind, ID and scope, with v1's contribution hash, and the
    run whose retained core v1 extracted it from."""
    actor = {"id": actor or host.instance.owner}
    blocks = [{"role": "owner", "text": text}]
    value = sources.digest(kind, source_id, scope, actor, at(minute), blocks)
    seed_scan(host, scope, f"{kind}:{source_id}", value, source_kind=kind, run_id="run-v1", **state)
    with host.instance.database.transaction() as uow:
        if not uow.connection.execute("SELECT 1 FROM learning_runs WHERE business_id IS ? AND id='run-v1'", (scope,)).fetchone():
            uow.connection.execute("INSERT INTO learning_runs(business_id,id,result_json,created_at) VALUES (?,?,?,?)", (scope, "run-v1",
                json.dumps({"id": "run-v1", "cores": {f"{kind}:{source_id}": {"source_key": f"{kind}:{source_id}",
                "source": {"kind": kind, "id": source_id, "business_id": scope, "hash": value}, "evidence_blocks": {"T1": blocks[0]}}}}), at(0)))
    return value


def preserved(host):
    """The notes and every learning row except Global scan state, which is the only thing a pass may add to."""
    return {"notes": query(host, "SELECT * FROM thread_messages ORDER BY business_id, id"),
            **{table: query(host, f"SELECT * FROM {table} ORDER BY rowid") for table in LEARNING_TABLES
               if table not in {"learning_scan_state", "learning_candidates", "learning_runs"}},
            "cursors": query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NOT NULL "
                                   "OR json_extract(state_json,'$.run_id') IN ('run-v1','run-earlier') ORDER BY rowid"),
            "runs": query(host, "SELECT * FROM learning_runs WHERE id='run-v1'")}


@pytest.mark.parametrize("scope", ["first", None])
def test_a_migrated_task_note_is_recognised_only_through_exact_provenance_and_its_v1_hash(stream, scope):
    host = stream
    scope = host.ids[scope] if scope else None
    task = seed_task(host, scope)
    same, same_id = migrated(host, scope, task, "task_notes", "Invoice on the first.", 1)
    v1_progress(host, scope, "task_note", same_id, "Invoice on the first.", 1)
    changed, changed_id = migrated(host, scope, task, "task_notes", "Invoice on the second.", 2)
    v1_progress(host, scope, "task_note", changed_id, "Invoice on the third.", 2)               # edited after v1 read it
    retrying, retrying_id = migrated(host, scope, task, "task_notes", "Retry this one.", 2)
    v1_progress(host, scope, "task_note", retrying_id, "Retry this one.", 2, status="retry")      # a retry is not complete
    # None of these is proof, though each has v1 progress that would match its recomputed hash.
    elsewhere, elsewhere_id = migrated(host, scope, task, "task_notes", "Wrong scope.", 3, mapped_scope=host.ids["second"])
    v1_progress(host, scope, "task_note", elsewhere_id, "Wrong scope.", 3)
    claimed = new_id()
    wrong_id, _ = migrated(host, scope, task, "task_notes", "Wrong ID.", 4,
                           attribution={"migration": {"source_table": "task_notes", "source_id": claimed, "source_sha256": CAPTURE}})
    v1_progress(host, scope, "task_note", claimed, "Wrong ID.", 4)
    author, author_id = migrated(host, scope, task, "task_notes", "Wrong author.", 5)
    v1_progress(host, scope, "task_note", author_id, "Wrong author.", 5, actor=new_id())
    malformed = []
    for minute, broken in enumerate((lambda o: {k: v for k, v in o.items() if k != "source_id"},        # no source ID
                                     lambda o: {**o, "source_sha256": "not-a-digest"},
                                     lambda o: {**o, "extra": 1},
                                     lambda o: {**o, "source_table": "stories"},                 # mapped, but not a note table
                                     lambda o: {**o, "source_table": ["task_notes"]},            # JSON values of the wrong type
                                     lambda o: {**o, "source_table": {"table": "task_notes"}},
                                     lambda o: {**o, "source_sha256": CAPTURE + "\n"}), 6):        # not exactly 64 hex characters
        source_id = new_id()
        origin = broken({"source_table": "task_notes", "source_id": source_id, "source_sha256": CAPTURE})
        malformed.append(seed_note(host, scope, task, f"Malformed {minute}.", minute, attribution={"migration": origin},
                                   identity=identity(origin.get("source_table", "task_notes"), scope, source_id)))
        v1_progress(host, scope, "task_note", source_id, f"Malformed {minute}.", minute)
    before = preserved(host)
    expected = [*sorted([changed, retrying]), elsewhere, wrong_id, author, *malformed]
    assert [packet["source"]["id"] for packet in selection(host, global_seen(host))] == expected
    extract(host)
    assert {core["source"]["id"] for core in host.spies.client.cores} == set(expected)
    assert selection(host, global_seen(host)) == []                        # each changed one entered once
    assert same not in {row["session_id"].rsplit(":", 1)[-1] for row in query(host, "SELECT session_id FROM learning_scan_state")}
    assert preserved(host) == before


@pytest.mark.parametrize("scope", ["first", None])
def test_a_migrated_story_note_with_unprovable_v1_progress_is_paused_never_replayed_and_others_continue(stream, scope):
    host = stream
    scope = host.ids[scope] if scope else None
    task = seed_task(host, scope)
    # v1 processed the story note body; the migration folded a two-line title in front of it.
    ambiguous, ambiguous_id = migrated(host, scope, task, "story_events", "Launch plan\nSecond line\nShip the beta to ten shops.", 1)
    v1_progress(host, scope, "story_note", ambiguous_id, "Ship the beta to ten shops.", 1)
    # An untitled story note projects to exactly the body v1 read: its v1 hash proves it.
    proven, proven_id = migrated(host, scope, task, "story_events", "Price per shop.", 2)
    v1_progress(host, scope, "story_note", proven_id, "Price per shop.", 2)
    fresh, _ = migrated(host, scope, task, "story_events", "Title\nNo v1 progress exists.", 3)
    ordinary = seed_note(host, scope, task, "A current note.", 4)
    before = preserved(host)
    result = extract(host)
    assert {core["source"]["id"] for core in host.spies.client.cores} == {fresh, ordinary}
    key = sources.scan_key(scope, "task_note", ambiguous)
    [paused] = query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NULL AND session_id=?", key)
    state = json.loads(paused["state_json"])
    assert (state["status"], state["last_error"]) == ("needs_attention", f"{sources.UNRESOLVED}: story_events {ambiguous_id}")
    assert [entry for entry in result["diagnostics"] if entry.get("stage") == "source_identity"] == [{
        "source": key, "stage": "source_identity", "reason": sources.UNRESOLVED, "business_id": scope, "source_table": "story_events",
        "source_id": ambiguous_id, "source_sha256": CAPTURE, "legacy_key": f"story_note:{ambiguous_id}"}]
    assert [source["session_id"] for source in result["paused_sources"]] == [key]
    assert not query(host, "SELECT 1 FROM learning_candidates WHERE json_extract(candidate_json,'$.evidence.source.id')=?", ambiguous)
    assert preserved(host) == before
    # Neither a later pass nor an explicit retry_failed sends it to the model, completes it or records it again.
    host.spies.client.cores.clear()
    for arguments in ({}, {"retry_failed": True}):
        result = extract(host, **arguments)
        assert host.spies.client.cores == [] and result["diagnostics"] == []
        assert query(host, "SELECT * FROM learning_scan_state WHERE business_id IS NULL AND session_id=?", key) == [paused]
    assert preserved(host) == before
