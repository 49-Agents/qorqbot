"""Reviewed offline removal of the retired per-worker owner conversations. Nothing here runs from the service or an agent.

The private owner conversation is with Ordubashy alone (``qorqut.inbox.private_conversations``). Earlier releases also let
the owner converse privately with individual workers; those rows (``owner_conversations.peer_agent_id`` set to anyone but the
registered Ordubashy) are retired history and the owner agreed to delete them, without a retained thread archive and without
any backup or export of their contents. This module is that deletion, prepared as two separately invoked phases for the
installed release worker to call later:

* ``inspect_target`` is read-only. It resolves the exact deletion set and reports **counts and identifiers only** (never a
  message body), the blockers that would refuse an apply, the rows that are protected, a schema fingerprint and one digest of
  the whole reviewed set (identifiers, the schema and a content digest of every selected table).
* ``apply_cleanup`` needs an explicit database target, the resolved Ordubashy identity, a ``MaintenanceAuthorization`` that
  names the reviewed digest, and the reviewed inspection itself. It re-derives the set inside one ``BEGIN IMMEDIATE``
  transaction, refuses anything that differs from the reviewed digest, and removes the set atomically or not at all. Repeating
  it after success is a no-op. There is no startup hook, default target, backup, export or network effect.

The deletion set is exact and derived, never matched by text:

1. every ``owner_conversations`` row bound to a ``peer_agent_id`` other than the stated Ordubashy (a null peer is the legacy
   Ordubashy channel and an explicitly stored Ordubashy identity is that same channel; both are protected);
2. their ``owner_conversation_messages`` and ``owner_conversation_reads``;
3. the ``inbox_events`` that announced them (``source_type='owner_conversations'``, ``source_id`` the conversation) and every
   ``inbox_deliveries`` row of those events;
4. the ``idempotency_receipts`` of ``owner_conversation_send`` / ``owner_conversation_mark_read`` whose stored result is bound
   to a deleted conversation or message, or to a retired peer: they hold the message and would retain or resurrect it;
5. the forwarded ``private_conversation`` notices of owner messages in (2): ``agent_messages`` rows whose identifier is the
   deterministic ``forwarded_id('private_conversation', <message id>, <recipient>)`` and whose sender, recipient, thread,
   event and delivery relationships match a forwarded notice exactly, with their ``inbox_events`` and ``inbox_deliveries``.
   Other forwarding kinds and every other ``agent_messages`` row are untouched.

Canonical Task activity, Signoff decisions, authored sources, email, native Chat, raw archives and the Ordubashy channel are
never touched. Backend nudge attempts that merely list a deleted event number are append-only Operations records and stay;
they hold no text and are inert once the event is gone (reported as retained references). A retained authored source that
names a removed message (for example a business-setup source) is not deleted: its reader then reports the message as
unavailable.

Hard foreign-key dependents of a candidate row that are not themselves in the set (an authored reply to a notice, a delegated
event claim, a snooze wake, a Task thread event, a broadcast delivery, ...) make the **whole** apply refuse, listing the
blocking identifiers. Nothing is cascaded, nulled or skipped, and foreign keys are never disabled.

Only the exact immutable-delete triggers the set needs are dropped, inside the maintenance transaction, and each is recreated
from its stored SQL before commit. The schema fingerprint must then equal the one before; a rollback restores both schema and
contents. Anything unexpected (another trigger on a touched table, a missing immutability trigger, a changed schema) refuses.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from qorqut.inbox.owner_forwarding import NOTICE_SUMMARY, forwarded_id

# The header line the retired forwarding wrote, naming the original message; checked against the derived identifier only.
ORIGINAL = "Original: private Task conversation message "

FORMAT = "qorqut-v1.2-private-peer-cleanup"
VERSION = 1
NOTICE_KIND = "private_conversation"
RECEIPT_OPERATIONS = ("owner_conversation_send", "owner_conversation_mark_read")
CHUNK = 400
LISTED = 50
# Parent tables whose rows the set removes, with the key column other tables' foreign keys must name.
PARENTS = {"owner_conversations": "id", "owner_conversation_messages": "id", "agent_messages": "id", "inbox_events": "id"}
# The only triggers a touched table may carry. The `delete` ones are the immutability guards this maintenance lifts.
EXPECTED_TRIGGERS = {
    "owner_conversations": {},
    "owner_conversation_reads": {},
    "owner_conversation_messages": {"owner_conversation_messages_no_update": False, "owner_conversation_messages_no_delete": True},
    "inbox_events": {"inbox_events_immutable_update": False, "inbox_events_immutable_delete": True},
    "inbox_deliveries": {"inbox_ack_immutable": False, "inbox_delivery_required_immutable": False},
    "agent_messages": {"agent_messages_immutable_update": False, "agent_messages_immutable_delete": True},
    "idempotency_receipts": {"idempotency_receipts_no_update": False},
}
# Delete order: receipts and deliveries first, parents last.
TABLES = ("idempotency_receipts", "inbox_deliveries", "agent_messages", "inbox_events", "owner_conversation_reads",
          "owner_conversation_messages", "owner_conversations")


class CleanupError(Exception):
    """A refusal. ``code`` is stable; ``details`` carry identifiers and counts only, never message text."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


@dataclass(frozen=True)
class MaintenanceAuthorization:
    """The caller's explicit statement that this exact cleanup may run now. Every field is required and checked.

    ``reviewed_digest`` is the digest of the inspection someone reviewed and approved; ``reference`` is an opaque identifier of
    that approval (never content). ``writers_quiesced`` states that every writer of the target is stopped."""

    maintenance_authorized: bool
    writers_quiesced: bool
    reviewed_digest: str
    reference: str


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# ----- target access ----------------------------------------------------------------------------------------------------

class _Target:
    """An explicit database target: an absolute path to an existing SQLite file, or a caller's open connection."""

    def __init__(self, database, *, write):
        self.owned = not isinstance(database, sqlite3.Connection)
        self.write = write
        self.restore = None
        if self.owned:
            path = Path(database) if isinstance(database, (str, Path)) else None
            if path is None or not path.is_absolute() or not path.is_file():
                raise CleanupError("invalid_target", "Select one existing database file by absolute path or pass an open connection")
            location = path.as_uri() + ("?mode=rw" if write else "?mode=ro")
            self.connection = sqlite3.connect(location, uri=True, isolation_level=None, timeout=30)
            self.connection.execute("PRAGMA foreign_keys=ON")
        else:
            self.connection = database
            if self.connection.in_transaction:
                raise CleanupError("invalid_target", "The supplied connection must not be inside a transaction")
            self.restore = self.connection.isolation_level
            self.connection.isolation_level = None
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.close()
            raise CleanupError("foreign_keys_disabled", "Foreign key enforcement must stay enabled on the target connection")

    def close(self):
        """Return the connection as it was found. A transaction still open here is failed work: it is rolled back, never committed
        (re-setting ``isolation_level`` to ``None`` on an open transaction would commit it)."""
        rollback(self.connection)
        if self.owned:
            self.connection.close()
        elif not self.connection.in_transaction:
            self.connection.isolation_level = self.restore


def rows(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchall()


def rollback(connection):
    """Roll back a transaction that is still open; True when none remains. SQLite may already have ended it on its own."""
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
    return not connection.in_transaction


def chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def in_clause(values):
    return "(" + ",".join("?" * len(values)) + ")"


def table_exists(connection, name):
    return bool(rows(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def schema_inventory(connection):
    return [list(row) for row in rows(connection,
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]


def schema_fingerprint(connection):
    return sha(schema_inventory(connection))


# ----- the exact set ----------------------------------------------------------------------------------------------------

def primary_key(connection, table):
    """The primary-key columns of a table in key order, or ``rowid`` for a table without a declared key."""
    keyed = sorted((row[5], row[1]) for row in rows(connection, f"PRAGMA table_info({table})") if row[5])
    return tuple(name for _, name in keyed) or ("rowid",)


def check_identity(connection, ordubashy_id):
    """The stated Ordubashy must be the stored configured coordinator holding the Ordubashy role (never a name)."""
    if not isinstance(ordubashy_id, str) or not ordubashy_id:
        raise CleanupError("invalid_identity", "Name the resolved Ordubashy identity")
    installation = rows(connection, "SELECT owner_principal_id,management_coordinator_id FROM installation WHERE id='service'")
    if not installation or installation[0][1] != ordubashy_id or not installation[0][0]:
        raise CleanupError("invalid_identity", "The stated identity is not the registered owner's configured Ordubashy")
    profile = rows(connection, "SELECT profile_json FROM agent_profiles WHERE agent_id=?", (ordubashy_id,))
    try:
        role = json.loads(profile[0][0])["role"]["id"] if profile else None
    except (ValueError, KeyError, TypeError):
        role = None
    if role != "ordubashy":
        raise CleanupError("invalid_identity", "The stated identity does not hold the Ordubashy role")
    return installation[0][0]


def deletion_set(connection, ordubashy_id, owner):
    """Resolve the whole set, its blockers and its retained references. Reads only."""
    conversations = {row[0]: (row[1], row[2]) for row in rows(connection,
        "SELECT id,source_key,peer_agent_id FROM owner_conversations ORDER BY id")}
    selected = sorted(identity for identity, (_, peer) in conversations.items() if peer is not None and peer != ordubashy_id)
    chosen_ids = set(selected)
    protected = {"null_peer": sum(1 for _, peer in conversations.values() if peer is None),
                 "explicit_ordubashy": sum(1 for _, peer in conversations.values() if peer == ordubashy_id)}
    # A null peer with a peer-shaped key, or a selected row whose key does not name its own peer, is ambiguous: refuse, never guess.
    anomalies = sorted(identity for identity, (key, peer) in conversations.items()
                       if (peer is None and key.startswith("peer:")) or (identity in chosen_ids and not key.startswith(f"peer:{peer}:")))
    messages, reads = [], []
    for part in chunks(selected):
        messages += [tuple(row) for row in rows(connection,
            f"SELECT sequence,id,author_role FROM owner_conversation_messages WHERE conversation_id IN {in_clause(part)}", part)]
        reads += [tuple(row) for row in rows(connection,
            f"SELECT conversation_id,principal_id FROM owner_conversation_reads WHERE conversation_id IN {in_clause(part)}", part)]
    messages.sort()
    reads.sort()
    events = []
    for part in chunks(selected):
        events += [row[0] for row in rows(connection,
            f"SELECT id FROM inbox_events WHERE source_type='owner_conversations' AND source_id IN {in_clause(part)}", part)]
    events.sort()
    notices, notice_events, mismatched = forwarded_notices(connection, owner, ordubashy_id, [m[1] for m in messages if m[2] == "owner"])
    every_event = sorted(set(events) | set(notice_events))
    deliveries = []
    for part in chunks(every_event):
        deliveries += [tuple(row) for row in rows(connection,
            f"SELECT agent_id,event_id FROM inbox_deliveries WHERE event_id IN {in_clause(part)}", part)]
    deliveries.sort()
    receipts = retired_receipts(connection, ordubashy_id, set(selected), {m[1] for m in messages})
    chosen = {
        "owner_conversations": [(identity,) for identity in selected],
        "owner_conversation_messages": [(sequence,) for sequence, _, _ in messages],
        "owner_conversation_reads": reads,
        "inbox_events": [(identity,) for identity in every_event],
        "inbox_deliveries": deliveries,
        "agent_messages": [(identity,) for identity in notices],
        "idempotency_receipts": receipts,
    }
    blockers = foreign_dependents(connection, chosen, {
        "owner_conversations": set(selected), "owner_conversation_messages": {m[1] for m in messages},
        "agent_messages": set(notices), "inbox_events": set(every_event)})
    if anomalies:
        blockers.append({"table": "owner_conversations", "reason": "key_peer_mismatch", "count": len(anomalies), "ids": anomalies[:LISTED]})
    if mismatched:
        blockers.append({"table": "agent_messages", "reason": "notice_relationship_mismatch", "count": len(mismatched),
                         "ids": sorted(mismatched)[:LISTED]})
    blockers += trigger_blockers(connection)
    return {"chosen": chosen, "blockers": blockers, "protected": protected, "conversations": selected,
            "messages": [m[1] for m in messages], "events": events, "notice_events": notice_events,
            "retained": retained_references(connection, set(every_event), {m[1] for m in messages})}


def forwarded_notices(connection, owner, ordubashy_id, owner_messages):
    """The forwarded ``private_conversation`` notices of these owner messages, proven by identifier and relationships.

    A candidate's identifier is derived, never searched: ``forwarded_id(kind, message id, recipient)`` for the current
    Ordubashy and for every recipient an owner-sent notice-shaped message ever had. The row must then also be a notice by the
    owner to that recipient, alone in its thread, on its own direct-message event with exactly one delivery. A matching
    identifier whose relationships differ is reported (``mismatched``), never silently skipped."""
    if not owner_messages:
        return [], [], []
    recipients = {ordubashy_id} | {row[0] for row in rows(connection,
        "SELECT DISTINCT recipient_id FROM agent_messages WHERE sender_id=? AND reply_to IS NULL AND thread_id=id", (owner,))}
    candidates = {forwarded_id(NOTICE_KIND, message, recipient): (message, recipient)
                  for message in owner_messages for recipient in recipients}
    notices, events, mismatched = [], [], []
    for part in chunks(sorted(candidates)):
        for row in rows(connection, f"SELECT id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,current_sender_id,"
                        f"current_recipient_id FROM agent_messages WHERE id IN {in_clause(part)}", part):
            identity, event, business, sender, recipient, reply, thread, current_sender, current_recipient = tuple(row)
            message, expected = candidates[identity]
            headed = connection.execute("SELECT instr(body,?)>0 FROM agent_messages WHERE id=?",
                                        (f"{ORIGINAL}{message}, accepted ", identity)).fetchone()[0]
            announced = rows(connection, "SELECT business_id,source_type,source_id,actor_id,event_type,summary FROM inbox_events WHERE id=?", (event,))
            delivered = rows(connection, "SELECT agent_id FROM inbox_deliveries WHERE event_id=?", (event,))
            exact = (headed == 1 and business is None and sender == owner and recipient == expected and current_sender == owner
                     and current_recipient == expected and reply is None and thread == identity
                     and bool(announced) and tuple(announced[0]) == (None, "agent_messages", identity, owner, "direct_message", NOTICE_SUMMARY)
                     and [d[0] for d in delivered] == [expected])
            if exact:
                notices.append(identity)
                events.append(event)
            else:
                mismatched.append(identity)
    return sorted(notices), sorted(events), mismatched


def retired_receipts(connection, ordubashy_id, conversations, messages):
    """Saved results of the two private-thread mutations that are bound to a deleted conversation/message or retired peer."""
    found = []
    for principal, namespace, operation, key, result in rows(connection,
            f"SELECT principal_id,target_namespace,operation_name,idempotency_key,result_json FROM idempotency_receipts "
            f"WHERE operation_name IN {in_clause(RECEIPT_OPERATIONS)}", RECEIPT_OPERATIONS):
        try:
            data = (json.loads(result) or {}).get("data") or {}
            conversation, message = data.get("conversation") or {}, data.get("message") or {}
        except (ValueError, AttributeError):
            continue
        peer = conversation.get("agent_id")
        if (conversation.get("id") in conversations or message.get("id") in messages
                or (conversation.get("id") is None and peer is not None and peer != ordubashy_id)):
            found.append((principal, namespace, operation, key))
    return sorted(found)


def foreign_dependents(connection, chosen, parent_values):
    """Rows outside the set whose foreign key names a candidate parent row, from the schema's own foreign-key graph."""
    blockers = []
    for table in [row[0] for row in rows(connection, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]:
        key = primary_key(connection, table)
        inside = set(chosen.get(table, ()))
        for fk in rows(connection, f"PRAGMA foreign_key_list({table})"):
            parent, column, target = fk[2], fk[3], fk[4]
            if parent not in PARENTS:
                continue
            if target not in (None, PARENTS[parent]):
                blockers.append({"table": table, "column": column, "parent": parent, "reason": "unsupported_foreign_key"})
                continue
            values = sorted(parent_values[parent])
            found = []
            for part in chunks(values):
                for row in rows(connection, f"SELECT {','.join(key)} FROM {table} WHERE {column} IN {in_clause(part)}", part):
                    if tuple(row) not in inside:
                        found.append(tuple(row))
            if found:
                blockers.append({"table": table, "column": column, "parent": parent, "reason": "dependent_row",
                                 "count": len(found), "ids": [list(item) for item in sorted(found)[:LISTED]]})
    return blockers


def trigger_blockers(connection):
    """Any trigger on a touched table other than the known immutability guards, and any guard that is missing or unrecognised."""
    blockers = []
    for table, expected in EXPECTED_TRIGGERS.items():
        present = {name: sql for name, sql in rows(connection, "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        unexpected, missing = sorted(set(present) - set(expected)), sorted(set(expected) - set(present))
        shaped = sorted(name for name, lifts in expected.items() if name in present and lifts
                        and not ("BEFORE DELETE ON " + table in " ".join(present[name].split()) and "RAISE(ABORT" in present[name]))
        if unexpected or missing or shaped:
            blockers.append({"table": table, "reason": "unexpected_triggers", "unexpected": unexpected, "missing": missing, "changed": shaped})
    return blockers


def retained_references(connection, events, messages):
    """Records that keep naming a deleted row and are not part of the set. Counts only."""
    retained = {}
    if table_exists(connection, "inbox_nudge_attempts"):
        count = 0
        for (listed,) in rows(connection, "SELECT event_ids_json FROM inbox_nudge_attempts"):
            try:
                count += bool(events & {item for item in json.loads(listed) if isinstance(item, int)})
            except (ValueError, TypeError):
                continue
        retained["inbox_nudge_attempts_naming_a_deleted_event"] = count
    # Authored sources may cite an owner message by identifier; they stay, and their readers now report it unavailable.
    for (table,) in rows(connection, "SELECT m.name FROM sqlite_master m JOIN pragma_table_info(m.name) c ON c.name='source_refs_json' "
                                     "WHERE m.type='table' AND m.name NOT LIKE 'sqlite_%' ORDER BY m.name"):
        count = 0
        for (listed,) in rows(connection, f'SELECT source_refs_json FROM "{table}" WHERE source_refs_json LIKE \'%owner_message%\''):
            try:
                count += sum(1 for item in json.loads(listed) if isinstance(item, dict)
                             and item.get("type") == "owner_message" and item.get("id") in messages)
            except (ValueError, TypeError):
                continue
        retained[f"{table}_owner_message_sources_now_unavailable"] = count
    return retained


def content_digests(connection, chosen):
    """One digest per selected table over every column of the selected rows: identifies the exact rows, discloses none."""
    digests = {}
    for table in TABLES:
        key, hasher = primary_key(connection, table), hashlib.sha256()
        for identity in sorted(chosen[table]):
            row = rows(connection, f"SELECT * FROM {table} WHERE " + " AND ".join(f"{column}=?" for column in key), identity)
            hasher.update(canonical([[value.hex() if isinstance(value, bytes) else value for value in item] for item in row]))
        digests[table] = hasher.hexdigest()
    return digests


def plan_of(connection, ordubashy_id, owner):
    """The reviewed plan: identifiers, schema fingerprint, blockers and content digests. ``digest`` identifies all of it."""
    found = deletion_set(connection, ordubashy_id, owner)
    chosen = found["chosen"]
    plan = {"format": FORMAT, "version": VERSION, "ordubashy_id": ordubashy_id, "schema_fingerprint": schema_fingerprint(connection),
            "selection": {table: [list(item) for item in sorted(chosen[table])] for table in TABLES},
            "blockers": found["blockers"], "content": content_digests(connection, chosen)}
    return plan, found


def empty(plan):
    return not any(plan["selection"].values())


def inspect_target(database, *, ordubashy_id):
    """Read-only report of the exact deletion set: counts and identifiers only, its blockers, protected rows and digest."""
    target = _Target(database, write=False)
    connection = target.connection
    try:
        connection.execute("BEGIN")
        try:
            owner = check_identity(connection, ordubashy_id)
            plan, found = plan_of(connection, ordubashy_id, owner)
        finally:
            connection.execute("ROLLBACK")
    finally:
        target.close()
    return {"format": FORMAT, "version": VERSION, "plan": plan, "digest": sha(plan), "blocked": bool(plan["blockers"]),
            "counts": {table: len(plan["selection"][table]) for table in TABLES}, "protected": found["protected"],
            "retained_references": found["retained"]}


# ----- apply ------------------------------------------------------------------------------------------------------------

def require_authorization(authorization, digest):
    if (not isinstance(authorization, MaintenanceAuthorization) or authorization.maintenance_authorized is not True
            or authorization.writers_quiesced is not True or not isinstance(authorization.reference, str)
            or not authorization.reference.strip() or authorization.reviewed_digest != digest):
        raise CleanupError("not_authorized", "Apply needs the explicit maintenance and quiescence authorization for the reviewed digest")


def apply_cleanup(database, *, ordubashy_id, reviewed, authorization):
    """Remove exactly the reviewed set in one transaction, or change nothing.

    ``reviewed`` is the unmodified ``inspect_target`` result that was approved. Refusals (``CleanupError``): unauthorized,
    unreviewed or altered plan, a target that differs from the reviewed digest, blockers, or an unexpected schema. A set that
    is already gone, with the same schema, is a no-op."""
    plan, digest = (reviewed or {}).get("plan"), (reviewed or {}).get("digest")
    if not isinstance(plan, dict) or not isinstance(digest, str) or sha(plan) != digest or plan.get("format") != FORMAT \
            or plan.get("version") != VERSION or plan.get("ordubashy_id") != ordubashy_id:
        raise CleanupError("invalid_plan", "Supply the unmodified reviewed inspection for this Ordubashy identity")
    require_authorization(authorization, digest)
    target = _Target(database, write=True)
    connection = target.connection
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = apply_in_transaction(connection, ordubashy_id, plan, digest)
            connection.execute("COMMIT")
        except BaseException as error:
            # Every failure up to and including COMMIT leaves nothing applied: the open transaction is rolled back before the
            # connection settings are restored or control returns.
            undone = rollback(connection)
            if isinstance(error, sqlite3.Error):
                raise CleanupError("apply_failed", f"The cleanup was rolled back: {error}" if undone else
                                   "The cleanup failed and its transaction could not be rolled back; close the connection "
                                   f"without committing: {error}") from None
            raise
        return result
    finally:
        target.close()


def apply_in_transaction(connection, ordubashy_id, reviewed_plan, digest):
    owner = check_identity(connection, ordubashy_id)
    current, found = plan_of(connection, ordubashy_id, owner)
    if current["blockers"]:
        raise CleanupError("blocked", "Dependents outside the reviewed set refer to it; nothing was changed",
                           blockers=current["blockers"])
    if current["schema_fingerprint"] != reviewed_plan["schema_fingerprint"]:
        raise CleanupError("schema_changed", "The target schema differs from the reviewed schema")
    if empty(current):
        if empty(reviewed_plan) or all(not exists(connection, table, identity) for table in TABLES
                                       for identity in reviewed_plan["selection"][table]):
            return {"applied": False, "noop": True, "digest": digest, "removed": dict.fromkeys(TABLES, 0),
                    "schema_fingerprint": current["schema_fingerprint"]}
        raise CleanupError("target_changed", "The target differs from the reviewed deletion set")
    if sha(current) != digest:
        raise CleanupError("target_changed", "The target differs from the reviewed deletion set; inspect it again")
    before = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}
    violations = rows(connection, "PRAGMA foreign_key_check")
    chosen = found["chosen"]
    lifted = lifted_triggers(connection, chosen)
    for name, _ in lifted:
        connection.execute(f"DROP TRIGGER {name}")
    removed = {}
    for table in TABLES:
        key = primary_key(connection, table)
        statement = f"DELETE FROM {table} WHERE " + " AND ".join(f"{column}=?" for column in key)
        removed[table] = 0
        for identity in sorted(chosen[table]):
            removed[table] += delete_row(connection, statement, identity)
        if removed[table] != len(chosen[table]):
            raise CleanupError("apply_failed", f"{table} did not delete exactly the reviewed rows")
    for _, sql in lifted:
        connection.execute(sql)
    fingerprint = schema_fingerprint(connection)
    remaining, _ = plan_of(connection, ordubashy_id, owner)
    if fingerprint != reviewed_plan["schema_fingerprint"] or not empty(remaining) or remaining["blockers"]:
        raise CleanupError("apply_failed", "The cleanup did not leave the reviewed schema and an empty deletion set")
    if rows(connection, "PRAGMA foreign_key_check") != violations:
        raise CleanupError("apply_failed", "The cleanup changed the foreign-key state")
    after = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}
    if any(after[table] != before[table] - removed[table] for table in TABLES):
        raise CleanupError("apply_failed", "Surviving row counts differ from the reviewed outcome")
    return {"applied": True, "noop": False, "digest": digest, "removed": removed, "schema_fingerprint": fingerprint}


def delete_row(connection, statement, identity):
    return connection.execute(statement, identity).rowcount


def lifted_triggers(connection, chosen):
    """(name, stored SQL) of the exact delete guards the selected rows need lifted; the order is stable."""
    wanted = [name for table, guards in EXPECTED_TRIGGERS.items() if chosen.get(table)
              for name, lifts in guards.items() if lifts]
    found = {name: sql for name, sql in rows(connection, "SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    if any(name not in found or not found[name] for name in wanted):
        raise CleanupError("schema_changed", "An expected immutability trigger is missing")
    return [(name, found[name]) for name in wanted]


def exists(connection, table, identity):
    key = primary_key(connection, table)
    return bool(rows(connection, f"SELECT 1 FROM {table} WHERE " + " AND ".join(f"{column}=?" for column in key), identity))
