"""Private offline managed application.

The caller must hold exclusive offline ownership of the affected instance tree,
Git state and runtime for the entire invocation. The module authoring lock below
serializes cooperating writers; it cannot fence arbitrary external processes.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import stat

from qorqut_agentic.changes import normalize_attribution, package_ids_of
from qorqut_agentic.store import Store
from bazarlyk.private_files import fsync_directory, read_private_file, require_private_directory
from . import (layout_activation, layout_files, layout_git, layout_intent, managed_ancestry, managed_binding,
               managed_feed, managed_files, managed_git, managed_intent, managed_journal, managed_plan,
               managed_runtime_evidence, provisioning)
from .installation import MAX_RECORD, open_installation
from .layout_inspection import inspect_git, inspect_runtime
from .layout_intent import MAX_INTENT_BYTES, _conflict
from .layout_runtime import _offline_reader
from .support import canonical, digest
from .upgrade import _unique, history_directory, history_snapshot, read_binding_bytes, retain_prior_binding

_fault_hook = lambda _stage, **_kwargs: None


def _publish_intent(path, raw):
    directory = require_private_directory(path.parent)
    if os.path.lexists(path):
        if read_private_file(path, max_bytes=MAX_INTENT_BYTES) != raw:
            raise _conflict('Managed intent differs from retained evidence')
        return
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open('.', os.O_WRONLY | os.O_TMPFILE | os.O_CLOEXEC, 0o600, dir_fd=parent)
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError('short intent write')
                view = view[count:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.linkat(-100, os.fsencode('/proc/self/fd/' + str(fd)), parent, os.fsencode(path.name), 0x400) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            os.fsync(parent)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _layout_proof(paths):
    intents = layout_intent.retained_intents(paths)
    verified = {}
    if layout_activation.incomplete(paths, intents, _verified=verified, offline_read_only=True):
        raise _conflict('Unfinished layout update requires offline resume')
    return intents, verified


def _proof(paths, plan, intent_raw, journal_path):
    return {'intent': {'path': plan['evidence']['intent'], 'sha256': digest(intent_raw)},
            'journal': {'path': str(journal_path), 'sha256': digest(read_private_file(journal_path, max_bytes=MAX_INTENT_BYTES))}}


def _receipt(base, proof, *, worked, recovered, replayed):
    result = {**base, 'worked': worked, 'replayed': replayed, 'recovered_partial_attempt': recovered}
    if proof is not None:
        result['proof'] = proof
    return result


def _packages(prior, plan):
    old = {p['id']: p for p in prior['inventory']['packages']}
    new = {p['id']: p for p in plan['target_inventory']['packages']}
    return sorted({p for p in old.keys() | new.keys() if old.get(p) != new.get(p)} |
                  set(package_ids_of([item['path'] for item in plan['changes']])))


def _provision_before(paths, plan):
    actual = provisioning.plan(paths)
    if actual != plan['provisioning']:
        raise _conflict('Managed provisioning changed since preparation')
    identities = {}
    for item in actual['items']:
        if item['state'] == 'ready':
            info = os.stat(item['path'], follow_symlinks=False)
            identities[item['name']] = {'device': info.st_dev, 'inode': info.st_ino,
                                        'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}
        else:
            identities[item['name']] = None
    return {'original': identities, 'created': {}, 'pending': None, 'result': None}


def _provision_verify(paths, plan, journal):
    state = provisioning.plan(paths)
    old = {item['name']: item for item in plan['provisioning']['items']}
    now = {item['name']: item for item in state['items']}
    created = journal['provisioning']['created']
    if not set(created) <= {name for name, item in old.items() if item['state'] == 'missing'}:
        raise _conflict('Unreviewed private provisioning identity')
    for identity in created.values():
        if (not isinstance(identity, dict) or set(identity) != {'device', 'inode', 'mode', 'uid'}
                or any(type(value) is not int or value < 0 for value in identity.values())):
            raise _conflict('Invalid private provisioning identity')
    for name, initial in old.items():
        current = now[name]
        if current['path'] != initial['path'] or current['state'] not in ('ready', 'missing'):
            raise _conflict('Unsafe managed provisioning state')
        identity = journal['provisioning']['original'][name]
        if initial['state'] == 'ready':
            if current['state'] != 'ready':
                raise _conflict('Preserved private root disappeared')
            info = os.stat(current['path'], follow_symlinks=False)
            if {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid} != identity:
                raise _conflict('Preserved private root identity changed')
        elif current['state'] == 'ready':
            created = journal['provisioning']['created'].get(name)
            if created is None:
                raise _conflict('Ambiguous managed provisioning interruption for plan ' + plan['plan_digest'] + ': ' + current['path'])
            info = os.stat(current['path'], follow_symlinks=False)
            if ({'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid} != created
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise _conflict('Created private root identity changed: ' + current['path'])
            root = Path(current['path'])
            allowed = {Path(other['path']).name for other in old.values() if Path(other['path']).parent == root}
            if any(child.name not in allowed for child in root.iterdir()):
                raise _conflict('New private provisioning root has unreviewed contents')
        elif initial['state'] != 'missing':
            raise _conflict('Invalid reviewed private provisioning state')
        elif name in journal['provisioning']['created']:
            raise _conflict('Created private root disappeared: ' + current['path'])
    return state


def _provision(paths, intent, journal, save):
    plan = intent['plan']
    _provision_verify(paths, plan, journal)
    provision = journal['provisioning']
    for item in plan['provisioning']['items']:
        if item['state'] != 'missing' or item['name'] in provision['created']:
            continue
        parent = Path(item['path']).parent
        if not parent.is_absolute() or any(p.is_symlink() for p in (parent, *parent.parents)):
            raise _conflict('Unsafe private provisioning parent')
        info = os.stat(parent, follow_symlinks=False)
        expected = {'name': item['name'], 'path': item['path'],
                    'parent': {'device': info.st_dev, 'inode': info.st_ino}}
        if provision['pending'] not in (None, expected):
            raise _conflict('Managed private provisioning parent changed')
        provision['pending'] = expected
        save()
        _fault_hook('before_provision_mkdir', name=item['name'], path=item['path'])
        if (managed_journal.read(Path(plan['evidence']['journal'])) != journal
                or read_private_file(Path(plan['evidence']['intent']), max_bytes=MAX_INTENT_BYTES) != canonical(intent) + b'\n'
                or digest(read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD)) != intent['prior_binding_sha256']):
            raise _conflict('Managed retained evidence changed before provisioning')
        if any(p.is_symlink() for p in (parent, *parent.parents)):
            raise _conflict('Private provisioning parent became unsafe')
        observed = os.stat(parent, follow_symlinks=False)
        if {'device': observed.st_dev, 'inode': observed.st_ino} != expected['parent']:
            raise _conflict('Private provisioning parent identity changed')
        _provision_verify(paths, plan, journal)
        identity = provisioning._create_root(paths, item['name'])
        _fault_hook('provision_mkdir', name=item['name'], path=item['path'])
        provision['created'][item['name']] = identity
        provision['pending'] = None
        save()
        _fault_hook('provision_recorded', name=item['name'], path=item['path'])
    state = _provision_verify(paths, plan, journal)
    if any(item['state'] != 'ready' for item in state['items']):
        raise _conflict('Managed provisioning is incomplete')
    old = plan['provisioning']['items']
    result = {'schema_version': 1, 'mode': '0700', 'created': [x['name'] for x in old if x['state'] == 'missing'],
              'preserved': [x['name'] for x in old if x['state'] == 'ready'], 'items': state['items']}
    if journal['provisioning']['result'] not in (None, result):
        raise _conflict('Managed provisioning receipt differs')
    journal['provisioning']['result'] = result
    save()


def _check_git(paths, intent, journal, *, allow_index_lock=False):
    rootfd = layout_files._open_root(paths.agentic_root)
    try:
        gitdir, head_identity = layout_files._head_identity(rootfd)
        if gitdir != journal['gitdir'] or head_identity != journal['head_identity']:
            raise _conflict('Managed Git directory or symbolic HEAD changed')
    finally:
        os.close(rootfd)
    runner = layout_git._runner(paths.agentic_root, history_directory(paths))
    layout_git._reject_filters(runner)
    from .layout_preflight import check as preflight
    def run(args, *, allow_missing=False):
        result = runner.run(args, check=False, env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30)
        if result.returncode not in ((0, 1) if allow_missing else (0,)) or len(result.stdout) > MAX_INTENT_BYTES:
            raise _conflict('Managed Git preflight failed')
        return result.stdout
    if preflight(paths.agentic_root, run):
        raise _conflict('Managed Git preflight found unsupported state')
    owned = None
    if allow_index_lock and journal['index_publication'] is not None:
        replacement = journal['index_publication']['replacement']
        owned = {k: replacement[k] for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
    layout_git._check_operation_locks(paths.agentic_root, intent['plan']['git']['branch'], owned_index=owned)
    branch = layout_git._run(runner, ['symbolic-ref', '-q', '--no-recurse', 'HEAD']).decode().strip()
    if branch != intent['plan']['git']['branch']:
        raise _conflict('Managed selected branch changed')
    head = layout_git._run(runner, ['rev-parse', '--verify', 'HEAD']).decode().strip()
    git = journal['git']
    allowed_heads = {intent['head']}
    if git is not None:
        allowed_heads.add(git['commit'])
    if head not in allowed_heads:
        raise _conflict('Managed Git HEAD changed')
    if os.path.lexists(paths.agentic_root / '.git/index.lock') and not allow_index_lock:
        raise _conflict('Unexpected Git index lock')
    fd = os.open(paths.agentic_root / '.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held, _, index = layout_git._read_index_at(fd, 'index')
        os.close(held)
    finally:
        os.close(fd)
    original = base64.b64decode(intent['index']['content_base64'], validate=True)
    final = original if git is None else base64.b64decode(git['final_index']['content_base64'], validate=True)
    if index not in (original, final):
        raise _conflict('Managed Git index changed')
    phase = journal['phase']
    if phase in ('prepared', 'files_published') and head != intent['head']:
        raise _conflict('Managed Git ref changed before its stage')
    if phase in ('prepared', 'files_published', 'git_prepared', 'ref_published') and index != original:
        raise _conflict('Managed original index is required at this stage')
    if phase in ('ref_published', 'index_publishing', 'git_published', 'feed_published', 'binding_published', 'complete'):
        expected_head = git['commit'] if git is not None else intent['head']
        if head != expected_head:
            raise _conflict('Managed published ref changed')
    if phase in ('git_published', 'feed_published', 'binding_published', 'complete') and index != final:
        raise _conflict('Managed published index changed')
    status = layout_git._run(runner, ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames'])
    allowed = {c['path'] for c in intent['plan']['changes']}
    for row in filter(None, status.split(b'\0')):
        if len(row) < 4 or row[3:].decode('utf-8') not in allowed:
            raise _conflict('Unrelated Git work changed during managed update')
    return head, index


def _check_runtime(paths, intent, *, after, commit):
    store = Store(paths.agentic_runtime)
    changes = intent['plan']['changes']
    fixed = intent['fixed']
    with _offline_reader(store) as conn:
        conn.execute('BEGIN')
        if after and changes:
            return managed_feed.verify_effects(conn, intent, commit)
        actual = managed_runtime_evidence.snapshot(conn, [c['path'] for c in changes], fixed['change_id'] if fixed else None)
        if actual != intent['runtime_before']:
            raise _conflict('Managed runtime changed during application')
    return None


def _common(paths, intent, journal, prior_raw):
    plan = intent['plan']
    if (read_private_file(Path(plan['evidence']['intent']), max_bytes=MAX_INTENT_BYTES) != canonical(intent) + b'\n'
            or read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD) != prior_raw
            or managed_journal.read(Path(plan['evidence']['journal'])) != journal):
        raise _conflict('Managed retained evidence changed during application')
    if (journal['plan_digest'] != intent['plan']['plan_digest'] or journal['intent_sha256'] != intent['receipt_sha256']
            or journal['prior_binding_sha256'] != intent['prior_binding_sha256'] or journal['attribution'] != intent['attribution']):
        raise _conflict('Managed journal does not match intent')
    if not set(journal['created_dirs']) <= managed_files.required_dirs(intent) - journal['initial_dirs'].keys():
        raise _conflict('Managed journal lists unreviewed directories')
    for path, identity in {**journal['initial_dirs'], **journal['created_dirs']}.items():
        from .support import relative
        relative(path)
        if (path.split('/', 1)[0] not in layout_files._AREAS or not isinstance(identity, dict)
                or set(identity) != {'device', 'inode', 'mode'}
                or any(type(v) is not int or v < 0 for v in identity.values()) or identity['mode'] > 0o777):
            raise _conflict('Invalid managed directory evidence')
    if (not isinstance(journal['provisioning'], dict) or set(journal['provisioning']) != {'original', 'created', 'pending', 'result'}
            or not isinstance(journal['provisioning']['original'], dict)
            or not isinstance(journal['provisioning']['created'], dict)
            or set(journal['provisioning']['original']) != {i['name'] for i in intent['plan']['provisioning']['items']}):
        raise _conflict('Invalid managed provisioning evidence')
    for item in intent['plan']['provisioning']['items']:
        identity = journal['provisioning']['original'][item['name']]
        if item['state'] == 'missing':
            if identity is not None:
                raise _conflict('Unexpected original provisioning identity')
        elif (not isinstance(identity, dict) or set(identity) != {'device', 'inode', 'mode', 'uid'}
              or any(type(v) is not int or v < 0 for v in identity.values())):
            raise _conflict('Invalid original provisioning identity')
    prior = journal['prior_binding']
    if (not isinstance(prior, dict) or set(prior) != {'snapshot', 'sha256', 'created'}
            or type(prior['created']) is not bool or prior['sha256'] != intent['prior_binding_sha256']
            or prior['snapshot'] != str(history_snapshot(paths, intent['prior_binding_sha256']))):
        raise _conflict('Invalid retained prior binding evidence')
    if journal['phase'] != 'prepared' and journal['provisioning']['result'] is None:
        raise _conflict('Missing managed provisioning result')
    if journal['phase'] != 'prepared' and journal['provisioning']['pending'] is not None:
        raise _conflict('Unfinished managed provisioning effect')
    provisioned = journal['provisioning']['result']
    if provisioned is not None:
        items = intent['plan']['provisioning']['items']
        expected = {'schema_version': 1, 'mode': '0700',
                    'created': [i['name'] for i in items if i['state'] == 'missing'],
                    'preserved': [i['name'] for i in items if i['state'] == 'ready'],
                    'items': [{**i, 'state': 'ready'} for i in items]}
        if provisioned != expected or set(journal['provisioning']['created']) != set(expected['created']):
            raise _conflict('Managed provisioning result differs from reviewed effects')
    if journal['phase'] in ('prepared', 'files_published') and journal['git'] is not None:
        raise _conflict('Unexpected managed Git proof')
    if journal['phase'] in ('git_prepared', 'ref_published', 'index_publishing') and journal['git'] is None:
        raise _conflict('Missing managed Git proof')
    if journal['phase'] in ('git_published', 'feed_published', 'binding_published'):
        if bool(intent['plan']['changes']) != (journal['git'] is not None):
            raise _conflict('Managed Git proof does not match reviewed changes')
    if journal['phase'] in ('prepared', 'files_published', 'git_prepared', 'ref_published', 'index_publishing', 'git_published'):
        if journal['feed'] is not None or journal['candidate_sha256'] is not None:
            raise _conflict('Unexpected managed later-stage evidence')
    if journal['phase'] in ('prepared', 'files_published', 'git_prepared', 'ref_published') and journal['index_publication'] is not None:
        raise _conflict('Unexpected managed index evidence')
    if journal['phase'] in ('index_publishing', 'git_published', 'feed_published', 'binding_published'):
        if (journal['git'] is None) != (journal['index_publication'] is None):
            raise _conflict('Managed index evidence differs from Git effects')
    if journal['candidate_sha256'] is not None:
        commit = journal['git']['commit'] if journal['git'] is not None else intent['head']
        _, _, expected_sha = managed_binding.derive(prior_raw, intent, commit)
        if journal['candidate_sha256'] != expected_sha:
            raise _conflict('Managed candidate evidence differs')
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        info = os.stat(path, follow_symlinks=False)
        if {'device': info.st_dev, 'inode': info.st_ino} != intent['plan']['identity'][name]:
            raise _conflict('Managed instance identity changed')
    current = read_binding_bytes(paths)
    if current != prior_raw:
        if journal['phase'] not in ('feed_published', 'binding_published'):
            raise _conflict('Active binding changed before managed publication')
        commit = journal['git']['commit'] if journal['git'] is not None else intent['head']
        _, candidate_raw, candidate_sha = managed_binding.derive(prior_raw, intent, commit)
        if current != candidate_raw or journal['candidate_sha256'] != candidate_sha:
            raise _conflict('Active binding differs from managed attempt')
    _provision_verify(paths, intent['plan'], journal)
    files = managed_files.verify(paths, intent, journal)
    if journal['phase'] != 'prepared' and files != managed_files.target(intent):
        raise _conflict('Published managed files changed')


def _initial_after_intent(paths, intent, prior_raw):
    plan = intent['plan']
    if read_binding_bytes(paths) != prior_raw:
        raise _conflict('Original managed binding changed')
    files, metadata = managed_intent._scan(paths)
    captured = {p: {**managed_intent._item(body), 'mode': metadata[p]['mode'], 'bytes': len(body)}
                for p, body in sorted(files.items())}
    if captured != intent['authored'] or inspect_git(paths.agentic_root, entire_worktree=True) != plan['git']:
        raise _conflict('Initial managed files or Git changed')
    state = inspect_runtime(paths.agentic_runtime, paths.agentic_root, plan['binding']['instance_id'])
    managed_plan._state_evidence(paths, state, files)
    if state != plan['state']:
        raise _conflict('Initial managed runtime changed')
    return _provision_before(paths, plan)


def _check_source_if_present(intent):
    """A removed export is recoverable; a still-present changed export is not."""
    root = intent['plan']['export']['root']
    if not os.path.lexists(root):
        return
    from bazarlyk.source_snapshot import validate_export
    from . import managed_witness
    try:
        manifest, manifest_sha = validate_export(root)
    except (ValueError, OSError) as exc:
        raise _conflict('Managed source export changed after capture') from exc
    raw = managed_intent._decode(intent['export_manifest'])
    if manifest_sha != digest(raw) or manifest != json.loads(raw, object_pairs_hook=_unique):
        raise _conflict('Managed source export changed after capture')
    try:
        managed_witness.recheck(root, manifest, intent['source_witnesses'])
    except ValueError as exc:
        raise _conflict('Managed source observations changed after capture') from exc


def _new_journal(paths, intent, prior_binding, provisioned):
    rootfd = layout_files._open_root(paths.agentic_root)
    try:
        root = layout_files._identity(os.fstat(rootfd))
        gitdir, head = layout_files._head_identity(rootfd)
        dirs = layout_files._directories(rootfd)
    finally:
        os.close(rootfd)
    return {'schema_version': 1, 'phase': 'prepared', 'plan_digest': intent['plan']['plan_digest'],
            'intent_sha256': intent['receipt_sha256'], 'attribution': intent['attribution'],
            'prior_binding_sha256': intent['prior_binding_sha256'], 'prior_binding': prior_binding,
            'root': root, 'gitdir': gitdir, 'head_identity': head, 'initial_dirs': dirs, 'created_dirs': {},
            'git': None, 'index_publication': None, 'provisioning': provisioned, 'feed': None,
            'candidate_sha256': None, 'result': None, 'receipt_sha256': None}


def apply(paths, plan, *, attribution, acknowledge_grants=False):
    from bazarlyk.posix_files import require_anonymous_publication
    require_anonymous_publication()
    """Apply only offline: the caller owns the instance exclusively for this call."""
    managed_plan.validate_plan(paths, plan)
    wanted = managed_intent._retained_plan(plan)
    attr = normalize_attribution(attribution, trust='host_installer')
    if type(acknowledge_grants) is not bool or acknowledge_grants != wanted['acknowledge_grants']:
        raise _conflict('Managed grant acknowledgement changed')
    pd = wanted['plan_digest']
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        evidence = managed_journal.discover(paths)
        pair = evidence.get(pd)
        if pair and 'journal' in pair and managed_journal.read(pair['journal'])['phase'] == 'complete':
            from .installation import _validated_installed_binding
            with _validated_installed_binding(paths, timeout=10, offline_read_only=True) as current:
                member = False
                node = current
                for _ in range(128):
                    metadata = node.get('managed_upgrade')
                    if metadata is None:
                        break
                    if metadata['plan_digest'] == pd:
                        member = True
                        break
                    prior = read_private_file(history_snapshot(paths, metadata['prior_binding_sha256']), max_bytes=MAX_RECORD)
                    if digest(prior) != metadata['prior_binding_sha256']:
                        raise _conflict('Managed prior binding changed')
                    node = json.loads(prior, object_pairs_hook=_unique)
                if not member:
                    raise _conflict('Managed completion does not belong to binding ancestry')
            journal = managed_journal.read(pair['journal'])
            if journal['attribution'] != attr:
                raise _conflict('Managed replay attribution changed')
            prior_raw = read_private_file(history_snapshot(paths, journal['prior_binding_sha256']), max_bytes=MAX_RECORD)
            intent_raw = read_private_file(pair['intent'], max_bytes=MAX_INTENT_BYTES)
            intent = managed_intent._read_retained(paths, intent_raw, prior_raw)
            if intent['plan'] != wanted:
                raise _conflict('Managed replay plan changed')
            return _receipt(journal['result'], _proof(paths, wanted, intent_raw, pair['journal']), worked=False, recovered=False, replayed=True)
        if any(key != pd for key in evidence if 'journal' not in evidence[key] or managed_journal.read(evidence[key]['journal'])['phase'] != 'complete'):
            raise _conflict('Another managed operation is incomplete')
        recovered = pair is not None
        if pair is None:
            intent = managed_intent.capture(paths, plan, attribution=attribution)
            if intent is None:
                return _receipt({'schema_version': 2, 'kind': 'agentic_managed_update_v2', 'plan_digest': pd,
                                 'no_change': True, 'upgrade_commit': wanted['git']['head'],
                                 'sha256': wanted['binding']['record_sha256'], 'packages_updated': [],
                                 'provisioning': {'schema_version': 1, 'mode': '0700', 'created': [], 'preserved': [], 'items': []},
                                 'prior_binding': None, 'change_id': None, 'sequence': None}, None, worked=False, recovered=False, replayed=False)
            prior_raw = read_binding_bytes(paths)
            before = _provision_before(paths, wanted)
            retained = retain_prior_binding(paths, prior_raw, intent['prior_binding_sha256'])
            intent_raw = canonical(intent) + b'\n'
            _publish_intent(Path(wanted['evidence']['intent']), intent_raw)
            _fault_hook('intent_published')
            journal_path = Path(wanted['evidence']['journal'])
            journal = _new_journal(paths, intent, retained, before)
            managed_journal.write(journal_path, journal)
        else:
            intent_raw = read_private_file(pair['intent'], max_bytes=MAX_INTENT_BYTES)
            prior_raw = read_private_file(history_snapshot(paths, wanted['binding']['record_sha256']), max_bytes=MAX_RECORD)
            intent = managed_intent._read_retained(paths, intent_raw, prior_raw)
            if intent['plan'] != wanted or intent['attribution'] != attr:
                raise _conflict('Managed resume plan or attribution changed')
            _check_source_if_present(intent)
            prior = json.loads(prior_raw, object_pairs_hook=_unique)
            layout, verified = _layout_proof(paths)
            managed_ancestry._verify_prior_for_resume(paths, prior, prior_raw, pd, layout_intents=layout, layout_verified=verified)
            journal_path = Path(wanted['evidence']['journal'])
            if 'journal' not in pair:
                before = _initial_after_intent(paths, intent, prior_raw)
                retained = {'snapshot': str(history_snapshot(paths, intent['prior_binding_sha256'])), 'sha256': intent['prior_binding_sha256'], 'created': False}
                journal = _new_journal(paths, intent, retained, before)
                managed_journal.write(journal_path, journal)
            else:
                journal = managed_journal.read(journal_path)
        prior = json.loads(prior_raw, object_pairs_hook=_unique)
        saved = [deepcopy(journal)]
        def save():
            if managed_journal.read(journal_path) != saved[0]:
                raise _conflict('Managed journal changed during application')
            managed_journal.write(journal_path, journal)
            saved[0] = deepcopy(journal)
        _common(paths, intent, journal, prior_raw)
        if journal['git'] is not None:
            managed_git.verify(paths, intent, journal['git'], history_directory(paths))
        _check_git(paths, intent, journal, allow_index_lock=journal['phase'] == 'index_publishing')
        if journal['phase'] == 'prepared':
            _check_runtime(paths, intent, after=False, commit=None)
            _provision(paths, intent, journal, save)
            def file_hook(stage, **kwargs):
                _fault_hook(stage, **kwargs)
                if stage == 'before_file':
                    _common(paths, intent, journal, prior_raw)
            managed_files.publish(paths, intent, journal, save, file_hook)
            journal['phase'] = 'files_published'
            save()
        _common(paths, intent, journal, prior_raw)
        if journal['phase'] == 'files_published':
            _check_runtime(paths, intent, after=False, commit=None)
            _check_git(paths, intent, journal)
            if intent['plan']['changes']:
                journal['git'] = managed_git.prepare(paths, intent, history_directory(paths))
                journal['phase'] = 'git_prepared'
            else:
                journal['phase'] = 'git_published'
            save()
        if journal['phase'] == 'git_prepared':
            _common(paths, intent, journal, prior_raw)
            _check_runtime(paths, intent, after=False, commit=None)
            head, index = _check_git(paths, intent, journal)
            if head == intent['head']:
                runner = layout_git._runner(paths.agentic_root, history_directory(paths))
                layout_git._run(runner, ['update-ref', intent['plan']['git']['branch'], journal['git']['commit'], intent['head']])
                _fault_hook('ref_published')
            elif head != journal['git']['commit']:
                raise _conflict('Unexpected managed Git ref')
            journal['phase'] = 'ref_published'
            save()
        if journal['phase'] in ('ref_published', 'index_publishing'):
            _common(paths, intent, journal, prior_raw)
            _check_runtime(paths, intent, after=False, commit=None)
            original = base64.b64decode(intent['index']['content_base64'], validate=True)
            final = managed_git.verify(paths, intent, journal['git'], history_directory(paths))
            gitfd = os.open(paths.agentic_root / '.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                def check(lockfd, *, expected):
                    if managed_journal.read(journal_path) != journal:
                        raise _conflict('Managed journal changed during Git index publication')
                    _common(paths, intent, journal, prior_raw)
                    head, _ = _check_git(paths, intent, journal, allow_index_lock=lockfd is not None)
                    if head != journal['git']['commit']:
                        raise _conflict('Managed ref changed during index publication')
                    _check_runtime(paths, intent, after=False, commit=None)
                layout_git._index_effect(gitfd, journal_path, journal, original, final, check)
                saved[0] = deepcopy(journal)
            finally:
                os.close(gitfd)
        commit = journal['git']['commit'] if journal['git'] else intent['head']
        if journal['phase'] == 'git_published':
            _common(paths, intent, journal, prior_raw)
            head, _ = _check_git(paths, intent, journal)
            if head != commit:
                raise _conflict('Managed Git ref is not final')
            if intent['plan']['changes']:
                journal['feed'] = managed_feed.apply(paths, intent, commit)
                _fault_hook('feed_published')
            else:
                _check_runtime(paths, intent, after=False, commit=None)
            journal['phase'] = 'feed_published'
            save()
        if journal['phase'] in ('feed_published', 'binding_published'):
            _common(paths, intent, journal, prior_raw)
            head, _ = _check_git(paths, intent, journal)
            if head != commit:
                raise _conflict('Managed Git ref changed before binding')
            event = _check_runtime(paths, intent, after=bool(intent['plan']['changes']), commit=commit)
            if event != journal['feed']:
                raise _conflict('Managed feed journal differs')
            candidate, raw, sha = managed_binding.derive(prior_raw, intent, commit)
            current = read_binding_bytes(paths)
            if current not in (prior_raw, raw):
                raise _conflict('Managed binding changed before publication')
            journal['candidate_sha256'] = sha
            save()
            if current == prior_raw:
                from .upgrade import _write_private_temp, _replace
                directory = require_private_directory(paths.instance_root / 'config')
                temporary = _write_private_temp(directory, raw)
                try:
                    if read_binding_bytes(paths) != prior_raw:
                        raise _conflict('Managed binding changed during publication')
                    _replace(temporary, directory / 'agentic-layer.json')
                    fsync_directory(directory)
                finally:
                    temporary.unlink(missing_ok=True)
                _fault_hook('binding_published')
            journal['phase'] = 'binding_published'
            save()
            base = {'schema_version': 2, 'kind': 'agentic_managed_update_v2', 'plan_digest': pd,
                    'no_change': False, 'upgrade_commit': commit, 'sha256': sha, 'packages_updated': _packages(prior, wanted),
                    'provisioning': journal['provisioning']['result'], 'prior_binding': journal['prior_binding'],
                    'change_id': event['change_id'] if event else None, 'sequence': event['sequence'] if event else None}
            journal['result'] = base
            journal['phase'] = 'complete'
            save()
        if journal['phase'] != 'complete':
            raise _conflict('Unknown managed recovery stage')
        open_installation(paths, tuple(wanted['selected_extensions']))
        return _receipt(journal['result'], _proof(paths, wanted, intent_raw, journal_path), worked=True, recovered=recovered, replayed=False)
