"""Business-folder synchronization and the approved-send lifecycle."""

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from . import routes
from . import store as store_module
from .fastmail import ProviderError
from .history import PrivateHistory
from .models import Outgoing
from .store import Store
from .verification import verify_approved_message

ACCOUNT_CURSOR = "@account"
RECONCILIATION_RETRY_DELAYS = (60, 120, 300, 600, 900, 1800, 3600)
PENDING_SEND_STATES = {"queued", "creating", "draft", "sending", "accepted", "uncertain"}

# Ledger state -> intent envelope receipt state (docs/INTEGRATION-CONTRACT.md).
INTENT_RECEIPT_STATES = {"sent": "confirmed", "canceled": "cancelled", "failed": "failed", "uncertain": "uncertain"}


class IntentConflict(ValueError):
    """Same (client_id, intent_id) resubmitted with a different payload."""


class IntentCancelled(ValueError):
    """The intent_id was tombstoned by a cancel before this submission arrived."""


def intent_request_id(client_id, intent_id):
    """Deterministic send-ledger key, namespaced away from direct send request IDs."""
    digest = hashlib.sha256(f"{client_id}:{intent_id}".encode()).hexdigest()
    return "itg-" + digest[:40]


class EmailService:
    def __init__(self, settings, store, provider, *, outlook_provider=None):
        from .mailbox_settings import load_settings
        settings = load_settings(settings, store)
        provider.settings = settings
        self.settings, self.store, self.provider = settings, store, provider
        self.history = PrivateHistory(settings.history, provider.account_id, settings.database) if settings.history else None
        if any(sender.enabled for sender in settings.shared_senders) and self.history is None:
            raise ValueError("Enabled shared sending requires private history")
        store.history = self.history
        store.flush_history()
        store.bind_provider(provider.account_id)
        store.bind_senders(settings.shared_senders)
        if settings.outlook_web:
            store.bind_outlook_provider(settings.outlook_web)
        if outlook_provider is None and settings.outlook_web:
            from .outlook_web import OutlookWeb
            outlook_provider = OutlookWeb(settings.outlook_web, store=store, history=self.history, fastmail=provider)
        self.outlook_provider = outlook_provider
        if store.bind_mailboxes(settings.mailboxes):
            self.reset_sync()
        self.send_lock, self.sync_lock = threading.Lock(), threading.Lock()
        self.history_lock = threading.Lock()

    def close(self):
        try:
            if self.outlook_provider is not None:
                self.outlook_provider.close()
        finally:
            self.provider.close()

    def outbound_provider(self, row):
        if row["transport"] == "fastmail":
            return self.provider
        if row["transport"] == "outlook_web" and self.outlook_provider is not None and self.history is not None:
            return self.outlook_provider
        raise ProviderError("outlook_web_unavailable", status=503)

    def shared_sender_metadata(self):
        status = self.outlook_provider.status() if self.outlook_provider is not None else {"available": False}
        metadata = []
        shared_quota = self.store.shared_quota()
        for sender in self.settings.shared_senders:
            item = {**sender.model_dump(), "shared_quota": shared_quota, "send_available": sender.enabled and (
                sender.transport == "fastmail" or status.get("available") is True)}
            if sender.transport == "outlook_web":
                collector = status.get("collector", {})
                item["collector"] = {"state": collector.get("state", "unavailable"),
                    "last_success_at": collector.get("last_success_at")}
            metadata.append(item)
        return metadata

    def collect_outlook(self):
        if self.outlook_provider is None:
            return {"state": "disabled"}
        if self.history is None:
            raise ValueError("Outlook collection requires private history")
        return self.outlook_provider.collect_history(self.history)

    def collect_history(self):
        if self.history is None:
            return {"state": "disabled"}
        if not self.history_lock.acquire(blocking=False):
            return {"state": "running"}
        try:
            self.store.flush_history()
            return self.provider.collect_history(self.history)
        except ProviderError as exc:
            self.history.collection_gap(exc.code, exc.status)
            # Never reset an expired history cursor into a silent fresh baseline.
            raise
        finally:
            self.history_lock.release()

    def reset_sync(self):
        for mailbox in self.settings.mailboxes:
            self.store.reset_folder(mailbox.ref, mailbox.folder_id)
        self.store.reset_folder(ACCOUNT_CURSOR, "all")

    def sync(self):
        if not self.settings.mailboxes:
            # An account-only deployment has no business folder to cache; the /fastmail account API reads the provider directly.
            return {"state": "idle", "errors": []}
        if not self.sync_lock.acquire(blocking=False):
            return {"state": "running"}
        try:
            for mailbox in self.settings.mailboxes:
                self.store.mailbox_status(mailbox.ref, "syncing")
            self.provider.folders()
            saved = self.store.folder(ACCOUNT_CURSOR, "all")
            cursor = saved["cursor"] if saved else None
            seen = set()
            for _ in range(20):
                if cursor and cursor in seen:
                    raise ProviderError("repeated_jmap_continuation")
                seen.add(cursor)
                data = self.provider.delta(cursor)
                touched = {item["id"] for item in data["items"]} | set(data["destroyed"])
                for mailbox in self.settings.mailboxes:
                    owned = {item["id"]: item for item in data["items"] if self.provider.owner(item) == mailbox}
                    items = [owned.get(identity, {"id": identity, "@removed": {}}) for identity in sorted(touched)]
                    self.store.sync_page(mailbox.ref, mailbox.folder_id, items, data["cursor"], complete=data["complete"])
                # Advance only after all business caches commit; replay is harmless if interrupted.
                self.store.sync_page(ACCOUNT_CURSOR, "all", [], data["cursor"], complete=data["complete"])
                cursor = data["cursor"]
                if data["complete"]:
                    for mailbox in self.settings.mailboxes:
                        self.store.mailbox_status(mailbox.ref, "synced")
                        # Reply-stop guard: record any newly correlated reply before returning.
                        self.store.correlate_replies(mailbox.ref, mailbox.business_id)
                    return {"state": "synced", "errors": []}
            return {"state": "syncing", "errors": []}
        except ProviderError as exc:
            if exc.status == 410:
                self.reset_sync()
            for mailbox in self.settings.mailboxes:
                self.store.mailbox_status(mailbox.ref, "partial", exc.code)
            return {"state": "partial", "errors": [{"error": exc.code}]}
        finally:
            self.sync_lock.release()

    def verify_draft(self, mailbox, message, draft, *, provider=None):
        verify_approved_message(provider or self.provider, mailbox, message, draft)

    def submit(self, message):
        mailbox = self.settings.mailbox(message.mailbox)
        with self.send_lock:
            existing = self.store.send(message.request_id)
            if existing:
                if existing["digest"] != message.digest():
                    raise ValueError("Request ID already belongs to different content")
                return self.reconcile(existing)
            sender = self.settings.sender(mailbox, message)
            row, created = self.store.begin_send(message, queued=sender is not None)
            if not created:
                return self.reconcile(row)
            return self.start_send(mailbox, message, row, sender)

    @staticmethod
    def expired(message):
        return message.expires_at is not None and datetime.fromisoformat(message.expires_at) <= datetime.now(timezone.utc)

    def stop_guard_intent(self, message):
        """The cold-outreach label (envelope intent or legacy /sends) a reply-stop guard has silenced."""
        intent = self.store.integration_intent_by_request(message.request_id)
        if intent is not None:
            if intent["category"] != "cold_outreach":
                return None
            owner, contact, business = (intent["workflow_owner"], intent["stop_contact_key"],
                intent["stop_business_id"])
        else:
            label = message.cold_outreach_label()
            if label is None:
                return None
            owner, contact, business = label
        if not self.store.stop_exists(contact, business):
            return None
        return {"workflow_owner": owner, "intent_id": intent["intent_id"] if intent is not None else None}

    def integration_revoked(self, message):
        """Current client scope is rechecked at dispatch, not only when queued."""
        intent = self.store.integration_intent_by_request(message.request_id)
        if intent is None:
            return False
        client = next((item for item in self.settings.integrations if item.client_id == intent["client_id"]), None)
        return client is None or not routes.submit_permitted(self, client, message.mailbox, message.business_id,
            message.sender_ref)

    def start_send(self, mailbox, message, row, sender, *, scheduled=False):
        # The worker's final send claim: once a row leaves 'queued' here, the guard
        # can no longer cancel it (matching the existing owner-cancel boundary).
        if row["state"] == "queued" and self.integration_revoked(message):
            return Store.receipt(self.store.save_send(message.request_id, "canceled",
                result={"reason": routes.REVOKED_REASON, "not_sent": True}))
        guard = self.stop_guard_intent(message)
        if guard is not None:
            result = {"reason": "stop_guard", "not_sent": True, "workflow_owner": guard["workflow_owner"]}
            if guard["intent_id"] is not None:
                result["intent_id"] = guard["intent_id"]
            return Store.receipt(self.store.save_send(message.request_id, "canceled", result=result))
        if self.expired(message):
            return Store.receipt(self.store.save_send(message.request_id, "failed",
                result={"error": "approval_expired", "not_sent": True}))
        if sender and self.outlook_provider is not None:
            try:
                refresh = getattr(self.outlook_provider, "refresh_quota", None)
                if refresh is not None:
                    refresh()
            except Exception as exc:
                # This read-only preflight precedes reservations and draft writes.
                # The existing queued approval can retry; never consume capacity
                # or expose provider exception text when its snapshot is unavailable.
                error = exc if isinstance(exc, (ProviderError, ValueError)) else ProviderError("outlook_quota_refresh_failed")
                return self.reconciliation_receipt(row, error, stage="quota", scheduled=scheduled)
            if self.expired(message):
                return Store.receipt(self.store.save_send(message.request_id, "failed",
                    result={"error": "approval_expired", "not_sent": True}))
        if sender:
            reserved, row = self.store.reserve(message.request_id, sender)
            if not reserved:
                return Store.receipt(row)
        self.store.flush_history()
        try:
            provider = self.outbound_provider(row)
            draft = provider.create_draft(mailbox, message)
            identity = draft.get("id")
            if not isinstance(identity, str) or not identity:
                raise ProviderError("missing_provider_draft", uncertain=True)
            self.store.save_send(message.request_id, "draft", draft_id=identity)
            return self.prepare_and_send(mailbox, message, identity, scheduled=scheduled)
        except ProviderError as exc:
            return self.record_error(message.request_id, exc, scheduled=scheduled)

    def prepare_and_send(self, mailbox, message, identity, *, scheduled=False):
        try:
            sender = self.settings.sender(mailbox, message)
        except ValueError as exc:
            raise ProviderError("mailbox_configuration_changed") from exc
        if self.expired(message):
            raise ProviderError("approval_expired", status=400)
        if sender:
            reserved, row = self.store.reserve(message.request_id, sender)
            if not reserved:
                return Store.receipt(row)
        row = self.store.send(message.request_id)
        if row["transport"] != (message.transport or "fastmail"):
            raise ProviderError("saved_transport_mismatch", status=400)
        provider = self.outbound_provider(row)
        draft = provider.get_message(mailbox, identity)
        self.verify_draft(mailbox, message, draft, provider=provider)
        # No recovery path resubmits after this durable boundary.
        self.store.save_send(message.request_id, "sending", draft_internet_message_id=draft.get("internetMessageId"))
        self.store.flush_history()
        submission = provider.send_draft(mailbox, identity, message)
        # RFC 8621 permits immediate removal of submission objects after sending.
        return self.reconcile(self.store.save_send(message.request_id, "accepted",
            result={"submission": submission}), scheduled=scheduled)

    @staticmethod
    def transient_error(exc):
        status = getattr(exc, "status", None)
        return (getattr(exc, "uncertain", False) or status == 429
            or type(status) is int and status >= 500
            or getattr(exc, "code", None) == "fastmail_connection_failed")

    @staticmethod
    def failure_code(exc):
        if isinstance(exc, ProviderError) and isinstance(exc.code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", exc.code):
            return exc.code
        if isinstance(exc, ValueError):
            return "reconciliation_configuration_invalid"
        return "provider_reconciliation_failed"

    @staticmethod
    def failure_stage(row, stage=None):
        if stage:
            return stage
        return "submission" if row["state"] in {"sending", "accepted", "uncertain"} else "draft"

    @staticmethod
    def submission_status(row, result, *, stage=None, exc=None):
        if row["state"] == "sent":
            return "confirmed"
        if row["state"] in {"failed", "canceled"}:
            return "failed"
        if exc is not None and stage == "submission":
            return "uncertain" if EmailService.transient_error(exc) else "failed"
        submission = result.get("submission") if isinstance(result, dict) else None
        if isinstance(submission, dict):
            if submission.get("undoStatus") == "final":
                return "confirmed"
            if submission.get("undoStatus") == "canceled":
                return "failed"
            return "waiting"
        if row["state"] == "sending":
            return "submitting"
        if row["state"] == "uncertain":
            return "uncertain"
        return "waiting"

    @staticmethod
    def business_copy_status(row, *, stage=None, exc=None):
        if row["state"] == "sent":
            return "linked"
        if row["state"] in {"failed", "canceled"}:
            return "blocked"
        if exc is not None and stage == "business_copy":
            return "pending" if EmailService.transient_error(exc) else "blocked"
        return "pending"

    @staticmethod
    def correlation(row, result):
        submission = result.get("submission") if isinstance(result, dict) else None
        correlation = {"request_id": row["request_id"]}
        if row.get("draft_id"):
            correlation["draft_id"] = row["draft_id"]
        if isinstance(submission, dict) and submission.get("id"):
            correlation["submission_id"] = submission["id"]
        for key in ("submission_id", "message_id", "internet_message_id", "native_internet_message_id"):
            if result.get(key):
                correlation[key] = result[key]
        return correlation

    def reconciliation_diagnostics(self, row, result, *, stage=None, exc=None, previous=None, retry=True):
        previous = previous if isinstance(previous, dict) else {}
        prior_failure = previous.get("failure") if isinstance(previous.get("failure"), dict) else None
        current_failure = None if exc is None else {
            "stage": self.failure_stage(row, stage), "code": self.failure_code(exc),
            **({"provider_status": exc.status} if type(getattr(exc, "status", None)) is int
                and 100 <= exc.status <= 599 else {})}
        same_failure = (current_failure is None and prior_failure is None
            or current_failure is not None and prior_failure == current_failure)
        count = previous.get("check_count", 0) if same_failure else 0
        if type(count) is not int or count < 0:
            count = 0
        count += 1
        checked_at = store_module.now()
        next_check_at = None
        if retry:
            delay = RECONCILIATION_RETRY_DELAYS[min(count - 1, len(RECONCILIATION_RETRY_DELAYS) - 1)]
            retry_after = getattr(exc, "retry_after", None)
            if type(retry_after) is int and retry_after > delay:
                # A provider deadline is authoritative even when it exceeds
                # the local cap; never poll earlier than Retry-After.
                delay = retry_after
            next_check_at = (datetime.fromisoformat(checked_at).astimezone(timezone.utc)
                + timedelta(seconds=delay)).isoformat()
        diagnostics = {
            "submission_status": self.submission_status(row, result, stage=stage, exc=exc),
            "business_copy_status": self.business_copy_status(row, stage=stage, exc=exc),
            "correlation": self.correlation(row, result),
            "last_check_at": checked_at,
            "check_count": count,
            "next_check_at": next_check_at,
            "retry_safety": "same_request_no_resend",
        }
        if current_failure is not None:
            diagnostics["failure"] = current_failure
        return diagnostics

    def record_error(self, request_id, exc, *, stage=None, scheduled=False):
        row = self.store.send(request_id)
        transient = self.transient_error(exc)
        if row["state"] in {"sending", "accepted"} and (transient or exc.status is None):
            state = "uncertain"
        elif row["state"] in {"creating", "draft"} and transient:
            state = row["state"]
        else:
            state = "failed"
        result = {"error": exc.code, "provider_status": exc.status,
            **({"not_sent": True} if state == "failed" and row["state"] in {"queued", "creating", "draft"} else {})}
        previous = json.loads(row["result"])
        prior_diagnostics = previous.get("reconciliation")
        failure = {"stage": self.failure_stage({**row, "state": state}, stage), "code": self.failure_code(exc),
            **({"provider_status": exc.status} if type(getattr(exc, "status", None)) is int
                and 100 <= exc.status <= 599 else {})}
        prior_failure = prior_diagnostics.get("failure") if isinstance(prior_diagnostics, dict) else None
        if scheduled or not isinstance(prior_diagnostics, dict) or prior_failure != failure:
            result["reconciliation"] = self.reconciliation_diagnostics({**row, "state": state}, result,
                stage=stage, exc=exc, previous=prior_diagnostics, retry=state in PENDING_SEND_STATES)
        elif isinstance(prior_diagnostics, dict):
            result["reconciliation"] = prior_diagnostics
        return Store.receipt(self.store.save_send(request_id, state, result=result))

    def reconciliation_receipt(self, row, exc=None, *, stage=None, scheduled=False):
        """Expose lookup failures without changing send evidence or authorizing a retry."""
        previous = json.loads(row["result"])
        result = {key: value for key, value in previous.items()
            if key not in {"reconciliation", "reconciliation_error", "reconciliation_provider_status"}}
        if exc is not None:
            code = self.failure_code(exc)
            if type(getattr(exc, "status", None)) is int and 100 <= exc.status <= 599:
                result["reconciliation_provider_status"] = exc.status
            result["reconciliation_error"] = code
            prior_diagnostics = previous.get("reconciliation")
            failure = {"stage": self.failure_stage(row, stage), "code": code,
                **({"provider_status": exc.status} if type(getattr(exc, "status", None)) is int
                    and 100 <= exc.status <= 599 else {})}
            prior_failure = prior_diagnostics.get("failure") if isinstance(prior_diagnostics, dict) else None
            if scheduled or not isinstance(prior_diagnostics, dict) or prior_failure != failure:
                result["reconciliation"] = self.reconciliation_diagnostics(row, result,
                    stage=stage, exc=exc, previous=prior_diagnostics, retry=row["state"] in PENDING_SEND_STATES)
            elif isinstance(prior_diagnostics, dict):
                result["reconciliation"] = prior_diagnostics
        elif scheduled and row["state"] in PENDING_SEND_STATES:
            result["reconciliation"] = self.reconciliation_diagnostics(row, result,
                stage=stage, previous=previous.get("reconciliation"), retry=True)
        if result != previous:
            row = self.store.save_send(row["request_id"], row["state"], result=result)
        return Store.receipt(row)

    def reconcile(self, row, *, scheduled=False):
        if row["state"] in {"sent", "failed", "canceled"}:
            return Store.receipt(row)
        stage = None
        try:
            mailbox = self.settings.mailbox(row["mailbox"])
            original = Outgoing.model_validate_json(row["payload"])
            if row["transport"] != (original.transport or "fastmail"):
                raise ValueError("Saved transport does not match the approval")
            if row["state"] in {"queued", "creating", "draft"}:
                try:
                    sender = self.settings.sender(mailbox, original)
                except ValueError:
                    return self.record_error(row["request_id"], ProviderError("mailbox_configuration_changed", status=400), scheduled=scheduled)
                if self.expired(original):
                    return Store.receipt(self.store.save_send(row["request_id"], "failed",
                        result={"error": "approval_expired", "not_sent": True}))
                if row["state"] == "queued":
                    return self.start_send(mailbox, original, row, sender, scheduled=scheduled)
            provider = self.outbound_provider(row)
            stage = "draft"
            if not row["draft_id"]:
                draft = provider.find_draft(mailbox, row["request_id"])
                if draft is None:
                    return self.reconciliation_receipt(row, scheduled=scheduled)
                row = self.store.save_send(row["request_id"], "draft" if row["state"] == "creating" else "uncertain", draft_id=draft["id"])
            if row["state"] == "draft":
                try:
                    return self.prepare_and_send(mailbox, original, row["draft_id"], scheduled=scheduled)
                except ProviderError as exc:
                    return self.record_error(row["request_id"], exc, scheduled=scheduled)
            stage = "submission"
            submission = json.loads(row["result"]).get("submission")
            if not submission or submission.get("undoStatus") not in {"final", "canceled"}:
                submission = provider.submission(mailbox, row["draft_id"], original)
                if submission:
                    result = {"submission": submission}
                    prior = json.loads(row["result"]).get("reconciliation")
                    if isinstance(prior, dict):
                        result["reconciliation"] = prior
                    row = self.store.save_send(row["request_id"], row["state"], result=result)
            if submission and submission.get("undoStatus") == "final":
                stage = "business_copy"
                message = provider.get_message(mailbox, row["draft_id"])
                row = self.store.save_send(row["request_id"], "sent", result={
                    "message_id": message["id"], "internet_message_id": message.get("internetMessageId"),
                    "conversation_id": message.get("conversationId"), "sent_at": submission.get("sendAt"),
                    "submission_id": submission["id"], "url": message.get("webLink"), "delivery": "submission_confirmed",
                    **({"outlook_message_id": message.get("outlookMessageId"),
                        "outlook_conversation_id": message.get("outlookConversationId"),
                        "native_internet_message_id": message.get("nativeInternetMessageId")}
                        if row["transport"] == "outlook_web" else {})})
            elif submission and submission.get("undoStatus") == "canceled":
                row = self.store.save_send(row["request_id"], "failed", result={"error": "provider_submission_canceled"})
            return self.reconciliation_receipt(row, scheduled=scheduled)
        except (ProviderError, ValueError) as exc:
            return self.reconciliation_receipt(self.store.send(row["request_id"]) or row, exc,
                stage=stage, scheduled=scheduled)

    def reconcile_pending(self, *, scheduled=False):
        with self.send_lock:
            self.store.flush_history()
            rows = self.store.pending_sends(due_only=scheduled, as_of=store_module.now())
            results = [self.reconcile(row, scheduled=scheduled) for row in rows]
            self.store.flush_history()
            return results

    def cancel(self, mailbox, request_id):
        with self.send_lock:
            result = Store.receipt(self.store.cancel_queued(request_id, mailbox))
            self.store.flush_history()
            return result

    # -- Integration envelope --

    @staticmethod
    def intent_receipt(intent_row, send_row):
        result = json.loads(send_row["result"]) if isinstance(send_row["result"], str) else send_row["result"]
        state = INTENT_RECEIPT_STATES.get(send_row["state"])
        if state is None:
            if result.get("reason") in {"sender_quota", "shared_sender_quota"}:
                state = "capacity_deferred"
            elif send_row["state"] == "queued":
                state = "queued"
            else:
                # creating/draft/sending/accepted: already past the guard's cancel boundary.
                state = "sending"
        # not_sent marks rows that left 'queued' straight to a terminal state without
        # ever reaching start_send's provider call (owner/guard cancel, pre-send expiry).
        transmission_began = send_row["state"] != "queued" and not result.get("not_sent")
        return {"intent_id": intent_row["intent_id"], "client_id": intent_row["client_id"],
            "request_id": intent_row["request_id"], "category": intent_row["category"],
            "workflow_owner": intent_row["workflow_owner"], "state": state,
            "transmission_began": transmission_began, "updated_at": send_row["updated_at"],
            "result": result}

    @staticmethod
    def tombstone_receipt(client, intent_id, tombstone):
        return {"intent_id": intent_id, "client_id": client.client_id, "request_id": None,
            "category": None, "workflow_owner": None, "state": "cancelled", "cancelled": True,
            "transmission_began": False, "tombstone": True, "updated_at": tombstone["created_at"],
            "result": {"reason": "integration_canceled", "not_sent": True}}

    def submit_intent(self, client, submission):
        """Validate and enqueue; always 'queued' for the worker to dispatch later.
        See docs/INTEGRATION-CONTRACT.md "Resolved ambiguities"."""
        correlation = submission.correlation
        message_payload = submission.message
        business_id = message_payload.get("business_id") if isinstance(message_payload, dict) else None
        if correlation.workflow_owner not in (None, client.workflow_owner):
            raise ValueError("correlation.workflow_owner must match the integration client")
        if correlation.business_id not in (None, business_id):
            raise ValueError("correlation.business_id must match message.business_id")
        if correlation.stop_key.business_id != business_id:
            raise ValueError("correlation.stop_key.business_id must match message.business_id")
        if not isinstance(message_payload, dict):
            raise ValueError("Mailbox is outside this integration client's scope")
        if (message_payload.get("mailbox") in client.allowed_mailboxes and client.business_ids
                and business_id not in client.business_ids):
            raise ValueError("Business is outside this integration client's scope")
        if not routes.submit_permitted(self, client, message_payload.get("mailbox"), business_id,
                message_payload.get("sender_ref")):
            raise ValueError("Mailbox is outside this integration client's scope")
        request_id = intent_request_id(client.client_id, submission.intent_id)
        substituted = {**message_payload, "request_id": request_id, "category": submission.category,
            "workflow_owner": client.workflow_owner, "stop_key": correlation.stop_key.model_dump(),
            "expires_at": message_payload.get("expires_at") or submission.expires_at}
        if substituted["expires_at"] != submission.expires_at:
            raise ValueError("The wrapped message's expires_at must match the envelope deadline")
        message = Outgoing.model_validate(substituted)
        mailbox = self.settings.mailbox(message.mailbox)
        payload_digest = message.digest()
        with self.send_lock:
            # Checked inside the same critical section as begin_integration_intent below, so a
            # cancel that tombstoned this id always wins a race with a late, lock-queued submit.
            if self.store.cancellation_tombstone(client.client_id, submission.intent_id):
                raise IntentCancelled("Intent was cancelled before this submission")
            existing_intent = self.store.integration_intent(client.client_id, submission.intent_id)
            if existing_intent:
                if existing_intent["payload_digest"] != payload_digest:
                    raise IntentConflict("Intent already recorded with different content")
                return self.intent_receipt(existing_intent, self.store.send(existing_intent["request_id"]))
            # Fail fast on an unresolvable sender/business combination rather than
            # leaving a doomed row queued for the background pass to discover.
            self.settings.sender(mailbox, message)
            intent_row, _ = self.store.begin_integration_intent(client_id=client.client_id,
                intent_id=submission.intent_id, request_id=request_id, payload_digest=payload_digest,
                workflow_owner=client.workflow_owner, business_id=message.business_id, mailbox=message.mailbox,
                category=submission.category, campaign_id=correlation.campaign_id,
                enrollment_id=correlation.enrollment_id, step_index=correlation.step_index,
                manifest_digest=correlation.manifest_digest, stop_contact_key=correlation.stop_key.contact_key,
                stop_business_id=correlation.stop_key.business_id, correlation=correlation.model_dump())
            row, _ = self.store.begin_send(message, queued=True)
            return self.intent_receipt(intent_row, row)

    def intent(self, client, intent_id):
        intent_row = self.store.integration_intent(client.client_id, intent_id)
        if intent_row is not None:
            return self.intent_receipt(intent_row, self.store.send(intent_row["request_id"]))
        tombstone = self.store.cancellation_tombstone(client.client_id, intent_id)
        if tombstone is not None:
            return self.tombstone_receipt(client, intent_id, tombstone)
        return None

    def cancel_intent(self, client, intent_id):
        """A never-submitted id is tombstoned rather than 404ed, so a late submit that was
        waiting on send_lock is rejected instead of silently queuing; see the contract doc."""
        with self.send_lock:
            intent_row = self.store.integration_intent(client.client_id, intent_id)
            if intent_row is not None:
                row = self.store.cancel_send_if_queued(intent_row["request_id"], reason="integration_canceled")
                self.store.flush_history()
                return self.intent_receipt(intent_row, row)
            tombstone = self.store.record_cancellation_tombstone(client.client_id, intent_id)
        return self.tombstone_receipt(client, intent_id, tombstone)

    def handoff(self, client, thread_or_message_id, request):
        """request.workflow_owner is the DESTINATION; must be the client's own owner
        or one of its configured handoff_destinations."""
        destination = request.workflow_owner or client.workflow_owner
        if destination != client.workflow_owner and destination not in client.handoff_destinations:
            raise ValueError("workflow_owner must be the client's own owner or a configured handoff destination")
        payload_digest = hashlib.sha256(json.dumps({"thread_or_message_id": thread_or_message_id,
            "workflow_owner": destination}, sort_keys=True).encode()).hexdigest()
        result = {"thread_or_message_id": thread_or_message_id, "workflow_owner": destination,
            "client_id": client.client_id, "ownership": "transferred"}
        mailboxes, business_ids = routes.handoff_scope(self, client)
        row, _ = self.store.begin_handoff(thread_or_message_id, request.idempotency_key,
            client_id=client.client_id, source_owner=client.workflow_owner, destination_owner=destination,
            payload_digest=payload_digest, result=result, mailboxes=mailboxes,
            business_ids=business_ids)
        if row["payload_digest"] != payload_digest:
            raise IntentConflict("Handoff already recorded with different content")
        return {"thread_or_message_id": row["thread_or_message_id"], "idempotency_key": row["idempotency_key"],
            **json.loads(row["result"])}
