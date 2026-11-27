"""Admission check for native updates with no managed-instruction migration.

Every new build changes the provenance a catalog records for its packaged sources (commit, tree and snapshot hashes, and
the source root if the folder moved) even when no instruction, skill or file changed. Such a provenance-only refresh is
admitted without changing the installed catalogs. Their previous provenance still names the exact bytes installed, which
the per-file digests prove unchanged, and the next update compares against it again. Keeping admission read-only lets the
native updater restore its previous runtime after a failure without leaving a separate managed update to recover. Any
other managed change, a schema migration or a missing provisioned item is refused before the new runtime is admitted.

A store that holds the record of extension tables is judged here, by the new runtime, because only its code can read the
record: the updater passes ``--recorded-store`` when it found the record table, and the store is then opened read-only,
as the service opens it, with the instance's extension selection and what the store records. A record row that is not
exactly what this version writes, or whose statements fail the statement check, refuses the update before the store is
compared or anything composes or compiles that row. Without the option the store is not read here, as before.
"""
import argparse
import base64
from pathlib import Path

import yaml

from qorqut.agentic.upgrade import prepare_upgrade
from qorqut_operations.contracts import DomainError
from qorqut.bootstrap.initialize import load_prepared
from qorqut.instance_paths import InstancePaths

STORE_REFUSAL = 'This update cannot open the existing database with its recorded extension tables: {}. The previous runtime will be restored.'
REFUSAL = ('This update requires managed-instruction adoption or conflict resolution; a qualified managed update is '
           'required. The previous runtime will be restored.')
# Keys of one packaged source in a catalog's ``source.sources`` list that describe where the bytes came from, not what
# they are. Per-file digests (``files``, ``snapshot_digest``) are content and are never in this set.
PROVENANCE_KEYS = frozenset({'commit', 'tree', 'snapshot_sha256', 'source_manifest_sha256', 'root'})


def _without_provenance(document):
    if not isinstance(document, dict):
        return document
    source = document.get('source')
    if not isinstance(source, dict) or not isinstance(source.get('sources'), list):
        return document
    sources = [{key: value for key, value in item.items() if key not in PROVENANCE_KEYS} if isinstance(item, dict) else item
               for item in source['sources']]
    return {**document, 'source': {**source, 'sources': sources}}


def provenance_only(change, agentic_root):
    """True when one planned change edits a catalog ``agentic.yaml`` and differs from the installed file only in the
    provenance keys of its packaged sources."""
    if not isinstance(change, dict) or change.get('op') != 'edit' or not isinstance(change.get('path'), str):
        return False
    relative = Path(change['path'])
    if relative.is_absolute() or '..' in relative.parts or relative.name != 'agentic.yaml':
        return False
    current_path = Path(agentic_root) / relative
    if current_path.is_symlink() or not current_path.is_file():
        return False
    try:
        current = yaml.safe_load(current_path.read_text())
        desired = yaml.safe_load(base64.b64decode(change['content_base64'], validate=True).decode())
    except (KeyError, ValueError, UnicodeDecodeError, yaml.YAMLError, OSError):
        return False
    return current != desired and _without_provenance(current) == _without_provenance(desired)


def admissible(plan):
    return (plan.get('kind') == 'agentic_managed_update_v2' and plan.get('complete')
            and not plan.get('state', {}).get('migration_required')
            and not any(item['state'] == 'missing' for item in plan.get('provisioning', {}).get('items', ())))


def check_store(paths):
    """Open the store read-only in this runtime's composition: the instance's selection and the store's own record."""
    from qorqut.composition.assembly import host_assembly, open_database
    from qorqut.composition.extension_tables import recorded
    from qorqut.extensions import installed_selection
    from qorqut.installation import extensions_configuration
    from qorqut.service.changes import ChangeFeed
    try:
        selection = installed_selection(extensions_configuration(paths.instance_root / 'config/extensions.json'))
        open_database(paths, host_assembly(ChangeFeed(), selection, tables=recorded(paths.database)))
    except DomainError as exc:
        raise RuntimeError(STORE_REFUSAL.format(exc.message)) from exc


def check(prepared, instance, *, recorded_store=False):
    _, _, components = load_prepared(prepared)
    paths = InstancePaths(instance, components)
    if recorded_store:
        check_store(paths)
    plan = prepare_upgrade(paths, prepared / 'src')
    if not admissible(plan):
        raise RuntimeError(REFUSAL)
    changes = plan.get('changes') or []
    if not changes:
        return {'applied': False}
    if not all(provenance_only(change, paths.agentic_root) for change in changes):
        raise RuntimeError(REFUSAL)
    return {'applied': False, 'retained_provenance': [change['path'] for change in changes]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--prepared-root', required=True, type=Path)
    parser.add_argument('--instance-root', required=True, type=Path)
    parser.add_argument('--recorded-store', action='store_true',
                        help='the store holds the record of extension tables; open it with that record')
    args = parser.parse_args(argv)
    result = check(args.prepared_root, args.instance_root, recorded_store=args.recorded_store)
    if result.get('retained_provenance'):
        print('Kept the previous source provenance in ' + str(len(result['retained_provenance']))
              + ' catalog files; their contents are unchanged.')


if __name__ == '__main__':
    main()
