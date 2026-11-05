"""Repeated receipt reads within an open must still inspect private files."""
import pytest

from bazarlyk.agentic import layout_intent, upgrade
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, legacy


def test_retained_read_rechecks_each_private_file(legacy):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    layout_intent.begin(paths, plan, attribution=ATTR)
    intent_path = upgrade.history_directory(paths) / ('layout-' + plan['plan_digest'] + '.json')
    intent_raw = intent_path.read_bytes()
    intent = layout_intent._read_intent(intent_path)
    prior_path = upgrade.history_snapshot(paths, intent['prior_binding_sha256'])
    with layout_intent._retained_reads():
        assert layout_intent._read_intent(intent_path) == intent
        for path in (intent_path, prior_path):
            saved = path.read_bytes()
            try:
                path.write_bytes(saved + b' ')
                with pytest.raises(DomainError):
                    layout_intent._read_intent(intent_path)
            finally:
                path.write_bytes(saved)
        result = layout_intent._read_intent(intent_path)
        result['plan'].clear()
        assert layout_intent._read_intent(intent_path) == intent
        with pytest.raises(DomainError):
            layout_intent._read_intent(intent_path, expected={})
    assert intent_path.read_bytes() == intent_raw
