"""Hooks and nudges of extensions: the entries of an extension's ``plugin/hooks.yaml`` as data (contract section 8).

No extension code runs. An entry says at which point (``start``, ``owner_message``, ``after_tool``, ``stop``, ``timed``)
the agent is told a text. This module finds the entries of the extensions in effect for an agent, reading them from the
agentic layer's copy of each plugin part (never the extension's own folder), decides whether an entry's conditions hold,
composes the texts for the room a point has, records each delivery and says when a timed entry is due.

Where each point is served: ``start`` and ``owner_message`` by the start hook (``bootstrap/claude_session.py``), which gets
the texts with the bootstrap answer; ``after_tool`` by the MCP server (``transport/mcp.py``) as a note beside the result;
``stop`` by the completion checkpoint (``agents/checkpoints.py``). With no extension in effect for the agent, each point
asks the registry once and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import uuid
import weakref
from typing import Callable, Iterable

from qorqut import extension_contract
from qorqut.composition.extension_deliveries import TABLE
from qorqut_agentic.behavior.instructions import EXTENSION_REST_FILE as REST_FILE, extension_notes

__all__ = ["Composition", "Entry", "TimedNudges", "applies", "compose", "due", "entries_for", "read_entries", "record", "stop_issues",
           "tool_notes", "watched_tools"]

LOGGER = logging.getLogger(__name__)
STOP_CODE = "extension_stop"
# The key beside a tool's result that carries the notes of extensions; the result itself is never changed.
NOTES_KEY = "extension_notes"
# What one row of ``agent_checkpoint_findings`` holds in ``message`` (its CHECK in ``composition/schema_ddl.py``). A stop
# text whose framed form is longer is stored as the sentence pointing to the stop rest file and is always written there.
FINDING_MESSAGE_LIMIT = 1000

Reader = Callable[[str], "bytes | None"]


@dataclass(frozen=True)
class Entry:
    extension_id: str
    extension_name: str
    entry_id: str
    at: str
    tools: tuple[str, ...] = ()
    every_seconds: int | None = None
    if_used: tuple[str, ...] = ()  # empty: the entry has no such condition
    unless_used: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class Composition:
    text: str                    # Qorqbot's text, the shown texts and, with a rest, the pointing sentence
    rest: str | None             # the content for ``rest_file``, or None when everything fit
    rest_file: str | None
    delivered: tuple[Entry, ...]  # shown in ``text``
    spilled: tuple[Entry, ...]    # written to ``rest_file``


def read_entries(extension_id: str, extension_name: str, read: Reader) -> tuple[tuple[Entry, ...], list[str]]:
    """The entries of one extension's ``hooks.yaml`` in file order and the checker's findings, read through ``read`` (see
    ``extension_contract.read_hooks``). An entry with a finding is left out by the checker."""
    items, findings = extension_contract.read_hooks(read, extension_id=extension_id)
    return tuple(Entry(extension_id=extension_id, extension_name=extension_name, entry_id=item["id"], at=item["at"],
                       tools=tuple(item["tools"]), every_seconds=item["every_seconds"], if_used=tuple(item["if_used"]),
                       unless_used=tuple(item["unless_used"]), text=item["text"].strip())
                 for item in items), findings


# The parsed entries of each extension, per registry: ``{ext_id: (key, entries)}``. ``key`` is the extension's version and
# layer package revision with the layer's change marker, so a changed copy is read again and an unchanged one never is.
_PARSED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _change_marker(registry):
    """The agentic layer's cheap marker of authored change (``CatalogAPI.change_marker``), or None when it cannot be read
    (then nothing is reused)."""
    catalog = getattr(getattr(registry, "layer", None), "catalog", None)
    try:
        return catalog.change_marker() if catalog is not None else None
    except Exception:
        return None


def entries_for(registry, agent_id: str, point: str | None = None, *, connection=None, role=None) -> tuple[Entry, ...]:
    """The entries at ``point`` (every point when None) of each extension in effect for the agent, by extension id and
    then file order. Each is read from the agentic layer's copy of the plugin part and kept until that copy changes. A copy
    that breaks a hook rule gives no entries; the registry is told (``refuse``), so it shows as needing attention, and one
    log line says why. With no extension found at start nothing is read at all; with none in effect, only the registry."""
    if registry is None or not registry.found:
        return ()
    names = {item.id: item.manifest.name for item in registry.found}
    effect = registry.in_effect(agent_id, connection=connection, role=role)
    if not effect:
        return ()
    marker = _change_marker(registry)
    parsed = _PARSED.setdefault(registry, {})
    found = []
    for ext_id, version, revision in effect:
        key = (version, revision, marker)
        cached = parsed.get(ext_id)
        if marker is not None and cached is not None and cached[0] == key:
            entries = cached[1]
        else:
            entries, findings = read_entries(ext_id, names.get(ext_id, ext_id),
                                             lambda path, ext_id=ext_id: registry.plugin_file(ext_id, path))
            if findings:
                LOGGER.warning("The hooks of extension %s in the agentic layer break the contract, so none is used: %s",
                               ext_id, findings[0])
                entries = ()
                refuse = getattr(registry, "refuse", None)
                if refuse is not None:
                    refuse(ext_id, findings[0])
            parsed[ext_id] = (key, entries)
        found.extend(entry for entry in entries if point is None or entry.at == point)
    return tuple(found)


def watched_tools(entries: Iterable[Entry]) -> list[str]:
    """Every tool named in a condition of ``entries``: what the activity hook notes per turn."""
    return sorted({name for entry in entries for name in (*entry.if_used, *entry.unless_used)})


def needs_owner_turn(entries: Iterable[Entry]) -> bool:
    """Whether the activity hook must note how each turn began: only a ``stop`` entry is held by that."""
    return any(entry.at == "stop" for entry in entries)


def applies(entry: Entry, used: Iterable[str] | None) -> bool:
    """Whether the entry's conditions hold for the tools ``used`` in this turn. ``None`` means not known: an entry with a
    condition does not apply then."""
    if not entry.if_used and not entry.unless_used:
        return True
    if used is None:
        return False
    used = set(used)
    if entry.if_used and not used.intersection(entry.if_used):
        return False
    return not used.intersection(entry.unless_used)


def compose(own: str, entries: Iterable[Entry], limit: int, instructions, point: str) -> Composition:
    """Qorqbot's ``own`` text at ``point`` followed by the texts of ``entries`` in order, within ``limit``. What does not fit
    goes to the point's rest file, and the text ends with the sentence that points to it; that sentence is always
    delivered, and ``own`` is cut further only when it needs the room."""
    entries = tuple(entries)
    rest_file = REST_FILE.format(point=point)
    text, rest, count = extension_notes(instructions, own, [(entry.extension_name, entry.text) for entry in entries],
                                        limit, rest_file)
    return Composition(text=text, rest=rest, rest_file=rest_file if rest is not None else None,
                       delivered=entries[:count], spilled=entries[count:])


def deliveries(composition: Composition) -> list[dict]:
    """What a composition delivered, as the record takes it: the shown entries, then those in the rest file."""
    return ([{"extension_id": e.extension_id, "entry_id": e.entry_id, "text": e.text, "place": "shown"}
             for e in composition.delivered]
            + [{"extension_id": e.extension_id, "entry_id": e.entry_id, "text": e.text, "place": "rest_file"}
               for e in composition.spilled])


def hook_entries(entries: Iterable[Entry]) -> list[dict]:
    """Entries as the start hook gets them with the bootstrap answer."""
    return [{"extension_id": e.extension_id, "entry_id": e.entry_id, "name": e.extension_name, "text": e.text}
            for e in entries]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(connection, agent_id: str, point: str, delivered: Iterable[dict], *, at: str, session_id=None, turn_id=None,
           request_id=None, native_input_id=None) -> int:
    """Insert one row per delivered text (``{"extension_id", "entry_id", "place"}`` and ``"text"`` or
    ``"text_sha256"``). The caller owns the transaction. Returns how many rows were written."""
    count = 0
    for item in delivered:
        digest = item.get("text_sha256") or text_digest(item["text"])
        connection.execute(
            f"INSERT INTO {TABLE}(id,agent_id,extension_id,entry_id,point,place,session_id,turn_id,request_id,"
            "native_input_id,text_sha256,delivered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), agent_id, item["extension_id"], item["entry_id"], point, item["place"], session_id,
             turn_id, request_id, native_input_id, digest, at))
        count += 1
    return count


def write_rest(workspace: Path, composition: Composition) -> None:
    """Write a composition's rest into the agent's workspace, when it has one."""
    if composition.rest is None:
        return
    from qorqut.bootstrap.client import write_text_private
    write_text_private(Path(workspace) / composition.rest_file, composition.rest)


# ----- after_tool -----------------------------------------------------------------------------------------------------

def tool_entries(entries: Iterable[Entry], names: Iterable[str]) -> tuple[Entry, ...]:
    """The ``after_tool`` entries that name one of ``names`` (the tool as called, and its canonical operation)."""
    names = {name for name in names if name}
    return tuple(entry for entry in entries if entry.at == "after_tool" and names.intersection(entry.tools))


def tool_notes(value, entries: Iterable[Entry], used, instructions, *, budget: int, size: Callable[[dict], int]):
    """A bounded tool result with the notes of the applicable ``entries`` beside it, and the composition (None when no note
    was added). Only an ``ok`` result gets notes. ``size(value)`` measures a result as it is sent; the notes go into what
    ``budget`` leaves after the result, which is never changed or cut."""
    if not isinstance(value, dict) or value.get("status") != "ok":
        return value, None
    applicable = [entry for entry in entries if applies(entry, used)]
    if not applicable:
        return value, None
    room = budget - size(value) - len(json.dumps(NOTES_KEY)) - 4  # the key, its colon and quotes, the comma
    composition = compose("", applicable, room, instructions, "after_tool")
    return {**value, NOTES_KEY: composition.text}, composition


# ----- stop -----------------------------------------------------------------------------------------------------------

def stop_issues(entries: Iterable[Entry], native: dict | None, instructions) -> dict:
    """The checkpoint issues of the ``stop`` entries that apply to this turn: none unless a message started the turn
    (``owner_turn``), and the conditions are judged from the tools used in the turn. Each is claimed once per root turn."""
    if not native or native.get("owner_turn") is not True:
        return {}
    issues = {}
    for entry in entries:
        if entry.at != "stop" or not applies(entry, native.get("used")):
            continue
        key = f"extension:{entry.extension_id}:{entry.entry_id}:{native['root_turn_id']}"
        framed = instructions.render("extension-hook-text", NAME=entry.extension_name, TEXT=entry.text)
        spilled = len(framed) > FINDING_MESSAGE_LIMIT
        # The row stores what it can hold: the framed text, or for a longer one the pointing sentence; the agent reads the
        # correction composed from ``text`` (``spilled`` texts go to the rest file the sentence names).
        issues[key] = {"code": STOP_CODE, "project_id": None, "task_id": None, "spilled": spilled,
                       "message": (instructions.render("extension-hook-rest", FILE=REST_FILE.format(point="stop"))
                                   if spilled else framed),
                       "extension_id": entry.extension_id, "entry_id": entry.entry_id,
                       "extension_name": entry.extension_name, "text": entry.text}
    return issues


# ----- timed ----------------------------------------------------------------------------------------------------------

def due(entry: Entry, anchor: datetime, now: datetime) -> bool:
    """Whether a timed entry is due. ``anchor`` is its last delivery to the agent, or the time the extension came into
    effect for the agent when it was not delivered since, so the first nudge comes one ``every`` after that."""
    return entry.at == "timed" and entry.every_seconds is not None and now - anchor >= timedelta(seconds=entry.every_seconds)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


FLIPS = frozenset({"status", "switch", "plugin"})  # registry ``Change.what`` values whose ``affected`` agents flipped


class TimedNudges:
    """The ``timed`` entries of the extensions in effect for each agent, sent from the inbox nudge pass
    (``native_chat/nudges.py`` ``run_once``) the way Qorqbot's own reminders are: one system input typed behind the marker
    through the input arbiter, on either CLI, waking an asleep agent through the session opener. An agent that is paused,
    stopped or retired gets nothing, and the turn the nudge starts is not a message turn.

    The anchor of an entry is its newest ``extension_deliveries`` row for the agent: a delivery, or the ``in_effect`` row
    written the first time the pass sees the entry in effect, so the first nudge comes one ``every`` later. When the
    registry says an extension came into effect for an agent again (its change feed, which ``sync`` also feeds with what
    changed since the last process), a new ``in_effect`` row is written, so it starts again one ``every`` after that."""

    def __init__(self, runtime, chat, *, clock=None):
        # ``chat``: the nudge pass's session probe, session registry, input arbiter and session opener (``probe``,
        # ``registry``, ``arbiter``, ``opener``), the ones every other nudge uses.
        self.runtime, self.chat = runtime, chat
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._back: dict[tuple[str, str], datetime] = {}  # (agent, extension) -> when it came into effect again
        registry = getattr(runtime, "extension_registry", None)
        if registry is not None and registry.found:
            registry.subscribe(self._changed)

    def _changed(self, changes) -> None:
        """The registry's change feed: only remember which agents' extensions changed (it must only schedule work)."""
        now = self.clock()
        for change in changes:
            # ``affected`` lists the agents for which the extension came into or went out of effect; ``sync`` reports an
            # agent it stayed in effect for while its package changed as ``updated``, and a plugin copy that changed
            # nothing in effect has no agents. Only a flip counts: the pass restarts the clock when the extension is in
            # effect after it, which means it came (back) into effect.
            if change.what in FLIPS:
                for agent_id in change.affected:
                    self._back[(agent_id, change.extension)] = now

    def send_due(self, profile: dict) -> str:
        """Send the agent's due ``timed`` texts as one nudge, or wake the agent for them. Returns what happened."""
        registry = getattr(self.runtime, "extension_registry", None)
        if registry is None or not registry.found:
            return "none"
        agent_id = profile["agent_id"]
        entries = entries_for(registry, agent_id, "timed")
        back = {ext: self._back.pop((agent_id, ext)) for agent, ext in list(self._back) if agent == agent_id}
        if not entries:
            return "none"
        from qorqut.native_chat import store
        now = self.clock()
        with self.runtime.database.read() as uow:
            last = {(row["extension_id"], row["entry_id"]): row["at"] for row in uow.connection.execute(
                f"SELECT extension_id,entry_id,max(delivered_at) AS at FROM {TABLE} WHERE agent_id=? AND point='timed' "
                "GROUP BY extension_id,entry_id", (agent_id,))}
            owner = uow.connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?",
                                           (agent_id,)).fetchone()
            waiting = bool(store.waiting_inputs(uow.connection, agent_id))
        starting = [entry for entry in entries if (entry.extension_id, entry.entry_id) not in last
                    or (entry.extension_id in back and _moment(last[(entry.extension_id, entry.entry_id)])
                        < back[entry.extension_id])]
        if starting:
            with self.runtime.database.transaction(operation="extension_hooks.timed_anchor") as uow:
                self.runtime.application.ensure_writes_open(uow)
                for entry in starting:
                    at = back.get(entry.extension_id, now)
                    record(uow.connection, agent_id, "timed", [{"extension_id": entry.extension_id,
                           "entry_id": entry.entry_id, "text": entry.text, "place": "in_effect"}], at=_stamp(at))
                    last[(entry.extension_id, entry.entry_id)] = _stamp(at)
        ready = [entry for entry in entries if due(entry, _moment(last[(entry.extension_id, entry.entry_id)]), now)]
        if not ready:
            return "not_due"
        if owner is not None and owner["desired_state"] in {"paused", "stopped"}:
            return "desired_" + owner["desired_state"]
        if waiting:
            return "chat_input_first"
        chat = self.chat
        observed = chat.probe.observe(profile)
        reconciled = chat.registry.reconcile(profile, observed)
        if not reconciled.live or reconciled.session is None:
            opener = getattr(chat, "opener", None)
            if opener is not None:
                opener.wake_quietly(profile, "extension")
                return "waking"
            return "asleep"
        from qorqut.native_chat.contracts import (MAX_TEXT_CHARS, STATE_SUBMITTED, STATE_SUBMITTING, STATE_UNCERTAIN,
                                                  STATE_WAITING)
        instructions = self.runtime.resolver.require_agentic_view().runtime_instructions()
        composition = compose("", ready, MAX_TEXT_CHARS, instructions, "timed")
        # One due slot (the entries and their anchors) may take several inputs: one whose send ended ``failed`` or
        # ``undelivered`` never reached the agent, so the next pass sends again under the slot's next key; one that was
        # submitted (or may have been) is the delivery and nothing more is sent for the slot.
        slot = "extension-timed:" + hashlib.sha256(json.dumps(sorted(
            [e.extension_id, e.entry_id, last[(e.extension_id, e.entry_id)]] for e in ready)).encode()).hexdigest()[:32] + ":"
        with self.runtime.database.read() as uow:
            earlier = {row["source_key"]: dict(row) for row in uow.connection.execute(
                "SELECT source_key,state,id,session_id,settled_at,updated_at FROM native_inputs WHERE agent_id=? "
                "AND substr(source_key,1,?)=? ORDER BY attempt", (agent_id, len(slot), slot))}
        tries = 0
        for key, row in sorted(earlier.items(), key=lambda item: int(item[0].rsplit(":", 1)[1])):
            if row["state"] in (STATE_SUBMITTED, STATE_UNCERTAIN):
                self._delivered(agent_id, composition, now, None, row["id"])
                return row["state"]
            if row["state"] in (STATE_WAITING, STATE_SUBMITTING):
                return "in_flight"
            tries = int(key.rsplit(":", 1)[1]) + 1
        # Paced as an inbox nudge that was not typed into the same session is (``inbox_nudges.state._retry``): the
        # agent's latest timed inputs, newest first, that failed in this session make the streak; the wait doubles from
        # the failed base with each, up to the cap, from the latest one's end. A new session is tried at once.
        from qorqut_operations.inbox_nudges.contracts import FAILED
        from qorqut_operations.inbox_nudges.state import TRAILING, retry_delay
        with self.runtime.database.read() as uow:
            trailing = [dict(row) for row in uow.connection.execute(
                "SELECT state,session_id,settled_at,updated_at FROM native_inputs WHERE agent_id=? "
                "AND substr(source_key,1,16)='extension-timed:' ORDER BY sequence DESC LIMIT ?", (agent_id, TRAILING))]
        streak = 0
        for row in trailing:
            if row["state"] not in ("failed", "undelivered") or row["session_id"] != reconciled.session["id"]:
                break
            streak += 1
        if streak:
            settled = _moment(trailing[0]["settled_at"] or trailing[0]["updated_at"])
            if now < settled + timedelta(seconds=retry_delay(FAILED, streak)):
                return "retry_later"
        from qorqut.composition.locations import agent_workspace_of
        write_rest(agent_workspace_of(self.runtime.paths, agent_id), composition)
        result = chat.arbiter.admit_nudge(agent_id, slot + str(tries), composition.text,
                                          expected_binding=reconciled.session["id"], metadata={"extension_timed": slot})
        if result.state not in (STATE_SUBMITTED, STATE_UNCERTAIN):
            return result.state
        self._delivered(agent_id, composition, now, reconciled.session.get("conversation_id"), result.input_id)
        return result.state

    def _delivered(self, agent_id, composition, now, session_id, input_id) -> None:
        with self.runtime.database.transaction(operation="extension_hooks.timed_delivered") as uow:
            self.runtime.application.ensure_writes_open(uow)
            record(uow.connection, agent_id, "timed", deliveries(composition), at=_stamp(now), session_id=session_id,
                   native_input_id=input_id)
