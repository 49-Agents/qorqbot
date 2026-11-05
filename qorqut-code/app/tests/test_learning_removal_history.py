"""The operational-history catalog against the post-removal target: every table classified and retained projections exported.

The store is disposable and built from the trusted target composition. Nothing is copied from a live instance: the
synthetic rows are ordinary retained learning candidates, decisions and history, and the assertions check the fixed
catalog contract (including ``learning_runs`` private exclusion and ``learning_scan_state`` technical exclusion)
rather than a current deployment's payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest
from qorqut_operations.history_exports import PRIVATE_EXCLUDED, TECHNICAL_EXCLUDED
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.extensions import NO_EXTENSIONS
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.operations_history.catalog import catalog

OWNER = "7c7c0f0f-0000-4000-8000-000000000001"
STAMP = "2026-01-01T00:00:00Z"


def target_instance(tmp_path):
    transition = removal._transition_for(NO_EXTENSIONS)
    root = tmp_path / "instance"
    root.mkdir()
    paths = InstancePaths(root, {})
    Database(paths.database, assembly=transition.target).initialize_target()
    return paths, transition


def test_the_catalog_validates_against_the_target_and_exports_retained_synthetic_projections(tmp_path):
    paths, transition = target_instance(tmp_path)
    database = Database(paths.database, assembly=transition.target).open_existing()
    try:
        with database.transaction() as uow:
            uow.connection.execute("INSERT INTO principals (id, kind, display_name, active, revision, created_at, "
                                   "updated_at) VALUES (?, 'human', 'Owner', 1, 1, ?, ?)", (OWNER, STAMP, STAMP))
            uow.connection.execute("INSERT INTO learning_candidates (business_id, id, candidate_json, revision, "
                                   "created_at, updated_at, work_state, agent_note, work_targets_json, work_outcome_json, "
                                   "filing_json) VALUES (NULL, 'candidate-1', ?, 1, ?, ?, NULL, '', '[]', '{}', '{}')",
                                   (json.dumps({"bucket": "context", "scope": "global", "tag": "fact",
                                                "text": "Retained factual projection", "created": STAMP}), STAMP, STAMP))
            uow.connection.execute("INSERT INTO learning_decisions (business_id, candidate_id, state, feedback, "
                                   "changes_json, decided_by, revision, created_at, updated_at) VALUES (NULL, "
                                   "'candidate-1', 'accepted', NULL, '{}', ?, 1, ?, ?)", (OWNER, STAMP, STAMP))
            uow.connection.execute("INSERT INTO learning_history (business_id, id, candidate_id, event, actor_id, "
                                   "before_json, after_json, created_at) VALUES (NULL, 'history-1', 'candidate-1', "
                                   "'learnings_decide', ?, '{}', '{\"state\":\"accepted\"}', ?)", (OWNER, STAMP))
            uow.connection.execute("INSERT INTO learning_runs (business_id, id, result_json, created_at) VALUES "
                                   "(NULL, 'run-1', '{\"secret\":\"never exported\"}', ?)", (STAMP,))
            uow.connection.execute("INSERT INTO learning_scan_state (business_id, session_id, transcript_hash, "
                                   "state_json, revision, created_at, updated_at) VALUES (NULL, 'session-1', ?, "
                                   "'{\"status\":\"processed\"}', 1, ?, ?)", ("a" * 64, STAMP, STAMP))
        with database.read() as uow:
            exported = catalog().export(uow)
        # The catalog composes against the exact target: every classified table exists. The only unclassified tables
        # are the separately owned Overseer tables (a pre-existing composition gap outside this removal, reported in the
        # task report); no removed learning table appears anywhere.
        assert all(name.startswith("overseer_") for name in exported.missing)
        assert not set(exported.missing) & set(removal.REMOVED_TABLES)
        names = set(exported.tables)
        assert names.isdisjoint(removal.REMOVED_TABLES)
        assert {"learning_candidates", "learning_decisions", "learning_history"} <= names
        assert "learning_runs" not in names and "learning_scan_state" not in names
        assert [row["id"] for row in exported.tables["learning_candidates"].rows] == ["candidate-1"]
        assert exported.tables["learning_decisions"].rows[0]["candidate_id"] == "candidate-1"
        assert exported.tables["learning_history"].rows[0]["event"] == "learnings_decide"
        classifications = {name: row for name, (_owner, row) in catalog().classifications.items()}
        assert classifications["learning_runs"].kind == PRIVATE_EXCLUDED
        assert classifications["learning_scan_state"].kind == TECHNICAL_EXCLUDED
        # CRM registrations stay exactly as classified by the Domain contribution (the test double when it stands in).
        from qorqut_operations import crm
        for row in crm.CLASSIFICATIONS:
            assert row.table in catalog().tables
    finally:
        database.close()
        for path in Path(paths.database).parent.glob("operations.sqlite3-*"):
            path.unlink(missing_ok=True)