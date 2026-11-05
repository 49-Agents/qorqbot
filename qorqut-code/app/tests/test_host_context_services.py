"""The stock host's Context composition and its one lifecycle owner (``runtime.context_services``), on a synthetic host.

Real: the stock host assembly (``host_assembly``) and ``build_runtime`` with the host's own domain factory (no wrapper, no storage
added by the test), the Context integration's queue/filing/graph-edit/usage storage the stock assembly carries, the single queue shared by the
filing ports and the domains, the Context integration's ``ContextQueueDriver`` and bounded ``ContextUsageCollector`` built by
``ContextServices``, its change-feed wake, the real ``Context`` (real Git) and the library's own ``Pipeline``. Faked: only the model
runner injected through ``ContextSettings`` (it records every call; no provider or model is ever started), the injected clock and
the synthetic principals, files and coordinator row under a pytest ``tmp_path``. These checks run the source trees on
``PYTHONPATH``: they are not installed-application proof and they start no user instance."""

import threading
import time

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_context import Usage
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.contracts import DomainError, InvocationOptions

from qorqut_context.integration.service import ContextSettings, driver_principal_id
from bazarlyk.shared_context.service import ContextServices, runner_config_from_file
from context_completion_support import Runner, build
from context_edit_support import build as build_edit

CONTEXT_TABLES = {"context_inputs", "context_input_attempts", "context_input_controls", "context_completions", "context_filings",
                  "context_source_bindings", "context_source_versions", "context_graph_edits", "context_usage_calls",
                  "context_usage_sync"}


class UsageRunner(Runner):
    """The deterministic runner that also reports observed native usage, as the normal adapter does through ``on_usage``."""

    def run(self, **args):
        index = len(self.calls) + 1
        usage = Usage(input_tokens=10 * index, cached_input_tokens=4, output_tokens=2, reasoning_output_tokens=1,
                      total_tokens=10 * index + 2, thread_id="script", turn_id=str(index), source="synthetic")
        identity, report = args.get("on_identity"), args.get("on_usage")

        def after_identity(value):
            identity(value)
            if report:
                report(usage)

        result = super().run(**{**args, "on_identity": after_identity})
        return RunResult(result.status, result.output, result.thread_id, result.turn_id, result.model, usage)


def upload(host, body="Met Alice about the pilot.", **arguments):
    data = host.upload(host.agent(), body, **arguments)
    return data["document"], data["job"]["id"]


def settle(host, job_id=None):
    host.clock.advance(seconds=16)
    return host.run_job(job_id)


def installation_id(host):
    return host.query("SELECT installation_id FROM installation")[0]["installation_id"]


def driver_row(host):
    rows = host.query("SELECT id, kind, active, display_name FROM principals WHERE id=?", driver_principal_id(installation_id(host)))
    return rows[0] if rows else None


def wait_for(condition, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


# -- the stock assembly and its one queue --------------------------------------------------------------------------------------

def test_the_stock_assembly_carries_the_complete_context_storage_and_one_actual_queue(tmp_path):
    host = build(tmp_path, build_service=False)
    tables = {row["name"] for row in host.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert CONTEXT_TABLES <= tables                                                  # queue, filing, graph-edit and usage storage
    composition = host.runtime.context_services.composition
    # One of each, shared: the domain factory, the filing ports and the lifecycle owner hold the very same objects.
    assert host.runtime.domains.queue is composition.queue and composition.filing.queue is composition.queue
    assert composition.queue.outcomes is composition.outcomes
    assert composition.graph_edits.authority is not None
    assert [name for name in ("context_upload", "context_edit", "context_queue_submit", "context_filing_costs")
            if not host.runtime.registry.known(name)] == []
    # The same registered operations answer on the stock host: an authorized upload is admitted through the actual queue.
    document, job_id = upload(host)
    assert host.query("SELECT state, input_kind FROM context_inputs") == [{"state": "queued", "input_kind": "general"}]
    assert host.job(job_id)["state"] == "queued" and document["business_id"] == host.ids["first"]


# -- inert construction and reads ----------------------------------------------------------------------------------------------

def test_construction_and_reads_create_no_context_principal_thread_or_provider_run(tmp_path):
    host = build(tmp_path, initialize=False, build_service=False)
    services, paths = host.runtime.context_services, host.paths
    before = {thread.name for thread in threading.enumerate()}
    assert not paths.context.exists() and not paths.context_runtime.exists()       # building the runtime made nothing
    reader = host.agent()                                                          # a registered business agent, made before the baseline
    principals = host.count("principals")
    assert driver_row(host) is None
    status = host.runtime.domains.shared_context.status()
    assert status["status"] == "not_initialized" and status["reason"]["code"] == "repository_missing"
    assert services.diagnostics()["state"] == "not_started" and not services.running and services.driver is None
    assert services.stop(timeout=0.1) == {"stopped": True, "in_flight": False}     # stopping what never started is a no-op
    for read, arguments in (("context_home", {}), ("context_find", {"query": "x"})):  # the shared reads report, never create
        result = host.runtime.application.invoke(reader, read, arguments, InvocationOptions())
        assert result.status == "ok" and (result.data or {}).get("status") == "not_initialized", result.data
    assert not paths.context.exists() and not paths.context_runtime.exists()
    assert host.runner.calls == [] and {thread.name for thread in threading.enumerate()} == before
    assert driver_row(host) is None and host.count("principals") == principals


def test_composition_validates_the_injected_runner_and_the_library_configuration(tmp_path):
    with pytest.raises(DomainError) as invalid:
        ContextSettings(runner=object()).resolve()
    assert invalid.value.code == "invalid_configuration"
    with pytest.raises(DomainError):
        ContextSettings(runner=Runner(), config=RunnerConfig(source_bytes=1234)).resolve()      # runner and config disagree
    config = tmp_path / "runner.json"
    config.write_text('{"model": "synthetic-model", "effort": "high", "source_bytes": 4096}')
    loaded = runner_config_from_file(config)
    assert (loaded.model, loaded.effort, loaded.source_bytes) == ("synthetic-model", "high", 4096)
    for text in ('{"unknown": 1}', '{"service_tier": "fast"}', "[]", "{"):
        config.write_text(text)
        with pytest.raises(DomainError):
            runner_config_from_file(config)
    with pytest.raises(DomainError):
        runner_config_from_file("relative.json")


# -- the dedicated driver principal ---------------------------------------------------------------------------------------------

def test_owned_context_start_repairs_missing_filing_workers_and_publishes_instruction(tmp_path):
    host = build(tmp_path, build_service=False, register_workers=False, settings={"poll_seconds": 30.0})
    with host.database.read() as uow:
        instruction = host.runtime.authority.worker_id("context.instruct", uow)
        unrelated = host.runtime.authority.worker_id("test.unrelated", uow)
    assert host.error(host.human(None), "context_instruct", {"text": "Alice leads the pilot."}) == "invalid_principal"
    assert host.services.start()["status"] == "started"
    assert host.services.stop(timeout=10)["stopped"]
    assert host.count("principals", "id=?", unrelated) == 0
    for scope in (None, "first"):
        data = host.data(host.human(scope), "context_instruct", {"text": "Alice leads the pilot."})
        assert host.services.step()["status"] == "published"
        assert settle(host, data["job"]["id"])["state"] == "succeeded"
    # Uploads need the other filing worker, including its per-business membership.
    document, job_id = upload(host)
    assert host.services.step()["status"] == "published"
    assert settle(host, job_id)["state"] == "succeeded"
    before = host.query("SELECT * FROM principals WHERE id=?", instruction)
    host.restart_driver()
    assert host.query("SELECT * FROM principals WHERE id=?", instruction) == before
    assert host.count("business_memberships", "principal_id=?", instruction) == len(host.ids)


@pytest.mark.parametrize("kind, active", [("agent", 1), ("system", 0)])
def test_filing_registration_does_not_replace_or_reactivate_worker(tmp_path, kind, active):
    host = build(tmp_path, build_service=False, register_workers=False)
    with host.database.transaction() as uow:
        worker = host.runtime.authority.worker_id("context.instruct", uow)
        uow.repository("principals").insert({"id": worker, "kind": kind, "active": active})
    before = host.query("SELECT * FROM principals WHERE id=?", worker)
    assert host.services.start()["status"] == "blocked"
    assert host.query("SELECT * FROM principals WHERE id=?", worker) == before
    assert not host.services.running


def test_filing_registration_preserves_disabled_membership(tmp_path):
    host = build(tmp_path, build_service=False)
    with host.database.transaction() as uow:
        worker = host.runtime.authority.worker_id("context.instruct", uow)
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?",
                               (worker, host.ids["first"]))
    host.services.build()
    assert host.error(host.human("first"), "context_instruct", {"text": "Alice leads the pilot."}) == "forbidden"
    assert host.data(host.human(None), "context_instruct", {"text": "Alice leads the pilot."})["filing"]

def test_start_registers_the_dedicated_driver_principal_once_and_stop_reports_drained(tmp_path):
    host = build(tmp_path, build_service=False, settings={"poll_seconds": 30.0})
    services = host.runtime.context_services
    identity = driver_principal_id(installation_id(host))
    assert driver_row(host) is None
    started = services.start()
    try:
        assert started["status"] == "started" and services.running
        assert driver_row(host) == {"id": identity, "kind": "system", "active": 1, "display_name": "Context queue driver"}
        assert services.start() == {"status": "running"}                            # idempotent while running
        diagnostics = services.diagnostics()
        assert diagnostics["state"] == "running" and diagnostics["driver_id"] == identity
        assert diagnostics["context"]["status"] == "ready" and diagnostics["blocked"] is None
    finally:
        stopped = services.stop(timeout=10)
    assert stopped == {"stopped": True, "in_flight": False} and not services.running
    assert services.diagnostics()["state"] == "stopped"
    assert services.start()["status"] == "started"                                  # a later start reuses the same identity
    assert services.stop(timeout=10) == {"stopped": True, "in_flight": False}
    assert host.query("SELECT COUNT(*) AS n FROM principals WHERE id=?", identity) == [{"n": 1}]
    assert host.runner.calls == []                                                  # no input: no model


@pytest.mark.parametrize("kind, active", [("agent", 1), ("system", 0)])
def test_an_occupied_or_disabled_driver_identity_blocks_context_but_not_ordinary_work(tmp_path, kind, active):
    host = build(tmp_path, build_service=False)
    services = host.runtime.context_services
    identity = driver_principal_id(installation_id(host))
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": "Occupied"})
        if not active:
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (identity,))
    before = driver_row(host)
    blocked = services.start()
    assert blocked["status"] == "blocked" and blocked["code"] == "invalid_principal" and not services.running
    with pytest.raises(DomainError) as refused:
        services.build()
    assert refused.value.code == "invalid_principal"
    assert driver_row(host) == before                                                # never repurposed, never re-enabled
    assert services.diagnostics()["state"] == "blocked" and services.diagnostics()["blocked"]["code"] == "invalid_principal"
    # Ordinary operational work is unaffected: an upload is admitted and stays queued with its job pending.
    document, job_id = upload(host)
    assert host.query("SELECT state FROM context_inputs") == [{"state": "queued"}]
    assert host.job(job_id)["state"] == "queued" and host.runner.calls == []
    assert services.stop(timeout=1) == {"stopped": True, "in_flight": False}


# -- unavailable Context and coordinator ---------------------------------------------------------------------------------------

def test_a_missing_context_is_never_initialized_and_inputs_wait_until_it_is_made_explicitly(tmp_path):
    from qorqut_context import Context

    host = build(tmp_path, initialize=False)
    services = host.runtime.context_services
    document, job_id = upload(host)                                                  # ordinary work continues without a Context
    assert host.query("SELECT state FROM context_inputs") == [{"state": "queued"}]
    for _ in range(2):
        gate = services.step()
        assert gate["status"] == "not_ready" and gate["reason"] == "context_not_initialized"
    assert services.recover()["status"] == "not_ready"
    assert not host.paths.context.exists() and host.runner.calls == []              # nothing was created, no model started
    assert host.query("SELECT state, attempt FROM context_inputs") == [{"state": "queued", "attempt": 0}]
    assert host.count("context_input_attempts") == 0 and host.count("context_completions") == 0
    assert services.diagnostics()["gate"]["status"] == "context_not_initialized"
    # Ordinary operations (here a second upload and the job read) keep working while Context inputs stay pending.
    second, _ = upload(host, "Second note.")
    assert host.count("context_inputs") == 2 and host.job(job_id)["state"] == "queued"
    Context.initialize(host.paths.context, host.paths.context_runtime)               # the explicit operator step
    assert services.step()["status"] == "published" and services.step()["status"] == "published"
    assert host.runner.propagations == 2
    assert settle(host, job_id) == {"job_id": job_id, "state": "succeeded"}
    assert document["id"] != second["id"]


def test_a_missing_coordinator_blocks_context_progress_but_not_ordinary_operations(tmp_path):
    host = build(tmp_path)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    document, job_id = upload(host)
    first = host.services.step()
    assert first == {"status": "not_ready", "reason": "no_coordinator", "input_id": host.query("SELECT id FROM context_inputs")[0]["id"]}
    assert host.runner.calls == [] and host.count("context_input_attempts") == 0     # no attempt started, no model run
    other, _ = upload(host, "Another record while Context waits.")                   # ordinary operations still work
    assert host.count("context_inputs") == 2 and host.query("SELECT state FROM context_inputs") == [{"state": "queued"}] * 2
    with host.database.transaction() as uow:                                         # the registered coordinator is restored
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (host.coordinator,))
    assert host.services.step()["status"] == "published" and host.runner.propagations == 1
    assert document["id"] != other["id"]


# -- queue order, publication and the exact receipt through the service body -----------------------------------------------------

def test_an_upload_and_an_action_revision_reach_one_ordered_queue_and_publish_with_exact_receipts(tmp_path):
    host = build(tmp_path)
    agent = host.agent()
    document, job_id = upload(host, title="Pilot call")
    # The global agent records its business work as a Global business-operational Action.
    recorded = host.data(agent, "actions_record", {"text": "Published the launch post", "tags": ["Launch"],
                                                   "category": "business-operational", "business_ids": [host.ids["first"]]})
    key = recorded["action"]["action_id"]
    queued = host.query("SELECT sequence, input_kind, operation, item_id, state FROM context_inputs ORDER BY sequence")
    assert [(row["input_kind"], row["operation"], row["state"]) for row in queued] == [("general", "add", "queued"),
                                                                                     ("action", "add", "queued")]
    assert queued[1]["item_id"] == key and queued[0]["sequence"] < queued[1]["sequence"]
    first, second = host.services.step(), host.services.step()                     # strictly in queue order
    assert [first["status"], second["status"]] == ["published", "published"]
    assert first["input_id"] == host.query("SELECT id FROM context_inputs ORDER BY sequence")[0]["id"]
    assert host.runner.propagations == 2 and host.services.step() == {"status": "idle"}
    states = host.query("SELECT input_kind, state, context_event_id FROM context_inputs ORDER BY sequence")
    assert [row["state"] for row in states] == ["published", "published"] and states[0]["context_event_id"] != states[1]["context_event_id"]
    assert settle(host, job_id) == {"job_id": job_id, "state": "succeeded"}          # the original job gets the exact receipt
    result = host.job(job_id)["result_json"]["data"]
    assert result["context_event_id"] == first["context_event_id"] and result["published_commit"] == first["published_commit"]
    assert result["document_id"] == document["id"] and host.count("context_completions", "state='applied'") == 1


def test_the_change_feed_wakes_the_running_service_without_waiting_for_the_poll_interval(tmp_path):
    host = build(tmp_path, build_service=False, settings={"poll_seconds": 30.0, "max_backoff_seconds": 60.0})
    services = host.runtime.context_services
    document, job_id = upload(host)                                                  # queued before the service starts
    assert services.start()["status"] == "started"
    try:
        # The start step publishes the queued input; the long poll interval means only a wake can reach the next one.
        assert wait_for(lambda: host.count("context_inputs", "state='published'") == 1)
        before = host.runner.propagations
        second, _ = upload(host, "A later note admitted while the loop sleeps.")
        assert wait_for(lambda: host.count("context_inputs", "state='published'") == 2, seconds=20)
        assert host.runner.propagations == before + 1 and second["id"] != document["id"]
    finally:
        assert services.stop(timeout=10) == {"stopped": True, "in_flight": False}
    assert host.runtime.changes._listeners == []                                     # the wake hook is removed with the service


# -- usage collection ---------------------------------------------------------------------------------------------------------

def test_the_service_collects_observed_usage_and_missing_evidence_is_never_invented(tmp_path):
    host = build(tmp_path, runner=UsageRunner())
    upload(host)
    assert host.services.step()["status"] == "published"                             # the collector ran after the driver step
    calls = host.query("SELECT stage, state, input_id, context_event_id FROM context_usage_calls ORDER BY started_at")
    assert calls and {row["context_event_id"] for row in calls} == {host.query("SELECT context_event_id FROM context_inputs")[0]["context_event_id"]}
    assert host.services.diagnostics()["collection"]["failures"] == 0
    answer = host.read(host.human(None), "context_filing_costs", {})
    assert answer["aggregates"]["calls"] == len(calls) and answer["coverage"]["ready_events"] == 1


def test_an_event_with_no_receipt_stays_missing_and_a_collection_failure_is_evidence_not_a_zero(tmp_path, monkeypatch):
    host = build(tmp_path)                                                           # this runner reports no usage at all
    upload(host)
    assert host.services.step()["status"] == "published" and host.runner.propagations == 1
    (call,) = host.query("SELECT input_tokens, cached_input_tokens, output_tokens, total_tokens, cost_usd, usage_source "
                         "FROM context_usage_calls")
    assert call == {key: None for key in call}                                       # observed nothing: null, never a fabricated zero
    answer = host.read(host.human(None), "context_filing_costs", {})
    assert len(answer["calls"]) == 1 and answer["aggregates"]["calls_without_token_usage"] == 1
    # A failing receipt read is recorded as evidence by the bounded collector; it never reruns the model or invents usage.
    def unreadable(*args, **kwargs):
        raise RuntimeError("unreadable receipts")

    monkeypatch.setattr(host.pipeline, "usage_receipts", unreadable)
    upload(host, "Next note.")
    assert host.services.step()["status"] == "published" and host.runner.propagations == 2
    for _ in range(3):
        host.services.step()
    collector = host.services.diagnostics()["collection"]["collector"]
    assert collector["last_error"]["code"] == "read_failed" and collector["steps"]["unavailable"] >= 1
    assert host.runner.propagations == 2 and host.count("context_usage_calls") == 1       # the earlier row stays; nothing was added
    assert host.count("context_usage_sync", "status='unavailable'") >= 1


# -- the engine root is not an authored tree ---------------------------------------------------------------------------------

def git_status(root):
    import subprocess

    return subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall"], capture_output=True, text=True, check=True).stdout


def test_a_global_upload_publishes_without_dirtying_the_graph_checkout_and_stays_scoped(tmp_path):
    host = build(tmp_path)
    owner = host.human(None)
    data = host.upload(owner, "Global strategy meeting.", title="Strategy")
    document, job_id = data["document"], data["job"]["id"]
    path = host.query("SELECT path FROM documents WHERE id=?", document["id"])[0]["path"]
    assert path.startswith("context/_inbox/") and document["business_id"] is None
    authored = host.paths.operations / path                                           # the Global authored tree, beside the graph
    assert authored.is_file() and host.paths.context == host.paths.operations / "context-graph"
    assert not authored.is_relative_to(host.paths.context) and git_status(host.paths.context) == ""
    # A business agent has no Global source access: scoped reads are unchanged by the path separation.
    refused = host.call(host.agent(), "context_get", {"document_id": document["id"]})
    assert refused.status == "error" and refused.error["code"] in {"not_found", "forbidden"}
    published = host.services.step()
    assert published["status"] == "published" and host.runner.propagations == 1
    assert git_status(host.paths.context) == "" and authored.is_file()               # publication used the Context's own Git only
    assert settle(host, job_id) == {"job_id": job_id, "state": "succeeded"}
    assert host.job(job_id)["result_json"]["data"]["published_commit"] == published["published_commit"]


def test_managed_documents_cannot_target_the_graph_root_and_discovery_never_offers_it(tmp_path):
    from qorqut_operations.application.plans import PlanBuilder
    from qorqut_operations.contracts import new_id

    from bazarlyk.composition.locations import document_path_at_global, document_root

    host = build(tmp_path, build_service=False)
    paths, store = host.paths, host.runtime.domains.documents.store
    for relative in ("context-graph", "context-graph/graph/config.json", "context-graph/_inbox/x.md"):
        with pytest.raises(DomainError) as refused:
            document_root(paths, None, relative)
        assert refused.value.code == "forbidden"
        with pytest.raises(DomainError) as resolved:                                   # the registered-file resolution of a plan
            store.resolve_file_target(store.file_ref(new_id(), relative, None))
        assert resolved.value.code == "forbidden"
        assert document_path_at_global(paths, relative) is None                       # inverse discovery
    with host.database.read() as uow, pytest.raises(DomainError) as prepared:          # and the write path refuses at preparation
        builder = PlanBuilder(host.human(None), "context_notes_create", {}, "Note", clock=host.clock)
        store._file(builder, uow, new_id(), "context-graph/graph/config.json", None, None, b"overwrite")
    assert prepared.value.code == "forbidden"
    # The Global authored tree and business HQ resolution are unchanged.
    assert document_root(paths, None, "context/_inbox/x.md") == paths.operations
    assert document_path_at_global(paths, "context/_inbox/x.md") == "context/_inbox/x.md"
    assert document_root(paths, {"slug": "first"}, "context-graph/x.md") == paths.business_hq("first")
    assert git_status(paths.context) == "" if paths.context.exists() else True


# -- retargeted callers -------------------------------------------------------------------------------------------------------

def test_confirmed_result_writeback_records_the_actual_outcome_as_one_action_and_writes_no_graph(tmp_path):
    from qorqut_operations.actions import settle
    from qorqut_operations.application.plans import PlanBuilder, apply_records

    host = build(tmp_path, initialize=False, build_service=False)
    agent, domains = host.agent(), host.runtime.domains
    refs = {"kind": "email", "channel": "email", "target": "customer@example.test", "signoff": "synthetic"}
    with host.database.transaction() as uow:
        builder = PlanBuilder(agent, "signoffs_platform_record_result", {}, "Record the delivery", clock=domains.activity.clock)
        domains.activity.append_to_plan(builder, uow, text="Email sent (provider confirmed): Launch day", tag="operations",
            refs=refs, operation_name="actions_record", source_key="signoff:synthetic:email")
        plan = builder.build()
        apply_records(agent, plan, uow)
        settle(agent, plan, uow, on_revision=domains.actions.on_revision)
    key = host.query("SELECT action_key FROM action_logs")[0]["action_key"]
    assert host.query("SELECT input_kind, operation, state, item_id FROM context_inputs") == [
        {"input_kind": "action", "operation": "add", "state": "queued", "item_id": key}]   # the accepted Action path, once
    assert host.count("jobs") == 0 and host.runner.calls == []                       # no second external operation, no model
    assert not host.paths.context.exists()                                           # and no direct graph write or initialization
    composed = host.runtime.context_services.composition                             # the one Action writer holds the composition's admission
    assert domains.actions.on_revision is composed.on_revision


def test_the_old_engine_is_not_assembled_and_the_old_package_is_not_a_dependency(tmp_path):
    import importlib.util
    import re
    from pathlib import Path

    host = build(tmp_path, build_service=False)
    runtime = host.runtime
    assert not hasattr(runtime, "context_base") and not hasattr(runtime.domains, "context_base")
    assert importlib.util.find_spec("bazarlyk.domains.contextbase") is None
    import tomllib

    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]
    declared = [*project["dependencies"], *(item for extra in project["optional-dependencies"].values() for item in extra)]
    assert not any(item.startswith("qorqut-contextbase") or "qorqut-context.git" in item for item in declared)
    assert "qorqut-context[integration]==0.1.0" in project["dependencies"]
    assert "contextbase" not in project["optional-dependencies"]
    assert "learning-engine" not in project["optional-dependencies"]
    assert not any("qorqut-learning" in item for item in declared)
    assert not any("@" in item or "git+" in item for item in declared)         # first-party packages are versioned names, never VCS URLs
