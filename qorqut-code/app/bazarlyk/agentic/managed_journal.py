"""Bounded private journals for one managed update attempt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from bazarlyk.private_files import read_private_file, require_private_directory
from .layout_files import _journal_write
from .layout_intent import MAX_INTENT_BYTES, _conflict
from .support import canonical, digest, relative
from .upgrade import _unique, history_directory

_NAME = re.compile(r'managed-(intent|journal)-([0-9a-f]{64})\.json\Z')
_PHASES = ('prepared', 'files_published', 'git_prepared', 'ref_published', 'index_publishing',
           'git_published', 'feed_published', 'binding_published', 'complete')
_KEYS = {'schema_version', 'phase', 'plan_digest', 'intent_sha256', 'attribution', 'prior_binding_sha256',
         'root', 'gitdir', 'head_identity', 'initial_dirs', 'created_dirs', 'git', 'index_publication',
         'provisioning', 'prior_binding', 'feed', 'candidate_sha256', 'result', 'receipt_sha256'}


def read(path):
    raw = read_private_file(path, max_bytes=MAX_INTENT_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != _KEYS or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['phase'] not in _PHASES
                or raw != canonical(value) + b'\n'
                or value['receipt_sha256'] != digest({k: v for k, v in value.items() if k != 'receipt_sha256'})
                or path.name != 'managed-journal-' + value['plan_digest'] + '.json'):
            raise ValueError('invalid managed journal')
        for key in ('plan_digest', 'intent_sha256', 'prior_binding_sha256'):
            if not isinstance(value[key], str) or not re.fullmatch(r'[0-9a-f]{64}', value[key]):
                raise ValueError('invalid managed journal digest')
        for key in ('root', 'gitdir'):
            if (not isinstance(value[key], dict) or set(value[key]) != {'device', 'inode', 'mode'}
                    or any(type(v) is not int or v < 0 for v in value[key].values())):
                raise ValueError('invalid managed directory identity')
        if (not isinstance(value['initial_dirs'], dict) or not isinstance(value['created_dirs'], dict)
                or set(value['initial_dirs']) & set(value['created_dirs'])
                or len(value['initial_dirs']) + len(value['created_dirs']) > 100000
                or (value['phase'] == 'complete') != isinstance(value['result'], dict)):
            raise ValueError('invalid managed journal contents')
        for path, identity in {**value['initial_dirs'], **value['created_dirs']}.items():
            relative(path)
            if (path.split('/', 1)[0] not in ('packages', 'roles', 'operations', 'overseer', 'plugins', 'user-preferences')
                    or not isinstance(identity, dict) or set(identity) != {'device', 'inode', 'mode'}
                    or any(type(v) is not int or v < 0 for v in identity.values()) or identity['mode'] > 0o777):
                raise ValueError('invalid managed directory evidence')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise _conflict('Invalid managed journal') from exc


def write(path, value):
    _journal_write(path, value)


def discover(paths):
    directory = history_directory(paths)
    if not os.path.lexists(directory):
        return {}
    require_private_directory(directory)
    result = {}
    with os.scandir(directory) as stream:
        entries = list(stream)
    if len(entries) > 10000:
        raise _conflict('Managed history exceeds inspection bound')
    for entry in sorted(entries, key=lambda x: x.name):
        if entry.name.startswith(('.managed-index-', '.agentic-layer.')):
            raise _conflict('Unfinished temporary private evidence')
        if not entry.name.startswith('managed-'):
            continue
        match = _NAME.fullmatch(entry.name)
        if not match:
            raise _conflict('Unknown managed evidence path')
        result.setdefault(match.group(2), {})[match.group(1)] = Path(directory) / entry.name
    if any('intent' not in item for item in result.values()):
        raise _conflict('Managed journal lacks its intent')
    return result
