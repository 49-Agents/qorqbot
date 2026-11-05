"""Single-host operational storage over one trusted, frozen target schema.

Opening a store never creates or converts anything: ``Database.initialize_target`` is an explicit act for an
isolated empty instance or a test, and ``Database.open_existing`` only validates an initialized target store."""

from .assembly import (Assembly, AssemblyBuilder, AssemblyError, FileEffectContribution, RecordEffectContribution,
                       SchemaTarget, StorageContribution, TransactionContribution)
from .admission import InstallationRecoveryLock, RecoveryAdmission
from .database import Database, UnitOfWork
from .directories import RegisteredDirectoryTarget, observe_directory
from .directory_layout import ACTION as DIRECTORY_ACTION, directory_values, layout_hash
from .files import FileApplier, RegisteredFileTarget, file_values
from .locks import LockPaths
from .quiescence import (BoundedWriter, DatabaseIdentity, KnownZeroWriters, QuiescenceProof, RecoveryBarrierProof,
                         RecoveryQuiescer, UnprovenStop, void_stop)
from .receipts import ApplyReceipts, IdempotencyReceipts
from .recovery_gate import DatabaseMaintenanceGate, GateClosure
from .repositories import Repository
from .specs import TableSpec

__all__ = ["Assembly", "AssemblyBuilder", "AssemblyError", "ApplyReceipts", "BoundedWriter", "Database",
           "DatabaseIdentity", "DatabaseMaintenanceGate", "DIRECTORY_ACTION", "FileApplier", "FileEffectContribution", "GateClosure", "IdempotencyReceipts",
           "InstallationRecoveryLock", "KnownZeroWriters", "LockPaths", "QuiescenceProof", "RecoveryAdmission",
           "RecoveryBarrierProof", "RecoveryQuiescer", "RecordEffectContribution", "RegisteredFileTarget", "Repository", "SchemaTarget",
           "StorageContribution", "TableSpec", "TransactionContribution", "UnitOfWork", "UnprovenStop", "directory_values", "file_values", "layout_hash", "observe_directory", "void_stop"]
