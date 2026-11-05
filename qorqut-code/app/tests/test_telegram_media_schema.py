"""The ``telegram_file_payloads`` storage contribution: constraints, triggers, and the ``photo`` kind of ``telegram_outbox``.

Everything runs on a real composed target store created in a temporary directory. No bridge, bytes or network are involved:
these checks pin what the schema alone guarantees (frozen identity and sealed content, one attachment, CAS revisions, the
state machine, the open-delivery guard)."""

import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from bazarlyk.composition import schema_ddl
from bazarlyk.composition.assembly import HostComposition, host_assembly, host_contributions
from bazarlyk.service.changes import ChangeFeed
from bazarlyk.telegram import media_schema
from qorqut_operations.contracts import new_id
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.storage.database import Database

NOW = "2026-10-19T12:00:00.000000Z"
HEX = "a" * 64
OTHER_HEX = "b" * 64


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """One composed target store, created once; every case works on its own copy."""
    directory = tmp_path_factory.mktemp("media-schema")
    database = Database(directory / "ops" / "db.sqlite3", assembly=host_assembly(ChangeFeed())).initialize_target()
    owner, agent, other = new_id(), new_id(), new_id()
    with database.transaction() as uow:
        for identity, kind in ((owner, "human"), (agent, "agent"), (other, "agent")):
            uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": kind})
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": owner})
    database.close()
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    return SimpleNamespace(database=database, agent=agent, other=other)


@pytest.fixture
def env(template, tmp_path):
    copy = tmp_path / "copy.sqlite3"
    shutil.copyfile(template.database.path, copy)
    connection = sqlite3.connect(copy)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    yield SimpleNamespace(database=template.database, connection=connection, agent=template.agent, other=template.other,
                          installation=new_id())
    connection.close()


def row(env, **overrides):
    values = {
        "id": new_id().replace("-", ""), "installation_id": env.installation, "principal_id": env.agent,
        "operation_name": "telegram_send_file", "request_key": new_id(), "arguments_hash": HEX,
        "source_binding_json": json.dumps({"kind": "workspace"}), "expected_sha256": HEX, "filename": "report.pdf",
        "send_as": "document", "storage_name": new_id().replace("-", ""), "max_bytes": 50_000_000,
        "state": "staging", "lease_owner": "c" * 32,
        "lease_expires_at": "2026-10-19T12:05:00.000000Z", "expires_at": "2026-10-20T12:00:00.000000Z",
        "created_at": NOW, "updated_at": NOW}
    values.update(overrides)
    values.setdefault("reserved_bytes", values["max_bytes"])  # a staging copy reserves its whole ceiling
    return values


def insert(env, **overrides):
    values = row(env, **overrides)
    env.connection.execute(f"INSERT INTO telegram_file_payloads({','.join(values)}) VALUES({','.join('?' * len(values))})",
                           list(values.values()))
    return values


SEAL = dict(state="ready", lease_owner=None, lease_expires_at=None, size_bytes=5, sha256=HEX, sealed_at=NOW,
            metadata_json=json.dumps({"format": "pdf"}), reserved_bytes=5)


def ready(env, **overrides):
    """A payload as the service creates one: inserted staging, then sealed by one update."""
    sealing = {name: overrides.pop(name) for name in list(overrides) if name in SEAL}
    payload = insert(env, **overrides)
    update(env, payload, **{**SEAL, **sealing})
    return {**payload, **SEAL, **sealing}


def update(env, payload, **changes):
    changes.setdefault("revision", None)
    current = env.connection.execute("SELECT revision FROM telegram_file_payloads WHERE id=?", (payload["id"],)).fetchone()[0]
    if changes["revision"] is None:
        changes["revision"] = current + 1
    sets = ",".join(f"{name}=?" for name in changes)
    env.connection.execute(f"UPDATE telegram_file_payloads SET {sets} WHERE id=?", [*changes.values(), payload["id"]])


def get(env, payload):
    return dict(env.connection.execute("SELECT * FROM telegram_file_payloads WHERE id=?", (payload["id"],)).fetchone())


def send(env, payload, *, agent=None, kind=None, source_kind="agent_file", state="queued", direction="outbound",
         message_agent=None, envelope=None, message_outbox=None):
    """A message and the outbox row of its send, in the shape the payload attaches to. ``envelope`` is the outbox payload
    text (default: the exact closed envelope); ``message_outbox`` points the message at another delivery."""
    message_id, outbox_id = new_id(), new_id()
    c = env.connection
    c.execute("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,message_thread_id,agent_id,purpose,body,outbox_id,"
              "occurred_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (message_id, direction, "ordubashy", -1001, 11, message_agent or agent or env.agent, "update", "caption",
               message_outbox, NOW, NOW, NOW))
    if envelope is None:
        envelope = json.dumps({"version": 1, "payload_id": payload["id"]})
    c.execute("INSERT INTO telegram_outbox(id,chat_name,chat_id,message_thread_id,kind,source_kind,source_key,message_id,payload_json,"
              "state,binding_fingerprint,config_generation,agent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (outbox_id, "ordubashy", -1001, 11, kind or payload["send_as"], source_kind, new_id(), message_id,
               envelope, "queued", "f" * 64, 1, agent or env.agent, NOW, NOW))
    if state != "queued":
        walk(env, outbox_id, state)
    return message_id, outbox_id


def walk(env, outbox_id, state):
    steps = {"dispatching": ["dispatching"], "uncertain": ["dispatching", "uncertain"], "sent": ["dispatching", "sent"],
             "failed": ["failed"], "cancelled": ["cancelled"]}[state]
    for step in steps:
        extra = {"dispatching": ", dispatch_started_at='%s'" % NOW, "uncertain": "",
                 "sent": ", provider_message_id=900, sent_at='%s'" % NOW, "failed": "", "cancelled": ""}[step]
        env.connection.execute(f"UPDATE telegram_outbox SET state=?, revision=revision+1{extra} WHERE id=?", (step, outbox_id))


def attach(env, payload, message_id, outbox_id):
    update(env, payload, state="attached", message_id=message_id, outbox_id=outbox_id, attached_at=NOW)


def refused(env, statement, *parameters, match=None):
    with pytest.raises(sqlite3.IntegrityError, match=match):
        env.connection.execute(statement, parameters)


# ----- composition ----------------------------------------------------------------------------------------


def test_the_contribution_is_registered_once_beside_the_telegram_tables_and_extends_the_target(env):
    assembly = env.database.assembly
    assert media_schema.TABLE in assembly.tables and assembly.tables[media_schema.TABLE].keys == ("id",)
    assert assembly.immutable_fields[media_schema.TABLE] == media_schema.IMMUTABLE_FIELDS[media_schema.TABLE]
    owners = [owner for owner, statements in assembly.schema if any(media_schema.TABLE in statement for statement in statements)]
    assert owners == ["telegram-media"]
    # The generated telegram tables are untouched and listed under their own owner, not this one.
    assert media_schema.TABLE not in schema_ddl.OWNER_TABLES["telegram"]
    assert "telegram_outbox" in schema_ddl.OWNER_TABLES["telegram"]
    without = HostComposition()
    for contribution in host_contributions(ChangeFeed()):
        if getattr(contribution, "owner", None) != "telegram-media":
            without.add(contribution)
    assert without.freeze().target.digest != assembly.target.digest
    with pytest.raises(AssemblyError, match="Duplicate host contribution"):
        composition = HostComposition()
        for contribution in host_contributions(ChangeFeed()):
            composition.add(contribution)
        composition.add(media_schema.storage_contribution())


def test_the_composed_store_reopens_with_the_same_identity_and_holds_every_object(env):
    reopened = Database(env.database.path, assembly=host_assembly(ChangeFeed())).open_existing()
    assert reopened.assembly.target == env.database.assembly.target
    objects = {name for (name,) in env.connection.execute("SELECT name FROM sqlite_master WHERE tbl_name='telegram_file_payloads'")}
    assert {"telegram_file_payloads", "telegram_file_payloads_state", "telegram_file_payloads_message", "telegram_file_payloads_outbox",
            "telegram_file_payloads_identity", "telegram_file_payloads_revision", "telegram_file_payloads_attach",
            "telegram_file_payloads_insert", "telegram_file_payloads_associate",
            "telegram_file_payloads_open_delivery", "telegram_file_payloads_no_delete"} <= objects


def test_the_table_is_strict_and_holds_no_binary_column(env):
    definition = env.connection.execute("SELECT sql FROM sqlite_master WHERE name='telegram_file_payloads'").fetchone()[0]
    assert definition.rstrip().endswith("STRICT")
    types = {name: kind for _, name, kind, *_ in env.connection.execute("PRAGMA table_info(telegram_file_payloads)")}
    assert set(types.values()) <= {"TEXT", "INTEGER"} and not any("BLOB" in kind for kind in types.values())


def test_the_outbox_accepts_photo_and_still_refuses_other_kinds_with_its_old_guards(env):
    payload = ready(env)
    message_id, outbox_id = send(env, payload, kind="photo", source_kind="agent_file")
    assert env.connection.execute("SELECT kind FROM telegram_outbox WHERE id=?", (outbox_id,)).fetchone()[0] == "photo"
    for kind in ("text", "document", "edit"):
        send(env, payload, kind=kind, source_kind="other")
    with pytest.raises(sqlite3.IntegrityError):
        send(env, payload, kind="video")
    # The v1 triggers and indexes on the table are all still present and enforcing.
    names = {name for (name,) in env.connection.execute("SELECT name FROM sqlite_master WHERE tbl_name='telegram_outbox'")}
    assert {"telegram_outbox_state", "telegram_outbox_no_delete", "telegram_outbox_identity", "telegram_outbox_due",
            "telegram_outbox_chat", "telegram_outbox_message", "telegram_outbox_event", "telegram_outbox_destination"} <= names
    refused(env, "UPDATE telegram_outbox SET state='sent', revision=revision+1 WHERE id=?", outbox_id, match="Invalid Telegram outbox")
    refused(env, "UPDATE telegram_outbox SET kind='document', revision=revision+1 WHERE id=?", outbox_id, match="immutable")
    refused(env, "DELETE FROM telegram_outbox WHERE id=?", outbox_id, match="retained")


# ----- constraints ----------------------------------------------------------------------------------------------


def test_one_request_identity_per_installation_principal_operation_and_key(env):
    first = insert(env)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(env, request_key=first["request_key"])
    insert(env, request_key=first["request_key"], principal_id=env.other)
    insert(env, request_key=first["request_key"], installation_id=new_id())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(env, request_key=new_id(), storage_name=first["storage_name"])


@pytest.mark.parametrize("overrides", [
    {"id": "A" * 32}, {"id": "a" * 31}, {"installation_id": "short"}, {"operation_name": "telegram_send"},
    {"request_key": ""}, {"request_key": "k" * 201}, {"arguments_hash": "A" * 64}, {"arguments_hash": "a" * 63},
    {"source_binding_json": "[]"}, {"source_binding_json": "not json"}, {"source_binding_json": json.dumps({"x": "y" * 9000})},
    {"expected_sha256": "g" * 64}, {"filename": ""}, {"filename": "é" * 128}, {"send_as": "video"},
    {"storage_name": "z" * 32}, {"max_bytes": 0}, {"max_bytes": 50_000_001}, {"send_as": "photo", "max_bytes": 10_000_001, "reserved_bytes": 10_000_001},
    {"reserved_bytes": -1}, {"state": "failed"}, {"principal_id": "missing-principal"},
    # A staging copy needs its lease, its full reservation and no sealed or delivery facts.
    {"lease_owner": None}, {"lease_expires_at": None}, {"expires_at": None}, {"reserved_bytes": 10},
    {"sealed_at": NOW}, {"size_bytes": 5}, {"message_id": "x"}, {"lease_owner": "short"},
    {"failure_code": "x", "failure_message": "y"}, {"error_code": "x"}, {"deleted_at": NOW}])
def test_invalid_rows_are_refused(env, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        insert(env, **overrides)


def test_the_filename_limit_counts_utf8_bytes_not_characters(env):
    insert(env, filename="é" * 127 + "a")  # exactly 255 bytes
    with pytest.raises(sqlite3.IntegrityError):
        insert(env, filename="é" * 128)


def test_sealed_facts_are_all_or_nothing_and_must_equal_the_expected_hash_and_fit_the_reservation(env):
    for overrides in ({"sha256": OTHER_HEX}, {"size_bytes": 0}, {"size_bytes": 51_000_000}, {"reserved_bytes": 6},
                      {"metadata_json": "[]"}, {"metadata_json": json.dumps({"x": "y" * 5000})}, {"sealed_at": None},
                      {"metadata_json": None}, {"size_bytes": None}, {"sha256": None}, {"lease_owner": "c" * 32}):
        with pytest.raises(sqlite3.IntegrityError):
            ready(env, **overrides)
    with pytest.raises(sqlite3.IntegrityError):  # larger than the photo reservation ceiling
        ready(env, send_as="photo", max_bytes=10_000_000, size_bytes=10_000_001, reserved_bytes=10_000_001)
    ready(env)


def test_failure_facts_belong_to_unsealed_deleted_attempts(env):
    failed = insert(env)
    update(env, failed, state="deleting", failure_code="hash_mismatch", failure_message="no", cleanup_reason="failed",
           lease_owner=None, lease_expires_at=None)
    with pytest.raises(sqlite3.IntegrityError):  # a failure with no cleanup claim reason
        update(env, failed, failure_code="other", failure_message="x", cleanup_reason=None)
    with pytest.raises(sqlite3.IntegrityError, match="keeps its request"):
        update(env, failed, failure_code="other")
    with pytest.raises(sqlite3.IntegrityError):
        ready(env, failure_code="x", failure_message="y", cleanup_reason="failed")


# ----- immutability and CAS -------------------------------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"id": "d" * 32}, {"installation_id": "e" * 36}, {"principal_id": "other"}, {"operation_name": "x"},
    {"request_key": "changed"}, {"arguments_hash": OTHER_HEX}, {"source_binding_json": json.dumps({"kind": "document"})},
    {"expected_sha256": OTHER_HEX}, {"filename": "other.pdf"}, {"send_as": "photo", "max_bytes": 10_000_000},
    {"storage_name": "f" * 32}, {"max_bytes": 1}, {"created_at": "2026-01-01T00:00:00.000000Z"}])
def test_request_manifest_and_storage_identity_never_change(env, change):
    payload = insert(env)
    if "principal_id" in change:
        change = {"principal_id": env.other}
    with pytest.raises(sqlite3.IntegrityError, match="keeps its request|constraint|CHECK"):
        update(env, payload, **change)
    assert get(env, payload)["request_key"] == payload["request_key"]


def test_sealed_content_and_association_never_change_after_they_are_set(env):
    payload = ready(env)
    for change in ({"size_bytes": 6, "reserved_bytes": 6}, {"sha256": OTHER_HEX}, {"metadata_json": json.dumps({"format": "x"})},
                   {"sealed_at": "2026-10-19T13:00:00.000000Z"}):
        with pytest.raises(sqlite3.IntegrityError):
            update(env, payload, **change)
    message_id, outbox_id = send(env, payload)
    attach(env, payload, message_id, outbox_id)
    other_message, other_outbox = send(env, ready(env))
    for change in ({"message_id": other_message}, {"outbox_id": other_outbox}, {"attached_at": "2026-10-19T13:00:00.000000Z"}):
        with pytest.raises(sqlite3.IntegrityError):
            update(env, payload, **change)


def test_every_update_raises_the_revision_by_exactly_one(env):
    payload = insert(env)
    for revision in (1, 3, 0, 99):
        with pytest.raises(sqlite3.IntegrityError, match="compare-and-swap"):
            update(env, payload, revision=revision, error_code="x", error_message="y", error_at=NOW)
    update(env, payload, error_code="x", error_message="y", error_at=NOW)
    assert get(env, payload)["revision"] == 2


def test_rows_are_retained(env):
    payload = insert(env)
    refused(env, "DELETE FROM telegram_file_payloads WHERE id=?", payload["id"], match="retained")


# ----- the state machine -------------------------------------------------------------------------------


def test_only_the_lifecycle_transitions_are_allowed(env):
    allowed = {"staging": {"ready", "deleting"}, "ready": {"attached", "deleting"}, "attached": {"deleting"}, "deleting": {"deleted"}}
    sealed = dict(size_bytes=5, sha256=HEX, sealed_at=NOW, metadata_json=json.dumps({}), reserved_bytes=5, lease_owner=None,
                  lease_expires_at=None)
    for origin, targets in allowed.items():
        for target in ("staging", "ready", "attached", "deleting", "deleted"):
            if target == origin:
                continue
            payload = insert(env) if origin == "staging" else ready(env)
            if origin in ("attached", "deleting"):
                message_id, outbox_id = send(env, payload)
                attach(env, payload, message_id, outbox_id)
                walk(env, outbox_id, "cancelled")  # a terminal delivery, so leaving ``attached`` is only a question of state
                if origin == "deleting":
                    update(env, payload, state="deleting", cleanup_reason="delivered")
            changes = {"state": target}
            if target == "ready":
                changes.update(sealed)
            elif target == "attached":
                message_id, outbox_id = send(env, payload)
                changes.update(message_id=message_id, outbox_id=outbox_id, attached_at=NOW)
            elif target == "deleting":
                changes.update(cleanup_reason="expired", lease_owner=None, lease_expires_at=None)
            elif target == "deleted":
                changes.update(reserved_bytes=0, deleted_at=NOW, cleanup_reason="expired", lease_owner=None, lease_expires_at=None)
            if target in targets:
                update(env, payload, **changes)
                assert get(env, payload)["state"] == target
            else:
                with pytest.raises(sqlite3.IntegrityError):
                    update(env, payload, **changes)
                assert get(env, payload)["state"] == origin


def test_a_deleted_payload_stays_deleted_and_keeps_its_receipt_metadata(env):
    payload = ready(env)
    update(env, payload, state="deleting", cleanup_reason="expired")
    update(env, payload, state="deleted", reserved_bytes=0, deleted_at=NOW)
    final = get(env, payload)
    assert (final["filename"], final["sha256"], final["size_bytes"], final["send_as"]) == ("report.pdf", HEX, 5, "document")
    with pytest.raises(sqlite3.IntegrityError):
        update(env, payload, state="deleting")
    with pytest.raises(sqlite3.IntegrityError):
        update(env, payload, state="ready")


# ----- association ---------------------------------------------------------------------------------------


def test_a_payload_attaches_to_one_queued_agent_file_send_of_its_own_agent_and_kind(env):
    payload = ready(env)
    message_id, outbox_id = send(env, payload)
    attach(env, payload, message_id, outbox_id)
    assert get(env, payload)["state"] == "attached"


@pytest.mark.parametrize("arrange", [
    lambda env, payload: send(env, payload, kind="photo"),                      # wrong delivery kind for a document payload
    lambda env, payload: send(env, payload, source_kind="notify"),              # not an agent_file send
    lambda env, payload: send(env, payload, agent=env.other),                   # another agent's send
    lambda env, payload: send(env, payload, state="dispatching"),               # already past queued
    lambda env, payload: send(env, payload, state="sent"),
    lambda env, payload: (new_id(), new_id()),                                  # no such rows
], ids=["kind", "source", "agent", "dispatching", "sent", "missing"])
def test_attachment_to_a_mismatched_or_non_queued_delivery_is_refused(env, arrange):
    payload = ready(env)
    arranged = arrange(env, payload)
    with pytest.raises(sqlite3.IntegrityError):
        attach(env, payload, *arranged)
    assert get(env, payload)["state"] == "ready"


GUARD = "attaches only to the queued agent file send of its own message"


@pytest.mark.parametrize("envelope", [
    lambda pid: json.dumps({"version": 1, "payload_id": "0" * 32}),                    # another payload
    lambda pid: json.dumps({"version": 1, "payload_id": pid.upper()}),                 # not the exact id
    lambda pid: json.dumps({"version": 1, "payload_id": pid, "path": "/etc/passwd"}),  # an extra field
    lambda pid: json.dumps({"version": 1, "payload_id": pid, "version2": 1}),
    lambda pid: json.dumps({"payload_id": pid}),                                       # a missing field
    lambda pid: json.dumps({"version": 1}),
    lambda pid: json.dumps({"version": 2, "payload_id": pid}),                         # a different version
    lambda pid: json.dumps({"version": 0, "payload_id": pid}),
    lambda pid: json.dumps({"version": "1", "payload_id": pid}),                       # text, not an integer
    lambda pid: json.dumps({"version": 1.0, "payload_id": pid}),                       # a real number, not an integer
    lambda pid: '{"version": 1.0, "payload_id": "%s"}' % pid,
    lambda pid: json.dumps({"version": True, "payload_id": pid}),                      # JSON true is not 1
    lambda pid: json.dumps({"version": None, "payload_id": pid}),
    lambda pid: json.dumps({"version": [1], "payload_id": pid}),
    lambda pid: json.dumps({"version": 1, "payload_id": None}),
    lambda pid: json.dumps({"version": 1, "payload_id": 5}),                           # not text
    lambda pid: json.dumps({"version": 1, "payload_id": [pid]}),
    lambda pid: json.dumps({"version": 1, "payload_id": {"id": pid}}),
    lambda pid: '{"version": 1, "version": 1, "payload_id": "%s"}' % pid,              # a repeated key
    lambda pid: '{"version": 1, "payload_id": "%s", "payload_id": "%s"}' % (pid, pid),
    lambda pid: json.dumps([1, pid]),                                                  # not an object
    lambda pid: json.dumps([{"version": 1, "payload_id": pid}]),
    lambda pid: json.dumps(pid),
    lambda pid: json.dumps(1),
    lambda pid: json.dumps(None),
    lambda pid: "{}",
    lambda pid: json.dumps({"envelope": {"version": 1, "payload_id": pid}}),
])
def test_the_storage_guard_refuses_any_outbox_envelope_that_is_not_exactly_this_payloads(env, envelope):
    payload = ready(env)
    message_id, outbox_id = send(env, payload, envelope=envelope(payload["id"]))
    with pytest.raises(sqlite3.IntegrityError, match=GUARD):
        attach(env, payload, message_id, outbox_id)
    assert get(env, payload)["state"] == "ready" and get(env, payload)["message_id"] is None


def test_the_storage_guard_accepts_the_closed_envelope_in_any_key_order_and_spacing(env):
    for text in ('{"payload_id":"%s","version":1}', '{ "version" : 1 ,\n "payload_id" : "%s" }', '{"version":1,"payload_id":"%s"}'):
        payload = ready(env)
        message_id, outbox_id = send(env, payload, envelope=text % payload["id"])
        attach(env, payload, message_id, outbox_id)
        assert get(env, payload)["state"] == "attached"


@pytest.mark.parametrize("arrange", [
    lambda env, payload: send(env, payload, direction="inbound"),                      # not an outbound message
    lambda env, payload: send(env, payload, message_agent=env.other),                  # the message is another agent's
    lambda env, payload: send(env, payload, message_outbox=new_id()),                  # the message points at another delivery
], ids=["inbound-message", "message-agent", "message-outbox"])
def test_the_storage_guard_requires_the_outbound_message_of_the_same_principal_and_delivery(env, arrange):
    payload = ready(env)
    message_id, outbox_id = arrange(env, payload)
    with pytest.raises(sqlite3.IntegrityError, match=GUARD):
        attach(env, payload, message_id, outbox_id)
    assert get(env, payload)["state"] == "ready"


def test_the_storage_guard_requires_the_delivery_to_belong_to_the_named_message(env):
    payload = ready(env)
    message_id, outbox_id = send(env, payload)
    other_message, _ = send(env, payload)
    with pytest.raises(sqlite3.IntegrityError, match=GUARD):
        attach(env, payload, other_message, outbox_id)
    with pytest.raises(sqlite3.IntegrityError, match=GUARD):
        attach(env, payload, message_id, new_id())
    attach(env, payload, message_id, outbox_id)


def test_a_message_already_pointing_at_the_same_delivery_may_attach(env):
    payload = ready(env)
    message_id, outbox_id = send(env, payload)
    env.connection.execute("UPDATE telegram_messages SET outbox_id=?, revision=revision+1 WHERE id=?", (outbox_id, message_id))
    attach(env, payload, message_id, outbox_id)
    assert get(env, payload)["state"] == "attached"


# ----- the association exists only through the one ready -> attached transition -----------------------------------------------


def facts(state, env):
    """Every column a directly inserted row of ``state`` needs to satisfy the table's own CHECKs, so only the triggers decide."""
    sealed = dict(size_bytes=5, sha256=HEX, sealed_at=NOW, metadata_json=json.dumps({"format": "pdf"}), reserved_bytes=5,
                  lease_owner=None, lease_expires_at=None)
    return {"ready": sealed, "attached": sealed, "deleting": {**sealed, "cleanup_reason": "expired"},
            "deleted": {**sealed, "cleanup_reason": "expired", "reserved_bytes": 0, "deleted_at": NOW}}[state]


@pytest.mark.parametrize("state", ["ready", "attached", "deleting", "deleted"])
def test_a_new_row_can_only_start_in_staging(env, state):
    pid = new_id().replace("-", "")
    values = facts(state, env)
    if state == "attached":
        message_id, outbox_id = send(env, {"id": pid, "send_as": "document"})
        values.update(message_id=message_id, outbox_id=outbox_id, attached_at=NOW)
    # The row is otherwise complete and valid: the same facts held by an update would satisfy every CHECK ...
    with pytest.raises(sqlite3.IntegrityError, match="starts in staging"):
        insert(env, id=pid, state=state, **values)
    assert env.connection.execute("SELECT count(*) FROM telegram_file_payloads WHERE id=?", (pid,)).fetchone()[0] == 0
    # ... a staging row is accepted ...
    insert(env, id=new_id().replace("-", ""))
    # ... and without the trigger the very same row would have been stored, so the trigger alone refused it.
    env.connection.execute("DROP TRIGGER telegram_file_payloads_insert")
    assert insert(env, id=pid, state=state, **values)["state"] == state


@pytest.mark.parametrize("origin", ["staging", "ready", "deleting"])
def test_an_association_cannot_be_installed_by_any_transition_but_ready_to_attached(env, origin):
    payload = ready(env) if origin != "staging" else insert(env)
    if origin == "deleting":
        update(env, payload, state="deleting", cleanup_reason="expired")
    message_id, outbox_id = send(env, payload)
    target = {"staging": "deleting", "ready": "deleting", "deleting": "deleted"}[origin]
    changes = dict(state=target, message_id=message_id, outbox_id=outbox_id, attached_at=NOW, cleanup_reason="expired")
    if origin == "staging":
        changes.update(lease_owner=None, lease_expires_at=None)
    if target == "deleted":
        changes.update(reserved_bytes=0, deleted_at=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="associated with its delivery only when a ready payload attaches"):
        update(env, payload, **changes)
    assert get(env, payload)["state"] == origin and get(env, payload)["message_id"] is None
    # Without the trigger the same update would have been stored, so the trigger alone refused it.
    env.connection.execute("DROP TRIGGER telegram_file_payloads_associate")
    update(env, payload, **changes)
    assert (get(env, payload)["state"], get(env, payload)["outbox_id"]) == (target, outbox_id)


def test_an_association_cannot_be_added_without_a_state_change(env):
    payload = ready(env)
    message_id, outbox_id = send(env, payload)
    for changes in ({"message_id": message_id}, {"outbox_id": outbox_id}, {"attached_at": NOW},
                    {"message_id": message_id, "outbox_id": outbox_id, "attached_at": NOW}):
        with pytest.raises(sqlite3.IntegrityError):
            update(env, payload, **changes)
    assert get(env, payload)["state"] == "ready" and get(env, payload)["message_id"] is None
    attach(env, payload, message_id, outbox_id)  # the one legitimate way still works


def test_a_message_or_a_send_belongs_to_at_most_one_payload(env):
    first, second = ready(env), ready(env)
    message_id, outbox_id = send(env, first)
    attach(env, first, message_id, outbox_id)
    # The envelope of that send names the first payload only, so the guard already refuses the second one ...
    with pytest.raises(sqlite3.IntegrityError, match="attaches only to the queued agent file send"):
        attach(env, second, message_id, outbox_id)
    other_message, _ = send(env, second)
    with pytest.raises(sqlite3.IntegrityError):  # ... and the same send again, under another message
        attach(env, second, other_message, outbox_id)
    # ... and independently of that guard the partial unique indexes keep one payload per message and per send.
    env.connection.execute("DROP TRIGGER telegram_file_payloads_attach")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        attach(env, second, message_id, outbox_id)
    third_message, third_outbox = send(env, second)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*message_id"):
        attach(env, second, message_id, third_outbox)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*outbox_id"):
        attach(env, second, third_message, outbox_id)


def test_an_open_delivery_keeps_its_payload_and_a_terminal_one_releases_it(env):
    for state in ("queued", "dispatching", "uncertain"):
        payload = ready(env)
        message_id, outbox_id = send(env, payload)
        attach(env, payload, message_id, outbox_id)
        if state != "queued":
            walk(env, outbox_id, state)
        with pytest.raises(sqlite3.IntegrityError, match="keeps its file payload"):
            update(env, payload, state="deleting", cleanup_reason="delivered")
        assert get(env, payload)["state"] == "attached"
    for state in ("sent", "failed", "cancelled"):
        payload = ready(env)
        message_id, outbox_id = send(env, payload)
        attach(env, payload, message_id, outbox_id)
        walk(env, outbox_id, state)
        update(env, payload, state="deleting", cleanup_reason="delivered")
        assert get(env, payload)["state"] == "deleting"
