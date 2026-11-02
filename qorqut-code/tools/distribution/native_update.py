#!/usr/bin/env python3
"""Offline, same-schema updates of the native core folder; never replaces instance data.

The caller supplies a local archive and its separately verified SHA-256. There
is no feed or automatic download. Schema-changing and extension-bearing updates
require a qualified migration and are refused by this first updater.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from urllib.parse import quote


def load_launcher(runtime):
    spec = importlib.util.spec_from_file_location('qorqbot_launcher', runtime / 'launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unpack(archive, stage):
    """Accept only a fresh package, never instance data or files outside its runtime."""
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 150000 or sum(m.size for m in members) > 8 * 1024 ** 3:
            raise ValueError('Package exceeds the extraction bounds')
        names = set()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'qorqbot':
                raise ValueError('Package contains an unsafe path')
            if member.name in names:
                raise ValueError('Package contains duplicate paths')
            names.add(member.name)
            if len(path.parts) > 1 and path.parts[1] not in {'runtime', 'qorqbot', 'README.txt'}:
                raise ValueError('Update package contains instance or unexpected files')
            if member.isdev() or member.isfifo():
                raise ValueError('Package contains a special file')
        tar.extractall(stage, filter='data')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    runtime, instance = root / 'runtime', root / 'instance'
    launcher = load_launcher(runtime)
    os.umask(0o077)
    with args.archive.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != args.sha256.lower():
        raise ValueError('Update archive checksum mismatch')
    stage, previous = root / '.update-staging', root / '.previous-runtime'
    journal = root / '.run/update.json'
    if stage.exists() or previous.exists() or journal.exists():
        raise ValueError('A previous update or recovery remains; inspect it before another update')
    root.joinpath('.run').mkdir(mode=0o700, exist_ok=True)
    with (root / '.run/app.lock').open('a') as app_lock:
        fcntl.flock(app_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lifetime = instance / 'operations/.runtime/instance-lifetime.lock'
        with lifetime.open('a') as instance_lock:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = launcher.environment(root, instance / 'home')
            tmux = subprocess.run([runtime / 'native/bin/tmux', '-L', 'bazarlyk', 'list-sessions'],
                                  env=env, capture_output=True, text=True)
            if tmux.returncode == 0:
                raise ValueError('Stop this installation’s agent terminals before updating')
            if not any(text in tmux.stderr.lower() for text in ('no server running', 'no such file or directory')):
                raise ValueError('Could not prove that this installation’s agent terminals are stopped')
            config = launcher.read_json(instance / 'config/extensions.json')
            if config.get('extensions'):
                raise ValueError('This updater requires a core-only instance; selected extensions need a compatible update bundle')
            stage.mkdir(mode=0o700)
            try:
                unpack(args.archive, stage)
                new_runtime = stage / 'qorqbot/runtime'
                new = launcher.verify(new_runtime)
                old = launcher.read_json(runtime / 'PAYLOAD.json')
                if not new.get('schema_target') or new['schema_target'] != old.get('schema_target'):
                    raise ValueError('This update changes the database schema; a qualified migration is required')
                db = instance / 'operations/.runtime/operations.sqlite3'
                with sqlite3.connect('file:' + quote(str(db)) + '?mode=ro', uri=True) as connection:
                    observed = connection.execute("SELECT name, version, digest FROM target_identity WHERE id='target'").fetchone()
                if list(observed or ()) != new['schema_target']:
                    raise ValueError('The existing database does not match the update’s supported schema')
                launcher.save_json(journal, {'format': 1, 'phase': 'prepared', 'archive_sha256': actual,
                                              'before': old.get('version'), 'after': new.get('version')})
                runtime.rename(previous)
                try:
                    new_runtime.rename(runtime)
                    launcher.save_json(journal, {'format': 1, 'phase': 'switched', 'archive_sha256': actual})
                    # The environment must be created at its final absolute path.
                    setup = ('import importlib.util,pathlib,sys;'
                             'root=pathlib.Path(sys.argv[1]);'
                             'spec=importlib.util.spec_from_file_location("setup",root/"runtime/launch.py");'
                             'm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);'
                             'env=m.environment(root,root/"runtime/setup-state");'
                             'm.prepare(root,env);m.initialize(root,env)')
                    subprocess.run([runtime / 'native/bin/python3.12', '-I', '-c', setup, root],
                                   env=env, cwd=root, check=True)
                except BaseException:
                    # No new application was admitted and no data migration ran. Keep failed bytes for inspection.
                    if runtime.exists():
                        runtime.rename(stage / 'failed-runtime')
                    previous.rename(runtime)
                    launcher.save_json(journal, {'format': 1, 'phase': 'restored-before-start', 'archive_sha256': actual})
                    raise
                shutil.copy2(stage / 'qorqbot/qorqbot', root / 'qorqbot')
                launcher.save_json(journal, {'format': 1, 'phase': 'complete', 'archive_sha256': actual,
                                              'before': old.get('version'), 'after': new.get('version')})
                print('Update installed. Your instance data was preserved. Start Qorqbot normally.')
                print('Previous runtime and update record are retained for inspection.')
            except BaseException:
                # Before a switch, discard only the newly extracted staging tree.
                if not journal.exists():
                    shutil.rmtree(stage)
                raise
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Qorqbot update: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
