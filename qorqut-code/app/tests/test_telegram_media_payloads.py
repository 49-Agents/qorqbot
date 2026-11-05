"""The private snapshot lifecycle of outbound Telegram files (``MediaPayloads``) over a real composed store.

The bridge is the real ``TelegramBridge`` (inert, never started) over a copy of a composed target store with an injected clock
and a temporary private state root. The source, the image validator and the delivery states are fakes at the boundary; the
database transactions, files, permissions, threads and leases are real. No network, no live path, no Telegram API."""

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import threading
from types import SimpleNamespace

import pytest

from bazarlyk.composition.assembly import host_assembly
from bazarlyk.service.changes import ChangeFeed
from bazarlyk.telegram import media_payloads, store
from bazarlyk.telegram.bridge import TelegramBridge
from bazarlyk.telegram.media_payloads import MediaPayloads, OpenPayload, validate_filename
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.database import Database

START = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
ASSEMBLY = host_assembly(ChangeFeed())
CHAT = SimpleNamespace(name="ordubashy", chat_id=-1001, message_thread_id=11)
PDF = b"%PDF-1.7 unique-marker-4f0a9c " + bytes(range(256)) * 4


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class Mono:
    """The injected monotonic clock the staging deadline reads."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    directory = tmp_path_factory.mktemp("media-payloads")
    database = Database(directory / "ops" / "db.sqlite3", assembly=ASSEMBLY).initialize_target()
    owner, agent, other = new_id(), new_id(), new_id()
    with database.transaction() as uow:
        for identity, kind in ((owner, "human"), (agent, "agent"), (other, "agent")):
            uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": kind})
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": owner})
    database.close()
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    return SimpleNamespace(path=database.path, agent=agent, other=other)


class Env:
    def __init__(self, template, tmp_path):
        copy = tmp_path / "ops" / "db.sqlite3"
        copy.parent.mkdir()
        shutil.copyfile(template.path, copy)
        self.database = Database(copy, assembly=ASSEMBLY).open_existing()
        self.clock = Clock()
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        runtime = SimpleNamespace(database=self.database, root=tmp_path / "root", workspace=tmp_path / "workspace",
                                  application=SimpleNamespace(ensure_writes_open=lambda uow: None, writer_admission=None))
        self.bridge = TelegramBridge(runtime, state_dir=self.state, clock=self.clock)
        self.mono = Mono()
        self.payloads = MediaPayloads(self.bridge, monotonic=self.mono)
        self.agent, self.other = template.agent, template.other
        self.tmp = tmp_path

    @property
    def media(self):
        return self.state / "outbound-media"

    def files(self):
        return sorted(item.name for item in self.media.iterdir()) if self.media.exists() else []

    def rows(self, where="1"):
        with self.bridge.read() as uow:
            return [dict(r) for r in uow.connection.execute(f"SELECT * FROM telegram_file_payloads WHERE {where} ORDER BY created_at, rowid")]

    def row(self, payload_id):
        return self.rows(f"id='{payload_id}'")[0]

    def path(self, payload_id):
        return self.media / (self.row(payload_id)["storage_name"] + ".media")

    def sql(self, statement, *parameters):
        with self.database.transaction() as uow:
            uow.connection.execute(statement, parameters)

    def restart(self):
        """A new service process over the same database and state root."""
        self.payloads = MediaPayloads(self.bridge, monotonic=self.mono)
        return self.payloads

    def close(self):
        self.database.close()


@pytest.fixture
def env(template, tmp_path):
    e = Env(template, tmp_path)
    yield e
    e.close()


class Source:
    """A scripted authorized source: counts opens and bytes served, and can gate, fail or change."""

    def __init__(self, data=PDF, *, entered=None, release=None, error=None, read_error=None, after_first=None):
        self.data, self.entered, self.release, self.error, self.read_error = data, entered, release, error, read_error
        self.after_first = after_first
        self.opens = self.served = 0

    def __call__(self):
        self.opens += 1
        if self.error:
            raise self.error
        return _Stream(self)


class _Stream:
    def __init__(self, source):
        self.source, self.position = source, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fileno(self):
        raise io.UnsupportedOperation

    def read(self, size):
        source = self.source
        if self.position == 0:
            if source.entered is not None:
                source.entered.set()
            if source.release is not None:
                assert source.release.wait(15), "the test never released the copy"
        if source.read_error:
            raise source.read_error
        chunk = source.data[self.position:self.position + size]
        self.position += len(chunk)
        source.served += len(chunk)
        if source.after_first and chunk:
            source.after_first()
            source.after_first = None
        return chunk


class Validator:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, stream, *, send_as, size, filename):
        position = stream.tell()
        content = stream.read()
        stream.seek(0)
        self.calls.append({"send_as": send_as, "size": size, "filename": filename, "content": content, "position": position,
                           "seekable": stream.seekable(), "writable": stream.writable()})
        if self.error:
            raise self.error
        return {"format": "pdf", "pages": 1} if self.result is None else self.result


ALIASES = {"key": "request_key", "name": "filename", "binding": "source_binding"}


def request(env, data=PDF, **overrides):
    overrides = {ALIASES.get(name, name): value for name, value in overrides.items()}
    values = dict(principal_id=env.agent, request_key=new_id(), arguments_hash="1" * 64,
                  source_binding={"kind": "workspace", "path": "exports/a.pdf", "destination": {"chat": "ordubashy"}},
                  expected_sha256=digest(data), filename="a.pdf", send_as="document", opener=Source(data),
                  validate_snapshot=Validator())
    values.update(overrides)
    return values


def stage(env, data=PDF, **overrides):
    return env.payloads.stage(**request(env, data, **overrides))


def queue(env, payload, *, agent=None, kind=None, payload_json=None, direction="outbound"):
    """The message and queued ``agent_file`` outbox row the operation's apply creates for a payload."""
    now = env.bridge.now()
    with env.bridge.write("test.queue") as uow:
        message = store.insert_message(uow, direction=direction, chat=CHAT, purpose="update", body="caption", now=now,
                                       agent_id=agent or env.agent)
        outbox, _ = store.enqueue(uow, CHAT, 1, "f" * 64, kind=kind or payload["send_as"], source_kind="agent_file",
                                  source_key=new_id(), payload=payload_json or {"version": 1, "payload_id": payload["payload_id"]},
                                  now=now, message_id=message["id"], agent_id=agent or env.agent)
    return message["id"], outbox["id"]


def attach(env, payload, message_id, outbox_id):
    with env.bridge.write("test.attach") as uow:
        return env.payloads.attach(uow, payload["payload_id"], message_id=message_id, outbox_id=outbox_id)


def attached(env, **overrides):
    payload = stage(env, **overrides)
    message_id, outbox_id = queue(env, payload)
    return attach(env, payload, message_id, outbox_id), message_id, outbox_id


def deliver(env, outbox_id, state, at=None):
    """Move a delivery through the legal outbox transitions from where it is to ``state``, stamped at ``at`` (default: now)."""
    at = at or env.bridge.now()
    with env.bridge.read() as uow:
        current = uow.connection.execute("SELECT state FROM telegram_outbox WHERE id=?", (outbox_id,)).fetchone()[0]
    path = {("queued", "dispatching"): ["dispatching"], ("queued", "uncertain"): ["dispatching", "uncertain"],
            ("dispatching", "uncertain"): ["uncertain"], ("queued", "sent"): ["dispatching", "sent"],
            ("dispatching", "sent"): ["sent"], ("uncertain", "sent"): ["sent"], ("queued", "failed"): ["failed"],
            ("dispatching", "failed"): ["failed"], ("uncertain", "failed"): ["failed"], ("queued", "cancelled"): ["cancelled"]}
    for step in path[(current, state)]:
        env.sql("UPDATE telegram_outbox SET state=?, revision=revision+1, updated_at=?, dispatch_started_at=COALESCE(dispatch_started_at, ?), "
                "provider_message_id=CASE WHEN ?='sent' THEN 900 ELSE provider_message_id END, "
                "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?", step, at, at, step, step, at, outbox_id)


def code(excinfo):
    return excinfo.value.code


# ----- inert construction, the private directory, metadata only ---------------------------------------------


def test_construction_and_every_read_only_call_create_nothing(env):
    assert env.state.exists() and list(env.state.iterdir()) == []
    payloads = MediaPayloads(env.bridge)
    assert payloads.status()["payloads"] == {"staging": 0, "ready": 0, "attached": 0, "deleting": 0, "deleted": 0}
    assert payloads.recover() == {"stale_staging": 0, "deleted": 0, "orphans_removed": 0, "flagged": 0}
    assert payloads.sweep()["deleted"] == 0
    with pytest.raises(DomainError) as missing:
        with payloads.open_payload(new_id().replace("-", "")):
            pass
    assert code(missing) == "payload_unavailable"
    assert list(env.state.iterdir()) == []


def test_stage_creates_a_0700_directory_and_a_sealed_0600_file_and_no_partial(env):
    payload = stage(env)
    assert stat.S_IMODE(env.media.stat().st_mode) == 0o700 and env.media.stat().st_uid == os.geteuid()
    path = env.path(payload["payload_id"])
    assert env.files() == [path.name] and path.name.endswith(".media")
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size == len(PDF)
    assert path.read_bytes() == PDF
    # Opaque: neither the payload id, the filename nor the request key appears in the file name.
    assert payload["payload_id"] not in path.name and "a.pdf" not in path.name and len(path.stem) == 32


def test_the_ready_payload_reports_its_manifest_and_a_reservation_settled_to_the_real_size(env):
    payload = stage(env, key="key-1", name="Отчёт — 2026.pdf", binding={"kind": "document", "document_id": "d1"})
    row = env.row(payload["payload_id"])
    assert payload["state"] == "ready" and payload["filename"] == "Отчёт — 2026.pdf" and payload["send_as"] == "document"
    assert (payload["size_bytes"], payload["sha256"], payload["expected_sha256"]) == (len(PDF), digest(PDF), digest(PDF))
    assert payload["validated"] == {"format": "pdf", "pages": 1}
    assert payload["source_binding"] == {"kind": "document", "document_id": "d1"} and payload["arguments_hash"] == "1" * 64
    assert payload["request_key"] == "key-1" and payload["principal_id"] == env.agent
    assert "storage_name" not in payload and row["storage_name"] not in json.dumps(payload)
    assert (row["state"], row["reserved_bytes"], row["max_bytes"], row["lease_owner"]) == ("ready", len(PDF), 50_000_000, None)
    assert row["expires_at"] == "2026-10-20T12:00:00.000000Z" and row["sealed_at"] == "2026-10-19T12:00:00.000000Z"


def test_the_database_holds_metadata_only_never_bytes_or_base64(env):
    marker = b"PRIVATE-PAYLOAD-MARKER-" + os.urandom(24).hex().encode()
    stage(env, marker * 8)
    found = b""
    for suffix in ("", "-wal"):
        path = str(env.database.path) + suffix
        found += open(path, "rb").read() if os.path.exists(path) else b""
    assert marker not in found and base64.b64encode(marker) not in found and base64.b64encode(marker * 8)[:60] not in found


# ----- request validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"principal_id": ""}, {"request_key": ""}, {"request_key": "   "}, {"request_key": "k" * 201}, {"request_key": "a\0b"},
    {"arguments_hash": "A" * 64}, {"arguments_hash": "a" * 63}, {"expected_sha256": "z" * 64}, {"expected_sha256": None},
    {"send_as": "video"}, {"send_as": None}, {"filename": "a/b.pdf"}, {"filename": ""}, {"source_binding": []},
    {"source_binding": {"x": object()}}, {"source_binding": {"x": "y" * 9000}}, {"source_binding": {1: "a"}},
    {"opener": None}, {"validate_snapshot": "not callable"}, {"filename": 'say "hi".pdf'}, {"filename": '"'},
])
def test_an_invalid_request_is_refused_before_any_reservation_or_file(env, overrides):
    with pytest.raises(DomainError) as refused:
        stage(env, **overrides)
    assert code(refused) == "validation_error"
    assert env.rows() == [] and not env.media.exists()


ACCEPTED_NAMES = [
    "a.pdf", "Отчёт 2026.pdf", "日本語のファイル.png", "emoji 👍🏽.pdf", "no extension", "tab", ".hidden", "...", "a.b.c", "a b",
    "a" * 255, "é" * 127 + "a",                                   # exactly 255 UTF-8 bytes
    "a\u202eb.pdf", "a\u200fb", "\u2066a\u2069", "a\u200db",    # format characters (Cf), including direction controls, are kept
    "a\u00a0b", "a\u3000b",                                       # no-break and ideographic spaces (Zs)
    "e\u0301.pdf", "\u00e9.pdf",                                  # NFD and NFC spellings are different names, neither is rewritten
    "a'b", "a;b", "a:b", "a*b", "a?b", "a<b>", "a|b", "100%", "a\ufeffb"]
REFUSED_NAMES = ["", ".", "..", "a/b", "/a", "a/", "a\\b", 'a"b', '"', '"a.pdf', 'a.pdf"',            # empty, dot names, / \ and "
                 "a\0b", "a\nb", "a\rb", "a\tb", "a\x1fb", "a\x7fb", "a\x85b", "a\x9fb",          # Cc, including C0, DEL and C1
                 "a\u2028b", "a\u2029b",                                                         # Zl, Zp
                 "a\ud800b", "a\udfffb",                                                         # Cs (lone surrogates)
                 "a" * 256, "é" * 128, "€" * 86, None, 5, b"a.pdf"]                                # over 255 UTF-8 bytes, not text


@pytest.mark.parametrize("name", ACCEPTED_NAMES)
def test_every_valid_unicode_basename_is_accepted_unchanged(name):
    assert validate_filename(name) is name


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_separators_quotes_controls_line_breaks_and_overlong_names_are_refused(name):
    with pytest.raises(DomainError) as refused:
        validate_filename(name)
    assert code(refused) == "validation_error"


def test_a_name_is_stored_exactly_as_given_without_normalization(env):
    for key, name in (("nfd", "e\u0301.pdf"), ("nfc", "\u00e9.pdf"), ("bidi", "a\u202eb.pdf")):
        payload = stage(env, key=key, name=name)
        assert payload["filename"] == name and env.row(payload["payload_id"])["filename"] == name


# ----- copying, hashing, limits ------------------------------------------------------------------------


def test_the_validator_sees_a_read_only_seekable_view_of_the_exact_copy(env):
    validator = Validator()
    stage(env, name="é.pdf", validate_snapshot=validator)
    (call,) = validator.calls
    assert call == {"send_as": "document", "size": len(PDF), "filename": "é.pdf", "content": PDF, "position": 0,
                    "seekable": True, "writable": False}


def test_a_hash_mismatch_is_enforced_by_the_store_before_the_validator_and_removes_the_copy(env):
    validator = Validator()
    with pytest.raises(DomainError) as refused:
        stage(env, key="k", expected_sha256=digest(b"something else"), validate_snapshot=validator)
    assert code(refused) == "hash_mismatch" and validator.calls == []
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["reserved_bytes"], row["size_bytes"]) == ("deleted", "hash_mismatch", 0, None)
    assert env.files() == []


def test_a_validator_that_approves_cannot_override_a_wrong_hash_or_an_oversize_copy(env, monkeypatch):
    monkeypatch.setitem(media_payloads.MAX_BYTES, "document", 10)
    approving = Validator(result={"ok": True})
    with pytest.raises(DomainError) as small:
        stage(env, b"tiny", expected_sha256="0" * 64, validate_snapshot=approving)
    with pytest.raises(DomainError) as large:
        stage(env, PDF, validate_snapshot=approving)
    assert (code(small), code(large)) == ("hash_mismatch", "source_too_large") and approving.calls == []


def test_the_size_limit_is_inclusive_and_the_reader_is_never_asked_for_more_than_one_extra_byte(env, monkeypatch):
    monkeypatch.setitem(media_payloads.MAX_BYTES, "document", 1000)
    exact = b"x" * 1000
    assert stage(env, exact)["size_bytes"] == 1000
    over = b"y" * 5000
    source = Source(over)
    with pytest.raises(DomainError) as refused:
        stage(env, over, opener=source)
    assert code(refused) == "source_too_large" and source.served <= 1001
    assert [r["state"] for r in env.rows()] == ["ready", "deleted"] and len(env.files()) == 1


def test_the_published_limits_and_the_reservation_per_mode(env):
    assert media_payloads.MAX_BYTES == {"document": 50_000_000, "photo": 10_000_000}
    assert (MediaPayloads.TOTAL_BYTES, MediaPayloads.MAX_PENDING, MediaPayloads.MAX_COPIES) == (1 << 30, 50, 2)
    assert (MediaPayloads.RETENTION_SECONDS, MediaPayloads.LEASE_SECONDS) == (86400, 300.0)
    assert MediaPayloads.COPY_SECONDS < MediaPayloads.LEASE_SECONDS
    stage(env, b"d", send_as="document")
    stage(env, b"p", send_as="photo", name="p.png")
    assert [(r["send_as"], r["max_bytes"], r["reserved_bytes"]) for r in env.rows()] == [("document", 50_000_000, 1), ("photo", 10_000_000, 1)]


def test_a_real_photo_at_ten_million_bytes_is_accepted_and_one_more_is_not(env):
    exact, over = b"\1" * 10_000_000, b"\1" * 10_000_001
    assert stage(env, exact, send_as="photo", name="x.png")["size_bytes"] == 10_000_000
    with pytest.raises(DomainError) as refused:
        stage(env, over, send_as="photo", name="y.png")
    assert code(refused) == "source_too_large"
    assert len(env.files()) == 1


def test_an_empty_source_fails(env):
    with pytest.raises(DomainError) as refused:
        stage(env, b"", expected_sha256=digest(b""))
    assert code(refused) == "source_empty" and env.files() == []


class _NoneReader(Source):
    def __call__(self):
        self.opens += 1
        return _NoneStream(self)


class _NoneStream(_Stream):
    def read(self, size):
        return None


@pytest.mark.parametrize("scenario", ["open", "read", "authority", "none"])
def test_a_source_failure_is_a_pre_send_staging_failure_that_spends_the_key(env, scenario):
    source = {"open": Source(error=OSError("denied")), "read": Source(read_error=OSError("io")),
              "authority": Source(error=DomainError("forbidden", "The source is no longer readable")), "none": _NoneReader()}[scenario]
    with pytest.raises(DomainError) as refused:
        stage(env, key="fixed", opener=source)
    assert code(refused) == ("forbidden" if scenario == "authority" else "source_unreadable")
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["reserved_bytes"]) == ("deleted", "source_unreadable", 0) and env.files() == []
    again = Source()
    with pytest.raises(DomainError) as spent:
        stage(env, key="fixed", opener=again)
    assert code(spent) == "staging_failed" and spent.value.details["failure_code"] == "source_unreadable" and again.opens == 0


@pytest.mark.parametrize("failure, expected", [
    (DomainError("invalid_image", "Not a PNG or JPEG"), "invalid_image"), (ValueError("boom"), "validation_failed")])
def test_a_validator_failure_is_a_staging_failure_with_its_own_error_for_the_caller(env, failure, expected):
    with pytest.raises(DomainError) as refused:
        stage(env, validate_snapshot=Validator(error=failure))
    assert code(refused) == expected
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["reserved_bytes"]) == ("deleted", expected, 0) and env.files() == []


@pytest.mark.parametrize("result", [object(), [1, 2], "text", {"x": "y" * 5000}, {"x": float("nan")}, {1: "a"}])
def test_the_validation_result_must_be_a_small_plain_json_object(env, result):
    with pytest.raises(DomainError) as refused:
        stage(env, validate_snapshot=Validator(result=result))
    assert code(refused) == "invalid_metadata" and env.files() == []


def test_a_source_changed_while_it_is_copied_is_detected(env):
    original = env.tmp / "source.pdf"
    original.write_bytes(PDF)

    class Real(Source):
        def __call__(self):
            self.opens += 1
            handle = open(original, "rb")

            class Changing:
                def __enter__(inner):
                    return inner

                def __exit__(inner, *exc):
                    handle.close()

                def fileno(inner):
                    return handle.fileno()

                def read(inner, size):
                    chunk = handle.read(size)
                    if chunk:
                        os.utime(original, ns=(1, 1))  # an ordinary edit time change after the first bytes were read
                    return chunk

            return Changing()

    with pytest.raises(DomainError) as refused:
        stage(env, opener=Real())
    assert code(refused) == "source_changed" and env.files() == []


def test_a_cancelled_copy_frees_its_quota_and_removes_its_files(env):
    with pytest.raises(KeyboardInterrupt):
        stage(env, key="cancelled", opener=Source(read_error=KeyboardInterrupt()))
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["reserved_bytes"]) == ("deleted", "interrupted", 0) and env.files() == []
    assert env.payloads.status()["reserved_bytes"] == 0 and env.payloads.status()["active_copies"] == 0


def test_an_unsafe_state_root_or_media_directory_is_refused_before_a_reservation(env):
    os.chmod(env.state, 0o755)
    with pytest.raises(DomainError) as refused:
        stage(env)
    assert code(refused) == "invalid_configuration" and env.rows() == []
    os.chmod(env.state, 0o700)
    (env.state / "elsewhere").mkdir(mode=0o700)
    os.symlink(env.state / "elsewhere", env.media)
    with pytest.raises(DomainError) as linked:
        stage(env)
    assert code(linked) == "invalid_configuration" and env.rows() == [] and list((env.state / "elsewhere").iterdir()) == []
    env.media.unlink()
    env.media.mkdir(mode=0o755)
    os.chmod(env.media, 0o755)
    with pytest.raises(DomainError) as open_dir:
        stage(env)
    assert code(open_dir) == "invalid_configuration" and env.rows() == []


# ----- idempotent request identity ---------------------------------------------------------------------


def test_a_repeated_key_returns_the_sealed_original_without_reading_the_source_again(env):
    source = Source()
    first = stage(env, key="same", opener=source)
    changed = Source(b"entirely different bytes")
    second = stage(env, key="same", opener=changed)
    third = env.restart().stage(**request(env, request_key="same", opener=changed))
    assert first == second == third and source.opens == 1 and changed.opens == 0
    assert len(env.rows()) == 1 and len(env.files()) == 1


def test_the_sealed_snapshot_survives_the_source_being_removed_or_edited_and_a_restart(env):
    original = env.tmp / "source.pdf"
    original.write_bytes(PDF)
    payload = stage(env, key="sealed", opener=lambda: open(original, "rb"))
    original.write_bytes(b"edited afterwards")
    assert stage(env, key="sealed", opener=lambda: open(original, "rb")) == payload
    original.unlink()
    with env.restart().open_payload(payload["payload_id"]) as opened:
        assert opened.stream.read() == PDF
    assert env.restart().stage(**request(env, request_key="sealed", opener=lambda: open(original, "rb"))) == payload


@pytest.mark.parametrize("change", [{"arguments_hash": "2" * 64}, {"source_binding": {"kind": "document"}},
                                    {"expected_sha256": digest(b"other"), "data": b"other"}, {"filename": "renamed.pdf"},
                                    {"send_as": "photo"}])
def test_the_same_key_with_a_different_manifest_is_a_conflict_even_before_any_receipt(env, change):
    stage(env, key="one", name="a.pdf")
    other_data = change.pop("data", PDF)
    source = Source(other_data)
    with pytest.raises(DomainError) as refused:
        stage(env, other_data, request_key="one", opener=source, **{"filename": "a.pdf", **change})
    assert code(refused) == "idempotency_conflict" and source.opens == 0 and len(env.rows()) == 1


def test_changing_the_caption_purpose_or_reply_is_a_changed_argument_hash(env):
    stage(env, key="args", arguments_hash=hashlib.sha256(b"caption one").hexdigest())
    with pytest.raises(DomainError) as refused:
        stage(env, request_key="args", arguments_hash=hashlib.sha256(b"caption two").hexdigest())
    assert code(refused) == "idempotency_conflict"


def test_different_keys_make_different_snapshots_and_principals_do_not_share_keys(env):
    first = stage(env, key="a")
    second = stage(env, key="b")
    third = stage(env, key="a", principal_id=env.other)
    assert len({first["payload_id"], second["payload_id"], third["payload_id"]}) == 3 and len(env.files()) == 3


def test_a_failed_request_key_is_spent_and_never_silently_reused(env):
    with pytest.raises(DomainError):
        stage(env, key="spent", expected_sha256="0" * 64)
    source = Source()
    with pytest.raises(DomainError) as refused:
        stage(env, key="spent", expected_sha256="0" * 64, opener=source)  # the same request again
    assert code(refused) == "staging_failed" and refused.value.details["failure_code"] == "hash_mismatch" and source.opens == 0
    with pytest.raises(DomainError) as corrected:
        stage(env, key="spent", opener=source)  # a corrected hash is a different request under the same key
    assert code(corrected) == "idempotency_conflict" and source.opens == 0 and len(env.rows()) == 1
    assert stage(env, key="fresh")["state"] == "ready"


def test_a_second_call_with_a_key_that_is_still_being_copied_is_busy_until_the_first_finishes(env):
    entered, release = threading.Event(), threading.Event()
    outcome = {}
    first_source = Source(entered=entered, release=release)
    thread = threading.Thread(target=lambda: outcome.setdefault("result", stage(env, key="slow", opener=first_source)))
    thread.start()
    assert entered.wait(10)
    with pytest.raises(DomainError) as busy:
        stage(env, key="slow")
    assert code(busy) == "busy" and busy.value.retryable
    release.set()
    thread.join(10)
    assert stage(env, key="slow") == outcome["result"]


# ----- capacity: copies, pending deliveries, bytes -------------------------------------------------------------


def blocked_copy(env, key):
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def run():
        try:
            outcome["result"] = stage(env, key=key, opener=Source(entered=entered, release=release))
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(10)
    return thread, release, outcome


def test_at_most_two_copies_run_and_a_third_is_refused_until_one_ends(env):
    first, release_first, _ = blocked_copy(env, "c1")
    second, release_second, _ = blocked_copy(env, "c2")
    with pytest.raises(DomainError) as refused:
        stage(env, key="c3")
    assert code(refused) == "capacity_exceeded" and refused.value.retryable and refused.value.details == {"limit": "concurrent_copies"}
    assert env.payloads.status()["active_copies"] == 2
    release_first.set()
    first.join(10)
    assert stage(env, key="c3")["state"] == "ready"
    release_second.set()
    second.join(10)
    assert env.payloads.status()["active_copies"] == 0


def test_a_failed_or_cancelled_copy_gives_its_slot_and_bytes_back(env):
    thread, release, outcome = blocked_copy(env, "fails")
    assert env.payloads.status()["reserved_bytes"] == 50_000_000
    env.sql("UPDATE telegram_file_payloads SET max_bytes=max_bytes WHERE 0")  # a no-op unit proves writers are not blocked by the copy
    release.set()
    thread.join(10)
    assert "result" in outcome and env.payloads.status()["reserved_bytes"] == len(PDF)
    for index in range(4):
        with pytest.raises(DomainError):
            stage(env, key=f"bad{index}", expected_sha256="0" * 64)
    status = env.payloads.status()
    assert status["active_copies"] == 0 and status["reserved_bytes"] == len(PDF)


def test_concurrent_staging_never_exceeds_two_copies_and_every_caller_gets_a_clear_outcome(env):
    running = peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(8)
    results = []

    class Counting(Source):
        def __call__(self):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            return _Counted(self)

    class _Counted(_Stream):
        def __exit__(inner, *exc):
            nonlocal running
            with lock:
                running -= 1
            return False

        def read(inner, size):
            if inner.position == 0:
                threading.Event().wait(0.15)  # hold the copy open long enough for the others to collide
            return super().read(size)

    def worker(index):
        barrier.wait()
        try:
            results.append(("ok", stage(env, key=f"k{index}", opener=Counting())["payload_id"]))
        except DomainError as exc:
            results.append((exc.code, exc.details and exc.details.get("limit")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert peak <= 2 and len(results) == 8
    assert {r for r in results if r[0] != "ok"} <= {("capacity_exceeded", "concurrent_copies")}
    assert sum(1 for r in results if r[0] == "ok") >= 1
    ready = env.rows("state='ready'")
    assert len(ready) == sum(1 for r in results if r[0] == "ok") and len(env.files()) == len(ready)
    assert env.payloads.status()["active_copies"] == 0


def test_pending_deliveries_are_capped_and_a_terminal_delivery_stops_counting(env):
    env.payloads.MAX_PENDING = 3
    payloads = [stage(env, key=f"p{i}") for i in range(3)]
    with pytest.raises(DomainError) as refused:
        stage(env, key="p3")
    assert code(refused) == "capacity_exceeded" and refused.value.details == {"limit": "pending_deliveries"}
    message_id, outbox_id = queue(env, payloads[0])
    attach(env, payloads[0], message_id, outbox_id)
    with pytest.raises(DomainError):
        stage(env, key="p3")  # attached to a queued send is still pending
    deliver(env, outbox_id, "uncertain")
    with pytest.raises(DomainError):
        stage(env, key="p3")  # and so is an uncertain one
    deliver(env, outbox_id, "sent")
    assert stage(env, key="p3")["state"] == "ready"
    assert env.payloads.status()["pending_deliveries"] == 3


def test_total_reserved_bytes_are_capped_conservatively_and_settle_to_the_real_size(env):
    env.payloads.TOTAL_BYTES, env.payloads.MAX_COPIES = 120_000_000, 10  # two 50 MB document reservations fit, a third does not
    one, release_one, _ = blocked_copy(env, "r1")
    two, release_two, _ = blocked_copy(env, "r2")
    with pytest.raises(DomainError) as refused:
        stage(env, key="r3")
    assert code(refused) == "capacity_exceeded" and refused.value.details == {"limit": "stored_bytes"}
    release_one.set(), release_two.set()
    one.join(10), two.join(10)
    assert env.payloads.status()["reserved_bytes"] == 2 * len(PDF)
    assert stage(env, key="r3")["state"] == "ready"


def test_concurrent_reservations_cannot_overshoot_the_byte_cap(env):
    env.payloads.TOTAL_BYTES, env.payloads.MAX_COPIES = 120_000_000, 10
    barrier, results = threading.Barrier(6), []
    gate = threading.Event()

    def worker(index):
        barrier.wait()
        try:
            results.append(stage(env, key=f"b{index}", opener=Source(release=gate, entered=threading.Event()))["state"])
        except DomainError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    threading.Event().wait(0.5)
    reserved_while_copying = env.payloads.status()["reserved_bytes"]
    gate.set()
    [t.join(20) for t in threads]
    assert reserved_while_copying <= 120_000_000 and results.count("capacity_exceeded") >= 4
    assert env.payloads.status()["reserved_bytes"] <= 120_000_000


# ----- attachment ---------------------------------------------------------------------------------------


def test_attach_binds_a_ready_payload_to_its_queued_send_and_inspect_reports_it(env):
    payload = stage(env, key="attach")
    message_id, outbox_id = queue(env, payload)
    done = attach(env, payload, message_id, outbox_id)
    assert (done["state"], done["message_id"], done["outbox_id"]) == ("attached", message_id, outbox_id) and done["attached_at"]
    with env.bridge.read() as uow:
        assert env.payloads.inspect(uow, payload["payload_id"]) == done
        with pytest.raises(DomainError) as missing:
            env.payloads.inspect(uow, new_id().replace("-", ""))
    assert code(missing) == "not_found"


def test_repeating_the_same_attachment_is_a_no_op_and_any_other_association_fails(env):
    payload = stage(env, key="twice")
    message_id, outbox_id = queue(env, payload)
    first = attach(env, payload, message_id, outbox_id)
    revision = env.row(payload["payload_id"])["revision"]
    assert attach(env, payload, message_id, outbox_id) == first and env.row(payload["payload_id"])["revision"] == revision
    other_message, other_outbox = queue(env, payload)
    for pair in ((other_message, other_outbox), (message_id, other_outbox), (other_message, outbox_id)):
        with pytest.raises(DomainError) as refused:
            attach(env, payload, *pair)
        assert code(refused) == "payload_attached"
    assert env.row(payload["payload_id"])["outbox_id"] == outbox_id


@pytest.mark.parametrize("arrange", [
    lambda env, payload: queue(env, payload, kind="photo"),
    lambda env, payload: queue(env, payload, agent=env.other),
    lambda env, payload: queue(env, payload, direction="inbound"),
    lambda env, payload: queue(env, payload, payload_json={"version": 1, "payload_id": "0" * 32}),
    lambda env, payload: queue(env, payload, payload_json={"version": 2, "payload_id": payload["payload_id"]}),
    lambda env, payload: queue(env, payload, payload_json={"version": True, "payload_id": payload["payload_id"]}),
    lambda env, payload: queue(env, payload, payload_json={"version": 1.0, "payload_id": payload["payload_id"]}),
    lambda env, payload: queue(env, payload, payload_json={"version": 1, "payload_id": 5}),
    lambda env, payload: queue(env, payload, payload_json={"version": 1, "payload_id": payload["payload_id"], "path": "/x"}),
], ids=["kind", "agent", "inbound-message", "other-payload", "version", "version-true", "version-float", "id-not-text",
        "extra-key"])
def test_attach_refuses_a_delivery_that_is_not_exactly_this_payloads_send(env, arrange):
    payload = stage(env)
    message_id, outbox_id = arrange(env, payload)
    with pytest.raises(DomainError) as refused:
        attach(env, payload, message_id, outbox_id)
    assert code(refused) == "payload_mismatch" and env.row(payload["payload_id"])["state"] == "ready"


def test_attach_refuses_missing_rows_a_started_delivery_and_a_second_payload_on_the_same_send(env):
    payload = stage(env)
    with pytest.raises(DomainError) as missing:
        attach(env, payload, new_id(), new_id())
    assert code(missing) == "payload_mismatch"
    message_id, outbox_id = queue(env, payload)
    deliver(env, outbox_id, "dispatching")
    with pytest.raises(DomainError) as started:
        attach(env, payload, message_id, outbox_id)
    assert code(started) == "payload_mismatch" and env.row(payload["payload_id"])["state"] == "ready"
    with env.bridge.write("test") as uow:
        with pytest.raises(DomainError) as unknown:
            env.payloads.attach(uow, new_id().replace("-", ""), message_id=message_id, outbox_id=outbox_id)
    assert code(unknown) == "not_found"
    winner, second = stage(env, key="w"), stage(env, key="s")
    message_id, outbox_id = queue(env, winner)
    attach(env, winner, message_id, outbox_id)
    with pytest.raises(DomainError) as duplicate:
        attach(env, second, message_id, outbox_id)
    assert code(duplicate) == "payload_mismatch" and env.row(second["payload_id"])["state"] == "ready"


def test_attach_needs_a_write_unit_and_commits_or_rolls_back_with_the_callers_unit(env):
    payload = stage(env, key="atomic")
    message_id, outbox_id = queue(env, payload)
    with env.bridge.read() as uow:
        with pytest.raises(DomainError) as read_only:
            env.payloads.attach(uow, payload["payload_id"], message_id=message_id, outbox_id=outbox_id)
    assert code(read_only) == "read_only"
    with pytest.raises(RuntimeError):
        with env.bridge.write("test.fails") as uow:
            env.payloads.attach(uow, payload["payload_id"], message_id=message_id, outbox_id=outbox_id)
            raise RuntimeError("the operation receipt could not be saved")
    assert env.row(payload["payload_id"])["state"] == "ready" and env.row(payload["payload_id"])["outbox_id"] is None
    assert attach(env, payload, message_id, outbox_id)["state"] == "attached"


def test_only_ready_payloads_attach(env):
    failed_key = "bad"
    with pytest.raises(DomainError):
        stage(env, key=failed_key, expected_sha256="0" * 64)
    (failed,) = env.rows()
    message_id, outbox_id = queue(env, {"send_as": "document", "payload_id": failed["id"]})
    with pytest.raises(DomainError) as refused:
        attach(env, {"payload_id": failed["id"]}, message_id, outbox_id)
    assert code(refused) == "payload_unavailable"


# ----- opening the sealed snapshot for dispatch -------------------------------------------------------------------


def test_open_payload_yields_a_verified_read_only_seekable_stream_and_plain_metadata(env):
    payload, _, _ = attached(env, key="open", name="Отчёт.pdf")
    calls = Source()
    with env.payloads.open_payload(payload["payload_id"]) as opened:
        assert isinstance(opened, OpenPayload)
        assert opened.metadata == {"payload_id": payload["payload_id"], "filename": "Отчёт.pdf", "send_as": "document",
                                   "size": len(PDF), "sha256": digest(PDF), "validated": {"format": "pdf", "pages": 1}}
        json.dumps(opened.metadata)
        stream = opened.stream
        assert stream.tell() == 0 and stream.seekable() and stream.readable() and not stream.writable()
        assert stream.read() == PDF
        stream.seek(5)
        assert stream.read(3) == PDF[5:8]
        with pytest.raises(io.UnsupportedOperation):
            stream.write(b"x")
    assert stream.closed and calls.opens == 0


def test_a_ready_unattached_payload_can_also_be_opened(env):
    payload = stage(env)
    with env.payloads.open_payload(payload["payload_id"]) as opened:
        assert opened.stream.read() == PDF


@pytest.mark.parametrize("damage, expected", [
    ("flip", "payload_corrupt"), ("truncate", "payload_corrupt"), ("grow", "payload_corrupt"), ("missing", "payload_missing"),
    ("symlink", "payload_unsafe"), ("mode644", "payload_unsafe"), ("mode666", "payload_unsafe"), ("mode700", "payload_unsafe"),
    ("hardlink", "payload_unsafe"), ("fifo", "payload_unsafe"), ("directory", "payload_unsafe"),
    ("media-dir-open", "payload_unsafe"), ("media-dir-gone", "payload_missing"),
])
def test_a_missing_altered_or_unsafe_snapshot_fails_explicitly_and_is_never_recreated(env, damage, expected):
    source = env.tmp / "source.pdf"
    source.write_bytes(PDF)
    payload, _, _ = attached(env, key="damaged", opener=lambda: open(source, "rb"))
    path = env.path(payload["payload_id"])
    if damage == "flip":
        data = bytearray(path.read_bytes())
        data[10] ^= 0xFF
        path.write_bytes(bytes(data))
    elif damage == "truncate":
        os.truncate(path, 10)
    elif damage == "grow":
        with open(path, "ab") as handle:
            handle.write(b"x")
    elif damage == "missing":
        path.unlink()
    elif damage == "symlink":
        target = env.tmp / "elsewhere.bin"
        target.write_bytes(PDF)
        path.unlink()
        os.symlink(target, path)
    elif damage in ("mode644", "mode666", "mode700"):
        os.chmod(path, {"mode644": 0o644, "mode666": 0o666, "mode700": 0o700}[damage])
    elif damage == "hardlink":
        os.link(path, env.tmp / "second-name")
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif damage == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif damage == "media-dir-open":
        os.chmod(env.media, 0o755)
    elif damage == "media-dir-gone":
        shutil.rmtree(env.media)
    with pytest.raises(DomainError) as refused:
        with env.payloads.open_payload(payload["payload_id"]):
            pytest.fail("a damaged snapshot must not be opened")
    assert code(refused) == expected
    assert source.exists()
    if damage == "missing":
        assert not path.exists()  # never regenerated from the still-existing source


def test_file_ownership_other_than_the_service_user_is_unsafe():
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=os.geteuid() + 1)
    assert MediaPayloads._unsafe(info) == "payload_unsafe"
    assert MediaPayloads._unsafe(SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=os.geteuid())) is None


@pytest.mark.parametrize("arrange", ["unknown", "deleted", "failed"])
def test_unfinished_removed_or_unknown_payloads_cannot_be_opened(env, arrange):
    if arrange == "unknown":
        payload_id = new_id().replace("-", "")
    elif arrange == "deleted":
        payload_id = stage(env)["payload_id"]
        env.clock.advance(hours=25)
        env.payloads.sweep()
    else:
        with pytest.raises(DomainError):
            stage(env, expected_sha256="0" * 64)
        payload_id = env.rows()[0]["id"]
    with pytest.raises(DomainError) as refused:
        with env.payloads.open_payload(payload_id):
            pass
    assert code(refused) == "payload_unavailable"


def test_a_payload_still_staging_cannot_be_opened(env):
    thread, release, _ = blocked_copy(env, "mid")
    (row,) = env.rows()
    with pytest.raises(DomainError) as refused:
        with env.payloads.open_payload(row["id"]):
            pass
    release.set()
    thread.join(10)
    assert code(refused) == "payload_unavailable"


# ----- crashes, recovery, restart -------------------------------------------------------------------------------


@pytest.fixture
def crashing(monkeypatch):
    """A process that dies mid-staging: nothing in ``_abandon`` runs, so rows and files stay exactly as the crash left them."""
    monkeypatch.setattr(MediaPayloads, "_abandon", lambda self, *args, **kwargs: None)


class Dies(Source):
    """A source whose process dies after the first chunk was copied."""

    def __call__(self):
        self.opens += 1
        stream = _Stream(self)
        original = stream.read

        def read(size):
            chunk = original(size)
            if chunk:
                raise KeyboardInterrupt
            return chunk

        stream.read = read
        return stream


ZERO = {"stale_staging": 0, "deleted": 0, "orphans_removed": 0, "flagged": 0}
SWEEP_ZERO = {"stale_staging": 0, "expired": 0, "delivered": 0, "deleted": 0}


def crash(env, key="crashed", opener=None):
    with pytest.raises(KeyboardInterrupt):
        stage(env, key=key, opener=opener or Dies())


@pytest.mark.parametrize("opener", [lambda: Source(error=KeyboardInterrupt()), Dies], ids=["after-reserve", "mid-copy"])
def test_an_abandoned_attempt_keeps_its_record_bytes_and_reservation_until_its_24_hour_retention(env, crashing, opener):
    crash(env, opener=opener())
    (row,) = env.rows()
    assert row["state"] == "staging" and row["reserved_bytes"] == 50_000_000 and env.payloads.status()["active_copies"] == 1
    kept = env.files()
    assert len(kept) == 1 and kept[0].endswith(".part")
    with pytest.raises(DomainError) as busy:
        stage(env, key="crashed")
    assert code(busy) == "busy"
    env.clock.advance(seconds=301)  # the lease is gone; nothing else is
    status = env.payloads.status()
    assert (status["active_copies"], status["reserved_bytes"], status["payloads"]["staging"]) == (0, 50_000_000, 1)
    restarted = env.restart()
    assert restarted.recover() == ZERO and restarted.sweep() == SWEEP_ZERO
    assert stage(env, key="other")["state"] == "ready"  # a new reservation reaps nothing either
    again = Source()
    with pytest.raises(DomainError) as spent:
        restarted.stage(**request(env, request_key="crashed", opener=again))
    assert code(spent) == "staging_failed" and spent.value.details == {"failure_code": "interrupted"} and again.opens == 0
    assert env.rows("request_key='crashed'")[0]["state"] == "staging" and kept[0] in env.files()
    env.clock.advance(seconds=86400 - 1 - 301)  # one second before expires_at
    assert restarted.recover() == ZERO and restarted.sweep() == SWEEP_ZERO and kept[0] in env.files()
    assert env.payloads.status()["reserved_bytes"] == 50_000_000 + len(PDF)
    env.clock.advance(seconds=1)
    assert restarted.recover() == {**ZERO, "stale_staging": 1, "deleted": 1}
    row = env.rows("request_key='crashed'")[0]
    assert (row["state"], row["failure_code"], row["cleanup_reason"], row["reserved_bytes"]) == ("deleted", "interrupted", "failed", 0)
    assert kept[0] not in env.files() and len(env.files()) == 1
    assert env.payloads.status()["reserved_bytes"] == len(PDF)
    with pytest.raises(DomainError) as still_spent:
        restarted.stage(**request(env, request_key="crashed", opener=again))
    assert code(still_spent) == "staging_failed" and again.opens == 0


def test_a_crash_after_the_atomic_rename_but_before_the_database_seal_is_not_adopted_and_follows_the_retention(env, crashing, monkeypatch):
    real = MediaPayloads._seal
    monkeypatch.setattr(MediaPayloads, "_seal", lambda self, *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    crash(env, key="unsealed", opener=Source())
    (row,) = env.rows()
    assert [name.endswith(".media") for name in env.files()] == [True] and row["state"] == "staging"
    monkeypatch.setattr(MediaPayloads, "_seal", real)
    env.clock.advance(hours=1)
    restarted = env.restart()
    assert restarted.recover() == ZERO and restarted.sweep() == SWEEP_ZERO
    assert len(env.files()) == 1 and env.rows()[0]["state"] == "staging"  # not adopted, not removed
    with pytest.raises(DomainError) as spent:
        restarted.stage(**request(env, request_key="unsealed", opener=Source()))
    assert code(spent) == "staging_failed"
    assert env.rows()[0]["state"] == "staging"  # still no sealed row, still no new copy
    env.clock.advance(hours=23)
    assert restarted.sweep() == {**SWEEP_ZERO, "stale_staging": 1, "deleted": 1}
    row = env.rows()[0]
    assert env.files() == [] and (row["state"], row["failure_code"], row["reserved_bytes"]) == ("deleted", "interrupted", 0)


def test_an_abandoned_attempt_counts_against_every_quota_until_cleaned_but_frees_its_copy_slot(env, crashing):
    env.payloads.TOTAL_BYTES, env.payloads.MAX_PENDING = 120_000_000, 2
    crash(env, key="c1")
    crash(env, key="c2")
    env.clock.advance(seconds=301)
    assert env.payloads.status()["active_copies"] == 0
    with pytest.raises(DomainError) as pending:
        stage(env, key="third")
    assert code(pending) == "capacity_exceeded" and pending.value.details == {"limit": "pending_deliveries"}
    env.payloads.MAX_PENDING = 10
    with pytest.raises(DomainError) as stored:  # 2 x 50 MB reserved: a third document reservation would not fit in 120 MB
        stage(env, key="third")
    assert code(stored) == "capacity_exceeded" and stored.value.details == {"limit": "stored_bytes"}
    assert stage(env, b"p", send_as="photo", name="p.png", key="small")["state"] == "ready"  # the copy slots are free, 10 MB fits
    env.clock.advance(seconds=86400)  # past the retention; the next reservation reaps both
    assert stage(env, key="third")["state"] == "ready"
    assert [r["state"] for r in env.rows("request_key IN ('c1','c2')")] == ["deleted", "deleted"]
    assert env.payloads.status()["reserved_bytes"] <= 2 * len(PDF) + 1


# ----- one attempt deadline and lease through validation and sealing -------------------------------------------------


class Slow(Validator):
    """A validator that works for a while: the monotonic and wall clocks have moved by the time it returns."""

    def __init__(self, env, *, monotonic=0.0, wall=0.0, **kwargs):
        super().__init__(**kwargs)
        self.env, self.monotonic, self.wall = env, monotonic, wall

    def __call__(self, stream, **kwargs):
        result = super().__call__(stream, **kwargs)
        self.env.mono.advance(self.monotonic)
        self.env.clock.advance(seconds=self.wall)
        return result


def assert_failed_and_spent(env, refused, failure, *, key):
    assert code(refused) == failure and not env.rows("state IN ('ready','attached')") and env.files() == []
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["cleanup_reason"], row["reserved_bytes"]) == ("deleted", failure, "failed", 0)
    message = refused.value.message
    assert "new idempotency key" in message and "same idempotency key" not in message and not refused.value.retryable
    source = Source()
    with pytest.raises(DomainError) as spent:
        stage(env, key=key, opener=source)
    assert code(spent) == "staging_failed" and spent.value.details == {"failure_code": failure} and source.opens == 0


@pytest.mark.parametrize("monotonic, wall, failure", [
    (MediaPayloads.COPY_SECONDS + 1, MediaPayloads.LEASE_SECONDS + 1, "timeout"),
    (MediaPayloads.COPY_SECONDS + 1, 0, "timeout"),
    (0, MediaPayloads.LEASE_SECONDS + 1, "lost_lease"),
], ids=["deadline-and-lease", "deadline-only", "lease-only"])
def test_a_copy_that_outlives_its_deadline_or_lease_while_validating_is_never_ready(env, monotonic, wall, failure):
    validator = Slow(env, monotonic=monotonic, wall=wall)
    with pytest.raises(DomainError) as refused:
        stage(env, key="slow", validate_snapshot=validator)
    assert len(validator.calls) == 1 and validator.calls[0]["content"] == PDF  # the bytes were read and validated
    assert_failed_and_spent(env, refused, failure, key="slow")


@pytest.mark.parametrize("monotonic, wall, failure", [
    (MediaPayloads.COPY_SECONDS + 1, 0, "timeout"), (0, MediaPayloads.LEASE_SECONDS + 1, "lost_lease")], ids=["deadline", "lease"])
def test_a_copy_that_runs_out_of_time_immediately_before_the_seal_is_not_sealed(env, monkeypatch, monotonic, wall, failure):
    real = MediaPayloads._seal

    def late(self, *args):
        env.mono.advance(monotonic)
        env.clock.advance(seconds=wall)
        return real(self, *args)

    monkeypatch.setattr(MediaPayloads, "_seal", late)
    with pytest.raises(DomainError) as refused:
        stage(env, key="late")
    assert_failed_and_spent(env, refused, failure, key="late")


def test_a_slow_source_past_the_deadline_fails_with_a_timeout(env):
    with pytest.raises(DomainError) as refused:
        stage(env, key="slow-source", opener=Source(after_first=lambda: env.mono.advance(MediaPayloads.COPY_SECONDS + 1)))
    assert_failed_and_spent(env, refused, "timeout", key="slow-source")


def test_the_attempt_deadline_is_shorter_than_the_lease_and_includes_the_reservation_itself(env, monkeypatch):
    assert MediaPayloads.COPY_SECONDS < MediaPayloads.LEASE_SECONDS
    original = MediaPayloads._reserve

    def reserve(self, request):
        env.mono.advance(1)  # the reservation takes a second of the attempt's budget
        return original(self, request)

    monkeypatch.setattr(MediaPayloads, "_reserve", reserve)
    inside = Slow(env, monotonic=MediaPayloads.COPY_SECONDS - 1.5, wall=MediaPayloads.LEASE_SECONDS - 5)
    assert stage(env, key="budget", validate_snapshot=inside)["state"] == "ready"
    over = Slow(env, monotonic=MediaPayloads.COPY_SECONDS - 0.5, wall=1)  # with the reserved second, half a second too many
    with pytest.raises(DomainError) as refused:
        stage(env, key="budget-2", validate_snapshot=over)
    assert code(refused) == "timeout"


def test_a_valid_seal_inside_the_limits_stays_idempotent_for_the_same_key(env):
    validator = Slow(env, monotonic=MediaPayloads.COPY_SECONDS - 1, wall=MediaPayloads.LEASE_SECONDS - 1)
    source = Source()
    payload = stage(env, key="just-in-time", validate_snapshot=validator, opener=source)
    assert payload["state"] == "ready" and env.row(payload["payload_id"])["lease_owner"] is None
    again = Source(b"never read")
    assert stage(env, key="just-in-time", opener=again) == payload and again.opens == 0 and len(validator.calls) == 1


def test_a_copy_whose_lease_was_taken_over_cannot_publish_ready(env, monkeypatch):
    real = MediaPayloads._seal

    def takeover(self, *args):
        env.sql("UPDATE telegram_file_payloads SET lease_owner=?, revision=revision+1", "d" * 32)
        return real(self, *args)

    monkeypatch.setattr(MediaPayloads, "_seal", takeover)
    with pytest.raises(DomainError) as refused:
        stage(env, key="taken")
    (row,) = env.rows()
    assert code(refused) == "lost_lease" and (row["state"], row["lease_owner"]) == ("staging", "d" * 32)  # the new owner's, untouched
    assert "new idempotency key" in refused.value.message


def lose_seal_commit(env, monkeypatch, *, commit):
    """The seal write: with ``commit`` it commits and then reports a failure; without, it fails before anything is written."""
    real = env.bridge.write

    @contextmanager
    def write(operation, *args, **kwargs):
        if operation != "media.seal":
            with real(operation, *args, **kwargs) as uow:
                yield uow
            return
        if not commit:
            raise sqlite3.OperationalError("database is locked")
        with real(operation, *args, **kwargs) as uow:
            yield uow
        raise OSError("the commit result was lost")

    monkeypatch.setattr(env.bridge, "write", write)


def test_a_seal_whose_commit_outcome_is_unknown_keeps_the_committed_ready_row_and_its_file(env, monkeypatch):
    lose_seal_commit(env, monkeypatch, commit=True)
    payload = stage(env, key="ambiguous")
    (row,) = env.rows()
    assert payload["state"] == "ready" and (row["state"], row["sha256"]) == ("ready", digest(PDF))
    assert env.files() == [env.path(payload["payload_id"]).name] and env.path(payload["payload_id"]).read_bytes() == PDF
    assert stage(env, key="ambiguous", opener=Source(b"never read")) == payload


def test_a_seal_that_failed_before_committing_leaves_no_ready_row_and_removes_the_copy(env, monkeypatch):
    lose_seal_commit(env, monkeypatch, commit=False)
    with pytest.raises(sqlite3.OperationalError):
        stage(env, key="not-sealed")
    (row,) = env.rows()
    assert (row["state"], row["failure_code"], row["reserved_bytes"]) == ("deleted", "interrupted", 0) and env.files() == []


def test_a_crash_after_sealing_but_before_the_operation_receipt_reuses_the_snapshot_on_retry(env):
    payload = stage(env, key="retry")
    restarted = env.restart()
    again = restarted.stage(**request(env, request_key="retry", opener=Source(b"never read")))
    assert again == payload and len(env.files()) == 1
    message_id, outbox_id = queue(env, payload)
    assert attach(env, payload, message_id, outbox_id)["state"] == "attached"
    assert restarted.stage(**request(env, request_key="retry", opener=Source(b"never read")))["state"] == "attached"


def test_a_retry_whose_sealed_file_is_known_missing_fails_explicitly(env):
    payload = stage(env, key="gone")
    env.path(payload["payload_id"]).unlink()
    with pytest.raises(DomainError) as refused:
        stage(env, key="gone")
    assert code(refused) == "payload_missing" and len(env.rows()) == 1


def test_recovery_removes_orphans_keeps_everything_owned_and_flags_missing_snapshots(env):
    keep, lost = stage(env, key="keep"), stage(env, key="lost")
    env.path(lost["payload_id"]).unlink()
    foreign = [env.media / (os.urandom(16).hex() + ".media"), env.media / (os.urandom(16).hex() + ".part"),
               env.media / (env.row(keep["payload_id"])["storage_name"] + ".part")]
    for path in foreign:
        path.write_bytes(b"orphan")
    unrelated = env.media / "notes.txt"
    unrelated.write_bytes(b"not ours")
    result = env.restart().recover()
    assert result == {"stale_staging": 0, "deleted": 0, "orphans_removed": 3, "flagged": 1}
    assert sorted(env.files()) == sorted([env.path(keep["payload_id"]).name, "notes.txt"])
    assert env.row(lost["payload_id"])["error_code"] == "payload_missing" and env.row(keep["payload_id"])["error_code"] is None
    status = env.payloads.status()
    assert status["unavailable_snapshots"] == 1 and status["last_error_code"] == "payload_missing"
    env.path(keep["payload_id"]).write_bytes(b"short")
    os.chmod(env.path(keep["payload_id"]), 0o600)
    env.restart().recover()
    assert env.row(keep["payload_id"])["error_code"] == "payload_corrupt"
    # A flagged snapshot is still refused at open time with the explicit error; it is never repaired.
    with pytest.raises(DomainError):
        with env.payloads.open_payload(lost["payload_id"]):
            pass


def test_recovery_keeps_a_live_copy_and_its_files(env):
    thread, release, outcome = blocked_copy(env, "live")
    result = env.restart().recover()
    assert result["stale_staging"] == 0 and result["orphans_removed"] == 0 and len(env.files()) == 1
    release.set()
    thread.join(10)
    assert outcome["result"]["state"] == "ready" and len(env.files()) == 1


def test_an_interrupted_deletion_resumes_and_failures_are_persisted_with_backoff(env, monkeypatch):
    payload = stage(env, key="del")
    env.clock.advance(hours=25)
    real = MediaPayloads._unlink
    monkeypatch.setattr(MediaPayloads, "_unlink", lambda self, name: (_ for _ in ()).throw(PermissionError(13, "denied")))
    result = env.payloads.sweep()
    assert (result["expired"], result["deleted"]) == (1, 0)
    row = env.row(payload["payload_id"])
    assert (row["state"], row["cleanup_reason"], row["cleanup_attempts"], row["error_code"]) == ("deleting", "expired", 1, "cleanup_failed")
    assert "EACCES" in row["error_message"] and str(env.media) not in row["error_message"] and row["reserved_bytes"] == len(PDF)
    status = env.payloads.status()
    assert (status["cleanup_errors"], status["cleanup_attempts"], status["last_error_code"]) == (1, 1, "cleanup_failed")
    assert status["reserved_bytes"] == len(PDF)  # the bytes still count until they are really gone
    assert env.payloads.sweep()["deleted"] == 0 and env.row(payload["payload_id"])["cleanup_attempts"] == 1  # not yet due
    env.clock.advance(seconds=31)
    env.payloads.sweep()
    assert env.row(payload["payload_id"])["cleanup_attempts"] == 2
    assert env.row(payload["payload_id"])["next_cleanup_at"] == "2026-10-20T13:02:31.000000Z"  # 31 s later plus the second, 120 s delay
    monkeypatch.setattr(MediaPayloads, "_unlink", real)
    restarted = env.restart()
    assert restarted.recover()["deleted"] == 1  # recovery retries immediately, not on the backoff
    row = env.row(payload["payload_id"])
    assert (row["state"], row["reserved_bytes"], row["error_code"], row["cleanup_attempts"]) == ("deleted", 0, None, 2) and env.files() == []


def test_a_crash_after_the_files_are_removed_but_before_the_row_is_marked_resumes(env, monkeypatch):
    payload = stage(env, key="half")
    env.clock.advance(hours=25)
    monkeypatch.setattr(MediaPayloads, "_finish", lambda self, payload_id: False)
    env.payloads.sweep()
    assert env.row(payload["payload_id"])["state"] == "deleting"
    env.path(payload["payload_id"]).unlink()
    monkeypatch.undo()
    assert env.restart().recover()["deleted"] == 1 and env.row(payload["payload_id"])["state"] == "deleted"


# ----- retention -------------------------------------------------------------------------------------


def test_an_unattached_payload_becomes_eligible_after_24_hours_and_keeps_its_receipt_metadata(env):
    payload = stage(env, key="old", name="keep-me.pdf")
    env.clock.advance(hours=23, minutes=59, seconds=59)
    assert env.payloads.sweep() == {"stale_staging": 0, "expired": 0, "delivered": 0, "deleted": 0} and len(env.files()) == 1
    env.clock.advance(seconds=1)
    assert env.payloads.sweep() == {"stale_staging": 0, "expired": 1, "delivered": 0, "deleted": 1}
    row = env.row(payload["payload_id"])
    assert (row["state"], row["filename"], row["sha256"], row["size_bytes"], row["send_as"], row["reserved_bytes"]) == (
        "deleted", "keep-me.pdf", digest(PDF), len(PDF), "document", 0)
    assert row["deleted_at"] == "2026-10-20T12:00:00.000000Z" and env.files() == []
    with pytest.raises(DomainError) as unavailable:
        stage(env, key="old", name="keep-me.pdf")
    assert code(unavailable) == "payload_unavailable"
    with env.bridge.read() as uow:
        assert env.payloads.inspect(uow, payload["payload_id"])["state"] == "deleted"


def test_a_copy_holding_a_valid_lease_is_never_claimed_however_old_its_retention(env):
    thread, release, _ = blocked_copy(env, "leased")
    env.sql("UPDATE telegram_file_payloads SET expires_at='2020-01-01T00:00:00.000000Z', revision=revision+1")
    restarted = env.restart()
    assert env.payloads.sweep() == SWEEP_ZERO and restarted.recover() == ZERO
    assert stage(env, key="reaps")["state"] == "ready"  # a new reservation runs the reaper, which leaves a valid lease alone
    assert env.rows("request_key='leased'")[0]["state"] == "staging" and len(env.files()) == 2
    release.set()
    thread.join(10)
    assert env.rows("request_key='leased'")[0]["state"] == "ready"


@pytest.mark.parametrize("state", ["queued", "dispatching", "uncertain"])
def test_a_payload_of_an_open_delivery_is_kept_indefinitely_through_sweeps_and_recovery(env, state):
    payload, message_id, outbox_id = attached(env, key=f"open-{state}")
    if state != "queued":
        deliver(env, outbox_id, state)
    for days in (2, 400, 4000):
        env.clock.advance(days=days)
        assert env.payloads.sweep() == {"stale_staging": 0, "expired": 0, "delivered": 0, "deleted": 0}
        env.restart().recover()
    assert env.row(payload["payload_id"])["state"] == "attached" and len(env.files()) == 1
    with env.payloads.open_payload(payload["payload_id"]) as opened:
        assert opened.stream.read() == PDF
    connection = sqlite3.connect(env.database.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="keeps its file payload"):
            connection.execute("UPDATE telegram_file_payloads SET state='deleting', cleanup_reason='delivered', revision=revision+1 "
                               "WHERE id=?", (payload["payload_id"],))
    finally:
        connection.close()


@pytest.mark.parametrize("state", ["sent", "failed", "cancelled"])
def test_a_terminal_delivery_releases_its_payload_24_hours_after_the_terminal_time(env, state):
    payload, _, outbox_id = attached(env, key=f"term-{state}")
    deliver(env, outbox_id, state, at=env.bridge.now())
    env.clock.advance(hours=23, minutes=59)
    assert env.payloads.sweep()["delivered"] == 0 and env.row(payload["payload_id"])["state"] == "attached"
    env.clock.advance(minutes=1)
    assert env.payloads.sweep() == {"stale_staging": 0, "expired": 0, "delivered": 1, "deleted": 1}
    row = env.row(payload["payload_id"])
    assert (row["state"], row["cleanup_reason"], row["filename"], row["sha256"], row["message_id"], row["outbox_id"]) == (
        "deleted", "delivered", "a.pdf", digest(PDF), row["message_id"], outbox_id) and row["message_id"] and env.files() == []


def test_retention_counts_from_the_terminal_time_not_from_creation(env):
    payload, _, outbox_id = attached(env, key="late")
    env.clock.advance(days=10)
    assert env.payloads.sweep()["delivered"] == 0  # still queued after ten days
    deliver(env, outbox_id, "sent")
    env.clock.advance(hours=23)
    assert env.payloads.sweep()["delivered"] == 0
    env.clock.advance(hours=1)
    assert env.payloads.sweep()["delivered"] == 1


def test_a_sweep_claims_in_bounded_batches(env):
    env.payloads.CLAIM_BATCH = 2
    for index in range(5):
        stage(env, key=f"batch{index}")
    env.clock.advance(hours=25)
    assert env.payloads.sweep()["expired"] == 2 and len(env.rows("state='deleted'")) == 2
    env.payloads.sweep()
    env.payloads.sweep()
    assert len(env.rows("state='deleted'")) == 5 and env.files() == []


def test_a_payload_claimed_for_deletion_can_no_longer_attach_and_an_attached_one_cannot_be_claimed(env):
    old = stage(env, key="expired-first")
    env.clock.advance(hours=25)
    message_id, outbox_id = queue(env, old)
    env.payloads.sweep()
    with pytest.raises(DomainError) as refused:
        attach(env, old, message_id, outbox_id)
    assert code(refused) == "payload_unavailable" and env.row(old["payload_id"])["state"] == "deleted"
    winner = stage(env, key="attached-first")
    message_id, outbox_id = queue(env, winner)
    attach(env, winner, message_id, outbox_id)
    env.clock.advance(hours=48)
    assert env.payloads.sweep()["expired"] == 0 and env.row(winner["payload_id"])["state"] == "attached"


def test_sweeping_and_attaching_concurrently_never_leave_an_attached_payload_without_bytes(env):
    payloads = []
    for index in range(12):
        payload = stage(env, key=f"race{index}")
        payloads.append((payload, *queue(env, payload)))
    env.clock.advance(hours=25)
    barrier, outcomes = threading.Barrier(2), {}

    def sweeper():
        barrier.wait()
        env.payloads.sweep()

    def attacher():
        barrier.wait()
        for payload, message_id, outbox_id in payloads:
            try:
                attach(env, payload, message_id, outbox_id)
                outcomes[payload["payload_id"]] = "attached"
            except DomainError as exc:
                outcomes[payload["payload_id"]] = exc.code

    threads = [threading.Thread(target=sweeper), threading.Thread(target=attacher)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    for payload, _, _ in payloads:
        row, outcome = env.row(payload["payload_id"]), outcomes[payload["payload_id"]]
        if outcome == "attached":
            assert row["state"] == "attached" and env.path(payload["payload_id"]).exists()
        else:
            assert outcome == "payload_unavailable" and row["state"] in ("deleting", "deleted")
    assert env.payloads.sweep()["delivered"] == 0
    for payload, _, outbox_id in payloads:
        if outcomes[payload["payload_id"]] == "attached":
            assert env.path(payload["payload_id"]).exists() and env.row(payload["payload_id"])["state"] == "attached"


# ----- status -----------------------------------------------------------------------------------------


def test_status_exposes_bounded_counts_and_bytes_only(env):
    stage(env, key="s1", name="secret-name.pdf")
    payload, _, outbox_id = attached(env, key="s2", name="another-secret.pdf")
    status = env.payloads.status()
    assert status == {
        "payloads": {"staging": 0, "ready": 1, "attached": 1, "deleting": 0, "deleted": 0}, "reserved_bytes": 2 * len(PDF),
        "limit_bytes": 1 << 30, "pending_deliveries": 2, "limit_pending": 50, "active_copies": 0, "limit_copies": 2,
        "cleanup_errors": 0, "cleanup_attempts": 0, "unavailable_snapshots": 0, "last_error_code": None}
    text = json.dumps(status)
    for row in env.rows():
        assert row["storage_name"] not in text and row["filename"] not in text and row["id"] not in text
    with env.bridge.read() as uow:
        assert env.payloads.status(uow) == status
