from copy import deepcopy

import pytest

from bazarlyk.release_worker.plugin_adoption import plugin_changes
from bazarlyk.release_worker.errors import ReleaseError


def test_update_keeps_unrelated_pins_and_selected_configuration():
    profile = {"plugins": [{"id": "other", "version": "1", "config": {"custom": True}},
                           {"id": "thinking", "version": "1", "config": {"keep": True}}]}
    before = deepcopy(profile)
    result = plugin_changes(profile, {"id": "thinking", "version": "2"})
    assert result["plugins"] == [before["plugins"][0], {"id": "thinking", "version": "2", "config": {"keep": True}}]
    assert profile == before


@pytest.mark.parametrize("plugins", [[], [{"id": "other", "version": "1", "config": {}}]])
def test_absent_pin_is_rejected_and_never_granted(plugins):
    profile = {"plugins": plugins}
    before = deepcopy(profile)
    with pytest.raises(ReleaseError, match="already have exactly one plugin pin"):
        plugin_changes(profile, {"id": "thinking", "version": "1"})
    assert profile == before


def test_duplicate_pin_is_not_silently_rewritten():
    profile = {"plugins": [{"id": "thinking", "config": {}}, {"id": "thinking", "config": {}}]}
    before = deepcopy(profile)
    with pytest.raises(ReleaseError, match="already have exactly one plugin pin"):
        plugin_changes(profile, {"id": "thinking", "version": "2"})
    assert profile == before
