"""SQL for the native Chat metadata tables. Every function takes an open connection of a unit of work and never opens one.

The transaction owner (the backend, the arbiter or the Chat service) decides what is atomic; this module only states the
rows, the transitions and the counters once. Nothing here starts a process, reads a transcript or touches a terminal.
"""

from __future__ import annotations

import hashlib
import json

from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, new_id, utc_now

from .contracts import (FINAL_STATES, ORIGIN_OWNER, RUNTIME_PROVIDERS, STATE_SUBMITTED, STATE_SUBMITTING, STATE_UNCERTAIN,
                        STATE_UNDELIVERED, STATE_WAITING, SessionBinding, TYPE_CHAT)
from .projection import Lineage, ORIGIN_UNKNOWN

PAGE_LIMIT = 100
PREVIEW_CHARS = 180


def rows(cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


# ----- identity -------------------------------------------------------------------------------------------------------

def active_profile(conn, agent_id: str, paths=None, *, any_status: bool = False):
    """The active registered profile row of this exact agent in any scope (global or business), or None. ``any_status`` also
    returns a retired profile, for reading the conversations it captured while it was active."""
    status = "" if any_status else " AND p.status='active'"
    row = conn.execute("SELECT p.agent_id,p.name,p.workspace,p.profile_json,p.revision,p.home_business_id,b.slug AS business_slug "
                       "FROM agent_profiles p LEFT JOIN businesses b ON b.id=p.home_business_id "
                       "WHERE p.agent_id=?" + status, (agent_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    if paths is not None:
        from bazarlyk.composition.locations import agent_workspace_of
        # The registered identity and home scope select the workspace; the stored column never does.
        result["workspace"] = str(agent_workspace_of(paths, result["agent_id"], result["home_business_id"], result["business_slug"]))
    return result


def observed_agents(conn, paths) -> list[dict]:
    """Active registered agents the backend observes: every global agent and each business agent of an unarchived business."""
    live = active_business_ids(conn)
    from bazarlyk.composition.locations import agent_workspace_of
    result = []
    for row in conn.execute("SELECT p.agent_id,p.name,p.workspace,p.profile_json,p.revision,p.home_business_id,b.slug AS business_slug "
                            "FROM agent_profiles p LEFT JOIN businesses b ON b.id=p.home_business_id "
                            "WHERE p.status='active' ORDER BY p.agent_id"):
        if row["home_business_id"] is not None and row["home_business_id"] not in live:
            continue
        item = dict(row)
        item["workspace"] = str(agent_workspace_of(paths, item["agent_id"], item["home_business_id"], item["business_slug"]))
        result.append(item)
    return result


def profile_runtime(profile) -> str | None:
    data = json.loads(profile["profile_json"])
    return data.get("runtime") if data.get("runtime") in RUNTIME_PROVIDERS else None


def profile_label(profile) -> str:
    data = json.loads(profile["profile_json"])
    return data.get("display_name") or data.get("name") or profile["name"]


# ----- counters and the visible-change revision -----------------------------------------------------------------------

def ensure_counters(conn, agent_id: str):
    conn.execute("INSERT OR IGNORE INTO native_agent_counters(agent_id,updated_at) VALUES(?,?)", (agent_id, utc_now()))
    return conn.execute("SELECT * FROM native_agent_counters WHERE agent_id=?", (agent_id,)).fetchone()


def counters(conn, agent_id: str) -> dict:
    row = conn.execute("SELECT * FROM native_agent_counters WHERE agent_id=?", (agent_id,)).fetchone()
    return dict(row) if row else {"agent_id": agent_id, "session_generation": 0, "completion_sequence": 0, "input_sequence": 0,
                                  "work_sequence": 0, "user_sequence": 0, "revision": 0}


def bump(conn, agent_id: str) -> int:
    """Advance the agent's visible-change counter (the Chat stream position); every change a viewer can see calls this."""
    ensure_counters(conn, agent_id)
    return conn.execute("UPDATE native_agent_counters SET revision=revision+1,updated_at=? WHERE agent_id=? RETURNING revision",
                        (utc_now(), agent_id)).fetchone()[0]


def revision(conn, agent_id: str) -> int:
    row = conn.execute("SELECT revision FROM native_agent_counters WHERE agent_id=?", (agent_id,)).fetchone()
    return row[0] if row else 0


# ----- session bindings -----------------------------------------------------------------------------------------------

def binding_of(row) -> SessionBinding:
    return SessionBinding(id=row["id"], agent_id=row["agent_id"], provider=row["provider"], conversation_id=row["conversation_id"],
                          generation=row["generation"], transport=row["transport"], process_pid=row["process_pid"],
                          process_started=row["process_started"], owner=row["owner"], transcript_path=row["transcript_path"])


def get_session(conn, session_id: str):
    row = conn.execute("SELECT * FROM native_sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def public_bindings(conn, session_ids) -> dict[str, dict]:
    """The public binding of each given session, ended or not. A binding row is never deleted and an input names its session
    by an immutable foreign key, so an old input keeps resolving to the binding it was admitted for."""
    wanted = sorted({session for session in session_ids if session})
    if not wanted:
        return {}
    found = rows(conn.execute("SELECT * FROM native_sessions WHERE id IN (" + ",".join("?" * len(wanted)) + ")", wanted))
    return {row["id"]: binding_of(row).public() for row in found}


def latest_session(conn, agent_id: str):
    row = conn.execute("SELECT * FROM native_sessions WHERE agent_id=? ORDER BY generation DESC LIMIT 1", (agent_id,)).fetchone()
    return dict(row) if row else None


def live_session(conn, agent_id: str):
    """The agent's open binding: its newest one while it has not ended."""
    row = latest_session(conn, agent_id)
    return row if row and row["ended_at"] is None else None


def open_sessions(conn) -> list[dict]:
    return rows(conn.execute("SELECT * FROM native_sessions WHERE ended_at IS NULL ORDER BY agent_id"))


def unfinalized_sessions(conn) -> list[dict]:
    return rows(conn.execute("SELECT * FROM native_sessions WHERE ended_at IS NOT NULL AND final_captured_at IS NULL "
                             "ORDER BY agent_id,generation"))


def same_session(row, observation) -> bool:
    """Whether a saved binding is the observed one: same conversation and the same process (and, for Codex, thread writer).
    The transcript path is capture metadata, not identity: it may appear after the binding is made."""
    return (row["provider"] == observation.provider and row["conversation_id"] == observation.conversation_id
            and row["process_pid"] == observation.pid and row["process_started"] == observation.started
            and row["transport"] == observation.transport and row["owner"] == observation.owner)


def attach_transcript(conn, session: dict, path: str) -> None:
    """Record the transcript path of a binding that was first observed without one (capture became visible later)."""
    if session["transcript_path"] is None and path:
        conn.execute("UPDATE native_sessions SET transcript_path=? WHERE id=? AND transcript_path IS NULL", (path, session["id"]))


def create_session(conn, observation, now: str) -> dict:
    """A new binding for an observed conversation/process: the agent's generation advances by one."""
    ensure_counters(conn, observation.agent_id)
    generation = conn.execute("UPDATE native_agent_counters SET session_generation=session_generation+1,updated_at=? "
                              "WHERE agent_id=? RETURNING session_generation", (now, observation.agent_id)).fetchone()[0]
    identity = new_id()
    conn.execute("INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
                 "owner,transcript_path,observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, observation.agent_id, observation.provider, observation.conversation_id, generation,
                  observation.transport, observation.pid, observation.started, observation.owner, observation.transcript_path,
                  now, now))
    bump(conn, observation.agent_id)
    return get_session(conn, identity)


def touch_session(conn, session_id: str, now: str) -> None:
    conn.execute("UPDATE native_sessions SET verified_at=? WHERE id=? AND ended_at IS NULL", (now, session_id))


def end_session(conn, session_id: str, reason: str, now: str) -> bool:
    """Close a binding and settle its inputs: nothing that was waiting was typed, so it becomes undelivered; an input claimed
    but not settled may have been typed, so it becomes uncertain. Neither is ever replayed."""
    row = get_session(conn, session_id)
    if row is None or row["ended_at"] is not None:
        return False
    conn.execute("UPDATE native_sessions SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL", (now, reason[:120], session_id))
    for item in rows(conn.execute("SELECT * FROM native_inputs WHERE session_id=? AND state IN ('waiting','submitting') ORDER BY sequence",
                                  (session_id,))):
        if item["state"] == STATE_WAITING:
            settle_input(conn, item["id"], STATE_UNDELIVERED, "session_ended", {"session_end": reason[:120]}, (STATE_WAITING,))
        else:
            settle_input(conn, item["id"], STATE_UNCERTAIN, "session_ended_during_submit", {"session_end": reason[:120]},
                         (STATE_SUBMITTING,))
    bump(conn, row["agent_id"])
    return True


def mark_final_captured(conn, session_id: str, now: str) -> None:
    conn.execute("UPDATE native_sessions SET final_captured_at=? WHERE id=? AND final_captured_at IS NULL", (now, session_id))


# ----- capture cursors ------------------------------------------------------------------------------------------------

def get_capture(conn, agent_id: str, provider: str, conversation_id: str):
    row = conn.execute("SELECT * FROM native_captures WHERE agent_id=? AND provider=? AND conversation_id=?",
                       (agent_id, provider, conversation_id)).fetchone()
    return dict(row) if row else None


def ensure_capture(conn, session: dict) -> dict:
    now = utc_now()
    conn.execute("INSERT OR IGNORE INTO native_captures(agent_id,provider,conversation_id,transcript_path,session_id,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?)", (session["agent_id"], session["provider"], session["conversation_id"],
                                           session["transcript_path"], session["id"], now, now))
    return get_capture(conn, session["agent_id"], session["provider"], session["conversation_id"])


def capture_changes(capture: dict, **values) -> dict:
    """The part of ``values`` that is not already the stored state of ``capture``. The cursor state is compared as data, not as
    text, so a re-serialisation of the same state is no change. Nothing is written here."""
    changed = {}
    for name, value in values.items():
        if name == "index_state_json":
            try:
                same = json.loads(capture[name]) == json.loads(value)
            except ValueError:
                same = False
        else:
            same = capture[name] == value
        if not same:
            changed[name] = value
    return changed


def update_capture(conn, capture: dict, **values) -> None:
    values["updated_at"] = utc_now()
    names = list(values)
    conn.execute("UPDATE native_captures SET " + ",".join(f"{name}=?" for name in names)
                 + " WHERE agent_id=? AND provider=? AND conversation_id=?",
                 [values[name] for name in names] + [capture["agent_id"], capture["provider"], capture["conversation_id"]])


def agent_captures(conn, agent_id: str) -> list[dict]:
    return rows(conn.execute("SELECT * FROM native_captures WHERE agent_id=? ORDER BY created_at,conversation_id", (agent_id,)))


# ----- inputs ---------------------------------------------------------------------------------------------------------

def chat_source_key(principal_id: str, key: str) -> str:
    """The source key of an owner Chat input: the owner's own idempotency key, scoped to the owner."""
    return f"chat:{principal_id}:{key}"


def chat_key(row: dict) -> str | None:
    """The idempotency key the owner sent with a Chat input, or ``None`` for every other producer: their source keys (Telegram
    updates, inbox episodes, relay ids) are internal and are never shown."""
    if row["origin"] != ORIGIN_OWNER or row["input_type"] != TYPE_CHAT or not row["principal_id"]:
        return None
    prefix = chat_source_key(row["principal_id"], "")
    return row["source_key"][len(prefix):] if row["source_key"].startswith(prefix) else None


def request_hash(request) -> str:
    """What makes a repeated source key the same request: type, content and the explicit session pin, never the time. A private
    payload is represented by its length only, so nothing derived from its text is stored."""
    content = [len(request.text)] if request.sensitive else [request.body if request.body is not None else request.text, request.text]
    value = [request.input_type, request.source_key, *content, request.principal_id, request.expected_binding,
             bool(request.sensitive)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def get_input(conn, input_id: str):
    row = conn.execute("SELECT * FROM native_inputs WHERE id=?", (input_id,)).fetchone()
    return dict(row) if row else None


def input_by_key(conn, agent_id: str, source_key: str):
    """The newest attempt for a source key (an attempt that never typed anything may be followed by another)."""
    row = conn.execute("SELECT * FROM native_inputs WHERE agent_id=? AND source_key=? ORDER BY attempt DESC LIMIT 1",
                       (agent_id, source_key)).fetchone()
    return dict(row) if row else None


def insert_input(conn, session: dict, request, digest: str, *, state: str, marker: str | None = None, reason: str | None = None,
                 attempt: int = 1) -> dict:
    ensure_counters(conn, request.agent_id)
    sequence = conn.execute("UPDATE native_agent_counters SET input_sequence=input_sequence+1,updated_at=? WHERE agent_id=? "
                            "RETURNING input_sequence", (utc_now(), request.agent_id)).fetchone()[0]
    identity, now = new_id(), utc_now()
    body = None if request.sensitive else (request.body if request.body is not None else request.text)
    conn.execute("INSERT INTO native_inputs(id,agent_id,session_id,origin,input_type,source_key,attempt,request_hash,principal_id,body,"
                 "sensitive,steer,marker,metadata_json,state,reason,sequence,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, request.agent_id, session["id"], request.origin, request.input_type, request.source_key, attempt, digest,
                  request.principal_id, body, 1 if request.sensitive else 0, 1 if request.steer else 0, marker,
                  json.dumps(dict(request.metadata), separators=(",", ":"), sort_keys=True), state, reason, sequence, now, now))
    record_input_event(conn, identity, state, reason, {})
    bump(conn, request.agent_id)
    return get_input(conn, identity)


def record_input_event(conn, input_id: str, state: str, reason: str | None, evidence: dict) -> None:
    conn.execute("INSERT INTO native_input_events(input_id,state,reason,evidence_json,created_at) VALUES(?,?,?,?,?)",
                 (input_id, state, (reason or "")[:200] or None, json.dumps(evidence, separators=(",", ":"), sort_keys=True), utc_now()))


def settle_input(conn, input_id: str, state: str, reason: str | None, evidence: dict, allowed: tuple[str, ...]) -> bool:
    """Move one input to ``state`` only from a state in ``allowed`` (a compare-and-set); a final delivery scrubs its text,
    because from then on the native conversation is the only copy of what was said."""
    now = utc_now()
    scrub = ", body=NULL" if state == STATE_SUBMITTED else ""
    done = conn.execute("UPDATE native_inputs SET state=?,reason=?,evidence_json=?,updated_at=?,settled_at=?" + scrub
                        + " WHERE id=? AND state IN (" + ",".join("?" * len(allowed)) + ")",
                        (state, (reason or "")[:200] or None, json.dumps(evidence, separators=(",", ":"), sort_keys=True), now,
                         now if state in FINAL_STATES else None, input_id, *allowed)).rowcount == 1
    if done:
        record_input_event(conn, input_id, state, reason, evidence)
        agent, input_type = conn.execute("SELECT agent_id,input_type FROM native_inputs WHERE id=?", (input_id,)).fetchone()
        if state == STATE_SUBMITTED and input_type == TYPE_CHAT:
            # A direct Chat input the transport positively delivered: the inbox nudge's user-work checkpoint counter.
            conn.execute("UPDATE native_agent_counters SET user_sequence=user_sequence+1 WHERE agent_id=?", (agent,))
        bump(conn, agent)
    return done


def claim_input(conn, row: dict) -> bool:
    """The durable crash boundary: waiting to submitting, only while no earlier waiting input of the agent is ahead of it.
    From here this code never types the input again."""
    ahead = conn.execute("SELECT 1 FROM native_inputs WHERE agent_id=? AND state IN ('waiting','submitting') AND sequence<? LIMIT 1",
                         (row["agent_id"], row["sequence"])).fetchone()
    if ahead:
        return False
    return settle_input(conn, row["id"], STATE_SUBMITTING, "claimed", {}, (STATE_WAITING,))


def waiting_inputs(conn, agent_id: str) -> list[dict]:
    return rows(conn.execute("SELECT * FROM native_inputs WHERE agent_id=? AND state='waiting' ORDER BY sequence", (agent_id,)))


def recover_claimed(conn) -> int:
    """After a restart a claimed input may have been typed: it becomes uncertain and is never retyped."""
    count = 0
    for row in rows(conn.execute("SELECT * FROM native_inputs WHERE state='submitting'")):
        count += settle_input(conn, row["id"], STATE_UNCERTAIN, "service_restarted", {}, (STATE_SUBMITTING,))
    return count


def recent_inputs(conn, agent_id: str, limit: int = 30) -> list[dict]:
    """The agent's latest ``limit`` inputs, newest first (descending sequence)."""
    return rows(conn.execute("SELECT * FROM native_inputs WHERE agent_id=? ORDER BY sequence DESC LIMIT ?", (agent_id, limit)))


def conversation_markers(conn, agent_id: str, provider: str, conversation_id: str, markers,
                         transcript_path: str | None = None) -> dict[str, dict]:
    """The system inputs, among the given marker ids, that were admitted for exactly this agent's conversation. The lookup is
    bounded by the markers a page actually shows (a unique index), never by how many inputs came later, and an input admitted
    for another conversation, agent or provider is not found: a marker copied there proves nothing. A binding that names a
    transcript source must name this conversation's source."""
    wanted = sorted({marker for marker in markers if marker})
    if not wanted:
        return {}
    found = rows(conn.execute(
        "SELECT i.* FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id "
        "WHERE i.origin='system' AND i.agent_id=? AND s.agent_id=? AND s.provider=? AND s.conversation_id=? "
        "AND (s.transcript_path IS NULL OR s.transcript_path=?) AND i.marker IN (" + ",".join("?" * len(wanted)) + ")",
        (agent_id, agent_id, provider, conversation_id, transcript_path, *wanted)))
    return {row["marker"]: row for row in found}


def input_events(conn, input_id: str) -> list[dict]:
    return rows(conn.execute("SELECT state,reason,evidence_json,created_at FROM native_input_events WHERE input_id=? ORDER BY id",
                             (input_id,)))


def input_view(row: dict, binding: dict | None) -> dict:
    """One input as the owner sees it: a delivery receipt, never a second transcript. ``binding`` is the public binding of the
    session it was admitted for (see :func:`public_bindings`), kept after that session ends. Text is present only while the
    native conversation does not yet hold it; the idempotency key only for the owner's own Chat inputs."""
    evidence = json.loads(row["evidence_json"])
    # Preserve the immutable transport outcome; later archive evidence proves arrival for Chat.
    confirmed = (row["state"] == STATE_UNCERTAIN and row["input_type"] == "telegram"
                 and row["reason"] == "transcript_confirmed" and evidence.get("native_input_observed") is True)
    return {"id": row["id"], "kind": "input", "sequence": row["sequence"], "origin": row["origin"], "input_type": row["input_type"],
            "state": STATE_SUBMITTED if confirmed else row["state"], "transport_state": row["state"], "reason": row["reason"], "body": row["body"], "session_id": row["session_id"],
            "binding": binding, "idempotency_key": chat_key(row),
            "evidence": evidence, "created_at": row["created_at"], "settled_at": row["settled_at"]}


# ----- reply completions ----------------------------------------------------------------------------------------------

def insert_reply(conn, *, lineage: Lineage, kind: str, key: str, session_id: str | None, receipt_id: str, offset: int, length: int,
                 completed_at: str, preview: str, origin: str = ORIGIN_UNKNOWN, user_work: bool = False):
    """One completion per logical reply of one conversation lineage. Returns the row, or ``None`` when that reply is already
    completed. The same native id or raw offset in another conversation is a different reply. ``origin`` is who started the turn;
    ``user_work`` is whether any genuine, non-notice user input took part in it, and only that advances the agent's work counter:
    a reply to a nudge the owner then steered is work, a nudge's or another system input's own reply never is."""
    identity = lineage.reply_id(kind, key)
    if conn.execute("SELECT 1 FROM native_replies WHERE id=?", (identity,)).fetchone():
        return None
    ensure_counters(conn, lineage.agent_id)
    sequence = conn.execute("UPDATE native_agent_counters SET completion_sequence=completion_sequence+1,work_sequence=work_sequence+?,"
                            "updated_at=? WHERE agent_id=? RETURNING completion_sequence",
                            (1 if user_work else 0, utc_now(), lineage.agent_id)).fetchone()[0]
    conn.execute("INSERT INTO native_replies(id,agent_id,provider,conversation_id,archive_id,identity_kind,identity_key,"
                 "completion_sequence,session_id,receipt_id,raw_offset,raw_length,completed_at,indexed_at,preview,origin,user_work) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, lineage.agent_id, lineage.provider, lineage.conversation_id, lineage.archive_id, kind, key, sequence,
                  session_id, receipt_id, offset, length, completed_at, utc_now(), preview[:PREVIEW_CHARS], origin,
                  1 if user_work else 0))
    bump(conn, lineage.agent_id)
    return dict(conn.execute("SELECT * FROM native_replies WHERE id=?", (identity,)).fetchone())


def reply_by_sequence(conn, agent_id: str, sequence: int):
    row = conn.execute("SELECT * FROM native_replies WHERE agent_id=? AND completion_sequence=?", (agent_id, sequence)).fetchone()
    return dict(row) if row else None


def replies_at(conn, agent_id: str, provider: str, conversation_id: str, offsets) -> list[dict]:
    """The completions of one conversation whose completing record starts at one of ``offsets`` of its archive payload."""
    wanted = sorted(set(offsets))
    if not wanted:
        return []
    return rows(conn.execute("SELECT * FROM native_replies WHERE agent_id=? AND provider=? AND conversation_id=? AND raw_offset IN ("
                             + ",".join("?" * len(wanted)) + ")", (agent_id, provider, conversation_id, *wanted)))


def agent_replies(conn, agent_id: str, *, after: int = 0, limit: int = PAGE_LIMIT) -> list[dict]:
    return rows(conn.execute("SELECT * FROM native_replies WHERE agent_id=? AND completion_sequence>? ORDER BY completion_sequence LIMIT ?",
                             (agent_id, after, limit)))


# ----- owner read boundary --------------------------------------------------------------------------------------------

def last_read(conn, agent_id: str, principal_id: str) -> int:
    row = conn.execute("SELECT last_read_sequence FROM native_reads WHERE agent_id=? AND principal_id=?", (agent_id, principal_id)).fetchone()
    return row[0] if row else 0


def mark_read(conn, agent_id: str, principal_id: str, sequence: int) -> int:
    """Move the owner's boundary forward to an existing completion. A sequence that is no completion of this agent is refused;
    a boundary behind the saved one changes nothing."""
    if sequence != 0 and reply_by_sequence(conn, agent_id, sequence) is None:
        raise DomainError("invalid_read_boundary", "That is not a completed reply of this agent")
    current = last_read(conn, agent_id, principal_id)
    if sequence > current:
        conn.execute("INSERT INTO native_reads(agent_id,principal_id,last_read_sequence,updated_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(agent_id,principal_id) DO UPDATE SET last_read_sequence=excluded.last_read_sequence,"
                     "updated_at=excluded.updated_at WHERE excluded.last_read_sequence>last_read_sequence",
                     (agent_id, principal_id, sequence, utc_now()))
        bump(conn, agent_id)
    return max(current, sequence)


def unread_replies(conn, principal_id: str) -> list[dict]:
    """The newest completed reply of each active agent that is newer than the owner's boundary, with the agent's scope."""
    return rows(conn.execute(
        """SELECT r.*,a.home_business_id,a.profile_json,a.name AS agent_name FROM native_replies r
        JOIN agent_profiles a ON a.agent_id=r.agent_id
        LEFT JOIN native_reads n ON n.agent_id=r.agent_id AND n.principal_id=?
        WHERE a.status='active' AND r.completion_sequence>COALESCE(n.last_read_sequence,0)
        AND r.completion_sequence=(SELECT MAX(l.completion_sequence) FROM native_replies l WHERE l.agent_id=r.agent_id)
        ORDER BY r.completed_at,r.id""", (principal_id,)))


def newest_sequence(conn, agent_id: str) -> int:
    return counters(conn, agent_id)["completion_sequence"]

