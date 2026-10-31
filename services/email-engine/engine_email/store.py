"""Rebuildable mailbox cache and durable, non-replayable send receipts."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Email database must not be a symlink")
        with self.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "email_engine_metadata" not in tables:
                raise ValueError("Refusing to adopt an unrelated database")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS email_engine_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO email_engine_metadata VALUES(1,0);
                CREATE TABLE IF NOT EXISTS mailbox_bindings (
                    ref TEXT PRIMARY KEY, object_id TEXT NOT NULL UNIQUE,
                    business_id TEXT NOT NULL, address TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    mailbox TEXT NOT NULL, id TEXT NOT NULL, folder TEXT NOT NULL,
                    content TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, PRIMARY KEY(mailbox,id));
                CREATE INDEX IF NOT EXISTS messages_revision ON messages(mailbox,revision);
                CREATE TABLE IF NOT EXISTS folders (
                    mailbox TEXT NOT NULL, id TEXT NOT NULL, cursor TEXT,
                    synced_at TEXT, error TEXT, PRIMARY KEY(mailbox,id));
                CREATE TABLE IF NOT EXISTS mailbox_sync (
                    mailbox TEXT PRIMARY KEY, state TEXT NOT NULL,
                    synced_at TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS sends (
                    request_id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL,
                    draft_id TEXT, result TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def bind_mailboxes(self, mailboxes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for box in mailboxes:
                values = (box.ref, box.object_id, box.business_id, box.address)
                old = db.execute("SELECT ref,object_id,business_id,address FROM mailbox_bindings WHERE ref=? OR object_id=?",
                    (box.ref, box.object_id)).fetchall()
                if old and (len(old) != 1 or tuple(old[0]) != values):
                    raise ValueError("Existing mailbox identity or ownership cannot be reassigned")
                db.execute("INSERT OR IGNORE INTO mailbox_bindings VALUES(?,?,?,?)", values)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def revision(db):
        return db.execute("UPDATE email_engine_metadata SET revision=revision+1 WHERE singleton=1 RETURNING revision").fetchone()[0]

    def sync_page(self, mailbox, folder, items, cursor, *, complete):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                identity = item["id"]
                prior = db.execute("SELECT * FROM messages WHERE mailbox=? AND id=?", (mailbox, identity)).fetchone()
                deleted = "@removed" in item
                # A move can appear first in the destination, then as a removal in the old folder.
                if deleted and prior is not None and prior["folder"] != folder:
                    continue
                if deleted:
                    value = {"id": identity}
                else:
                    value = {**(json.loads(prior["content"]) if prior and not prior["deleted"] else {}), **item}
                content = json.dumps(value, sort_keys=True, separators=(",", ":"))
                if prior and prior["content"] == content and prior["deleted"] == deleted and prior["folder"] == folder:
                    continue
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?) ON CONFLICT(mailbox,id) DO UPDATE SET folder=excluded.folder,content=excluded.content,deleted=excluded.deleted,revision=excluded.revision",
                    (mailbox, identity, folder, content, int(deleted), self.revision(db)))
            db.execute("INSERT INTO folders VALUES(?,?,?,?,NULL) ON CONFLICT(mailbox,id) DO UPDATE SET cursor=excluded.cursor,synced_at=COALESCE(excluded.synced_at,folders.synced_at),error=NULL",
                (mailbox, folder, cursor, now() if complete else None))

    def folder(self, mailbox, folder):
        with self.connect() as db:
            row = db.execute("SELECT * FROM folders WHERE mailbox=? AND id=?", (mailbox, folder)).fetchone()
            return dict(row) if row else None

    def reset_folder(self, mailbox, folder):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._clear_folder(db, mailbox, folder)
            db.execute("UPDATE folders SET cursor=NULL,synced_at=NULL,error='resync_required' WHERE mailbox=? AND id=?", (mailbox, folder))

    def _clear_folder(self, db, mailbox, folder):
        for row in db.execute("SELECT id FROM messages WHERE mailbox=? AND folder=? AND deleted=0", (mailbox, folder)).fetchall():
            db.execute("UPDATE messages SET deleted=1,content=?,revision=? WHERE mailbox=? AND id=?",
                (json.dumps({"id": row["id"]}), self.revision(db), mailbox, row["id"]))

    def retain_folders(self, mailbox, identities):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id FROM folders WHERE mailbox=?", (mailbox,)).fetchall():
                if row["id"] not in identities:
                    self._clear_folder(db, mailbox, row["id"])
                    db.execute("DELETE FROM folders WHERE mailbox=? AND id=?", (mailbox, row["id"]))

    def mailbox_status(self, mailbox, state, error=None):
        with self.connect() as db:
            db.execute("INSERT INTO mailbox_sync VALUES(?,?,?,?) ON CONFLICT(mailbox) DO UPDATE SET state=excluded.state,synced_at=COALESCE(excluded.synced_at,mailbox_sync.synced_at),error=excluded.error",
                (mailbox, state, now() if state == "synced" else None, error))

    def sync_error(self, mailbox, folder, code):
        with self.connect() as db:
            db.execute("INSERT INTO folders(mailbox,id,error) VALUES(?,?,?) ON CONFLICT(mailbox,id) DO UPDATE SET error=excluded.error", (mailbox, folder, code))

    def messages(self, mailbox, *, after=0, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE mailbox=? AND revision>? ORDER BY revision LIMIT ?", (mailbox, after, limit)).fetchall()
            items = [{"mailbox": mailbox, "revision": row["revision"], "deleted": bool(row["deleted"]),
                "message": json.loads(row["content"])} for row in rows]
            return {"items": items, "cursor": rows[-1]["revision"] if rows else after}

    def message(self, mailbox, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE mailbox=? AND id=? AND deleted=0", (mailbox, identity)).fetchone()
            return json.loads(row["content"]) if row else None

    def sync_status(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM mailbox_sync ORDER BY mailbox")]

    def begin_send(self, message):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (message.request_id,)).fetchone()
            if row:
                if row["digest"] != message.digest():
                    raise ValueError("Request ID already belongs to different content")
                return dict(row), False
            db.execute("INSERT INTO sends(request_id,business_id,mailbox,digest,payload,state,updated_at) VALUES(?,?,?,?,?,'creating',?)",
                (message.request_id, message.business_id, message.mailbox, message.digest(), message.model_dump_json(), now()))
            return dict(db.execute("SELECT * FROM sends WHERE request_id=?", (message.request_id,)).fetchone()), True

    def save_send(self, request_id, state, *, draft_id=None, result=None):
        with self.connect() as db:
            db.execute("UPDATE sends SET state=?,draft_id=COALESCE(?,draft_id),result=?,updated_at=? WHERE request_id=?",
                (state, draft_id, json.dumps(result or {}), now(), request_id))
        return self.send(request_id)

    def send(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def pending_sends(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sends WHERE state IN ('creating','draft','sending','accepted','uncertain') ORDER BY updated_at LIMIT 100")]

    @staticmethod
    def receipt(row):
        return {"request_id": row["request_id"], "business_id": row["business_id"], "mailbox": row["mailbox"],
            "state": row["state"], "draft_id": row["draft_id"], "updated_at": row["updated_at"],
            "result": json.loads(row["result"])}
