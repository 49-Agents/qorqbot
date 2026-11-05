"""Explicit, repeatable conversion of legacy project-note categories into ordinary tags.

Project notes no longer have a category. Notes written before that keep a ``category`` label (``strategy-notes``,
``marketing-notes``, ...) in their metadata; nothing interprets it any more. Run with --root and --workspace on an explicit
isolated copy to inspect; --apply adds the old label as an ordinary tag with a revision/hash-bound ``notes_update`` written by the
installation owner. Ids, authors, paths and body text are preserved and the legacy ``category`` key stays as uninterpreted
metadata. A note that already carries its label as a tag is left alone, so its bytes and hash do not change. A converted note's
frontmatter changes, so its content hash is recomputed by the ordinary write and reported as before/after. Nothing is converted
at run time or in the background, and no grant is issued or inferred from a label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, canonical_hash, plain
from qorqut_operations.documents.business_notes import KIND, TARGET
from qorqut_operations.documents.materials import TAGS
from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from bazarlyk.service.runtime import build_runtime
from bazarlyk.transport.auth import Authentication

MAX_TAGS = TAGS["maxItems"]
TAG_MAX_LENGTH = TAGS["items"]["maxLength"]


def tag_for(category, tags):
    """The tags a note carries after conversion, or the reason it is not converted."""
    if not isinstance(category, str) or not category.strip() or len(category) > TAG_MAX_LENGTH:
        return None, "category is not a valid tag"
    if category in tags:
        return None, "already tagged"
    if len(tags) >= MAX_TAGS:
        return None, "no room for another tag"
    return [*tags, category], None


def candidates(runtime):
    """Every active project note that carries a legacy category, with its observed bytes and revision."""
    store = runtime.domains.documents.store
    found = []
    with runtime.database.read() as uow:
        rows = [dict(row) for row in uow.connection.execute(
            "SELECT id,business_id FROM documents WHERE kind=? AND archived=0 ORDER BY business_id,id", (KIND,))]
        for ref in rows:
            row = uow.repository('documents', business_id=ref['business_id']).get(ref['id'])
            view = store.view(row, uow)
            if 'category' not in view['metadata']:
                continue
            found.append((row, store.read_bytes(row, uow), view))
    return found


def migrate(runtime, *, apply=False):
    runtime.require_authority()
    owner, _, _ = Authentication(runtime.database, runtime.root).installation_owner()
    store = runtime.domains.documents.store
    plan, skipped = [], []
    for row, data, view in candidates(runtime):
        tags = view['metadata'].get('tags', [])
        converted, reason = tag_for(view['metadata']['category'], tags)
        entry = {'document_id': row['id'], 'business_id': row['business_id'], 'path': row['path'],
                 'author_id': view['metadata'].get('author_id'), 'category': view['metadata']['category']}
        if converted is None:
            skipped.append({**entry, 'reason': reason})
        else:
            plan.append((row, data, view, converted, entry))
    result = {'apply': apply, 'notes': len(plan), 'skipped': skipped, 'converted': []}
    if not apply or not plan:
        result['pending'] = [entry | {'tags': converted} for _, _, _, converted, entry in plan]
        return result
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = runtime.root / '.bazarlyk' / 'migration-backups' / ('project-note-tags-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for row, data, _, _, _ in plan:
        target = backup / (row['id'] + '.md')
        target.write_bytes(data); target.chmod(0o600)
        manifest.append({'record': plain(row), 'file_hash': hashlib.sha256(data).hexdigest()})
    path = backup / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'); path.chmod(0o600)
    for row, data, view, converted, entry in plan:
        ctx = InvocationContext(owner, business_id=row['business_id'],
            allowed_business_ids=frozenset({row['business_id']}), source='dashboard')
        before = hashlib.sha256(data).hexdigest()
        args = {'document_id': row['id'], TARGET: row['business_id'], 'expected_content_hash': before,
                'metadata': {'tags': converted}}
        outcome = runtime.application.invoke(ctx, 'notes_update', args, InvocationOptions(
            expected_revision=row['revision'],
            idempotency_key=canonical_hash(['project-note-tags-v1', row['id'], row['revision'], args])))
        if outcome.status != 'ok':
            raise DomainError('migration_incomplete', 'A note changed or could not be converted; rerun after resolving it',
                details={'document_id': row['id'], 'result': outcome.to_dict(), 'backup': str(backup)})
        with runtime.database.read() as uow:
            saved = uow.repository('documents', business_id=row['business_id']).get(row['id'])
            observed = store.view(saved, uow)
            if (saved['kind'] != KIND or saved['path'] != row['path'] or saved['business_id'] != row['business_id']
                    or observed['body'] != view['body']
                    or observed['metadata'].get('author_id') != view['metadata'].get('author_id')
                    or observed['metadata'].get('tags') != converted):
                raise DomainError('migration_verification_failed', 'Tag conversion did not preserve the original note',
                    details={'document_id': row['id'], 'backup': str(backup)})
            after = observed['content_hash']
        result['converted'].append(entry | {'tags': converted, 'content_hash_before': before, 'content_hash_after': after,
                                            'revision_before': row['revision'], 'revision_after': saved['revision']})
    result.update(backup=str(backup))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    add_instance_arguments(parser)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    runtime = build_runtime(args.root, args.workspace, paths=instance_paths_from_arguments(args, required=('app',)))
    print(json.dumps(migrate(runtime, apply=args.apply), indent=2))


if __name__ == '__main__':
    main()
