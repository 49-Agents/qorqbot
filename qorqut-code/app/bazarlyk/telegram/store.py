"""Telegram bridge persistence helpers. Every function takes a unit of work; none performs network or terminal I/O.

Times are UTC strings in the fixed-width form produced by ``iso`` so that string comparison orders them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain

OUTBOX_OPEN = ("queued", "dispatching", "uncertain")
# What an outbound message row shows an agent as history. Notices, Signoff posts and urgent events are
# bridge-owned and never appear in an agent's bounded history.
HISTORY_PURPOSES = ("reply", "blocking_input", "update", "owner")
REDACTED_BODY = "[secret input redacted]"
# The outbox source of an agent's attachment: an immutable closed payload that names a sealed snapshot, never its location.
AGENT_FILE_SOURCE = "agent_file"
# What the public surfaces may say about an attachment, and nothing else: its name, mode, size and digest.
ATTACHMENT_FIELDS = ("filename", "send_as", "size", "sha256")
PAYLOAD_TABLE = "telegram_file_payloads"
MAX_UNICODE_CHARS = 4000
MAX_UTF16_UNITS = 4096


def iso(value):
    if value.tzinfo is None:
        raise ValueError("Telegram clocks must return aware timestamps")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def plus(now, seconds):
    return iso(parse(now) + timedelta(seconds=seconds))


def target_fingerprint(profile):
    """The registered target an owner input was accepted for: agent, home scope, workspace, role and runtime."""
    data = profile["profile_json"]
    data = json.loads(data) if isinstance(data, str) else data
    data = data if isinstance(data, dict) else {}
    return canonical_hash({"agent": profile["agent_id"], "home": profile["home_business_id"], "namespace": profile["namespace"],
                           "workspace": profile["workspace"], "role": (data.get("role") or {}).get("id"),
                           "runtime": data.get("runtime")})


def inserted(uow, table, values):
    return uow.repository(table).insert(values)


# ----- integration state ------------------------------------------------------------------------------------


def get_state(uow):
    return uow.repository("telegram_state").get("telegram")


def next_second(now):
    """The first whole UTC second after ``now``. Telegram dates have second precision, so an update dated in the second of a
    reconfiguration might have been sent before it: only a later whole second is unambiguously after it."""
    return iso(parse(now).replace(microsecond=0) + timedelta(seconds=1))


def cutoff(state):
    """The instant an update must not precede to gain authority: the later of activation and this generation's effective time."""
    return max(state["activation_at"], state["binding_effective_at"])


def ensure_state(uow, config, installation, *, now):
    """Create the singleton on first enabled start; later starts keep cursor and cutoff.

    The configured installation and Qorqut owner are matched against the authoritative records at first activation and
    saved as the integration's pin. They are never inferred again: an unchanged file with a different database owner is
    ``owner_binding_mismatch``, and a different installation is ``installation_binding_mismatch`` and never adopts this state.
    Only an operator's file edit repins the owner (and the Telegram owner and chat/agent bindings): once it validates against
    the authoritative owner it is a new generation whose effective time is the next whole UTC second. The bot cursor and the
    original activation cutoff carry over; every older queued authority is invalidated by the generation.
    A different bot ID fails closed: its update IDs are unrelated to the saved cursor.
    """
    state = get_state(uow)
    fingerprint = config.binding_fingerprint()
    if installation["installation_id"] != config.installation_id or (
            state is not None and state["installation_id"] != installation["installation_id"]):
        raise DomainError("installation_binding_mismatch",
                          "The configured or saved installation is not this installation; this state cannot be reused for it")
    if state is None:
        if installation["owner_principal_id"] != config.owner_principal_id:
            raise DomainError("owner_binding_mismatch", "The configured Qorqut owner is not the installation owner")
        return uow.repository("telegram_state").insert({
            "id": "telegram", "bot_id": config.bot_id, "installation_id": config.installation_id,
            "owner_principal_id": config.owner_principal_id, "owner_telegram_user_id": config.owner_telegram_user_id,
            "config_generation": 1, "binding_fingerprint": fingerprint, "binding_effective_at": config.activation_at,
            "activation_at": config.activation_at, "poll_offset": 0, "created_at": now, "updated_at": now})
    if state["bot_id"] != config.bot_id:
        raise DomainError("binding_conflict", "The saved Telegram state belongs to a different bot; its cursor cannot be reused")
    # An unchanged pin must still match the database; an edited pin must name the actual current owner. Nothing is inferred.
    if installation["owner_principal_id"] != config.owner_principal_id:
        raise DomainError("owner_binding_mismatch",
                          "The installation owner is not the configured Qorqut owner; edit expected_owner_principal_id to repin")
    if state["binding_fingerprint"] != fingerprint or state["owner_telegram_user_id"] != config.owner_telegram_user_id:
        updated = uow.repository("telegram_state").update("telegram", {
            "config_generation": state["config_generation"] + 1, "binding_fingerprint": fingerprint,
            "owner_telegram_user_id": config.owner_telegram_user_id, "owner_principal_id": config.owner_principal_id,
            "binding_effective_at": max(next_second(now), state["binding_effective_at"])}, expected_revision=state["revision"])
        cancel_misplaced_requests(uow, config, now)
        return updated
    return state


def cancel_misplaced_requests(uow, config, now):
    """End blocking-input requests that were asked at a destination the configuration no longer names for their logical chat.

    A request belongs to the exact ``(chat, topic)`` it was sent to. After a rebind it can no longer be answered (what is said
    there is not routed), and its unique open-request slot would block the new destination, so it is cancelled. Its metadata and
    its original timestamps stay; the closure time is when it was cancelled. Nothing is retargeted.
    """
    for chat in config.chats:
        for row in uow.connection.execute(
                "SELECT * FROM telegram_messages WHERE chat_name=? AND request_state='pending' "
                "AND NOT (chat_id=? AND message_thread_id IS ?)", (chat.name, chat.chat_id, chat.message_thread_id)).fetchall():
            close_request(uow, dict(row), "cancelled", now)


def touch_state(uow, **changes):
    state = get_state(uow)
    if state is None:
        return None
    return uow.repository("telegram_state").update("telegram", changes, expected_revision=state["revision"])


# ----- received updates -------------------------------------------------------------------------------------


def store_batch(uow, bot_id, updates, *, now, polled_at):
    """Persist normalized updates and advance the cursor in one transaction. Duplicates keep their first row."""
    state = get_state(uow)
    if state is None or state["bot_id"] != bot_id:
        raise DomainError("binding_conflict", "Telegram state does not match the polling bot")
    offset = state["poll_offset"]
    new = 0
    for item in updates:
        found = uow.connection.execute(
            "INSERT OR IGNORE INTO telegram_updates(bot_id,update_id,kind,chat_id,message_id,actor_id,sent_at,normalized_json,"
            "state,received_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (bot_id, item["update_id"], item["kind"], item.get("chat_id"), item.get("message_id"), item.get("actor_id"),
             item.get("sent_at"), json.dumps(plain(item), sort_keys=True), "received", now, now, now))
        new += found.rowcount
        offset = max(offset, item["update_id"] + 1)
    changes = {"last_poll_at": polled_at}
    if updates:
        changes["last_update_received_at"] = now
    if offset != state["poll_offset"]:
        changes["poll_offset"] = offset
    touch_state(uow, **changes)
    return new


def pending_updates(uow, bot_id, limit=100):
    return [dict(row) | {"normalized": json.loads(row["normalized_json"])} for row in uow.connection.execute(
        "SELECT * FROM telegram_updates WHERE bot_id=? AND state='received' ORDER BY update_id LIMIT ?", (bot_id, limit))]


def finish_update(uow, bot_id, update_id, state, *, reason=None, result=None, now):
    row = uow.repository("telegram_updates").get((bot_id, update_id), required=True)
    values = {"state": state, "reason": reason, "processed_at": now if state in ("done", "ignored") else None,
              "attempts": row["attempts"] + 1}
    normalized = row["normalized_json"]
    if state == "ignored" and isinstance(normalized, dict) and ("text" in normalized or "attachments" in normalized):
        # Discarded backlog keeps its metadata and the reason, never the words.
        values["normalized_json"] = {key: value for key, value in normalized.items() if key not in ("text", "attachments")}
    if result is not None:
        values["result_json"] = result
    return uow.repository("telegram_updates").update((bot_id, update_id), values, expected_revision=row["revision"])


# ----- messages ---------------------------------------------------------------------------------------------


def message(uow, message_id):
    return uow.repository("telegram_messages").get(message_id)


def message_by_provider(uow, chat_id, provider_message_id):
    """A known message by its provider identity. Provider IDs are unique group-wide, so a caller that routes by destination
    must also compare the row's destination (``in_destination``); this lookup alone says nothing about which topic it is in."""
    row = uow.connection.execute("SELECT * FROM telegram_messages WHERE chat_id=? AND telegram_message_id=?",
                                 (chat_id, provider_message_id)).fetchone()
    return dict(row) if row else None


def in_destination(row, chat):
    """Whether a saved message or send was made at exactly this binding's logical name, chat and topic."""
    return bool(row) and (row["chat_name"], row["chat_id"], row["message_thread_id"]) == (chat.name, chat.chat_id, chat.message_thread_id)


def insert_message(uow, *, direction, chat, purpose, body, now, occurred_at=None, agent_id=None, reply_to_id=None,
                   reply_to_telegram_id=None, request=None, update_id=None, outbox_id=None, telegram_message_id=None,
                   sensitive=False, message_id=None):
    values = {"id": message_id or new_id(), "direction": direction, "chat_name": chat.name, "chat_id": chat.chat_id,
              "message_thread_id": chat.message_thread_id, "telegram_message_id": telegram_message_id, "agent_id": agent_id, "purpose": purpose,
              "body": None if sensitive else body, "body_redacted": 1 if sensitive else 0, "sensitive": 1 if sensitive else 0,
              "reply_to_id": reply_to_id, "reply_to_telegram_id": reply_to_telegram_id, "update_id": update_id,
              "outbox_id": outbox_id, "occurred_at": occurred_at or now, "created_at": now, "updated_at": now}
    if request:
        values.update(request_key=request["request_key"], input_mode=request["input_mode"],
                      request_reason=request.get("reason"), request_state="pending",
                      request_expires_at=request["expires_at"])
    return uow.repository("telegram_messages").insert(values)


def update_message(uow, row, **changes):
    return uow.repository("telegram_messages").update(row["id"], changes, expected_revision=row["revision"])


def attachment_of(uow, message_id):
    """The public attachment description of one message, or ``None``: ``{filename, send_as, size, sha256}``. It survives the
    removal of the sealed bytes (the payload row keeps these four facts) and never carries an identifier, path or manifest."""
    row = uow.connection.execute(
        f"SELECT filename, send_as, size_bytes AS size, sha256 FROM {PAYLOAD_TABLE} WHERE message_id=? AND sha256 IS NOT NULL",
        (message_id,)).fetchone()
    return attachment_view(row) if row else None


def attachment_view(row):
    """The four public fields of a row that carries them as ``filename``, ``send_as``, ``size`` and ``sha256``."""
    return {name: row[name] for name in ATTACHMENT_FIELDS}


def attachment_label(attachment):
    """A short display label for a captionless attachment: ``File: <name>`` or ``Photo: <name>``. Never sent to Telegram."""
    return f"{'Photo' if attachment['send_as'] == 'photo' else 'File'}: {attachment['filename']}"


def display_text(body, attachment):
    """The words shown for a message in history and excerpts: its body, or the attachment label when the body is empty."""
    if attachment is not None and not (body or "").strip():
        return attachment_label(attachment)
    return body


def pending_request(uow, chat, request_key=None):
    """The currently pending blocking-input request at this exact destination (optionally by key), or None.

    A request asked at another topic, or at a former binding of the same logical name, is never borrowed.
    """
    query = "SELECT * FROM telegram_messages WHERE chat_name=? AND chat_id=? AND message_thread_id IS ? AND request_state='pending'"
    params = [chat.name, chat.chat_id, chat.message_thread_id]
    if request_key is not None:
        query += " AND request_key=?"
        params.append(request_key)
    row = uow.connection.execute(query + " ORDER BY occurred_at DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def secret_at(uow, chat, sent_at):
    """The newest secret request at this exact destination whose lifetime contains ``sent_at`` (the message's own Telegram date).

    Only a request asked at this chat and topic counts: another topic's request, or a former binding's of the same logical name,
    never classifies a message here.

    The lifetime runs from when the request was queued to whichever came first: its expiry or its closure (answered or
    cancelled). It is judged by the message's send time, never by when the bridge got round to reading it, so a code that was
    sent while the prompt was open stays a secret however late it is ingested. A tie at either end counts as inside.

    A Telegram date has only second precision, so it stands for the whole second it names. The request's start is therefore
    floored to its second for this comparison (the stored timestamp keeps its precision): a text dated in the second the
    request was queued might have been sent after it, and is protected. This only classifies; whether the text may be
    delivered, and who may send it, are decided elsewhere.
    """
    row = uow.connection.execute(
        "SELECT * FROM telegram_messages WHERE chat_name=? AND chat_id=? AND message_thread_id IS ? AND direction='outbound' "
        "AND input_mode='secret' AND substr(occurred_at,1,19)<=substr(?,1,19) AND request_expires_at>=? "
        "AND (request_closed_at IS NULL OR request_closed_at>=?) "
        "ORDER BY occurred_at DESC, rowid DESC LIMIT 1",
        (chat.name, chat.chat_id, chat.message_thread_id, sent_at, sent_at, sent_at)).fetchone()
    return dict(row) if row else None


def close_request(uow, row, state, closed_at):
    """End a blocking-input request and keep when, so later ingestion can still tell what was sent while it was open."""
    return update_message(uow, row, request_state=state, request_closed_at=closed_at)


def history(uow, chat, agent_id, *, activation_at, limit):
    """What one agent may read: only messages of its own exact destination (logical name, provider chat and topic) and agent
    identity, at most ``limit`` of them. A message that an earlier binding of the same chat name recorded for another agent, or at
    another topic, is kept but never shown to this one."""
    rows = uow.connection.execute(
        "SELECT m.*, o.state AS delivery_state, o.id AS delivery_id, p.filename AS filename, p.send_as AS send_as, "
        "p.size_bytes AS size, p.sha256 AS sha256 FROM telegram_messages m "
        "LEFT JOIN telegram_outbox o ON o.id=m.outbox_id "
        f"LEFT JOIN {PAYLOAD_TABLE} p ON p.message_id=m.id "
        "WHERE m.chat_name=? AND m.chat_id=? AND m.message_thread_id IS ? AND m.agent_id=? AND m.occurred_at>=? AND m.purpose IN (%s) "
        "AND (m.direction='inbound' OR o.state IS NULL OR o.state!='cancelled') "
        "ORDER BY m.occurred_at DESC, m.rowid DESC LIMIT ?" % ",".join("?" * len(HISTORY_PURPOSES)),
        (chat.name, chat.chat_id, chat.message_thread_id, agent_id, activation_at, *HISTORY_PURPOSES, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]


# ----- outbox -----------------------------------------------------------------------------------------------


def outbox(uow, outbox_id):
    return uow.repository("telegram_outbox").get(outbox_id)


def outbox_by_source(uow, source_kind, source_key, part="main"):
    row = uow.connection.execute("SELECT * FROM telegram_outbox WHERE source_kind=? AND source_key=? AND part=?",
                                 (source_kind, source_key, part)).fetchone()
    return decode(row)


def decode(row):
    if row is None:
        return None
    from qorqut_operations.storage.repositories import decode_row
    return decode_row(row)


def enqueue(uow, chat, config_generation, binding_fingerprint, *, kind="text", source_kind, source_key, part="main",
            payload, now, message_id=None, agent_id=None, producer_id=None, next_attempt_at=None, event=None):
    """Insert an outbox entry; an existing entry for the same (source_kind, source_key, part) is returned unchanged.

    ``event`` is ``(producer, event_key, sequence)`` for an urgent-event send or edit: the persisted sequence lets dispatch
    and owner reconciliation tell current content from content a newer transition already replaced.
    """
    existing = outbox_by_source(uow, source_kind, source_key, part)
    if existing is not None:
        return existing, False
    event_producer, event_key, event_sequence = event or (None, None, None)
    row = uow.repository("telegram_outbox").insert({
        "id": new_id(), "chat_name": chat.name, "chat_id": chat.chat_id, "message_thread_id": chat.message_thread_id,
        "kind": kind, "source_kind": source_kind,
        "source_key": source_key, "part": part, "message_id": message_id, "payload_json": payload, "state": "queued",
        "next_attempt_at": next_attempt_at, "binding_fingerprint": binding_fingerprint,
        "config_generation": config_generation, "agent_id": agent_id, "producer_id": producer_id,
        "event_producer": event_producer, "event_key": event_key, "event_sequence": event_sequence,
        "created_at": now, "updated_at": now})
    if message_id is not None:
        message_row = message(uow, message_id)
        if message_row and message_row["outbox_id"] is None:
            update_message(uow, message_row, outbox_id=row["id"])
    return row, True


def set_outbox(uow, row, **changes):
    return uow.repository("telegram_outbox").update(row["id"], changes, expected_revision=row["revision"])


def due_outbox(uow, now, limit=20, *, uploads=None):
    """Due queued entries, oldest first. ``uploads`` False leaves out agent attachment uploads, True keeps only them, and the
    default ``None`` is every kind in one queue. The dispatcher asks for everything else first so that a backlog of large
    uploads can never starve text, edits, Signoffs or notifications."""
    scope = "" if uploads is None else (" AND source_kind=?" if uploads else " AND source_kind!=?")
    values = (now, *(() if uploads is None else (AGENT_FILE_SOURCE,)), limit)
    return [decode(row) for row in uow.connection.execute(
        "SELECT * FROM telegram_outbox WHERE state='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)" + scope +
        " ORDER BY created_at, rowid LIMIT ?", values)]


def pending_notify_count(uow, producer_id=None):
    """Urgent events not yet delivered for one producer (``None`` is the owner), so a noisy producer never starves another."""
    return uow.connection.execute("SELECT count(*) FROM telegram_outbox WHERE source_kind='notify' AND producer_id IS ? "
                                  "AND state IN ('queued','dispatching','uncertain')", (producer_id,)).fetchone()[0]


# ----- cleanup ----------------------------------------------------------------------------------------------


def enqueue_cleanup(uow, message_id, reason, *, now, not_before=None):
    """Queue deletion of one known message. A known message is one this bridge stored with a provider ID."""
    known = message(uow, message_id)
    if known is None:
        raise DomainError("not_found", "Only messages known to the bridge can be deleted")
    existing = uow.connection.execute("SELECT * FROM telegram_cleanup_jobs WHERE message_id=?", (message_id,)).fetchone()
    if existing:
        return dict(existing)
    return uow.repository("telegram_cleanup_jobs").insert({
        "id": new_id(), "message_id": message_id, "reason": reason, "state": "queued", "not_before": not_before,
        "next_attempt_at": not_before, "created_at": now, "updated_at": now})


# ----- status -----------------------------------------------------------------------------------------------


def _with_attachments(uow, entries):
    """Each delivery entry of an agent attachment also names the file (name, mode, size, digest) the owner would be reconciling."""
    for entry in entries:
        if entry["source_kind"] == AGENT_FILE_SOURCE and entry["message_id"]:
            attachment = attachment_of(uow, entry["message_id"])
            if attachment is not None:
                entry["attachment"] = attachment
    return entries


def status_counts(uow, now):
    c = uow.connection
    return {
        "updates_backlog": c.execute("SELECT count(*) FROM telegram_updates WHERE state='received'").fetchone()[0],
        "outbox": {row["state"]: row["n"] for row in c.execute("SELECT state,count(*) n FROM telegram_outbox GROUP BY state")},
        # Each entry is one exact delivery (a send or an edit): ``outbox_id`` plus ``receipt_revision`` is what the owner resolves.
        "uncertain_sends": _with_attachments(uow, [dict(row) for row in c.execute(
            "SELECT id AS outbox_id, message_id, kind, revision AS receipt_revision, event_sequence, source_kind, created_at, "
            "chat_name AS chat, message_thread_id FROM telegram_outbox WHERE state='uncertain' ORDER BY created_at, rowid LIMIT 20")]),
        "failed_sends": _with_attachments(uow, [dict(row) for row in c.execute(
            "SELECT id AS outbox_id, message_id, kind, revision AS receipt_revision, event_sequence, error_code, source_kind, "
            "chat_name AS chat, message_thread_id FROM telegram_outbox WHERE state='failed' ORDER BY created_at DESC LIMIT 20")]),
        "oldest_queued_input": (c.execute("SELECT min(created_at) FROM telegram_owner_inputs WHERE state IN ('queued','submitting')").fetchone()[0]),
        "unavailable_targets": [dict(row) for row in c.execute(
            "SELECT agent_id, deferral_reason AS reason, count(*) AS queued FROM telegram_owner_inputs "
            "WHERE state='queued' AND deferral_reason IS NOT NULL GROUP BY agent_id, deferral_reason LIMIT 20")],
        "uncertain_inputs": c.execute("SELECT count(*) FROM telegram_owner_inputs WHERE state='uncertain'").fetchone()[0],
        # Direct terminal delivery that did not happen (the canonical inbox event or Signoff note still holds the text).
        "undelivered_inputs": [dict(row) for row in c.execute(
            "SELECT purpose, state, error_code, count(*) AS count FROM telegram_owner_inputs WHERE state IN ('failed','expired') "
            "GROUP BY purpose, state, error_code LIMIT 20")],
        "cleanup": {row["state"]: row["n"] for row in c.execute("SELECT state,count(*) n FROM telegram_cleanup_jobs GROUP BY state")},
        "cleanup_failures": [dict(row) for row in c.execute(
            "SELECT j.message_id, j.error_code, j.reason, m.chat_name, m.chat_id, m.message_thread_id, m.telegram_message_id "
            "FROM telegram_cleanup_jobs j "
            "JOIN telegram_messages m ON m.id=j.message_id WHERE j.state='failed' ORDER BY j.created_at LIMIT 20")],
    }
