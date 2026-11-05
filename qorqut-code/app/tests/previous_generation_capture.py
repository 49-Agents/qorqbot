"""Capture one genuine generation of a synthetic previous-target instance with the frozen previous backend.

Run (by ``test_v12_project_backup_conversion.py``) with the previous release's ``app`` plus the Operations and Context libraries on
``PYTHONPATH``: ``python previous_generation_capture.py <instance-root> <generation-id>``. The Context is initialised by the real
Context library, the barrier is the actual ``RecoveryQuiescer`` over a synthetic stopped installation (no writers exist), and the
backup store's own ``create`` seals and verifies the generation - nothing about the generation is written by hand."""
import json
import sys
from pathlib import Path

root, generation = Path(sys.argv[1]), sys.argv[2]
from bazarlyk.composition.assembly import host_assembly
from bazarlyk.composition.files import lock_paths
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.changes import ChangeFeed
from bazarlyk.storage.recovery import BackupStore
from qorqut_context import Context
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.quiescence import KnownZeroWriters, QuiescenceProof, RecoveryQuiescer

paths = InstancePaths(root, {})
database = Database(paths.database, assembly=host_assembly(ChangeFeed())).open_existing()
if not paths.context.exists():
    Context.initialize(paths.context, paths.context_runtime)


class Frozen:
    def __init__(self, name):
        self.name = name

    def freeze(self, *, timeout):
        return QuiescenceProof(self.name, "frozen")

    def inventory_and_freeze(self, *, timeout):
        return QuiescenceProof(self.name, "frozen")

    def thaw(self):
        return None


lock = InstallationRecoveryLock(lock_paths(paths).recovery)
quiescer = RecoveryQuiescer(database, admission=RecoveryAdmission(installation_lock=lock, serving=False),
                            background=KnownZeroWriters("background"), signoffs=KnownZeroWriters("signoffs"),
                            email_history=KnownZeroWriters("email_history"), browser_writers=KnownZeroWriters("browser"),
                            sandbox_writers=KnownZeroWriters("sandbox"), container_writers=Frozen("containers"),
                            host_writers=Frozen("hosts"), installation_lock=lock)
with quiescer.quiesce() as proof:
    metadata = BackupStore(database, paths).create(generation, barrier=proof)
print(json.dumps(metadata, sort_keys=True))
