"""Rebuildable mailbox cache and durable, non-replayable send receipts."""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = path
        self.history = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Email database must not be a symlink")
        with self.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "email_engine_metadata" not in tables:
                raise ValueError("Refusing to adopt an unrelated database")
            if "mailbox_bindings" in tables and "folder_id" not in {row[1] for row in db.execute("PRAGMA table_info(mailbox_bindings)")}:
                raise ValueError("Use a fresh Fastmail database; preserve the prior provider ledger separately")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS email_engine_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO email_engine_metadata VALUES(1,0);
                CREATE TABLE IF NOT EXISTS mailbox_bindings (
                    ref TEXT PRIMARY KEY, folder_id TEXT NOT NULL UNIQUE,
                    business_id TEXT NOT NULL, address TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS address_bindings (
                    address TEXT PRIMARY KEY, mailbox TEXT NOT NULL);
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
                CREATE TABLE IF NOT EXISTS sender_bindings (
                    ref TEXT PRIMARY KEY, address TEXT NOT NULL UNIQUE, account_ref TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sender_reservations (
                    request_id TEXT PRIMARY KEY REFERENCES sends(request_id), account_ref TEXT NOT NULL,
                    state TEXT NOT NULL, used_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sender_quota ON sender_reservations(account_ref,state,used_at);
                CREATE TABLE IF NOT EXISTS history_outbox (
                    event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, envelope TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def _history(self, db, request_id, kind):
        if self.history is None:
            return
        row = dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())
        row["payload"], row["result"] = json.loads(row["payload"]), json.loads(row["result"])
        record = self.history.seal("engine:" + str(uuid.uuid4()), kind, row)
        db.execute("INSERT INTO history_outbox VALUES(?,?,?)", record)

    def flush_history(self):
        if self.history is None:
            return
        # Copy encrypted records first; deletion follows a durable idempotent append.
        while True:
            with self.connect() as db:
                record = db.execute("SELECT event_id,digest,envelope FROM history_outbox LIMIT 1").fetchone()
            if record is None:
                return
            self.history.append_sealed(tuple(record))
            with self.connect() as db:
                db.execute("DELETE FROM history_outbox WHERE event_id=?", (record[0],))

    def bind_senders(self, senders):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for sender in senders:
                values = (sender.ref, sender.address, sender.account_ref)
                old = db.execute("SELECT ref,address,account_ref FROM sender_bindings WHERE ref=? OR address=?",
                    (sender.ref, sender.address)).fetchall()
                if old and (len(old) != 1 or tuple(old[0]) != values):
                    raise ValueError("Shared sender identity and account cannot be reassigned")
                db.execute("INSERT OR IGNORE INTO sender_bindings VALUES(?,?,?)", values)
                # Adding a quota cannot erase already-recorded sends from that address.
                for row in db.execute("SELECT * FROM sends WHERE json_extract(payload,'$.from_address')=?",
                        (sender.address,)).fetchall():
                    if row["state"] in {"failed", "canceled", "queued"}:
                        continue
                    db.execute("INSERT OR IGNORE INTO sender_reservations VALUES(?,?,?,?)",
                        (row["request_id"], sender.account_ref, "sent" if row["state"] == "sent" else "reserved", row["updated_at"]))

    def bind_provider(self, account_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT account_id FROM provider_binding WHERE singleton=1").fetchone()
            if old and old[0] != account_id:
                raise ValueError("Fastmail account identity cannot be reassigned")
            db.execute("INSERT OR IGNORE INTO provider_binding VALUES(1,?)", (account_id,))

    def bind_mailboxes(self, mailboxes):
        changed = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for box in mailboxes:
                values = (box.ref, box.folder_id, box.business_id, box.address)
                old = db.execute("SELECT ref,folder_id,business_id,address FROM mailbox_bindings WHERE ref=? OR folder_id=?",
                    (box.ref, box.folder_id)).fetchall()
                if old and (len(old) != 1 or tuple(old[0]) != values):
                    raise ValueError("Existing mailbox identity or ownership cannot be reassigned")
                changed = changed or not old
                db.execute("INSERT OR IGNORE INTO mailbox_bindings VALUES(?,?,?,?)", values)
                for address in {box.address, *box.aliases}:
                    prior = db.execute("SELECT mailbox FROM address_bindings WHERE address=?", (address,)).fetchone()
                    if prior and prior[0] != box.ref:
                        raise ValueError("Existing address ownership cannot be reassigned")
                    db.execute("INSERT OR IGNORE INTO address_bindings VALUES(?,?)", (address, box.ref))
        return changed

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
                if deleted and prior is None:
                    continue
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

    def mailbox_status(self, mailbox, state, error=None):
        with self.connect() as db:
            db.execute("INSERT INTO mailbox_sync VALUES(?,?,?,?) ON CONFLICT(mailbox) DO UPDATE SET state=excluded.state,synced_at=COALESCE(excluded.synced_at,mailbox_sync.synced_at),error=excluded.error",
                (mailbox, state, now() if state == "synced" else None, error))

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

    def begin_send(self, message, *, queued=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (message.request_id,)).fetchone()
            if row:
                if row["digest"] != message.digest():
                    raise ValueError("Request ID already belongs to different content")
                return dict(row), False
            db.execute("INSERT INTO sends(request_id,business_id,mailbox,digest,payload,state,updated_at) VALUES(?,?,?,?,?,?,?)",
                (message.request_id, message.business_id, message.mailbox, message.digest(), message.model_dump_json(),
                 "queued" if queued else "creating", now()))
            self._history(db, message.request_id, "send.prepared")
            return dict(db.execute("SELECT * FROM sends WHERE request_id=?", (message.request_id,)).fetchone()), True

    def reserve(self, request_id, sender):
        """Atomically claim one account slot; unresolved reservations never age out."""
        instant = now()
        cutoff = (datetime.fromisoformat(instant) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone()
            if row["state"] not in {"queued", "creating", "draft"}:
                return False, dict(row)
            existing = db.execute("SELECT * FROM sender_reservations WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["account_ref"] != sender.account_ref or existing["state"] != "reserved":
                    raise ValueError("Invalid sender reservation")
                return True, dict(row)
            used = db.execute("SELECT * FROM sender_reservations WHERE account_ref=? AND (state='reserved' OR (state='sent' AND used_at>?))",
                (sender.account_ref, cutoff)).fetchall()
            if len(used) >= sender.rolling_limit:
                releases = sorted(datetime.fromisoformat(item["used_at"]) + timedelta(hours=24)
                    for item in used if item["state"] == "sent")
                needed = len(used) - sender.rolling_limit + 1
                retry = releases[needed - 1].isoformat() if len(releases) >= needed else None
                db.execute("UPDATE sends SET result=?,updated_at=? WHERE request_id=?",
                    (json.dumps({"reason": "sender_quota", "retry_at": retry}), instant, request_id))
                return False, dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())
            db.execute("INSERT INTO sender_reservations VALUES(?,?,'reserved',?)", (request_id, sender.account_ref, instant))
            db.execute("UPDATE sends SET state=CASE WHEN state='queued' THEN 'creating' ELSE state END,result='{}',updated_at=? WHERE request_id=?",
                (instant, request_id))
            self._history(db, request_id, "send.reserved")
            return True, dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())

    def cancel_queued(self, request_id, mailbox):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=? AND mailbox=? AND business_id=?",
                (request_id, mailbox.ref, mailbox.business_id)).fetchone()
            if row is None:
                raise ValueError("Send not found")
            if row["state"] == "queued":
                db.execute("UPDATE sends SET state='canceled',result=?,updated_at=? WHERE request_id=?",
                    (json.dumps({"reason": "owner_canceled", "not_sent": True}), now(), request_id))
                self._history(db, request_id, "send.canceled")
            return dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())

    def save_send(self, request_id, state, *, draft_id=None, result=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE sends SET state=?,draft_id=COALESCE(?,draft_id),result=?,updated_at=? WHERE request_id=?",
                (state, draft_id, json.dumps(result or {}), now(), request_id))
            if state in {"failed", "canceled"}:
                db.execute("UPDATE sender_reservations SET state='released' WHERE request_id=?", (request_id,))
            elif state == "sent":
                # Confirmation time is conservative if the provider's send timestamp is unavailable.
                db.execute("UPDATE sender_reservations SET state='sent',used_at=? WHERE request_id=? AND state='reserved'", (now(), request_id))
            self._history(db, request_id, "send." + state)
        return self.send(request_id)

    def send(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def pending_sends(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sends WHERE state IN ('queued','creating','draft','sending','accepted','uncertain') ORDER BY updated_at LIMIT 100")]

    @staticmethod
    def receipt(row):
        return {"request_id": row["request_id"], "business_id": row["business_id"], "mailbox": row["mailbox"],
            "sender_ref": json.loads(row["payload"]).get("sender_ref"),
            "state": row["state"], "draft_id": row["draft_id"], "updated_at": row["updated_at"],
            "result": json.loads(row["result"])}
