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
from qorqut_operations.storage.database import Database

from bazarlyk.instance_paths import InstancePaths
from bazarlyk.native_chat.schema import storage_contribution as native_chat_storage
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


def host_contributions(change_feed):
    """Every contribution of the retained host domains, in registration order. The Operations engine owns the Signoff,
    routine, job and neutral Context relationship/review tables; its Signoff and background machinery state the host
    tables they read (``inbox_changes``, ``documents``) as required-table contributions, checked when the store is
    composed, initialised and validated. The inbox nudge engine owns its three tables and reads ``inbox_events`` and
    ``inbox_deliveries``."""
    return (*storage_contributions(), signoff_storage(), background_storage(), native_chat_storage(), inbox_nudges_storage(),
            FileEffectContribution("documents", {DOCUMENTS: DocumentLifecycle()}, DocumentReceiptHistory()),
            RecordEffectContribution("records", HostRecordEffects()),
            TransactionContribution("inbox", finalizers=inbox_finalizers()),
            TransactionContribution("change-feed", change_publisher=require_change_feed(change_feed)))


def host_assembly(change_feed) -> Assembly:
    """The frozen assembly of the retained host. ``change_feed`` is the process's one ``ChangeFeed``."""
    composition = HostComposition()
    for contribution in host_contributions(change_feed):
        composition.add(contribution)
    return composition.freeze()


def open_database(paths: InstancePaths, assembly: Assembly) -> Database:
    """Validate the initialised target store at ``paths.database``; creates and converts nothing."""
    return Database(paths.database, assembly=assembly).open_existing()


def initialize_empty_target(paths: InstancePaths, assembly: Assembly) -> Database:
    """Explicitly create an empty target store at ``paths.database`` (isolated synthetic instances and tests only)."""
    return Database(paths.database, assembly=assembly).initialize_target()
