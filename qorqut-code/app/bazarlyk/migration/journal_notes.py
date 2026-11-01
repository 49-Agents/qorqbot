"""Explicit, repeatable adoption of registered Journal entries as literal Notes.

Run with --root and --workspace to inspect; --apply adopts all current entries.
Uses revision/hash-bound application writes without starting background services.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from qorqut_operations.contracts import DomainError, InvocationContext, canonical_hash, plain
from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from bazarlyk.service.runtime import build_runtime
from bazarlyk.transport.auth import Authentication


def migrate(runtime, *, apply=False):
    runtime.require_authority()
    owner, _, _ = Authentication(runtime.database, runtime.root).installation_owner()
    store = runtime.domains.documents.store
    with runtime.database.read() as uow:
        businesses = {row['id']: uow.repository('businesses').get(row['id'])
                      for row in uow.connection.execute('SELECT id FROM businesses')}
        rows = [uow.repository('documents', business_id=row['business_id']).get(row['id'])
                for row in uow.connection.execute("SELECT id,business_id FROM documents WHERE kind='journal' ORDER BY business_id,id")]
        if any(row['business_id'] not in businesses for row in rows):
            raise DomainError('invalid_document', 'Journal entry has no registered business')
        entries = []
        for row in rows:
            data = store.read_bytes(row, uow)
            entries.append((row, data, store.view(row, uow)))
    counts = {row['slug']: 0 for row in businesses.values()}
    for row, _, _ in entries:
        counts[businesses[row['business_id']]['slug']] += 1
    result = {'apply': apply, 'entries': len(entries), 'businesses': counts}
    if not apply or not entries:
        return result
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = runtime.root / '.bazarlyk' / 'migration-backups' / ('journal-notes-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for row, data, document in entries:
        target = backup / (row['id'] + '.md')
        target.write_bytes(data); target.chmod(0o600)
        digest = hashlib.sha256(data).hexdigest()
        manifest.append({'record': plain(row), 'file_hash': digest})
    path = backup / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'); path.chmod(0o600)
    for row, data, document in entries:
        ctx = InvocationContext(owner, business_id=row['business_id'],
            allowed_business_ids=frozenset({row['business_id']}), source='dashboard')
        args = {'document_id': row['id'], 'expected_content_hash': hashlib.sha256(data).hexdigest()}
        outcome = runtime.application.invoke(ctx, 'notes_update', args, {
            'expected_revision': row['revision'],
            'idempotency_key': canonical_hash(['journal-notes-v1', row['id'], row['revision'], args]),
        })
        if outcome.status != 'ok':
            raise DomainError('migration_incomplete', 'An entry changed or could not be adopted; rerun after resolving it',
                details={'document_id': row['id'], 'result': outcome.to_dict(), 'backup': str(backup)})
        with runtime.database.read() as uow:
            saved = uow.repository('documents', business_id=row['business_id']).get(row['id'])
            observed = store.view(saved, uow)
            if (saved['kind'] != 'business_note'
                    or store.read_bytes(saved, uow) != data or observed['body'] != document['body']
                    or saved['path'] != row['path']):
                raise DomainError('migration_verification_failed', 'Journal adoption did not preserve its original source',
                    details={'document_id': row['id'], 'backup': str(backup)})
    result.update(verified=len(entries), backup=str(backup))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    add_instance_arguments(parser)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    runtime = build_runtime(args.root, args.workspace, paths=instance_paths_from_arguments(args, required=('app', 'template')))
    print(json.dumps(migrate(runtime, apply=args.apply), indent=2))


if __name__ == '__main__':
    main()
