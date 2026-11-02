"""Derive a managed binding candidate; this never publishes or activates it."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from . import managed_intent
from .support import canonical, digest
from .upgrade import MAX_RECORD, _unique
from .layout_intent import _conflict


def derive(prior_raw, intent, resulting_commit):
    """Pure projection from an already validated intent, without activation authority."""
    if (not isinstance(intent, dict) or intent.get('prior_binding_sha256') != digest(prior_raw)
            or intent.get('receipt_sha256') != digest({k: v for k, v in intent.items() if k != 'receipt_sha256'})):
        raise _conflict('Managed candidate inputs differ')
    if not isinstance(resulting_commit, str) or not re.fullmatch(r'[0-9a-f]{40}', resulting_commit):
        raise _conflict('Malformed managed resulting commit')
    plan = intent['plan']
    if not plan['changes'] and resulting_commit != intent['head']:
        raise _conflict('Binding-only update must retain the original commit')
    prior = json.loads(prior_raw, object_pairs_hook=_unique)
    imported = {'schema_version': 1, 'source_digest': plan['import']['source_digest'],
                'plan_digest': plan['plan_digest'], 'previous_import_digest': plan['import']['previous_supplier_digest'],
                'existing_digest': plan['import']['existing_digest'], 'files': deepcopy(plan['import']['desired_files']),
                'bindings': deepcopy(plan['target_inventory']['bindings'])}
    imported['manifest_digest'] = digest(imported)
    candidate = {k: deepcopy(prior[k]) for k in ('version', 'instance_id', 'root', 'state', 'selected_extensions',
                                                   'initial_commit', 'preferences_seed')}
    candidate['selected_extensions'] = deepcopy(plan['selected_extensions'])
    candidate.update(inventory=deepcopy(plan['target_inventory']), inventory_sha256=digest(plan['target_inventory']),
                     import_manifest=imported, source_manifest_sha256=plan['export']['source_manifest_sha256'],
                     management_seed=deepcopy(plan['management_seed']['desired']),
                     overseer_seed=deepcopy(plan['overseer_seed']['desired']),
                     managed_upgrade={'format_version': 1, 'plan_digest': plan['plan_digest'],
                                      'prior_binding_sha256': intent['prior_binding_sha256'],
                                      'intent_sha256': intent['receipt_sha256'], 'resulting_commit': resulting_commit})
    raw = canonical(candidate) + b'\n'
    if len(raw) > MAX_RECORD:
        raise _conflict('Managed candidate exceeds binding size bound')
    return candidate, raw, digest(raw)


def candidate(paths, prior_raw, intent, resulting_commit):
    validated = managed_intent.validate(paths, intent, prior_raw)
    return derive(prior_raw, validated, resulting_commit)
