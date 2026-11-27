#!/usr/bin/env python3
"""Install a checksummed local connector bundle while this folder is stopped."""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import zipfile

# WhatsApp is built in (selected by the launcher, see native_launch.select_whatsapp), so no bundle installs it.
FACTORIES = {name: 'qorqut_' + name + '.extension:create' for name in ('fastmail', 'telegram')}


def _shared_checks():
    """The bundle and wheel checks this installer shares with the app, read from the app source beside this file."""
    here = Path(__file__).absolute().parent
    for path in (here / 'src/qorqut-code/app/qorqut/bootstrap/extension_bundle.py',  # runtime/extension_install.py
                 here.parent.parent / 'app/qorqut/bootstrap/extension_bundle.py'):  # tools/distribution in a checkout
        if path.is_file():
            spec = importlib.util.spec_from_file_location('qorqut_extension_bundle_checks', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError('The app source with the extension bundle checks is missing')


_shared = _shared_checks()
LIMIT, canonical_name, checksum, unique_object = _shared.LIMIT, _shared.canonical_name, _shared.checksum, _shared.unique_object
plain, relative, inspect_wheel = _shared.plain, _shared.relative, _shared.inspect_wheel


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def manifest(value):
    if not isinstance(value, dict) or set(value) != {'format', 'id', 'version', 'factory', 'wheels'} or type(value['format']) is not int or value['format'] != 1:
        raise ValueError('Unsupported extension manifest')
    if value['id'] not in FACTORIES or value['factory'] != FACTORIES[value['id']]:
        raise ValueError('Unsupported extension factory')
    if not isinstance(value['version'], str) or not re.fullmatch(r'[A-Za-z0-9.!+_-]{1,64}', value['version']):
        raise ValueError('Invalid extension version')
    wheels = value['wheels']
    if not isinstance(wheels, list) or not 1 <= len(wheels) <= 100:
        raise ValueError('Extension bundle must contain 1–100 wheels')
    names = set()
    for wheel in wheels:
        if (not isinstance(wheel, dict) or set(wheel) != {'filename', 'sha256'}
                or not isinstance(wheel['filename'], str) or not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', wheel['filename'])
                or len(wheel['filename']) > 255 or wheel['filename'] in names
                or not isinstance(wheel['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', wheel['sha256'])):
            raise ValueError('Invalid or duplicate wheel declaration')
        names.add(wheel['filename'])
    return value


def inspect_bundle(archive, expected, destination=None):
    """Verify the same open file before reading; extract regular declared files only."""
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Supply the exact lowercase SHA-256 of the bundle')
    with Path(archive).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError('Extension bundle checksum mismatch')
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode='r:gz') as tar:
            members = tar.getmembers()
            if len(members) > 110 or sum(m.size for m in members) > LIMIT:
                raise ValueError('Extension bundle exceeds extraction limits')
            indexed = _shared.index_members(members, directories=('wheels',))
            header = indexed.get('EXTENSION.json')
            if header is None or not header.isfile() or header.size > 65536:
                raise ValueError('Missing or oversized EXTENSION.json')
            value = manifest(json.loads(tar.extractfile(header).read(), object_pairs_hook=unique_object))
            allowed = {'EXTENSION.json', 'wheels', *('wheels/' + w['filename'] for w in value['wheels'])}
            if set(indexed) - allowed or not (allowed - {'wheels'}) <= indexed.keys():
                raise ValueError('Archive contents differ from the extension manifest')
            for wheel in value['wheels']:
                member = indexed['wheels/' + wheel['filename']]
                if not member.isfile():
                    raise ValueError('A wheel must be an ordinary file')
                with tar.extractfile(member) as body:
                    actual = hashlib.file_digest(body, 'sha256').hexdigest()
                if actual != wheel['sha256']:
                    raise ValueError('Wheel checksum mismatch')
            if destination is not None:
                _shared.extract(tar, indexed, destination)
            return value


def inspect_resources(path, extension_id):
    """Check the selected wheel's managed instruction contract before installing code."""
    package = 'qorqut_' + extension_id
    index_name = package + '/resources/index.json'
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        if index_name not in names or package + '/extension.py' not in names:
            raise ValueError('Connector wheel lacks its instruction index or factory')
        raw = wheel.read(index_name)
        if len(raw) > 1024 * 1024:
            raise ValueError('Instruction index exceeds its limit')
        index = json.loads(raw, object_pairs_hook=unique_object)
        if (not isinstance(index, dict) or set(index) != {'schema_version', 'entries'}
                or type(index['schema_version']) is not int or index['schema_version'] not in (1, 2)
                or not isinstance(index['entries'], list) or not 1 <= len(index['entries']) <= 64):
            raise ValueError('Invalid connector instruction index')
        agentic_supplied = index['schema_version'] == 2
        code = ast.parse(wheel.read(package + '/extension.py').decode())
        if not any(isinstance(node, ast.FunctionDef) and node.name == 'create' for node in code.body):
            raise ValueError('Connector factory is missing')
        declared, ids = set(), set()
        for entry in index['entries']:
            if not isinstance(entry, dict) or set(entry) != {'id', 'path', 'parameters', 'source_site'}:
                raise ValueError('Invalid instruction declaration')
            name = entry['path']
            if (not isinstance(name, str) or not name.startswith(index_name.rsplit('/', 1)[0] + '/') or not name.endswith('.md')
                    or (name in names if agentic_supplied else name not in names)
                    or name in declared or not isinstance(entry['id'], str) or entry['id'] in ids
                    or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', entry['id'])):
                raise ValueError('Missing or duplicate declared instruction resource')
            relative(name)
            if not agentic_supplied:
                body = wheel.read(name)
                if len(body) > 1024 * 1024:
                    raise ValueError('Instruction resource exceeds its limit')
                body.decode()
            parameters = entry['parameters']
            if (not isinstance(parameters, list) or any(not isinstance(p, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', p) for p in parameters)
                    or len(parameters) != len(set(parameters)) or not isinstance(entry['source_site'], str)
                    or not entry['source_site'].startswith(package + '/extension.py:')):
                raise ValueError('Invalid instruction call-site declaration')
            nodes = [code]
            for symbol in entry['source_site'].split(':', 1)[1].split('.'):
                nodes = [child for node in nodes for child in getattr(node, 'body', ())
                         if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == symbol]
            calls = [call for node in nodes for call in ast.walk(node) if isinstance(call, ast.Call)
                     and isinstance(call.func, ast.Name) and call.func.id == 'render' and len(call.args) == 3
                     and isinstance(call.args[1], ast.Constant) and call.args[1].value == extension_id
                     and isinstance(call.args[2], ast.Constant) and call.args[2].value == name
                     and {keyword.arg for keyword in call.keywords} == set(parameters)]
            if not calls:
                raise ValueError('Instruction renderer does not match its declared source site')
            declared.add(name); ids.add(entry['id'])
        prefix = index_name.rsplit('/', 1)[0] + '/'
        if any(name.startswith(prefix) and name.endswith('.md') and name not in declared for name in names):
            raise ValueError('Connector has undeclared instruction resources')
        # Version 2 binds prose supplied by the host's Agentic library. Older
        # installers reject this index before installing a wheel. The current
        # installer verifies every binding against its core before any pip write.
        return index['entries'] if agentic_supplied else []


CHECK_AGENTIC_SOURCES = r'''
import json,sys
from qorqut_agentic.behavior.sources import seed_bytes
from qorqut.agentic.text import render_text
entries=json.load(sys.stdin)
for entry in entries:
    body=seed_bytes(sys.argv[1],entry['path'])
    render_text(body.decode('utf-8'),entry['parameters'],{name:'' for name in entry['parameters']})
print(json.dumps({'verified':len(entries)}))
'''


CHECK_ENVIRONMENT = r'''
import importlib.metadata as m,json,sys
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import canonicalize_name,parse_wheel_filename
from pip._vendor.packaging.version import Version
request=json.load(open(sys.argv[1])); wheels=request['wheels']; before=request['before']
installed={canonicalize_name(d.metadata['Name']):d.version for d in m.distributions() if d.metadata.get('Name')}
if before is None:
    before=installed
elif any(installed.get(name)!=version for name,version in before.items()):
    raise SystemExit('Installed baseline changed since the interrupted extension install')
merged=dict(installed)
owned={}; script_owners={}
for distribution in m.distributions():
    name=canonicalize_name(distribution.metadata.get('Name',''))
    if name in before:
        for path in distribution.files or ():
            owned[str(path)]=name
    for entry in distribution.entry_points:
        if entry.group in ('console_scripts','gui_scripts'):
            script_owners[entry.name]=name
incoming=set(); incoming_scripts=set()
for wheel in wheels:
    name=wheel['name']; version=wheel['version']
    if name in before:
        raise SystemExit('Bundle would replace an existing distribution: '+name)
    if name in installed and installed[name]!=version:
        raise SystemExit('Interrupted installation conflicts with installed distribution: '+name)
    if any(path in owned or path in incoming for path in wheel['files']):
        raise SystemExit('Wheel would overwrite another distribution file: '+name)
    for script in wheel['scripts']:
        if script in incoming_scripts or ((Path(sys.prefix)/'bin'/script).exists() and script_owners.get(script)!=name):
            raise SystemExit('Wheel would overwrite an existing launcher: '+script)
        incoming_scripts.add(script)
    incoming.update(wheel['files'])
    _,parsed,_,tags=parse_wheel_filename(wheel['filename'])
    if parsed != Version(version) or not tags.intersection(set(sys_tags())):
        raise SystemExit('Wheel is incompatible with this Python/platform runtime: '+name)
    if wheel['requires_python'] and Version('.'.join(map(str,sys.version_info[:3]))) not in SpecifierSet(wheel['requires_python']):
        raise SystemExit('Wheel requires another Python version: '+name)
    merged[name]=version
for wheel in wheels:
    for raw in wheel['requires']:
        req=Requirement(raw)
        if req.marker and not req.marker.evaluate({'extra':''}):continue
        name=canonicalize_name(req.name)
        if req.url or name not in merged or Version(merged[name]) not in req.specifier:
            raise SystemExit('Missing or incompatible offline dependency: '+str(req))
print(json.dumps({'installed_before':before}))
'''


def load_launcher(runtime):
    spec = importlib.util.spec_from_file_location('qorqbot_native_launcher', runtime / 'launch.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


def installation_processes(root):
    return load_launcher(root / 'runtime').installation_processes(root)


def install(root, archive, expected, *, config=None, resume=False):
    root = plain(root)
    runtime, instance = plain(root / 'runtime'), plain(root / 'instance')
    value = inspect_bundle(archive, expected)
    launcher = load_launcher(runtime)
    os.umask(0o077)
    run_dir = plain(root / '.run')
    run_dir.mkdir(mode=0o700, exist_ok=True)
    with plain(run_dir / 'app.lock').open('a') as app_lock:
        try:
            fcntl.flock(app_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Stop Qorqbot before installing an extension') from None
        launcher.require_completed_update(root)
        lifetime = plain(instance / 'operations/.runtime/instance-lifetime.lock')
        instance_lock = None
        try:
            if lifetime.parent.is_dir():
                instance_lock = lifetime.open('a')
                try:
                    fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('Stop the running instance before installing an extension') from None
            env = launcher.environment(root, runtime / 'setup-state')
            for key in ('PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT', 'PIP_USER', 'PYTHONUSERBASE'):
                env.pop(key, None)
            env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX='1')
            launcher.require_no_terminal_agents(root, env)
            launcher.stop_managed_hosts(root, env)
            # macOS process inspection uses the verified app environment's
            # psutil library. A fresh download has no such environment yet.
            launcher.prepare(root, env)
            launcher.stop_agent_browsers(root)
            if installation_processes(root):
                raise ValueError('Stop this installation’s remaining agents, browsers and helper processes before installing an extension')
            launcher.initialize(root, env)
            if instance_lock is None:
                instance_lock = lifetime.open('a')
                fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            base = plain(runtime / 'optional-install')
            base.mkdir(mode=0o700, exist_ok=True)
            for pending in base.glob('*/install.json'):
                pending = plain(pending)
                if pending.parent.name != expected and read_json(pending).get('phase') not in {'complete', 'refused'}:
                    raise ValueError('Another interrupted extension installation needs recovery first')
            stage = plain(base / expected)
            journal_path = plain(stage / 'install.json')
            if stage.exists():
                if not resume or not journal_path.is_file():
                    raise ValueError('An extension installation already exists; use --resume with the identical bundle')
                journal = read_json(journal_path)
                if journal.get('bundle_sha256') != expected or journal.get('extension') != value:
                    raise ValueError('Interrupted installation does not match this bundle')
                if journal.get('phase') == 'complete':
                    print('This extension bundle is already installed.')
                    return 0
            else:
                if resume:
                    raise ValueError('No interrupted installation exists for this bundle')
                stage.mkdir(mode=0o700)
                inspect_bundle(archive, expected, stage)
                journal = {'format':1, 'bundle_sha256':expected, 'extension':value, 'phase':'staged', 'installed_before':None}
                launcher.save_json(journal_path, journal)
            wheels = []
            for declaration in value['wheels']:
                wheel_path = plain(stage / 'wheels' / declaration['filename'])
                if checksum(wheel_path) != declaration['sha256']:
                    raise ValueError('Staged wheel checksum mismatch')
                wheels.append(inspect_wheel(wheel_path))
            by_name = {wheel['name']:wheel for wheel in wheels}
            if len(by_name) != len(wheels) or by_name.get('qorqut-' + value['id'], {}).get('version') != value['version']:
                raise ValueError('Bundle has duplicate distributions or lacks its declared connector version')
            managed_sources = inspect_resources(stage / 'wheels' / by_name['qorqut-' + value['id']]['filename'], value['id'])
            if managed_sources:
                check = subprocess.run([runtime / 'venv/bin/python', '-I', '-B', '-c', CHECK_AGENTIC_SOURCES, value['id']],
                    input=json.dumps(managed_sources), env=env, cwd=root, capture_output=True, text=True)
                if check.returncode or json.loads(check.stdout) != {'verified': len(managed_sources)}:
                    raise ValueError('This core lacks the connector’s required Agentic instructions; update the core first')
            if config is not None:
                config = plain(config)
                info = config.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
                    raise ValueError('Extension config must be a private JSON file owned by this user')
                configured = read_json(config)
                if not isinstance(configured, dict):
                    raise ValueError('Extension config must be a JSON object')
            else:
                configured = {}
            config_directory = plain(instance / 'config/extension-inputs')
            config_directory.mkdir(mode=0o700, exist_ok=True)
            if config_directory.stat().st_uid != os.getuid() or config_directory.stat().st_mode & 0o077:
                raise ValueError('Extension configuration directory must be private')
            config_path = plain(config_directory / (expected + '.json'))
            adoption_journal = plain(instance / 'config/extension-install.json')
            if config_path.exists():
                if read_json(config_path) != configured:
                    admitted = (adoption_journal.exists() and
                        read_json(adoption_journal).get('request', {}).get('bundle_sha256') == expected)
                    if not resume or admitted:
                        raise ValueError('Resume requires the identical admitted extension configuration')
                    # Factory validation may reject config before the backend
                    # records any request. Correcting that input is safe until
                    # its schema/managed transaction has actually been admitted.
                    launcher.save_json(config_path, configured)
            else:
                launcher.save_json(config_path, configured)
            request = stage / 'dependency-check.json'
            launcher.save_json(request, {'wheels':wheels, 'before':journal['installed_before']})
            python = runtime / 'venv/bin/python'
            check = subprocess.run([python, '-I', '-B', '-c', CHECK_ENVIRONMENT, request], env=env, cwd=root, capture_output=True, text=True)
            if check.returncode:
                journal['phase'] = 'refused'
                launcher.save_json(journal_path, journal)
                raise ValueError(check.stderr.strip() or 'Offline dependency validation failed')
            journal['installed_before'] = json.loads(check.stdout)['installed_before']
            journal['phase'] = 'installing-wheels'
            launcher.save_json(journal_path, journal)
            with plain(stage / 'pip.log').open('ab') as log:
                subprocess.run([python, '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--force-reinstall',
                                *(stage / 'wheels' / wheel['filename'] for wheel in wheels)],
                               env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
                subprocess.run([python, '-I', '-B', '-m', 'pip', 'check'], env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
            journal['phase'] = 'applying'
            launcher.save_json(journal_path, journal)
            # The app lock remains held; the additive installer owns the instance
            # lock and its schema/Agentic transaction, backups and resume journal.
            instance_lock.close(); instance_lock = None
            command = [python, '-I', '-B', '-m', 'qorqut.bootstrap.extension_install', '--prepared-root', runtime,
                       '--instance-root', instance, '--factory', value['factory'], '--bundle-sha256', expected, '--config', config_path]
            if resume and adoption_journal.exists() and read_json(adoption_journal).get('request', {}).get('bundle_sha256') == expected:
                command.append('--resume')
            with plain(stage / 'adoption.log').open('ab') as log:
                subprocess.run(command, env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
            journal['phase'] = 'complete'
            launcher.save_json(journal_path, journal)
            print('Extension installed. Start Qorqbot normally; account setup remains explicit.')
            return 0
        except BaseException:
            if 'journal' in locals() and journal.get('phase') in {'staged', 'refused'}:
                journal['phase'] = 'refused'
                launcher.save_json(journal_path, journal)
            raise
        finally:
            if instance_lock is not None:
                instance_lock.close()


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    return install(root or Path(__file__).resolve().parent.parent, args.bundle, args.sha256, config=args.config, resume=args.resume)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SyntaxError, RuntimeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        print('Qorqbot extension install: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
