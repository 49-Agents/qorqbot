"""invocation_context reads the active project list once, in the unit its snapshot already holds."""
from collections import OrderedDict
import contextlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from qorqut_operations.contracts import Principal

from qorqut.agents import resolver as resolver_module
from qorqut.agents.models import EffectiveAgentConfig
from qorqut.agents.resolver import AgentConfigResolver

ONE = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0001'
TWO = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0002'
THREE = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0003'
DIGEST = 'sha256:' + 'a' * 64
PRINCIPAL = Principal(id='6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0009', kind='agent')
ROLE = {'id': 'ordubashy', 'version': '1.0.0', 'digest': DIGEST, 'kind': 'role'}


class InvocationProjectsReadOnceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.connection = connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript(
            'CREATE TABLE projects (id TEXT, name TEXT, archived_at TEXT);'
            'CREATE TABLE machine_registrations (principal_id TEXT, workspace TEXT);'
            'CREATE TABLE installation (id TEXT, owner_principal_id TEXT);'
            'CREATE TABLE principals (id TEXT, display_name TEXT, active INTEGER);'
            'CREATE TABLE documents (id TEXT, project_id TEXT, namespace TEXT, metadata_json TEXT);'
            'CREATE TABLE artifacts (kind TEXT, artifact_id TEXT, version TEXT, digest TEXT, stored_path TEXT);')
        connection.executemany("INSERT INTO projects VALUES (?, 'p', NULL)", [(ONE,), (TWO,)])
        connection.execute("INSERT INTO artifacts VALUES ('role', 'ordubashy', '1.0.0', ?, ?)", (DIGEST, directory.name))
        self.reads = 0

        @contextmanager
        def read():
            self.reads += 1
            yield SimpleNamespace(connection=connection)

        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver._lock, resolver._cache = threading.RLock(), OrderedDict()
        resolver.database = SimpleNamespace(read=read)
        resolver.root = Path(directory.name)
        resolver.registry = SimpleNamespace(installed=lambda: [], frozen=False)
        resolver.check_agentic_revision = lambda revision: None
        view = SimpleNamespace(catalog_revision='rev', read_snapshot=contextlib.nullcontext)
        resolver.agentic_view, resolver.require_agentic_view = view, lambda: view
        resolver.profile = lambda principal, connection=None: (
            SimpleNamespace(data={'role': ROLE, 'plugins': []},
                            scope=SimpleNamespace(global_capabilities=('read_global_context',))),
            {'workspace': 'w', 'profile_json': '{}'})
        self.composed = 0

        def compose(snapshot, *, context_home=None):
            self.composed += 1
            return EffectiveAgentConfig({
                'scope': {'kind': 'ecosystem', 'global_capabilities': []}, 'project_id': None,
                'profile_revision': 3, 'role': ROLE, 'plugins': [], 'effective_config_id': DIGEST,
                'preparation': {}, 'delivery': None, 'context_refresh_required': False})

        resolver._compose = compose
        self.resolver = resolver
        # The project-list read, counted on its own.
        self.listed = 0
        real = resolver_module.active_project_ids

        def listed(connection):
            self.listed += 1
            return real(connection)

        patch = mock.patch.object(resolver_module, 'active_project_ids', listed)
        patch.start()
        self.addCleanup(patch.stop)

    def invoke(self):
        return self.resolver.invocation_context(PRINCIPAL)

    def test_one_call_opens_one_unit_and_reads_the_list_once(self):
        context = self.invoke()
        self.assertEqual(context.allowed_project_ids, frozenset({ONE, TWO}))
        self.assertIsInstance(context.allowed_project_ids, frozenset)
        self.assertEqual(self.reads, 1)              # the snapshot's unit only; it was two before
        self.assertEqual(self.listed, 0)             # the snapshot's own project query serves the list
        self.assertEqual(self.composed, 1)

    def test_a_cached_configuration_still_sees_the_current_projects(self):
        self.assertEqual(self.invoke().allowed_project_ids, frozenset({ONE, TWO}))
        self.connection.execute("UPDATE projects SET archived_at='2026-01-01' WHERE id=?", (TWO,))
        self.connection.execute("INSERT INTO projects VALUES (?, 'p', NULL)", (THREE,))
        # The project list is part of the cache key today, so this recomposes; the list must be the new one either way.
        self.assertEqual(self.invoke().allowed_project_ids, frozenset({ONE, THREE}))
        self.assertEqual(self.invoke().allowed_project_ids, frozenset({ONE, THREE}))
        self.assertEqual(self.composed, 2)           # the third call was a cache hit
        self.assertEqual(self.reads, 3)

    def test_a_cache_hit_takes_the_list_from_its_own_snapshot(self):
        self.invoke()
        self.assertEqual(self.composed, 1)
        # A cached configuration reached with a different list in the store: poison the thread's leftover first.
        resolver_module._SNAPSHOT_PROJECTS.seen = (self.resolver, frozenset({'stale'}))
        self.assertEqual(self.invoke().allowed_project_ids, frozenset({ONE, TWO}))
        self.assertEqual(self.composed, 1)


if __name__ == '__main__':
    unittest.main()
