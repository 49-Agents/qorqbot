"""Compatibility boundaries for installations and transcripts predating the rename."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
from types import SimpleNamespace
import tomllib
import unittest
from unittest import mock

from qorqut.bootstrap.codex_config import configuration
from qorqut.native_chat.projection import _codex_result
from qorqut.service.main import transcript_capture_configuration
from qorqut.storage.recovery import _walk
from qorqut.storage.host_writers import CommandResult, HostTerminalWriters
from qorqut.storage.versioning import GitVersioner, public_path
from qorqut_operations.contracts import DomainError


class RenameCompatibilityTests(unittest.TestCase):
    def test_old_private_state_is_never_exported_as_authored_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.bazarlyk', '.qorqut'):
                (root / name).mkdir()
                (root / name / 'credential').write_text('private fixture')
                self.assertFalse(public_path('agents/worker/' + name + '/credential', []))
            (root / '.bazarlyk-interrupted').write_text('old partial private write')
            (root / '.qorqut-interrupted').write_text('new partial private write')
            (root / 'README.md').write_text('authored fixture')
            self.assertEqual([p.name for p in _walk(root)], ['README.md'])

    def test_pre_rename_capture_opt_out_survives_and_new_setting_wins(self):
        with mock.patch.dict(os.environ, {'BAZARLYK_CAPTURE_TRANSCRIPTS': 'false'}, clear=True):
            self.assertFalse(transcript_capture_configuration())
            os.environ['QORQUT_CAPTURE_TRANSCRIPTS'] = 'true'
            self.assertTrue(transcript_capture_configuration())

    def test_old_and_new_transcript_receipts_survive_but_foreign_servers_do_not(self):
        item = {'tool': 'send', 'status': 'completed', 'result': {'content': ['receipt']}}
        for server in ('bazarlyk', 'qorqut'):
            self.assertEqual(_codex_result({**item, 'server': server}, 'send'), ['receipt'])
            self.assertIsNone(_codex_result({**item, 'server': server, 'status': 'failed'}, 'send'))
        self.assertIsNone(_codex_result({**item, 'server': 'foreign'}, 'send'))

    def test_repreparation_retires_the_old_managed_server_and_keeps_foreign_servers(self):
        data, _ = configuration(workspace='/tmp/rename-fixture', endpoint='http://127.0.0.1:1096',
            header_command='headers', command='hook', snapshot_command='snapshot', protected=[], read_denied=[],
            preserved_servers={'bazarlyk': {'url': 'http://old.invalid'}, 'foreign': {'url': 'http://foreign.invalid'}})
        servers = tomllib.loads(data.decode())['mcp_servers']
        self.assertEqual(set(servers), {'qorqut', 'foreign'})
        self.assertEqual(servers['foreign']['url'], 'http://foreign.invalid')

    def test_runtime_update_refuses_a_live_pre_rename_terminal(self):
        path = Path(__file__).parents[2] / 'tools/distribution/native_launch.py'
        spec = importlib.util.spec_from_file_location('rename_native_launch', path)
        launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launch)
        replies = [subprocess.CompletedProcess([], 1, '', 'no server running'),
                   subprocess.CompletedProcess([], 0, 'worker', '')]
        with mock.patch.object(launch.shutil, 'which', return_value='/usr/bin/tmux'), \
                mock.patch.object(launch.subprocess, 'run', side_effect=replies) as run:
            with self.assertRaisesRegex(ValueError, 'Stop this installation'):
                launch.require_stopped_terminals(Path('/tmp/rename-fixture'), {})
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ['qorqut', 'bazarlyk'])

    def test_first_new_history_ref_continues_the_old_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (root / 'README.md').write_text('main')
            git('add', 'README.md')
            git('commit', '-m', 'main')
            git('checkout', '-b', 'bazarlyk-history')
            (root / 'README.md').write_text('previous history')
            git('commit', '-am', 'previous history')
            previous = git('rev-parse', 'HEAD')
            generation = Path(directory) / 'generation'
            (generation / 'authored').mkdir(parents=True)
            (generation / 'authored/manifest.json').write_text(json.dumps({'businesses': []}))
            with tarfile.open(generation / 'authored/sources.tar', 'w') as archive:
                archive.add(root / 'README.md', arcname='README.md')
            git('checkout', 'main')
            control = SimpleNamespace(check=lambda: None, remaining_seconds=lambda: 30)
            result = GitVersioner(root).version(generation,
                {'created_at': '2026-11-08T12:00:00Z', 'generation_id': 'fixture'},
                SimpleNamespace(files={}), {}, lambda: None, control)
            self.assertEqual(result['ref'], 'refs/heads/qorqut-history')
            self.assertEqual(git('rev-parse', 'qorqut-history'), previous)
            self.assertEqual(git('rev-parse', 'bazarlyk-history'), previous)

    def test_recovery_inventory_also_rejects_live_pre_rename_terminals(self):
        replies = [CommandResult(1, 'no server running on /tmp/fixture/qorqut\n'),
                   CommandResult(0, '$1\tworker\n')]
        run = mock.Mock(side_effect=replies)
        writers = HostTerminalWriters('/usr/bin/tmux', command_runner=run)
        with self.assertRaisesRegex(DomainError, 'Live host terminal'):
            writers.inventory_and_freeze(timeout=5)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ['qorqut', 'bazarlyk'])

    def test_layout_receipts_preserve_pre_rename_retirement(self):
        from qorqut.agentic.layout_inspection import retained_layout_inventory
        for host in ('bazarlyk', 'qorqut'):
            with self.subTest(host=host):
                path = f'native/app/{host}/agentic/assets/learning-work.md'
                full = 'packages/imported-host-builtins/' + path
                ref = 'imported-host-builtins/learning-work'
                inventory = {'packages': [{'id': 'imported-host-builtins',
                    'entries': [{'id': 'learning-work', 'path': path}], 'files': {path: 'hash'}}],
                    'assets': [{'destination': full}], 'bindings': [{'entry_refs': [ref],
                    'skills': [{'canonical_entry': ref}]}], 'coverage': {'asset_count': 1}}
                result = retained_layout_inventory(inventory, {'ref_map': {}, 'path_map': {}})
                self.assertEqual(result['packages'][0]['entries'], [])
                self.assertEqual(result['packages'][0]['files'], {})
                self.assertEqual(result['assets'], [])
                self.assertEqual(result['bindings'][0], {'entry_refs': [], 'skills': []})
                self.assertEqual(inventory['coverage']['asset_count'], 1)
                self.assertIs(retained_layout_inventory(inventory,
                    {'ref_map': {}, 'path_map': {full: 'retained'}}), inventory)
