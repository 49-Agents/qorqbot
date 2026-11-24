"""Agent configuration and the verified instruction snapshot, on a real initialized authoring root.

The resolver's database-facing parts are replaced by small stand-ins; the instruction layer, its lock, its state
store and the host view's snapshot are the real ones. Each test counts tree inventories, state store connections and
authoring lock acquisitions instead of timing anything.
"""
from collections import OrderedDict
import hashlib
import shutil
import tempfile
import threading
from pathlib import Path
import unittest
from unittest import mock

from qorqut_agentic import catalog_api
from qorqut_agentic.layer import AgenticLayer
from qorqut_agentic.store import Store
from qorqut_operations.contracts import DomainError, Principal

from qorqut.agentic.view import HostAgenticView
from qorqut.agents import resolver as resolver_module
from qorqut.agents.models import EffectiveAgentConfig
from qorqut.agents.resolver import AgentConfigResolver

MANIFEST = """schema_version: 1
id: demo
name: Demo package
description: Synthetic package used by the snapshot reuse test.
entries:
  - id: greeting
    kind: skill
    description: Synthetic greeting skill.
    when_to_use: Use only inside the synthetic fixture.
    path: skills/greeting/SKILL.md
"""

SKILL = """---
name: greeting
description: Synthetic greeting skill.
---
Say hello in one sentence.
"""
SKILL_PATH = 'plugins/demo/skills/greeting/SKILL.md'
ATTRIBUTION = {'actor_id': 'test', 'actor_kind': 'agent', 'reason': 'Synthetic instruction change'}
PRINCIPAL = Principal(id='6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0009', kind='agent')
DIGEST = 'sha256:' + 'a' * 64


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fixture(unittest.TestCase):
    seconds = 2.0

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='snapshot reuse '))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tree = self.root / 'authored'
        self.layer = AgenticLayer.initialize(self.tree, self.root / 'state')
        (self.tree / 'plugins' / 'demo').mkdir(parents=True)
        (self.tree / 'plugins' / 'demo' / 'agentic.yaml').write_text(MANIFEST)
        self.write_skill(SKILL)
        self.view = HostAgenticView.__new__(HostAgenticView)
        self.view._layer = self.layer
        self.clock = Clock()
        self.resolver = self.make_resolver()

    def make_resolver(self):
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver._lock, resolver._cache = threading.RLock(), OrderedDict()
        resolver.agentic_view = self.view
        resolver._snapshot_reuse = resolver_module.SnapshotReuse(seconds=self.seconds, clock=self.clock)
        resolver.composed = []

        def snapshot(principal, project_id, *, agentic_revision=None):
            revision = agentic_revision if agentic_revision is not None else resolver.require_agentic_view().catalog_revision
            return {'cache_key': 'key:' + revision, 'agentic_catalog_revision': revision, 'registration': None,
                    'project': None}

        def compose(snapshot, *, context_home=None):
            # Composition reads the tree through the pinned snapshot, as the real one does.
            body = self.view.layer.catalog.read_resource('demo', 'skills/greeting/SKILL.md')['content']
            resolver.composed.append(snapshot['agentic_catalog_revision'])
            return EffectiveAgentConfig({'revision': snapshot['agentic_catalog_revision'], 'skill': body,
                                         'preparation': {}, 'delivery': None, 'effective_config_id': DIGEST,
                                         'context_refresh_required': False})

        resolver._snapshot, resolver._compose = snapshot, compose
        resolver.preparation_status = lambda registration, data: {'ready': True}
        return resolver

    # -- fixture --------------------------------------------------------------
    def write_skill(self, body):
        path = self.tree / SKILL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def apply_skill(self, body):
        current = (self.tree / SKILL_PATH).read_bytes()
        return self.layer.authoring.apply(ATTRIBUTION, [{'op': 'edit', 'path': SKILL_PATH,
            'expected_sha256': hashlib.sha256(current).hexdigest(), 'content': body}])

    def resolve(self, **kwargs):
        return self.resolver.resolve_agent_config(PRINCIPAL, include_preparation=False, **kwargs)

    def counted(self):
        """Count tree inventories, state store connections and authoring lock acquisitions inside the block."""
        counts = {'inventories': 0, 'connections': 0, 'locks': 0}
        real_source, real_connect, real_lock = catalog_api.DiskSource, Store.connect, Store.lock

        class CountingSource(real_source):
            def __init__(self, *args, **kwargs):
                counts['inventories'] += 1
                super().__init__(*args, **kwargs)

        def connect(store, *args, **kwargs):
            counts['connections'] += 1
            return real_connect(store, *args, **kwargs)

        def lock(store, *args, **kwargs):
            counts['locks'] += 1
            return real_lock(store, *args, **kwargs)

        patches = (mock.patch.object(catalog_api, 'DiskSource', CountingSource),
                   mock.patch.object(Store, 'connect', connect), mock.patch.object(Store, 'lock', lock))

        class Counted:
            def __enter__(self_inner):
                for item in patches:
                    item.start()
                return counts

            def __exit__(self_inner, *exc):
                for item in reversed(patches):
                    item.stop()
                return False

        return Counted()


class TodayTests(Fixture):
    """Behaviour that holds with and without reuse of the verified snapshot."""

    def test_a_change_applied_through_the_layer_is_seen_by_the_next_call(self):
        first = self.resolve()
        self.apply_skill(SKILL + '\nApplied.\n')
        second = self.resolve()
        self.assertNotEqual(first.data['revision'], second.data['revision'])
        self.assertTrue(second.data['skill'].endswith('Applied.\n'))

    def test_a_cached_configuration_is_shared_by_unchanged_calls(self):
        first = self.resolve()
        second = self.resolve()
        self.assertIs(first, second)
        self.assertEqual(len(self.resolver.composed), 1)

    def test_a_commit_from_another_connection_makes_the_next_call_verify(self):
        self.resolve()
        with self.layer._ctx.store.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('snapshot_reuse_test', '1')")
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)
        self.assertGreaterEqual(counts['locks'], 1)

    def test_preparation_and_session_registration_always_verify(self):
        self.resolve()
        with self.counted() as counts:
            config = self.resolver.resolve_agent_config(PRINCIPAL)
        self.assertEqual(config.data['preparation'], {'ready': True})
        self.assertGreaterEqual(counts['inventories'], 2)
        with self.counted() as counts, self.assertRaises(DomainError) as caught:
            self.resolver.resolve_agent_config(PRINCIPAL, include_preparation=False, session_registration='reg-1')
        self.assertEqual(caught.exception.code, 'registration_conflict')
        self.assertGreaterEqual(counts['inventories'], 1)

    def test_an_explicit_revision_always_verifies(self):
        revision = self.resolve().data['revision']
        with self.counted() as counts:
            self.assertEqual(self.view.consistent(lambda: self.view.catalog_revision, revision=revision), revision)
        self.assertGreaterEqual(counts['inventories'], 2)


class TodayWithoutReuseTests(TodayTests):
    """With reuse switched off, a hand edit and an invalid tree are seen by the very next call."""
    seconds = 0

    def test_a_hand_edit_is_seen_by_the_next_call(self):
        self.resolve()
        self.write_skill(SKILL + '\nBy hand.\n')
        self.assertTrue(self.resolve().data['skill'].endswith('By hand.\n'))

    def test_invalid_instructions_fail_the_next_call(self):
        self.resolve()
        (self.tree / 'plugins' / 'demo' / 'agentic.yaml').write_text('schema_version: [unclosed\n')
        with self.assertRaises(DomainError):
            self.resolve()

    def test_every_call_verifies(self):
        self.resolve()
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 2)
        self.assertEqual(counts['locks'], 1)


class ReuseTests(Fixture):
    """A call that finds nothing changed since the last full verification reuses it, for at most ``seconds``."""

    def test_a_reused_call_builds_no_inventory_opens_no_connection_and_takes_no_lock(self):
        first = self.resolve()
        with self.counted() as counts:
            second = self.resolve()
            third = self.resolve()
        self.assertIs(first, second)
        self.assertIs(first, third)
        self.assertEqual(counts, {'inventories': 0, 'connections': 0, 'locks': 0})
        self.assertEqual(len(self.resolver.composed), 1)

    def test_a_change_applied_in_this_process_is_seen_with_no_time_passing(self):
        self.resolve()
        self.apply_skill(SKILL + '\nApplied.\n')
        with self.counted() as counts:
            config = self.resolve()
        self.assertTrue(config.data['skill'].endswith('Applied.\n'))
        self.assertGreaterEqual(counts['inventories'], 2)

    def test_a_commit_from_another_process_is_seen_with_no_time_passing(self):
        """A commit without this process's lock (as another process makes it) moves only the state store."""
        self.resolve()
        other = Store(self.root / 'state')
        with other.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('snapshot_reuse_test', '2')")
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)

    def test_any_exclusive_release_ends_the_reuse_even_when_its_body_raised(self):
        self.resolve()
        with self.assertRaises(RuntimeError):
            with self.layer._ctx.store.lock(exclusive=True):
                raise RuntimeError('body failed')
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 0)

    def test_a_scan_that_cannot_take_the_lock_does_not_end_the_reuse(self):
        self.resolve()
        with self.layer._ctx.store.lock(exclusive=False):
            done = []
            thread = threading.Thread(target=lambda: done.append(self.attempt_scan()))
            thread.start()
            thread.join(10)
        self.assertEqual(done, ['lock_timeout'])
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 0)

    def attempt_scan(self):
        try:
            self.layer.changes.scan(lock_timeout=0)
        except Exception as exc:
            return getattr(exc, 'code', type(exc).__name__)
        return 'scanned'

    def test_a_hand_edit_is_seen_only_once_the_period_has_passed(self):
        first = self.resolve()
        self.write_skill(SKILL + '\nBy hand.\n')
        self.clock.now += 1.9
        self.assertIs(self.resolve(), first)
        self.clock.now += 0.1
        self.assertTrue(self.resolve().data['skill'].endswith('By hand.\n'))

    def test_invalid_instructions_are_never_served_after_the_period(self):
        self.resolve()
        (self.tree / 'plugins' / 'demo' / 'agentic.yaml').write_text('schema_version: [unclosed\n')
        self.clock.now += 2.0
        for _ in range(2):
            with self.assertRaises(DomainError):
                self.resolve()

    def test_a_clock_that_moves_back_ends_the_reuse(self):
        self.resolve()
        self.clock.now -= 1
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)

    def test_an_unreadable_data_version_means_changed(self):
        self.resolve()
        with mock.patch.object(Store, 'data_version', return_value=None), self.counted() as counts:
            self.resolve()
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 4)

    def test_a_failed_version_connection_is_replaced_and_never_compared_across_connections(self):
        store = self.layer._ctx.store
        self.resolve()
        before = store.data_version()
        store._version_conn.close()  # the next read fails on the closed connection
        self.assertIsNone(store.data_version())
        after = store.data_version()
        self.assertNotEqual(before[0], after[0])
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 0)

    def test_the_version_connection_reads_from_any_thread(self):
        store = self.layer._ctx.store
        here = store.data_version()
        found = []
        thread = threading.Thread(target=lambda: found.append(store.data_version()))
        thread.start()
        thread.join(10)
        self.assertEqual(found, [here])

    def test_a_configuration_not_yet_composed_is_never_served_from_the_record(self):
        self.resolve()
        self.resolver._cache.clear()
        with self.counted() as counts:
            self.resolve()
        self.assertGreaterEqual(counts['inventories'], 2)
        self.assertEqual(len(self.resolver.composed), 2)

    def test_preparation_session_registration_and_explicit_revision_never_reuse(self):
        self.resolve()
        with self.counted() as counts:
            self.resolver.resolve_agent_config(PRINCIPAL)
        self.assertGreaterEqual(counts['inventories'], 2)
        with self.counted() as counts, self.assertRaises(DomainError):
            self.resolver.resolve_agent_config(PRINCIPAL, include_preparation=False, session_registration='reg-1')
        self.assertGreaterEqual(counts['inventories'], 1)
        revision = self.resolve().data['revision']
        with self.counted() as counts:
            self.view.consistent(lambda: None, revision=revision)
        self.assertGreaterEqual(counts['inventories'], 2)

    def test_a_call_inside_a_callers_snapshot_neither_reuses_nor_records(self):
        self.resolve()
        reuse = self.resolver._snapshot_reuse
        recorded = reuse._record
        self.clock.now += 1
        with self.view.read_snapshot():
            with mock.patch.object(reuse, 'current', side_effect=AssertionError('reused inside a snapshot')):
                self.resolve()
        self.assertIs(reuse._record, recorded)

    def test_racing_verifications_leave_one_consistent_record(self):
        reuse = self.resolver._snapshot_reuse
        barrier = threading.Barrier(2)
        real_observe = reuse.observe

        def observe(view):
            observed = real_observe(view)
            barrier.wait(10)  # both threads are inside their snapshot before either records
            return observed

        errors = []

        def run():
            try:
                self.resolve()
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(reuse, 'observe', observe):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(errors, [])
        revision, marker, at = reuse._record
        self.assertEqual(revision, self.view.layer.catalog.revision()['catalog_revision'])
        self.assertEqual(marker, self.view.change_marker())
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 0)

    def test_an_older_verification_never_replaces_a_newer_record(self):
        reuse = self.resolver._snapshot_reuse
        reuse.remember('newer', (('marker',), 10.0))
        reuse.remember('older', (('marker',), 5.0))
        self.assertEqual(reuse._record, ('newer', ('marker',), 10.0))


class ReuseOffTests(Fixture):
    seconds = 0

    def test_a_period_of_zero_never_reuses_or_records(self):
        self.resolve()
        self.assertIsNone(self.resolver._snapshot_reuse._record)
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 2)

    def test_a_resolver_built_by_hand_without_the_record_never_reuses(self):
        del self.resolver._snapshot_reuse
        self.resolve()
        with self.counted() as counts:
            self.resolve()
        self.assertEqual(counts['inventories'], 2)


if __name__ == '__main__':
    unittest.main()
