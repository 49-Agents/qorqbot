"""Read-only verification of managed binding ancestry and completion evidence."""
from __future__ import annotations

import base64
import json
import os
import re

from qorqut import file_identity
from qorqut.private_files import read_private_file
from qorqut_agentic.store import Store, _verify_layout
from qorqut_agentic.changes import package_ids_of
from . import managed_binding, managed_feed, managed_files, managed_git, managed_intent, managed_journal, managed_verified
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


def _metadata(record):
    metadata = record.get('managed_upgrade')
    if (not isinstance(metadata, dict) or set(metadata) != {'format_version', 'plan_digest', 'prior_binding_sha256',
                                                           'intent_sha256', 'resulting_commit'}
            or type(metadata['format_version']) is not int or metadata['format_version'] != 1
            or any(not isinstance(metadata[k], str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[k])
                   for k in ('plan_digest', 'prior_binding_sha256', 'intent_sha256'))
            or not isinstance(metadata['resulting_commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', metadata['resulting_commit'])):
        raise _conflict('Malformed managed binding metadata')
    return metadata


def _read_node(paths, metadata, pair):
    """The exact retained bytes of one node: prior binding, intent and journal."""
    prior_raw = read_private_file(history_snapshot(paths, metadata['prior_binding_sha256']), max_bytes=MAX_RECORD)
    if digest(prior_raw) != metadata['prior_binding_sha256']:
        raise _conflict('Managed prior binding changed')
    intent_raw = read_private_file(pair['intent'], max_bytes=MAX_INTENT_BYTES)
    journal_raw, journal = managed_journal.read_raw(pair['journal'])
    return prior_raw, intent_raw, journal_raw, journal


def _check_identities(paths, record, intent, journal):
    """Current directories and Git match the retained identities, or an explicit copy receipt accepts them.

    ``intent`` needs only ``plan.plan_digest``, ``plan.identity`` and ``receipt_sha256``.
    """
    from .copy_identity import accepts
    copied = accepts(paths, record, intent, journal)
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        info = os.stat(path, follow_symlinks=False)
        if not copied and not file_identity.matches(intent['plan']['identity'][name], {'device': info.st_dev, 'inode': info.st_ino},
                                                    file_identity.parent_volume(path)):
            raise _conflict('Managed instance directory identity changed')
    from . import layout_files
    rootfd = layout_files._open_root(paths.agentic_root)
    try:
        gitdir, _ = layout_files._head_identity(rootfd)
        root = os.fstat(rootfd)
        if not copied and (not file_identity.matches(journal['gitdir'], gitdir, root.st_dev)
                or not file_identity.matches(journal['root'], layout_files._identity(root), file_identity.fd_parent_volume(rootfd))):
            raise _conflict('Managed Git or authored root identity changed')
    finally:
        os.close(rootfd)


def _check_ancestor(paths, commit):
    runner = layout_git._runner(paths.agentic_root, history_directory(paths))
    if runner.run(['merge-base', '--is-ancestor', commit, 'HEAD'], check=False,
                  env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30).returncode != 0:
        raise _conflict('Managed completion commit is not in current Git ancestry')


def _load_node(paths, record, evidence, *, offline_read_only=False, _read=None):
    metadata = _metadata(record)
    pd = metadata['plan_digest']
    pair = evidence.get(pd)
    if not pair or set(pair) != {'intent', 'journal'}:
        raise _conflict('Managed completion evidence is missing')
    prior_raw, intent_raw, _, journal = _read if _read is not None else _read_node(paths, metadata, pair)
    intent = managed_intent._read_retained(paths, intent_raw, prior_raw)
    _check_identities(paths, record, intent, journal)
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
    _check_ancestor(paths, commit)
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


def _load_known_node(paths, record, metadata, journal, known):
    """Current-state checks for a node whose exact retained bytes already passed ``_load_node``.

    The skipped re-derivation depends only on those bytes. Everything that can change without them changing is
    checked again here exactly as ``_load_node`` checks it: directory and Git identities (or the copy receipt), the
    feed event, and that the resulting commit is still in the current Git ancestry.
    """
    pd = metadata['plan_digest']
    if (known['plan_digest'] != pd or known['receipt_sha256'] != metadata['intent_sha256']
            or journal['phase'] != 'complete' or journal['plan_digest'] != pd
            or journal['intent_sha256'] != known['receipt_sha256']
            or journal['prior_binding_sha256'] != metadata['prior_binding_sha256']):
        raise _conflict('Managed completion evidence differs')
    view = {'plan': {'plan_digest': pd, 'identity': known['identity']}, 'receipt_sha256': known['receipt_sha256']}
    _check_identities(paths, record, view, journal)
    commit = metadata['resulting_commit']
    if known['changes']:
        if journal['git'] is None or journal['git']['commit'] != commit or journal['feed'] is None:
            raise _conflict('Managed Git proof is missing')
        store = Store(paths.agentic_runtime)
        with store.reader() as conn:
            conn.execute('BEGIN')
            _verify_layout(conn, '3')
            managed_feed.verify_recorded_event(conn, journal['feed'])
    elif journal['git'] is not None or journal['feed'] is not None or journal['index_publication'] is not None:
        raise _conflict('Managed binding-only completion has file effects')
    _check_ancestor(paths, commit)
    return True


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
    # Offline and read-only callers prove everything afresh and leave no cache behind.
    known = None if offline_read_only else managed_verified.load(paths)
    if known is not None and 'managed_upgrade' in record:
        # A remembered proof covers the installed bytes; the record parsed from them must be exactly those bytes,
        # as the full proof's candidate comparison would require.
        try:
            same = json.loads(raw, object_pairs_hook=_unique) == record
        except (TypeError, ValueError):
            same = False
        if not same:
            raise _conflict('Managed candidate bytes differ')
    partial, checked_hash = False, None
    while 'managed_upgrade' in record:
        if len(hashes) >= _LIMIT:
            raise _conflict('Managed ancestry exceeds its bound')
        # After the first node, raw is a prior binding whose bytes were already checked against this digest.
        current_hash = checked_hash or digest(raw)
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
        if known is None:
            prior_raw, intent, journal, candidate_raw = _load_node(paths, record, evidence, offline_read_only=offline_read_only)
            if candidate_raw != raw:
                raise _conflict('Managed candidate bytes differ')
            record = json.loads(prior_raw, object_pairs_hook=_unique)
        else:
            node_metadata = _metadata(record)
            read = _read_node(paths, node_metadata, pair)
            prior_raw, intent_raw, journal_raw, journal = read
            try:
                # _read_node checked the prior bytes against this digest.
                key = managed_verified.node_key(paths, current_hash, node_metadata['prior_binding_sha256'], intent_raw, journal_raw)
            except Exception:
                key = None
            entry = known.get(key) if key is not None else None
            try:
                hit = entry is not None and _load_known_node(paths, record, node_metadata, journal, entry)
            except Exception:
                # The full proof below decides: it raises the real failure, or replaces a bad entry.
                hit = False
            if hit:
                # Only the fields the walk reads; the base binding is parsed whole after the loop.
                record, partial = dict(entry['prior']), True
            else:
                if partial:
                    record = json.loads(raw, object_pairs_hook=_unique)
                _, intent, _, candidate_raw = _load_node(paths, record, evidence, _read=read)
                if candidate_raw != raw:
                    raise _conflict('Managed candidate bytes differ')
                record, partial = json.loads(prior_raw, object_pairs_hook=_unique), False
                try:
                    fact = None if key is None else managed_verified.facts(intent, record)
                except Exception:
                    fact = None
                if fact is not None:
                    known[key] = fact
                    managed_verified.remember(paths, key, fact)
        used.add(pd)
        raw, checked_hash = prior_raw, metadata['prior_binding_sha256']
    if partial:
        record = json.loads(raw, object_pairs_hook=_unique)
    if set(evidence) != used:
        raise _conflict('Unfinished or orphan managed update requires explicit offline resume')
    _identity(paths, record, initial)
    candidate = _validate_completed_record(paths, record, raw, _intents=layout_intents,
                                           _verified=layout_verified, offline_read_only=offline_read_only)
    _validate_binding(paths, record, _verified_candidate=candidate, _historical=True)
    return used
