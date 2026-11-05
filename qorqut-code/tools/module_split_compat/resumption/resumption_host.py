"""The only module of the runner that touches host, Operations or Context code.

Two roles, kept apart on purpose:

* ``capture`` (baseline code only) builds a synthetic instance with the real stock host fixtures of the baseline checkout
  (``app/tests/context_completion_support.build`` and its helpers, reached through an explicit ``--tests-dir``) and leaves
  it at a controlled interruption point. Nothing in the candidate resume path imports those fixtures.
* ``open_instance`` (baseline control and candidate continuation) reopens an existing, closed instance with whatever code the
  import roots select, composes the host the way production assembles it (``build_runtime`` over the validated stored
  database, the one background service, the Context services) and exposes only the entry points the real workers call.

If the candidate renames an entry point, this is the single place to adapt; every case goes through ``Opened``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from resumption_support import git

# Crash simulation: a ``BaseException`` is not caught by the engine's ``except Exception`` bookkeeping, so it stands for losing
# the process at that point without any error handling running (the existing acceptance tests use the same device).


class ProcessStopped(BaseException):
    """Stands for the process disappearing; raised by a controlled interruption point."""


class Clock:
    """The injected clock of the queue and background executor: deferral never sleeps."""

    def __init__(self):
        self.now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now


def counting_runner(config):
    """The deterministic fake Context model runner: finishes a propagation the way the Context tools require and counts calls.

    The fake lives here, not in production, and is the only model stand-in; no provider or network is ever contacted."""
    from qorqut_context.input_modes import ACTION
    from qorqut_context.runner import RunResult

    class Runner:
        def __init__(self):
            self.config, self.calls, self.handoff = config, [], ""

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
            tools = args["tools"]
            if tools.input_mode == ACTION:
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

    return Runner()


# -- capture (baseline only) -----------------------------------------------------------------------------------------------

def capture_fixtures(tests_dir: Path):
    """The real stock-host test fixtures of the baseline checkout, imported from an explicit directory."""
    tests_dir = str(Path(tests_dir).resolve())
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import context_completion_support as support
    return support


def build_baseline_host(fixture: Path, tests_dir: Path, *, businesses=("first", "second")):
    """The stock host with a disposable real Context at ``fixture`` (instance below ``fixture/instance``)."""
    return capture_fixtures(tests_dir).build(Path(fixture), businesses=businesses)


def initialize_table_history_repository(operations_root: Path) -> dict:
    """The baseline publisher's documented prerequisite: a private Git repository at the operations root.

    This is the explicit synthetic setup the plan allows for baseline captures (the fresh-instance initializer of T2 is tested
    separately): a neutral branch with one empty bootstrap commit built from the empty tree, a local machine identity, no
    remote, nothing staged and nothing scanned."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Qorqut", GIT_AUTHOR_EMAIL="qorqut@localhost", GIT_COMMITTER_NAME="Qorqut",
               GIT_COMMITTER_EMAIL="qorqut@localhost", GIT_AUTHOR_DATE="2026-10-12T12:00:00Z",
               GIT_COMMITTER_DATE="2026-10-12T12:00:00Z", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    git(operations_root, "init", "-q", "-b", "qorqut-instance", env=env)
    empty_tree = git(operations_root, "hash-object", "-t", "tree", "/dev/null", env=env)
    commit = git(operations_root, "commit-tree", empty_tree, "-m", "Synthetic empty bootstrap", env=env)
    git(operations_root, "update-ref", "HEAD", commit, env=env)
    return {"head": commit, "tree": empty_tree, "branch": "qorqut-instance"}


def close_instance(host) -> None:
    """Close the stores of a captured host so nothing of this process still writes: ownership released, pool drained."""
    runtime = host.runtime
    try:
        runtime.release_instance()
    finally:
        runtime.database.close()


# -- resume (baseline control and candidate) ---------------------------------------------------------------------------------

@dataclass
class Opened:
    """An existing instance opened with the selected code: the runtime and the entry points the real workers call."""

    runtime: object
    database: object
    paths: object
    clock: Clock
    runner: object
    workspace: Path
    fixture: Path
    ids: dict = field(default_factory=dict)

    # -- the real worker entry points ------------------------------------------------------------------------------------

    def signoff_worker_pass(self) -> int:
        """The body of the Signoff worker loop: apply every approved proposal (``SignoffService.run_pending``)."""
        return self.runtime.signoffs.run_pending()

    def file_recovery_pass(self) -> list:
        """The startup and periodic recovery of retained file applications (what the host lifetime runs before serving)."""
        return self.runtime.application.file_executor.recover_pending(self.runtime.application)

    def background_tick(self, job_id=None):
        """One background-service pass (``BackgroundService.run_one``): the next due job, or exactly the named one."""
        return self.runtime.background.run_one(job_id)

    def context_recover(self) -> dict:
        """Mechanical startup reconciliation of the Context queue (the Context loop's first call)."""
        self.runtime.context_services.build()
        return self.runtime.context_services.recover()

    def context_step(self) -> dict:
        """One Context worker step (the loop body: usage collection, one driver step, question outbox)."""
        self.runtime.context_services.build()
        return self.runtime.context_services.step()

    def advance(self, **delta):
        return self.clock.advance(**delta)

    def close(self) -> None:
        close_instance(self)


def instance_components(code_components: dict, fixture: Path) -> dict:
    """The explicit component locations of an instance: the selected code roots, and the role source copy the fixture holds."""
    roles = Path(fixture) / "workspace" / "qorqut-roles" / "agent-roles"
    return {**{name: Path(path) for name, path in code_components.items()}, "roles": roles}


def open_instance(fixture: Path, code_components: dict) -> Opened:
    """Reopen the closed instance below ``fixture`` with the selected code, validating (never creating) its stored target.

    ``code_components`` are the explicit ``app``, ``operations_engine``, ``context_engine`` and ``dashboard`` locations of the
    code under test. Production assembly is followed: validated database, ``build_runtime``, the one background service, a
    frozen registry, owned entry without starting any service thread."""
    from bazarlyk.background.integration import attach_background
    from bazarlyk.composition.assembly import host_assembly
    from bazarlyk.instance_paths import InstancePaths
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.runtime import build_runtime
    from bazarlyk.shared_context.service import ContextSettings
    from qorqut_context.runner import RunnerConfig
    from qorqut_operations.storage import Database

    fixture = Path(fixture).absolute()
    paths = InstancePaths(fixture / "instance", instance_components(code_components, fixture))
    workspace = fixture / "workspace"
    clock, config = Clock(), RunnerConfig()
    runner = counting_runner(config)
    database = Database(paths.database, assembly=host_assembly(ChangeFeed()))
    runtime = build_runtime(workspace, paths=paths, database=database, context=ContextSettings(runner=runner, clock=clock))
    runtime.own_instance()
    attach_background(runtime, runtime.programs, clock=clock)
    runtime.freeze_registry()
    return Opened(runtime, database, paths, clock, runner, workspace, fixture)


# -- calls of the authenticated principals the saved work belongs to -------------------------------------------------------

def owner_context(opened: Opened, business_id=None):
    """The installation owner's invocation context holding every global capability (the owner's dashboard surface)."""
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES
    from qorqut_operations.contracts import InvocationContext, Principal

    with opened.database.read() as uow:
        owner = uow.repository("installation").get("service")["owner_principal_id"]
        allowed = frozenset(row["id"] for row in uow.repository("businesses").list())
    return InvocationContext(Principal(owner, "human"), business_id=business_id, allowed_business_ids=allowed,
                             global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")


def invoke(opened: Opened, context, operation, arguments=None, *, key, **options):
    from qorqut_operations.contracts import InvocationOptions

    return opened.runtime.application.invoke(context, operation, arguments or {},
                                             InvocationOptions(idempotency_key=key, **options))


def agent_context(opened: Opened, agent_id, business_id=None):
    from qorqut_operations.contracts import Principal

    return opened.runtime.resolver.invocation_context(Principal(agent_id, "agent"), business_id=business_id)
