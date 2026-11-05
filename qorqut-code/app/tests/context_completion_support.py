"""A synthetic stock host for the Context filing acceptance cases (upload to settled job).

Everything real is the product path: the stock host assembly (``host_assembly``, which composes the Context integration's queue, filing,
graph-edit and usage storage itself), the retained runtime (``build_runtime``) with its policy, application, file effects and its
own domain factory, the actual ``DocumentOperations`` and ``ContextFiling``, the registered ``context.file`` / ``context.instruct``
programs and their worker authority, the real ``BackgroundService`` executor, the host's one ``runtime.context_services`` (the
Context integration's ``FilingOutcomes``, ``ExecutionFacts``, queue, ``GraphEdits``, ``ContextQueueDriver``, usage collector and the dedicated driver
principal it registers at its owned start), the supported global agents registered by the owner through the real catalog and
``agents_create`` (the coordinator is the one Ordubashy, selected by ``management_coordinator_set``), and a disposable real Context
(real Git, the library's own ``Pipeline``). The only fakes are the model runner injected through ``ContextSettings``, the injected
clock and the synthetic principals under a pytest ``tmp_path``. Nothing is wrapped or replaced in the domain registration.

No operational data, configuration, registration, transcript or credential is read, and no provider or network is used."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from qorqut_context import Context, Pipeline
from qorqut_context.input_modes import ACTION
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain, utc_now

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
from bazarlyk.service.changes import ChangeFeed
from bazarlyk.service.runtime import build_runtime
from qorqut_context.integration.service import ContextSettings
from bazarlyk.shared_context.service import ContextServices
from host_foundation_support import (Instance, create_registered_global, make_paths, make_workspace, register_sources,
                                     select_coordinator)

class Clock:
    """The one injected clock of the background executor and the queue: deferral never sleeps."""

    def __init__(self):
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now


class Runner:
    """The injected model runner: it finishes a propagation the way the Context tools require and records every call."""

    config = RunnerConfig()

    def __init__(self):
        self.calls, self.handoff, self.fail, self.during = [], "", None, None

    @property
    def propagations(self):
        return self.calls.count("propagation")

    def run(self, **args):
        self.calls.append(args["stage"])
        if args.get("on_identity"):
            args["on_identity"]({"stage": args["stage"], "thread_id": "script", "turn_id": str(len(self.calls))})
        if args["stage"] == "bootstrap":
            return RunResult("completed", json.dumps({"node_ids": [v["record"]["id"] for v in args["payload"]["candidates"]]}),
                             "script", str(len(self.calls)), self.config.model)
        tools, payload = args["tools"], args["payload"]
        if self.during:
            self.during(tools, payload)
        if self.fail:
            raise self.fail
        if tools.input_mode == ACTION:                    # an Action revision needs its explicit (here empty) association set
            self.dispatch(tools, "stage_action_associations", date=0, summary="Recorded the action.", associations=[])
        self.dispatch(tools, "stage_handoff_note", text=self.handoff)
        self.dispatch(tools, "finish_propagation", status="complete", summary="Processed the supplied input.",
                      reassessed=tools.required_reassessment)
        return RunResult("completed", "Finished.", "script", str(len(self.calls)), self.config.model)

    @staticmethod
    def dispatch(tools, name, **arguments):
        result = tools.dispatch(name, arguments)
        assert result["ok"], result
        return result["result"]


class CountingUnrelated:
    """A registered program unrelated to Context: it only counts its runs, and has no output."""

    def __init__(self):
        self.runs = 0

    def run(self, control, arguments):
        from qorqut_operations.background.programs import ProgramResult

        self.runs += 1
        return ProgramResult(outputs=(), data={"ran": self.runs})


class Host(SimpleNamespace):
    """The composed host and the small helpers the cases read and drive it with."""

    # -- identities ------------------------------------------------------------------------------------------------

    def human(self, business="first", **options):
        return InvocationContext(Principal(self.owner, "human"), business_id=self.ids.get(business, business) if business else None,
                                 allowed_business_ids=frozenset(self.ids.values()), global_capabilities=GLOBAL_CAPABILITIES,
                                 source="dashboard", **options)

    def agent_id(self, role="ordubashy"):
        """The one registered global agent of a supported ``role`` (Ordubashy is the coordinator ``build`` registered)."""
        return create_registered_global(self.instance, role)

    def agent(self, business="first", role="ordubashy"):
        """That global agent's invocation context selecting ``business`` (``None`` selects the Global target). Businesses have no
        resident agents: every supported global reaches every active business, so the selection only names the scope."""
        return self.runtime.resolver.invocation_context(Principal(self.agent_id(role), "agent"),
                                                        business_id=self.ids[business] if business else None)

    # -- operations ------------------------------------------------------------------------------------------------

    def call(self, ctx, operation, arguments=None, *, key=None, **options):
        return self.runtime.application.invoke(ctx, operation, arguments or {}, InvocationOptions(
            idempotency_key=key or new_id(), **options))

    def data(self, ctx, operation, arguments=None, **options):
        result = self.call(ctx, operation, arguments, **options)
        assert result.status in ("ok", "accepted"), (result.status, result.error)
        return plain(result.data)

    def error(self, ctx, operation, arguments=None, **options):
        result = self.call(ctx, operation, arguments, **options)
        assert result.status == "error", result.status
        return result.error["code"]

    def read(self, ctx, operation, arguments=None):
        result = self.runtime.application.invoke(ctx, operation, arguments or {}, InvocationOptions())
        assert result.status == "ok", (result.status, result.error)
        return plain(result.data)

    def upload(self, ctx, body="Met Alice about the pilot.", **arguments):
        return self.data(ctx, "context_upload", {"body": body, **arguments})

    # -- stored state ------------------------------------------------------------------------------------------------

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [json_row(row) for row in uow.connection.execute(sql, parameters)]

    def count(self, table, where="1", *parameters):
        return self.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", *parameters)[0]["n"]

    def job(self, job_id):
        return self.query("SELECT * FROM jobs WHERE id=?", job_id)[0]

    # -- driving ---------------------------------------------------------------------------------------------------

    def run_job(self, job_id=None):
        return self.background.run_one(job_id)

    def step(self):
        """One step of the host's own queue driver (the driver contract the cases assert); ``services.step()`` is the service body."""
        return self.driver.process_next()

    def restart_driver(self, runner=None):
        """A fresh service process over the same database and Context (a new, empty runner unless one is supplied)."""
        self.runner = runner or Runner()
        base = self.runtime.context_services.composition
        config = getattr(self.runner, "config", RunnerConfig())
        composition = dataclasses.replace(base, context=self.context, pipeline=Pipeline(self.context, runner=self.runner, config=config))
        services = ContextServices(composition, self.database, self.paths, programs=self.runtime.programs,
                                   reader=self.runtime.domains.shared_context, changes=None)
        self.driver_id = services.build()
        self.services, self.pipeline, self.driver = services, composition.pipeline, services.driver
        return self.driver


def json_row(row):
    """A row as a dict with its ``*_json`` columns decoded."""
    result = dict(row)
    for name, value in list(result.items()):
        if name.endswith("_json") and isinstance(value, str):
            result[name] = json.loads(value)
    return result


def build(tmp_path, *, businesses=("first", "second"), source_bytes=None, runner=None, assembly=host_assembly,
          workspace_factory=make_workspace, initialize=True, settings=None, build_service=True, register_workers=True, coordinator=True):
    """The stock host with a disposable Context, the injected runner and the host's own Context service built (not started).

    ``assembly`` is the stock ``host_assembly`` unless a case deliberately composes an incomplete one; ``workspace_factory``
    makes the protected-configuration workspace. ``initialize=False`` leaves the Context unmade (the host never makes it);
    ``settings`` are extra ``ContextSettings`` fields (for example a long ``poll_seconds``). ``build_service=False`` leaves the
    Context service unbuilt (no driver principal), as the runtime constructor leaves it. ``coordinator`` (the default) registers
    the one Ordubashy and selects it as the coordinator the driver notifies; ``False`` leaves the roster empty."""
    from qorqut_operations.application.plans import schema
    from qorqut_operations.background.programs import Program

    workspace, paths, feed, clock = workspace_factory(tmp_path), make_paths(tmp_path), ChangeFeed(), Clock()
    database = initialize_empty_target(paths, assembly(feed))
    owner, ids = new_id(), {}
    with database.transaction() as uow:
        now = utc_now()
        uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": owner,
                                               "created_at": now, "updated_at": now})
        for slug in businesses:
            ids[slug] = new_id()
            uow.repository("businesses").insert({"id": ids[slug], "slug": slug, "name": slug.title(),
                                                 "workspace": str(paths.business_root(slug)), "hq_path": str(paths.business_hq(slug))})
            uow.repository("business_memberships", business_id=ids[slug]).insert({"principal_id": owner, "is_owner": True})
    runner = runner or Runner()
    if source_bytes is not None:
        runner.config = RunnerConfig(source_bytes=source_bytes)
    from agentic_installation_support import provision
    provision(paths, roles=workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults")
    runtime = build_runtime(workspace, paths=paths, database=database, context=ContextSettings(runner=runner, clock=clock, **(settings or {})))
    runtime.own_instance()
    instance = Instance(paths, workspace, database, runtime, feed, owner, ids[businesses[0]])
    unrelated = CountingUnrelated()
    runtime.programs.register(Program("test.unrelated", 1, schema({}), unrelated.run, {}, (), (), scope_kinds=("business", "global")))
    background = attach_background(runtime, runtime.programs, clock=clock)
    runtime.freeze_registry()
    if register_workers:
        with database.transaction() as uow:
            runtime.authority.register_workers(uow, list(ids.values()))
    for slug in businesses:
        register_sources(instance, ids[slug], slug)
    # The registered coordinator the driver notifies: the one Ordubashy, which the owner registers and selects.
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy")) if coordinator else None
    if initialize:
        Context.initialize(paths.context, paths.context_runtime)       # initialized explicitly, never by the host
    services = runtime.context_services
    composition = services.composition
    context = composition.context
    host = Host(instance=instance, runtime=runtime, database=database, ids=ids, queue=composition.queue, outcomes=composition.outcomes,
                clock=clock, background=background, unrelated=unrelated, tmp=tmp_path, paths=paths, ports=composition.filing,
                owner=owner, members={}, homes={}, context=context, coordinator=coordinator,
                edits=composition.graph_edits, runner=runner, services=services, feed=feed)
    host.pipeline, host.driver, host.driver_id = composition.pipeline, None, None
    if build_service:
        host.driver_id = services.build()
        host.driver = services.driver
    return host
