"""Migration 106: the constraints that keep Telegram cursors, receipts and redactions honest."""
import sqlite3

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.storage import TABLES, Database
from foundation_support import HUMAN, database_at

NOW = "2026-10-01T12:00:00.000000Z"
FP = "f" * 64
INSTALLATION = "00000000-0000-4000-8000-0000000000b1"
FROZEN = {"owner_principal_id": HUMAN, "config_generation": 1, "binding_fingerprint": FP, "target_fingerprint": FP}
FROZEN_COLUMNS = ",".join(FROZEN)
FROZEN_VALUES = "'%s',1,'%s','%s'" % (HUMAN, FP, FP)
TELEGRAM_TABLES = ("telegram_state", "telegram_updates", "telegram_messages", "telegram_outbox", "telegram_events",
                   "telegram_signoff_posts", "telegram_owner_inputs", "telegram_cleanup_jobs")


@pytest.fixture
def conn(tmp_path):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": (agent := new_id()), "kind": "agent", "display_name": "A"})
    connection = database.dedicated_connection()
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()
    database.close()


def agent_id(conn):
    return conn.execute("SELECT id FROM principals WHERE kind='agent'").fetchone()[0]


def refused(conn, sql, *parameters):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, parameters)


def state(conn, **overrides):
    values = {"id": "telegram", "bot_id": 7, "installation_id": INSTALLATION, "owner_principal_id": HUMAN,
              "owner_telegram_user_id": 9, "config_generation": 1, "binding_fingerprint": FP, "binding_effective_at": NOW,
              "activation_at": NOW, "poll_offset": 10, "created_at": NOW, "updated_at": NOW, **overrides}
    conn.execute(f"INSERT INTO telegram_state({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def message(conn, **overrides):
    values = {"id": new_id(), "direction": "outbound", "chat_name": "founder", "chat_id": -5, "purpose": "reply", "body": "hello",
              "occurred_at": NOW, "created_at": NOW, "updated_at": NOW, **overrides}
    conn.execute(f"INSERT INTO telegram_messages({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
    return values["id"]


def outbox(conn, **overrides):
    values = {"id": new_id(), "chat_name": "founder", "chat_id": -5, "kind": "text", "source_kind": "agent", "source_key": new_id(),
              "payload_json": "{}", "binding_fingerprint": FP, "config_generation": 1, "created_at": NOW, "updated_at": NOW, **overrides}
    conn.execute(f"INSERT INTO telegram_outbox({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
    return values["id"]


def test_every_new_table_is_registered_with_repository_metadata(tmp_path):
    database = database_at(tmp_path)
    for table in TELEGRAM_TABLES:
        assert table in TABLES and database.columns(table)
    assert TABLES["telegram_updates"].keys == ("bot_id", "update_id")
    assert Database.migrations()[-1][0] >= 106


def test_state_is_a_singleton_whose_cursor_bot_and_cutoff_never_move_back(conn):
    state(conn)
    refused(conn, "INSERT INTO telegram_state(id,bot_id,installation_id,owner_principal_id,owner_telegram_user_id,config_generation,"
                  "binding_fingerprint,binding_effective_at,activation_at,created_at,updated_at) VALUES ('other',1,?,?,1,1,?,?,?,?,?)",
            INSTALLATION, HUMAN, FP, NOW, NOW, NOW, NOW)
    conn.execute("UPDATE telegram_state SET poll_offset=11")
    for sql in ("UPDATE telegram_state SET poll_offset=5", "UPDATE telegram_state SET bot_id=8",
                "UPDATE telegram_state SET activation_at='2026-10-02T00:00:00.000000Z'",
                "UPDATE telegram_state SET config_generation=0", "DELETE FROM telegram_state",
                "UPDATE telegram_state SET installation_id='00000000-0000-4000-8000-0000000000b2'",
                "UPDATE telegram_state SET binding_effective_at='2026-10-01T11:00:00.000000Z'"):
        refused(conn, sql)
    # An owner repin and a later binding cutoff are the moves the configuration edit may make.
    conn.execute("UPDATE telegram_state SET config_generation=2, binding_effective_at='2026-10-01T12:00:01.000000Z'")


def test_updates_are_keyed_by_bot_and_update_and_finished_updates_stay_finished(conn):
    row = lambda uid, **kw: conn.execute(
        "INSERT INTO telegram_updates(bot_id,update_id,kind,normalized_json,received_at,created_at,updated_at,state,processed_at) "
        "VALUES (7,?,?,'{}',?,?,?,?,?)", (uid, kw.get("kind", "message"), NOW, NOW, NOW, kw.get("state", "received"), kw.get("at")))
    row(1)
    refused(conn, "INSERT INTO telegram_updates(bot_id,update_id,kind,normalized_json,received_at,created_at,updated_at) "
                  "VALUES (7,1,'message','{}',?,?,?)", NOW, NOW, NOW)  # duplicate delivery keeps one row
    refused(conn, "INSERT INTO telegram_updates(bot_id,update_id,kind,normalized_json,received_at,created_at,updated_at) "
                  "VALUES (7,2,'message','not json',?,?,?)", NOW, NOW, NOW)
    refused(conn, "INSERT INTO telegram_updates(bot_id,update_id,kind,normalized_json,received_at,created_at,updated_at,state) "
                  "VALUES (7,3,'message','{}',?,?,?,'done')", NOW, NOW, NOW)  # done needs processed_at
    conn.execute("UPDATE telegram_updates SET state='done', processed_at=? WHERE update_id=1", (NOW,))
    refused(conn, "UPDATE telegram_updates SET state='failed' WHERE update_id=1")
    refused(conn, "UPDATE telegram_updates SET normalized_json='{\"x\":1}' WHERE update_id=1")
    refused(conn, "UPDATE telegram_updates SET update_id=9 WHERE update_id=1")


def test_a_sensitive_message_can_never_hold_or_regain_a_body(conn):
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,sensitive,body_redacted,occurred_at,created_at,updated_at) "
                  "VALUES (?,'inbound','founder',-5,'owner','secret',1,1,?,?,?)", new_id(), NOW, NOW, NOW)
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,sensitive,body_redacted,occurred_at,created_at,updated_at) "
                  "VALUES (?,'inbound','founder',-5,'owner',NULL,1,0,?,?,?)", new_id(), NOW, NOW, NOW)
    redacted = message(conn, direction="inbound", purpose="owner", body=None, sensitive=1, body_redacted=1)
    refused(conn, "UPDATE telegram_messages SET body='leak' WHERE id=?", redacted)
    refused(conn, "UPDATE telegram_messages SET sensitive=0 WHERE id=?", redacted)
    refused(conn, "DELETE FROM telegram_messages WHERE id=?", redacted)
    ordinary = message(conn, telegram_message_id=41)
    refused(conn, "UPDATE telegram_messages SET telegram_message_id=42 WHERE id=?", ordinary)  # a provider ID binds once
    refused(conn, "UPDATE telegram_messages SET chat_name='urgent' WHERE id=?", ordinary)
    message(conn, chat_id=-6, telegram_message_id=41)
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,telegram_message_id,occurred_at,created_at,updated_at) "
                  "VALUES (?,'inbound','founder',-5,'owner','dup',41,?,?,?)", new_id(), NOW, NOW, NOW)


def test_request_bookkeeping_is_all_or_nothing_and_one_secret_request_is_open_per_chat(conn):
    request = dict(purpose="blocking_input", request_key="sms", input_mode="secret", request_state="pending", request_expires_at=NOW)
    message(conn, **request)
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,request_key,input_mode,request_state,request_expires_at,"
                  "occurred_at,created_at,updated_at) VALUES (?,'outbound','founder',-5,'blocking_input','x','other','secret','pending',?,?,?,?)",
            new_id(), NOW, NOW, NOW, NOW)  # one outstanding secret request per chat
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,request_key,input_mode,request_state,request_expires_at,"
                  "occurred_at,created_at,updated_at) VALUES (?,'outbound','founder',-5,'blocking_input','x','sms','text','pending',?,?,?,?)",
            new_id(), NOW, NOW, NOW, NOW)  # nor a second pending key
    message(conn, chat_name="marketing", chat_id=-6, **request)  # another chat is independent
    refused(conn, "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,request_key,occurred_at,created_at,updated_at) "
                  "VALUES (?,'outbound','urgent',-9,'blocking_input','x','k',?,?,?)", new_id(), NOW, NOW, NOW)  # partial request columns


def test_outbox_intent_is_immutable_transitions_are_ordered_and_receipts_are_kept(conn):
    first = outbox(conn, source_key="k1")
    refused(conn, "INSERT INTO telegram_outbox(id,chat_name,chat_id,kind,source_kind,source_key,payload_json,binding_fingerprint,config_generation,"
                  "created_at,updated_at) VALUES (?, 'founder',-5,'text','agent','k1','{}',?,1,?,?)", new_id(), FP, NOW, NOW)
    outbox(conn, source_key="k1", part="second")  # per-part receipts share a source
    refused(conn, "UPDATE telegram_outbox SET payload_json='{\"x\":1}' WHERE id=?", first)
    refused(conn, "UPDATE telegram_outbox SET chat_id=-9 WHERE id=?", first)
    refused(conn, "UPDATE telegram_outbox SET state='sent' WHERE id=?", first)  # queued cannot jump to sent
    refused(conn, "UPDATE telegram_outbox SET state='uncertain', dispatch_started_at=NULL WHERE id=?", first)
    conn.execute("UPDATE telegram_outbox SET state='dispatching', dispatch_started_at=? WHERE id=?", (NOW, first))
    refused(conn, "UPDATE telegram_outbox SET state='sent' WHERE id=?", first)  # a receipt needs the provider ID
    conn.execute("UPDATE telegram_outbox SET state='uncertain' WHERE id=?", (first,))
    conn.execute("UPDATE telegram_outbox SET state='sent', provider_message_id=5, sent_at=? WHERE id=?", (NOW, first))
    for sql in ("UPDATE telegram_outbox SET state='queued' WHERE id=?", "UPDATE telegram_outbox SET state='failed' WHERE id=?",
                "DELETE FROM telegram_outbox WHERE id=?"):
        refused(conn, sql, first)


def test_signoff_post_bindings_reference_real_signoffs_and_index_the_current_post(conn):
    refused(conn, "INSERT INTO telegram_signoff_posts(id,signoff_id,generation,category,proposal_revision,payload_hash,"
                  "expected_status_revision,input_form,activation_at,created_at,updated_at) VALUES (?,?,1,'input_request',1,?,1,'input',?,?,?)",
            new_id(), new_id(), FP, NOW, NOW, NOW)
    indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(telegram_signoff_posts)")}
    assert indexes["telegram_signoff_posts_current"] == 1  # unique: one current post per proposal
    assert "telegram_signoff_posts_open" in indexes


def test_owner_inputs_never_replay_after_a_terminal_state_and_secrets_need_expiry(conn):
    m = message(conn, direction="inbound", purpose="owner", body="hi")
    def add(**kw):
        values = {"id": new_id(), "purpose": "owner_message", "correlation_id": kw.pop("correlation_id", new_id()), "message_id": m,
                  "agent_id": agent_id(conn), **FROZEN, "created_at": NOW, "updated_at": NOW, **kw}
        conn.execute(f"INSERT INTO telegram_owner_inputs({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
        return values["id"]
    refused(conn, "INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,%s,sensitive,created_at,updated_at) "
                  "VALUES (?,'owner_message',?,?,?,%s,1,?,?)" % (FROZEN_COLUMNS, FROZEN_VALUES),
            new_id(), new_id(), m, agent_id(conn), NOW, NOW)  # secret needs expiry + spool
    refused(conn, "INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,%s,sensitive,spool_id,created_at,updated_at) "
                  "VALUES (?,'owner_message',?,?,?,%s,0,?,?,?)" % (FROZEN_COLUMNS, FROZEN_VALUES),
            new_id(), new_id(), m, agent_id(conn), "s" * 32, NOW, NOW)  # only secrets spool
    refused(conn, "INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,%s,created_at,updated_at) "
                  "VALUES (?,'signoff_reply',?,?,?,%s,?,?)" % (FROZEN_COLUMNS, FROZEN_VALUES),
            new_id(), new_id(), m, agent_id(conn), NOW, NOW)  # a reply names its Signoff
    refused(conn, "INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,created_at,updated_at) "
                  "VALUES (?,'owner_message',?,?,?,?,?)", new_id(), new_id(), m, agent_id(conn), NOW, NOW)  # a delivery freezes its identity
    one = add(correlation_id="c1")
    refused(conn, "INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,%s,created_at,updated_at) "
                  "VALUES (?,'owner_message','c1',?,?,%s,?,?)" % (FROZEN_COLUMNS, FROZEN_VALUES),
            new_id(), m, agent_id(conn), NOW, NOW)
    for column, value in (("config_generation", 2), ("owner_principal_id", agent_id(conn)), ("binding_fingerprint", "e" * 64),
                          ("target_fingerprint", "e" * 64)):
        refused(conn, f"UPDATE telegram_owner_inputs SET {column}=? WHERE id=?", value, one)  # identity is never upgraded
    conn.execute("UPDATE telegram_owner_inputs SET state='uncertain' WHERE id=?", (one,))
    conn.execute("UPDATE telegram_owner_inputs SET state='submitted' WHERE id=?", (one,))
    for state in ("queued", "failed", "cancelled"):
        refused(conn, "UPDATE telegram_owner_inputs SET state=? WHERE id=?", state, one)
    refused(conn, "UPDATE telegram_owner_inputs SET sensitive=1 WHERE id=?", add())


def test_cleanup_targets_only_known_messages_once_and_deleted_stays_deleted(conn):
    known = message(conn)
    refused(conn, "INSERT INTO telegram_cleanup_jobs(id,message_id,reason,created_at,updated_at) VALUES (?,?,'x',?,?)", new_id(), new_id(), NOW, NOW)
    conn.execute("INSERT INTO telegram_cleanup_jobs(id,message_id,reason,created_at,updated_at) VALUES (?,?,'x',?,?)", (job := new_id(), known, NOW, NOW))
    refused(conn, "INSERT INTO telegram_cleanup_jobs(id,message_id,reason,created_at,updated_at) VALUES (?,?,'y',?,?)", new_id(), known, NOW, NOW)
    refused(conn, "UPDATE telegram_cleanup_jobs SET state='deleted' WHERE id=?", job)  # needs completed_at
    conn.execute("UPDATE telegram_cleanup_jobs SET state='deleted', completed_at=? WHERE id=?", (NOW, job))
    refused(conn, "UPDATE telegram_cleanup_jobs SET state='queued' WHERE id=?", job)
