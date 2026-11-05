"""Trusted, plan-bound persistent channel datasets. No executable plugin hooks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from .errors import ReleaseError
from .io import atomic_json, canonical, digest, file_digest, durable_makedirs, fsync_directory


FIELDS = {'schema_version', 'channel_id', 'data_root', 'dataset_id', 'dataset_path', 'source_view',
          'contract_sha256', 'receipt_path'}


def validate_selection(selection, component, installation):
    if not isinstance(selection, dict) or set(selection) != FIELDS or selection.get('schema_version') != 1:
        raise ReleaseError('experience_binding requires the fixed schema 1 selection')
    if component['category'] != 'plugin' or selection['channel_id'] != component['id']:
        raise ReleaseError('experience binding must target its owning channel plugin')
    try: uuid.UUID(selection['dataset_id'])
    except (ValueError, TypeError): raise ReleaseError('dataset_id must be a stable UUID') from None
    for field in ('data_root', 'dataset_path', 'source_view', 'receipt_path'):
        value = selection[field]
        if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
            raise ReleaseError(f'experience_binding.{field} must be an absolute safe path')
        if 'worktrees' in Path(value).parts or '.worktrees' in Path(value).parts or '.git' in Path(value).parts:
            raise ReleaseError('live experience paths cannot be worktrees or Git data')
    root = Path(selection['data_root'])
    expected = root / 'plugin-experiences' / component['id'] / 'sessions'
    if Path(selection['dataset_path']) != expected:
        raise ReleaseError('dataset_path must agree with the fixed persistent layout')
    source = Path(selection['source_view'])
    if source == root or source.is_relative_to(root) or root.is_relative_to(source):
        raise ReleaseError('data root must be outside the installed source view')
    if Path(selection['receipt_path']) != source / 'experiences/binding.json':
        raise ReleaseError('binding receipt must belong to the installed source view')
    forbidden = [installation.get('release_root'), installation.get('businesses_root'),
                 component.get('adoption_recipe', {}).get('release_root'), component.get('adoption_recipe', {}).get('businesses_root')]
    for path in (root, source):
        if 'artifacts' in path.parts or 'skills' in path.parts:
            raise ReleaseError('binding/data cannot be in immutable artifacts or skill subtrees')
        if any(Path(p) == path or path.is_relative_to(Path(p)) for p in forbidden if p):
            raise ReleaseError('binding/data cannot be in releases or business projects')
    sha = selection['contract_sha256']
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise ReleaseError('contract_sha256 must bind exact sidecar bytes')


def contract_bytes(path, channel, expected_sha):
    if path.is_symlink() or not path.is_file():
        raise ReleaseError('experience contract is missing/nonregular')
    raw = path.read_bytes()
    try: value = json.loads(raw)
    except (ValueError, UnicodeDecodeError): raise ReleaseError('invalid experience contract JSON') from None
    session_format = value.get('session_format') if isinstance(value, dict) else None
    if type(session_format) is not int or session_format not in (2, 3) or value != {'schema_version': 1, 'channel_id': channel, 'session_format': session_format,
                  'binding': 'experiences/sessions', 'dataset_layout': f'plugin-experiences/{channel}/sessions'}:
        raise ReleaseError('unsupported experience sidecar contract')
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ReleaseError('experience contract differs from frozen plan')
    return raw


def no_alias(path):
    if path.resolve() != path:
        raise ReleaseError(f'owned path aliases a symlink: {path}')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ReleaseError(f'owned path has a symlink ancestor: {path}')


@contextmanager
def dataset_lock(root):
    no_alias(root)
    durable_makedirs(root)
    with (root / '.experience-bindings.lock').open('a+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def expected_receipt(plan, component):
    selection = component['experience_binding']
    return {'schema_version': 1, 'operation': 'bind-experiences', 'outcome': 'verified',
            'release_id': plan['release_id'], 'plan_digest': plan['plan_digest'], 'component': component['id'],
            'source_commit': component['source_commit'], 'channel_id': component['id'],
            'selection_digest': digest(selection), 'dataset_id': selection['dataset_id'],
            'dataset_path': selection['dataset_path'], 'source_view': selection['source_view'],
            'binding_path': str(Path(selection['source_view']) / 'experiences/sessions'),
            'contract_sha256': selection['contract_sha256']}


def verify_dataset(selection, session_format=2):
    dataset = Path(selection['dataset_path']); parent = dataset.parent
    no_alias(parent); no_alias(dataset)
    marker = parent / 'dataset.json'
    expected = {'schema_version': 1, 'dataset_id': selection['dataset_id'], 'channel_id': selection['channel_id'], 'session_format': session_format}
    if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != expected or not dataset.is_dir():
        raise ReleaseError('persistent dataset identity is missing/mismatched; reconcile without deleting data')
    return marker


def verify_binding(plan, component):
    selection = component['experience_binding']
    validate_selection(selection, component, plan['installation'])
    source = Path(selection['source_view']); no_alias(source)
    session_format = json.loads(contract_bytes(source / 'experiences/contract.json', component['id'], selection['contract_sha256']))['session_format']
    verify_dataset(selection, session_format)
    expected = expected_receipt(plan, component)
    binding = Path(expected['binding_path'])
    if not binding.is_symlink() or str(binding.resolve()) != selection['dataset_path'] or os.readlink(binding) != selection['dataset_path']:
        raise ReleaseError('experience binding is absent or incorrect')
    receipt_path = Path(selection['receipt_path'])
    no_alias(receipt_path)
    if not receipt_path.is_file() or json.loads(receipt_path.read_text()) != expected:
        raise ReleaseError('experience binding receipt does not match the frozen plan')
    return expected


def bind_experiences(plan, component, artifact):
    selection = component['experience_binding']
    validate_selection(selection, component, plan['installation'])
    contract_bytes(Path(artifact) / 'experiences/contract.json', component['id'], selection['contract_sha256'])
    source = Path(selection['source_view']); no_alias(source)
    session_format = json.loads(contract_bytes(source / 'experiences/contract.json', component['id'], selection['contract_sha256']))['session_format']
    dataset, root = Path(selection['dataset_path']), Path(selection['data_root'])
    binding = source / 'experiences/sessions'
    no_alias(binding.parent)
    if os.path.lexists(binding) and (not binding.is_symlink() or os.readlink(binding) != str(dataset)):
        raise ReleaseError('existing binding path requires reconciliation; nothing is changed')
    with dataset_lock(root):
        parent = dataset.parent
        no_alias(parent); no_alias(dataset)
        expected_marker = {'schema_version': 1, 'dataset_id': selection['dataset_id'], 'channel_id': component['id'], 'session_format': session_format}
        if parent.exists() and not (parent / 'dataset.json').exists() and any(parent.iterdir()):
            raise ReleaseError('nonempty unowned dataset path requires reconciliation')
        if not (parent / 'dataset.json').exists():
            durable_makedirs(parent, anchor=root)
            atomic_json(parent / 'dataset.json', expected_marker)
        if json.loads((parent / 'dataset.json').read_text()) != expected_marker:
            raise ReleaseError('persistent dataset marker differs; reconcile without changing it')
        durable_makedirs(dataset, anchor=root)
        verify_dataset(selection, session_format)
        if os.path.lexists(binding):
            if not binding.is_symlink() or os.readlink(binding) != str(dataset):
                raise ReleaseError('existing binding path requires reconciliation; nothing is deleted')
        else:
            binding.symlink_to(dataset, target_is_directory=True)
        fsync_directory(binding.parent)  # also retry an existing, previously unflushed link
        receipt = expected_receipt(plan, component)
        atomic_json(Path(selection['receipt_path']), receipt)
        return verify_binding(plan, component)


def unbind_experiences(plan, component):
    # Caller must be the approved trusted uninstall/recovery lifecycle.
    receipt = verify_binding(plan, component)
    with dataset_lock(Path(component['experience_binding']['data_root'])):
        Path(receipt['binding_path']).unlink()
        fd = os.open(Path(receipt['binding_path']).parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        result = {**receipt, 'operation': 'unbind-experiences', 'dataset_retained': True}
        atomic_json(Path(component['experience_binding']['receipt_path']), result)
        return result


def main(argv=None):
    from .manifest import load_plan
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--component', required=True)
    parser.add_argument('--artifact', required=True, type=Path)
    parser.add_argument('--operation', choices=('bind', 'verify', 'unbind'), default='verify')
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    component = next(c for c in plan['components'] if c['id'] == args.component)
    if args.operation != 'verify':
        record = json.loads(args.plan.with_name('record.json').read_text())
        scope = 'adopt' if args.operation == 'bind' else 'recover'
        if record.get('approvals', {}).get(scope, {}).get('plan_digest') != plan['plan_digest']:
            raise ReleaseError('binding mutation needs the exact plan authorization')
    if args.operation == 'bind': result = bind_experiences(plan, component, args.artifact)
    elif args.operation == 'unbind': result = unbind_experiences(plan, component)
    else: result = verify_binding(plan, component)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__': main()
