"""Rebuildable mailbox cache and durable, non-replayable send receipts."""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .fastmail import internet_message_id


SHARED_ROLLING_LIMIT = 150


def now():
    return datetime.now(timezone.utc).isoformat()


def legacy_cold_label(send_row):
    """(workflow_owner, contact_key, business_id) for a labelled legacy /sends row, else None."""
    try:
        payload = json.loads(send_row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    stop, owner = payload.get("stop_key"), payload.get("workflow_owner")
    if payload.get("category") != "cold_outreach" or not owner or not isinstance(stop, dict):
        return None
    if not stop.get("contact_key") or not stop.get("business_id"):
        return None
    return (owner, stop["contact_key"], stop["business_id"])


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
                CREATE TABLE IF NOT EXISTS managed_mailboxes (
                    ref TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS mailbox_setting_requests (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mailbox_bindings (
                    ref TEXT PRIMARY KEY, folder_id TEXT NOT NULL UNIQUE,
                    business_id TEXT NOT NULL, address TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outlook_provider_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_ref TEXT NOT NULL, address TEXT NOT NULL);
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
                CREATE TABLE IF NOT EXISTS external_sender_sends (
                    account_ref TEXT NOT NULL, provider_id TEXT NOT NULL,
                    internet_message_id TEXT NOT NULL, sent_at TEXT NOT NULL,
                    PRIMARY KEY(account_ref,provider_id));
                CREATE TABLE IF NOT EXISTS history_outbox (
                    event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_operations (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, action TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS integration_intents (
                    client_id TEXT NOT NULL, intent_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                    payload_digest TEXT NOT NULL, workflow_owner TEXT NOT NULL, business_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL, category TEXT NOT NULL, campaign_id TEXT, enrollment_id TEXT,
                    step_index INTEGER, manifest_digest TEXT, stop_contact_key TEXT NOT NULL,
                    stop_business_id TEXT NOT NULL, correlation TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, PRIMARY KEY(client_id, intent_id));
                CREATE INDEX IF NOT EXISTS integration_intents_request ON integration_intents(request_id);
                CREATE TABLE IF NOT EXISTS integration_stops (
                    contact_key TEXT NOT NULL, business_id TEXT NOT NULL, workflow_owner TEXT NOT NULL,
                    reason TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(contact_key, business_id));
                CREATE TABLE IF NOT EXISTS integration_handoffs (
                    thread_or_message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, client_id TEXT NOT NULL,
                    workflow_owner TEXT NOT NULL, payload_digest TEXT NOT NULL, result TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(thread_or_message_id, idempotency_key));
                CREATE TABLE IF NOT EXISTS message_correlations (
                    mailbox TEXT NOT NULL, message_id TEXT NOT NULL, is_sent_copy INTEGER NOT NULL,
                    workflow_owner TEXT, intent_id TEXT, request_id TEXT, correlation TEXT,
                    computed_at TEXT NOT NULL, PRIMARY KEY(mailbox, message_id));
                CREATE TABLE IF NOT EXISTS correlation_cursor (
                    mailbox TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            """)
            # Never reinterpret a saved approval when an owner changes transport.
            # This additive column preserves every legacy payload and wire digest.
            db.execute("BEGIN IMMEDIATE")
            if "transport" not in {row[1] for row in db.execute("PRAGMA table_info(sends)")}:
                db.execute("ALTER TABLE sends ADD COLUMN transport TEXT NOT NULL DEFAULT 'fastmail' CHECK(transport IN ('fastmail','outlook_web'))")
            if "draft_internet_message_id" not in {row[1] for row in db.execute("PRAGMA table_info(sends)")}:
                db.execute("ALTER TABLE sends ADD COLUMN draft_internet_message_id TEXT")
            # Stops were once keyed by workflow_owner too; fold them onto the contact scope.
            stops = [row[1] for row in db.execute("PRAGMA table_info(integration_stops)")]
            if stops[:3] == ["workflow_owner", "contact_key", "business_id"]:
                db.execute("ALTER TABLE integration_stops RENAME TO integration_stops_pre_scope")
                db.execute("""CREATE TABLE integration_stops (
                    contact_key TEXT NOT NULL, business_id TEXT NOT NULL, workflow_owner TEXT NOT NULL,
                    reason TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(contact_key, business_id))""")
                db.execute("INSERT OR IGNORE INTO integration_stops(contact_key,business_id,workflow_owner,"
                    "reason,source_kind,source_id,created_at) SELECT contact_key,business_id,workflow_owner,"
                    "reason,source_kind,source_id,created_at FROM integration_stops_pre_scope ORDER BY rowid")
                db.execute("DROP TABLE integration_stops_pre_scope")
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

    def bind_outlook_provider(self, config):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT account_ref,address FROM outlook_provider_binding WHERE singleton=1").fetchone()
            values = (config.account_ref, config.address)
            if old and tuple(old) != values:
                raise ValueError("Outlook account identity cannot be reassigned")
            db.execute("INSERT OR IGNORE INTO outlook_provider_binding VALUES(1,?,?)", values)

    def bind_mailboxes(self, mailboxes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.bind_mailboxes_in(db, mailboxes)

    @staticmethod
    def bind_mailboxes_in(db, mailboxes):
        changed = False
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

    def messages(self, mailbox, *, after=0, limit=100, exclude_sent_copies=False):
        """``correlation``/``is_sent_copy`` are additive annotations; the cursor
        always advances past filtered rows even when ``exclude_sent_copies``."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT m.*, c.is_sent_copy AS c_sent, c.workflow_owner AS c_owner, c.intent_id AS c_intent, "
                "c.request_id AS c_request, c.correlation AS c_correlation FROM messages m "
                "LEFT JOIN message_correlations c ON c.mailbox=m.mailbox AND c.message_id=m.id "
                "WHERE m.mailbox=? AND m.revision>? ORDER BY m.revision LIMIT ?", (mailbox, after, limit)).fetchall()
            items = []
            cursor = after
            for row in rows:
                cursor = row["revision"]
                sent_copy = bool(row["c_sent"])
                if exclude_sent_copies and sent_copy:
                    continue
                item = {"mailbox": mailbox, "revision": row["revision"], "deleted": bool(row["deleted"]),
                    "message": json.loads(row["content"]), "is_sent_copy": sent_copy}
                if row["c_owner"]:
                    item["correlation"] = {"workflow_owner": row["c_owner"], "intent_id": row["c_intent"],
                        "request_id": row["c_request"], **(json.loads(row["c_correlation"]) if row["c_correlation"] else {})}
                items.append(item)
            return {"items": items, "cursor": cursor}

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
            db.execute("INSERT INTO sends(request_id,business_id,mailbox,digest,payload,state,updated_at,transport) VALUES(?,?,?,?,?,?,?,?)",
                (message.request_id, message.business_id, message.mailbox, message.digest(), message.model_dump_json(),
                 "queued" if queued else "creating", now(), message.transport or "fastmail"))
            self._history(db, message.request_id, "send.prepared")
            return dict(db.execute("SELECT * FROM sends WHERE request_id=?", (message.request_id,)).fetchone()), True

    @staticmethod
    def quota_usage(db, cutoff, account_ref=None):
        # Keep removed/disabled accounts in the aggregate until their usage expires.
        used = db.execute("SELECT * FROM sender_reservations WHERE "
            "(state='reserved' OR (state='sent' AND used_at>?))" +
            (" AND account_ref=?" if account_ref is not None else ""),
            (cutoff, account_ref) if account_ref is not None else (cutoff,)).fetchall()
        native_ids, internet_ids = set(), set()
        for item in used:
            saved = db.execute("SELECT * FROM sends WHERE request_id=? AND transport='outlook_web'",
                (item["request_id"],)).fetchone()
            if saved:
                result = json.loads(saved["result"])
                account = item["account_ref"]
                native_ids.update((account, value) for value in
                    (saved["draft_id"], result.get("outlook_message_id")) if value)
                for value in (saved["draft_internet_message_id"], result.get("internet_message_id"), result.get("native_internet_message_id")):
                    if value:
                        internet_ids.add((account, value.strip("<> ")))
        observed = []
        for item in db.execute("SELECT * FROM external_sender_sends WHERE sent_at>?" +
                (" AND account_ref=?" if account_ref is not None else "") + " ORDER BY sent_at DESC",
                (cutoff, account_ref) if account_ref is not None else (cutoff,)):
            native = (item["account_ref"], item["provider_id"])
            internet = (item["account_ref"], item["internet_message_id"])
            if native in native_ids or item["internet_message_id"] and internet in internet_ids:
                continue
            observed.append(item)
            if item["internet_message_id"]:
                internet_ids.add(internet)
        return used, observed

    @staticmethod
    def quota_retry(used, observed, limit):
        releases = sorted([
            *(datetime.fromisoformat(item["used_at"]) + timedelta(hours=24)
                for item in used if item["state"] == "sent"),
            *(datetime.fromisoformat(item["sent_at"]) + timedelta(hours=24) for item in observed)])
        needed = len(used) + len(observed) - limit + 1
        return releases[needed - 1].isoformat() if len(releases) >= needed else None

    def shared_quota(self):
        instant = now()
        cutoff = (datetime.fromisoformat(instant) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN")
            used, observed = self.quota_usage(db, cutoff)
            queued = db.execute("SELECT COUNT(*) FROM sends WHERE state='queued' AND "
                "json_extract(payload,'$.sender_ref') IS NOT NULL").fetchone()[0]
        count = len(used) + len(observed)
        return {"limit": SHARED_ROLLING_LIMIT, "window_hours": 24,
            "scope": "all_shared_senders", "used": count,
            "reserved": sum(item["state"] == "reserved" for item in used),
            "observed_external": len(observed), "remaining": max(0, SHARED_ROLLING_LIMIT - count),
            "queued": queued, "as_of": instant, "external_usage": "last_observed"}

    def reserve(self, request_id, sender):
        """Atomically claim both account and installation capacity, once per send."""
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
            # One write transaction protects the aggregate across service/store instances.
            for account, limit, reason in ((None, SHARED_ROLLING_LIMIT, "shared_sender_quota"),
                    (sender.account_ref, sender.rolling_limit, "sender_quota")):
                used, observed = self.quota_usage(db, cutoff, account)
                admission_limit = limit + (1 if existing else 0)
                if len(used) + len(observed) >= admission_limit:
                    result = {"reason": reason, "retry_at": self.quota_retry(used, observed, admission_limit)}
                    db.execute("UPDATE sends SET result=?,updated_at=? WHERE request_id=?",
                        (json.dumps(result), instant, request_id))
                    return False, dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())
            if existing:
                return True, dict(row)
            db.execute("INSERT INTO sender_reservations VALUES(?,?,'reserved',?)", (request_id, sender.account_ref, instant))
            db.execute("UPDATE sends SET state=CASE WHEN state='queued' THEN 'creating' ELSE state END,result='{}',updated_at=? WHERE request_id=?",
                (instant, request_id))
            self._history(db, request_id, "send.reserved")
            return True, dict(db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone())

    def observe_external_send(self, account_ref, provider_id, internet_message_id, sent_at):
        """Count observed Outlook sends, including manual sends, without replaying any."""
        instant = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        if instant.tzinfo is None or not account_ref or not provider_id:
            raise ValueError("Observed send requires account, provider ID and timezone")
        message_id = (internet_message_id or "").strip("<> ")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM external_sender_sends WHERE account_ref=? AND provider_id=?",
                (account_ref, provider_id)).fetchone()
            if prior and prior["internet_message_id"] and message_id and prior["internet_message_id"] != message_id:
                raise ValueError("Observed provider message identity cannot be reassigned")
            db.execute("INSERT INTO external_sender_sends VALUES(?,?,?,?) ON CONFLICT(account_ref,provider_id) "
                "DO UPDATE SET internet_message_id=CASE WHEN excluded.internet_message_id='' THEN internet_message_id ELSE excluded.internet_message_id END",
                (account_ref, provider_id, message_id, instant.astimezone(timezone.utc).isoformat()))

    def cancel_queued(self, request_id, mailbox):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=? AND mailbox=? AND business_id=?",
                (request_id, mailbox.ref, mailbox.business_id)).fetchone()
            if row is None:
                raise ValueError("Send not found")
            return self._cancel_if_queued_in(db, row, reason="owner_canceled")

    def cancel_send_if_queued(self, request_id, *, reason="owner_canceled"):
        """As ``cancel_queued``, but scoped by the caller (already checked ownership)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sends WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Send not found")
            return self._cancel_if_queued_in(db, row, reason=reason)

    def _cancel_if_queued_in(self, db, row, *, reason):
        # Only 'queued' is still cancellable; matches start_send's guard boundary.
        if row["state"] == "queued":
            db.execute("UPDATE sends SET state='canceled',result=?,updated_at=? WHERE request_id=?",
                (json.dumps({"reason": reason, "not_sent": True}), now(), row["request_id"]))
            self._history(db, row["request_id"], "send.canceled")
        return dict(db.execute("SELECT * FROM sends WHERE request_id=?", (row["request_id"],)).fetchone())

    def save_send(self, request_id, state, *, draft_id=None, draft_internet_message_id=None, result=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE sends SET state=?,draft_id=COALESCE(?,draft_id),draft_internet_message_id=COALESCE(draft_internet_message_id,?),result=?,updated_at=? WHERE request_id=?",
                (state, draft_id, draft_internet_message_id, json.dumps(result or {}), now(), request_id))
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

    def begin_account_operation(self, request_id, digest, action, payload):
        """Durably reserve an owner account operation before provider effects."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM account_operations WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("Request ID already belongs to different account content")
                return dict(row), False
            db.execute("INSERT INTO account_operations(request_id,digest,action,payload,state,result,updated_at) "
                "VALUES(?,?,?,?,?,'{}',?)", (request_id, digest, action, payload, "prepared", now()))
            return dict(db.execute("SELECT * FROM account_operations WHERE request_id=?", (request_id,)).fetchone()), True

    def save_account_operation(self, request_id, state, *, result=None):
        if state not in {"prepared", "completed", "failed", "uncertain"}:
            raise ValueError("Invalid account operation state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE account_operations SET state=?,result=?,updated_at=? WHERE request_id=?",
                (state, json.dumps(result or {}, sort_keys=True), now(), request_id))
            row = db.execute("SELECT * FROM account_operations WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Account operation not found")
            return dict(row)

    def save_account_operation_if_state(self, request_id, states, state, *, result=None):
        """Advance recovery state without overwriting a concurrent terminal receipt."""
        if state not in {"prepared", "completed", "failed", "uncertain"}:
            raise ValueError("Invalid account operation state")
        states = tuple(states)
        if not states:
            raise ValueError("At least one expected state is required")
        placeholders = ",".join("?" for _ in states)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"UPDATE account_operations SET state=?,result=?,updated_at=? "
                f"WHERE request_id=? AND state IN ({placeholders})",
                (state, json.dumps(result or {}, sort_keys=True), now(), request_id, *states))
            row = db.execute("SELECT * FROM account_operations WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Account operation not found")
            return dict(row)

    def account_operation(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM account_operations WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def account_receipt(row):
        payload = json.loads(row["payload"])
        return {"request_id": row["request_id"], "account_id": payload.get("account_id"),
            "digest": row["digest"], "action": row["action"], "state": row["state"],
            "updated_at": row["updated_at"], "result": json.loads(row["result"])}

    def pending_sends(self, *, due_only=False, as_of=None):
        with self.connect() as db:
            query = "SELECT * FROM sends WHERE state IN ('queued','creating','draft','sending','accepted','uncertain')"
            params = []
            if due_only:
                # Apply the schedule before the batch limit so an old stuck row
                # cannot hide due work behind a full page of delayed rows.
                query += " AND (json_type(result,'$.reconciliation.next_check_at') IS NULL " \
                    "OR json_type(result,'$.reconciliation.next_check_at') != 'text' " \
                    "OR json_extract(result,'$.reconciliation.next_check_at') <= ?)"
                params.append(as_of or now())
            query += " ORDER BY updated_at LIMIT 100"
            return [dict(row) for row in db.execute(query, params)]

    @staticmethod
    def receipt(row):
        return {"request_id": row["request_id"], "business_id": row["business_id"], "mailbox": row["mailbox"],
            "sender_ref": json.loads(row["payload"]).get("sender_ref"),
            "transport": row["transport"],
            "state": row["state"], "draft_id": row["draft_id"], "updated_at": row["updated_at"],
            "result": json.loads(row["result"])}

    # -- Integration envelope: intents, reply-stop guard, handoff, correlation --

    def begin_integration_intent(self, *, client_id, intent_id, request_id, payload_digest, workflow_owner,
            business_id, mailbox, category, stop_contact_key, stop_business_id, correlation=None,
            campaign_id=None, enrollment_id=None, step_index=None, manifest_digest=None):
        """Durably reserve ``(client_id, intent_id)`` before touching the send ledger."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM integration_intents WHERE client_id=? AND intent_id=?",
                (client_id, intent_id)).fetchone()
            if row:
                return dict(row), False
            db.execute("""INSERT INTO integration_intents(client_id,intent_id,request_id,payload_digest,
                workflow_owner,business_id,mailbox,category,campaign_id,enrollment_id,step_index,manifest_digest,
                stop_contact_key,stop_business_id,correlation,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client_id, intent_id, request_id, payload_digest, workflow_owner, business_id, mailbox, category,
                 campaign_id, enrollment_id, step_index, manifest_digest, stop_contact_key, stop_business_id,
                 json.dumps(correlation or {}, sort_keys=True), now()))
            return dict(db.execute("SELECT * FROM integration_intents WHERE client_id=? AND intent_id=?",
                (client_id, intent_id)).fetchone()), True

    def integration_intent(self, client_id, intent_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM integration_intents WHERE client_id=? AND intent_id=?",
                (client_id, intent_id)).fetchone()
            return dict(row) if row else None

    def integration_intent_by_request(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM integration_intents WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def record_stop(self, workflow_owner, contact_key, business_id, *, reason, source_kind, source_id=None):
        """Idempotent per contact/business: the first triggering owner is kept for attribution."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO integration_stops(contact_key,business_id,workflow_owner,"
                "reason,source_kind,source_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (contact_key, business_id, workflow_owner, reason, source_kind, source_id, now()))
            return dict(db.execute("SELECT * FROM integration_stops WHERE contact_key=? AND "
                "business_id=?", (contact_key, business_id)).fetchone())

    def stop_exists(self, contact_key, business_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM integration_stops WHERE contact_key=? AND "
                "business_id=?", (contact_key, business_id)).fetchone() is not None

    def begin_handoff(self, thread_or_message_id, idempotency_key, *, client_id, workflow_owner,
            payload_digest, result):
        """Idempotent on ``(thread_or_message_id, idempotency_key)``."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM integration_handoffs WHERE thread_or_message_id=? AND "
                "idempotency_key=?", (thread_or_message_id, idempotency_key)).fetchone()
            if row:
                return dict(row), False
            db.execute("INSERT INTO integration_handoffs VALUES(?,?,?,?,?,?,?)",
                (thread_or_message_id, idempotency_key, client_id, workflow_owner, payload_digest,
                 json.dumps(result, sort_keys=True), now()))
            return dict(db.execute("SELECT * FROM integration_handoffs WHERE thread_or_message_id=? AND "
                "idempotency_key=?", (thread_or_message_id, idempotency_key)).fetchone()), True

    def correlate_replies(self, mailbox, business_id):
        """Annotate replies to a cold_outreach intent and record its stop, one transaction.
        Keyed once per (mailbox, message_id); a later label-only revision is skipped."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor_row = db.execute("SELECT revision FROM correlation_cursor WHERE mailbox=?", (mailbox,)).fetchone()
            after = cursor_row[0] if cursor_row else 0
            rows = db.execute("SELECT * FROM messages WHERE mailbox=? AND revision>? ORDER BY revision",
                (mailbox, after)).fetchall()
            max_revision = after
            sent_rows = None
            for row in rows:
                max_revision = max(max_revision, row["revision"])
                message_id = row["id"]
                if row["deleted"]:
                    continue
                if db.execute("SELECT 1 FROM message_correlations WHERE mailbox=? AND message_id=?",
                        (mailbox, message_id)).fetchone():
                    continue
                content = json.loads(row["content"])
                is_sent_copy = bool(content.get("requestId"))
                workflow_owner = intent_id = matched_request_id = None
                correlation_payload = None
                if not is_sent_copy:
                    candidates = set()
                    for key in ("inReplyTo", "references"):
                        for value in content.get(key) or []:
                            normalized = internet_message_id(value)
                            if normalized:
                                candidates.add(normalized)
                    if candidates:
                        if sent_rows is None:
                            sent_rows = db.execute("SELECT * FROM sends WHERE business_id=? AND mailbox=? "
                                "AND state='sent'", (business_id, mailbox)).fetchall()
                        for send_row in sent_rows:
                            result = json.loads(send_row["result"])
                            sent_identity = (internet_message_id(result.get("internet_message_id"))
                                or internet_message_id(send_row["draft_internet_message_id"]))
                            if not sent_identity or sent_identity not in candidates:
                                continue
                            intent = db.execute("SELECT * FROM integration_intents WHERE request_id=?",
                                (send_row["request_id"],)).fetchone()
                            if intent is not None and intent["category"] == "cold_outreach":
                                workflow_owner, intent_id = intent["workflow_owner"], intent["intent_id"]
                                contact, stop_business = intent["stop_contact_key"], intent["stop_business_id"]
                            else:
                                label = legacy_cold_label(send_row)
                                if intent is not None or label is None:
                                    continue
                                workflow_owner, intent_id = label[0], None
                                contact, stop_business = label[1], label[2]
                            matched_request_id = send_row["request_id"]
                            correlation_payload = {"matched_internet_message_id": sent_identity,
                                "sent_request_id": send_row["request_id"]}
                            db.execute("INSERT OR IGNORE INTO integration_stops(contact_key,business_id,"
                                "workflow_owner,reason,source_kind,source_id,created_at) VALUES(?,?,?,?,?,?,?)",
                                (contact, stop_business, workflow_owner, "reply_detected", "reply",
                                 message_id, now()))
                            break
                db.execute("INSERT INTO message_correlations VALUES(?,?,?,?,?,?,?,?)",
                    (mailbox, message_id, int(is_sent_copy), workflow_owner, intent_id, matched_request_id,
                     json.dumps(correlation_payload) if correlation_payload else None, now()))
            db.execute("INSERT INTO correlation_cursor VALUES(?,?) ON CONFLICT(mailbox) DO UPDATE SET "
                "revision=excluded.revision", (mailbox, max_revision))
