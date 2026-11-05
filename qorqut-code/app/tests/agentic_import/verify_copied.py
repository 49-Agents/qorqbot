"""Explicit offline full-import rehearsal after committing copied sources.

Run this script with --source-roots JSON and a new --scratch directory. It uses
only public AgenticLayer APIs and never imports source provider implementations.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from bazarlyk.agentic import inventory_sources, build_import_plan, apply_import_plan
from bazarlyk.agentic.support import decoded
from qorqut_agentic import AgenticLayer


def verify(source_roots, scratch):
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=False)
    inventory = inventory_sources(source_roots, ['email-management','content','paper','fastmail','agent-terminal-control'])
    assert inventory['coverage']['complete'], inventory['diagnostics']
    assert all(s['clean'] and s['commit'] for s in inventory['sources'])
    (scratch/'inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
    plan = build_import_plan(inventory)
    layer = AgenticLayer.initialize(scratch/'authored',scratch/'state',
                                   git_user=dict(name='Offline fixture',email='fixture@example.invalid'))
    applied = apply_import_plan(layer,plan,dict(actor_id='offline-verifier',actor_kind='owner',
                                               reason='Verify committed copied-source import'))
    reopened = AgenticLayer.open(layer.root, layer.state)
    validation = reopened.catalog.validate()
    assert not validation['issues'], validation
    actual = {p.relative_to(layer.root).as_posix():p.read_bytes() for p in layer.root.rglob('*')
              if p.is_file() and '.git' not in p.relative_to(layer.root).parts}
    assert {p:sha256(b).hexdigest() for p,b in actual.items()} == plan['desired_files']
    catalog = reopened.catalog.list(limit=1000)
    expected = {f"{package['id']}/{entry['id']}" for package in inventory['packages'] for entry in package['entries']}
    assert {entry['ref'] for entry in catalog['entries']} == expected
    for package in inventory['packages']:
        for path, record in package['files'].items():
            result = reopened.catalog.read_resource(package['id'],path)
            body = result['content'].encode() if result['encoding']=='utf-8' else decoded(result['content'])
            assert body == decoded(record['content_base64'])
            assert result['sha256'] == record['sha256']
    repeat = build_import_plan(inventory,actual,applied['import_manifest'])
    assert not repeat['changes'] and not repeat['conflicts']
    assert apply_import_plan(reopened,repeat,dict(actor_id='offline-verifier',actor_kind='owner',reason='No-op verification'))['no_op']
    assert len(reopened.changes.list()['changes']) == 1
    report = dict(coverage=inventory['coverage'],sources=[{k:s[k] for k in ('id','commit','clean','snapshot_digest')} for s in inventory['sources']],
                  packages=len(inventory['packages']),files=len(actual),catalog_entries=len(expected),
                  bytes=sum(map(len,actual.values())),public_catalog_valid=True,exact_byte_match=True,
                  durable_events=1,reimport_no_op=True,source_digest=inventory['source_digest'],
                  composition=[a['path'] for a in inventory['assets'] if a['status']=='declared_composition'])
    (scratch/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-roots',required=True,type=Path)
    parser.add_argument('--scratch',required=True,type=Path)
    args=parser.parse_args()
    verify(json.loads(args.source_roots.read_text()),args.scratch)
