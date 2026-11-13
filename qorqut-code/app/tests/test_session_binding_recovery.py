"""A verified conversation change updates MCP authority without restarting its client."""
import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from qorqut.bootstrap.bridge_binding import BridgeBinding
from qorqut.bootstrap.native_binding import NativeBindingAdapter
from qorqut.bootstrap.supervisor import Supervisor
from qorqut.native_chat.chat import NativeChat
from qorqut_operations.contracts import DomainError


class SessionBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / 'agent'
        self.local = self.workspace / '.qorqut'
        self.local.mkdir(parents=True)
        self.registration = {'schema_version': 1, 'agent_id': str(uuid4()),
            'registration_id': str(uuid4()), 'workspace': str(self.workspace),
            'service_url': 'http://127.0.0.1:1096', 'credential_file': '.qorqut/credential'}
        self.write('registration.json', self.registration)
        self.native = {**self.registration, 'session_id': str(uuid4()), 'runtime': 'codex',
            'owner': '42:99', 'pid': 42, 'started': '99', 'execution_target': 'local'}
        self.write('native-binding.json', self.native)
        self.receipt = {k: self.native[k] for k in ('registration_id', 'session_id', 'runtime', 'owner')}
        self.receipt.update(generation=37, desired_state='running')
        self.write('session-binding.json', self.receipt)
        proc = self.root / 'proc' / '42'
        proc.mkdir(parents=True)
        (proc / 'cmdline').write_bytes(b'codex\0')
        # macOS has no /proc: there the bridge asks the process inspector for the same command line.
        inspected = patch('qorqut.process_info.cmdline', return_value=['codex'])
        inspected.start()
        self.addCleanup(inspected.stop)
        self.bridge = BridgeBinding(self.registration,
            ancestor=lambda runtime: {'pid': 42, 'started': '99'} if runtime == 'codex' else None,
            proc=proc.parent)
        self.alive = patch('qorqut.bootstrap.bridge_binding.owner_alive', return_value=True)
        self.alive.start()
        self.addCleanup(self.alive.stop)

    def write(self, name, value):
        (self.local / name).write_text(json.dumps(value))

    def test_generation_refreshes_without_restarting_bridge(self):
        self.assertEqual(self.bridge.headers()['X-Qorqut-Binding-Generation'], '37')
        self.write('session-binding.json', {**self.receipt, 'generation': 38})
        self.assertEqual(self.bridge.headers()['X-Qorqut-Binding-Generation'], '38')

    def test_conversation_change_fences_old_receipt_until_new_one_matches(self):
        self.assertIn('X-Qorqut-Native-Session', self.bridge.headers())
        new_session = str(uuid4())
        self.write('native-binding.json', {**self.native, 'session_id': new_session})
        self.assertNotIn('X-Qorqut-Native-Session', self.bridge.headers())
        self.write('session-binding.json', {**self.receipt, 'session_id': new_session, 'generation': 38})
        self.assertEqual(self.bridge.headers()['X-Qorqut-Native-Session'], new_session)

    def test_replaced_process_cannot_borrow_new_process_binding(self):
        self.bridge.headers()
        self.write('native-binding.json', {**self.native, 'owner': '43:100'})
        self.write('session-binding.json', {**self.receipt, 'owner': '43:100'})
        self.assertNotIn('X-Qorqut-Native-Session', self.bridge.headers())

    def test_missing_or_malformed_receipt_drops_previous_authority(self):
        self.bridge.headers()
        for value in ([], {**self.receipt, 'generation': True}, {**self.receipt, 'generation': 0}):
            self.write('session-binding.json', value)
            self.assertNotIn('X-Qorqut-Native-Session', self.bridge.headers())
        (self.local / 'session-binding.json').unlink()
        self.assertNotIn('X-Qorqut-Native-Session', self.bridge.headers())

    def test_adoption_publishes_server_generation_through_existing_helper(self):
        config = self.root / 'installation.json'
        config.write_text(json.dumps({'format': 1, 'root': str(self.root)}))
        selected = NS(binding=self.native, generation=38, desired_state='running')
        profile = {'agent_id': self.registration['agent_id'], 'workspace': str(self.workspace)}
        self.bridge.headers()
        with patch.dict(os.environ, {'QORQUT_INSTALLATION_CONFIG': str(config)}), \
                patch('qorqut.bootstrap.codex_session.owner_alive', return_value=True):
            NativeChat._publish_local_binding(NativeBindingAdapter(NS()), profile, selected)
        self.assertEqual(self.bridge.headers()['X-Qorqut-Binding-Generation'], '38')
        binding = json.loads((self.root / 'state/agent-supervisor' /
                              (self.registration['registration_id'] + '.json')).read_text())
        self.assertEqual(binding['session_id'], self.native['session_id'])
        self.assertEqual(binding['generation'], 38)

    def test_adoption_does_not_publish_remote_or_replaced_observation(self):
        profile = {'agent_id': self.registration['agent_id'], 'workspace': str(self.workspace)}
        adapter = NativeBindingAdapter(NS())
        with patch('qorqut.bootstrap.codex_session.owner_alive', return_value=True), \
                patch('qorqut.bootstrap.supervisor.register') as publish:
            selected = NS(binding={**self.native, 'execution_target': 'remote'}, generation=38, desired_state='running')
            self.assertFalse(adapter.publish_local(profile, selected))
            selected.binding = {**self.native, 'session_id': str(uuid4())}
            self.assertFalse(adapter.publish_local(profile, selected))
        publish.assert_not_called()

    def test_bad_legacy_registration_does_not_stop_other_session_recovery(self):
        directory = self.root / 'state/agent-recovery'
        directory.mkdir(parents=True)
        bad = {**self.native, 'registration_path': str(self.workspace / '.bazarlyk/registration.json')}
        good = {**self.native, 'request_id': str(uuid4()),
                'registration_path': str(self.local / 'registration.json')}
        (directory / 'a-obsolete.json').write_text(json.dumps(bad))
        (directory / 'b-current.json').write_text(json.dumps(good))
        with patch('qorqut.bootstrap.supervisor.ThreadPoolExecutor') as pool, \
                patch('qorqut.bootstrap.supervisor.load_registration',
                      side_effect=DomainError('registration_conflict', 'Legacy registration')):
            supervisor = Supervisor({'root': str(self.root)}, alive=lambda _: True)
            supervisor.context_check_at = supervisor.reference_check_at = supervisor.report_check_at = float('inf')
            supervisor.tick()
        pool.return_value.submit.assert_called_once_with(supervisor.recover, good)

    def test_selection_publishes_after_commit_with_server_generation(self):
        events = []
        @contextmanager
        def transaction(**_):
            events.append('begin')
            yield NS(connection=object())
            events.append('commit')
        selected = NS(binding=self.native, generation=38, desired_state='running',
                      public=lambda: {'generation': 38})
        profile = {'agent_id': self.registration['agent_id'], 'workspace': str(self.workspace)}
        adapter = NS(verify=Mock(return_value=(self.native, 'dead')),
                     publish_local=Mock(side_effect=lambda *_: events.append('publish')))
        live = NS(provider='codex', conversation_id=self.native['session_id'], pid=42, started='99', owner=None)
        runtime = NS(native_binding_adapter=adapter,
                     application=NS(writer_admission=NS(writer=nullcontext), ensure_writes_open=Mock()),
                     resolver=NS(require_agentic_view=lambda: NS(read_snapshot=nullcontext)),
                     agent_context=Mock(), inbox=NS(start_session=Mock()))
        chat = NS(runtime=runtime, arbiter=NS(locks=lambda _: nullcontext()),
                  database=NS(read=lambda: nullcontext(NS(connection=object())), transaction=transaction),
                  probe=NS(observe=lambda _: NS(reason='live', observation=live)),
                  _target=lambda *_: profile, _candidate_marker=lambda *_: self.native,
                  _publish_local_binding=NativeChat._publish_local_binding, backend=NS(wake=Mock()))
        with patch('qorqut.native_chat.chat.require_protected_control'), \
                patch('qorqut.native_chat.chat.selection.selected', side_effect=[None, selected]), \
                patch('qorqut.native_chat.chat.store.bump'):
            result = NativeChat.select(chat, 'owner', profile['agent_id'], 'codex', self.native['session_id'], 0)
        self.assertTrue(result['changed'])
        self.assertEqual(events, ['begin', 'commit', 'publish'])
        adapter.publish_local.assert_called_once_with(profile, selected)


if __name__ == '__main__':
    unittest.main()
