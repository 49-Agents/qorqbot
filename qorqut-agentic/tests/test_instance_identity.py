"""Public identity reads are stable, validated, and never repair state."""

import sqlite3

import pytest

from qorqut_agentic import AgenticLayer
from qorqut_agentic.errors import StoreError
from tests.conftest import make_layer


def snapshot(layer):
    with sqlite3.connect(layer.state / "state.sqlite3") as connection:
        return tuple(connection.iterdump())


def test_identity_is_unique_persistent_and_read_only(tmp_path):
    first, second = make_layer(tmp_path / "first"), make_layer(tmp_path / "second")
    before = snapshot(first)
    identity = first.instance_id
    assert identity != second.instance_id
    assert first.instance_id == identity
    assert AgenticLayer.open(first.root, first.state).instance_id == identity
    assert snapshot(first) == before
    with pytest.raises(AttributeError):
        first.instance_id = second.instance_id


@pytest.mark.parametrize("invalid", [None, "", "inst_", "foreign", "inst_" + "z" * 32, "inst_" + "0" * 33])
def test_missing_or_invalid_identity_is_a_state_error_without_repair(tmp_path, invalid):
    layer = make_layer(tmp_path)
    with sqlite3.connect(layer.state / "state.sqlite3") as connection:
        if invalid is None:
            connection.execute("DELETE FROM meta WHERE key='instance_id'")
        else:
            connection.execute("UPDATE meta SET value=? WHERE key='instance_id'", (invalid,))
    before = snapshot(layer)
    for opened in (layer, AgenticLayer.open(layer.root, layer.state)):
        with pytest.raises(StoreError) as error:
            _ = opened.instance_id
        assert error.value.code == "state_mismatch"
    assert snapshot(layer) == before
