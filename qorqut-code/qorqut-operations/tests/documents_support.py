"""A synthetic host for the operational source documents: real engine parts and the real documents/history storage.

Real here: the ``Application``, the policy resolver, prepared file receipts and their applier, the ``documents`` and shared
history tables, the source store, library, source service, journal and history readers. Fake here: the host's location
functions (plain temporary directories), the agent registry and the coordinator binding. No product code, provider or live
data, and no Context filing: a source-only caller composes the identity presentation."""

from __future__ import annotations

import json
from pathlib import Path

from qorqut_operations.application import Application, OperationRegistry
from qorqut_operations.application.plans import TEXT, UUID
from qorqut_operations.contracts import InvocationContext, Principal, new_id, plain
from qorqut_operations.document_history import schema as history_schema
from qorqut_operations.document_history.files import DocumentReceiptHistory
from qorqut_operations.document_history.lifecycle import DocumentLifecycle, RESOURCE_TYPE
from qorqut_operations.document_history.reads import DocumentHistory
from qorqut_operations.document_history.records import RecordHistory
from qorqut_operations.document_history.text import SqlDocuments
from qorqut_operations.documents import schema as documents_schema
from qorqut_operations.documents.journal import JournalOperations
from qorqut_operations.documents.library import SourceLibrary
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.serialization import BODY
from qorqut_operations.documents.store import DocumentStore
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.storage import (AssemblyBuilder, Database, FileApplier, FileEffectContribution, LockPaths,
                                       RecordEffectContribution, TransactionContribution)
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.repositories import decode_row
from signoff_support import FakeAgents

HUMAN = "00000000-0000-4000-8000-000000000001"
BUSINESS = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
AGENT = "00000000-0000-4000-8000-000000000004"
STRANGER = "00000000-0000-4000-8000-0000000000c3"
COORDINATOR = "00000000-0000-4000-8000-0000000000c1"
READER = "00000000-0000-4000-8000-0000000000a4"  # a Global agent that may read Global context but is not the coordinator
INSTALLATION = "11111111-1111-4111-8111-111111111111"


class RecordHooks:
    """The one record-history callback of this synthetic host: the generic ``RecordHistory`` with no host-owned tables."""

    def __init__(self):
        self.history = RecordHistory(sql_documents=SqlDocuments({}), facts={})
        self.tracked_tables = self.history.tracked_tables

    def apply_effect(self, context, plan, change, uow, repository, key, values):
        return None

    def force_revision(self, plan, change):
        return False

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        self.history.record_applied(uow, plan, table, before, after, actor_id=actor_id)


class Agents(FakeAgents):
    """The fake agent registry: every operation of this synthetic host is allowed by default."""

    def __init__(self, operations):
        super().__init__()
        self.defaults = {name: "allow" for name in operations}


class DocumentsHost:
    def __init__(self, tmp_path, *, byte_capture=None, coordinator=COORDINATOR, present_item=None, present_view=None):
        self.tmp = Path(tmp_path)
        self.ops_root = self.tmp / "operations"
        self.lifecycle = DocumentLifecycle()
        self.receipts = DocumentReceiptHistory(byte_capture=byte_capture or {})
        self.hooks = RecordHooks()
        assembly = (AssemblyBuilder(target_name="documents-synthetic", target_version=1)
            .add(documents_schema.storage_contribution())
            .add(history_schema.storage_contribution())
            .add(TransactionContribution("documents", (), None))
            .add(FileEffectContribution("documents", {RESOURCE_TYPE: self.lifecycle}, self.receipts))
            .add(RecordEffectContribution("documents", self.hooks)).freeze())
        self.database = Database(self.tmp / "state.sqlite3", assembly=assembly).initialize_target()
        self.seed()
        self.locks = LockPaths.under(self.tmp / "locks")
        self.store = DocumentStore(self.database, operations_root=self.ops_root, document_root=self.document_root)
        self.library = SourceLibrary(self.store, **({"present": present_item} if present_item else {}))
        self.sources = DocumentSources(self.store, self.library, agent_workspace=self.agent_workspace,
                                       scope_root=self.scope_root, **({"present": present_view} if present_view else {}))
        self.journal = JournalOperations(self.store, lambda uow: coordinator)
        self.history = DocumentHistory(self.store, source_resolvers={},
                                       private_kinds={"working_memory": "Working-memory history is read elsewhere"})
        self.registry = OperationRegistry()
        self.agents = Agents(())
        self.app = Application(self.database, self.registry, policy=PolicyResolver(self.registry, self.agents, rules=PolicyRules()),
                               writer_admission=RecoveryAdmission())
        self.sources.register(self.registry, self.app)
        self.register_fixture()
        self.journal.register(self.registry)
        self.history.register(self.registry)
        self.agents.defaults = {name: "allow" for name, descriptor in self.registry._descriptors.items() if not descriptor.owner_only}
        applier = FileApplier(self.database, self.store.resolve_file_target, locks=self.locks)
        self.app.file_executor = PreparedFileExecutor(self.database, applier)
        self.registry.freeze()

    def register_fixture(self):
        """A test-only worker operation that writes any registered document through the real store, plan and receipt."""
        def prepare(ctx, args, options, uow):
            builder = self.sources.builder("context_fixture_write", ctx, args)
            before = self.store.get(ctx, args["document_id"], uow) if args.get("document_id") else None
            metadata = {**(before["metadata_json"] if before else {}), **args.get("metadata", {})}
            self.store.write(builder, uow, kind=args["kind"], before=before, path=args.get("path"), body=args.get("body"),
                             metadata=metadata, expected_revision=before["revision"] if before else None)
            return self.sources.finalize(builder, uow, "context_fixture_write", args)
        properties = {"kind": TEXT, "path": TEXT, "body": BODY, "metadata": {"type": "object"}, "document_id": UUID}
        self.registry.register(DocumentSources.mutation_descriptor("context_fixture_write", properties, ("kind",),
            prepare=prepare, apply=self.sources.apply, validate_targets=self.sources.validate_targets,
            scope_kinds={"business"}, resolve_namespace=None, global_capability=None, exposure={"mcp", "dashboard", "worker"},
            description="Test-only document writer."))

    def write(self, kind, path=None, body=None, metadata=None, document_id=None, ctx=None):
        """Create or edit one registered document; an edit that names no body keeps the stored one. Returns the stored row."""
        arguments = {"kind": kind, "metadata": metadata or {}}
        arguments.update({key: value for key, value in (("path", path), ("body", body), ("document_id", document_id)) if value is not None})
        return self.ok("context_fixture_write", arguments, ctx)["document"]

    # -- locations -------------------------------------------------------------------------------------------------

    def scope_root(self, business=None):
        return self.ops_root if business is None else Path(business["hq_path"])

    def agent_workspace(self, agent_id, business=None):
        return self.scope_root(business) / "agents" / agent_id

    def document_root(self, business, path):
        return self.scope_root(business)

    # -- seeding ---------------------------------------------------------------------------------------------------

    def seed(self):
        self.ops_root.mkdir(parents=True, exist_ok=True)
        with self.database.transaction() as uow:
            principals = uow.repository("principals")
            principals.insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
            principals.insert({"id": STRANGER, "kind": "human", "display_name": "Stranger"})
            principals.insert({"id": AGENT, "kind": "agent", "display_name": "Agent"})
            principals.insert({"id": COORDINATOR, "kind": "agent", "display_name": "Coordinator"})
            principals.insert({"id": READER, "kind": "agent", "display_name": "Reader"})
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION,
                                                   "owner_principal_id": HUMAN})
            for identity, name in ((BUSINESS, "first"), (OTHER, "second")):
                root = self.tmp / name
                uow.repository("businesses").insert({"id": identity, "slug": name, "name": name,
                    "workspace": str(root), "hq_path": str(root / "hq")})
                (root / "hq").mkdir(parents=True)
                uow.repository("business_memberships", business_id=identity).insert({"principal_id": HUMAN, "is_owner": True})
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": STRANGER})
            for agent, home, scope in ((AGENT, None, {"kind": "ecosystem", "allowed_business_ids": [BUSINESS], "global_capabilities": []}),
                                       (READER, None, {"kind": "ecosystem", "allowed_business_ids": [BUSINESS],
                                                       "global_capabilities": ["read_global_context"]}),
                                       (COORDINATOR, None, {"kind": "ecosystem", "allowed_business_ids": [BUSINESS],
                                                            "global_capabilities": ["read_global_context"]})):
                profile = {"status": "active", "role": {"id": "global-worker"}, "scope": scope}
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, "
                    "profile_json, created_at, updated_at) VALUES (?,?,?,'ecosystem',?,?,?, 'now', 'now')",
                    (agent, agent, home, "agent-" + agent[-2:], "/nonexistent/" + agent, json.dumps(profile)))

    # -- contexts and calls ----------------------------------------------------------------------------------------

    @staticmethod
    def owner(business=BUSINESS, **kwargs):
        return InvocationContext(Principal(HUMAN, "human"), business_id=business,
                                 allowed_business_ids=frozenset({business} if business else {BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}) if business is None else frozenset(),
                                 source="dashboard", **kwargs)

    @staticmethod
    def stranger(business=BUSINESS, **kwargs):
        return InvocationContext(Principal(STRANGER, "human"), business_id=business,
                                 allowed_business_ids=frozenset({BUSINESS}),
                                 global_capabilities=frozenset({"read_global_context"}) if business is None else frozenset(),
                                 source="dashboard", **kwargs)

    @staticmethod
    def agent(principal=AGENT, business=BUSINESS, **kwargs):
        return InvocationContext(Principal(principal, "agent"), business_id=business,
                                 allowed_business_ids=frozenset({BUSINESS}), profile_revision=1, source="mcp",
                                 global_capabilities=frozenset({"read_global_context"}) if principal in {COORDINATOR, READER} else frozenset(),
                                 **kwargs)

    def invoke(self, operation, arguments=None, ctx=None, **options):
        options.setdefault("idempotency_key", new_id())
        return self.app.invoke(ctx or self.owner(), operation, arguments or {}, options)

    def ok(self, operation, arguments=None, ctx=None, **options):
        result = self.invoke(operation, arguments, ctx, **options)
        assert result.status == "ok", result.error
        return plain(result.data)

    def refused(self, operation, arguments=None, ctx=None, **options):
        result = self.invoke(operation, arguments, ctx, **options)
        assert result.status != "ok", result.data
        return result.error

    # -- stored state ----------------------------------------------------------------------------------------------

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [decode_row(row) for row in uow.connection.execute(sql, parameters)]

    def document(self, identity):
        return self.query("SELECT * FROM documents WHERE id=?", identity)[0]

    def file(self, identity):
        row = self.document(identity)
        return (self.scope_root(None if row["business_id"] is None else {"hq_path": str(self.tmp / ("first" if row["business_id"] == BUSINESS else "second") / "hq")}) / row["path"])
