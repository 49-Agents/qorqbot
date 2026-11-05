"""A synthetic stock host for the explicit Context graph-edit acceptance cases (FEATURE_COMPLETION D2).

Real: the stock host assembly and runtime (resolver, policy, application, file effects, the host's own domain factory), the actual
``context_edit`` operation with the Context integration's ``GraphEdits`` and its typed ``GraphEditAuthority``, the Context integration's queue with its
``context_graph_edits`` binding and ``context_filing`` storage (all composed by the host itself), the Context integration's ``ContextQueueDriver``
wired with that authority port by ``runtime.context_services``, a disposable real Context (real Git) and the library's own
``Pipeline``. Faked: only the model runner (it FAILS the test when called: the deterministic edit path must never reach a model),
the injected clock and the synthetic principals, roles and files under a pytest ``tmp_path``. The role sources are the committed
defaults root when ``QORQUT_TEST_ROLES_SOURCE`` names an export of it, otherwise the test workspace's agentic defaults. No
operational data, registration, credential or transcript is read; no provider, model or network is used."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_context import Context, Pipeline
from qorqut_context import graph_edits as library
from qorqut_context.runner import RunnerConfig
from qorqut_context.integration import graph_edits as context_graph_edits
from qorqut_operations.contracts import Principal, new_id

from bazarlyk.composition.assembly import HostComposition, host_contributions
from bazarlyk.shared_context.service import coordinator_of
from qorqut_context.integration.driver import ContextQueueDriver
import context_completion_support
from context_completion_support import Host as FilingHost
from host_foundation_support import publish_role, roles_source

ROLES_ENV = "QORQUT_TEST_ROLES_SOURCE"
PEOPLE = {"basic_entity_type": ["person"]}


class NoModel:
    """The injected model runner: the deterministic edit path never calls it, so any call is a failure the cases assert on."""

    config = RunnerConfig()

    def __init__(self):
        self.calls = []

    def run(self, **args):
        self.calls.append(args.get("stage"))
        raise AssertionError("the deterministic graph-edit path must never run a model")


class Interrupted(BaseException):
    """Simulates losing the process without any normal error bookkeeping."""


def make_workspace(tmp_path: Path) -> Path:
    """A protected-configuration workspace carrying a COPY of the role sources: the committed prerequisite export when named."""
    named = os.environ.get(ROLES_ENV)
    source = Path(named) if named else roles_source()
    if not (source / "roles/imported-roles/native/roles/ordubashy").is_dir():
        pytest.skip(f"{ROLES_ENV} does not name a role-source repository")
    workspace = tmp_path / "workspace"
    copy = workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns(".git"))
    return workspace


class Host(FilingHost):
    """The composed host plus the graph-edit helpers the cases drive it with."""

    def restart_driver(self, runner=None, *, wired=True):
        """A fresh driver process over the same database and Context. The model runner is always one that fails if called, unless
        a case supplies the ordinary model runner. ``wired=False`` is a process whose graph-edit authority port is unavailable."""
        super().restart_driver(runner or NoModel())
        if not wired:
            self.driver = ContextQueueDriver(self.database, self.context, self.pipeline, driver_id=self.driver_id, outcomes=self.outcomes,
                                             programs=self.runtime.programs, coordinator=coordinator_of)
        return self.driver

    # -- the graph -------------------------------------------------------------------------------------------------

    @property
    def head(self) -> str:
        return self.context.snapshot().revision

    def plan(self, *operations, rationale="Correct the recorded facts.", revision=None) -> dict:
        return {"format": library.FORMAT, "expected_revision": revision or self.head, "rationale": rationale,
                "operations": list(operations)}

    def seed(self, nodes=(), edges=(), *, scope="unset", event_id=None, support=("self",)) -> str:
        """Publish disposable objects as an earlier controlled event, with the given first-creation scope: a scope dict, ``None``
        for a malformed record, or ``"unset"`` for no record context at all. Returns the event ID."""
        operations = [put_node(*node) if isinstance(node, tuple) else put_node(node) for node in nodes]
        operations += [put_edge(*edge) for edge in edges]
        event_id = event_id or self.unused_event_id()
        record_context = None if scope == "unset" else {"scope": scope, "provenance": {"fixture": event_id}}
        Pipeline(self.context, runner=NoModel()).submit_edit(library.normalize(self.plan(*operations, rationale="Seed the graph.")),
                                                             submitted_by="fixture", event_id=event_id, record_context=record_context)
        return event_id

    def unused_event_id(self) -> str:
        used = self.context.snapshot().used_ids
        while True:
            candidate = "z" + new_id().replace("-", "")[:4]
            if candidate not in used:
                return candidate

    def scope(self, business) -> dict:
        return {"kind": "business", "business_id": self.ids[business]}

    # -- calling context_edit --------------------------------------------------------------------------------------

    def edit(self, ctx, plan, *, key=None):
        return self.call(ctx, "context_edit", {"edit": plan}, key=key)

    def queued(self, ctx, plan, *, key=None) -> dict:
        """Prepare and apply one edit; the queue DTO and execution facts of the new input."""
        return self.data(ctx, "context_edit", {"edit": plan}, key=key)

    def refused(self, ctx, plan, *, key=None) -> str:
        return self.error(ctx, "context_edit", {"edit": plan}, key=key)

    # -- stored state ----------------------------------------------------------------------------------------------

    def inputs(self) -> list[dict]:
        return self.query("SELECT * FROM context_inputs ORDER BY sequence")

    def bindings(self) -> list[dict]:
        return self.query("SELECT * FROM context_graph_edits ORDER BY reserved_event_id")

    def input(self, input_id: str) -> dict:
        return self.query("SELECT * FROM context_inputs WHERE id=?", input_id)[0]

    def events(self) -> list[str]:
        return list(self.context.snapshot().events)

    def no_model(self) -> None:
        """Nothing here ever started a model and no published event carries a model attempt or usage receipt."""
        assert self.runner.calls == []
        for event_id in self.context.snapshot().events:
            page = self.pipeline.usage_receipts(event_id)
            assert page["items"] == [], (event_id, page)

    def fingerprint(self) -> tuple:
        """Everything observable of the Context: its head, events, object IDs, and any pending draft."""
        snapshot = self.context.snapshot()
        return snapshot.revision, tuple(snapshot.events), tuple(sorted(snapshot.nodes)), tuple(sorted(snapshot.edges)), self.context.pending()

    # -- agents ----------------------------------------------------------------------------------------------------

    def pin(self, role_id: str) -> dict:
        """The exact published artifact of a supported global role (published once through the real catalog)."""
        return publish_role(self.instance, role_id)

    def as_agent(self, agent_id, business=None):
        return self.runtime.resolver.invocation_context(Principal(agent_id, "agent"), business_id=self.ids[business] if business else None)

    def _revision(self, agent_id) -> int:
        with self.database.read() as uow:
            return uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()["revision"]

    def set_overrides(self, agent_id, overrides):
        """Replace an agent's explicit permission overrides through the real operation (the revoke / restore / deny cases)."""
        return self.data(self.human(None), "permissions_set", {"agent_id": agent_id, "overrides": overrides},
                         expected_revision=self._revision(agent_id))

    def deactivate(self, agent_id):
        return self.data(self.human(None), "agents_deactivate", {"agent_id": agent_id}, expected_revision=self._revision(agent_id))


# -- plan builders --------------------------------------------------------------------------------------------------------

def put_node(id, summary=None, attributes=PEOPLE, *, explanation="Record the fact.", support=("self",)) -> dict:
    return {"op": "put_node", "record": {"id": id, "summary": summary or f"Node {id}", "attributes": dict(attributes)},
            "explanation": explanation, "citations": ["self"], "support_after": list(support)}


def put_edge(id, source, target, summary=None, attributes=None, *, explanation="Record the link.", support=("self",)) -> dict:
    return {"op": "put_edge", "record": {"id": id, "summary": summary or f"Edge {id}", "attributes": dict(attributes or {}),
                                         "from_id": source, "to_id": target},
            "explanation": explanation, "citations": ["self"], "support_after": list(support)}


def remove_node(id, *, explanation="Remove the fact.") -> dict:
    return {"op": "remove_node", "id": id, "explanation": explanation, "citations": ["self"]}


def remove_edge(id, *, explanation="Remove the link.") -> dict:
    return {"op": "remove_edge", "id": id, "explanation": explanation, "citations": ["self"]}


# -- the composed host ----------------------------------------------------------------------------------------------------

def build(tmp_path, *, businesses=("first", "second"), source_bytes=None, graph_schema=True, coordinator=True):
    """The stock host with the real ``context_edit`` operation, a disposable Context and the driver wired with the graph-edit
    authority port by the host's own Context service. ``graph_schema=False`` composes an assembly without the required
    ``context_graph_edits`` storage, to exercise the composition refusal of an incomplete synthetic target; no other case uses it.
    ``coordinator=False`` leaves the roster empty (no registered Ordubashy), for a case that registers the globals itself."""
    def incomplete(feed):
        composition = HostComposition()
        owner = context_graph_edits.storage_contribution().owner
        for contribution in host_contributions(feed):
            if getattr(contribution, "owner", None) != owner:
                composition.add(contribution)
        return composition.freeze()

    kwargs = {"workspace_factory": make_workspace, **({} if graph_schema else {"assembly": incomplete})}
    base = context_completion_support.build(tmp_path, businesses=businesses, source_bytes=source_bytes, runner=NoModel(),
                                            coordinator=coordinator, **kwargs)
    host = Host(**vars(base))
    host.restart_driver(host.runner)
    return host
