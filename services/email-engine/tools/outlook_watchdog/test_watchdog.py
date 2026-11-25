"""Offline outage, notification and real-flock checks; never touch live services."""
import contextlib
import fcntl
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('watchdog', Path(__file__).with_name('watchdog.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


@contextlib.contextmanager
def unlocked():
    yield True


class WatchdogTests(unittest.TestCase):
    def tick(self, previous, status='connection_failed', *, now=10030, **kwargs):
        return w.tick({}, previous, check=lambda _: status, save=lambda _: None,
                      clock=lambda: now, now=now, lock=unlocked, **kwargs)

    def test_two_failures_and_persist_before_restart(self):
        first, restart = w.decide({}, 'connection_failed', 10000)
        self.assertFalse(restart)
        saved = []
        def restart(_):
            self.assertTrue(saved[-1]['restart_attempted'])
            self.assertEqual(saved[-1]['last_restart_at'], 10030)
            return 'healthy'
        state = w.tick({}, first, check=lambda _: 'connection_failed', lock=unlocked,
                       save=lambda s: saved.append(dict(s)), restart=restart,
                       now=10030, clock=lambda: 10030)
        self.assertEqual(state['status'], 'healthy')
        self.assertFalse(state['restart_attempted'])

    def test_healthy_and_stale_samples_break_streak(self):
        first, _ = w.decide({}, 'connection_failed', 10000)
        healthy, _ = w.decide(first, 'healthy', 10030)
        self.assertFalse(w.decide(healthy, 'connection_failed', 10060)[1])
        self.assertFalse(w.decide(first, 'connection_failed', 10121)[1])

    def test_login_and_configuration_never_restart(self):
        for status in ('login_required', 'configuration_error'):
            with self.subTest(status=status):
                state = self.tick({}, status, restart=lambda _: self.fail('must not restart'))
                self.assertTrue(state['restart_attempted'])
                self.assertFalse(w.decide(state, 'connection_failed', 10060)[1])

    def test_cooldown_and_clock_rollback(self):
        previous = {'last_restart_at': 10000, 'failures': 1, 'last_failure_at': 11000}
        self.assertFalse(w.decide(previous, 'connection_failed', 11030)[1])
        previous['last_failure_at'] = 12000
        self.assertTrue(w.decide(previous, 'connection_failed', 12030)[1])
        previous['last_restart_at'] = 13000
        self.assertFalse(w.decide(previous, 'connection_failed', 12030)[1])

    def test_failed_recovery_latches_and_alerts_once_then_resolves(self):
        first, _ = w.decide({}, 'connection_failed', 10000)
        state = self.tick(first, restart=lambda _: 'connection_failed')
        self.assertEqual(state['status'], 'needs_attention')
        events = []
        w.report_attention(state, events.append)
        state = self.tick(state, now=10060, restart=lambda _: self.fail('latched'))
        w.report_attention(state, events.append)
        self.assertEqual([e['state'] for e in events], ['firing'])
        state = self.tick(state, 'healthy', now=10090)
        w.report_attention(state, events.append)
        w.report_attention(state, events.append)
        self.assertEqual([e['state'] for e in events], ['firing', 'resolved'])
        self.assertFalse(state['restart_attempted'])

    def test_busy_sender_cannot_hide_crash_or_consume_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'lock'
            lock_path.touch()
            checks, events = [], []
            with lock_path.open('rb') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def check(_):
                    checks.append(True)
                    return 'connection_failed'
                state = {}
                for now in (10000, 10030, 10060, 10090, 10120, 10150):
                    state = w.tick({}, state, check=check, save=lambda _: None,
                                   lock=lambda: w.browser_lock(lock_path, wait=0),
                                   restart=lambda _: self.fail('sender lock held'),
                                   now=now, clock=lambda: now)
                    w.report_attention(state, events.append)
                self.assertEqual(len(checks), 6)
                self.assertEqual(state['status'], 'needs_attention')
                self.assertFalse(state.get('restart_attempted', False))
                self.assertEqual(len(events), 1)
            def restart(_):
                with lock_path.open('rb') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 'healthy'
            state = w.tick({}, state, check=check, save=lambda _: None,
                           lock=lambda: w.browser_lock(lock_path, wait=0), restart=restart,
                           now=10180, clock=lambda: 10180)
            self.assertEqual(state['status'], 'healthy')
            self.assertNotIn('lock_wait_since', state)

    def test_health_rechecked_after_lock_before_restart(self):
        first, _ = w.decide({}, 'connection_failed', 10000)
        statuses = iter(('connection_failed', 'healthy'))
        state = w.tick({}, first, check=lambda _: next(statuses), save=lambda _: None,
                       lock=unlocked, restart=lambda _: self.fail('already recovered'),
                       now=10030, clock=lambda: 10030)
        self.assertEqual(state['status'], 'healthy')

    def test_missing_lock_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing'
            with self.assertRaises(FileNotFoundError):
                with w.browser_lock(path, wait=0):
                    self.fail('missing lock must refuse')
            self.assertFalse(path.exists())

    def test_notifications_retry_without_marking_failed_delivery_successful(self):
        state = {'status': 'login_required'}
        def fail(_):
            raise OSError('secret should never appear in state')
        w.report_attention(state, fail)
        self.assertEqual(state['notification_status'], 'failed')
        self.assertNotIn('alert_state', state)
        events = []
        w.report_attention(state, events.append)
        w.report_attention(state, events.append)
        self.assertEqual(len(events), 1)
        self.assertEqual(state['notification_status'], 'queued')
        self.assertNotIn('secret', json.dumps(state))

    def test_no_unsolicited_recovery_notice(self):
        w.report_attention({'status': 'healthy'}, lambda _: self.fail('no previous outage'))

    def test_lost_acknowledgement_is_resolved_even_without_known_delivery(self):
        state = {'status': 'needs_attention'}
        saved, events = [], []
        def lost_ack(event):
            self.assertEqual(saved[-1]['alert_pending'], event['state'])
            events.append(event['state'])
            raise TimeoutError('gateway accepted but response was lost')
        save = lambda s: saved.append(dict(s))
        w.report_attention(state, lost_ack, save)
        state['status'] = 'healthy'
        w.report_attention(state, lost_ack, save)
        w.report_attention(state, lambda e: events.append(e['state']), save)
        self.assertEqual(events, ['firing', 'resolved', 'resolved'])
        self.assertEqual(state['alert_state'], 'resolved')
        self.assertNotIn('alert_pending', state)

    def test_check_only_never_locks_saves_restarts_or_notifies(self):
        with patch.object(w, 'runtime', return_value={}), \
             patch.object(w, 'probe', return_value='healthy'), \
             patch.object(w, 'browser_lock', side_effect=AssertionError('lock')), \
             patch.object(w, 'save_state', side_effect=AssertionError('save')), \
             patch.object(w, 'report_attention', side_effect=AssertionError('alert')), \
             patch('sys.argv', ['watchdog', '--check-only']):
            w.main()

    def test_state_private_health_public_and_minimal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = {'failures': 1, 'last_restart_at': 10000, 'checked_at': 10030,
                     'probe': 'connection_failed', 'status': 'first_failure', 'private': 'not published'}
            w.save_state(state, path)
            self.assertEqual(w.load_state(path), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            health = path.with_name('health.json')
            w.publish_health(state, 'account', health)
            self.assertEqual(health.stat().st_mode & 0o777, 0o644)
            self.assertNotIn('private', health.read_text())
            for bad in ('broken', float('nan')):
                path.write_text(json.dumps({'failures': bad}))
                with self.assertRaises(ValueError):
                    w.load_state(path)

    def test_gateway_wire_format_and_private_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret'
            secret.write_text('test-only-secret')
            secret.chmod(0o600)
            config = root / 'notify.json'
            sock_path = root / 'gateway.sock'
            config.write_text(json.dumps({'secret_file': str(secret), 'socket_path': str(sock_path)}))
            received = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(sock_path)); server.listen(1); server.settimeout(2)
                def respond():
                    connection, _ = server.accept()
                    with connection:
                        received.append(json.loads(connection.recv(65536)))
                        connection.sendall(b'{"ok":true,"result":{"action":"posted"}}\n')
                worker = threading.Thread(target=respond)
                worker.start()
                w.notify_owner({'event_key': 'test', 'state': 'firing'}, config)
                worker.join(2)
            self.assertEqual(received[0]['action'], 'notify')
            self.assertEqual(received[0]['secret'], 'test-only-secret')
            secret.chmod(0o644)
            with self.assertRaises(ValueError):
                w.notify_owner({}, config)


if __name__ == '__main__':
    unittest.main()
