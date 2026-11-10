"""Per-tool-call work is remembered only while its exact inputs are unchanged, and results stay identical.

Every cache here is checked two ways: an unchanged input is not recomputed, and each kind of change a cache key
must notice produces the same value an uncached computation gives.
"""
import asyncio
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_agentic import files as agentic_files
from qorqut_agentic.files import FileEntry, SafeTree

from qorqut.agents import resolver as resolver_module
from qorqut.agents.attention import Attention
from qorqut.agents.models import EffectiveAgentConfig
from qorqut.agents.resolver import AgentConfigResolver
from qorqut.catalog.packages import Catalog
from qorqut.composition.attention import SCHEMA
from qorqut.transport import mcp

DIGEST = 'sha256:' + 'a' * 64
REAL_TIME_NS = time.time_ns


def reference_inventory(tree):
    """The original inventory: walk, then open and hash every regular file."""
    files, special = {}, {}
    for rel, kind in tree.walk(""):
        if kind == "file":
            entry = tree.digest(rel)
            if entry is not None:
                files[rel] = entry
        else:
            special[rel] = kind
    return files, special


class DigestCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "roles" / "deep").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("ignored")
        (self.root / "roles" / "a.md").write_text("alpha")
        (self.root / "roles" / "deep" / "b.md").write_text("beta")
        (self.root / "empty.md").write_text("")
        (self.root / (agentic_files.TMP_PREFIX + "x")).write_text("ignored")
        os.symlink("roles/a.md", self.root / "link.md")
        self.tree = SafeTree(self.root)
        self.hashed = []
        real = agentic_files.sha256_hex
        patcher = mock.patch.object(agentic_files, "sha256_hex", lambda data: self.hashed.append(data) or real(data))
        patcher.start()
        self.addCleanup(patcher.stop)

    def settled(self):
        """Run with a clock far enough ahead that every file written so far counts as settled.

        The real margin guarantees that any later change lands in a later kernel timestamp tick than the one the
        hashed bytes were stamped with. A test that skips the wait must keep that true itself: ``later_tick``."""
        return mock.patch.object(agentic_files.time, "time_ns", lambda: REAL_TIME_NS() + 10 * agentic_files.DIGEST_SETTLE_NS)

    def later_tick(self, path):
        """Wait (a few milliseconds) until the kernel stamps new changes later than ``path``'s last change."""
        stamped, probe = os.lstat(path).st_ctime_ns, self.root / (agentic_files.TMP_PREFIX + "tick")
        for _ in range(2000):
            probe.write_text("")
            if probe.stat().st_ctime_ns > stamped:
                return
            time.sleep(0.001)
        self.fail("the filesystem clock did not advance")

    def inventory(self):
        result = self.tree.inventory()
        self.assertEqual(result, reference_inventory(SafeTree(self.root)))
        return result

    def test_inventory_matches_the_uncached_walk(self):
        files, special = self.inventory()
        self.assertEqual(set(files), {"roles/a.md", "roles/deep/b.md", "empty.md"})
        self.assertEqual(files["roles/a.md"], FileEntry(hashlib.sha256(b"alpha").hexdigest(), 5))
        self.assertEqual(special, {"link.md": "symlink"})
        self.assertEqual(self.tree.inventory("roles")[0].keys(), {"roles/a.md", "roles/deep/b.md"})
        self.assertEqual(self.tree.inventory("missing"), ({}, {}))

    def test_a_settled_file_is_hashed_once(self):
        with self.settled():
            first = self.tree.inventory()
            self.hashed.clear()
            self.assertEqual(self.tree.inventory(), first)
            self.assertEqual(self.tree.inventory("roles")[0], {k: v for k, v in first[0].items() if k.startswith("roles/")})
        self.assertEqual(self.hashed, [])

    def test_a_fresh_file_is_hashed_until_it_settles(self):
        self.tree.inventory()
        self.hashed.clear()
        self.tree.inventory()
        self.assertEqual(sorted(self.hashed), [b"", b"alpha", b"beta"])
        self.assertEqual(self.tree._digests, {})

    def test_every_kind_of_change_is_noticed(self):
        target = self.root / "roles" / "a.md"
        with self.settled():
            self.tree.inventory()
            before = target.stat()
            # Same size and restored mtime: only the change time and the bytes differ.
            self.later_tick(target)
            target.write_text("omega")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.inventory()[0]["roles/a.md"].sha256, hashlib.sha256(b"omega").hexdigest())
            # Atomic replacement by another file of the same size.
            staged = self.root / "roles" / "staged"
            staged.write_text("delta")
            os.replace(staged, target)
            self.assertEqual(self.inventory()[0]["roles/a.md"].sha256, hashlib.sha256(b"delta").hexdigest())
            # Growth, removal, and a link taking the file's place.
            target.write_text("longer text")
            self.assertEqual(self.inventory()[0]["roles/a.md"].size, 11)
            target.unlink()
            self.assertNotIn("roles/a.md", self.inventory()[0])
            os.symlink("deep/b.md", target)
            files, special = self.inventory()
            self.assertNotIn("roles/a.md", files)
            self.assertEqual(special["roles/a.md"], "symlink")

    def test_bytes_that_changed_while_being_read_are_not_remembered(self):
        target = self.root / "roles" / "a.md"
        real_read = os.read
        def racing_read(fd, count):
            data = real_read(fd, count)
            if data == b"alpha":
                target.write_text("ALPHA and more")
            return data
        with self.settled(), mock.patch.object(agentic_files.os, "read", racing_read):
            self.tree.inventory()
        self.assertNotIn("roles/a.md", self.tree._digests)
        with self.settled():
            self.assertEqual(self.inventory()[0]["roles/a.md"].size, 14)

    def test_the_cache_is_bounded(self):
        with self.settled(), mock.patch.object(agentic_files, "DIGEST_CACHE_LIMIT", 2):
            first = self.tree.inventory()
            self.assertEqual(self.tree._digests, {})
            self.assertEqual(self.tree.inventory(), first)


class TreeSignatureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "store"
        (self.root / "skills" / "one").mkdir(parents=True)
        (self.root / "manifest.json").write_text("{}")
        (self.root / "skills" / "one" / "SKILL.md").write_text("skill")
        (self.root / "skills-extra").mkdir()
        (self.root / "skills-extra" / "z").write_text("z")

    def test_artifact_signature_equals_the_pathlib_walk(self):
        legacy = resolver_module._legacy_tree_signature(self.root)
        self.assertEqual(resolver_module._tree_signature(self.root), legacy)
        self.assertEqual([name for name, _ in legacy],
                         ["manifest.json", "skills", "skills/one", "skills/one/SKILL.md", "skills-extra", "skills-extra/z"])
        hash(resolver_module._tree_signature(self.root))
        (self.root / "skills" / "one" / "SKILL.md").write_text("changed skill")
        self.assertNotEqual(resolver_module._tree_signature(self.root), legacy)
        self.assertEqual(resolver_module._tree_signature(self.root), resolver_module._legacy_tree_signature(self.root))

    def test_unusual_trees_keep_the_original_walk(self):
        os.symlink("manifest.json", self.root / "alias")
        with mock.patch.object(resolver_module, "_legacy_tree_signature", return_value=("legacy",)) as legacy:
            self.assertEqual(resolver_module._tree_signature(self.root), ("legacy",))
            self.assertEqual(resolver_module._tree_signature(self.root / "absent"), ("legacy",))
        self.assertEqual(legacy.call_count, 2)

    def test_catalog_signature_equals_the_walk_it_replaced(self):
        os.symlink("manifest.json", self.root / "alias")
        alias = Path(self.directory.name) / "alias-root"
        os.symlink(self.root, alias)

        def reference(root):
            root = root.resolve(strict=True)
            entries = []
            for directory, names, found in os.walk(root, followlinks=False):
                for name in (*names, *found):
                    path = Path(directory) / name
                    info = path.lstat()
                    entries.append((path.relative_to(root).as_posix(), path.is_symlink(),
                                    info.st_size, info.st_mtime_ns, info.st_ino, info.st_mode))
            return tuple(sorted(entries))

        self.assertEqual(Catalog._signature(self.root), reference(self.root))
        self.assertEqual(Catalog._signature(alias), reference(self.root))
        self.assertEqual(len(Catalog._signature(self.root)), 7)
        self.assertIsNone(Catalog._signature(self.root / "absent"))


class ResolverReuseTests(unittest.TestCase):
    def resolver(self):
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver._lock, resolver._cache = threading.RLock(), OrderedDict()
        resolver._fingerprints, resolver._fingerprints_registry = OrderedDict(), None
        resolver.registry = NS(frozen=True)
        resolver.checked = []
        resolver.check_agentic_revision = resolver.checked.append
        return resolver

    def test_hashes_are_computed_once_per_exact_input_and_registry(self):
        resolver = self.resolver()
        calls = []
        def compute(value=('f', 'k')):
            calls.append(value)
            return value
        self.assertEqual(resolver._remembered_hashes(('a',), compute), ('f', 'k'))
        self.assertEqual(resolver._remembered_hashes(('a',), lambda: self.fail('recomputed')), ('f', 'k'))
        self.assertEqual(resolver._remembered_hashes(('b',), lambda: compute(('g', 'l'))), ('g', 'l'))
        self.assertEqual(resolver._remembered_hashes(None, compute), ('f', 'k'))
        self.assertEqual(resolver._remembered_hashes(None, compute), ('f', 'k'))
        self.assertEqual(len(calls), 4)
        resolver.registry = NS(frozen=True)
        self.assertEqual(resolver._remembered_hashes(('a',), lambda: compute(('h', 'm'))), ('h', 'm'))
        self.assertEqual(list(resolver._fingerprints), [('a',)])

    def test_a_registry_replaced_during_the_computation_is_not_remembered(self):
        resolver = self.resolver()
        def compute():
            resolver.registry = NS(frozen=True)
            return ('f', 'k')
        resolver._remembered_hashes(('a',), compute)
        self.assertEqual(resolver._fingerprints, {})

    def test_the_memo_is_bounded_and_keeps_recent_inputs(self):
        resolver = self.resolver()
        with mock.patch.object(resolver_module, 'FINGERPRINT_CACHE_LIMIT', 2):
            for key in ('a', 'b', 'a', 'c'):
                resolver._remembered_hashes((key,), lambda key=key: (key, key))
        self.assertEqual(list(resolver._fingerprints), [('a',), ('c',)])

    def test_ordinary_resolution_shares_the_composed_configuration(self):
        resolver = self.resolver()
        composed = []
        data = {'scope': {'kind': 'ecosystem', 'business_id': None, 'allowed_business_ids': [], 'global_capabilities': []},
                'business_id': None, 'profile_revision': 3, 'plugins': [],
                'role': {'id': 'ordubashy', 'version': '1.0.0', 'digest': DIGEST, 'kind': 'role'},
                'effective_config_id': DIGEST, 'preparation': {}, 'delivery': None, 'context_refresh_required': False}
        resolver._snapshot = lambda principal, business_id: {
            'cache_key': DIGEST, 'registration': {'id': 'reg-1'}, 'agentic_catalog_revision': 'rev-1'}
        resolver._compose = lambda snapshot, *, context_home=None: composed.append(1) or EffectiveAgentConfig(data)
        resolver.preparation_status = lambda registration, data: {'ready': True, 'status': 'prepared'}
        first = resolver._resolve_agent_config(object(), include_preparation=False)
        checked = len(resolver.checked)
        second = resolver._resolve_agent_config(object(), include_preparation=False)
        self.assertIs(first, second)
        self.assertEqual(first.to_dict(), data)
        self.assertEqual(len(composed), 1)
        # The revision is still checked on every resolution, and the diagnostic projection is still its own value.
        self.assertEqual(resolver.checked, ['rev-1'] * (checked + 1))
        public = resolver._resolve_agent_config(object())
        self.assertIsNot(public, first)
        self.assertEqual(public.to_dict()['preparation'], {'ready': True, 'status': 'prepared'})
        self.assertEqual(first.to_dict()['preparation'], {})
        with self.assertRaises(TypeError):
            first.data['preparation']['ready'] = True


class ClockWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        for statement in SCHEMA:
            self.conn.execute(statement)
        self.attention = Attention(NS(database=None, application=NS(), focus=NS(inbox=NS())))
        self.uow = NS(connection=self.conn)

    def row(self):
        return dict(self.conn.execute("SELECT * FROM agent_attention WHERE agent_id='agent'").fetchone())

    def test_only_a_real_difference_is_written(self):
        update = lambda **changes: self.attention.update_clock(self.uow, 'agent', changes)
        update(board_due_at='2026-11-09T12:00:00.000000Z', reminder_sequence=1)
        stored, writes = self.row(), self.conn.total_changes
        update(board_due_at='2026-11-09T12:00:00.000000Z', reminder_sequence=1)
        update(reminder_sequence=1)
        self.assertEqual(self.conn.total_changes, writes)
        self.assertEqual(self.row(), stored)
        update(board_due_at='2026-11-09T12:00:00.000000Z', reminder_sequence=2)
        self.assertEqual(self.conn.total_changes, writes + 1)
        self.assertEqual(self.row()['reminder_sequence'], 2)
        update(board_due_at=None)
        self.assertIsNone(self.row()['board_due_at'])
        update(board_due_at=None)
        self.assertEqual(self.conn.total_changes, writes + 2)
        # A value of another type is never treated as already stored.
        update(reminder_sequence='2')
        self.assertEqual(self.conn.total_changes, writes + 3)
        with self.assertRaises(ValueError):
            update(focus_json='{}')


class TransportTests(unittest.TestCase):
    def transport(self, authority):
        runtime = NS(registry=NS(frozen=True), resolver=NS(), require_authority=authority)
        return mcp.MCPTransport(runtime, None, None, caller=NS())

    def test_a_tool_call_never_runs_discovery_for_the_sdk_definition_cache(self):
        transport = self.transport(lambda: None)
        listed = []
        async def list_tools(request):
            listed.append(request)
        transport.server.request_handlers[mcp.types.ListToolsRequest] = list_tools
        self.assertIsNone(asyncio.run(transport.server._get_cached_tool_definition('tasks_list')))
        self.assertEqual(listed, [])

    def test_the_actor_lookup_can_leave_the_authority_check_to_its_worker(self):
        checks = []
        transport = self.transport(lambda: checks.append(threading.current_thread()))
        actor = object()
        context = NS(request=NS(scope={'qorqut.auth': actor}))
        with mock.patch.object(type(transport.server), 'request_context', new_callable=mock.PropertyMock,
                               return_value=context):
            self.assertIs(transport._request_actor(authority=False), actor)
            self.assertEqual(checks, [])
            self.assertIs(transport._request_actor(), actor)
            self.assertEqual(len(checks), 1)

    def test_batch_authorization_checks_authority_off_the_event_loop(self):
        checks = []
        actor = NS(name='actor')
        caller = NS(authenticate=lambda authorization: actor, identity=lambda value: value.name,
                    check_batch=lambda value: value)
        transport = self.transport(lambda: checks.append(threading.current_thread()))
        transport.caller = caller
        context = NS(request=NS(scope={'qorqut.auth': actor}, headers={'authorization': 'Bearer x'}))
        loop_thread = []
        async def run():
            loop_thread.append(threading.current_thread())
            return await transport._authorize_batch()
        with mock.patch.object(type(transport.server), 'request_context', new_callable=mock.PropertyMock,
                               return_value=context):
            self.assertIs(asyncio.run(run()), actor)
        self.assertEqual(len(checks), 1)
        self.assertIsNot(checks[0], loop_thread[0])


class SingleCallAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_mcp_phase_timing as support
        self.support = support
        clock, delays = support.Clock(), {'sample_read': {}}
        self.caller = support.Caller(clock, delays)
        self.application = support.Application(clock, delays, {})
        self.checks, self.failure = [], None
        def require_authority():
            self.checks.append((threading.current_thread(), list(self.caller.configured)))
            if self.failure is not None:
                raise self.failure
        runtime = NS(registry=support.Registry(clock, delays, {}), application=self.application,
                     require_authority=require_authority)
        self.transport = mcp.MCPTransport(runtime, None, None, caller=self.caller)
        context = NS(request=NS(scope={'qorqut.auth': support.ACTOR}))
        for patcher in (mock.patch.object(type(self.transport.server), 'request_context',
                                          new_callable=mock.PropertyMock, return_value=context),
                        mock.patch.object(mcp, 'time', NS(perf_counter=clock))):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_single_call_passes_the_gate_once_on_its_worker_before_configuration(self):
        result = await self.transport._single_result('sample_read', {})
        self.assertEqual(result['status'], 'ok')
        (thread, configured), = self.checks
        self.assertIsNot(thread, threading.current_thread())
        self.assertEqual(configured, [])
        self.assertEqual(self.application.invoked, ['sample_read'])

    async def test_a_closed_gate_refuses_the_call_before_any_configuration(self):
        from qorqut_operations.contracts import DomainError
        self.failure = DomainError('maintenance', 'Operational access is paused for maintenance', retryable=True)
        result = await self.transport._single_result('sample_read', {})
        self.assertEqual((result['status'], result['error']['code']), ('error', 'maintenance'))
        self.assertEqual((self.caller.configured, self.application.invoked), ([], []))

    async def test_a_batch_entry_relies_on_its_batch_authorization(self):
        result = await self.transport._single_result('sample_read', {}, self.support.ACTOR)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.checks, [])


class ImportCostTests(unittest.TestCase):
    """The activity hook is a new process on every native tool call; it must not load what it never uses."""

    def run_python(self, code):
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120,
                                env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_hook_modules_leave_the_engine_and_authoring_layers_unloaded(self):
        loaded = self.run_python(
            'import sys\n'
            'import qorqut.bootstrap.activity_hook, qorqut.bootstrap.activity_context, qorqut.bootstrap.client\n'
            'heavy = ("qorqut_agentic", "jsonschema", "yaml", "sqlite3", "urllib.request", "http.client",\n'
            '         "qorqut_operations.storage.database", "qorqut_operations.storage.assembly")\n'
            'print(*[name for name in heavy if name in sys.modules])\n')
        self.assertEqual(loaded, [])

    def test_storage_exports_resolve_on_first_use(self):
        missing = self.run_python(
            'import sys\n'
            'import qorqut_operations.storage.safe_io\n'
            'import qorqut_operations.storage as storage\n'
            'assert "qorqut_operations.storage.database" not in sys.modules\n'
            'assert sorted(storage.__all__) == sorted(set(storage.__all__))\n'
            'assert set(storage.__all__) <= set(dir(storage))\n'
            'print(*[name for name in storage.__all__ if getattr(storage, name, None) is None])\n'
            'from qorqut_operations.storage import Database, DIRECTORY_ACTION\n'
            'from qorqut_operations.storage import directory_layout\n'
            'assert DIRECTORY_ACTION is directory_layout.ACTION\n'
            'assert storage.Database is sys.modules["qorqut_operations.storage.database"].Database\n'
            'try:\n'
            '    storage.no_such_export\n'
            'except AttributeError:\n'
            '    pass\n'
            'else:\n'
            '    raise SystemExit("unknown export resolved")\n')
        self.assertEqual(missing, [])

    def test_context_delivery_still_fails_to_import_without_the_instruction_package(self):
        self.run_python(
            'import importlib.util\n'
            'real = importlib.util.find_spec\n'
            'importlib.util.find_spec = lambda name, *a, **k: None if name == "qorqut_agentic" else real(name, *a, **k)\n'
            'try:\n'
            '    import qorqut.bootstrap.activity_context\n'
            'except ImportError:\n'
            '    pass\n'
            'else:\n'
            '    raise SystemExit("imported without the instruction package")\n')


if __name__ == '__main__':
    unittest.main()
