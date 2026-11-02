"""The host's trusted composition of one target store: contributions in, one frozen ``Assembly`` out.

Registration is single-threaded code in this package, finished before any ``Database`` exists; nothing reachable
from HTTP, MCP or a worker can add a contribution. The host assembler refuses a duplicate contribution itself (the
engine's builder refuses them again), then the engine freezes the result, requiring the mandatory hooks, validating the
schema it describes and computing the target identity that ``open_existing`` compares with the stored one.

Opening and initialising are separate on purpose. ``open_database`` only validates an already initialised store at the
instance's database path. ``initialize_empty_target`` is the explicit entrypoint synthetic checks and a reviewed
empty-instance setup call; the service, ``build_runtime`` and the host commands never do."""

from __future__ import annotations

from qorqut_operations.background import storage_contribution as background_storage
from qorqut_operations.inbox_nudges import storage_contribution as inbox_nudges_storage
from qorqut_operations.signoffs.service import storage_contribution as signoff_storage
from qorqut_operations.storage.assembly import (Assembly, AssemblyBuilder, AssemblyError, CONTRIBUTION_TYPES,
                                                FileEffectContribution, RecordEffectContribution,
                                                TransactionContribution)
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import Database

from bazarlyk.extensions import NO_EXTENSIONS
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.native_chat.schema import storage_contribution as native_chat_storage
from bazarlyk.telegram.media_schema import storage_contribution as telegram_media_storage
from .agent_work import storage_contribution as agent_work_storage
from .documents import DocumentLifecycle, DocumentReceiptHistory, RESOURCE_TYPE as DOCUMENTS
from .records import HostRecordEffects
from .schema import storage_contributions
from .transactions import inbox_finalizers, require_change_feed

TARGET_NAME = "qorqut-v1.2"
TARGET_VERSION = 1


class HostComposition:
    """Collects contributions, refusing a repeated owner or table before the engine builder sees it."""

    def __init__(self, *, target_name=TARGET_NAME, target_version=TARGET_VERSION):
        self._builder = AssemblyBuilder(target_name=target_name, target_version=target_version)
        self._owners: set[tuple[type, str]] = set()
        self._tables: set[str] = set()

    def add(self, contribution) -> HostComposition:
        kind = type(contribution)
        if kind not in CONTRIBUTION_TYPES:
            raise AssemblyError("Only typed contributions made by trusted assembly code can be registered")
        key = (kind, contribution.owner)
        if key in self._owners:
            raise AssemblyError(f"Duplicate host contribution: {kind.__name__} for owner {contribution.owner}")
        for name in getattr(contribution, "tables", ()):
            if name in self._tables:
                raise AssemblyError(f"Duplicate host table ownership: {name}")
            self._tables.add(name)
        self._owners.add(key)
        self._builder.add(contribution)
        return self

    def freeze(self) -> Assembly:
        return self._builder.freeze()


def host_contributions(change_feed, extensions=NO_EXTENSIONS):
    """Every contribution of the retained host domains, in registration order, then the selected extensions' own (their
    tables, triggers and indexes sit on the host tables they reference, so they come last and in id order). The Operations engine owns the Signoff,
    routine, job and neutral Context relationship/review tables; its Signoff and background machinery state the host
    tables they read (``inbox_changes``, ``documents``) as required-table contributions, checked when the store is
    composed, initialised and validated. The inbox nudge engine owns its three tables and reads ``inbox_events`` and
    ``inbox_deliveries``. A worker's one current assignment (``agent_current_work``) is its own host table beside the
    generated ``agent_activity`` observations. The sealed-snapshot metadata of outbound
    Telegram files (``telegram_file_payloads``) is the host's own table beside the generated ``telegram`` tables it references."""
    return (*storage_contributions(), signoff_storage(), background_storage(), native_chat_storage(), inbox_nudges_storage(),
            agent_work_storage(), telegram_media_storage(),
            FileEffectContribution("documents", {DOCUMENTS: DocumentLifecycle()}, DocumentReceiptHistory()),
            RecordEffectContribution("records", HostRecordEffects()),
            TransactionContribution("inbox", finalizers=inbox_finalizers(extensions.sources)),
            TransactionContribution("change-feed", change_publisher=require_change_feed(change_feed)),
            *extensions.storage())


def host_assembly(change_feed, extensions=NO_EXTENSIONS) -> Assembly:
    """The frozen assembly of the retained host and the selected extensions. ``change_feed`` is the process's one
    ``ChangeFeed``. The selected storage is part of the target fingerprint, so a store prepared for another extension
    set is refused when it is opened."""
    composition = HostComposition()
    for contribution in host_contributions(change_feed, extensions):
        composition.add(contribution)
    return composition.freeze()


def require_selected_storage(assembly: Assembly, extensions) -> None:
    """Refuse an opened store whose extension storage is not the selected extensions' own: the same owner names are not
    enough, so the ordered owner/DDL entries, table specifications, immutable columns and required table / lifecycle
    metadata of ``extensions.storage()`` must be the ones this assembly carries, and it may carry no other non-core owner,
    table or immutable column. A deliberately incomplete core assembly (isolated checks) is judged by the same rule."""
    core = host_assembly(assembly.change_publisher, NO_EXTENSIONS)
    expected = extensions.storage()
    core_owners = {owner for owner, _ in core.schema}
    entries = [(owner, tuple(statements)) for owner, statements in assembly.schema if owner not in core_owners]
    tables = {name for item in expected for name in item.tables}
    immutable = {name for item in expected for name in item.immutable_fields}
    valid = (entries == [(item.owner, tuple(item.schema)) for item in expected]
             and set(assembly.tables) - set(core.tables) == tables
             and set(assembly.immutable_fields) - set(core.immutable_fields) == immutable)
    for item in expected:
        valid = valid and all(assembly.tables.get(name) == spec for name, spec in item.tables.items())
        valid = valid and all(assembly.immutable_fields.get(name) == fields for name, fields in item.immutable_fields.items())
        valid = valid and item.required_lifecycles <= assembly.required_lifecycles
        valid = valid and all(item.owner in assembly.required_tables.get(name, ()) for name in item.required_tables)
    if not valid:
        raise DomainError("invalid_configuration", "The database was not opened for the selected extension set")


def require_additive_registrations(assembly: Assembly, extensions, core_validators) -> None:
    """Refuse a selected extension that would replace core behavior instead of adding to it: a package validator for a plugin
    core already validates (``core_validators``), an inbox source keyed like a core table or source, or a typed reference
    named like a core item kind. Core's own vocabulary decides: its assembly tables and inbox source / item tables."""
    from bazarlyk.inbox.events import FOCUS_SOURCE_TABLES, ITEM_TYPES
    from bazarlyk.inbox.service import SOURCES, THREAD_SOURCES, VISIBLE_TYPES
    from qorqut_operations.storage.inventory import compile_target
    core_sources = {*compile_target(host_assembly(assembly.change_publisher, NO_EXTENSIONS)).columns, *SOURCES, *THREAD_SOURCES,
                    *ITEM_TYPES, *FOCUS_SOURCE_TABLES.values()}
    core_kinds = {*VISIBLE_TYPES.values(), *FOCUS_SOURCE_TABLES}
    replaced = sorted(set(extensions.package_validators) & set(core_validators))
    if replaced:
        raise DomainError("invalid_installation", "An extension cannot replace a core package validator: " + ", ".join(replaced))
    for key in extensions.sources.sources:
        handler = extensions.sources.get(key)
        if key in core_sources or handler.item_type in core_kinds:
            raise DomainError("invalid_installation", f"An extension source cannot replace a core source or item kind: {key}")


def open_database(paths: InstancePaths, assembly: Assembly) -> Database:
    """Validate the initialised target store at ``paths.database``; creates and converts nothing."""
    return Database(paths.database, assembly=assembly).open_existing()


def initialize_empty_target(paths: InstancePaths, assembly: Assembly, extensions=NO_EXTENSIONS) -> Database:
    """Explicitly create an empty target store at ``paths.database`` (isolated synthetic instances and tests only)."""
    database = Database(paths.database, assembly=assembly).initialize_target()
    from bazarlyk.operations_history.composition import history_for
    history_for(database, paths, extensions.history()).initialize()
    return database
