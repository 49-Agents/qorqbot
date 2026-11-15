"""Remembered re-derivation of retained managed updates (qorqut.agentic.managed_verified).

Run from the repository root with the source directories on PYTHONPATH:
    python -m unittest discover -s qorqut-code/app/tests -p test_managed_history_cache.py

The full per-update proof (``_load_node``) and the current-state checks are replaced by recorders, so these tests
cover only when the full proof runs, when it is skipped, and that every current-state check still runs on a skip.
Real retained files, journals, digests and the cache file are used.
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

from qorqut.agentic import managed_ancestry as ancestry
from qorqut.agentic import managed_feed, managed_journal, managed_verified
from qorqut.agentic.layout_intent import _conflict
from qorqut.agentic.support import canonical, digest
from qorqut.agentic.upgrade import history_directory, history_snapshot
from qorqut_operations.contracts import DomainError

INSTANCE = 'inst_' + '1' * 32
IDENTITY = {'root': {'device': 1, 'inode': 2}, 'state': {'device': 1, 'inode': 3}}


class FakeConnection:
    def __init__(self, meta):
        self.meta = meta

    def execute(self, sql, *args):
        return list(self.meta.items()) if 'FROM meta' in sql else []


class FakeStore:
    meta = {}

    def __init__(self, state):
        self.state_dir = state

    @contextlib.contextmanager
    def reader(self):
        yield FakeConnection(self.meta)


class Chain(unittest.TestCase):
    """An installed binding with two retained managed updates on top of a base binding."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        self.paths = types.SimpleNamespace(instance_root=self.base / 'instance', agentic_root=self.base / 'agentic',
                                           agentic_runtime=self.base / 'runtime' / 'agentic', runtime=self.base / 'runtime')
        for directory in (self.paths.agentic_root, self.paths.agentic_runtime):
            directory.mkdir(parents=True)
        self.history = history_directory(self.paths)
        self.history.mkdir(parents=True, mode=0o700)
        os.chmod(self.history, 0o700)
        common = {'instance_id': INSTANCE, 'root': str(self.paths.agentic_root), 'state': str(self.paths.agentic_runtime),
                  'initial_commit': 'c' * 40, 'selected_extensions': []}
        self.records, self.raws = [], []
        record = {**common, 'payload': 'base'}
        self.records.append(record)
        self.raws.append(canonical(record) + b'\n')
        self.digests = []
        for number, changes in ((1, True), (2, False)):
            pd = str(number) * 64
            prior_raw = self.raws[-1]
            self.write(history_snapshot(self.paths, digest(prior_raw)), prior_raw)
            metadata = {'format_version': 1, 'plan_digest': pd, 'prior_binding_sha256': digest(prior_raw),
                        'intent_sha256': 'e' * 63 + str(number), 'resulting_commit': str(number) * 40}
            record = {**common, 'payload': 'update ' + str(number), 'managed_upgrade': metadata}
            self.records.append(record)
            self.raws.append(canonical(record) + b'\n')
            self.digests.append(pd)
            self.write(self.history / ('managed-intent-' + pd + '.json'), b'{"intent":' + str(number).encode() + b'}\n')
            self.write(self.history / ('managed-journal-' + pd + '.json'), self.journal(metadata, changes))
        self.changes = {self.digests[0]: True, self.digests[1]: False}
        FakeStore.meta = {'schema_version': '3', 'root_path': str(self.paths.agentic_root), 'instance_id': INSTANCE,
                          'initialized_at': 'now'}
        self.full = mock.Mock(side_effect=self.full_proof)
        self.based = []
        self.recorders = {}
        for owner, name, value in (
                (ancestry, '_load_node', self.full), (ancestry, 'Store', FakeStore),
                (ancestry, '_verify_layout', mock.Mock()), (ancestry, '_installed_selection', mock.Mock()),
                (ancestry, '_check_identities', mock.Mock()), (ancestry, '_check_ancestor', mock.Mock()),
                (ancestry, '_validate_completed_record', mock.Mock(side_effect=lambda paths, record, raw, **kw: self.based.append((record, raw)))),
                (ancestry, '_validate_binding', mock.Mock()),
                (managed_feed, 'verify_recorded_event', mock.Mock())):
            patcher = mock.patch.object(owner, name, value)
            self.recorders[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, data):
        path.write_bytes(data)
        os.chmod(path, 0o600)

    def journal(self, metadata, changes):
        value = {'schema_version': 1, 'phase': 'complete', 'plan_digest': metadata['plan_digest'],
                 'intent_sha256': metadata['intent_sha256'], 'attribution': {},
                 'prior_binding_sha256': metadata['prior_binding_sha256'],
                 'root': {'device': 1, 'inode': 2, 'mode': 0o755}, 'gitdir': {'device': 1, 'inode': 4, 'mode': 0o755},
                 'head_identity': '', 'initial_dirs': {}, 'created_dirs': {},
                 'git': {'commit': metadata['resulting_commit']} if changes else None,
                 'index_publication': {} if changes else None, 'provisioning': {}, 'prior_binding': {},
                 'feed': {'change_id': 'chg_' + metadata['plan_digest'][:32], 'sequence': 1} if changes else None,
                 'candidate_sha256': 'f' * 64, 'result': {}}
        value['receipt_sha256'] = digest(value)
        return canonical(value) + b'\n'

    def full_proof(self, paths, record, evidence, *, offline_read_only=False, _read=None):
        """Stands in for the complete proof: returns what it would for these exact records."""
        index = self.records.index(record)
        pd = record['managed_upgrade']['plan_digest']
        prior_raw = self.raws[index - 1]
        intent = {'plan': {'plan_digest': pd, 'identity': IDENTITY, 'changes': [{}] if self.changes[pd] else []},
                  'receipt_sha256': record['managed_upgrade']['intent_sha256']}
        journal = managed_journal.read(Path(evidence[pd]['journal']))
        return prior_raw, intent, journal, self.raws[index]

    def verify(self, offline=False):
        self.based.clear()
        record = json.loads(self.raws[-1])
        if offline:
            with mock.patch('qorqut.agentic.layout_runtime._offline_reader', lambda store: FakeStore(None).reader()):
                return ancestry.verify(self.paths, record, self.raws[-1], layout_intents=[], offline_read_only=True)
        return ancestry.verify(self.paths, record, self.raws[-1], layout_intents=[])

    def cache_file(self):
        return managed_verified.location(self.paths)


class RememberedProofTests(Chain):
    def test_first_check_proves_every_update_and_remembers_it(self):
        self.assertEqual(self.verify(), set(self.digests))
        self.assertEqual(self.full.call_count, 2)
        self.assertEqual(len(managed_verified.load(self.paths)), 2)
        self.assertEqual(oct(os.stat(self.cache_file()).st_mode & 0o777), '0o600')
        self.assertEqual(oct(os.stat(self.cache_file().parent).st_mode & 0o777), '0o700')

    def test_repeat_check_skips_rederivation_but_repeats_every_current_state_check(self):
        self.verify()
        for name in ('_check_identities', '_check_ancestor', 'verify_recorded_event'):
            self.recorders[name].reset_mock()
        self.full.reset_mock()
        self.assertEqual(self.verify(), set(self.digests))
        self.full.assert_not_called()
        self.assertEqual(self.recorders['_check_identities'].call_count, 2)
        self.assertEqual([c.args[1] for c in self.recorders['_check_ancestor'].call_args_list], ['2' * 40, '1' * 40])
        # Only the update with file changes has a feed event, checked against its journal's recorded event.
        self.recorders['verify_recorded_event'].assert_called_once()
        self.assertEqual(self.recorders['verify_recorded_event'].call_args.args[1]['change_id'], 'chg_' + '1' * 32)

    def test_base_binding_is_validated_whole_after_a_remembered_walk(self):
        self.verify()
        self.verify()
        record, raw = self.based[-1]
        self.assertEqual(record, self.records[0])
        self.assertEqual(raw, self.raws[0])

    def test_changed_intent_bytes_are_proven_again(self):
        self.verify()
        self.full.reset_mock()
        self.write(self.history / ('managed-intent-' + self.digests[0] + '.json'), b'{"intent":"changed"}\n')
        self.verify()
        self.assertEqual(self.full.call_count, 1)
        self.assertEqual(self.full.call_args.args[1], self.records[1])

    def test_changed_journal_bytes_are_proven_again(self):
        self.verify()
        self.full.reset_mock()
        path = self.history / ('managed-journal-' + self.digests[1] + '.json')
        value = json.loads(path.read_bytes())
        value['attribution'] = {'changed': True}
        value['receipt_sha256'] = digest({k: v for k, v in value.items() if k != 'receipt_sha256'})
        self.write(path, canonical(value) + b'\n')
        self.verify()
        self.assertEqual(self.full.call_count, 1)
        self.assertEqual(self.full.call_args.args[1], self.records[2])

    def test_changed_prior_binding_is_refused_before_any_cache_use(self):
        self.verify()
        self.write(history_snapshot(self.paths, self.records[2]['managed_upgrade']['prior_binding_sha256']), b'{}\n')
        with self.assertRaises(DomainError):
            self.verify()

    def test_failed_current_state_check_runs_the_full_proof_which_decides(self):
        self.verify()
        self.full.reset_mock()
        self.recorders['_check_ancestor'].side_effect = _conflict('Managed completion commit is not in current Git ancestry')
        self.full.side_effect = _conflict('Managed completion commit is not in current Git ancestry')
        with self.assertRaises(DomainError):
            self.verify()
        self.assertEqual(self.full.call_count, 1)

    def test_full_proof_replaces_an_entry_that_no_longer_matches(self):
        self.verify()
        key, entry = next(iter(managed_verified.load(self.paths).items()))
        stale = {**entry, 'receipt_sha256': '0' * 64}
        self.assertTrue(managed_verified.remember(self.paths, key, stale))
        self.full.reset_mock()
        self.verify()
        self.assertEqual(self.full.call_count, 1)
        self.assertEqual(managed_verified.load(self.paths)[key], entry)

    def test_other_code_does_not_reuse_entries(self):
        self.verify()
        self.full.reset_mock()
        with mock.patch.object(managed_verified, 'code_identity', return_value='0' * 64):
            self.verify()
        self.assertEqual(self.full.call_count, 2)

    def test_offline_checks_neither_read_nor_write_the_cache(self):
        self.verify(offline=True)
        self.assertFalse(os.path.lexists(self.cache_file()))
        self.verify()
        self.full.reset_mock()
        self.verify(offline=True)
        self.assertEqual(self.full.call_count, 2)

    def test_unusable_cache_files_are_ignored(self):
        self.verify()
        for data in (b'not json', b'[]', json.dumps({'format': 99, 'entries': {}}).encode(),
                     json.dumps({'format': 1, 'entries': {'k': {}}}).encode()):
            self.write(self.cache_file(), data)
            self.full.reset_mock()
            self.verify()
            self.assertEqual(self.full.call_count, 2)
        os.chmod(self.cache_file(), 0o644)
        self.full.reset_mock()
        self.verify()
        self.assertEqual(self.full.call_count, 2)

    def test_paths_without_a_runtime_directory_never_cache(self):
        del self.paths.runtime
        self.verify()
        self.verify()
        self.assertEqual(self.full.call_count, 4)

    def test_cache_write_failure_does_not_fail_the_check(self):
        with mock.patch.object(managed_verified.tempfile, 'mkstemp', side_effect=OSError('disk full')):
            self.assertEqual(self.verify(), set(self.digests))
        self.assertEqual(managed_verified.load(self.paths), {})


class KnownNodeTests(Chain):
    """``_load_known_node`` re-asserts the retained relations before trusting an entry."""

    def known(self, index):
        record = self.records[index]
        pd = record['managed_upgrade']['plan_digest']
        _, intent, journal, _ = self.full_proof(self.paths, record, {pd: {'journal': self.history / ('managed-journal-' + pd + '.json')}})
        return record, record['managed_upgrade'], journal, managed_verified.facts(intent, self.records[index - 1])

    def test_matching_entry_passes_and_checks_current_state(self):
        record, metadata, journal, entry = self.known(1)
        self.assertTrue(ancestry._load_known_node(self.paths, record, metadata, journal, entry))
        managed_feed.verify_recorded_event.assert_called_once()
        self.recorders['_check_identities'].assert_called_once()
        view = self.recorders['_check_identities'].call_args.args[2]
        self.assertEqual(view, {'plan': {'plan_digest': metadata['plan_digest'], 'identity': IDENTITY},
                                'receipt_sha256': metadata['intent_sha256']})

    def test_mismatched_entry_or_journal_is_refused(self):
        record, metadata, journal, entry = self.known(1)
        for bad_entry, bad_journal in (({**entry, 'plan_digest': '0' * 64}, journal),
                                       ({**entry, 'receipt_sha256': '0' * 64}, journal),
                                       (entry, {**journal, 'phase': 'feed_published'}),
                                       (entry, {**journal, 'prior_binding_sha256': '0' * 64}),
                                       (entry, {**journal, 'git': None}),
                                       (entry, {**journal, 'feed': None})):
            with self.assertRaises(DomainError):
                ancestry._load_known_node(self.paths, record, metadata, bad_journal, bad_entry)

    def test_binding_only_update_with_file_effects_is_refused(self):
        record, metadata, journal, entry = self.known(2)
        self.assertTrue(ancestry._load_known_node(self.paths, record, metadata, journal, entry))
        managed_feed.verify_recorded_event.assert_not_called()
        with self.assertRaises(DomainError):
            ancestry._load_known_node(self.paths, record, metadata, {**journal, 'git': {'commit': '2' * 40}}, entry)


class RecordedEventTests(unittest.TestCase):
    def test_event_must_be_the_single_identical_row(self):
        event = {'change_id': 'chg_' + '1' * 32, 'sequence': 1}

        class Connection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, sql, args):
                self.args = args
                return types.SimpleNamespace(fetchall=lambda: self.rows)

        with mock.patch.object(managed_feed, 'event_dict', lambda conn, row: row):
            self.assertEqual(managed_feed.verify_recorded_event(Connection([event]), event), event)
            for rows in ([], [event, event], [{**event, 'sequence': 2}]):
                with self.assertRaises(DomainError):
                    managed_feed.verify_recorded_event(Connection(rows), event)
            with self.assertRaises(DomainError):
                managed_feed.verify_recorded_event(Connection([event]), None)


class JournalReadTests(Chain):
    def test_read_raw_returns_the_exact_bytes_and_the_same_value(self):
        path = self.history / ('managed-journal-' + self.digests[0] + '.json')
        raw, value = managed_journal.read_raw(path)
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(value, managed_journal.read(path))


if __name__ == '__main__':
    unittest.main()
