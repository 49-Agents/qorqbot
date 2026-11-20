"""Qorqut's own compaction of the coordinator's conversation, in place of the client's.

When the conversation's context reaches the limit (``limit``: 300,000 tokens, or less for a model whose own window is
small) the coordinator is told to stop and file its handoff (``domains/compaction_handoff.py``). Once it is
filed, the conversation is closed and a fresh one is opened. The fresh session's own role command gives the agent its
instructions again; its next message is the opening: the owner's messages in their exact words with where each stands by the
filing, where the agent stopped, and the last messages of the closed conversation word for word. No model summarises anything
and no instruction is carried over by this code.

``step`` runs on every pass of the native Chat backend. It never waits and never raises: it reads the host's state, moves the
one flow one stage, and gives the only slow part (closing the host and opening the next conversation) to a thread of its own.

    watching -> asked -> filed, or asked once more, or given up -> replacing -> opened

The row of ``compactions`` that waits to be opened is the whole durable state. The flow in memory is dropped whenever the
conversation it belongs to is no longer the one the host runs (a restart of the service, an idle close, a crash, the owner
ending the session); when that same conversation is seen running again, the flow continues from its row.

What the two messages are to the client: plain user messages handed to the host directly, like the wake notice
(``opener.WAKE_TEXT``). Both start with an opening tag, so Chat does not show them as something the owner said and the index of
the owner's own turns never holds them.

``QORQUT_OWN_COMPACTION`` in the service's environment says whether this runs (``mode_by_environment``): unset, it does
nothing; ``1`` turns it on and leaves the client's own compaction in place behind it; ``only`` turns it on and has the
coordinator's session started with the client's automatic compaction switched off (``replaces_client``, which the managed
runtime asks at each start). Only the configured coordinator's conversation is compacted, and only when its client reports what
the context holds (``usage`` in the host's status). It runs for a Claude Code session only: the request, the last messages and
the switch for the client's own compaction were made and checked there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.composition.compactions import TABLE
from qorqut.management.identity import configured_ordubashy

from . import store

LOG = logging.getLogger(__name__)

ENVIRONMENT = "QORQUT_OWN_COMPACTION"
# The owner's choice (2026-11-19). Claude Code's own compaction of this conversation has come at about 330,000 (its
# ``autoCompactWindow`` of 360,000); the model's own limit is far above both.
LIMIT_TOKENS = 300_000
FULL_PERCENT = 70
LEFT_TOKENS = 45_000
REASON = "context"
CLIENT = "claude"              # ``managed_runtime.clients.CLAUDE``: the one client whose conversations are compacted here
CHECK_SECONDS = 5.0            # how often the host is read while nothing is going on
MAX_ASKS = 2                   # the request, and once more in a turn of its own when the first turn ended without a filing
ASK_SECONDS = 15 * 60.0        # a request that is neither filed nor finished after this long is given up on
STOP_SECONDS = 120.0           # how long the agent has to end its turn once the handoff is filed (or given up on)
CLOSE_SECONDS = 45.0
OPEN_TRIES = 3
RETRY_SECONDS = 30.0
REQUEST_TAG, OPENING_TAG = "qorqut-handoff-request", "qorqut-handoff"
AGAIN = ("Your turn ended and no handoff was filed. File it now with compaction_handoff_file, before anything else: this "
         "conversation is closed after this turn whether or not it is filed.\n\n")
TAIL_EVENTS, TAIL_RESULT, TAIL_CALL, TAIL_CAP, TAIL_BYTES = 12, 1_000, 300, 8_000, 1 << 20
TAIL_LABELS = {"owner": "OWNER", "agent": "YOU", "call": "TOOL CALL", "result": "TOOL RESULT"}
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|passcode|cvv|cvc)\b(\s*(?:is|:|=|-)?\s*)\S+")
_OPENING_TAG = re.compile(r"\s*<[A-Za-z]")
# User records that are the client's own notes, not something said to the agent.
_CLIENT_NOTES = ("[Request interrupted", "Caveat:", "This session is being continued", "[Your previous response")


OFF, ON, ONLY = "off", "on", "only"


def mode_by_environment() -> str:
    """``off`` (unset), ``on`` (the client's own compaction stays behind this one) or ``only`` (it is switched off)."""
    value = os.environ.get(ENVIRONMENT, "").strip().lower()
    return ONLY if value == ONLY else ON if value in ("1", "true", "yes", "on") else OFF


def limit(window=None) -> int:
    """How many tokens the context may hold before the handoff is asked for: ``LIMIT_TOKENS``, or less for a model whose own
    window (``window``, when the client has reported it) is small: the earlier of 70% of it and 45,000 tokens before its end."""
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return LIMIT_TOKENS
    return max(1, min(LIMIT_TOKENS, window * FULL_PERCENT // 100, window - LEFT_TOKENS))


def wrapped(tag: str, text: str) -> str:
    return f"<{tag}>\n{text}\n</{tag}>"


def scrub(text: str) -> str:
    """Long digit runs (card numbers) and values that follow a password word never travel into the next conversation."""
    return _SECRET.sub(r"\1\2[removed]", _CARD.sub("[number removed]", text))


def _said(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _events(record: dict):
    """The events of one Claude Code transcript record: ``(kind, text)`` with kind owner, agent, call, result or request (the
    handoff request itself). Subagent records, the client's own notes and tagged notices give none."""
    kind = record.get("type")
    if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
        return
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    if kind == "attachment":                 # a message taken up while a turn was running
        attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
        if attachment.get("type") != "queued_command":
            return
        kind, message = "user", {"content": attachment.get("prompt")}
    if kind == "user":
        content = message.get("content")
        results = [part for part in content if isinstance(part, dict) and part.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        for part in results:
            body = part.get("content")
            body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            if '"type": "image"' in body or '"type":"image"' in body:
                body = "[image]"
            yield "result", ("ERROR " if part.get("is_error") else "") + body
        if results:
            return
        text = _said(content).strip()
        if "<" + REQUEST_TAG in text[:200]:
            yield "request", ""
        elif text and not _OPENING_TAG.match(text) and not text.startswith(_CLIENT_NOTES):
            yield "owner", text
    elif kind == "assistant":
        for part in message.get("content") or ():
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and str(part.get("text") or "").strip():
                yield "agent", part["text"].strip()
            elif part.get("type") == "tool_use":
                call = json.dumps(part.get("input"), ensure_ascii=False)
                yield "call", f"{part.get('name')} {call if len(call) <= TAIL_CALL else call[:TAIL_CALL] + ' …[cut]'}"


def transcript_tail(path) -> str:
    """The last messages of a conversation before its handoff was asked for, word for word: the owner's and the agent's text,
    the tool calls and their results, the last ``TAIL_EVENTS`` of them, bounded. Read from the end of the client's own
    transcript file; "" when there is none or it is not a Claude Code transcript. No model writes this."""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TAIL_BYTES))
            lines = stream.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return ""
    if size > TAIL_BYTES:
        lines = lines[1:]                    # the first line of the window starts in the middle of a record
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            events = list(_events(record))
        except (TypeError, ValueError, AttributeError):
            continue                         # a record of a shape this does not know says nothing
        if any(kind == "request" for kind, _ in events):
            break                            # what follows is the filing itself, which the opening already holds
        found.extend(events)
    shown = []
    for kind, text in found[-TAIL_EVENTS:]:
        text = scrub(text)
        if kind == "result" and len(text) > TAIL_RESULT:
            text = text[:TAIL_RESULT] + " …[cut]"
        shown.append(f"{TAIL_LABELS[kind]}: {text}")
    return "\n".join(shown)[-TAIL_CAP:]


class CompactionController:
    def __init__(self, runtime, *, mode=None, clock=time.monotonic, spawn=None):
        self.runtime = runtime
        self.mode = mode_by_environment() if mode is None else mode
        self._clock = clock
        self._spawn = spawn or self._thread
        self._flow: dict | None = None
        self._checked = float("-inf")

    # ----- what it works with -----------------------------------------------------------------------------------------

    @property
    def database(self):
        return self.runtime.database

    @property
    def handoff(self):
        return getattr(self.runtime, "compaction_handoff", None)

    @property
    def chat(self):
        return getattr(self.runtime, "native_chat", None)

    @property
    def managed(self):
        return getattr(self.chat, "managed", None)

    @staticmethod
    def _thread(target, *args) -> None:
        threading.Thread(target=target, args=args, name="compaction-replace", daemon=True).start()

    def _context(self, agent_id, uow):
        """The coordinator's own context, as the handoff's host side asks for it."""
        return self.runtime.application.policy.context_for(agent_id, business_id=None, review_business_id=None, uow=uow)

    def _profile(self, agent_id, uow):
        return store.active_profile(uow.connection, agent_id, self.runtime.paths)

    def _write(self):
        return self.database.transaction(operation="native_chat.compaction")

    def _host(self, agent_id):
        """``(host, status)`` of the agent's verified running session, or ``(None, None)``."""
        try:
            host = self.managed.handle(agent_id)
            if host is None or host.client is None:
                return None, None
            status = host.client.status()
        except Exception:  # noqa: BLE001 - a host that cannot be read now is read again on a later pass
            LOG.debug("The session of %s could not be read for compaction", agent_id, exc_info=True)
            return None, None
        if not isinstance(status, dict) or status.get("status") != "ok" or not status.get("session_id"):
            return None, None
        return host, status

    @staticmethod
    def _message_uuid(row_id, asks) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"qorqut-compaction:{row_id}:{asks}"))

    # ----- one pass ---------------------------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.mode in (ON, ONLY)

    def replaces_client(self, agent_id) -> bool:
        """Whether this agent's session is to be started with the client's own automatic compaction switched off: the mode
        is ``only`` and the agent is the configured coordinator. Never raises; when it cannot tell, the client's stays on."""
        if self.mode != ONLY or self.handoff is None:
            return False
        try:
            with self.database.read() as uow:
                return configured_ordubashy(uow) == agent_id
        except Exception:  # noqa: BLE001 - asked while a session starts: that start must not fail here
            LOG.debug("The coordinator could not be read for %s", agent_id, exc_info=True)
            return False

    def step(self) -> None:
        if not self.enabled or self.handoff is None or self.managed is None:
            return
        try:
            self._step()
        except DomainError:
            LOG.debug("Compaction deferred", exc_info=True)           # writes are closed, or the coordinator cannot act now
        except Exception:  # noqa: BLE001 - nothing here may stop the backend's pass
            LOG.warning("Compaction step failed", exc_info=True)

    def _step(self) -> None:
        now, flow = self._clock(), self._flow
        if flow is None and now - self._checked < CHECK_SECONDS:
            return
        self._checked = now
        if flow is not None and flow["stage"] == "replacing":
            self._replaced(flow)
            return
        with self.database.read() as uow:
            agent_id = configured_ordubashy(uow)
        host, status = self._host(agent_id) if agent_id else (None, None)
        if flow is None:
            if status is not None:
                self._watch(agent_id, host, status)
        elif status is None or flow["agent_id"] != agent_id or status["session_id"] != flow["session"]:
            self._flow = None                # that conversation is not running now; its row keeps where the flow stood
        else:
            self._asked(flow, host, status)

    def _watch(self, agent_id, host, status) -> None:
        """No flow: continue the one this conversation's row holds, or start one when the context has reached the limit."""
        if status.get("state") != "ready" or status.get("phase") in ("starting", "closing"):
            return
        if status.get("client") not in (None, CLIENT):
            return                           # run and checked on Claude Code only; another client keeps its own compaction
        with self.database.read() as uow:
            waiting, _ = self.handoff.rows(uow, agent_id)
        request = (waiting["request_json"] or {}) if waiting else {}
        flow = {"agent_id": agent_id, "session": status["session_id"], "stage": "asked", "row": None, "asks": 1, "text": None,
                "sent": False, "asked_at": self._clock(), "stopping": None, "interrupted": False, "filed": False}
        if waiting is not None and waiting["reason"] == REASON and request.get("session_id") == flow["session"]:
            asks = request.get("asks")
            flow.update(row=waiting["id"], asks=asks if isinstance(asks, int) and 1 <= asks <= MAX_ASKS else 1)
            self._flow = flow
            return
        usage = status.get("usage") if isinstance(status.get("usage"), dict) else {}
        tokens = usage.get("tokens")
        if not isinstance(tokens, int) or tokens < limit(usage.get("window")):
            return
        flow["usage"] = {"tokens": tokens, "window": usage.get("window"), "limit": limit(usage.get("window"))}
        self._flow = flow
        self._send(flow, host)

    # ----- asked: wait for the filing ---------------------------------------------------------------------------------

    def _asked(self, flow, host, status) -> None:
        now = self._clock()
        if flow["row"] is not None:
            with self.database.read() as uow:
                row = uow.repository(TABLE).get(flow["row"])
            if row is None or row["opened_at"]:
                self._flow = None
                return
            flow["filed"] = bool(row["filed_at"]) and row["filed_at"] >= row["requested_at"]
        if flow["filed"] and flow["stopping"] is None:
            flow["stopping"] = now
        if flow["stopping"] is not None:
            self._stopping(flow, host, status, now)
            return
        if not flow["sent"]:
            if flow["row"] is not None and flow["text"] is None and self._seen(host, flow).get("seen"):
                flow["sent"] = True          # a flow continued from its row: this host already has the request
            else:
                self._send(flow, host)
            return
        seen = self._seen(host, flow)
        if not seen:
            return                           # the host did not answer; ask it again on the next pass
        if not seen.get("seen"):
            flow["sent"] = False             # another host of the same conversation: it never got the request
            return
        finished = bool(seen.get("finished"))
        if finished and flow["asks"] < MAX_ASKS:
            flow.update(asks=flow["asks"] + 1, text=None, sent=False)      # once more, in a turn of its own
            self._send(flow, host)
        elif finished or now - flow["asked_at"] > ASK_SECONDS:
            LOG.warning("No handoff was filed by %s; its conversation is closed without one", flow["agent_id"])
            flow["stopping"] = now

    def _seen(self, host, flow) -> dict:
        try:
            reply = host.client.lookup(self._message_uuid(flow["row"], flow["asks"]))
        except Exception:  # noqa: BLE001
            return {}
        return reply if isinstance(reply, dict) and reply.get("status") == "ok" else {}

    def _send(self, flow, host) -> None:
        """Ask for the handoff: fix what it answers for (the row), then hand the request to the host. A request the host does not
        take now is sent again on the next pass with the same id, so it is taken once."""
        if flow["text"] is None:
            context = {**flow.get("usage", {}), "session_id": flow["session"], "asks": flow["asks"]}
            with self._write() as uow:
                self.runtime.application.ensure_writes_open(uow)
                flow["row"], request = self.handoff.ask(self._context(flow["agent_id"], uow), uow, reason=REASON, context=context)
            flow["text"] = wrapped(REQUEST_TAG, (AGAIN if flow["asks"] > 1 else "") + request)
        try:
            reply = host.client.send(flow["text"], self._message_uuid(flow["row"], flow["asks"]), True)
        except Exception:  # noqa: BLE001 - unanswered: the same id is sent again
            LOG.debug("The handoff request did not reach the session of %s", flow["agent_id"], exc_info=True)
            return
        if isinstance(reply, dict) and reply.get("status") == "ok":
            flow["sent"], flow["asked_at"] = True, self._clock()

    def _stopping(self, flow, host, status, now) -> None:
        """The handoff is filed (or given up on): the conversation is replaced once its turn has ended. A turn that goes on is
        interrupted after ``STOP_SECONDS``, and replaced shortly after whatever it does."""
        waited = now - flow["stopping"]
        if status.get("phase") != "idle" and waited <= STOP_SECONDS + CLOSE_SECONDS:
            if waited > STOP_SECONDS and not flow["interrupted"]:
                flow["interrupted"] = True
                try:
                    host.client.interrupt()
                except Exception:  # noqa: BLE001 - closing the session ends the turn as well
                    LOG.debug("The turn of %s could not be interrupted", flow["agent_id"], exc_info=True)
            return
        self._replace(flow, status)

    # ----- replacing: close, then open the next conversation ----------------------------------------------------------

    def _replace(self, flow, status) -> None:
        with self.database.read() as uow:
            waiting, _ = self.handoff.rows(uow, flow["agent_id"])
            profile = self._profile(flow["agent_id"], uow)
        if waiting is None or waiting["id"] != flow["row"] or profile is None:
            self._flow = None
            return
        flow.update(stage="replacing", transcript=status.get("transcript_path"), profile=profile, opening=None, tries=1,
                    outcome=None, started=self._clock())
        self._spawn(self._replacing, flow)

    def _replacing(self, flow) -> None:
        """On a thread of its own: the opener closes the conversation and opens a fresh one with the opening, all under the
        agent's flight (``SessionOpener.replace``). ``flow["outcome"]`` says how it went; the backend's pass records it."""
        try:
            session = self.chat.opener.replace(flow["profile"], flow["session"], lambda: self._opening(flow),
                                               "compaction:" + flow["row"], close_seconds=CLOSE_SECONDS)
            flow["outcome"] = ("opened", session)
        except Exception as error:  # noqa: BLE001 - reported to the pass, which decides whether to try again
            LOG.info("Replacing the conversation of %s failed", flow["agent_id"], exc_info=True)
            flow["outcome"] = ("failed", str(getattr(error, "code", None) or type(error).__name__))

    def _opening(self, flow) -> str:
        """The opening, composed once the closed conversation can take nothing more: an owner's message that reached it while
        it was closing is in it."""
        tail = transcript_tail(flow["transcript"])
        with self.database.read() as uow:
            opening = self.handoff.opening(self._context(flow["agent_id"], uow), uow, tail=tail)
        if opening is None or opening["id"] != flow["row"]:
            raise DomainError("no_handoff", "The handoff this conversation was closed for is no longer waiting")
        flow["opening"] = opening
        return wrapped(OPENING_TAG, opening["text"])

    def _replaced(self, flow) -> None:
        outcome = flow["outcome"]
        if outcome is None:
            return
        if outcome[0] == "opened":
            with self._write() as uow:
                self.runtime.application.ensure_writes_open(uow)
                self.handoff.opened(uow, flow["row"], flow["opening"]["unanswered"])
            LOG.info("The conversation of %s was replaced by %s", flow["agent_id"], outcome[1])
            self._flow = None
        elif outcome[1] in ("agent_off", "no_handoff") or flow["tries"] >= OPEN_TRIES:
            # The row keeps waiting: the closed conversation continues its flow when something opens it again.
            LOG.warning("The conversation of %s was not replaced: %s", flow["agent_id"], outcome[1])
            self._flow = None
        elif self._clock() - flow["started"] >= RETRY_SECONDS:
            flow.update(tries=flow["tries"] + 1, outcome=None, started=self._clock())
            self._spawn(self._replacing, flow)


__all__ = ["CompactionController", "limit", "transcript_tail"]
