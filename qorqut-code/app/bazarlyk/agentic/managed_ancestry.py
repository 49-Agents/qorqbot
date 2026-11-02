"""Read-only verification of managed binding ancestry and completion evidence."""
from __future__ import annotations

import base64
import json
import os
import re

from bazarlyk.private_files import read_private_file
from qorqut_agentic.store import Store, _verify_layout
from qorqut_agentic.changes import package_ids_of
from . import managed_binding, managed_feed, managed_files, managed_git, managed_intent, managed_journal
from . import layout_git
from .installation import MAX_RECORD, _validate_binding
from .layout_activation import _validate_completed_record
from .layout_intent import MAX_INTENT_BYTES, _conflict
from .support import digest
from .upgrade import _installed_selection, _unique, history_directory, history_snapshot

_LIMIT = 128


def _identity(paths, record, expected):
    for key in ('instance_id', 'root', 'state', 'initial_commit'):
        if record.get(key) != expected.get(key):
            raise _conflict('Managed ancestry changes instance identity')
    # Each retained intent validates an exact additive selection; selection is not an immutable instance identity.
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            raise _conflict('Unsafe managed instance identity')


def _load_node(paths, record, evidence, *, offline_read_only=False):
    metadata = record.get('managed_upgrade')
    if (not isinstance(metadata, dict) or set(metadata) != {'format_version', 'plan_digest', 'prior_binding_sha256',
                                                           'intent_sha256', 'resulting_commit'}
            or type(metadata['format_version']) is not int or metadata['format_version'] != 1
            or any(not isinstance(metadata[k], str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[k])
                   for k in ('plan_digest', 'prior_binding_sha256', 'intent_sha256'))
            or not isinstance(metadata['resulting_commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', metadata['resulting_commit'])):
        raise _conflict('Malformed managed binding metadata')
    pd = metadata['plan_digest']
    pair = evidence.get(pd)
    if not pair or set(pair) != {'intent', 'journal'}:
        raise _conflict('Managed completion evidence is missing')
    prior_raw = read_private_file(history_snapshot(paths, metadata['prior_binding_sha256']), max_bytes=MAX_RECORD)
    if digest(prior_raw) != metadata['prior_binding_sha256']:
        raise _conflict('Managed prior binding changed')
    intent_raw = read_private_file(pair['intent'], max_bytes=MAX_INTENT_BYTES)
    intent = managed_intent._read_retained(paths, intent_raw, prior_raw)
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        info = os.stat(path, follow_symlinks=False)
        if {'device': info.st_dev, 'inode': info.st_ino} != intent['plan']['identity'][name]:
            raise _conflict('Managed instance directory identity changed')
    journal = managed_journal.read(pair['journal'])
    from . import layout_files
    rootfd = layout_files._open_root(paths.agentic_root)
    try:
        gitdir, _ = layout_files._head_identity(rootfd)
        if gitdir != journal['gitdir'] or layout_files._identity(os.fstat(rootfd)) != journal['root']:
            raise _conflict('Managed Git or authored root identity changed')
    finally:
        os.close(rootfd)
    if (intent['receipt_sha256'] != metadata['intent_sha256'] or intent['plan']['plan_digest'] != pd
            or journal['phase'] != 'complete' or journal['plan_digest'] != pd
            or journal['intent_sha256'] != intent['receipt_sha256']
            or journal['prior_binding_sha256'] != metadata['prior_binding_sha256']
            or journal['attribution'] != intent['attribution']):
        raise _conflict('Managed completion evidence differs')
    expected_head = base64.b64encode(('ref: ' + intent['plan']['git']['branch'] + '\n').encode()).decode('ascii')
    if journal['head_identity'] != expected_head:
        raise _conflict('Managed symbolic HEAD evidence differs')
    if not set(journal['created_dirs']) <= managed_files.required_dirs(intent) - journal['initial_dirs'].keys():
        raise _conflict('Managed authored directory evidence differs')
    commit = metadata['resulting_commit']
    if intent['plan']['changes']:
        if journal['git'] is None or journal['git']['commit'] != commit:
            raise _conflict('Managed Git proof is missing')
        managed_git.verify(paths, intent, journal['git'], history_directory(paths))
        publication = journal['index_publication']
        if (not isinstance(publication, dict) or set(publication) != {'original', 'replacement'}
                or not isinstance(publication['original'], dict) or not isinstance(publication['replacement'], dict)
                or set(publication['original']) != {'device', 'inode', 'mode', 'size', 'mtime_ns', 'ctime_ns'}
                or set(publication['replacement']) != {'device', 'inode', 'mode', 'size', 'mtime_ns', 'sha256'}
                or publication['replacement'].get('sha256') != journal['git']['final_index']['sha256']):
            raise _conflict('Managed index publication evidence differs')
        if (any(type(v) is not int or v < 0 for v in publication['original'].values())
                or any(type(v) is not int or v < 0 for k, v in publication['replacement'].items() if k != 'sha256')
                or publication['original']['size'] != len(base64.b64decode(intent['index']['content_base64'], validate=True))
                or publication['replacement']['size'] != len(base64.b64decode(journal['git']['final_index']['content_base64'], validate=True))
                or publication['original']['mode'] != publication['replacement']['mode']):
            raise _conflict('Malformed managed index publication identity')
        from .layout_runtime import _offline_reader
        store = Store(paths.agentic_runtime)
        with (_offline_reader(store) if offline_read_only else store.reader()) as conn:
            conn.execute('BEGIN')
            _verify_layout(conn, '3')
            event = managed_feed.verify_event(conn, intent, commit)
        if journal['feed'] != event:
            raise _conflict('Managed event receipt differs')
    elif commit != intent['head'] or journal['git'] is not None or journal['feed'] is not None or journal['index_publication'] is not None:
        raise _conflict('Managed binding-only completion has file effects')
    runner = layout_git._runner(paths.agentic_root, history_directory(paths))
    if runner.run(['merge-base', '--is-ancestor', commit, 'HEAD'], check=False,
                  env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30).returncode != 0:
        raise _conflict('Managed completion commit is not in current Git ancestry')
    candidate, candidate_raw, candidate_sha = managed_binding.derive(prior_raw, intent, commit)
    if record != candidate or journal['candidate_sha256'] != candidate_sha:
        raise _conflict('Managed candidate differs from retained proof')
    prior = json.loads(prior_raw, object_pairs_hook=_unique)
    provision = journal['provisioning']
    items = intent['plan']['provisioning']['items']
    expected_provision = {'schema_version': 1, 'mode': '0700',
                          'created': [i['name'] for i in items if i['state'] == 'missing'],
                          'preserved': [i['name'] for i in items if i['state'] == 'ready'],
                          'items': [{**i, 'state': 'ready'} for i in items]}
    retained = journal['prior_binding']
    if (not isinstance(provision, dict) or set(provision) != {'original', 'created', 'pending', 'result'}
            or provision['pending'] is not None
            or set(provision['created']) != set(expected_provision['created'])
            or provision['result'] != expected_provision or not isinstance(retained, dict)
            or set(retained) != {'snapshot', 'sha256', 'created'} or type(retained['created']) is not bool
            or retained['snapshot'] != str(history_snapshot(paths, metadata['prior_binding_sha256']))
            or retained['sha256'] != metadata['prior_binding_sha256']):
        raise _conflict('Managed retained or provisioning evidence differs')
    if set(provision['original']) != {i['name'] for i in items}:
        raise _conflict('Managed provisioning origin evidence differs')
    for item in items:
        origin = provision['original'][item['name']]
        if item['state'] == 'missing':
            if origin is not None:
                raise _conflict('Invalid original provisioning identity')
            identity = provision['created'][item['name']]
        else:
            identity = origin
        if (not isinstance(identity, dict) or set(identity) != {'device', 'inode', 'mode', 'uid'}
                or any(type(v) is not int or v < 0 for v in identity.values())):
            raise _conflict('Invalid managed provisioning identity')
    old = {p['id']: p for p in prior['inventory']['packages']}
    new = {p['id']: p for p in intent['plan']['target_inventory']['packages']}
    packages = sorted({p for p in old.keys() | new.keys() if old.get(p) != new.get(p)} |
                      set(package_ids_of([c['path'] for c in intent['plan']['changes']])))
    event = journal['feed']
    expected_result = {'schema_version': 2, 'kind': 'agentic_managed_update_v2', 'plan_digest': pd,
                       'no_change': False, 'upgrade_commit': commit, 'sha256': candidate_sha,
                       'packages_updated': packages, 'provisioning': expected_provision, 'prior_binding': retained,
                       'change_id': event['change_id'] if event else None, 'sequence': event['sequence'] if event else None}
    if journal['result'] != expected_result:
        raise _conflict('Managed completion result differs')
    return prior_raw, intent, journal, candidate_raw


def verify(paths, record, raw, *, layout_intents, layout_verified=None, offline_read_only=False):
    """Verify every complete node and reject orphan or incomplete retained operations."""
    evidence = managed_journal.discover(paths)
    return _verify_chain(paths, record, raw, evidence, layout_intents, layout_verified, offline_read_only)


def _verify_prior_for_resume(paths, record, raw, pending_digest, *, layout_intents, layout_verified=None):
    evidence = managed_journal.discover(paths)
    if pending_digest not in evidence:
        raise _conflict('Managed pending intent is missing')
    pair = evidence.pop(pending_digest)
    if 'journal' in pair and managed_journal.read(pair['journal'])['phase'] == 'complete':
        raise _conflict('Managed completion cannot be treated as an unfinished attempt')
    return _verify_chain(paths, record, raw, evidence, layout_intents, layout_verified, True)


def _verify_chain(paths, record, raw, evidence, layout_intents, layout_verified, offline_read_only):
    _installed_selection(record)
    used, hashes, initial = set(), set(), record
    from .layout_runtime import _offline_reader
    store = Store(paths.agentic_runtime)
    with (_offline_reader(store) if offline_read_only else store.reader()) as conn:
        conn.execute('BEGIN')
        _verify_layout(conn, '3')
        meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','root_path','instance_id','initialized_at')"))
        if (meta.get('schema_version') != '3' or meta.get('root_path') != str(paths.agentic_root)
                or meta.get('instance_id') != initial.get('instance_id') or not meta.get('initialized_at')):
            raise _conflict('Managed runtime identity differs')
    cumulative = len(raw)
    while 'managed_upgrade' in record:
        if len(hashes) >= _LIMIT:
            raise _conflict('Managed ancestry exceeds its bound')
        current_hash = digest(raw)
        if current_hash in hashes:
            raise _conflict('Managed ancestry contains a cycle')
        hashes.add(current_hash)
        _identity(paths, record, initial)
        pd = record['managed_upgrade'].get('plan_digest') if isinstance(record['managed_upgrade'], dict) else None
        if not isinstance(pd, str) or not re.fullmatch(r'[0-9a-f]{64}', pd):
            raise _conflict('Malformed managed ancestry plan digest')
        if pd in used:
            raise _conflict('Duplicate managed ancestry node')
        metadata = record['managed_upgrade']
        pair = evidence.get(pd, {})
        if (not isinstance(metadata, dict) or not isinstance(metadata.get('prior_binding_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', metadata['prior_binding_sha256'])
                or set(pair) != {'intent', 'journal'}):
            raise _conflict('Managed ancestry evidence is missing or malformed')
        for path in (pair['intent'], pair['journal'], history_snapshot(paths, metadata['prior_binding_sha256'])):
            try:
                cumulative += os.stat(path, follow_symlinks=False).st_size
            except OSError as exc:
                raise _conflict('Managed ancestry evidence is missing') from exc
            if cumulative > MAX_INTENT_BYTES:
                raise _conflict('Managed ancestry exceeds cumulative evidence bound')
        prior_raw, intent, journal, candidate_raw = _load_node(paths, record, evidence, offline_read_only=offline_read_only)
        if candidate_raw != raw:
            raise _conflict('Managed candidate bytes differ')
        used.add(pd)
        record = json.loads(prior_raw, object_pairs_hook=_unique)
        raw = prior_raw
    if set(evidence) != used:
        raise _conflict('Unfinished or orphan managed update requires explicit offline resume')
    _identity(paths, record, initial)
    candidate = _validate_completed_record(paths, record, raw, _intents=layout_intents,
                                           _verified=layout_verified, offline_read_only=offline_read_only)
    _validate_binding(paths, record, _verified_candidate=candidate, _historical=True)
    return used
