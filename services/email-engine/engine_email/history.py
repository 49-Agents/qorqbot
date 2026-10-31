"""Append-only encrypted history. No private key or archive read API exists here."""

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import now


class PrivateHistory:
    def __init__(self, config, account_id, operational_database):
        self.config = config
        self.account_id = account_id
        self.path = Path(config.database).expanduser()
        if not self.path.is_absolute() or self.path.resolve() == operational_database.resolve():
            raise ValueError("Use a separate absolute private history database")
        if any(path.is_symlink() for path in [self.path, *self.path.parents]):
            raise ValueError("History path cannot contain symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.public_key = serialization.load_pem_public_key(Path(config.public_key_file).read_bytes())
        if not isinstance(self.public_key, rsa.RSAPublicKey) or self.public_key.key_size < 2048:
            raise ValueError("History requires an RSA public key of at least 2048 bits")
        public_bytes = self.public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.key_id = hashlib.sha256(public_bytes).hexdigest()
        with self.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "private_history_binding" not in tables:
                raise ValueError("Refusing to adopt unrelated history database")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS private_history_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL,
                    start_cursor TEXT NOT NULL, key_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collection (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_messages (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_metadata (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL);
            """)
            values = (account_id, config.start_cursor, self.key_id)
            prior = db.execute("SELECT account_id,start_cursor,key_id FROM private_history_binding WHERE singleton=1").fetchone()
            if prior and tuple(prior) != values:
                raise ValueError("Private history account, starting cursor, and encryption key cannot be reassigned")
            db.execute("INSERT OR IGNORE INTO private_history_binding VALUES(1,?,?,?)", values)
            db.execute("INSERT OR IGNORE INTO collection VALUES(1,?)", (config.start_cursor,))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def seal(self, event_id, kind, content):
        payload = json.dumps({"event_id": event_id, "kind": kind, "content": content},
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        digest = hashlib.sha256(payload).hexdigest()
        key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        aad = (self.key_id + ":" + event_id + ":" + digest).encode()
        encrypted = AESGCM(key).encrypt(nonce, payload, aad)
        wrapped = self.public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        encode = lambda value: base64.b64encode(value).decode()
        envelope = json.dumps({"version": 1, "key_id": self.key_id, "algorithm": "RSA-OAEP-SHA256+A256GCM",
            "wrapped_key": encode(wrapped), "nonce": encode(nonce), "ciphertext": encode(encrypted)})
        return event_id, digest, envelope

    def append_sealed(self, record):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._append(db, record)

    @staticmethod
    def _append(db, record):
        prior = db.execute("SELECT digest FROM events WHERE event_id=?", (record[0],)).fetchone()
        if prior and prior[0] != record[1]:
            raise ValueError("History event ID belongs to different content")
        db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)", record)

    def append(self, event_id, kind, content):
        self.append_sealed(self.seal(event_id, kind, content))
        return {"event_id": event_id, "stored": True}

    def cursor(self):
        with self.connect() as db:
            return db.execute("SELECT cursor FROM collection WHERE singleton=1").fetchone()[0]

    def observed(self, identity):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM observed_messages WHERE id=?", (identity,)).fetchone() is not None

    def provenance(self):
        return {"schema_version": 1, "provider": "fastmail-jmap", "account_id": self.account_id,
            "observed_at": now()}

    def observation(self, identity, snapshot, *, provenance=None):
        self._observation("observed_messages", identity, "provider.message", {"message": snapshot}, provenance)

    def metadata_observation(self, kind, snapshot, *, provenance=None):
        self._observation("observed_metadata", kind, kind, snapshot, provenance)

    def _observation(self, table, identity, kind, content, provenance):
        # These fixed internal tables share the same id/digest shape. Timestamps
        # and collection cursors do not turn an unchanged replay into a new version.
        assert table in {"observed_messages", "observed_metadata"}
        data = content["message"] if table == "observed_messages" else content
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(f"SELECT digest FROM {table} WHERE id=?", (identity,)).fetchone()
            if prior and prior[0] == digest:
                return
            # The observation marker and event commit together. A distinct event
            # also preserves A->B->A changes observed while replaying one cursor.
            event_id = "provider:" + str(uuid.uuid4())
            self._append(db, self.seal(event_id, kind, {**self.provenance(), **content,
                "provenance": provenance or {}}))
            db.execute(f"INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest", (identity, digest))

    def collection_gap(self, error, status=None, *, message_id=None):
        cursor = self.cursor()
        content = {"cursor": cursor, "error": error, "provider_status": status}
        identity = cursor + ":" + error + ":" + str(status)
        if message_id is not None:
            content["message_id"] = message_id
            identity += ":" + message_id
        event_id = "collection-gap:" + hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                return
            self._append(db, self.seal(event_id, "provider.collection_gap", {
                **self.provenance(), **content, "capture_scope": self.config.capture_scope}))

    def advance(self, cursor):
        with self.connect() as db:
            db.execute("UPDATE collection SET cursor=? WHERE singleton=1", (cursor,))

    def external_binding(self, provider, account_address, start_at=None):
        """Independent source binding; the existing Fastmail cursor is untouched."""
        if provider != "outlook-web":
            raise ValueError("Unsupported external history provider")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS external_history_bindings (
                    provider TEXT NOT NULL, account_address TEXT NOT NULL, start_at TEXT NOT NULL,
                    PRIMARY KEY(provider,account_address));
                CREATE TABLE IF NOT EXISTS external_history_observations (
                    identity TEXT PRIMARY KEY, digest TEXT NOT NULL);
            """)
            prior = db.execute("SELECT start_at FROM external_history_bindings WHERE provider=? AND account_address=?",
                (provider, account_address)).fetchone()
            if start_at is not None:
                if prior and prior[0] != start_at:
                    raise ValueError("External history account cutover cannot be reassigned")
                db.execute("INSERT OR IGNORE INTO external_history_bindings VALUES(?,?,?)", (provider, account_address, start_at))
            return prior[0] if prior else None

    def external_observation(self, provider, account_address, identity, snapshot, *, kind="provider.message", provenance=None):
        if self.external_binding(provider, account_address) is None:
            raise ValueError("External history requires an explicit bound cutover")
        namespace = json.dumps([provider, account_address, kind, identity], separators=(",", ":"))
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest FROM external_history_observations WHERE identity=?", (namespace,)).fetchone()
            if prior and prior[0] == digest:
                return
            content = {"schema_version": 1, "provider": provider, "account_address": account_address,
                "observed_at": now(), "capture_scope": "account", "provenance": provenance or {},
                **({"message": snapshot} if kind == "provider.message" else {"snapshot": snapshot})}
            self._append(db, self.seal("external-provider:" + str(uuid.uuid4()), kind, content))
            db.execute("INSERT INTO external_history_observations VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET digest=excluded.digest",
                (namespace, digest))

    def external_gap(self, provider, account_address, cursor, error, *, status=None, message_id=None):
        if self.external_binding(provider, account_address) is None:
            raise ValueError("External history requires an explicit bound cutover")
        identity = json.dumps([provider, account_address, cursor, error, status, message_id], separators=(",", ":"))
        event_id = "external-gap:" + hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                return
            self._append(db, self.seal(event_id, "provider.collection_gap", {
                "schema_version": 1, "provider": provider, "account_address": account_address,
                "observed_at": now(), "capture_scope": "account", "cursor": cursor,
                "error": error, "provider_status": status, "message_id": message_id}))
