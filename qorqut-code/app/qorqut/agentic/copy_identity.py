"""Explicit installer receipt for a copy retaining its canonical instance paths.

Only completed, verified source histories can be authorized. This receipt binds
their exact intent hashes to the copied directories; it neither changes retained
evidence nor relaxes identity checks for an unfinished update. All content, Git,
runtime and ancestry checks still run normally when the copy is opened.
"""
from pathlib import Path
import json
import os
import stat

from qorqut.private_files import read_private_file, write_private_file
from .support import canonical

RECEIPT = 'agentic-copy-identity.json'


def identities(root, state):
    result = {}
    for name, path in (('root', root), ('state', state), ('git', root / '.git')):
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Copied Agentic directories must be absolute and not symlinks')
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('Copied Agentic directories must be owned by the installer')
        result[name] = {'device': info.st_dev, 'inode': info.st_ino,
                        'mode': stat.S_IMODE(info.st_mode)}
    return result


def authorize_copy(source_paths, destination_root):
    """Verify the source read-only and exclusively create the destination receipt.

    The service must subsequently see the copy at the source's canonical paths
    (for example through an isolated mount namespace). Ordinary path relocation
    and copying an interrupted update are deliberately unsupported.
    """
    from .installation import _validated_installed_binding, MAX_RECORD, RECORD
    from . import managed_journal

    destination = Path(destination_root)
    root = destination / source_paths.agentic_root.relative_to(source_paths.instance_root)
    state = destination / source_paths.agentic_runtime.relative_to(source_paths.instance_root)
    source_identity = identities(source_paths.agentic_root, source_paths.agentic_runtime)
    target_identity = identities(root, state)
    if source_identity == target_identity:
        raise ValueError('Destination must be a separate copy')
    # Normal readers use SQLite's read-only WAL transactions, so the original
    # service can remain online while its completed history is verified.
    with _validated_installed_binding(source_paths, timeout=30) as record:
        before = read_private_file(source_paths.instance_root / 'config' / RECORD, max_bytes=MAX_RECORD)
        if read_private_file(destination / 'config' / RECORD, max_bytes=MAX_RECORD) != before:
            raise ValueError('Copied binding differs from the verified source')
        nodes = {}
        for plan, pair in managed_journal.discover(source_paths).items():
            journal = managed_journal.read(pair['journal'])
            if journal['phase'] != 'complete':
                raise ValueError('An unfinished update cannot be copied')
            nodes[plan] = journal['intent_sha256']
        if identities(source_paths.agentic_root, source_paths.agentic_runtime) != source_identity:
            raise ValueError('Source directories changed during verification')
        receipt = {'version': 1, 'instance_id': record['instance_id'],
                   'canonical_root': str(source_paths.instance_root),
                   'source': source_identity, 'destination': target_identity, 'nodes': nodes}
        write_private_file(destination / 'config', RECEIPT, canonical(receipt) + b'\n')
    return {'authorized_completed_updates': len(nodes), 'instance_id': record['instance_id']}


def accepts(paths, record, intent, journal):
    """An explicit receipt authorizes only its exact completed source identities."""
    path = paths.instance_root / 'config' / RECEIPT
    if not os.path.lexists(path):
        return False
    receipt = json.loads(read_private_file(path, max_bytes=65536))
    if (set(receipt) != {'version', 'instance_id', 'canonical_root', 'source', 'destination', 'nodes'}
            or receipt['version'] != 1 or receipt['instance_id'] != record['instance_id']
            or receipt['canonical_root'] != str(paths.instance_root)
            or journal['phase'] != 'complete'
            or receipt['nodes'].get(intent['plan']['plan_digest']) != intent['receipt_sha256']
            or receipt['destination'] != identities(paths.agentic_root, paths.agentic_runtime)):
        return False
    source = receipt['source']
    return (set(source) == {'root', 'state', 'git'}
            and journal['root'] == source['root'] and journal['gitdir'] == source['git']
            and all(intent['plan']['identity'][name] == {k: source[name][k] for k in ('device', 'inode')}
                    for name in ('root', 'state')))
