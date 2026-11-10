"""Single-host operational storage over one trusted, frozen target schema.

Opening a store never creates or converts anything: ``Database.initialize_target`` is an explicit act for an
isolated empty instance or a test, and ``Database.open_existing`` only validates an initialized target store."""

from importlib import import_module

# Each public name is imported from its module on first use. Importing this package, or one small member such as
# ``storage.safe_io``, therefore no longer loads the database, assembly, quiescence and file engines: the per-tool-call
# agent hooks and the MCP bridge read one registration file through ``safe_io`` and start in a fresh process each time.
_EXPORTS = {
    "Assembly": "assembly", "AssemblyBuilder": "assembly", "AssemblyError": "assembly",
    "FileEffectContribution": "assembly", "RecordEffectContribution": "assembly", "SchemaTarget": "assembly",
    "StorageContribution": "assembly", "TransactionContribution": "assembly",
    "InstallationRecoveryLock": "admission", "RecoveryAdmission": "admission",
    "Database": "database", "UnitOfWork": "database",
    "RegisteredDirectoryTarget": "directories", "observe_directory": "directories",
    "DIRECTORY_ACTION": ("directory_layout", "ACTION"), "directory_values": "directory_layout", "layout_hash": "directory_layout",
    "FileApplier": "files", "RegisteredFileTarget": "files", "file_values": "files",
    "LockPaths": "locks",
    "BoundedWriter": "quiescence", "DatabaseIdentity": "quiescence", "KnownZeroWriters": "quiescence",
    "QuiescenceProof": "quiescence", "RecoveryBarrierProof": "quiescence", "RecoveryQuiescer": "quiescence",
    "UnprovenStop": "quiescence", "void_stop": "quiescence",
    "ApplyReceipts": "receipts", "IdempotencyReceipts": "receipts",
    "DatabaseMaintenanceGate": "recovery_gate", "GateClosure": "recovery_gate",
    "Repository": "repositories",
    "TableSpec": "specs",
}


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = (target, name) if isinstance(target, str) else target
    value = getattr(import_module(f"{__name__}.{module}"), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted({*globals(), *_EXPORTS})


__all__ = ["Assembly", "AssemblyBuilder", "AssemblyError", "ApplyReceipts", "BoundedWriter", "Database",
           "DatabaseIdentity", "DatabaseMaintenanceGate", "DIRECTORY_ACTION", "FileApplier", "FileEffectContribution", "GateClosure", "IdempotencyReceipts",
           "InstallationRecoveryLock", "KnownZeroWriters", "LockPaths", "QuiescenceProof", "RecoveryAdmission",
           "RecoveryBarrierProof", "RecoveryQuiescer", "RecordEffectContribution", "RegisteredFileTarget", "Repository", "SchemaTarget",
           "StorageContribution", "TableSpec", "TransactionContribution", "UnitOfWork", "UnprovenStop", "directory_values", "file_values", "layout_hash", "observe_directory", "void_stop"]
