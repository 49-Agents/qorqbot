"""A folder or desktop installation has no installation configuration; its agents still receive their session binding."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.bootstrap import supervisor
from qorqut.bootstrap.bridge_binding import BridgeBinding


class LocalSessionBindingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / 'agent'
        (self.workspace / '.qorqut').mkdir(parents=True)
        self.registration = {'schema_version': 1, 'agent_id': str(uuid4()), 'registration_id': str(uuid4()),
            'workspace': str(self.workspace), 'service_url': 'http://127.0.0.1:1096', 'credential_file': '.qorqut/credential'}
        self.path = self.workspace / '.qorqut/registration.json'
        self.path.write_text(json.dumps(self.registration))
        self.native = {'registration_id': self.registration['registration_id'], 'session_id': str(uuid4()),
            'runtime': 'claude-code', 'owner': '42:99', 'pid': 42, 'started': '99'}
        (self.workspace / '.qorqut/native-binding.json').write_text(json.dumps(self.native))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('QORQUT_INSTALLATION_CONFIG', None)
        loaded = patch.object(supervisor, 'load_registration', return_value=self.registration)
        loaded.start()
        self.addCleanup(loaded.stop)

    def test_binding_is_written_without_an_installation_configuration(self):
        self.assertTrue(supervisor.register(self.path, self.native, {'inbox_generation': 7}))
        receipt = json.loads((self.workspace / '.qorqut/session-binding.json').read_text())
        self.assertEqual((receipt['session_id'], receipt['generation'], receipt['owner'], receipt['runtime']),
                         (self.native['session_id'], 7, '42:99', 'claude-code'))
        # The agent's MCP bridge then carries the binding on every call.
        bridge = BridgeBinding(self.registration, ancestor=lambda runtime: None)
        bridge.parent = ('claude-code', '42:99')
        with patch('qorqut.bootstrap.bridge_binding.owner_alive', return_value=True):
            self.assertEqual(bridge.headers(), {'X-Qorqut-Native-Session': self.native['session_id'],
                                                'X-Qorqut-Binding-Generation': '7'})

    def test_no_generation_writes_nothing(self):
        self.assertFalse(supervisor.register(self.path, self.native, {}))
        self.assertFalse((self.workspace / '.qorqut/session-binding.json').exists())


if __name__ == '__main__':
    unittest.main()
