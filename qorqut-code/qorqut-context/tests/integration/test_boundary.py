"""The P5 boundary: Operations stands alone, base Context stands alone, the integration connects them, and Context's feature
declarations are exactly what a host composed before the move."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import textwrap

import pytest

from operational_support import BUSINESS, OperationalHost, WORKER
from qorqut_context.integration import queue as context_queue, registration
from qorqut_context.integration.service import ContextLocations, ContextServices, compose_context
from qorqut_operations.actions import settle
from qorqut_operations.actions.writeback import ActionWriteback
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.contracts import DomainError, InvocationContext, Principal

BLOCK = textwrap.dedent('''
    import importlib.abc, sys
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(name + " is blocked in this process")
    sys.meta_path.insert(0, Block())
''')


def isolated(blocked, body):
    """Run ``body`` in a fresh interpreter in which the ``blocked`` top-level packages cannot be imported."""
    code = f"BLOCKED = {set(blocked)!r}\n" + BLOCK + textwrap.dedent(body)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return result.stdout


# -- import independence --------------------------------------------------------------------------------------------------

def test_base_context_imports_without_operations_and_never_loads_the_integration():
    isolated({"qorqut_operations"}, """
        import importlib, pkgutil
        import qorqut_context
        for module in pkgutil.iter_modules(qorqut_context.__path__):
            if module.name not in ("__main__", "integration"):
                importlib.import_module("qorqut_context." + module.name)
        loaded = [name for name in sys.modules if name.split(".")[0] == "qorqut_operations" or name.startswith("qorqut_context.integration")]
        assert not loaded, loaded
    """)


def test_operations_imports_without_context_or_host_and_no_longer_has_the_four_context_packages():
    isolated({"qorqut_context", "bazarlyk"}, """
        import importlib, importlib.util, pkgutil
        import qorqut_operations
        for module in pkgutil.iter_modules(qorqut_operations.__path__):
            importlib.import_module("qorqut_operations." + module.name)
        for gone in ("context_queue", "context_filing", "context_graph_edits", "context_usage"):
            assert importlib.util.find_spec("qorqut_operations." + gone) is None, gone
        assert not [name for name in sys.modules if name.split(".")[0] in ("qorqut_context", "bazarlyk")]
    """)


def test_the_integration_package_is_light_and_its_modules_import_with_operations():
    isolated(set(), """
        import qorqut_context.integration
        assert not [name for name in sys.modules if name.startswith("qorqut_context.integration.") or name == "qorqut_operations"]
        import qorqut_context.integration.registration, qorqut_context.integration.service, qorqut_context.integration.driver
        import qorqut_context.integration.loop, qorqut_context.integration.recovery
        import qorqut_context.integration.queue, qorqut_context.integration.filing, qorqut_context.integration.graph_edits
        import qorqut_context.integration.reading, qorqut_context.integration.questions, qorqut_context.integration.usage
    """)


# -- the exact moved declarations -----------------------------------------------------------------------------------------

def digest(value):
    return hashlib.sha256(re.sub(r" at 0x[0-9a-f]+", "", repr(value)).encode()).hexdigest()


STORAGE = {
    "context-queue": ("dca59407d3abddb2f38b5a3e97d0ae41e290f88020dd0aa3782ca402839cbbd3", 19,
                      "5f092a2f7842af839a85b21059b5c59e3b2f129e010c132bec90306ec9534609",
                      "3ad80fbe2ea4c7c8cf396fc6d68ad32362d1355f24e1a5baae7648de0bc942a1",
                      ["context_input_attempts", "context_input_controls", "context_inputs"], ["action_revisions"]),
    "context-filing": ("79e3e4cbdaeef32641b25035c92c91ac98bfa25f676852c1d51840aa090d9e4f", 23,
                       "d9e675531ff2f24b658f5648a629a4aafd8bd5be9e03a21ee071ca26b4a247b6",
                       "5342055c7da0e6649be23b4f12131c06be8cc66d5f5780855f2cfae76a723bac",
                       ["context_completions", "context_filings", "context_source_bindings", "context_source_versions"], ["context_inputs"]),
    "context-graph-edits": ("e94cfcfbd435877f283f740207741331113427dbf7f5aa0d49cc5b2365154088", 8,
                            "0325f0b9be08cba0e010d5157e722dc0ee4e5f8dea77abcdb2c710a64803d4e6",
                            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                            ["context_graph_edits"], ["context_input_attempts", "context_inputs"]),
    "context-usage": ("97f96b7d938645734dba0233719d4144eb41f0aace11ec6b8c3a187136c3702b", 15,
                      "b8bef7257a57d86eb94c53fac0a41d61b1f11d3e12067590dfa3c5f180ddcc03",
                      "526c0b46d127d06db3ddf7e79f11f1b59046cbe77a56472adecce8a9520a0342",
                      ["context_usage_calls", "context_usage_sync"], ["context_input_attempts", "context_inputs"]),
}


def test_the_four_storage_contributions_keep_their_owner_labels_order_ddl_specs_and_immutable_fields():
    contributions = registration.storage_contributions()
    assert [item.owner for item in contributions] == list(STORAGE)
    for item in contributions:
        schema, length, tables, immutable, names, required = STORAGE[item.owner]
        assert (digest(item.schema), len(item.schema)) == (schema, length)
        assert digest(sorted((key, repr(value)) for key, value in item.tables.items())) == tables
        assert digest(sorted((key, sorted(value)) for key, value in item.immutable_fields.items())) == immutable
        assert sorted(item.tables) == names and sorted(item.required_tables) == required
        assert not item.validators and not item.required_lifecycles


def test_the_authority_entries_delegated_group_history_and_classifications_are_the_moved_ones():
    assert sorted(entry.name for entry in registration.CONTEXT_ENTRIES) == [
        "context_changes", "context_edit", "context_expand", "context_filing_receipt", "context_filings", "context_find",
        "context_home", "context_instruct", "context_object", "context_question_edit", "context_questions", "context_queue_discard",
        "context_queue_get", "context_queue_list", "context_queue_retry", "context_queue_submit", "context_retract", "context_retry",
        "context_upload", "context_upload_map", "journal_upload_to_filer", "notes_upload"]
    [group] = registration.CONTEXT_CREATION_GROUPS
    assert (group.operation_name, group.resource_types) == ("context_edit", frozenset({"context_graph_edits", "context_inputs"}))
    history = registration.FILING_HISTORY
    assert history.owner == "host.context_filing"
    assert [export.table for export in history.exports] == ["context_source_versions", "context_source_bindings", "context_filings",
                                                            "context_completions", "context_inputs"]
    assert [(item.table, item.kind) for item in (*registration.PRIVATE_CLASSIFICATIONS, *registration.SEPARATE_HISTORY_CLASSIFICATIONS)] == [
        ("context_graph_edits", "private_excluded"), ("context_input_attempts", "private_excluded"),
        ("context_input_controls", "private_excluded"), ("context_usage_calls", "separate_history"), ("context_usage_sync", "separate_history")]


# -- explicit host facts and inert construction ---------------------------------------------------------------------------

def test_the_composition_requires_its_host_facts_and_exposes_the_one_admission(tmp_path):
    host = OperationalHost(tmp_path / "host")
    places = ContextLocations(tmp_path / "graph", tmp_path / "runtime", tmp_path / "questions", tmp_path / "locks", tmp_path / "operations")
    roles = frozenset({"ordubashy"})
    with pytest.raises(ValueError):
        compose_context(host.app, places.repository, None, instruct_roles=roles, coordinator=lambda uow: None)
    with pytest.raises(ValueError):
        compose_context(host.app, places, None, instruct_roles=None, coordinator=lambda uow: None)
    with pytest.raises(ValueError):
        compose_context(host.app, places, None, instruct_roles=roles, coordinator=None)
    composed = compose_context(host.app, places, None, instruct_roles=roles, coordinator=lambda uow: None)
    assert composed.on_revision is context_queue.admit_action_revision and composed.locations is places
    assert not places.repository.exists() and not places.runtime.exists() and not places.questions.exists()    # inert construction
    callbacks = {"subscribe_commit": print, "unsubscribe_commit": print, "should_wake": print}
    for missing in callbacks:
        with pytest.raises(ValueError):
            ContextServices(composed, host.database, places, programs=None, reader=None, **{**callbacks, missing: None})


# -- one Action admission: the normal Action and a confirmed writeback ----------------------------------------------------

def written(host, source_key, text="Sent the launch email"):
    """One applying unit of work as a confirmed provider receipt does: append, apply, settle with the Actions' own admission."""
    ctx = InvocationContext(Principal(WORKER, "system"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}), source="worker")
    with host.database.transaction() as uow:
        builder = PlanBuilder(ctx, "actions_record", {}, "Confirmed result", clock=host.clock)
        ActionWriteback(clock=host.clock).append_to_plan(builder, uow, text=text, tag="operations", source_key=source_key,
                                                         operation_name="actions_record")
        plan = builder.build()
        apply_records(ctx, plan, uow)
        return settle(ctx, plan, uow, on_revision=host.actions.on_revision)


def rows(host):
    return {table: host.count(table) for table in ("action_logs", "action_revisions", "action_businesses", "context_inputs", "item_tags",
                                                    "thread_follows", "inbox_events")}


def test_a_confirmed_writeback_settles_through_the_actions_own_admission_and_a_failure_leaves_nothing(tmp_path, monkeypatch):
    host = OperationalHost(tmp_path)
    assert host.actions.on_revision is context_queue.admit_action_revision
    empty = rows(host)

    def refuse(uow, **arguments):
        raise DomainError("operation_failed", "the queue refused the input")

    monkeypatch.setattr("qorqut_context.integration.queue.queue.enqueue_action", refuse)
    with pytest.raises(DomainError):
        written(host, "signoff:synthetic:email")
    assert rows(host) == empty                       # the Action, its revision, tags and the input roll back together
    monkeypatch.undo()

    [first] = written(host, "signoff:synthetic:email")
    after = rows(host)
    assert after["action_logs"] == after["action_revisions"] == after["context_inputs"] == 1
    assert host.query("SELECT input_kind, operation, state, item_id FROM context_inputs") == [
        {"input_kind": "action", "operation": "add", "state": "queued", "item_id": first["action_key"]}]
    assert written(host, "signoff:synthetic:email", "Different wording on the retry") == []      # the confirmed retry plans and admits nothing
    assert rows(host) == after


# -- the lifecycle step order ---------------------------------------------------------------------------------------------

class Recorder:
    """Stands in for the driver, the usage collector and the question outbox, recording the order of the service's calls."""

    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def collect_next(self):
        self.log.append("collect")

    def recover(self):
        self.log.append("recover")

    def process_next(self):
        self.log.append("process_next")
        if self.fail:
            raise RuntimeError("the driver failed")
        return {"status": "idle"}

    def drain(self):
        self.log.append("drain")


def stepped(gate, fail=False):
    log = []
    services = ContextServices(None, None, None, programs=None, reader=None, subscribe_commit=print, unsubscribe_commit=print,
                               should_wake=print)
    services._driver = services._collector = services._outbox = Recorder(log, fail)
    services._ready = lambda: gate
    return services, log


def test_the_service_step_collects_gates_recovers_once_processes_then_drains_questions_and_collects():
    services, log = stepped(None)
    assert services.step() == {"status": "idle"}
    assert log == ["collect", "recover", "process_next", "drain", "collect"]          # the deferred recovery runs once, first
    log.clear()
    services.step()
    assert log == ["collect", "process_next", "drain", "collect"]


def test_an_unready_context_collects_only_and_never_recovers_processes_or_drains():
    services, log = stepped({"status": "unready"})
    assert services.step() == {"status": "unready"} and log == ["collect", "collect"]


def test_the_question_drain_still_runs_when_the_driver_step_raises():
    services, log = stepped(None, fail=True)
    with pytest.raises(RuntimeError):
        services.step()
    assert log == ["collect", "recover", "process_next", "drain"]
