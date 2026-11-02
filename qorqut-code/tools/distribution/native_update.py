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
import uuid
from urllib.parse import quote


def load_launcher(runtime):
    spec = importlib.util.spec_from_file_location('qorqbot_launcher', runtime / 'launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_processes(runtime):
    """Own-UID processes still executing this runtime, including detached browsers/helpers."""
    found = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid == os.getuid() and (process / 'exe').resolve().is_relative_to(runtime):
                found.append(int(process.name))
        except (OSError, ValueError):
            continue
    return found


def unpack(archive, stage):
    """Accept only a fresh package, never instance data or files outside its runtime."""
    kwargs = {'fileobj': archive} if hasattr(archive, 'read') else {'name': archive}
    with tarfile.open(mode='r:gz', **kwargs) as tar:
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


def retain_completed(root, launcher):
    journal = root / '.run/update.json'
    if not journal.exists():
        return
    record = launcher.read_json(journal)
    if record.get('phase') not in {'complete', 'restored-before-start'}:
        raise ValueError('An interrupted update remains. Run ./qorqbot update --recover first')
    archive = root / '.updates' / uuid.uuid4().hex
    archive.mkdir(parents=True)
    for name in ('.previous-runtime', '.update-staging'):
        if (root / name).exists():
            (root / name).rename(archive / name.lstrip('.'))
    journal.rename(archive / 'update.json')


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def restore_runtime(root, launcher, record):
    runtime, previous, stage = root / 'runtime', root / '.previous-runtime', root / '.update-staging'
    old_launcher = stage / 'previous-launcher'
    if (not record.get('old_payload_sha256') or not old_launcher.is_file()
            or file_sha(old_launcher) != record.get('old_launcher_sha256')):
        raise ValueError('Verified prior runtime/launcher recovery evidence is missing')
    if previous.exists() and file_sha(previous / 'PAYLOAD.json') != record['old_payload_sha256']:
        raise ValueError('The prior runtime identity changed; recovery was refused')
    if not previous.exists() and (not runtime.exists() or file_sha(runtime / 'PAYLOAD.json') != record['old_payload_sha256']):
        raise ValueError('The previous runtime is missing; no automatic recovery is safe')
    record['phase'] = 'restoring'
    launcher.save_json(root / '.run/update.json', record)
    if previous.exists():
        if runtime.exists():
            runtime.rename(stage / 'failed-runtime')
        previous.rename(runtime)
    shutil.copy2(old_launcher, root / '.qorqbot-new')
    (root / '.qorqbot-new').replace(root / 'qorqbot')
    record['phase'] = 'restored-before-start'
    launcher.save_json(root / '.run/update.json', record)


def recover(root, launcher):
    """Restore only a runtime whose replacement has never been admitted to start."""
    journal = root / '.run/update.json'
    if not journal.exists():
        raise ValueError('There is no recorded update to recover')
    record = launcher.read_json(journal)
    if record.get('phase') in {'complete', 'restored-before-start'}:
        retain_completed(root, launcher)
        print('Completed update records retained in .updates. Qorqbot can start normally.')
        return
    if record.get('phase') not in {'prepared', 'switched', 'restoring'}:
        raise ValueError('Unrecognized update recovery state; no files were changed')
    restore_runtime(root, launcher, record)
    retain_completed(root, launcher)
    print('Previous runtime restored. Instance data was unchanged; start Qorqbot normally.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, nargs='?')
    parser.add_argument('--sha256')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    runtime, instance = root / 'runtime', root / 'instance'
    launcher = load_launcher(runtime if runtime.exists() else root / '.previous-runtime')
    os.umask(0o077)
    if args.recover:
        if args.archive or args.sha256:
            parser.error('--recover takes no archive or checksum')
    else:
        if not args.archive or not args.sha256:
            parser.error('An archive and --sha256 are required')
        archive_stream = args.archive.open('rb')
        actual = hashlib.file_digest(archive_stream, 'sha256').hexdigest()
        archive_stream.seek(0)
        if actual != args.sha256.lower():
            raise ValueError('Update archive checksum mismatch')
    stage, previous = root / '.update-staging', root / '.previous-runtime'
    journal = root / '.run/update.json'
    root.joinpath('.run').mkdir(mode=0o700, exist_ok=True)
    with (root / '.run/app.lock').open('a') as app_lock:
        fcntl.flock(app_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lifetime = instance / 'operations/.runtime/instance-lifetime.lock'
        with lifetime.open('a') as instance_lock:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.recover:
                if runtime_processes(runtime) or runtime_processes(previous):
                    raise ValueError('Stop this installation’s remaining processes before recovery')
                recover(root, launcher)
                return 0
            retain_completed(root, launcher)
            if stage.exists() or previous.exists():
                raise ValueError('Unrecorded update files remain; inspect them before another update')
            launcher.require_completed_extension_installs(runtime)
            env = launcher.environment(root, instance / 'home')
            tmux = subprocess.run([runtime / 'native/bin/tmux', '-L', 'bazarlyk', 'list-sessions'],
                                  env=env, capture_output=True, text=True)
            if tmux.returncode == 0:
                raise ValueError('Stop this installation’s agent terminals before updating')
            if not any(text in tmux.stderr.lower() for text in ('no server running', 'no such file or directory')):
                raise ValueError('Could not prove that this installation’s agent terminals are stopped')
            if runtime_processes(runtime):
                raise ValueError('Stop this installation’s remaining browsers and helper processes before updating')
            config = launcher.read_json(instance / 'config/extensions.json')
            if any(item.get('factory') != 'qorqut_google_calendar.extension:create' for item in config.get('extensions', ())):
                raise ValueError('Selected optional extensions need a compatible update bundle; this bundle supports core and the bundled calendar provider')
            stage.mkdir(mode=0o700)
            try:
                unpack(archive_stream, stage)
                archive_stream.close()
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
                shutil.copy2(root / 'qorqbot', stage / 'previous-launcher')
                record = {'format': 1, 'phase': 'prepared', 'archive_sha256': actual,
                          'before': old.get('version'), 'after': new.get('version'),
                          'old_payload_sha256': file_sha(runtime / 'PAYLOAD.json'),
                          'old_launcher_sha256': file_sha(stage / 'previous-launcher')}
                launcher.save_json(journal, record)
                runtime.rename(previous)
                try:
                    new_runtime.rename(runtime)
                    record['phase'] = 'switched'
                    launcher.save_json(journal, record)
                    # The environment must be created at its final absolute path.
                    setup = ('import importlib.util,pathlib,sys;'
                             'root=pathlib.Path(sys.argv[1]);'
                             'spec=importlib.util.spec_from_file_location("setup",root/"runtime/launch.py");'
                             'm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);'
                             'env=m.environment(root,root/"runtime/setup-state");'
                             'm.prepare(root,env);m.initialize(root,env)')
                    subprocess.run([runtime / 'native/bin/python3.12', '-I', '-c', setup, root],
                                   env=env, cwd=root, check=True)
                    subprocess.run([runtime / 'venv/bin/python', '-I', '-m', 'bazarlyk.bootstrap.native_update_check',
                                    '--prepared-root', runtime, '--instance-root', instance], env=env, cwd=root, check=True)
                except BaseException:
                    # No new application was admitted and no data migration ran. Keep failed bytes for inspection.
                    restore_runtime(root, launcher, record)
                    raise
                shutil.copy2(stage / 'qorqbot/qorqbot', root / '.qorqbot-new')
                (root / '.qorqbot-new').replace(root / 'qorqbot')
                record['phase'] = 'complete'
                launcher.save_json(journal, record)
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
