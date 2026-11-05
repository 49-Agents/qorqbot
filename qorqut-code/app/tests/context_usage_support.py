"""Synthetic fixtures for the host Context usage checks (collector, owner endpoint, Global cost overview).

Everything lives under a pytest ``tmp_path``: a host runtime composed with the Context integration's usage-ledger tables, a disposable
Context library store (real ``qorqut_context`` Git store, real ``Pipeline``) driven by a scripted injected runner, and queue
inputs advanced through the real Context integration queue transitions. No model, provider, network, native session, role, credential or
operational data is used; the library is only ever called through its read-only ``usage_receipts`` by the collector under test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qorqut_context import Context, Pipeline, PropagationError, RunResult, Usage
from qorqut_context.runner import RunnerConfig
from qorqut_context.integration import usage as context_usage
from qorqut_context.integration.queue import transitions
from qorqut_operations.contracts import InvocationContext, Principal

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from host_foundation_support import SUPPORTED_GLOBAL_ROLES, build_instance, create_registered_global, role_root


SYSTEM = "00000000-0000-4000-8000-0000000000aa"
CONTENT = "Jesse appears in this note."


def require_synthetic_sources() -> None:
    """The read-only sources every B3 acceptance case needs; a missing one is an actionable failure, never a skip."""
    roles = role_root() / "roles/imported-roles/native/roles"
    missing = [role for role in SUPPORTED_GLOBAL_ROLES if not (roles / role / "role.yaml").is_file()]
    if missing:
        pytest.fail(f"The B3 acceptance cases need the supported global role sources {missing} at {roles}; set "
                    "QORQUT_TEST_WORKSPACE_ROOT to a workspace containing agentic defaults", pytrace=False)


class ProcessStopped(BaseException):
    """A crash of the whole process in the middle of a model call (not an ordinary error)."""


def call(tools, name, **arguments):
    result = tools.dispatch(name, arguments)
    if not result["ok"]:
        raise AssertionError(result)
    return result["result"]


def finish(tools):
    call(tools, "stage_handoff_note", text="")
    return call(tools, "finish_propagation", status="complete", summary="Processed the supplied source.",
                reassessed=tools.required_reassessment)


def new_node(tools, summary="A new person"):
    return call(tools, "put_node", record={"summary": summary, "attributes": {"basic_entity_type": ["person"], "name": [summary]}},
                explanation="Supported by this source.", citations=["self"], support_after=["self"])["id"]


def snap(index, input_tokens=10, **changes):
    """A cumulative native snapshot attributed to the ScriptRunner's call number ``index``."""
    return Usage(**{"input_tokens": input_tokens, "cached_input_tokens": 4, "output_tokens": 2, "reasoning_output_tokens": 1,
                    "total_tokens": input_tokens + 2, "thread_id": "script", "turn_id": str(index), "source": "synthetic",
                    **changes})


def propagate(tools, payload):
    new_node(tools)
    finish(tools)


def report(stage, index, emit, **changes):
    final = snap(index, 10 * index, **changes)
    emit(final)
    return final


class ScriptRunner:
    """Injected runner honoring the library's explicit ``Runner`` protocol (a copy of the library's own test double)."""

    config = RunnerConfig()

    def __init__(self, action=propagate, usage=report):
        self.action, self.calls, self.usage = action, [], usage

    def run(self, *, stage, instructions, payload, directory, tools=None, output_schema=None, on_identity=None, on_usage=None):
        self.calls.append(stage)
        if on_identity:
            on_identity({"stage": stage, "thread_id": "script", "turn_id": str(len(self.calls))})
        final = self.usage(stage, len(self.calls), on_usage) if self.usage else None
        if stage == "bootstrap":
            output = json.dumps({"node_ids": [value["record"]["id"] for value in payload["candidates"]]})
        else:
            self.action(tools, payload)
            output = "Finished."
        return RunResult("completed", output, "script", str(len(self.calls)), self.config.model, final)


class World:
    """One synthetic host runtime with the usage ledger, a library store, a pipeline and queue helpers."""

    def __init__(self, tmp_path):
        require_synthetic_sources()
        self.tmp = Path(tmp_path)
        self.instance, self.ids = build_instance(self.tmp, businesses=("first",))
        self.database = self.instance.database
        self.owner = self.instance.owner
        with self.database.transaction() as uow:
            uow.repository("principals").insert({"id": SYSTEM, "kind": "system", "display_name": "Context worker"})
        self.context = Context.initialize(self.tmp / "library" / "business", basic_entity_types=["person"])
        seed = self.context.begin("add", "foundation/seed.md", content=b"Jesse is a person.", submitted_by="test-owner",
                                  metadata={"kind": "note", "title": "seed", "origin": "test", "created_at": "2026"})
        if not seed.published_commit:
            self.context.stage_handoff_note(seed.event_id, "")
        self.context.put_node(seed.event_id, {"id": "jesse", "summary": "Jesse",
                                              "attributes": {"basic_entity_type": ["person"], "name": ["Jesse"]}},
                              explanation="The source supplies this context.", citations=["self"], support_after=["self"])
        self.context.publish(seed.event_id)         # a node the notes mention, so bootstrap really calls the model
        self.sequence = 0
        self._coordinator = None
        self.runner = ScriptRunner()
        self.pipeline = Pipeline(self.context, runner=self.runner)
        self.lock = self.tmp / "private" / "context-usage.lock"

    # -- the host ------------------------------------------------------------------------------------------------------------

    def collector(self, reader=None, **options):
        from qorqut_context.integration.usage import ContextUsageCollector

        options.setdefault("driver_id", SYSTEM)
        options.setdefault("lock_path", self.lock)
        return ContextUsageCollector(self.database, reader if reader is not None else self.pipeline, **options)

    def owner_global(self):
        return InvocationContext(Principal(self.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")

    def invoke(self, name, arguments=None, ctx=None):
        return self.instance.invoke(ctx or self.owner_global(), name, arguments or {})

    def rows(self, sql, *parameters):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def sync(self):
        return {(row["input_id"], row["context_event_id"]): row["status"]
                for row in self.rows("SELECT input_id, context_event_id, status FROM context_usage_sync")}

    def usage_calls(self):
        return self.rows("SELECT * FROM context_usage_calls ORDER BY started_at, call_id")

    def dump(self, *tables):
        return {name: self.rows(f"SELECT * FROM {name} ORDER BY rowid") for name in tables}

    def register_context_sources(self):
        """The mandatory Context sources of the Global scope (packaged seeds) and the business (explicit synthetic documents)."""
        from host_foundation_support import register_sources

        register_sources(self.instance, self.ids["first"], "first")

    def coordinator(self):
        """The registered Ordubashy: the recipient the queue requires for a blocked-input notice."""
        if self._coordinator is None:
            self.register_context_sources()                          # agents need the mandatory Context sources
            self._coordinator = create_registered_global(self.instance, "ordubashy")
        return self._coordinator

    # -- queue input + attempt + library event, in the order the real driver does them ---------------------------------------------

    def enqueue(self, name):
        result = self.instance.invoke(self.instance.human(), "context_queue_submit", {
            "operation": "add", "path": f"foundation/notes/{name}.md", "content": CONTENT,
            "metadata": {"kind": "note", "title": name, "origin": "agent"}})
        assert result.status == "ok", result.error
        return result.data["input"]["id"]

    def process(self, name, *, action=None, usage=..., ending="published"):
        """Queue one note, reserve its Context event in a real attempt, run the scripted pipeline, and close the attempt.

        ``ending``: ``published`` closes it published, ``blocked`` records the failure the pipeline raised, and ``open`` leaves
        the attempt open (a process stop mid-call). Returns ``(input_id, event_id)``."""
        self.sequence += 1
        input_id, event_id = self.enqueue(name), f"e{self.sequence:04d}"      # five lowercase characters, as the queue requires
        with self.database.transaction() as uow:
            transitions.start_attempt(uow, driver_id=SYSTEM, input_id=input_id, context_event_id=event_id)
        self.runner.action = action or propagate
        self.runner.usage = report if usage is ... else usage
        outcome = None
        try:
            outcome = self.pipeline.submit("add", f"foundation/notes/{name}.md", content=CONTENT.encode(), submitted_by="test",
                                           metadata={"kind": "note", "title": name, "origin": "agent"}, event_id=event_id)
        except PropagationError as error:
            assert ending == "blocked", error
            coordinator = self.coordinator()
            with self.database.transaction() as uow:
                transitions.record_blocked(uow, driver_id=SYSTEM, input_id=input_id, ordubashy_id=coordinator,
                                           error={"code": "context_failed", "message": "The scripted propagation failed"})
            return input_id, event_id
        except ProcessStopped:
            assert ending == "open"
            return input_id, event_id
        assert ending == "published", ending
        with self.database.transaction() as uow:
            transitions.record_published(uow, driver_id=SYSTEM, input_id=input_id, ordubashy_id="synthetic",
                                         result={"context_event_id": event_id, "published_commit": outcome["published_commit"]},
                                         handoff_note={"disposition": "empty", "text": ""},
                                         outcomes=self.instance.runtime.context_services.composition.outcomes)
        return input_id, event_id

    def reserve(self, name, event_id=None):
        """Queue one note and reserve a Context event for it in a real attempt without ever submitting to the library.

        The library holds nothing for that event: a collector scan of it is ``ready`` with no receipt. Returns ``(input_id, event_id)``."""
        self.sequence += 1
        input_id, event_id = self.enqueue(name), event_id or f"z{self.sequence:04d}"
        with self.database.transaction() as uow:
            transitions.start_attempt(uow, driver_id=SYSTEM, input_id=input_id, context_event_id=event_id)
        return input_id, event_id

    def receipt_files(self, event_id):
        return sorted((self.context.runtime / "runs" / event_id).glob("*/calls/*.json"))


def fail(tools, payload):
    raise RuntimeError("transport disappeared")


def stop(tools, payload):
    raise ProcessStopped()


def with_cost(amount):
    """A usage reporter whose snapshots carry an observed provider cost."""
    return lambda stage, index, emit: report(stage, index, emit, cost_usd=amount)
