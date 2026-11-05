"""Focused synthetic before/after evidence for the one offline Project-source conversion.

A store is composed from a *before* assembly (the same host composition, with the pre-feature
``owner_conversations`` CHECK and without ``project_item_links``), filled with synthetic private rows, then converted
to the *after* assembly by ``bazarlyk.migration.project_sources.upgrade``. The check proves:

* the before CHECK refuses ``project`` and the converted schema accepts it;
* the after target identity opens through the ordinary ``Database.open_existing`` validation;
* every private conversation key, id, peer binding, revision, message sequence/body, read cursor and change sequence
  is byte-for-byte what it was;
* the link table exists exactly once;
* the conversion refuses a store that is not the before composition;
* a failure inside the unit rolls the entire conversion back.

Nothing here is called by startup, a service, a release worker or deployment: only this module invokes the
conversion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.assembly import AssemblyBuilder
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.composition.assembly import host_assembly, host_contributions
from bazarlyk.migration import project_sources
from bazarlyk.service.changes import ChangeFeed

NOW = "2026-10-27T12:00:00Z"
# The accepted Overseer integration's own additive storage owner; it is not part of this conversion's pair.
OVERSEER_OWNER = "overseer"
# Recorded from the immutable pre-feature source trees (host ceb46263 + Operations 6ff63572 + Context 8ba00f6b),
# not only from this test's reverse-derived fixture.
PRE_FEATURE_DIGEST = "a8e1f55391c02eaa948c0c89f0e3eef7ab4dcaa58d95dabbf2ef48c7e63438b6"
PRE_FEATURE_OBJECTS = 509
PRE_FEATURE_OBJECT_HASH = "be828d9f7fd1d04cdb4f409c15feb9470667691ab5d7e34e8e940b19466510a5"


def object_hash(objects):
    payload = json.dumps(sorted([list(key) + [value[0], value[1]] for key, value in objects.items()]),
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def connect(path):
    connection = sqlite3.connect(path)
    install_sql_functions(connection)
    return connection


def project_lineage(feed):
    """The host contributions this conversion's recorded identities are built from.

    The accepted Overseer integration added its own storage contribution to the host composition after both identities
    below were recorded (``PRE_FEATURE_*`` from the immutable pre-feature source trees and the approved after target).
    That contribution is a separate, additive unit applied by its own migration, so this conversion is qualified on the
    Project lineage alone: the same host contributions without the Overseer storage owner."""
    return [contribution for contribution in host_contributions(feed) if contribution.owner != OVERSEER_OWNER]


def assemblies():
    """The feature's before and after host compositions, built from the same trusted contributions."""
    feed = ChangeFeed()
    full = host_assembly(feed)
    builder = AssemblyBuilder(target_name=full.target.name, target_version=full.target.version)
    for contribution in project_lineage(feed):
        builder.add(contribution)
    after = builder.freeze()
    builder = AssemblyBuilder(target_name=full.target.name, target_version=full.target.version)
    for contribution in project_lineage(feed):
        if "project_item_links" in getattr(contribution, "tables", {}):
            continue
        if any("CREATE TABLE owner_conversations" in statement for statement in getattr(contribution, "schema", ())):
            contribution = replace(contribution, schema=tuple(
                statement.replace("'reference','project'", "'reference'") for statement in contribution.schema))
        builder.add(contribution)
    return builder.freeze(), after


def before_store(tmp_path):
    """A before store with several private channels: messages with a sequence gap, a peer binding, unread/pre-read cursors."""
    before, after = assemblies()
    path = tmp_path / "target.sqlite3"
    Database(path, assembly=before).initialize_target()
    owner, coordinator, project = new_id(), new_id(), new_id()
    task_channel, task_message = new_id(), new_id()
    peer_channel, peer_source = new_id(), new_id()
    empty_channel, empty_source = new_id(), new_id()
    with connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (owner, "human", "Owner", NOW, NOW))
        connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (coordinator, "agent", "Ordubashy", NOW, NOW))
        connection.execute(
            "INSERT INTO businesses(id,slug,workspace,hq_path,name,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (project, "harbor", str(tmp_path / "harbor"), str(tmp_path / "harbor" / "hq"), "Harbor", NOW, NOW))
        connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,"
            "latest_sequence,created_at,updated_at,peer_agent_id) VALUES(?,?,?,'task',?,'owner_ordubashy',3,1,?,?,NULL)",
            (task_channel, f"source:{project}:task:{task_message}", project, task_message, NOW, NOW))
        connection.execute(
            "INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,summary,created_at)"
            " VALUES(?,?,?,'owner',?,?,?)",
            (new_id(), task_channel, owner, "A private note", "Note", NOW))
        last = connection.execute("SELECT max(sequence) FROM owner_conversation_messages").fetchone()[0]
        connection.execute("UPDATE owner_conversations SET latest_sequence=? WHERE id=?", (last, task_channel))
        connection.execute("INSERT INTO owner_conversation_reads(conversation_id,principal_id,last_read_sequence,"
                           "updated_at) VALUES(?,?,?,?)", (task_channel, owner, last, NOW))
        # A peer-bound channel whose messages contain a real sequence gap (a deleted middle message).
        connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,"
            "latest_sequence,created_at,updated_at,peer_agent_id) VALUES(?,?,?,'signoff',?,'owner_ordubashy',5,3,?,?,?)",
            (peer_channel, f"source:{project}:signoff:{peer_source}", project, peer_source, NOW, NOW, coordinator))
        for sequence, role, body in ((2, "owner", "First"), (4, "coordinator", "Second"), (5, "owner", "Third")):
            connection.execute(
                "INSERT INTO owner_conversation_messages(sequence,id,conversation_id,created_by,author_role,body,summary,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sequence, new_id(), peer_channel, owner if role == "owner" else coordinator, role, body, None, NOW))
        connection.execute("INSERT INTO owner_conversation_reads(conversation_id,principal_id,last_read_sequence,"
                           "updated_at) VALUES(?,?,?,?)", (peer_channel, coordinator, 2, NOW))
        # An empty channel is preserved too; reads/link/list must never create a message for it.
        connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,"
            "latest_sequence,created_at,updated_at,peer_agent_id) VALUES(?,?,?,'reference',?,'owner_ordubashy',1,0,?,?,NULL)",
            (empty_channel, f"source:{project}:reference:{empty_source}", project, empty_source, NOW, NOW))
        connection.execute("INSERT INTO change_sequences(name,value) VALUES('synthetic_conversion_probe',7)")
        connection.commit()
    return path, before, after, (owner, project, task_channel, peer_channel, empty_channel, len((task_channel, peer_channel, empty_channel)))


def test_the_before_fixture_matches_the_immutable_pre_feature_source(tmp_path):
    before, _after = assemblies()
    assert before.target.name == "qorqut-v1.2" and before.target.version == 1
    assert before.target.digest == PRE_FEATURE_DIGEST
    objects = compile_target(before).objects
    assert len(objects) == PRE_FEATURE_OBJECTS
    assert object_hash(objects) == PRE_FEATURE_OBJECT_HASH


def private_rows(path):
    with connect(path) as connection:
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
                for table in ("owner_conversations", "owner_conversation_messages", "owner_conversation_reads",
                              "change_sequences")}


def test_the_conversion_widens_the_private_source_and_preserves_every_private_fact(tmp_path):
    path, before, after, (owner, project, conversation, peer_channel, empty_channel, conversations) = before_store(tmp_path)
    project_conversation = new_id()
    # The old CHECK refuses a Project source; the widened one accepts it.
    with connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,created_at,"
                "updated_at) VALUES(?,?,?,'project',?,'owner_ordubashy',?,?)",
                (project_conversation, f"source:{project}:project:{project}", project, project, NOW, NOW))
    kept = private_rows(path)
    summary = project_sources.upgrade(path, before=before, after=after)
    assert summary == {"rebuilt": "owner_conversations", "created": "project_item_links", "conversations": conversations,
                       "target_digest": after.target.digest, "target_version": after.target.version}
    # The ordinary open validation accepts the converted store.
    Database(path, assembly=after).open_existing()
    assert private_rows(path) == kept
    with connect(path) as connection:
        conversation_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='owner_conversations'").fetchone()[0]
        assert "'project'" in conversation_sql
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                  "AND name='project_item_links'").fetchone()[0] == 1
        assert connection.execute(f'SELECT digest FROM "{IDENTITY_TABLE}"').fetchone()[0] == after.target.digest
        connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,created_at,"
            "updated_at) VALUES(?,?,?,'project',?,'owner_ordubashy',?,?)",
            (project_conversation, f"source:{project}:project:{project}", project, project, NOW, NOW))
        connection.commit()
        assert connection.execute("SELECT source_type FROM owner_conversations WHERE id=?",
                                  (project_conversation,)).fetchone()[0] == "project"
    # The converted store is not the before composition any more: a repeated conversion refuses and changes nothing.
    with pytest.raises(DomainError) as refused:
        project_sources.upgrade(path, before=before, after=after)
    assert refused.value.code == "invalid_database"


def test_a_failure_inside_the_unit_rolls_the_whole_conversion_back(tmp_path, monkeypatch):
    path, before, after, _ids = before_store(tmp_path)
    with connect(path) as connection:
        kept_objects = read_objects(connection)
    kept_rows = private_rows(path)

    def fail(*_arguments, **_keywords):
        raise DomainError("invalid_database", "Synthetic verification failure")

    with monkeypatch.context() as patch:
        patch.setattr(project_sources, "_verify", fail)
        with pytest.raises(DomainError) as failed:
            project_sources.upgrade(path, before=before, after=after)
    assert failed.value.code == "invalid_database"
    with connect(path) as connection:
        assert read_objects(connection) == kept_objects
        assert connection.execute(f'SELECT digest FROM "{IDENTITY_TABLE}"').fetchone()[0] == before.target.digest
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                  "AND name='project_item_links'").fetchone()[0] == 0
    assert private_rows(path) == kept_rows
    # The same store converts cleanly after the injected failure: the rollback left no half-applied schema.
    project_sources.upgrade(path, before=before, after=after)
    Database(path, assembly=after).open_existing()


def test_the_conversion_requires_an_existing_target_path(tmp_path):
    before, after = assemblies()
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(DomainError) as refused:
        project_sources.upgrade(missing, before=before, after=after)
    assert refused.value.code == "invalid_database"
    assert not missing.exists()


def test_the_conversion_refuses_a_store_that_is_not_the_before_composition(tmp_path):
    before, after = assemblies()
    path = tmp_path / "after.sqlite3"
    Database(path, assembly=after).initialize_target()
    before_rows = private_rows(path) if path.exists() else {}
    with pytest.raises(DomainError) as refused:
        project_sources.upgrade(path, before=before, after=after)
    assert refused.value.code == "invalid_database"
    # A store composed from the after composition is untouched: no copy, no identity rewrite.
    with connect(path) as connection:
        assert connection.execute(f'SELECT digest FROM "{IDENTITY_TABLE}"').fetchone()[0] == after.target.digest
        assert read_objects(connection) == compile_target(after).objects
    assert private_rows(path) == before_rows


def test_the_conversion_is_narrow_and_refuses_an_unrelated_schema_change(tmp_path):
    before, after = assemblies()
    builder = AssemblyBuilder(target_name=after.target.name, target_version=after.target.version)
    for contribution in host_contributions(ChangeFeed()):
        if any("CREATE TABLE focus_cards" in statement for statement in getattr(contribution, "schema", ())):
            # One unrelated schema change in another owner, for the narrowness check only.
            contribution = replace(contribution, schema=tuple(
                statement.replace("CREATE TABLE focus_cards (", 'CREATE TABLE focus_cards (\n "unused_probe" TEXT,', 1)
                for statement in contribution.schema))
        builder.add(contribution)
    changed = builder.freeze()
    assert changed.target.digest != after.target.digest
    path = tmp_path / "narrow.sqlite3"
    Database(path, assembly=before).initialize_target()
    with pytest.raises(DomainError) as refused:
        project_sources.upgrade(path, before=before, after=changed)
    assert refused.value.code == "invalid_database"
    # The refusal happens before a single row is copied or an object created.
    with connect(path) as connection:
        assert read_objects(connection) == compile_target(before).objects


def test_the_conversion_refuses_another_owner_conversations_rewrite(tmp_path):
    before, after = assemblies()
    builder = AssemblyBuilder(target_name=after.target.name, target_version=after.target.version)
    for contribution in host_contributions(ChangeFeed()):
        schema = tuple(statement.replace(
            "created_at TEXT NOT NULL",
            "created_at TEXT NOT NULL DEFAULT ''") if "CREATE TABLE owner_conversations" in statement else statement
            for statement in getattr(contribution, "schema", ()))
        builder.add(replace(contribution, schema=schema) if schema != tuple(getattr(contribution, "schema", ())) else contribution)
    changed = builder.freeze()
    assert changed.target.digest != after.target.digest
    path = tmp_path / "rewrite.sqlite3"
    Database(path, assembly=before).initialize_target()
    with pytest.raises(DomainError) as refused:
        project_sources.upgrade(path, before=before, after=changed)
    assert refused.value.code == "invalid_database"
    with connect(path) as connection:
        assert read_objects(connection) == compile_target(before).objects