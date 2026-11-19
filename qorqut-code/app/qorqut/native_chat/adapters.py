"""The input adapter: the only code that writes into a session.

An adapter receives a verified binding, the exact text, and a ``checkpoint`` it calls once, after its last pre-input check and
before the first input. The arbiter's checkpoint re-verifies the exact session and makes the durable claim; if it raises, nothing
is sent. After the checkpoint every outcome other than an explicit ``deferred`` may follow a write, so it is reported as
``submitted`` or ``uncertain`` and is never replayed. The text never appears in a result: ``receipt`` names only what was observed
(client, host, message uuid).

``ManagedAdapter`` sends to the one verified host of an agent (``managed_runtime``) over its control socket. The host dedupes by
message uuid, so an unanswered send is resolved by asking the host (``lookup`` with a fence) instead of guessing. It never opens a
session. A Codex session takes ``/compact`` and ``/model <name>`` through the host's ``command`` operation, because its app-server
reads them as plain text (``commands.codex_command``); ``$role`` and every other text is a message.
"""

from __future__ import annotations

import uuid

from .contracts import OwnerInputOutcome

SUBMITTED = "submitted"
DEFERRED = "deferred"
UNCERTAIN = "uncertain"
FAILED = "failed"


# Host rejections that mean nothing reached Claude: the producer may try again later.
MANAGED_DEFERRALS = frozenset({"session_starting", "session_closing", "session_closed", "turn_running", "host_busy"})
# What a failed call to the host says about whether anything was written: ``no_host``/``not_running`` is a closed session,
# everything else that happened before the request was written is a host that could not be verified.
MANAGED_NOT_RUNNING = frozenset({"no_host", "not_running"})
MANAGED_EXIT = "/exit"


class ManagedAdapter:
    """An agent's session (Claude Code or Codex), reached through its host. The client is the bound session's
    (``session["provider"]``), looked up in ``managed_runtime.clients``. ``managed`` is the ``ManagedRuntime`` (or a callable
    returning it, or None while it is not composed: every input is then deferred as ``session_closed``)."""

    name = "managed"

    def __init__(self, managed, *, new_uuid=None):
        self._managed = managed
        self._new_uuid = new_uuid

    def _runtime(self):
        found = self._managed
        if found is not None and not hasattr(found, "handle") and callable(found):
            found = found()
        return found

    def submit(self, profile: dict, session: dict, text: str, *, steer: bool, echo: bool, checkpoint,
               replace_composer=False) -> OwnerInputOutcome:
        # ``echo`` and ``replace_composer`` are kept for the arbiter's call; a headless session has no composer.
        from qorqut.managed_runtime import clients, manager as hosts
        client = clients.by_provider(session.get("provider"))
        if client is None:
            return OwnerInputOutcome(FAILED, "unsupported_runtime")
        if not clients.available(client):
            return OwnerInputOutcome(FAILED, "driver_unavailable")
        named = clients.receipt_client(client)
        managed = self._runtime()
        if managed is None:
            return OwnerInputOutcome(DEFERRED, "session_closed")
        agent_id = profile["agent_id"]
        try:
            host = managed.handle(agent_id)
        except hosts.HostUnverified:
            return OwnerInputOutcome(DEFERRED, "host_unverified")
        if host is None:
            return OwnerInputOutcome(DEFERRED, "session_closed")
        if host.client is None:
            return OwnerInputOutcome(DEFERRED, "session_starting")
        try:
            status = host.client.status()
        except Exception as error:  # noqa: BLE001
            if not hosts.is_host_error(error):
                raise
            return OwnerInputOutcome(DEFERRED, self._unreachable(error))
        if not isinstance(status, dict) or status.get("status") not in (None, "ok"):
            return OwnerInputOutcome(DEFERRED, "host_unverified")
        refusal = self._refusal(hosts, agent_id, session, status, steer, closing=text.strip() == MANAGED_EXIT)
        if refusal is not None:
            return refusal
        try:
            checkpoint()
        except Exception:  # noqa: BLE001 - nothing was sent
            return OwnerInputOutcome(DEFERRED, "checkpoint_failed")
        if text.strip() == MANAGED_EXIT:
            return self._close(hosts, host, named)
        if client == "codex":
            from .commands import codex_command
            command = codex_command(text)
            if command is not None:
                return self._command(hosts, host, named, *command)
        # An owner message that opened the session was already handed to the host (``ManagedRuntime.send_first``) with its own
        # uuid, because the session could not be bound before Claude had taken a first message. The host keeps one copy of each
        # uuid, so sending that uuid again is recognised (``accepted_by_host``) and the message is not typed twice.
        staged = getattr(managed, "staged_uuid", None)
        message_uuid = (staged(agent_id, text) if staged is not None else None) or str(
            self._new_uuid() if self._new_uuid is not None else uuid.uuid4())
        receipt = {"client": named, "transport": "managed", "message_uuid": message_uuid, "queued": False}
        try:
            reply = host.client.send(text, message_uuid, steer)
        except Exception as error:  # noqa: BLE001
            if not hosts.is_host_error(error):
                raise
            outcome = self._resolve(hosts, managed, agent_id, session, message_uuid, error, receipt)
        else:
            outcome = self._reply(reply, receipt)
        if outcome.status == SUBMITTED and staged is not None and hasattr(managed, "unstage"):
            managed.unstage(agent_id, text)
        return outcome

    # ----- before the first write -----------------------------------------------------------------------------------

    @staticmethod
    def _unreachable(error) -> str:
        return "session_closed" if getattr(error, "reason", None) in MANAGED_NOT_RUNNING else "host_unverified"

    @staticmethod
    def _refusal(hosts, agent_id, session, status, steer, *, closing):
        """The deferral (or failure) for a host that cannot take this input now, or None."""
        identity = hosts.identity_of(agent_id, status)
        state = status.get("state")
        if state == "failed":
            return OwnerInputOutcome(FAILED, "driver_unavailable")
        if state == "closed":
            return OwnerInputOutcome(DEFERRED, "session_closed")
        if state == "closing" and not closing:
            return OwnerInputOutcome(DEFERRED, "session_closing")
        if state == "starting":
            return OwnerInputOutcome(DEFERRED, "session_starting")
        mine = (identity["conversation_id"], identity["pid"], identity["started"], identity["owner"])
        wanted = (session.get("conversation_id"), session.get("process_pid"), session.get("process_started"), session.get("owner"))
        if mine != wanted:
            return OwnerInputOutcome(DEFERRED, "stale_session")
        # A running turn means "not ready" for a producer that does not steer (Telegram, broadcast, direct messages), exactly as
        # a busy composer does in the terminal; Chat, nudges and recovery steer and are accepted by the host mid-turn.
        if status.get("phase") in hosts.WORKING and not steer and not closing:
            return OwnerInputOutcome(DEFERRED, "turn_running")
        return None

    # ----- the write and what it can leave behind -------------------------------------------------------------------

    @staticmethod
    def _close(hosts, host, named="claude") -> OwnerInputOutcome:
        receipt = {"client": named, "transport": "managed", "closed": True}
        try:
            reply = host.client.close("graceful")
        except Exception as error:  # noqa: BLE001
            if not hosts.is_host_error(error):
                raise
            if getattr(error, "sent", True) is False:
                return OwnerInputOutcome(DEFERRED, ManagedAdapter._unreachable(error))
            return OwnerInputOutcome(UNCERTAIN, "host_timeout" if hosts.is_timeout(error) else "host_connection_lost", receipt)
        if isinstance(reply, dict) and reply.get("status") == "rejected":
            return OwnerInputOutcome(DEFERRED, reply.get("reason") or "session_closed", receipt)
        return OwnerInputOutcome(SUBMITTED, "session_closing", receipt)

    @staticmethod
    def _command(hosts, host, named, name, arguments) -> OwnerInputOutcome:
        """``/compact`` or ``/model`` of a Codex session, through the host (``thread/compact/start``, ``thread/settings/update``).
        Nothing is replayed after the request may have been written."""
        receipt = {"client": named, "transport": "managed", "command": name}
        try:
            reply = host.client.command(name, **arguments)
        except Exception as error:  # noqa: BLE001
            if not hosts.is_host_error(error):
                raise
            if getattr(error, "sent", True) is False:
                return OwnerInputOutcome(DEFERRED, ManagedAdapter._unreachable(error))
            return OwnerInputOutcome(UNCERTAIN, "host_timeout" if hosts.is_timeout(error) else "host_connection_lost", receipt)
        if not isinstance(reply, dict):
            return OwnerInputOutcome(UNCERTAIN, "host_connection_lost", receipt)
        if reply.get("status") == "rejected":
            reason = reply.get("reason")
            return OwnerInputOutcome(DEFERRED, reason if reason in MANAGED_DEFERRALS else "command_unsupported", receipt)
        if reply.get("status") != "ok":
            return OwnerInputOutcome(FAILED, "command_refused", receipt)
        if not reply.get("accepted"):
            # The thread is not ready for it or a turn is running: nothing was changed.
            return OwnerInputOutcome(DEFERRED, "turn_running", receipt)
        return OwnerInputOutcome(SUBMITTED, "command_accepted", receipt)

    @staticmethod
    def _reply(reply, receipt) -> OwnerInputOutcome:
        if not isinstance(reply, dict):
            return OwnerInputOutcome(UNCERTAIN, "host_connection_lost", receipt)
        if reply.get("status") == "rejected":
            reason = reply.get("reason")
            if reason == "fenced":
                return OwnerInputOutcome(DEFERRED, "host_never_received", receipt)
            return OwnerInputOutcome(DEFERRED, reason if reason in MANAGED_DEFERRALS else "host_rejected", receipt)
        if reply.get("status") != "ok":
            return OwnerInputOutcome(UNCERTAIN, "host_connection_lost", receipt)
        receipt = {**receipt, "queued": reply.get("mode") == "queued"}
        if reply.get("duplicate"):
            return OwnerInputOutcome(SUBMITTED, "accepted_by_host", receipt)
        if reply.get("mode") == "queued":
            return OwnerInputOutcome(SUBMITTED, "queued_mid_turn", receipt)
        return OwnerInputOutcome(SUBMITTED, "processing_confirmed" if reply.get("turn_started") else "processing_not_confirmed", receipt)

    def _resolve(self, hosts, managed, agent_id, session, message_uuid, error, receipt) -> OwnerInputOutcome:
        """A send that was not answered. If the request cannot have been written, nothing happened. Otherwise the host is asked
        once, with a fence, whether it ever accepted this uuid: it then either has it, or will never accept it."""
        reason = getattr(error, "reason", None)
        if getattr(error, "sent", True) is False:
            return OwnerInputOutcome(DEFERRED, self._unreachable(error), receipt)
        if reason == "refused":
            return OwnerInputOutcome(DEFERRED, "host_unverified", receipt)
        lost = "host_timeout" if hosts.is_timeout(error) else "host_connection_lost"
        try:
            host = managed.handle(agent_id)
        except hosts.HostUnverified:
            return OwnerInputOutcome(UNCERTAIN, lost, receipt)
        if host is None:
            # The host is gone: it closed (idle, or asked to) while this message was on its way. Its ledger is written before
            # anything reaches Claude, so a uuid it never recorded was never sent: that is a closed session, not a lost message.
            ledger = getattr(managed, "sent_before_exit", None)
            if ledger is not None and ledger(agent_id, message_uuid) in ("absent", "fenced"):
                return OwnerInputOutcome(DEFERRED, "session_closed", receipt)
            return OwnerInputOutcome(UNCERTAIN, lost, receipt)
        if host.client is None:
            return OwnerInputOutcome(UNCERTAIN, lost, receipt)
        identity = host.identity
        if (identity["conversation_id"], identity["pid"], identity["started"], identity["owner"]) != (
                session.get("conversation_id"), session.get("process_pid"), session.get("process_started"), session.get("owner")):
            # Another host or session now answers for this agent: its ledger says nothing about this message.
            return OwnerInputOutcome(UNCERTAIN, "session_changed", receipt)
        try:
            found = host.client.lookup(message_uuid, True)
        except Exception as failure:  # noqa: BLE001
            if not hosts.is_host_error(failure):
                raise
            return OwnerInputOutcome(UNCERTAIN, lost, receipt)
        if not isinstance(found, dict) or found.get("status") != "ok":
            return OwnerInputOutcome(UNCERTAIN, lost, receipt)
        if found.get("seen"):
            return OwnerInputOutcome(SUBMITTED, "accepted_by_host", {**receipt, "queued": found.get("mode") == "queued"})
        if found.get("fenced"):
            return OwnerInputOutcome(DEFERRED, "host_never_received", receipt)
        return OwnerInputOutcome(UNCERTAIN, lost, receipt)
