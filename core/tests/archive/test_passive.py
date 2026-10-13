import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import qorqut_core


class PassiveTests(unittest.TestCase):
    def test_import_assembly_and_public_inventory_are_passive(self):
        probe = r'''
import os, sys
sys.dont_write_bytecode = True
from pathlib import Path
def audit(event, args):
    if event in {"sqlite3.connect", "subprocess.Popen", "os.system", "socket.__new__", "socket.bind", "os.mkdir", "ctypes.dlopen"}:
        raise AssertionError("Unexpected archive import/assembly effect: " + event)
    if event == "open":
        path, mode, flags = args
        if str(path).startswith("/home/alp/qorqut/"):
            raise AssertionError("Upstream runtime access")
        if (mode and any(c in mode for c in "wax+")) or flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR):
            raise AssertionError("Import/assembly write")
sys.addaudithook(audit)
import qorqut_core
from qorqut_core.app import Application
from qorqut_core.transcripts.archive import Archive, ArchiveMetadata
from qorqut_core.transcripts import archive_models
from qorqut_core.storage.database import Database, HostLock
from qorqut_core.storage.migrations import SCHEMA_VERSION
from qorqut_core.config import InstallationSettings
from uuid import UUID
root = Path(sys.argv[2])
settings = InstallationSettings(installation_id=UUID("11111111-1111-4111-8111-111111111111"),
    source_root=root/"source", release_root=root/"release", data_root=root/"data",
    listen_address="127.0.0.1", listen_port=17501)
db = Database(root/"data/state.sqlite3", HostLock(root/"data/locks/host.lock"))
app = Application(settings, db)
assert isinstance(app.archive, Archive)
assert set(app.registry) == {"config_get", "config_update", "operations_get", "events_list",
    "agents_create", "agents_get", "agents_list", "agents_update", "agents_status_set",
    "messages_send", "messages_list", "messages_acknowledge", "deliveries_get", "conversation_list", "runtime_get", "terminal_get", "files_reference", "browsers_attach", "browser_get", "input_requests_create",
    "input_requests_list", "input_requests_complete", "input_requests_cancel", "input_requests_resume"}
assert set(app.jobs.handlers) == {"file_install", "agent_provision", "agent_project", "message_delivery", "conversation_publish", "browser_control"}
assert SCHEMA_VERSION == 11
expected = Path(sys.argv[1]).resolve()
for name, module in tuple(sys.modules.items()):
    if name == "qorqut_core" or name.startswith("qorqut_core."):
        assert Path(module.__file__).resolve().is_relative_to(expected), (name, module.__file__)
print(qorqut_core.__file__)
'''
        with tempfile.TemporaryDirectory(prefix="archive-passive-") as temporary:
            package = Path(qorqut_core.__file__).resolve().parent
            env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "PYTHONPATH": str(package.parent), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-c", probe, str(package), temporary],
                                    cwd=temporary, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(Path(qorqut_core.__file__)))
            self.assertEqual(list(Path(temporary).iterdir()), [])
