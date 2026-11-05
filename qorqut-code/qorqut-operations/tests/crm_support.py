"""A synthetic host for the CRM domain: the tables host with the CRM storage, operations and thread/activity seams installed.

Everything real is the engine: ``Application``, policy, Signoff, the Task and Table services and ``CRMOperations`` bound to the
Table service. Everything fake is what a trusted host supplies (the Task ports, agents, project directories). The
``Dashboard`` here is a test double for the Host's activity reader, labelled as such: it only merges the pieces the
domain exposes, to show they compose. No product code, provider, live data or network is involved."""

from __future__ import annotations

from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER, STRANGER  # noqa: F401
from types import SimpleNamespace

from qorqut_operations import crm
from qorqut_operations.inbox import EventFinalizer
from qorqut_operations.contracts import new_id
from tables_support import COLUMNS, ELIGIBLE, TablesHost  # noqa: F401

STAGES = {"stages": [{"id": "lead", "name": "Lead"}, {"id": "won", "name": "Won"}, {"id": "lost", "name": "Lost"}]}
CRM_COLUMNS = [{"id": "name", "name": "Name", "type": "text", "record_field": "title"},
               {"id": "email", "name": "Email", "type": "text", "record_field": "customer_profile.email"},
               {"id": "value", "name": "Value", "type": "number"},
               {"id": "notes", "name": "Notes", "type": "text"}]
CRM_OPERATIONS = sorted(crm.MUTATION_NAMES + crm.READ_NAMES)


class Routing:
    """A test double for the host's ``EventRouting`` adapter: no Focus cards, coordinator or eligibility rules, so only the
    package's own mapping and recipient rules are exercised."""

    def begin_batch(self, uow): return None
    def coalesced_creation(self, capture): return False
    def responsible(self, uow, capture): return None
    def early_targets(self, uow, capture, data): return None
    def additional_targets(self, uow, capture, data, business, actor): return None
    def terminal_signoff(self, data, before, after, terminal): return terminal
    def augment_direct(self, uow, capture, business, record, direct, responsible): return None
    def presentation(self, capture): return None
    def augment_recipients(self, uow, business, table, identity, change, recipients, batch): return None
    def recipient_allowed(self, uow, agent, business, table, guard): return True
    def presentation_summary(self, record): return None


class CRMHost(TablesHost):
    """The tables host with CRM entries, Table discussions and recorded activity composed over the same database."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path, extra=(crm.storage_contribution(),))
        self.agents.defaults.update({name: "allow" for name in CRM_OPERATIONS})
        self.agents.defaults.update({"tasks_get": "allow", "tasks_create": "allow", "tasks_update": "allow"})

    def build_tables(self):
        tables = super().build_tables()
        self.crm = crm.CRMOperations(self.app, tables, self.tasks, self.items, clock=self.clock)
        tables.bind_crm(self.crm)
        return tables

    def extend(self, registry):
        self.crm.register(registry)

    # -- inbox ------------------------------------------------------------------------------------------------------

    def forget_changes(self):
        """Drop the captures made so far, so a following ``finalize`` announces only the operation under test."""
        with self.database.transaction() as uow:
            uow.connection.execute("DELETE FROM inbox_changes")

    def finalize(self):
        """Run the package's real ``EventFinalizer`` over the pending captures (the test host installs no finalizer of its own)."""
        sources = SimpleNamespace(get=lambda key: None, item_types={}, subscribable=())
        finalizer = EventFinalizer(sources=sources, item_types={"tasks": "task", "actions": "action", "data_tables": "table"},
                                   feed_only=frozenset({"tasks", "actions"}), host=Routing())
        with self.database.transaction() as uow:
            finalizer.finalize(uow)

    def events(self):
        return self.query("SELECT id, source_type, source_id, event_type, actor_id FROM inbox_events ORDER BY id")

    # -- calls ------------------------------------------------------------------------------------------------------

    def crm_table(self, title="Pipeline", ctx=None, **extra):
        return self.data("tables_create", {"title": title, "table_kind": "crm", "columns": CRM_COLUMNS,
                                           "crm_config": STAGES, **extra}, ctx)["table"]

    def add(self, table, record=None, *, ctx=None, revision=None, **arguments):
        """Add an existing Task (``record``, a Task DTO) or create one (``create``) as an entry."""
        if record is not None:
            arguments["record"] = {"type": "task", "id": record["id"], "business_id": record["business_id"]}
        return self.data("crm_entries_add", {"table_id": table["id"], **arguments}, ctx,
                         expected_revision=table["revision"] if revision is None else revision)

    def entries(self, table, ctx=None, **arguments):
        return self.read("crm_entries_list", {"table_id": table["id"], **arguments}, ctx)

    def thread(self, table, ctx=None, **arguments):
        return self.read("tables_thread_get", {"table_id": table["id"], **arguments}, ctx)

    def get(self, table, ctx=None):
        return self.read("tables_get", {"table_id": table["id"]}, ctx)["table"]


CONVERSATION = "11111111-1111-4111-8111-111111111111"


def ref_of(task):
    """The scoped public reference of a Task DTO."""
    return {"type": "task", "id": task["id"], "business_id": task["business_id"]}


def email_task(host, title="Old thread", business=BUSINESS):
    """A historical Email Task (its conversation binding is read-only data), inserted the way an upgraded database holds one."""
    identity = new_id()
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=business).insert({
            "id": identity, "title": title, "status": "open", "created_by": HUMAN,
            "fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": CONVERSATION}}})
    return {"type": "task", "id": identity, "business_id": business}
