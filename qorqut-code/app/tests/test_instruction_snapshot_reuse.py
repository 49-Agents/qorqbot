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
        reuse = getattr(resolver_module, 'SnapshotReuse', None)
        if reuse is not None:
            resolver._snapshot_reuse = reuse(seconds=self.seconds, clock=self.clock)
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


if __name__ == '__main__':
    unittest.main()
