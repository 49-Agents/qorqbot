"""Private Outlook change capture and exact confirmed-message business routing.

No message bodies or headers are stored in the operational collector tables.
An uncertain import is reconciled by lookup, never replayed on mere absence.
"""

import base64
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default

from .fastmail import ProviderError, internet_message_id, mime_content_digest
from .models import MAX_MESSAGE_BYTES, Outgoing, address
from .store import now
from .verification import verify_approved_message

PROVIDER = "outlook-web"


def timestamp(value):
    if not isinstance(value, str):
        raise ProviderError("outlook_timestamp_missing")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise ProviderError("outlook_timestamp_invalid") from exc
    return instant.astimezone(timezone.utc).isoformat()


def threading_ids(mail):
    result = set()
    for key in ("Message-ID", "In-Reply-To", "References"):
        for value in mail.get_all(key, []):
            for token in re.findall(r"<[^<>\s]+>", str(value)):
                identity = internet_message_id(token)
                if identity:
                    result.add(identity)
    return result


class OutlookCollection:
    def __init__(self, store, *, account_address, start_at, capture, archive, fastmail,
                 mailboxes, account_ref, retrieve=None, clock=now):
        self.store, self.capture, self.archive, self.fastmail = store, capture, archive, fastmail
        self.account_address, self.start_at = address(account_address), timestamp(start_at)
        # A callable keeps managed routes visible without a restart.
        self._mailboxes = mailboxes if callable(mailboxes) else tuple(mailboxes)
        self.account_ref, self.clock = account_ref, clock
        self.retrieve = retrieve
        if archive is None or not account_ref:
            raise ValueError("Outlook collection requires private history and a registered account")
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outlook_collection_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_address TEXT NOT NULL,
                    account_ref TEXT NOT NULL, start_at TEXT NOT NULL, cursor TEXT, state TEXT NOT NULL,
                    last_success_at TEXT, error TEXT, lease_token TEXT, lease_until REAL);
                CREATE TABLE IF NOT EXISTS outlook_collection_items (
                    native_id TEXT PRIMARY KEY, digest TEXT NOT NULL, removed INTEGER NOT NULL,
                    observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outlook_collection_imports (
                    native_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, content_digest TEXT NOT NULL,
                    business_id TEXT NOT NULL, mailbox TEXT NOT NULL, state TEXT NOT NULL,
                    fastmail_id TEXT, fastmail_thread_id TEXT, native_thread_id TEXT, error TEXT, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS outlook_import_message ON outlook_collection_imports(message_id);
                CREATE TABLE IF NOT EXISTS outlook_collection_routing (
                    native_id TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outlook_collection_issues (
                    id TEXT PRIMARY KEY, native_id TEXT, code TEXT NOT NULL, observed_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(outlook_collection_imports)")}
            for column, definition in (("content_variant", "INTEGER NOT NULL DEFAULT 0"),
                                       ("correlated_request_id", "TEXT"), ("original_content_digest", "TEXT")):
                if column not in columns:
                    db.execute(f"ALTER TABLE outlook_collection_imports ADD COLUMN {column} {definition}")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM outlook_collection_binding WHERE singleton=1").fetchone()
            binding = (self.account_address, account_ref, self.start_at)
            if prior and tuple(prior[key] for key in ("account_address", "account_ref", "start_at")) != binding:
                raise ValueError("Outlook account and original cutover cannot be reassigned")
            archive_start = archive.external_binding(PROVIDER, self.account_address)
            if prior is None and archive_start is not None:
                raise ValueError("Outlook collector state is missing; recover its cursor without rebaselining")
            archive.external_binding(PROVIDER, self.account_address, self.start_at)
            db.execute("INSERT OR IGNORE INTO outlook_collection_binding(singleton,account_address,account_ref,start_at,state) VALUES(1,?,?,?,'pending')", binding)

    @property
    def mailboxes(self):
        return tuple(self._mailboxes()) if callable(self._mailboxes) else self._mailboxes

    @mailboxes.setter
    def mailboxes(self, value):
        self._mailboxes = value if callable(value) else tuple(value)

    def status(self):
        with self.store.connect() as db:
            row = db.execute("SELECT state,last_success_at,error FROM outlook_collection_binding WHERE singleton=1").fetchone()
            unresolved = db.execute("""SELECT count(*) FROM (
                SELECT native_id FROM outlook_collection_routing WHERE state IN ('pending','awaiting_confirmation','retry','uncertain')
                UNION SELECT native_id FROM outlook_collection_imports WHERE state IN ('importing','uncertain','retry'))""").fetchone()[0]
            issues = db.execute("SELECT count(*) FROM outlook_collection_issues").fetchone()[0]
        return {**dict(row), "pending_imports": unresolved, "reported_issues": issues}

    def _queue_state(self, native_id, state, error=None):
        with self.store.connect() as db:
            db.execute("UPDATE outlook_collection_routing SET state=?,error=?,updated_at=? WHERE native_id=?",
                (state, error, self.clock(), native_id))

    def _issue(self, code, native_id=None):
        identity = hashlib.sha256(json.dumps([code, native_id]).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO outlook_collection_issues VALUES(?,?,?,?)", (identity, native_id, code, self.clock()))
        self.archive.external_observation(PROVIDER, self.account_address, identity,
            {"code": code, "native_id": native_id}, kind="provider.routing_issue")

    def _eligible(self, item):
        if item.get("after_cutover") is True:
            return True
        dates = [timestamp(item[key]) for key in ("changed_at", "created_at", "received_at", "sent_at") if item.get(key) is not None]
        if dates:
            return max(dates) >= self.start_at
        if item.get("removed") is True:
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM outlook_collection_items WHERE native_id=?", (item["id"],)).fetchone():
                    return True
        raise ProviderError("outlook_cutover_evidence_missing")

    def _owner(self, identities):
        with self.store.connect() as db:
            matches, pending = set(), set()
            for row in db.execute("SELECT business_id,mailbox,state,result,draft_internet_message_id FROM sends"):
                result = json.loads(row["result"])
                owner = (row["business_id"], row["mailbox"])
                actual = internet_message_id(result.get("internet_message_id"))
                if row["state"] == "sent" and actual in identities:
                    matches.add(owner)
                elif row["state"] not in {"sent", "failed", "canceled", "cancelled"} and (
                        actual in identities or internet_message_id(row["draft_internet_message_id"]) in identities):
                    # A draft identity can delay routing, but never authorize exposure.
                    pending.add(owner)
        if pending:
            # Even a same-business pending original may still be in the sender's
            # durable copy step. Wait for confirmation before competing with it.
            return None, "outlook_routing_awaiting_confirmation"
        if not matches:
            return None, "outlook_routing_no_confirmed_owner"
        if len(matches) != 1:
            return None, "outlook_routing_conflicting_owners"
        business_id, mailbox = next(iter(matches))
        boxes = [box for box in self.mailboxes if box.ref == mailbox and box.business_id == business_id]
        if len(boxes) != 1:
            return None, "outlook_routing_mailbox_unavailable"
        with self.store.connect() as db:
            registered = db.execute("SELECT business_id,folder_id FROM mailbox_bindings WHERE ref=?", (mailbox,)).fetchone()
        if not registered or tuple(registered) != (business_id, boxes[0].folder_id):
            return None, "outlook_routing_mailbox_binding_changed"
        return boxes[0], None

    def _mapped(self, native_id, result, *, correlation=None):
        if not isinstance(result, dict) or not result.get("id") or not result.get("conversationId"):
            raise ProviderError("outlook_import_result_invalid", uncertain=True)
        with self.store.connect() as db:
            expected = db.execute("SELECT message_id,content_digest,business_id,mailbox FROM outlook_collection_imports WHERE native_id=?", (native_id,)).fetchone()
            if not expected or internet_message_id(result.get("internetMessageId")) != expected[0]:
                raise ProviderError("outlook_import_result_invalid", uncertain=True)
            db.execute("""UPDATE outlook_collection_imports SET state='imported',fastmail_id=?,fastmail_thread_id=?,error=NULL,updated_at=?,
                content_variant=?,correlated_request_id=?,original_content_digest=?
                WHERE message_id=? AND content_digest=? AND business_id=? AND mailbox=?""",
                (result["id"], result["conversationId"], self.clock(),
                 int(bool(correlation and correlation["content_variant"])),
                 correlation["request_id"] if correlation else None,
                 correlation["original_content_digest"] if correlation else None, *expected))
            db.execute("""UPDATE outlook_collection_routing SET state='done',error=NULL,updated_at=? WHERE native_id IN (
                SELECT native_id FROM outlook_collection_imports WHERE message_id=? AND content_digest=? AND business_id=? AND mailbox=?)""",
                (self.clock(), *expected))
        self._queue_state(native_id, "done")

    def retry_routing(self, native_id, *, reason):
        """Operator retry of an observed source; ownership and content are rechecked."""
        if self.retrieve is None or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("Routing retry requires retrieval and a bounded operator reason")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM outlook_collection_routing WHERE native_id=?", (native_id,)).fetchone()
        if row is None:
            raise ValueError("Routing retry requires an already observed native ID")
        self.archive.external_observation(PROVIDER, self.account_address, str(uuid.uuid4()),
            {"native_id": native_id, "reason": reason, "previous_state": row["state"], "previous_error": row["error"]},
            kind="provider.routing_retry_requested")
        self._queue_state(native_id, "pending")

    def _confirmed_original(self, native_id, message_id, mailbox, content, *, previous_state):
        with self.store.connect() as db:
            candidates = []
            for row in db.execute("SELECT * FROM sends WHERE state='sent' AND transport='fastmail'"):
                receipt = json.loads(row["result"])
                if internet_message_id(receipt.get("internet_message_id")) == message_id:
                    candidates.append((dict(row), receipt))
        if not candidates:
            return False
        if len(candidates) != 1:
            raise ProviderError("outlook_confirmed_original_invalid")
        row, receipt = candidates[0]
        try:
            message = Outgoing.model_validate_json(row["payload"])
            valid = (message.digest() == row["digest"] and message.request_id == row["request_id"]
                and message.transport in {None, "fastmail"}
                and message.business_id == row["business_id"] == mailbox.business_id
                and message.mailbox == row["mailbox"] == mailbox.ref
                and isinstance(receipt.get("message_id"), str) and bool(receipt["message_id"])
                and isinstance(receipt.get("conversation_id"), str) and bool(receipt["conversation_id"]))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ProviderError("outlook_confirmed_original_invalid")
        try:
            original, original_mime = self.fastmail.get_message_mime(mailbox, receipt["message_id"])
            if (original.get("id") != receipt["message_id"]
                    or original.get("conversationId") != receipt["conversation_id"]
                    or internet_message_id(original.get("internetMessageId")) != message_id):
                raise ProviderError("outlook_confirmed_original_invalid")
            verify_approved_message(self.fastmail, mailbox, message, original, is_draft=False)
        except ProviderError as exc:
            if exc.code in {"message_outside_business", "provider_draft_changed", "provider_reply_changed", "provider_attachments_changed"}:
                raise ProviderError("outlook_confirmed_original_invalid") from exc
            raise
        native_digest, original_digest = mime_content_digest(content), mime_content_digest(original_mime)
        correlation = {"native_id": native_id, "message_id": message_id, "request_id": message.request_id,
            "business_id": mailbox.business_id, "mailbox": mailbox.ref, "fastmail_id": original["id"],
            "fastmail_thread_id": original["conversationId"], "approved_payload_digest": row["digest"],
            "native_content_digest": native_digest, "original_content_digest": original_digest,
            "native_mime_sha256": hashlib.sha256(content).hexdigest(),
            "original_mime_sha256": hashlib.sha256(original_mime).hexdigest(),
            "content_variant": native_digest != original_digest, "previous_import_state": previous_state,
            "basis": "confirmed_fastmail_send_verified_against_approval", "native_content_imported": False}
        # The received variant stays private; only the verified existing original is mapped.
        self.archive.external_observation(PROVIDER, self.account_address, native_id, correlation,
            kind="provider.message_correlation")
        self._mapped(native_id, original, correlation=correlation)
        return True

    def _route(self, item, content, mail):
        native_id = item["id"]
        values = mail.get_all("Message-ID", [])
        message_id = internet_message_id(str(values[0])) if len(values) == 1 else None
        if not message_id:
            self._issue("outlook_routing_message_id_missing", native_id)
            self._queue_state(native_id, "private", "outlook_routing_message_id_missing")
            return
        mailbox, error = self._owner(threading_ids(mail))
        if error:
            # Personal or ambiguous mail remains exclusively in the encrypted archive.
            self._issue(error, native_id)
            self._queue_state(native_id, "awaiting_confirmation" if error == "outlook_routing_awaiting_confirmation" else "private", error)
            return
        digest = mime_content_digest(content)
        expected = (message_id, digest, mailbox.business_id, mailbox.ref)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM outlook_collection_imports WHERE native_id=?", (native_id,)).fetchone()
            if prior and tuple(prior[key] for key in ("message_id", "content_digest", "business_id", "mailbox")) != expected:
                conflict = True
            else:
                conflict = False
                thread = item.get("raw", {}).get("ConversationId")
                thread = thread.get("Id") if isinstance(thread, dict) else thread if isinstance(thread, str) else None
                db.execute("INSERT OR IGNORE INTO outlook_collection_imports(native_id,message_id,content_digest,business_id,mailbox,state,native_thread_id,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (native_id, *expected, thread, self.clock()))
        if conflict:
            self._issue("outlook_import_source_changed", native_id)
            self._queue_state(native_id, "conflict", "outlook_import_source_changed")
            return
        if prior and prior["state"] == "imported":
            self._queue_state(native_id, "done")
            return
        attempted = False
        try:
            if self._confirmed_original(native_id, message_id, mailbox, content,
                    previous_state=prior["state"] if prior else "pending"):
                return
            found = self.fastmail.find_observed_mime(mailbox, message_id, content)
            if found:
                self._mapped(native_id, found)
                return
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                related = list(db.execute("SELECT * FROM outlook_collection_imports WHERE message_id=?", (message_id,)))
                if any((row["content_digest"], row["business_id"], row["mailbox"]) != expected[1:] for row in related):
                    raise ProviderError("outlook_import_content_conflict")
                if any(row["state"] in {"importing", "uncertain", "imported"} for row in related):
                    # Native IDs can change after a move; a second ID is not a new import.
                    raise ProviderError("outlook_import_unconfirmed", uncertain=True)
                updated = db.execute("UPDATE outlook_collection_imports SET state='importing',updated_at=? WHERE native_id=? AND state IN ('pending','retry')", (self.clock(), native_id))
                if updated.rowcount != 1:
                    raise ProviderError("outlook_import_unconfirmed", uncertain=True)
            attempted = True
            result = self.fastmail.import_observed_mime(mailbox, content, message_id=message_id,
                received_at=timestamp(item["received_at"]) if item.get("received_at") else None,
                is_read=item.get("is_read") is True)
            self._mapped(native_id, result)
        except ProviderError as exc:
            if exc.code in {"outlook_import_conflicting_owner", "outlook_import_content_conflict", "outlook_import_lookup_ambiguous", "outlook_confirmed_original_invalid"}:
                with self.store.connect() as db:
                    db.execute("UPDATE outlook_collection_imports SET state='conflict',error=?,updated_at=? WHERE native_id=?", (exc.code, self.clock(), native_id))
                self._issue(exc.code, native_id)
                self._queue_state(native_id, "conflict", exc.code)
                return
            with self.store.connect() as db:
                current = db.execute("SELECT state FROM outlook_collection_imports WHERE native_id=?", (native_id,)).fetchone()
                uncertain = exc.uncertain or (not attempted and current[0] in {"importing", "uncertain", "imported"})
                db.execute("UPDATE outlook_collection_imports SET state=?,error=?,updated_at=? WHERE native_id=?",
                    ("uncertain" if uncertain else "retry", exc.code, self.clock(), native_id))
            raise

    def _archive_item(self, item, provenance):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 4096:
            raise ProviderError("outlook_item_invalid")
        if not self._eligible(item):
            return
        native_id = item["id"]
        if not isinstance(item.get("raw", {}), dict):
            raise ProviderError("outlook_item_metadata_invalid")
        removed = item.get("removed") is True
        snapshot = dict(item)
        content = None
        if not removed:
            try:
                content = base64.b64decode(item["mime_base64"], validate=True)
                if not content or len(content) > MAX_MESSAGE_BYTES:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                self.archive.external_observation(PROVIDER, self.account_address, native_id,
                    {"id": native_id, "raw": item.get("raw", {}), "unavailable": True, "reason": "complete_mime_unavailable"}, provenance=provenance)
                raise ProviderError("outlook_complete_mime_unavailable") from None
            snapshot.update(mime_sha256=hashlib.sha256(content).hexdigest(), mime_size=len(content))
        # Archival durability always precedes any possible business-folder exposure.
        self.archive.external_observation(PROVIDER, self.account_address, native_id, snapshot, provenance=provenance)
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("INSERT INTO outlook_collection_items VALUES(?,?,?,?) ON CONFLICT(native_id) DO UPDATE SET digest=excluded.digest,removed=excluded.removed,observed_at=excluded.observed_at",
                (native_id, digest, int(removed), self.clock()))
        if removed:
            self._queue_state(native_id, "private", "outlook_source_removed")
            return
        draft = item.get("raw", {}).get("IsDraft") is True
        with self.store.connect() as db:
            db.execute("INSERT INTO outlook_collection_routing VALUES(?,?,?,?) ON CONFLICT(native_id) DO UPDATE SET state=excluded.state,error=excluded.error,updated_at=excluded.updated_at",
                (native_id, "private" if draft else "pending", "outlook_draft_only" if draft else None, self.clock()))
        # A draft's references never prove that it was sent. Keep the full private
        # snapshot, and let a later native sent update make routing eligible again.
        if draft:
            return
        if item.get("is_sent") is True:
            sent_at = timestamp(item.get("sent_at"))
            if sent_at >= self.start_at:
                mail = BytesParser(policy=default).parsebytes(content)
                values = mail.get_all("Message-ID", [])
                message_id = internet_message_id(str(values[0])) if len(values) == 1 else None
                self.store.observe_external_send(self.account_ref, native_id, message_id, sent_at)
        return item, content

    def _route_safely(self, item, content, token):
        with self.store.connect() as db:
            refreshed = db.execute("UPDATE outlook_collection_binding SET lease_until=? WHERE singleton=1 AND lease_token=?",
                (time.time() + 300, token))
            if refreshed.rowcount != 1:
                raise ProviderError("outlook_collector_lease_changed")
        try:
            self._route(item, content, BytesParser(policy=default).parsebytes(content))
        except Exception as exc:
            code = exc.code if isinstance(exc, ProviderError) else "outlook_routing_failed"
            self._queue_state(item["id"], "retry", code)
            self._issue(code, item["id"])

    def _retry(self, queued, observed, token):
        with self.store.connect() as db:
            active = {row[0] for row in db.execute("SELECT native_id FROM outlook_collection_routing WHERE state IN ('pending','awaiting_confirmation','retry','uncertain')")}
        requested = [identity for identity in queued if identity not in observed and identity in active]
        if not requested or self.retrieve is None:
            return
        # Two full-size messages fit the browser bridge's aggregate MIME bound.
        for offset in range(0, len(requested), 2):
            self._retry_batch(requested[offset:offset + 2], token)

    def _retry_batch(self, requested, token):
        try:
            items = self.retrieve(requested)
            if not isinstance(items, list) or len(items) > len(requested):
                raise ProviderError("outlook_retrieve_response_invalid")
            identities = [item.get("id") if isinstance(item, dict) else None for item in items]
            if len(set(identities)) != len(identities) or any(identity not in requested for identity in identities):
                raise ProviderError("outlook_retrieve_response_invalid")
            archived = []
            for item in items:
                value = self._archive_item(item, {"source": "routing_retry", "start_at": self.start_at})
                if value:
                    archived.append(value)
                elif item.get("removed") is True:
                    self._issue("outlook_routing_source_missing", item["id"])
            for identity in set(requested) - set(identities):
                self._queue_state(identity, "retry", "outlook_routing_source_missing")
                self._issue("outlook_routing_source_missing", identity)
            for item, content in archived:
                self._route_safely(item, content, token)
        except Exception as exc:
            code = exc.code if isinstance(exc, ProviderError) else "outlook_routing_retrieve_failed"
            for identity in requested:
                self._queue_state(identity, "retry", code)
                self._issue(code, identity)

    def run(self):
        token = str(uuid.uuid4())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT * FROM outlook_collection_binding WHERE singleton=1").fetchone()
            if saved["lease_token"] and saved["lease_until"] > time.time():
                return {**self.status(), "state": "running"}
            cursor = saved["cursor"]
            queued = [row[0] for row in db.execute("SELECT native_id FROM outlook_collection_routing WHERE state IN ('pending','awaiting_confirmation','retry','uncertain') ORDER BY updated_at,native_id LIMIT 10")]
            db.execute("UPDATE outlook_collection_binding SET lease_token=?,lease_until=?,state='collecting' WHERE singleton=1", (token, time.time() + 300))
        native_id = None
        try:
            self.archive.external_observation(PROVIDER, self.account_address, "configuration",
                {"start_at": self.start_at, "capture_scope": "account"}, kind="provider.capture_configuration")
            page = self.capture(cursor)
            if not isinstance(page, dict) or address(page.get("account_address")) != self.account_address:
                raise ProviderError("outlook_collection_account_changed")
            if timestamp(page.get("start_at")) != self.start_at:
                raise ProviderError("outlook_collection_cutover_changed")
            if page.get("gap"):
                self.archive.external_observation(PROVIDER, self.account_address, "native_gap",
                    {"cursor": cursor, "gap": page["gap"]}, kind="provider.native_gap")
                raise ProviderError("outlook_changes_gap")
            if (not isinstance(page.get("items"), list) or len(page["items"]) > 500
                    or not isinstance(page.get("cursor"), str) or not page["cursor"] or len(page["cursor"]) > 1000000
                    or type(page.get("complete")) is not bool):
                raise ProviderError("outlook_capture_page_invalid")
            if page["cursor"] == cursor and not page["complete"]:
                raise ProviderError("outlook_cursor_did_not_advance")
            if "folders" in page:
                self.archive.external_observation(PROVIDER, self.account_address, "folders", page["folders"],
                    kind="provider.mailbox_catalog", provenance={"cursor": cursor})
            provenance = {"cursor": cursor, "next_cursor": page["cursor"], "start_at": self.start_at}
            archived, observed = [], set()
            for item in page["items"]:
                native_id = item.get("id") if isinstance(item, dict) else None
                value = self._archive_item(item, provenance)
                observed.add(native_id)
                if value:
                    archived.append(value)
            with self.store.connect() as db:
                updated = db.execute("UPDATE outlook_collection_binding SET cursor=?,state=?,last_success_at=COALESCE(?,last_success_at),error=NULL WHERE singleton=1 AND lease_token=? AND cursor IS ?",
                    (page["cursor"], "synced" if page["complete"] else "partial", self.clock() if page["complete"] else None, token, cursor))
                if updated.rowcount != 1:
                    raise ProviderError("outlook_collector_lease_changed")
            # Private capture progress is independent from business import recovery.
            for item, content in archived:
                self._route_safely(item, content, token)
            self._retry(queued, observed, token)
        except Exception as exc:
            code = exc.code if isinstance(exc, ProviderError) else "outlook_collection_failed"
            with self.store.connect() as db:
                db.execute("UPDATE outlook_collection_binding SET state='gap',error=? WHERE singleton=1 AND lease_token=?", (code, token))
            self.archive.external_gap(PROVIDER, self.account_address, cursor, code,
                status=getattr(exc, "status", None), message_id=native_id)
        finally:
            with self.store.connect() as db:
                db.execute("UPDATE outlook_collection_binding SET lease_token=NULL,lease_until=NULL WHERE singleton=1 AND lease_token=?", (token,))
        return self.status()

    collect = run
