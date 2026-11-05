import base64
import copy
import json
import shutil

import pytest
from agentic_installation_support import snapshot
from bazarlyk.agentic.installation import prepare_installation
from bazarlyk.agentic.managed_witness import _records, capture, recheck, validate
from bazarlyk.source_snapshot import canonical
from bazarlyk.agentic.support import digest


def test_witness_replays_verified_inventory_after_export_removal(tmp_path):
    root = snapshot(tmp_path / 'export')
    prepared = prepare_installation(root, [])
    raw = (root / 'SOURCE-MANIFEST.json').read_bytes()
    manifest = json.loads(raw)
    seeds = {'management_seed': prepared['seed'], 'overseer_seed': prepared['overseer_seed']}
    witness = capture(root, manifest, prepared['inventory'], seeds)
    extra_dir = root / 'qorqut-code/app/temporary-empty'
    extra_dir.mkdir()
    with pytest.raises(ValueError, match='observations changed'):
        recheck(root, manifest, witness)
    extra_dir.rmdir()
    recheck(root, manifest, witness)
    shutil.rmtree(root)
    assert validate(root, raw, prepared['inventory'], seeds, witness, []) == manifest
    component = next(iter(witness))
    for mutation in ('omitted', 'extra', 'duplicate', 'executable', 'input', 'symlink', 'prefix'):
        changed = copy.deepcopy(witness)
        records = changed[component]['records']
        if mutation == 'omitted':
            records.pop()
        elif mutation == 'extra':
            records.append({'path': 'zzzz-new', 'kind': 'file', 'sha256': '0' * 64, 'executable': False})
        elif mutation == 'duplicate':
            records.append(copy.deepcopy(records[-1]))
        elif mutation == 'executable':
            file = next(r for r in records if r['kind'] == 'file')
            file['executable'] = not file['executable']
        elif mutation == 'symlink':
            path = next(iter(changed[component]['used_files']))
            record = next(r for r in records if r['path'] == path)
            record.clear()
            record.update(path=path, kind='symlink', target='other')
        elif mutation == 'prefix':
            path = next(iter(changed[component]['used_files']))
            changed[component]['used_files']['other/' + path] = changed[component]['used_files'].pop(path)
        else:
            path = next(iter(changed[component]['used_files']))
            item = changed[component]['used_files'][path]
            body = base64.b64decode(item['content_base64']) + b'changed'
            item.update(sha256=digest(body), content_base64=base64.b64encode(body).decode('ascii'))
        with pytest.raises(ValueError):
            validate(root, raw, prepared['inventory'], seeds, changed, [])
    changed_seeds = copy.deepcopy(seeds)
    changed = copy.deepcopy(witness)
    body = changed_seeds['overseer_seed']['instructions/review.md'] + b'changed'
    changed_seeds['overseer_seed']['instructions/review.md'] = body
    name = 'src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    changed['qorqut-agentic']['used_files'][name] = {'sha256': digest(body), 'content_base64': base64.b64encode(body).decode('ascii')}
    with pytest.raises(ValueError):
        validate(root, raw, prepared['inventory'], changed_seeds, changed, [])


def test_directory_witness_rejects_unsafe_and_conflicting_paths():
    import hashlib
    records = [{'path': 'a/link', 'kind': 'symlink', 'target': 'target'},
               {'path': 'a/target', 'kind': 'file', 'sha256': 'a' * 64, 'executable': False}]
    component = {'name': 'outer', 'destination': 'outer', 'snapshot_sha256': hashlib.sha256(canonical(records)).hexdigest()}
    nested = {'name': 'nested', 'destination': 'outer/nested'}
    assert _records(component, [component, nested], records, ['a'])
    for directories in (['../bad'], ['a', 'a'], ['a', 'a/target'], ['a', 'a/link', 'a/link/child'],
                        ['a', 'nested'], ['a/child'], ['a', 'a/link/child']):
        with pytest.raises(ValueError):
            _records(component, [component, nested], records, directories)
