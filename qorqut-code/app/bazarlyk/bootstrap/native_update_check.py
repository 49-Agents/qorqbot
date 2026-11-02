"""Read-only admission check for native updates with no managed-instruction migration."""
import argparse
from pathlib import Path

from bazarlyk.agentic.upgrade import prepare_upgrade
from bazarlyk.bootstrap.initialize import load_prepared
from bazarlyk.instance_paths import InstancePaths


def check(prepared, instance):
    _, _, components = load_prepared(prepared)
    plan = prepare_upgrade(InstancePaths(instance, components), prepared / 'src')
    if (plan.get('kind') != 'agentic_managed_update_v2' or not plan.get('complete')
            or plan.get('changes') or plan.get('state', {}).get('migration_required')
            or any(item['state'] == 'missing' for item in plan.get('provisioning', {}).get('items', ()))):
        raise RuntimeError('This update requires managed-instruction adoption or conflict resolution; a qualified managed update is required. The previous runtime will be restored.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-root', required=True, type=Path)
    parser.add_argument('--instance-root', required=True, type=Path)
    args = parser.parse_args()
    check(args.prepared_root, args.instance_root)
