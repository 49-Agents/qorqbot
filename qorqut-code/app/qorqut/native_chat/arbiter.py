"""The one input arbiter: every input to a native session passes through ``InputArbiter.submit``.

Chat, Telegram owner input, broadcasts, terminal-control direct messages and (later) the inbox nudge dispatcher build an
``InputRequest`` and receive an ``InputResult``. The arbiter alone decides whether the exact registered session is verifiably
there, makes the durable claim, calls the provider adapter and records what it reported. It never launches, resumes or restarts
an agent.

Delivery rules, in order of precedence:

* Nothing is typed unless the session the input was admitted for is still the verified live one, checked again at the
  adapter's checkpoint. A different or ended session never receives it.
* The durable claim (``waiting``/new row → ``submitting``) is the crash boundary. From it on an input is never typed again:
  a crash, a restart or a doubtful adapter outcome leaves it ``uncertain``.
* An input that was definitely never typed (the adapter deferred, or the session ended/changed while it waited) is not
  uncertain: a waiting one becomes ``undelivered`` at session end; an attempt that deferred after its claim is ``failed``
  (``not_typed``) and the same source key may be attempted again.
* Chat inputs wait (durably, in order) for the same conversation to be ready; every other producer attempts once, now, and is
  told ``deferred`` when the session is not ready.
* The shared Codex server takes plain text only and cannot run a CLI slash command, so an owner Chat input that is one of
  Codex's built-in commands is refused (``terminal_command``) before anything is stored or typed; it needs the agent's Terminal.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from typing import Callable, Mapping

from qorqut_operations.contracts import DomainError

from qorqut.inbox.owner_forwarding import forward_owner_message

from . import store
from .commands import codex_builtin
from .contracts import (INPUT_TYPES, MAX_TEXT_CHARS, ORIGIN_SYSTEM, RESULT_DEFERRED, RESULT_REJECTED, STATE_FAILED,
                        STATE_SUBMITTED, STATE_SUBMITTING, STATE_UNCERTAIN, STATE_UNDELIVERED, STATE_WAITING, TYPE_CHAT,
                        TYPE_NUDGE, TYPE_RECOVERY, TYPE_TELEGRAM, InputRequest, InputResult, NativeChatError)
from .probe import (ProbeResult, REASON_CAPTURE_DISABLED, REASON_LIVE, REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME,
                    SessionProbe)
from .projection import marker_text
from .sessions import SessionRegistry

LOG = logging.getLogger(__name__)
NOT_TYPED = "not_typed"
TERMINAL_COMMAND = "terminal_command"


class AgentLocks:
    """One re-entrant lock per agent, shared by the arbiter and the backend so an agent's session state, its waiting inputs and
    its terminal are never driven by two threads at once."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def __call__(self, agent_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(agent_id, threading.RLock())


class StaleSession(Exception):
    """The session the input was admitted for is no longer the verified live one."""


class InputArbiter:
    def __init__(self, runtime, probe: SessionProbe, registry: SessionRegistry, adapters: Mapping[str, object], *,
                 locks: AgentLocks | None = None):
        self.runtime, self.probe, self.registry, self.adapters = runtime, probe, registry, dict(adapters)
        self.locks = locks or AgentLocks()
        self.wake: Callable[[], None] = lambda: None

    @property
    def database(self):
        return self.runtime.database

    # ----- admission -------------------------------------------------------------------------------------------------

    @staticmethod
    def validate(request: InputRequest) -> None:
        if request.input_type not in INPUT_TYPES:
            raise NativeChatError("validation_error", "Unknown native input type")
        text = request.text
        if type(text) is not str or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise NativeChatError("validation_error", f"Native input needs 1-{MAX_TEXT_CHARS} characters")
        for char in text:
            code = ord(char)
            if char not in "\n\t" and (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F):
                raise NativeChatError("validation_error", "Native input cannot contain terminal control characters")
        if not isinstance(request.source_key, str) or not 1 <= len(request.source_key) <= 200:
            raise NativeChatError("validation_error", "A source key of 1-200 characters is required")
        if request.body is not None and (type(request.body) is not str or len(request.body) > 70000):
            raise NativeChatError("validation_error", "The input display text is too long")
        if request.sensitive and request.wait:
            raise NativeChatError("validation_error", "A private payload is attempted once and never held while it waits")
        if request.origin == ORIGIN_SYSTEM and request.principal_id is not None:
            raise NativeChatError("validation_error", "A system input has no owner principal")
        if request.input_type == TYPE_RECOVERY and not request.expected_binding:
            raise NativeChatError("validation_error", "A recovery notice names the exact session it is for")

    def submit(self, request: InputRequest, *, on_checkpoint: Callable[[], None] | None = None) -> InputResult:
        """Admit and (for an attempt) deliver one input. Never raises for availability: the result states what happened.

        ``on_checkpoint`` is the producer's own gate (for example an inbox lease check). It runs just before the durable claim
        and just before the first input; if it raises, nothing is typed and the result is ``deferred``.
        """
        self.validate(request)
        profile = self._profile(request.agent_id)
        if profile is None:
            return self._refused(request, RESULT_REJECTED, "agent_unavailable")
        with self.locks(request.agent_id):
            digest = store.request_hash(request)
            with self.database.read() as uow:
                prior = store.input_by_key(uow.connection, request.agent_id, request.source_key)
            attempt = 1
            if prior is not None:
                if prior["request_hash"] != digest:
                    raise NativeChatError("idempotency_conflict", "This source key was already used for a different input")
                if not self._may_retry(prior, request):
                    return self._result(prior, created=False)
                attempt = prior["attempt"] + 1
            observed = self.probe.observe(profile)
            reconciled = self.registry.reconcile(profile, observed)
            if reconciled.session is None or not reconciled.live:
                return self._unavailable(request, observed)
            session = reconciled.session
            if request.expected_binding and request.expected_binding != session["id"]:
                return self._refused(request, RESULT_REJECTED, "stale_session", session)
            if (request.input_type == TYPE_CHAT and session["transport"] == "codex_queue"
                    and codex_builtin(request.text) is not None):
                # Decided for a new input only (an exact repeat was answered above) and before a row or a checkpoint exists.
                return self._refused(request, RESULT_REJECTED, TERMINAL_COMMAND, session)
            marker = secrets.token_hex(16) if request.origin == ORIGIN_SYSTEM else None
            if request.wait and not session["transcript_path"]:
                # Chat shows the archived conversation; a session with no transcript has none to show, so Chat does not
                # accept input for it. Producers that only reach the terminal are not affected. Capture turned off by the
                # registration is final; capture that is on but has not published its transcript yet may be retried.
                enabled = observed.observation is not None and observed.observation.capture_enabled
                return self._refused(request, RESULT_REJECTED, "capture_not_ready" if enabled else "capture_disabled", session)
            if request.wait:
                with self.database.transaction(operation="native_chat.admit") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    row = store.insert_input(uow.connection, session, request, digest, state=STATE_WAITING, marker=marker,
                                             attempt=attempt)
                    retention = getattr(self.runtime, 'overseer_retention', None)
                    if retention is not None:
                        retention.capture_owner_input(uow, session, request, row)
                    if request.input_type == TYPE_CHAT:
                        from qorqut.agents.attention import enrolled_board
                        from qorqut.management.identity import installation_owner
                        if (enrolled_board(uow, request.agent_id)
                                and request.principal_id == installation_owner(uow)):
                            from qorqut.agents.attention_delivery import record_attention_activity
                            record_attention_activity(uow, request.agent_id, 'owner_message', row['created_at'],
                                'native-input:' + row['id'], attention=self.runtime.application.ordubashy_attention)
                        # The message is accepted here, whether or not its delivery is later delayed; a worker's owner message
                        # is noted to Ordubashy in this same transaction and never from the scrubbed input.
                        forward_owner_message(uow, kind="chat", source_id=row["id"], sender_id=request.principal_id,
                                              worker_id=request.agent_id, body=request.text, accepted_at=row["created_at"])
                self.wake()
                return self._result(row)
            return self._attempt(profile, session, request, digest, attempt, marker, on_checkpoint)

    @staticmethod
    def _may_retry(prior: dict, request: InputRequest) -> bool:
        return (not request.wait and prior["state"] == STATE_FAILED
                and (prior["reason"] or "").startswith(NOT_TYPED))

    def _profile(self, agent_id: str):
        with self.database.read() as uow:
            return store.active_profile(uow.connection, agent_id, self.runtime.paths)

    @staticmethod
    def _unavailable(request: InputRequest, observed: ProbeResult) -> InputResult:
        code = {REASON_CAPTURE_DISABLED: "capture_disabled", REASON_UNSUPPORTED: "unsupported_target",
                REASON_UNSUPPORTED_RUNTIME: "unsupported_runtime"}.get(observed.reason, "terminal_offline")
        # A refusal that no later retry changes is rejected; an offline session may come back, so an attempt is deferred.
        definitive = code in {"capture_disabled", "unsupported_target", "unsupported_runtime"} or request.wait
        return InputResult(state=RESULT_REJECTED if definitive else RESULT_DEFERRED, reason=code, origin=request.origin,
                           input_type=request.input_type)

    @staticmethod
    def _refused(request: InputRequest, state: str, reason: str, session: dict | None = None) -> InputResult:
        return InputResult(state=state, reason=reason, origin=request.origin, input_type=request.input_type,
                           binding=store.binding_of(session) if session else None)

    def _result(self, row: dict, *, created: bool = True) -> InputResult:
        with self.database.read() as uow:
            session = store.get_session(uow.connection, row["session_id"])
        return InputResult(state=row["state"], input_id=row["id"], binding=store.binding_of(session) if session else None,
                           origin=row["origin"], input_type=row["input_type"], reason=row["reason"],
                           evidence=json.loads(row["evidence_json"]), created=created, marker=row["marker"])

    # ----- delivery --------------------------------------------------------------------------------------------------

    def _verify(self, profile: dict, session: dict) -> None:
        observed = self.probe.observe(profile)
        if observed.reason != REASON_LIVE or not store.same_session(session, observed.observation):
            raise StaleSession()

    @staticmethod
    def _typed(origin: str, marker: str | None, input_type: str, text: str) -> str:
        return marker_text(marker, input_type, text) if origin == ORIGIN_SYSTEM and marker else text

    def _attempt(self, profile, session, request, digest, attempt, marker, on_checkpoint) -> InputResult:
        """One immediate attempt: the row is created at the claim, so a deferral before it leaves nothing behind."""
        claimed: dict = {}

        def checkpoint():
            if on_checkpoint is not None:
                on_checkpoint()
            self._verify(profile, session)
            with self.database.transaction(operation="native_chat.claim") as uow:
                self.runtime.application.ensure_writes_open(uow)
                claimed["row"] = store.insert_input(uow.connection, session, request, digest, state=STATE_SUBMITTING,
                                                    marker=marker, reason="claimed", attempt=attempt)
                retention = getattr(self.runtime, 'overseer_retention', None)
                if retention is not None:
                    retention.capture_owner_input(uow, session, request, claimed['row'])

        text = self._typed(request.origin, marker, request.input_type, request.text)
        outcome, adapter = self._run(profile, session, text, request.steer, request.input_type, checkpoint, claimed)
        if "row" not in claimed:
            return InputResult(state=RESULT_DEFERRED, reason=outcome.reason or "not_delivered", binding=store.binding_of(session),
                               origin=request.origin, input_type=request.input_type)
        row = self._settle(claimed["row"], outcome, adapter, requeue=False)
        if outcome.status == "deferred":
            # Claimed but never typed: the audit row stays, the producer is told to try again.
            return InputResult(state=RESULT_DEFERRED, input_id=row["id"], binding=store.binding_of(session), origin=row["origin"],
                               input_type=row["input_type"], reason=outcome.reason, marker=row["marker"])
        return self._result(row)

    def _run(self, profile, session, text, steer, input_type, checkpoint, claimed):
        from qorqut.transport.agent_terminal_control import OwnerInputOutcome
        adapter = self.adapters.get(session["transport"])
        if adapter is None:
            return OwnerInputOutcome("deferred", "unsupported_transport"), "none"
        try:
            # Chat, Telegram and inbox/inactivity nudges can contain text the client clips or collapses.
            # Nudges use Telegram's submission option and still require an empty composer.
            # The terminal adapter forces exact-paste verification when Chat replaces a draft.
            # Other producers retain whole-text echo verification.
            return adapter.submit(profile, session, text, steer=steer,
                                  echo=input_type not in {TYPE_CHAT, TYPE_TELEGRAM, TYPE_NUDGE}, checkpoint=checkpoint,
                                  **({"replace_composer": True} if input_type == TYPE_CHAT else {})), adapter.name
        except StaleSession:
            return OwnerInputOutcome("deferred", "stale_session"), adapter.name
        except (DomainError, OSError, ValueError, KeyError, RuntimeError) as error:
            LOG.debug("Native input adapter raised for %s", session["agent_id"], exc_info=True)
            # Before the claim nothing was typed; after it nothing can be assumed.
            return OwnerInputOutcome("uncertain" if claimed else "deferred", type(error).__name__), adapter.name

    def _settle(self, row: dict, outcome, adapter: str, *, requeue: bool) -> dict:
        evidence = {"adapter": adapter, **(dict(outcome.receipt) if outcome.receipt else {})}
        with self.database.transaction(operation="native_chat.receipt") as uow:
            conn = uow.connection
            if outcome.status == "submitted":
                store.settle_input(conn, row["id"], STATE_SUBMITTED, outcome.reason, evidence, (STATE_SUBMITTING,))
            elif outcome.status == "deferred":
                if requeue:
                    store.settle_input(conn, row["id"], STATE_WAITING, f"deferred:{outcome.reason}", evidence, (STATE_SUBMITTING,))
                else:
                    store.settle_input(conn, row["id"], STATE_FAILED, f"{NOT_TYPED}:{outcome.reason}", evidence, (STATE_SUBMITTING,))
            elif outcome.status == "failed":
                store.settle_input(conn, row["id"], STATE_FAILED, outcome.reason, evidence, (STATE_SUBMITTING,))
            else:
                store.settle_input(conn, row["id"], STATE_UNCERTAIN, outcome.reason, evidence, (STATE_SUBMITTING,))
            return store.get_input(conn, row["id"])

    # ----- waiting inputs --------------------------------------------------------------------------------------------

    def dispatch(self, agent_id: str) -> bool:
        """Try the oldest waiting input of this agent once. Returns whether an attempt was made.

        Only the head of the queue is attempted, so a deferred head keeps every later input behind it. An input admitted for a
        session that is no longer the open one is never sent into the new one: it becomes ``undelivered``."""
        with self.locks(agent_id):
            with self.database.read() as uow:
                waiting = store.waiting_inputs(uow.connection, agent_id)
            if not waiting:
                return False
            profile = self._profile(agent_id)
            if profile is None:
                self._undeliver(waiting, "agent_inactive")
                return False
            observed = self.probe.observe(profile)
            reconciled = self.registry.reconcile(profile, observed)
            with self.database.read() as uow:
                waiting = store.waiting_inputs(uow.connection, agent_id)
            if reconciled.session is None or not reconciled.live:
                return False
            session = reconciled.session
            stale = [row for row in waiting if row["session_id"] != session["id"]]
            if stale:
                self._undeliver(stale, "conversation_changed")
            head = next((row for row in waiting if row not in stale), None)
            if head is None:
                return False
            claimed: dict = {}

            def checkpoint():
                self._verify(profile, session)
                with self.database.transaction(operation="native_chat.claim") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    # The durable crash boundary: from here this input is never typed again by this code.
                    if not store.claim_input(uow.connection, head):
                        raise StaleSession()
                claimed["row"] = head

            text = self._typed(head["origin"], head["marker"], head["input_type"], head["body"] or "")
            outcome, adapter = self._run(profile, session, text, bool(head["steer"]), head["input_type"], checkpoint, claimed)
            if "row" not in claimed:
                LOG.info("Native input for %s is waiting: %s", agent_id, outcome.reason)
                return True
            self._settle(head, outcome, adapter, requeue=True)
            return True

    def _undeliver(self, rows: list[dict], reason: str) -> None:
        with self.database.transaction(operation="native_chat.undelivered") as uow:
            for row in rows:
                store.settle_input(uow.connection, row["id"], STATE_UNDELIVERED, reason, {}, (STATE_WAITING,))

    def admit_nudge(self, agent_id: str, source_key: str, text: str, *, wait: bool = False, metadata: Mapping | None = None,
                    expected_binding: str | None = None, on_checkpoint=None) -> InputResult:
        """The system-origin ``inbox_nudge`` admission path for the inbox dispatcher (the dispatcher, coalescing and cadence are
        its own). The text is typed behind an application marker whose id is returned in ``InputResult.marker`` and stored with
        the input, so the transcript entry that starts with it can be attributed to this input; nothing else makes a native
        user turn a system turn.

        A nudge is submitted at once even while a turn is running, through the same adapter path and checks as every other
        input: a verified empty composer, a successful paste without requiring whole-text echo, and normal submit.
        Processing is never confirmed unless the transport evidences it. When that is not safe (an owner's text in the
        composer, a modal, an unreadable or changing
        screen, no verified session) nothing is typed: ``wait=False`` returns ``deferred`` and the caller asks again, ``wait=True``
        keeps it ``waiting`` for this very session.

        ``expected_binding`` pins the exact session the producer reserved the notice for: any other live session rejects it."""
        return self.submit(InputRequest(agent_id, TYPE_NUDGE, source_key, text, wait=wait, steer=True, expected_binding=expected_binding,
                                        metadata=dict(metadata or {})), on_checkpoint=on_checkpoint)

    def admit_recovery(self, agent_id: str, source_key: str, text: str, *, expected_binding: str,
                       metadata: Mapping | None = None) -> InputResult:
        """The system-origin ``bootstrap_recovery`` wake of a session that lost its context. It is its own input type, unrelated
        to inbox events, and goes through the same adapter path and checks as every input: a verified empty composer, the whole text
        echoed, one normal submit, no processing confirmed. ``expected_binding`` is required: the wake is for one exact session and
        any other live session rejects it. Nothing here starts or resumes a session."""
        return self.submit(InputRequest(agent_id, TYPE_RECOVERY, source_key, text, steer=True, expected_binding=expected_binding,
                                        metadata=dict(metadata or {})))


__all__ = ["AgentLocks", "InputArbiter", "NOT_TYPED", "StaleSession", "TERMINAL_COMMAND"]
