"""A synthetic pre-CRM store for the offline upgrade checks.

``fixtures/crm_baseline_schema.json`` holds the frozen DDL of the pre-CRM target (generated once from the base commit's
composition; it holds no row). ``create`` replays it into a new file exactly as ``Database.initialize_target`` would have, with
the base identity, and ``populate`` adds invented rows - a business, a few principals, Tasks, Tables, discussion, follows,
private conversations and the change-feed rows the capture triggers write. Nothing here is, or was derived from, a live store."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.sqlfunctions import install_sql_functions

FIXTURE = Path(__file__).with_name("fixtures") / "crm_baseline_schema.json"
STAMP = "2026-03-01T10:00:00Z"
OWNER, ORDUBASHY, WORKER = "principal-owner", "principal-ordubashy", "principal-worker"
BUSINESS = "business-one"


def definition():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def create(path):
    """The empty pre-CRM store at ``path`` (DELETE journaling, like a stopped, cleanly closed instance)."""
    data = definition()
    connection = sqlite3.connect(path, isolation_level=None)
    install_sql_functions(connection)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(data["identity_ddl"])
    for _owner, statements in data["schema"]:
        for statement in statements:
            connection.execute(statement)
    target = data["target"]
    connection.execute("INSERT INTO target_identity VALUES ('target', ?, ?, ?, ?)", (target["name"], target["version"], target["digest"], STAMP))
    connection.execute(f"PRAGMA user_version = {target['version']}")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute("COMMIT")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def populate(connection):
    """Invented rows across the tables the upgrade rebuilds or must carry unchanged. Returns the identifiers by role."""
    run = connection.execute
    for identity, kind, name in ((OWNER, "human", "Synthetic Owner"), (ORDUBASHY, "agent", "Synthetic Coordinator"), (WORKER, "agent", "Synthetic Worker")):
        run("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES (?,?,?,?,?)", (identity, kind, name, STAMP, STAMP))
    run("INSERT INTO businesses(id,slug,workspace,hq_path,name,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (BUSINESS, "synthetic-one", "synthetic-one", "hq/synthetic-one", "Synthetic One", STAMP, STAMP))
    for business, identity, title in ((BUSINESS, "task-a", "Synthetic follow-up A"), (BUSINESS, "task-b", "Synthetic follow-up B"),
                                      (None, "task-g", "Synthetic global task")):
        run("INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (business, identity, title, "open", OWNER, STAMP, STAMP))
    for business, identity, title in ((BUSINESS, "table-a", "Synthetic ledger A"), (None, "table-g", "Synthetic global table")):
        run("INSERT INTO data_tables(business_id,id,title,columns_json,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (business, identity, title, json.dumps([{"id": "name", "type": "text"}]), OWNER, STAMP, STAMP))
    for index, (business, item_type, item_id, kind, body) in enumerate((
            (BUSINESS, "task", "task-a", "comment", "Synthetic comment one"),
            (BUSINESS, "task", "task-a", "system", "Synthetic system note"),
            (BUSINESS, "task", "task-b", "comment", "Synthetic comment two"),
            (None, "task", "task-g", "comment", "Synthetic global comment"))):
        run("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,refs_json,author_id,recorder_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (business, f"message-{index}", item_type, item_id, kind, body,
             json.dumps([{"type": "task", "id": "task-b"}]) if index == 0 else "[]", None if kind == "system" else OWNER, OWNER, f"2026-03-01T10:0{index}:00Z"))
    run("INSERT INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)", (BUSINESS, "task", "task-a", OWNER, 1, 0, "creator", STAMP, STAMP))
    run("INSERT INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)", (BUSINESS, "task", "task-b", WORKER, 0, 1, "explicit", STAMP, STAMP))
    for identity, source_type, source_id, business in (("conversation-a", "task", "task-a", BUSINESS), ("conversation-g", "project", "project-g", None)):
        run("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,latest_sequence,created_at,updated_at,peer_agent_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (identity, f"{source_type}:{business or 'global'}:{source_id}", business, source_type, source_id, "owner_ordubashy", 0, STAMP, STAMP))
    for identity, conversation, author, role in (("private-1", "conversation-a", OWNER, "owner"), ("private-2", "conversation-a", ORDUBASHY, "coordinator"),
                                                  ("private-3", "conversation-a", OWNER, "owner"), ("private-4", "conversation-g", OWNER, "owner")):
        run("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) VALUES (?,?,?,?,?,?)",
            (identity, conversation, author, role, f"Synthetic private body {identity}", STAMP))
    # Private messages are immutable, so a retired id is simulated by raising the counter above the highest remaining one: the
    # upgrade must carry the counter, not recompute it from the rows.
    run("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'owner_conversation_messages'")
    return {"tasks": ("task-a", "task-b", "task-g"), "tables": ("table-a", "table-g"), "conversations": ("conversation-a", "conversation-g")}


def tables(connection):
    """``{table: [rows ordered by rowid, as tuples]}`` for every ordinary table - a full content snapshot for comparisons."""
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
    return {name: [tuple(row) for row in connection.execute(f'SELECT rowid, * FROM "{name}" ORDER BY rowid')] for name in names}
