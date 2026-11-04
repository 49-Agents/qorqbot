"""Admission check for native updates with no managed-instruction migration.

Every new build changes the provenance a catalog records for its packaged sources (commit, tree and snapshot hashes, and
the source root if the folder moved) even when no instruction, skill or file changed. Such a provenance-only refresh is
admitted. On Linux it is applied as the check's last step, through the same journaled offline managed apply the connector
installer uses. The managed apply publishes files with Linux-only unnamed files (O_TMPFILE), so on macOS the catalogs keep
their previous provenance: it still names the exact bytes installed, which the per-file digests prove unchanged, and the
next update compares against it again. Any other managed change, a schema migration or a missing provisioned item is still
refused before the new runtime is admitted.
"""
import argparse
import base64
import os
from pathlib import Path
import sys

import yaml

from bazarlyk.agentic.upgrade import prepare_upgrade
from bazarlyk.bootstrap.initialize import load_prepared
from bazarlyk.instance_paths import InstancePaths

REFUSAL = ('This update requires managed-instruction adoption or conflict resolution; a qualified managed update is '
           'required. The previous runtime will be restored.')
# Keys of one packaged source in a catalog's ``source.sources`` list that describe where the bytes came from, not what
# they are. Per-file digests (``files``, ``snapshot_digest``) are content and are never in this set.
PROVENANCE_KEYS = frozenset({'commit', 'tree', 'snapshot_sha256', 'source_manifest_sha256', 'root'})
ATTRIBUTION = {'actor_id': 'local-native-updater', 'actor_kind': 'human',
               'reason': 'Native update: refresh packaged source provenance'}


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


def managed_apply_supported():
    return sys.platform.startswith('linux') and hasattr(os, 'O_TMPFILE')


def check(prepared, instance, *, apply=None, supported=managed_apply_supported):
    _, _, components = load_prepared(prepared)
    paths = InstancePaths(instance, components)
    plan = prepare_upgrade(paths, prepared / 'src')
    if not admissible(plan):
        raise RuntimeError(REFUSAL)
    changes = plan.get('changes') or []
    if not changes:
        return {'applied': False}
    if not all(provenance_only(change, paths.agentic_root) for change in changes):
        raise RuntimeError(REFUSAL)
    if not supported():
        return {'applied': False, 'retained_provenance': [change['path'] for change in changes]}
    if apply is None:
        from bazarlyk.agentic.managed_update import apply
    apply(paths, plan, attribution=ATTRIBUTION)
    return {'applied': True, 'files': [change['path'] for change in changes]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--prepared-root', required=True, type=Path)
    parser.add_argument('--instance-root', required=True, type=Path)
    args = parser.parse_args()
    result = check(args.prepared_root, args.instance_root)
    if result['applied']:
        print('Refreshed packaged source provenance in ' + str(len(result['files'])) + ' catalog files.')
    elif result.get('retained_provenance'):
        print('Kept the previous source provenance in ' + str(len(result['retained_provenance']))
              + ' catalog files; their contents are unchanged.')
