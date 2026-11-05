"""A synthetic host for the operational records: Tasks, Actions, tags and discussion on real engine parts.

Everything real is the engine: ``Application``, ``PolicyResolver``, the Signoff service, the tags/discussion/tasks/actions
storage contributions and operations. Everything fake is what a trusted host supplies: the ``TaskPorts`` (assignee rules,
Focus, incident link, note references, owner-work routing), a ``business_configs`` table, the neutral item lookups, a fake
delegation provider, the agent registry and the Action module's required ``on_revision`` port (``Admissions``, a test-owned
callable that records each exact revision it is handed). No product code, provider or live data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from qorqut_operations import actions, discussion, signoffs, tags, tasks
from qorqut_operations.application import Application, OperationRegistry
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.signoffs import ABSENT, SignoffPorts, SignoffService
from qorqut_operations.storage import LockPaths, StorageContribution, TableSpec
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.repositories import decode_row
from relationship_support import Candidates, Hooks, NotesLookup, SCHEMA as ITEM_SCHEMA, TABLES as ITEM_TABLES, TicketLookup
from relationship_support import install
from associated_support import ActionSignoffLookup
from signoff_support import FakeAgents, FakeExecution, FakePeers, FakeReferences, FakeTasks
from support import AGENT, BUSINESS, HUMAN, OTHER, WORKER, Demo, database_at

AGENT2 = "00000000-0000-4000-8000-0000000000a2"      # a second global agent, usually working in BUSINESS
OTHER_AGENT = "00000000-0000-4000-8000-0000000000a3"  # a global agent usually working in OTHER
HOME_AGENT = "00000000-0000-4000-8000-0000000000a4"   # a retained business-home registration: history, never an actor
COORDINATOR = "00000000-0000-4000-8000-0000000000c1"  # the ecosystem Ordubashy-like global agent
STRANGER = "00000000-0000-4000-8000-0000000000c3"     # an active human member who owns nothing
INSTALLATION = "11111111-1111-4111-8111-111111111111"
START = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

SCHEMA = (
    *ITEM_SCHEMA,
    """CREATE TABLE business_configs (
        business_id TEXT REFERENCES businesses(id), domain TEXT NOT NULL, config_json TEXT NOT NULL CHECK(json_valid(config_json)),
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (business_id, domain))""",
    """CREATE UNIQUE INDEX business_configs_global ON business_configs(domain) WHERE business_id IS NULL""",
    """CREATE TABLE admitted_revisions (
        input_id TEXT PRIMARY KEY, business_id TEXT, action_key TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
        submitted_by TEXT NOT NULL, revision_json TEXT NOT NULL CHECK(json_valid(revision_json)), UNIQUE(action_key, revision))""",
)
HOST_TABLES = {**ITEM_TABLES, "business_configs": TableSpec(("business_id", "domain"), "business_id"),
               "admitted_revisions": TableSpec(("input_id",), mutable=False, revision_column=None)}

OPERATIONS = tuple(entry.name for entry in (*tags.TAG_ENTRIES, *tasks.TASK_ENTRIES, *actions.ACTION_ENTRIES))


# -- the test-owned Action revision admission ----------------------------------------------------------------------------

class Admissions:
    """The ``on_revision(ctx, exact_revision, uow) -> input_id`` port, owned by the test host.

    Each call writes one ``admitted_revisions`` row in the caller's open transaction (so a refusal rolls it back with the
    Action) and returns that row's input ID. ``refuse`` makes the next calls raise instead."""

    def __init__(self):
        self.calls, self.refuse = [], None

    def __call__(self, ctx, exact_revision, uow):
        if self.refuse is not None:
            raise self.refuse
        input_id = new_id()
        uow.connection.execute(
            "INSERT INTO admitted_revisions(input_id, business_id, action_key, revision, operation, submitted_by, revision_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (input_id, exact_revision["business_id"], exact_revision["action_key"], exact_revision["revision"],
             exact_revision["operation"], ctx.principal_id, json.dumps(plain(exact_revision), sort_keys=True)))
        self.calls.append((exact_revision["action_key"], exact_revision["revision"], input_id))
        return input_id


# -- fake task ports -------------------------------------------------------------------------------------------------

class FakeAssignees:
    def __init__(self):
        self.protected, self.directed, self.worker_exclusions = set(), set(), set()

    def protect(self, ctx, identity, uow):
        if identity in self.protected:
            raise DomainError("forbidden", "That principal is protected from peer direction")

    def owner_directed(self, ctx, identity, uow):
        return identity in self.directed

    def worker_excluded(self, ctx, identity, uow):
        return identity in self.worker_exclusions


class FakeFocus:
    schema = {"card": {"type": "string", "maxLength": 80}}

    def __init__(self):
        self.calls = []

    def creation_access(self, ctx, focus, uow):
        self.calls.append(("access", dict(focus)))

    def present_created(self, ctx, focus, item, uow):
        self.calls.append(("present", dict(focus), item["type"], item["id"]))


class FakeIncidents:
    def __init__(self):
        self.links = []

    def link_task(self, uow, ticket_id, business_id, task_id, principal_id, at):
        self.links.append((ticket_id, business_id, task_id, principal_id))


class FakeNoteReferences:
    types = ("topic", "message", "ticket", "dev_ticket")
    host_types = ("dev_ticket",)

    def resolve(self, ctx, item, uow, builder=None):
        return {"label": "Development ticket " + item["id"], "state": "open"}

    def derived(self, ctx, uow, message):
        return None


class FakeRouting:
    """The host's mandatory and optional routes for a Task event, set per test."""

    def __init__(self):
        self.required, self.optional, self.calls = [], [], []
        self.snooze_required, self.snooze_calls = [], []

    def routes(self, uow, business_id, task, actor_id):
        self.calls.append((business_id, task["id"], actor_id))
        return list(self.required), list(self.optional)

    def snooze_routes(self, uow, business_id, task, human_ids):
        self.snooze_calls.append((business_id, task["id"], list(human_ids)))
        return list(self.snooze_required)


class FakeDelegation:
    """One verified delegation assignment, as the host's provider would read it."""

    worker_operations = frozenset()

    def __init__(self):
        self.rows = {}

    def assignment(self, uow, identity):
        return self.rows.get(identity)


class Agents(FakeAgents):
    def __init__(self):
        super().__init__()
        self.defaults = {name: "allow" for name in (*OPERATIONS, "signoffs_get", "signoffs_list", "signoffs_notes_list", "signoffs_comment",
                                                    "signoffs_input_create", "signoffs_input_cancel", "signoffs_attention_set",
                                                    "signoffs_reject", "signoffs_input_answer_delegated")}


# -- the composed host -------------------------------------------------------------------------------------------------

class OperationalHost:
    """Tags, discussion, Tasks and Actions composed over one synthetic database."""

    def __init__(self, tmp_path, *, with_signoffs=True, extra=(), records=True):
        self.now = START
        self.agents, self.assignees = Agents(), FakeAssignees()
        self.focus, self.incidents, self.routing = FakeFocus(), FakeIncidents(), FakeRouting()
        self.references, self.delegation = FakeNoteReferences(), FakeDelegation()
        demo = Demo()
        demo.hooks = Hooks()
        self.database = database_at(tmp_path, demo=demo, extra=(
            StorageContribution("synthetic", HOST_TABLES, schema=SCHEMA), tags.storage_contribution(),
            discussion.storage_contribution(), tasks.storage_contribution(), actions.storage_contribution(),
            signoffs.storage_contribution(), *extra))
        self.locks = LockPaths.under(tmp_path / "locks")
        self.seed()
        ports = SignoffPorts(execution=FakeExecution(), peers=FakePeers(), tasks=FakeTasks(), references=FakeReferences(),
                             platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
        self.service = SignoffService(self.database, ports, locks=self.locks, clock=self.stamp)
        self.registry = OperationRegistry()
        self.policy = PolicyResolver(self.registry, self.agents, rules=PolicyRules())
        self.app = Application(self.database, self.registry, policy=self.policy, signoffs=self.service,
                               writer_admission=RecoveryAdmission(), delegation=self.delegation)
        self.notes, self.tickets = NotesLookup(self), TicketLookup(self)
        self.records = ActionSignoffLookup(self) if records else None
        contributions = [ItemContribution("notes", ("topic", "message"), self.notes),
                         ItemContribution("tracker", ("ticket",), self.tickets)]
        if records:
            contributions.append(ItemContribution("records", ("action", "signoff"), self.records))
        self.items = ItemResolver(self.app, tuple(contributions), candidates=Candidates())
        self.task_ports = tasks.TaskPorts(assignees=self.assignees, focus=self.focus, incidents=self.incidents,
                                          references=self.references, routing=self.routing)
        self.tasks = tasks.TaskOperations(self.app, self.items, self.task_ports, clock=self.clock)
        self.admissions = Admissions()
        self.actions = actions.ActionOperations(self.app, self.tasks.note_references(), on_revision=self.admissions, clock=self.clock)
        self.tag_ops = tags.TagOperations(clock=self.clock)
        install(self.registry, *(c for c in (self.service.contribution(), self.tag_ops.contribution(), self.tasks.contribution(),
                                             self.actions.contribution())))
        self.service.bind(self.app)

    # -- time ------------------------------------------------------------------------------------------------------

    def clock(self):
        return self.now

    def stamp(self):
        return self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def later(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.stamp()

    # -- seeding ---------------------------------------------------------------------------------------------------

    def seed(self):
        with self.database.transaction() as uow:
            principals = uow.repository("principals")
            for identity, kind, name in ((AGENT, "agent", "Proposer"), (AGENT2, "agent", "Second"), (OTHER_AGENT, "agent", "Elsewhere"),
                                         (COORDINATOR, "agent", "Coordinator"), (STRANGER, "human", "Stranger"),
                                         (WORKER, "system", "Queue driver")):
                principals.insert({"id": identity, "kind": kind, "display_name": name})
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": HUMAN})
            # Every acting agent is a current supported global: business access is the active directory, never a membership
            # row or the stored list. AGENT, AGENT2 and OTHER_AGENT hold no global capability, so they act only with a
            # business selected.
            plain = {"status": "active", "role": {"id": "global-worker"},
                     "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []}}
            profiles = {AGENT: (None, plain), AGENT2: (None, {**plain, "role": {"id": "learning-applier"}}), OTHER_AGENT: (None, plain),
                        COORDINATOR: (None, {"status": "active", "role": {"id": "ordubashy"}, "runtime": "codex", "scope": {
                            "kind": "ecosystem", "allowed_business_ids": [BUSINESS, OTHER], "global_capabilities": ["read_global_context"]}})}
            for identity, (home, profile) in profiles.items():
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                    (identity, identity, home, home or "ecosystem", "agent-" + identity[-2:], "/nonexistent/" + identity, json.dumps(profile)))
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": STRANGER})

    def home_agent(self, business=BUSINESS):
        """Retain one business-home registration with its membership: refusal tests show it never acts or is addressed."""
        with self.database.transaction() as uow:
            uow.repository("principals").insert({"id": HOME_AGENT, "kind": "agent", "display_name": "Home"})
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                (HOME_AGENT, HOME_AGENT, business, business, "agent-a4", "/nonexistent/" + HOME_AGENT,
                 json.dumps({"role": {"id": "business-agent"}, "scope": {"kind": "business", "business_id": business}})))
            uow.repository("business_memberships", business_id=business).insert({"principal_id": HOME_AGENT})
        return InvocationContext(Principal(HOME_AGENT, "agent"), business_id=business, home_business_id=business,
                                 allowed_business_ids=frozenset({business}), profile_revision=1, source="mcp")

    # -- contexts and calls ----------------------------------------------------------------------------------------

    @staticmethod
    def owner(business=BUSINESS, **kwargs):
        return InvocationContext(Principal(HUMAN, "human"), business_id=business, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}), source="dashboard", **kwargs)

    @staticmethod
    def stranger(**kwargs):
        return InvocationContext(Principal(STRANGER, "human"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}),
                                 source="dashboard", **kwargs)

    @staticmethod
    def agent(identity=AGENT, business=BUSINESS, **kwargs):
        """A capability-less global agent invoked with one business selected."""
        return InvocationContext(Principal(identity, "agent"), business_id=business,
                                 allowed_business_ids=frozenset({business} if business else ()), profile_revision=1, source="mcp",
                                 **kwargs)

    @staticmethod
    def coordinator(business=None, **kwargs):
        return InvocationContext(Principal(COORDINATOR, "agent"), business_id=business, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}), profile_revision=1, source="mcp", **kwargs)

    def invoke(self, operation, arguments=None, ctx=None, **options):
        if not operation.endswith(("_list", "_get", "_get_all")):
            options.setdefault("idempotency_key", new_id())
        return self.app.invoke(ctx or self.owner(), operation, arguments or {}, options)

    def read(self, operation, arguments=None, ctx=None):
        result = self.app.invoke(ctx or self.owner(), operation, arguments or {}, {})
        assert result.status == "ok", result.error
        return plain(result.data)

    def ok(self, operation, arguments=None, ctx=None, **options):
        result = self.invoke(operation, arguments, ctx, **options)
        assert result.status in {"ok", "signoff_required"}, result.error
        return result

    def data(self, operation, arguments=None, ctx=None, **options):
        result = self.ok(operation, arguments, ctx, **options)
        assert result.status == "ok", result.status
        return plain(result.data)

    def error(self, operation, arguments=None, ctx=None, **options):
        result = self.invoke(operation, arguments, ctx, **options)
        assert result.status == "error", f"expected a failure, got {result.status}"
        return result.error["code"]

    def stage(self, operation, arguments=None, ctx=None, **options):
        """An agent's protected call: returns the staged Signoff id."""
        result = self.invoke(operation, arguments, ctx or self.agent(), **options)
        assert result.status == "signoff_required", (result.status, result.error)
        return result.signoff["id"]

    def row(self, signoff_id):
        return self.query("SELECT * FROM signoffs WHERE id=?", signoff_id)[0]

    def approve(self, signoff_id, ctx=None):
        row = self.row(signoff_id)
        return self.service.review(ctx or self.owner(), signoff_id, decision="approve", proposal_revision=row["proposal_revision"],
                                   payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                                   idempotency_key=new_id(), feedback=None)

    def apply_approved(self, signoff_id, ctx=None):
        """The owner approves a staged proposal and the Signoff worker applies it; returns the applied row."""
        assert self.approve(signoff_id, ctx).status == "accepted"
        return self.service.run_one(signoff_id)

    # -- stored-state reads ----------------------------------------------------------------------------------------

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [decode_row(row) for row in uow.connection.execute(sql, parameters)]

    def count(self, table, where="1", *parameters):
        return self.query(f"SELECT count(*) AS n FROM {table} WHERE {where}", *parameters)[0]["n"]

    def task(self, task_id, business=BUSINESS):
        return self.query("SELECT * FROM tasks WHERE business_id IS ? AND id=?", business, task_id)[0]

    def deliveries(self, agent=None, *, source_type=None):
        sql = ("SELECT e.id AS event_id, e.source_type, e.source_id, e.event_type, d.agent_id, d.required, e.business_id FROM inbox_events e "
               "JOIN inbox_deliveries d ON d.event_id=e.id WHERE 1")
        parameters = []
        if agent:
            sql += " AND d.agent_id=?"
            parameters.append(agent)
        if source_type:
            sql += " AND e.source_type=?"
            parameters.append(source_type)
        return self.query(sql + " ORDER BY e.id, d.agent_id", *parameters)

    # -- common record helpers -------------------------------------------------------------------------------------

    def create_task(self, title="A task", ctx=None, **arguments):
        data = self.data("tasks_create", {"title": title, **arguments}, ctx)
        return data["task"]

    def record_action(self, text="Shipped the release", ctx=None, **arguments):
        data = self.data("actions_record", {"text": text, **arguments}, ctx or self.agent())
        return data["action"]

    def topic(self, identity, title="Topic", business=BUSINESS):
        with self.database.transaction() as uow:
            uow.repository("topics", business_id=business).insert({"id": identity, "title": title, "body": "Body", "visibility": "shared"})
            uow.repository("topic_files", business_id=business).insert({"id": identity, "content_hash": "0" * 64})
        return {"type": "topic", "id": identity}

    def system_uow(self):
        return self.database.transaction()


def utc(delta=None) -> str:
    return (START + (delta or timedelta())).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["AGENT", "AGENT2", "BUSINESS", "COORDINATOR", "FakeRouting", "HOME_AGENT", "HUMAN", "OTHER", "OTHER_AGENT",
           "OperationalHost", "STRANGER", "WORKER", "replace", "utc"]
