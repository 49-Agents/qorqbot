"""Single-host operational storage over one trusted, frozen target schema.

Opening a store never creates or converts anything: ``Database.initialize_target`` is an explicit act for an
isolated empty instance or a test, and ``Database.open_existing`` only validates an initialized target store."""

from .assembly import (Assembly, AssemblyBuilder, AssemblyError, FileEffectContribution, RecordEffectContribution,
                       SchemaTarget, StorageContribution, TransactionContribution)
from .database import Database, UnitOfWork
from .files import FileApplier, RegisteredFileTarget, file_values
from .locks import LockPaths
from .receipts import ApplyReceipts, IdempotencyReceipts
from .repositories import Repository
from .specs import TableSpec

__all__ = ["Assembly", "AssemblyBuilder", "AssemblyError", "ApplyReceipts", "Database", "FileApplier",
           "FileEffectContribution", "IdempotencyReceipts", "LockPaths", "RecordEffectContribution",
           "RegisteredFileTarget", "Repository", "SchemaTarget", "StorageContribution", "TableSpec",
           "TransactionContribution", "UnitOfWork", "file_values"]
