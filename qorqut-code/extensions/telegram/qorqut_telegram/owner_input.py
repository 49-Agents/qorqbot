"""Guarded delivery of owner input to an agent's registered native terminal.

Every accepted owner message is already a durable canonical inbox message; this worker additionally types the same text
into the exact agent's native session through the shared input arbiter, when, and only when, the arbiter verifies the
registered session and an idle empty composer of the right client. Anything else queues with a stable reason, is retried
with bounded backoff, and produces one owner-visible notice per queue episode. The intent is committed as ``submitting``
before the first input, so a crash or a doubtful outcome is ``uncertain`` and is never typed again. This worker never
starts, restarts or kills a terminal.
"""

from __future__ import annotations

import json
import logging

from qorqut_operations.contracts import DomainError
from qorqut.agents.global_scope import active_global
from qorqut.native_chat.contracts import (InputRequest, NativeChatError, RESULT_DEFERRED, STATE_FAILED, STATE_SUBMITTED,
                                            STATE_UNCERTAIN, TYPE_TELEGRAM)
from . import store
from .bridge import MISMATCH_CODES
from .config import AGENT_CHATS
from .inbound import LABELS
from .outbox import enqueue_notice

LOGGER = logging.getLogger(__name__)
MAX_PER_CYCLE = 3
FINAL = ("submitted", "uncertain", "expired", "failed")
NOTICE_REASONS = {
    "terminal_absent": "its terminal is not running (nothing was started)",
    "terminal_unverified": "its terminal is not running the expected client",
    "terminal_unsupported": "its terminal cannot be reached from here",
}
# What the shared arbiter reports for an input it did not type, in this worker's stable status codes.
DEFERRALS = {"terminal_offline": "terminal_absent", "unsupported_target": "terminal_unsupported"}
PREFACE = ("Qorqut owner message via Telegram (correlation {correlation}). The same message is in your inbox; handle it "
           "once. It is owner input for this request, not blanket authorization.")
SIGNOFF_PREFACE = ("Qorqut owner reply via Telegram on your Signoff \"{title}\" (note {correlation}). The same reply is saved on "
                   "the Signoff and in your inbox; handle it once. It is the owner's answer or comment, not blanket authorization.")


def notice(bridge, uow, message, text, source_key):
    """Tell the owner in the chat the input arrived in. An input from a chat that is no longer an agent chat (Founder's and
    Marketing's were removed) settles without one: that chat has no destination any more, and nothing falls back to another."""
    if message["chat_name"] in AGENT_CHATS:
        enqueue_notice(bridge, uow, message["chat_name"], text, source_key)


class Final(Exception):
    def __init__(self, state, code):
        super().__init__(code)
        self.state, self.code = state, code


class OwnerInputs:
    def __init__(self, bridge):
        self.bridge = bridge
        self._unrecorded = {}

    def arbiter(self):
        native = getattr(self.bridge.runtime, "native_chat", None)
        return getattr(native, "arbiter", None)

    # ----- cycle ----------------------------------------------------------------------------------------------

    def run(self):
        worked = self.flush()
        worked = self.reconcile() or worked
        for _ in range(MAX_PER_CYCLE):
            if not self.deliver_next():
                break
            worked = True
        return worked

    def deliver_next(self):
        now = self.bridge.now()
        with self.bridge.read() as uow:
            candidates = [dict(row) for row in uow.connection.execute(
                "SELECT i.* FROM telegram_owner_inputs i WHERE i.state='queued' AND (i.next_attempt_at IS NULL OR i.next_attempt_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM telegram_owner_inputs e WHERE e.agent_id=i.agent_id AND e.state='queued' "
                "AND (e.created_at<i.created_at OR (e.created_at=i.created_at AND e.rowid<i.rowid))) "
                "ORDER BY i.created_at, i.rowid LIMIT 10", (now,))]
        for row in candidates:
            if row["agent_id"] in self._busy_agents():
                continue
            self.attempt(row, now)
            return True
        return False

    def _busy_agents(self):
        return {agent for agent, _ in self._unrecorded.values()}

    # ----- one attempt ----------------------------------------------------------------------------------------

    def attempt(self, row, now):
        bridge = self.bridge
        if row["expires_at"] and row["expires_at"] <= now:
            self._settle(row, "expired", "expired")
            return
        entered = {"done": False}
        try:
            text, profile, body = self._load(row, now)
            arbiter = self.arbiter()
            if arbiter is None:
                self._defer(row, "terminal_unavailable", now)
                return

            def checkpoint():
                # The terminal wait can be long: authority is checked again in the very transaction that commits the intent,
                # and the target must still be the exact registration that was loaded.
                with bridge.write("input.checkpoint") as uow:
                    current = uow.repository("telegram_owner_inputs").get(row["id"])
                    if current is None or current["state"] != "queued":
                        raise DomainError("revision_conflict", "The owner input changed before it was typed")
                    fresh = self._authorize(uow, row)
                    if store.target_fingerprint(fresh) != store.target_fingerprint(profile):
                        raise DomainError("binding_changed", "The target registration changed while the input waited")
                    uow.repository("telegram_owner_inputs").update(
                        row["id"], {"state": "submitting", "attempts": current["attempts"] + 1, "next_attempt_at": None},
                        expected_revision=current["revision"])
                entered["done"] = True

            # One request per owner input, keyed by its ID: a retry after the arbiter's claim finds the saved input and never
            # types it again. A private payload is typed but never stored by the arbiter.
            request = InputRequest(row["agent_id"], TYPE_TELEGRAM, "telegram:" + row["id"], text, sensitive=bool(row["sensitive"]),
                                   metadata={"purpose": row["purpose"]})
            outcome = self._outcome(arbiter.submit(request, on_checkpoint=checkpoint))
        except Final as exc:
            self._settle(row, exc.state, exc.code)
            return
        except NativeChatError as exc:
            self._settle(row, "failed", "invalid_input" if exc.code == "validation_error" else exc.code)
            return
        except DomainError as exc:
            if exc.code in MISMATCH_CODES and not entered["done"]:
                raise
            if exc.code == "validation_error":
                self._settle(row, "failed", "invalid_input")
                return
            if entered["done"]:
                self._settle(row, "uncertain", exc.code)
                return
            self._defer(row, exc.code, now)
            return
        except Exception:  # noqa: BLE001 - after the first input the outcome is unknown; never replay
            LOGGER.exception("Owner input delivery failed unexpectedly")
            if entered["done"]:
                self._settle(row, "uncertain", "internal_error")
            else:
                self._defer(row, "internal_error", now)
            return
        if outcome.status == "submitted":
            self._settle(row, "submitted", None, outcome.receipt)
        elif outcome.status == "uncertain":
            self._settle(row, "uncertain", outcome.reason, outcome.receipt)
        elif outcome.status == "failed" or outcome.reason == "terminal_unsupported":
            self._settle(row, "failed", outcome.reason)
        else:
            self._defer(row, outcome.reason, now)

    @staticmethod
    def _outcome(result):
        """The arbiter's result in this worker's vocabulary: submitted | uncertain | failed | deferred."""
        from qorqut.transport.agent_terminal_control import OwnerInputOutcome
        receipt = dict(result.evidence) or None
        if result.state == STATE_SUBMITTED:
            return OwnerInputOutcome("submitted", result.reason, receipt)
        if result.state == STATE_UNCERTAIN:
            return OwnerInputOutcome("uncertain", result.reason, receipt)
        if result.state == STATE_FAILED:
            return OwnerInputOutcome("failed", result.reason, receipt)
        reason = DEFERRALS.get(result.reason, result.reason or "terminal_unavailable")
        if result.state == RESULT_DEFERRED or reason == "stale_session":
            return OwnerInputOutcome("deferred", reason)
        return OwnerInputOutcome("failed", reason)

    def _authorize(self, uow, row):
        """The target's profile row when this delivery is still authorized exactly as it was accepted, else ``Final``.

        The generation, Telegram binding, Qorqut owner and target registration it froze must all still hold; an active
        changed identity alone is never enough, and nothing is retargeted or reattributed. A refusal of the pinned identity
        itself (owner or installation mismatch) is raised, not settled: the delivery waits for the operator.
        """
        bridge = self.bridge
        binding = bridge.check_binding(uow)
        if (row["owner_principal_id"], row["config_generation"], row["binding_fingerprint"]) != (
                binding["owner"], binding["generation"], binding["fingerprint"]):
            raise Final("failed", "binding_changed")
        agent = uow.repository("principals").get(row["agent_id"])
        profile = uow.connection.execute(
            "SELECT * FROM agent_profiles WHERE agent_id=? AND status='active'", (row["agent_id"],)).fetchone()
        if not agent or not agent["active"] or profile is None or active_global(uow.connection, row["agent_id"]) is None:
            raise Final("failed", "agent_unavailable")
        if row["purpose"] == "owner_message":
            message = store.message(uow, row["message_id"])
            # The input goes to the agent of the exact topic it was received in, as that topic is configured now: a moved topic, a
            # rebound agent or another topic of the same group never inherits it.
            chat = bridge.config.destination(message["chat_id"], message["message_thread_id"]) if message else None
            if chat is None or chat.name != message["chat_name"] or chat.agent_id != row["agent_id"]:
                raise Final("failed", "binding_changed")
            try:
                bridge.check_agent_chat(uow, chat)
            except DomainError:
                raise Final("failed", "binding_changed") from None
        profile = dict(profile)
        if store.target_fingerprint(profile) != row["target_fingerprint"]:
            raise Final("failed", "target_changed")
        return profile

    def _load(self, row, now):
        """The exact text to type and the target's profile row, or ``Final``. Authority is rechecked here."""
        bridge = self.bridge
        with bridge.read() as uow:
            profile = self._authorize(uow, row)
            message = store.message(uow, row["message_id"])
            if row["purpose"] == "signoff_reply":
                canonical = uow.connection.execute(
                    "SELECT n.body, s.preview_json FROM signoff_notes n JOIN signoffs s ON s.id=n.signoff_id "
                    "WHERE n.id=? AND s.id=?", (row["correlation_id"], row["signoff_id"])).fetchone()
            else:
                canonical = uow.connection.execute("SELECT body FROM agent_messages WHERE id=?", (row["correlation_id"],)).fetchone()
        if message is None or canonical is None:
            raise Final("failed", "message_missing")
        if row["purpose"] == "owner_message":
            text = PREFACE.format(correlation=row["correlation_id"]) + "\n\n" + canonical["body"]
        else:
            title = " ".join(str(json.loads(canonical["preview_json"]).get("title", "")).split())[:120].replace('"', "'")
            text = SIGNOFF_PREFACE.format(title=title, correlation=row["correlation_id"]) + "\n\n" + canonical["body"]
        if row["sensitive"]:
            secret = bridge.spool.take(row["spool_id"], now) if row["spool_id"] else None
            if secret is None:
                raise Final("expired" if row["expires_at"] <= now else "failed", "expired" if row["expires_at"] <= now else "spool_lost")
            text += "\n" + secret
        return text, profile, canonical["body"]

    # ----- outcomes -------------------------------------------------------------------------------------------

    def _defer(self, row, reason, now):
        """Nothing was typed: stay queued with the reason, retry later and tell the owner once per queue episode."""
        bridge = self.bridge
        delay = min(30.0, 2.0 * 2 ** min(row["attempts"], 5)) * (0.5 + bridge.rng.random() * 0.5)
        with bridge.write("input.defer") as uow:
            current = uow.repository("telegram_owner_inputs").get(row["id"])
            if current is None or current["state"] not in ("queued", "submitting"):
                return
            changes = {"state": "queued", "deferral_reason": reason, "next_attempt_at": store.plus(now, delay)}
            if current["state"] == "queued":
                changes["attempts"] = current["attempts"] + 1
            if current["purpose"] == "owner_message" and current["notice_sent_at"] is None and not uow.connection.execute(
                    "SELECT 1 FROM telegram_owner_inputs WHERE agent_id=? AND state='queued' AND notice_sent_at IS NOT NULL",
                    (row["agent_id"],)).fetchone():
                message = store.message(uow, current["message_id"])
                label = LABELS.get(message["chat_name"], "agent")
                why = NOTICE_REASONS.get(reason, "its terminal is busy or not ready for input")
                # A secret's canonical inbox note holds only a redaction marker, so it never claims an inbox copy.
                kept = ("Only a redacted marker is in the agent's inbox; the text is kept briefly" if current["sensitive"]
                        else "It is also in the agent's inbox")
                delivery = "will be typed into the terminal when the composer is free. "
                notice(bridge, uow, message,
                       f"Your message is queued for the {label} agent because {why}. {kept} and {delivery}"
                       "Nothing was started or interrupted.",
                       f"input:{row['id']}:queued")
                changes["notice_sent_at"] = now
            uow.repository("telegram_owner_inputs").update(row["id"], changes, expected_revision=current["revision"])
        bridge.wake()

    def _settle(self, row, state, code, receipt=None):
        """Record a terminal state. Spool data goes with it; a failed write is retried from memory, never replayed."""
        try:
            self._record(row, state, code, receipt)
        except DomainError as exc:
            if exc.code not in ("maintenance", "busy"):
                raise
            self._unrecorded[row["id"]] = (row["agent_id"], (row, state, code, receipt))

    def flush(self):
        worked = False
        for identity, (agent, (row, state, code, receipt)) in list(self._unrecorded.items()):
            try:
                self._record(row, state, code, receipt)
            except DomainError as exc:
                if exc.code not in ("maintenance", "busy"):
                    raise
                continue
            del self._unrecorded[identity]
            worked = True
        return worked

    def _record(self, row, state, code, receipt=None):
        bridge = self.bridge
        now = bridge.now()
        with bridge.write("input.settle") as uow:
            current = uow.repository("telegram_owner_inputs").get(row["id"])
            if current is None or current["state"] in ("submitted", "expired", "cancelled"):
                return
            changes = {"state": state, "error_code": code, "deferral_reason": None, "next_attempt_at": None}
            if state == "submitted":
                changes["submitted_at"] = now
            if receipt is not None:
                changes["receipt_json"] = receipt
            uow.repository("telegram_owner_inputs").update(row["id"], changes, expected_revision=current["revision"])
            self._after(uow, {**current, **changes}, now)
        if row["spool_id"]:
            bridge.spool.delete(row["spool_id"])
        bridge.wake()

    def _after(self, uow, row, now):
        """Owner-visible consequences of a final state, in the same transaction as the state."""
        bridge = self.bridge
        message = store.message(uow, row["message_id"])
        if row["sensitive"]:
            store.enqueue_cleanup(uow, message["id"], "secret_" + row["state"], now=now)
            prompt = store.message(uow, message["reply_to_id"]) if message["reply_to_id"] else None
            if prompt is not None and prompt["input_mode"] == "secret":
                store.enqueue_cleanup(uow, prompt["id"], "secret_" + row["state"], now=now)
        if row["purpose"] != "owner_message" or row["state"] not in ("uncertain", "failed", "expired"):
            return
        label = LABELS.get(message["chat_name"], "agent")
        texts = {
            "uncertain": f"Your message to the {label} agent may or may not have reached the terminal, and it will not be typed again. "
                         "It is in the agent's inbox; check the terminal before resending.",
            "failed": f"Your message to the {label} agent could not be typed into its terminal ({row['error_code']}). "
                      "It is in the agent's inbox.",
            "expired": f"Your secret input for the {label} agent expired before it could be delivered and was discarded. "
                       "Ask the agent to request it again.",
        }
        notice(bridge, uow, message, texts[row["state"]], f"input:{row['id']}:{row['state']}")

    # ----- reconciliation -------------------------------------------------------------------------------------

    def reconcile(self):
        """Close what a crash left open: expired secrets, uncertain rows found at recovery, unqueued cleanups."""
        bridge = self.bridge
        now = bridge.now()
        worked = False
        spools = []
        with bridge.write("input.reconcile") as uow:
            expired = [dict(row) for row in uow.connection.execute(
                "SELECT * FROM telegram_owner_inputs WHERE state='queued' AND expires_at IS NOT NULL AND expires_at<=?", (now,))]
            for row in expired:
                current = uow.repository("telegram_owner_inputs").get(row["id"])
                changes = {"state": "expired", "error_code": "expired", "deferral_reason": None, "next_attempt_at": None}
                uow.repository("telegram_owner_inputs").update(row["id"], changes, expected_revision=current["revision"])
                self._after(uow, {**current, **changes}, now)
                if row["spool_id"]:
                    spools.append(row["spool_id"])
                worked = True
            agent_chats = ",".join("?" * len(AGENT_CHATS))
            unresolved = [dict(row) for row in uow.connection.execute(
                "SELECT i.* FROM telegram_owner_inputs i WHERE i.state IN ('uncertain','failed','expired','submitted') "
                "AND ((i.sensitive=1 AND NOT EXISTS (SELECT 1 FROM telegram_cleanup_jobs c WHERE c.message_id=i.message_id)) "
                "OR (i.purpose='owner_message' AND i.state IN ('uncertain','failed','expired') "
                f"AND EXISTS (SELECT 1 FROM telegram_messages m WHERE m.id=i.message_id AND m.chat_name IN ({agent_chats})) "
                "AND NOT EXISTS (SELECT 1 FROM telegram_outbox o WHERE o.source_kind='notice' "
                "AND o.source_key='input:'||i.id||':'||i.state))) LIMIT 20", tuple(AGENT_CHATS))]
            for row in unresolved:
                self._after(uow, row, now)
                worked = True
        for spool_id in spools:
            bridge.spool.delete(spool_id)
        return worked
