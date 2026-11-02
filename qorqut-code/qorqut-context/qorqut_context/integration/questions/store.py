"""The host-owned store of open Context review questions: one SQLite file outside the published Context.

Questions name Context records only by ID and are never Context data. The file is ``InstancePaths.context_questions``;
nothing here imports the Context engine or any host module. Constructing a store resolves a path and touches nothing: a
read of an absent or never-initialized file is an empty list or ``None``, and the file is created and its schema written
only by the first write (a delivery or a mutation) or by an explicit ``initialize``.

Resolving a question means deleting its row. The delivery ledger (``deliveries``) records only event identity, published
commit and count, so a redelivered batch can never bring a deleted question back. The mutation receipt table
(``mutations``) records only a request key, a digest of the requested change, the action, the question ID and whether the
effect happened, never question text, so an uncertain retry of a create, update or delete is a replay and not a second
effect. The store is independent of any Context capture and is not covered by one.

Provenance: the approved standalone question store of the Context notes trial, ported with lazy initialization,
absent-store reads and the mutation receipt added."""

from __future__ import annotations

from contextlib import closing, contextmanager, suppress
from functools import cache
import hashlib
import json
import os
import sqlite3
import uuid

KINDS = ("event", "node", "edge", "note")
ACTIONS = ("create", "update", "delete")
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
SCHEMA_VERSION = 1
BUSY_TIMEOUT = 30

SCHEMA = (
    """CREATE TABLE questions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL CHECK (length(text) > 0),
        origin_event_id TEXT
    )""",
    """CREATE TABLE question_related (
        question_seq INTEGER NOT NULL REFERENCES questions(seq) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('event', 'node', 'edge', 'note')),
        ref_id TEXT NOT NULL CHECK (length(ref_id) > 0),
        PRIMARY KEY (question_seq, position),
        UNIQUE (question_seq, kind, ref_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX question_related_ref ON question_related (kind, ref_id)",
    """CREATE TABLE deliveries (
        event_id TEXT PRIMARY KEY,
        published_commit TEXT NOT NULL,
        question_count INTEGER NOT NULL CHECK (question_count >= 0)
    ) WITHOUT ROWID""",
    """CREATE TABLE mutations (
        request_key TEXT PRIMARY KEY,
        digest TEXT NOT NULL CHECK (length(digest) = 64),
        action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
        question_id TEXT NOT NULL CHECK (length(question_id) > 0),
        applied INTEGER NOT NULL CHECK (applied IN (0, 1))
    ) WITHOUT ROWID""",
)


class QuestionError(Exception):
    """A validation failure of a question change or a delivered batch."""


class QuestionNotFound(QuestionError):
    """The named question does not exist."""


class QuestionConflict(QuestionError):
    """A request key or delivery identity was already used for different input."""


class QuestionStoreError(Exception):
    """The store file cannot be used: it is not a question store, has another version, or cannot be opened."""


@cache
def _expected_structure():
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        return _structure(connection)


def _structure(connection):
    return {(kind, name): (table, " ".join(sql.split()) if sql else None)
            for kind, name, table, sql in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table', 'index')")}


def _encodable(value, name):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise QuestionError(f"{name} must be valid Unicode text") from None


def _text(value, name="text"):
    if not isinstance(value, str) or not value.strip():
        raise QuestionError(f"{name} must be a non-empty string")
    _encodable(value, name)
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not value or value != value.strip():
        raise QuestionError(f"{name} must be a non-empty string without surrounding whitespace")
    _encodable(value, name)
    return value


def _kind(value, name="kind"):
    if not isinstance(value, str) or value not in KINDS:
        raise QuestionError(f"{name} must be one of: {', '.join(KINDS)}")
    return value


def _related(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise QuestionError("related must be a list of {kind, id} objects")
    result, seen = [], set()
    for index, reference in enumerate(value):
        if not isinstance(reference, dict) or set(reference) != {"kind", "id"}:
            raise QuestionError(f"related[{index}] must be an object with exactly kind and id")
        kind = _kind(reference["kind"], f"related[{index}].kind")
        identifier = _identifier(reference["id"], f"related[{index}].id")
        if (kind, identifier) not in seen:
            seen.add((kind, identifier))
            result.append({"kind": kind, "id": identifier})
    return result


def _question(entry, index):
    if not isinstance(entry, dict) or "text" not in entry or not set(entry) <= {"text", "related"}:
        raise QuestionError(f"questions[{index}] must be an object with text and optional related")
    return _text(entry["text"], f"questions[{index}].text"), _related(entry.get("related"))


def _integer(value, name, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (
            maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise QuestionError(f"{name} must be an integer {bound}")
    return value


def _database_path(path):
    try:
        path = os.fspath(path)
    except TypeError:
        raise QuestionError("a database path is required") from None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not path or path == ":memory:":
        raise QuestionError("a database file path is required")
    return os.path.abspath(path)


def normalize_mutation(action, question_id=None, text=None, related=None):
    """The validated, normalized change a create, update or delete request asks for; nothing is read or written."""
    if action not in ACTIONS:
        raise QuestionError(f"action must be one of: {', '.join(ACTIONS)}")
    if action == "create":
        if question_id is not None:
            raise QuestionError("create does not take a question_id")
        return {"action": action, "question_id": None, "text": _text(text), "related": _related(related)}
    identifier = _identifier(question_id, "question_id")
    if action == "delete":
        if text is not None or related is not None:
            raise QuestionError("delete takes only a question_id")
        return {"action": action, "question_id": identifier, "text": None, "related": None}
    if text is None and related is None:
        raise QuestionError("update needs text, related or both")
    return {"action": action, "question_id": identifier, "text": None if text is None else _text(text),
            "related": None if related is None else _related(related)}


def mutation_digest(change):
    encoded = json.dumps(change, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class QuestionStore:
    def __init__(self, path):
        self.path = _database_path(path)

    def exists(self):
        return os.path.exists(self.path)

    def initialize(self):
        """Create the file and its schema when missing; verify it otherwise. Idempotent."""
        with self._session(True):
            return True

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    def _materialize(self):
        try:
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        except OSError as error:
            raise QuestionStoreError(f"cannot open question database {self.path}: {error}") from error

    def _verify(self, connection, create):
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise QuestionStoreError(f"{self.path} is a database but not a question store")
                if not create:
                    return False
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return True
            if version != SCHEMA_VERSION:
                raise QuestionStoreError(f"{self.path} has unsupported question store version {version}")
            if _structure(connection) != _expected_structure():
                raise QuestionStoreError(f"{self.path} is a database but not a question store: "
                                         f"its tables and indexes do not match the question store schema")
            return True
        except sqlite3.DatabaseError as error:
            if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
                raise
            raise QuestionStoreError(f"cannot open question database {self.path}: {error}") from error

    @contextmanager
    def _session(self, write):
        """A transaction yielding the connection, or ``None`` for a read of an absent or never-initialized store."""
        if write:
            self._materialize()
        elif not os.path.exists(self.path):
            yield None
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection if self._verify(connection, write) else None
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    @staticmethod
    def _records(connection, rows):
        references = {row[0]: [] for row in rows}
        if references:
            marks = ",".join("?" * len(references))
            for seq, kind, ref_id in connection.execute(
                    f"SELECT question_seq, kind, ref_id FROM question_related "
                    f"WHERE question_seq IN ({marks}) ORDER BY question_seq, position", list(references)):
                references[seq].append({"kind": kind, "id": ref_id})
        return [{"id": identifier, "text": text, "related": references[seq], "origin_event_id": origin}
                for seq, identifier, text, origin in rows]

    @staticmethod
    def _set_related(connection, seq, related):
        connection.execute("DELETE FROM question_related WHERE question_seq = ?", (seq,))
        connection.executemany(
            "INSERT INTO question_related (question_seq, position, kind, ref_id) VALUES (?, ?, ?, ?)",
            [(seq, position, reference["kind"], reference["id"]) for position, reference in enumerate(related)])

    @staticmethod
    def _insert(connection, text, related, origin_event_id):
        identifier = str(uuid.uuid4())
        seq = connection.execute("INSERT INTO questions (id, text, origin_event_id) VALUES (?, ?, ?)",
                                 (identifier, text, origin_event_id)).lastrowid
        QuestionStore._set_related(connection, seq, related)
        return seq

    @staticmethod
    def _load(connection, question_id):
        rows = connection.execute(
            "SELECT seq, id, text, origin_event_id FROM questions WHERE id = ?", (question_id,)).fetchall()
        return QuestionStore._records(connection, rows)[0] if rows else None

    @staticmethod
    def _revise(connection, question_id, text, related):
        row = connection.execute("SELECT seq FROM questions WHERE id = ?", (question_id,)).fetchone()
        if row is None:
            raise QuestionNotFound(f"question not found: {question_id}")
        if text is not None:
            connection.execute("UPDATE questions SET text = ? WHERE seq = ?", (text, row[0]))
        if related is not None:
            QuestionStore._set_related(connection, row[0], related)
        return QuestionStore._load(connection, question_id)

    def create(self, text, related=None, origin_event_id=None):
        text = _text(text)
        related = _related(related)
        if origin_event_id is not None:
            _identifier(origin_event_id, "origin_event_id")
        with self._session(True) as connection:
            seq = self._insert(connection, text, related, origin_event_id)
            return self._records(connection, connection.execute(
                "SELECT seq, id, text, origin_event_id FROM questions WHERE seq = ?", (seq,)).fetchall())[0]

    def read(self, question_id):
        _identifier(question_id, "question id")
        with self._session(False) as connection:
            return None if connection is None else self._load(connection, question_id)

    def update(self, question_id, *, text=None, related=None):
        change = normalize_mutation("update", question_id, text, related)
        with self._session(True) as connection:
            return self._revise(connection, question_id, change["text"], change["related"])

    def delete(self, question_id):
        _identifier(question_id, "question id")
        with self._session(True) as connection:
            return connection.execute("DELETE FROM questions WHERE id = ?", (question_id,)).rowcount > 0

    def list(self, *, related_kind=None, related_id=None, offset=0, limit=DEFAULT_LIMIT):
        if (related_kind is None) != (related_id is None):
            raise QuestionError("related_kind and related_id must be given together")
        offset = _integer(offset, "offset", 0)
        limit = _integer(limit, "limit", 1, MAX_LIMIT)
        where, parameters = "", []
        if related_kind is not None:
            where = ("WHERE EXISTS (SELECT 1 FROM question_related r "
                     "WHERE r.question_seq = q.seq AND r.kind = ? AND r.ref_id = ?)")
            parameters = [_kind(related_kind, "related_kind"), _identifier(related_id, "related_id")]
        with self._session(False) as connection:
            if connection is None:
                return {"items": [], "next_offset": None}
            rows = connection.execute(
                f"SELECT q.seq, q.id, q.text, q.origin_event_id FROM questions q {where} "
                f"ORDER BY q.seq LIMIT ? OFFSET ?", parameters + [limit + 1, offset]).fetchall()
            items = self._records(connection, rows[:limit])
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    def mutate(self, request_key, action, *, question_id=None, text=None, related=None):
        """Apply one create, update or delete exactly once per request key.

        The effect and its receipt commit in one transaction. A repeat of the same key and change returns the recorded
        result without touching the question, whether it still exists or not; the same key for a different change is a
        conflict. The result carries identifiers only: the action, the question ID, its current links and whether the
        effect happened.
        """
        key = _identifier(request_key, "request_key")
        change = normalize_mutation(action, question_id, text, related)
        digest = mutation_digest(change)
        with self._session(True) as connection:
            receipt = connection.execute(
                "SELECT digest, question_id, applied FROM mutations WHERE request_key = ?", (key,)).fetchone()
            if receipt is not None:
                if receipt[0] != digest:
                    raise QuestionConflict("this request key was already used for a different question change")
                return self._result(connection, action, receipt[1], bool(receipt[2]), True)
            if action == "create":
                seq = self._insert(connection, change["text"], change["related"], None)
                identifier = connection.execute("SELECT id FROM questions WHERE seq = ?", (seq,)).fetchone()[0]
                applied = True
            elif action == "update":
                identifier = change["question_id"]
                self._revise(connection, identifier, change["text"], change["related"])
                applied = True
            else:
                identifier = change["question_id"]
                applied = connection.execute("DELETE FROM questions WHERE id = ?", (identifier,)).rowcount > 0
            connection.execute(
                "INSERT INTO mutations (request_key, digest, action, question_id, applied) VALUES (?, ?, ?, ?, ?)",
                (key, digest, action, identifier, int(applied)))
            return self._result(connection, action, identifier, applied, False)

    def _result(self, connection, action, question_id, applied, replayed):
        current = self._load(connection, question_id) if action != "delete" else None
        return {"action": action, "question_id": question_id, "applied": applied, "replayed": replayed,
                "related": [] if current is None else current["related"]}

    def deliver(self, event_id, published_commit, questions):
        """Insert an event's question batch and its receipt in one transaction.

        The input is validated before the ledger is consulted. A retry of an already delivered event and commit inserts
        nothing, whatever the batch.
        """
        _identifier(event_id, "event_id")
        _identifier(published_commit, "published_commit")
        if not isinstance(questions, (list, tuple)):
            raise QuestionError("questions must be a list")
        batch = [_question(entry, index) for index, entry in enumerate(questions)]
        with self._session(True) as connection:
            if self._receipt(connection, event_id, published_commit):
                return {"inserted": 0, "already_delivered": True}
            for text, related in batch:
                self._insert(connection, text, related, event_id)
            connection.execute(
                "INSERT INTO deliveries (event_id, published_commit, question_count) VALUES (?, ?, ?)",
                (event_id, published_commit, len(batch)))
        return {"inserted": len(batch), "already_delivered": False}

    def delivered(self, event_id, published_commit):
        _identifier(event_id, "event_id")
        _identifier(published_commit, "published_commit")
        with self._session(False) as connection:
            return False if connection is None else self._receipt(connection, event_id, published_commit)

    @staticmethod
    def _receipt(connection, event_id, published_commit):
        row = connection.execute(
            "SELECT published_commit FROM deliveries WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return False
        if row[0] != published_commit:
            raise QuestionConflict(f"event {event_id} was already delivered for commit {row[0]}")
        return True
