"""Synthetic hook I/O and deadlines."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.bootstrap import activity_hook as hook


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.registration = {'schema_version': 1, 'workspace': str(self.workspace),
            'registration_id': str(uuid4()), 'agent_id': str(uuid4()),
            'service_url': 'http://127.0.0.1:1', 'credential_file': '.qorqut/credential'}
        self.path = self.workspace / '.qorqut/registration.json'
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(self.registration))
        self.session = str(uuid4())
        self.event = {'hook_event_name': 'PostToolUse', 'session_id': self.session,
                      'cwd': str(self.workspace), 'tool_use_id': 'call-1'}
        self.policy = NS(activity_reminder_due=lambda **_: False,
                         from_prepared=lambda _: None, completion_wrapper=lambda _, text: text)
        self.addCleanup(patch.stopall)
        patch.object(hook, 'session_owner', return_value='123:456').start()
        patch.object(hook, 'agentic_policy', return_value=self.policy).start()

    def claim(self):
        return {'id': str(uuid4()), 'claim_token': str(uuid4()), 'claim_consumer_id': self.session,
                'claim_binding_generation': 1, 'focus_generation': 2,
                'registration_id': self.registration['registration_id'], 'text': 'Continue the current task.'}

    def test_real_module_entry_does_not_import_preparation(self):
        code = ("import runpy,sys; runpy.run_module('qorqut.bootstrap.activity_hook',run_name='__main__'); "
                "assert 'qorqut.bootstrap.preparation' not in sys.modules")
        result = subprocess.run([sys.executable, '-B', '-c', code, '--registration', str(self.path),
                                 '--runtime', 'codex'], input=json.dumps({'hook_event_name': 'diagnostic_noop'}),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_record_stores_the_tool_activity(self):
        self.assertEqual(hook.record(self.event, self.path, 'codex'), {})
        activity = hook.read_activity(self.workspace / '.qorqut/activity' / (self.session + '.json'))
        self.assertEqual(activity['genuine_tool_activity']['tool_id'], 'call-1')

    def test_concurrent_activity_updates_serialize_without_losing_sequence(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: hook.record({**self.event, 'tool_use_id': f'call-{index}'},
                          self.path, 'codex', now=index), range(32)))
        path = self.workspace / '.qorqut/activity' / (self.session + '.json')
        before = hook.read_activity(path)
        hook.record(self.event, self.path, 'codex')
        after = hook.read_activity(path)
        self.assertGreater(after['sequence'], before['sequence'])
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['genuine_tool_activity']['tool_id'], 'call-1')

    def test_attention_requests_share_budget_and_keep_receipt_if_settle_skipped(self):
        now, timeouts, actions = [10.0], [], []
        claim = self.claim()
        def post(registration, body, *, route, timeout):
            actions.append(body['action'])
            timeouts.append(timeout)
            now[0] += timeout
            return {'result': claim if body['action'] == 'claim' else True}
        stream = io.StringIO()
        original = {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': 'Recovered context.'}}
        with patch.object(hook.time, 'monotonic', side_effect=lambda: now[0]), patch.object(hook, 'checkpoint_post', post):
            self.assertTrue(hook.emit_attention(self.event, self.path, 'codex', original, stream, deadline=12.0))
        self.assertEqual(actions, ['claim', 'begin'])
        self.assertAlmostEqual(timeouts[0], 1)
        self.assertAlmostEqual(timeouts[1], .9)
        output = json.loads(stream.getvalue())
        self.assertEqual(output['hookSpecificOutput']['additionalContext'], 'Recovered context.\n\n' + claim['text'])
        receipt_path = self.workspace / '.qorqut/attention-receipts' / (claim['id'] + '-' + claim['claim_token'] + '.json')
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt['claim_token'], claim['claim_token'])
        self.assertIn('delivered_at', receipt)

    def test_expired_attention_budget_preserves_existing_output(self):
        stream = io.StringIO()
        with patch.object(hook.time, 'monotonic', return_value=20), patch.object(hook, 'checkpoint_post') as post:
            hook.emit_attention(self.event, self.path, 'codex', {'existing': 'context'}, stream, deadline=10)
        post.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue()), {'existing': 'context'})

    def test_lost_begin_response_never_emits_attention(self):
        stream = io.StringIO()
        with patch.object(hook, 'checkpoint_post', side_effect=[{'result': self.claim()}, TimeoutError()]):
            self.assertTrue(hook.emit_attention(self.event, self.path, 'codex', {'existing': True}, stream))
        self.assertEqual(json.loads(stream.getvalue()), {'existing': True})
        self.assertFalse((self.workspace / '.qorqut/attention-receipts').exists())

    def test_failed_flush_never_prints_a_second_response_or_settles(self):
        class Broken(io.StringIO):
            def flush(self):
                raise OSError('closed pipe')
        stream = Broken()
        with patch.object(hook, 'checkpoint_post', side_effect=[{'result': self.claim()}, {'result': True}]) as post:
            self.assertFalse(hook.emit_attention(self.event, self.path, 'codex', {}, stream))
        self.assertEqual(len(stream.getvalue().splitlines()), 1)
        self.assertEqual(post.call_count, 2)
        self.assertFalse((self.workspace / '.qorqut/attention-receipts').exists())

    def checkpoint(self):
        turn = str(uuid4())
        event = {**self.event, 'hook_event_name': 'Stop', 'turn_id': turn, 'stop_hook_active': False}
        value = {'sequence': 1, 'checkpoint': {'protocol': 1, 'turn_id': turn, 'root_turn_id': turn}}
        path = self.workspace / '.qorqut/activity' / (self.session + '.json')
        path.parent.mkdir()
        hook.write_state(path, value)
        return event, value, path

    def test_checkpoint_network_does_not_lock_or_overwrite_new_activity(self):
        event, value, path = self.checkpoint()
        def post(registration, body):
            with path.with_suffix('.lock').open() as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(hook.checkpoint_receipt(path.with_suffix('.checkpoint.json'))['claim_id'])
            hook.record(self.event, self.path, 'codex')
            return {'state': 'checked', 'correction': 'Older completion suggestion'}
        self.assertEqual(hook.checkpoint_stop(event, self.registration, path, value, post=post), {})
        latest = hook.read_activity(path)
        self.assertGreater(latest['sequence'], 1)
        self.assertEqual(latest['state'], 'running')
        self.assertNotIn('continuation', latest.get('checkpoint', {}))

    def test_checkpoint_spends_one_correction_and_keeps_its_receipt(self):
        event, value, path = self.checkpoint()
        requests = []
        def post(registration, body):
            requests.append(body)
            return {'state': 'checked', 'correction': 'Record completion' if body['allow_correction'] else None}
        result = hook.checkpoint_stop(event, self.registration, path, value, post=post)
        self.assertEqual(result, {'decision': 'block', 'reason': 'Record completion'})
        receipt = hook.checkpoint_receipt(path.with_suffix('.checkpoint.json'))
        self.assertEqual(hook.checkpoint_stop(event, self.registration, path, value, post=post), {})
        self.assertTrue(requests[0]['allow_correction'])
        self.assertFalse(requests[1]['allow_correction'])
        self.assertEqual(hook.checkpoint_receipt(path.with_suffix('.checkpoint.json'))['claim_id'], receipt['claim_id'])


if __name__ == '__main__':
    unittest.main()
