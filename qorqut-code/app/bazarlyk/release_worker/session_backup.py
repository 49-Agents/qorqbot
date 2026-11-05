"""Verify the one approved Linux NFSv4 backup binding without mounting anything."""
from pathlib import Path
import hashlib
import ipaddress
import json
import os
import re
import socket
import uuid

from .errors import ReleaseError
from .io import atomic_json


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.islink(path) or not os.path.isfile(path):
        raise ReleaseError(f'missing absolute regular binding record: {path}')
    if Path(path).resolve() != Path(path):
        raise ReleaseError(f'binding record has an aliased ancestor: {path}')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mount_observations():
    result = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            pre, post = line.rstrip().split(' - ', 1)
            a, b = pre.split(), post.split()
            result.append({'mount_id': a[0], 'root': _unescape(a[3]), 'mountpoint': _unescape(a[4]),
                           'options': a[5].split(',') + b[2].split(','), 'filesystem': b[0],
                           'source': _unescape(b[1])})
    return result


def local_addresses():
    """All Linux interface addresses, including non-loopback IP aliases of this host."""
    try:
        lines = Path('/proc/net/fib_trie').read_text().splitlines()
        addresses = {str(ipaddress.ip_address(lines[i - 1].split()[-1]))
                     for i, line in enumerate(lines) if i and '/32 host LOCAL' in line}
        addresses.update(str(ipaddress.IPv6Address(int(line.split()[0], 16)))
                         for line in Path('/proc/net/if_inet6').read_text().splitlines())
        return addresses
    except (OSError, ValueError, IndexError):
        raise ReleaseError('local network identity is unavailable; cannot verify independent backup') from None


def local_server(server):
    name = server.strip('[]').lower().rstrip('.')
    if name in {'localhost', socket.gethostname().lower(), socket.getfqdn().lower()} or name.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(name)
        if address.is_loopback or address.is_unspecified:
            return True
    except ValueError:
        pass
    try:
        remote = {x[4][0] for x in socket.getaddrinfo(name, None)}
        local = local_addresses() | {x[4][0] for host in {socket.gethostname(), socket.getfqdn(), 'localhost'}
                 for x in socket.getaddrinfo(host, None)}
        return bool(remote & local) or any(ipaddress.ip_address(x).is_loopback for x in remote)
    except socket.gaierror:
        raise ReleaseError('backup server identity cannot be resolved') from None


def verify_backup_binding(config, *, _provision_receipt=False):
    required = {'destination_id', 'mountpoint', 'dataset_root', 'dataset_id', 'server', 'export', 'nfs_version',
                'verification_path', 'verification_sha256', 'plan_digest', 'receipt'}
    if not isinstance(config, dict) or not required <= set(config):
        raise ReleaseError('backup binding/verification is missing')
    if (not isinstance(config['server'], str) or not config['server'] or
            not isinstance(config['export'], str) or not config['export'].startswith('/') or
            not isinstance(config['nfs_version'], str) or not re.fullmatch(r'4(?:\.[012])?', config['nfs_version'])):
        raise ReleaseError('backup must identify the selected server/export and NFSv4 version')
    for name in ('mountpoint', 'dataset_root'):
        path = config[name]
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path or not os.path.isdir(path):
            raise ReleaseError(f'backup {name} is missing or aliased; no local fallback')
    mountpoint, root = config['mountpoint'], config['dataset_root']
    if not root.startswith(mountpoint.rstrip('/') + '/') or mountpoint == '/':
        raise ReleaseError('backup dataset must be inside its selected remote mount')
    if local_server(config['server']):
        raise ReleaseError('local/loopback backup server cannot protect host loss')
    candidates = [m for m in mount_observations() if m['mountpoint'] == mountpoint]
    if len(candidates) != 1:
        raise ReleaseError('approved backup mount is unavailable or ambiguous')
    mounted = candidates[0]
    expected_source = config['server'] + ':' + config['export']
    versions = [x.split('=', 1)[1] for x in mounted['options'] if x.startswith(('vers=', 'nfsvers='))]
    version = versions[-1] if versions else '4' if mounted['filesystem'] == 'nfs4' else None
    if (mounted['filesystem'] not in ('nfs', 'nfs4') or mounted['source'] != expected_source or
            mounted['root'] != '/' or 'bind' in mounted['options'] or not version or
            not version.startswith('4') or version != config['nfs_version']):
        raise ReleaseError('backup mount/server/export/NFSv4 identity mismatch')
    # A more specific overmount inside the dataset is an unsafe replacement too.
    if any(m['mountpoint'] != mountpoint and (root == m['mountpoint'] or root.startswith(m['mountpoint'] + '/') or m['mountpoint'].startswith(root + '/')) and
           m['mountpoint'].startswith(mountpoint + '/') for m in mount_observations()):
        raise ReleaseError('backup dataset has an unexpected nested mount')
    marker = read_json(os.path.join(root, 'dataset.json'))
    if marker != {'schema_version': 1, 'destination_id': config['destination_id'], 'dataset_id': config['dataset_id']}:
        raise ReleaseError('backup dataset identity mismatch')
    verification = read_json(config['verification_path'])
    if _sha256_file(config['verification_path']) != config['verification_sha256']:
        raise ReleaseError('backup verification bytes changed')
    fields = ('destination_id', 'mountpoint', 'dataset_root', 'dataset_id', 'server', 'export', 'nfs_version')
    if (verification.get('schema_version') != 1 or any(verification.get(k) != config[k] for k in fields) or
            not verification.get('verified_by') or not verification.get('verified_at') or
            verification.get('different_host') is not True or verification.get('survives_gallery_host_loss') is not True):
        raise ReleaseError('backup infrastructure attestation is incomplete or mismatched')
    recovery = verification.get('restore_evidence', {})
    evidence = read_json(recovery.get('path'))
    if (_sha256_file(recovery['path']) != recovery.get('sha256') or evidence.get('outcome') != 'verified' or
            evidence.get('destination_id') != config['destination_id'] or evidence.get('host_loss_exercised') is not True):
        raise ReleaseError('backup restore/host-loss evidence missing or changed')
    if _provision_receipt:
        return {'mount': mounted, 'binding_verification': config['verification_sha256'], 'destination_id': config['destination_id'], 'dataset_id': config['dataset_id']}
    receipt = read_json(config['receipt'])
    if (receipt.get('schema_version') != 1 or receipt.get('operation') != 'bind-backup' or receipt.get('outcome') != 'verified' or
            any(receipt.get(k) != config[k] for k in (*fields, 'plan_digest', 'verification_sha256'))):
        raise ReleaseError('backup binding receipt belongs to another installation')
    return {'mount': mounted, 'binding_verification': config['verification_sha256'],
            'destination_id': config['destination_id'], 'dataset_id': config['dataset_id']}


def bind_backup(plan):
    config = {**plan['installation']['session_backup'], 'plan_digest': plan['plan_digest']}
    verify_backup_binding(config, _provision_receipt=True)
    fields = ('destination_id', 'mountpoint', 'dataset_root', 'dataset_id', 'server', 'export', 'nfs_version', 'plan_digest', 'verification_sha256')
    receipt = {**{k: config[k] for k in fields}, 'schema_version': 1, 'operation': 'bind-backup', 'outcome': 'verified'}
    atomic_json(Path(config['receipt']), receipt)
    verify_backup_binding(config)
    return receipt


def validate_backup_selection(config):
    fields = {'destination_id', 'mountpoint', 'dataset_root', 'dataset_id', 'server', 'export', 'nfs_version',
              'verification_path', 'verification_sha256', 'receipt'}
    if not isinstance(config, dict) or set(config) != fields:
        raise ReleaseError('session_backup requires the fixed NFSv4 installation selection')
    for field in ('destination_id', 'dataset_id'):
        try: uuid.UUID(config[field])
        except (ValueError, TypeError): raise ReleaseError(f'{field} must be a stable UUID') from None
    for field in ('mountpoint', 'dataset_root', 'verification_path', 'receipt'):
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute() or '..' in Path(config[field]).parts:
            raise ReleaseError(f'session_backup.{field} must be an absolute safe path')
    if not config['dataset_root'].startswith(config['mountpoint'].rstrip('/') + '/'):
        raise ReleaseError('backup dataset must belong to the configured mount')
    if not isinstance(config['server'], str) or not config['server'] or not config['export'].startswith('/'):
        raise ReleaseError('session_backup must name the NFS server and export')
    if not re.fullmatch(r'4(?:\.[012])?', config['nfs_version']):
        raise ReleaseError('session backup only supports NFSv4')
    if not re.fullmatch(r'[0-9a-f]{64}', config['verification_sha256']):
        raise ReleaseError('session_backup verification must bind exact evidence bytes')
