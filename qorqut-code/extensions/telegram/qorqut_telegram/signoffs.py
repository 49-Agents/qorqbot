"""Signoffs in Telegram: one post per eligible item, exact reactions and replies, and cleanup after a durable handoff.

Nothing here is a second decision path. Posts come from the same owner deliveries the dashboard shows
(``SignoffNotifications``: scope, visibility, ``attention.notify``, urgent/blocking, snooze, reminders, supersession).
A thumb reaction or a reply is accepted only from the exact owner in the Signoffs chat on a known active post whose
frozen binding (category, proposal revision, payload hash, status revision) still matches; it is recorded as a durable
intent in the inbound transaction and executed through ``SignoffService.review`` / ``respond`` with an owner context
rebuilt here, under a deterministic idempotency key. The first accepted canonical decision wins. Retirement and
deletion of bot posts and owner replies only follow committed canonical state.
"""

from __future__ import annotations

from dataclasses import replace
import json
import logging

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.agents.global_scope import active_global
from bazarlyk.signoffs.execution import execution_sql
from qorqut_operations.signoffs.inputs import validate_response
from qorqut_operations.signoffs.notes import eligible as has_owner_note
from qorqut_operations.storage.repositories import decode_row
from . import store
from .bridge import MISMATCH_CODES
from .inbound import attachment_marker, clean_text
from .outbox import enqueue_notice, enqueue_text

LOGGER = logging.getLogger(__name__)
UP, DOWN = "👍", "👎"
THUMBS = (UP, DOWN)
REFRESH_SECONDS = 24 * 3600
REACTION_FEEDBACK = "Owner reacted 👎 in Telegram"
MAX_NOTES = 8000
TEXT_BUDGET = 3800
PREVIEW_BUDGET = 1600
DECISION_ATTEMPTS = 8
# A post retired for these reasons is not re-posted for the same delivery and binding; the next reminder may post again.
SUPPRESSING = ("replied", "decision_failed", "send_failed")
RETRYABLE = frozenset({"busy", "maintenance", "operation_unavailable", "storage_error", "lock_held"})
HELP = {
    "action": "React 👍 to approve or 👎 to reject. A reply adds a comment and never decides.",
    "binary_input": "React 👍 for Yes or 👎 for No, or reply with a written answer.",
    "input": "Reply to this message with your answer. Choices can only be selected in the dashboard.",
}
# A form that requires a choice is not answered by written text alone: a reply is saved as a comment and the request stays open.
CHOICE_HELP = {
    "binary_input": "React 👍 for Yes or 👎 for No. A written reply is saved as a comment and leaves the question open.",
    "input": "This question needs a choice, which can only be selected in the dashboard. A written reply is saved as a comment "
             "and leaves the question open.",
}
KINDS = {"input_request": "Question"}


def ignored(reason):
    return "ignored", reason, None


class Budget:
    """Clips text to fit while remembering that something was cut, so the post can say so."""

    def __init__(self):
        self.truncated = False

    def clip(self, value, limit):
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        self.truncated = True
        return text[:limit - 1] + "…"


def form_of(row):
    """How the owner can answer from Telegram, from the frozen proposal: action, binary_input or input."""
    if row["category"] != "input_request":
        return "action"
    form = (row["preview_json"] or {}).get("input") or {}
    ids = sorted(choice["id"] for choice in form.get("choices", []))
    if form.get("selection_mode") == "single" and ids == ["no", "yes"] and not form.get("notes_required"):
        return "binary_input"
    return "input"


def binding(row):
    return row["proposal_revision"], row["payload_hash"], row["status_revision"]


PRIVATE_OPERATION = "whatsapp_send"


def bridgeable(item) -> bool:
    """Whether a delivery item may be mirrored into Telegram. A personal WhatsApp send carries the exact recipient and
    message text in its preview, and its decision stays in the dashboard's Signoffs view."""
    return item["operation_name"] != PRIVATE_OPERATION


class SignoffBridge:
    def __init__(self, bridge):
        self.bridge = bridge
        self._fingerprint = None
        self._swept_at = None
        bridge.signoff_bridge = self
        bridge.admit_hooks["signoff"] = self.admit
        bridge.sent_hooks["signoff"] = self.sent
        bridge.steps.append(self.run)

    # ----- access ---------------------------------------------------------------------------------------------

    @property
    def service(self):
        return getattr(self.bridge.runtime, "signoffs", None) or getattr(self.bridge.application, "signoffs", None)

    def owner_id(self, uow):
        """The Qorqut owner the bridge is pinned to, after revalidating it. Never whichever owner exists now."""
        return self.bridge.check_binding(uow)["owner"]

    def context(self, uow, business_id=None, *, owner=None):
        """The pinned owner's context for a review scope, rebuilt from current rights. Trusted paths only.

        ``owner`` is the owner a stored intent was accepted under: if it is no longer the pinned one, nothing is built.
        """
        pinned = self.owner_id(uow)
        if owner is not None and owner != pinned:
            raise DomainError("binding_changed", "The Telegram owner binding changed after this was accepted")
        return self.bridge.application.policy.context_for(pinned, business_id=business_id, review_business_id=business_id, uow=uow)

    @staticmethod
    def signoff(uow, signoff_id):
        return decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone())

    @staticmethod
    def post_of(uow, **where):
        column, value = next(iter(where.items()))
        row = uow.connection.execute(f"SELECT * FROM telegram_signoff_posts WHERE {column}=?", (value,)).fetchone()
        return store.decode(row)

    def url(self, dto):
        origin = self.bridge.config.dashboard_origin or ""
        if dto.get("review_business_id") is None:
            return f"{origin}/#/global/signoffs"
        return f"{origin}/#{dto.get('business_slug') or ''}"

    # ----- rendering ------------------------------------------------------------------------------------------

    def _field_lines(self, mapping, budget, limit=10):
        lines = []
        for name, value in list(mapping.items())[:limit]:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            lines.append(f"  {name}: {budget.clip(value, 240)}")
        if len(mapping) > limit:
            budget.truncated = True
        return lines

    def _action_lines(self, dto, budget, share):
        preview = dto["preview"]
        lines = []
        if preview.get("rationale"):
            lines.append("Reason: " + budget.clip(preview["rationale"], 500))
        effects = preview.get("effects") or []
        lines += ["• " + budget.clip(effect, 220) for effect in effects[:8]]
        if len(effects) > 8:
            budget.truncated = True
        for change in (preview.get("changes") or [])[:6]:
            label = (change.get("resource") or {}).get("label", "")
            if change.get("kind") == "text":
                lines.append(f"- {label}: {budget.clip(change.get('after', ''), 500)}")
            else:
                for field in (change.get("fields") or [])[:8]:
                    lines.append(f"- {label} {field.get('path')}: {budget.clip(field.get('after'), 240)}")
        if len(preview.get("changes") or []) > 6:
            budget.truncated = True
        if dto.get("platform"):
            lines += self._field_lines(dto["platform"].get("submission") or {}, budget)
        return lines

    def _input_lines(self, dto, budget):
        form = (dto["preview"] or {}).get("input") or {}
        lines = [budget.clip(form.get("explanation", ""), 1200)]
        for choice in form.get("choices", [])[:26]:
            lines.append(f"• {choice['id']}: {budget.clip(choice['label'], 80)} — {budget.clip(choice['explanation'], 200)}")
        return lines

    def render(self, dto, form, url):
        for attempt in (PREVIEW_BUDGET, 800, 300, 0):
            budget = Budget()
            body = self._input_lines(dto, budget) if dto["category"] == "input_request" else self._action_lines(dto, budget, attempt)
            body = self._fit(body, attempt, budget)
            attention = dto.get("attention") or {}
            head = [f"{KINDS.get(dto['category'], 'Approval')}: {dto['title']}",
                    f"{dto.get('business_name') or 'Global'} · requested by {dto.get('proposer_display_name') or 'an agent'}"]
            tail = []
            if attention.get("reason"):
                tail.append("Why now: " + budget.clip(attention["reason"], 400))
            if attention.get("deadline"):
                tail.append("Deadline: " + attention["deadline"])
            needs_choice = bool(((dto.get("preview") or {}).get("input") or {}).get("selection_required"))
            tail += [CHOICE_HELP[form] if needs_choice and form in CHOICE_HELP else HELP[form], f"Open the Signoffs view: {url}"]
            if budget.truncated:
                tail.append(f"Truncated: the complete text is in the Signoffs view: {url}")
            text = "\n".join([*head, "", *body, "", *tail]).strip()
            if len(text) <= TEXT_BUDGET and store.utf16_units(text) <= store.MAX_UTF16_UNITS - 60:
                return text
        raise DomainError("validation_error", "The Signoff post does not fit a Telegram message")

    @staticmethod
    def _fit(lines, budget_chars, budget):
        if budget_chars <= 0:
            budget.truncated = budget.truncated or bool(lines)
            return []
        result, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget_chars:
                budget.truncated = True
                break
            result.append(line)
            used += len(line) + 1
        return result

    # ----- cycle ----------------------------------------------------------------------------------------------

    def fingerprint(self, uow):
        c = uow.connection
        sequence = c.execute("SELECT value FROM change_sequences WHERE name='signoffs'").fetchone()
        return (sequence[0] if sequence else None,
                tuple(c.execute("SELECT count(*), max(rowid), count(dismissed_at) FROM signoff_notifications").fetchone()),
                tuple(c.execute("SELECT count(*), max(updated_at) FROM signoff_attention").fetchone()))

    def run(self):
        bridge = self.bridge
        if not bridge.config.enabled or self.service is None:
            return False
        worked = self.execute_decisions()
        now = bridge.now()
        with bridge.read() as uow:
            current = self.fingerprint(uow)
            due = uow.connection.execute("SELECT 1 FROM telegram_signoff_posts WHERE state='active' AND refresh_at<=? LIMIT 1",
                                         (now,)).fetchone()
            stuck = uow.connection.execute(
                "SELECT 1 FROM telegram_signoff_posts p JOIN telegram_outbox o ON o.id=p.outbox_id WHERE p.state='queued' "
                "AND o.state IN ('failed','cancelled') LIMIT 1").fetchone()
        swept = self._swept_at is None or store.parse(now) - store.parse(self._swept_at) >= store.timedelta(
            seconds=bridge.timings["sweep_interval"])
        if current != self._fingerprint or due or stuck or swept:
            worked = self.reconcile(current) or worked
        return worked

    def reconcile(self, fingerprint=None):
        """Bring posts in line with the owner's current deliveries. Idempotent; the only writer of post lifecycle."""
        bridge = self.bridge
        now = bridge.now()
        changed = False
        with bridge.write("signoffs.reconcile") as uow:
            state = store.get_state(uow)
            if state is None:
                return False
            ctx = self.context(uow)
            current = {}
            for delivery in self.service.notifications.deliveries_in(ctx, uow)["items"]:
                for item in delivery["items"]:
                    if bridgeable(item):
                        current[item["id"]] = (delivery, item)
            for post in self._current_posts(uow):
                reason = self._retire_reason(uow, post, current, now, ctx.principal_id)
                if reason:
                    self.retire(uow, post, reason, now)
                    changed = True
            for post in self._refreshable(uow, now):
                changed = self._refresh(uow, ctx, post, current, now) or changed
            for signoff_id, (delivery, item) in current.items():
                if self._needs_post(uow, signoff_id, delivery, item):
                    changed = self._create_post(uow, ctx, delivery, item, state, now) or changed
            changed = self._send_failures(uow, now) or changed
            changed = self._status_notices(uow, ctx, state, now) or changed
            changed = self._release_held(uow, now) or changed
            changed = self.release_predecessors(uow, now) or changed
        self._fingerprint = fingerprint if fingerprint is not None else self._fingerprint
        self._swept_at = now
        if changed:
            bridge.wake()
        return changed

    @staticmethod
    def _current_posts(uow):
        return [store.decode(row) for row in uow.connection.execute(
            "SELECT * FROM telegram_signoff_posts WHERE state IN ('queued','active') AND input_form!='status' ORDER BY created_at, rowid")]

    def _retire_reason(self, uow, post, current, now, owner):
        message = store.message(uow, post["message_id"]) if post["message_id"] else None
        if message is not None and not store.in_destination(message, self.bridge.config.chat("signoffs")):
            # A post at a chat or topic that is no longer the configured Signoffs destination is never inherited (another topic
            # of the same group is not the old one either).
            return "chat_changed"
        row = self.signoff(uow, post["signoff_id"])
        if row is None or row["state"] != "pending" or row["superseded_by_id"]:
            return "resolved"
        entry = current.get(post["signoff_id"])
        if entry is None:
            attention = uow.connection.execute("SELECT * FROM signoff_attention WHERE signoff_id=?", (post["signoff_id"],)).fetchone()
            if attention and attention["notify"] and attention["snoozed_until"] and attention["snoozed_until"] > now:
                return "snoozed"
            if self._carried(uow, post, row, attention, owner):
                if post["refresh_at"] and post["refresh_at"] <= now:
                    return "owner_changed"  # nothing of the pinned owner can refresh it: its next delivery publishes a fresh post
                return None
            return "notify_cleared"
        delivery, item = entry
        if delivery["id"] != post["notification_id"] or binding(item) != (
                post["proposal_revision"], post["payload_hash"], post["expected_status_revision"]):
            return "replaced"
        return None

    @staticmethod
    def _carried(uow, post, row, attention, owner):
        """Whether an outstanding post is still the live delivery of the owner before an explicit repin (Manager decision 1019).

        The pinned owner has no delivery row for it yet. It stays while that delivery is undismissed, notifications are on and
        the canonical proposal mapping is exactly the one posted. A delivery of the pinned owner is never carried this way.
        """
        if attention is None or not attention["notify"] or not post["notification_id"]:
            return False
        delivery = uow.connection.execute("SELECT owner_id, dismissed_at, signoff_ids_json FROM signoff_notifications WHERE id=?",
                                          (post["notification_id"],)).fetchone()
        return bool(delivery and delivery["owner_id"] != owner and delivery["dismissed_at"] is None
                    and post["signoff_id"] in json.loads(delivery["signoff_ids_json"])
                    and binding(row) == (post["proposal_revision"], post["payload_hash"], post["expected_status_revision"]))

    def _needs_post(self, uow, signoff_id, delivery, item):
        if uow.connection.execute("SELECT 1 FROM telegram_signoff_posts WHERE signoff_id=? AND state IN ('queued','active') "
                                  "AND input_form!='status' LIMIT 1", (signoff_id,)).fetchone():
            return False
        proposal_revision, payload_hash, status_revision = binding(item)
        return not uow.connection.execute(
            "SELECT 1 FROM telegram_signoff_posts WHERE signoff_id=? AND notification_id=? AND proposal_revision=? AND payload_hash=? "
            "AND expected_status_revision=? AND retire_reason IN (%s) LIMIT 1" % ",".join("?" * len(SUPPRESSING)),
            (signoff_id, delivery["id"], proposal_revision, payload_hash, status_revision, *SUPPRESSING)).fetchone()

    def _detail(self, uow, ctx, item):
        scoped = replace(ctx, business_id=item["review_business_id"], review_business_id=item["review_business_id"])
        row = self.service._row(scoped, item["id"], uow)
        dto = self.service._dto(row, uow)
        dto.update(business_name=item.get("business_name"), business_slug=item.get("business_slug"))
        return row, dto

    def _create_post(self, uow, ctx, delivery, item, state, now):
        row, dto = self._detail(uow, ctx, item)
        if row["state"] != "pending" or binding(row) != binding(item):
            return False
        form = form_of(row)
        text = self.render(dto, form, self.url(dto))
        generation = (uow.connection.execute("SELECT max(generation) FROM telegram_signoff_posts WHERE signoff_id=?",
                                             (row["id"],)).fetchone()[0] or 0) + 1
        identity = new_id()
        message, outbox, _ = enqueue_text(self.bridge, uow, self.bridge.config.chat("signoffs"), text=text, purpose="signoff_post",
                                          source_kind="signoff", source_key=f"post:{identity}")
        uow.repository("telegram_signoff_posts").insert({
            "id": identity, "signoff_id": row["id"], "generation": generation, "notification_id": delivery["id"],
            "category": row["category"], "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"],
            "expected_status_revision": row["status_revision"], "input_form": form, "state": "queued",
            "message_id": message["id"], "outbox_id": outbox["id"], "activation_at": state["activation_at"],
            "created_at": now, "updated_at": now})
        return True

    # ----- lifecycle: retire, refresh, hooks --------------------------------------------------------------------

    def retire(self, uow, post, reason, now, *, cleanup=True, cancel_send=True):
        """Mark a post retired (it can no longer decide) and queue deletion of its known bot message. Idempotent.

        ``cancel_send=False`` is for the dispatcher's own admission check, which closes the outbox row itself.
        """
        current = uow.repository("telegram_signoff_posts").get(post["id"])
        if current is None or current["state"] == "retired":
            return current
        retired = uow.repository("telegram_signoff_posts").update(
            post["id"], {"state": "retired", "retire_reason": reason, "retired_at": now}, expected_revision=current["revision"])
        outbox = store.outbox(uow, current["outbox_id"]) if current["outbox_id"] else None
        if cancel_send and outbox is not None and outbox["state"] == "queued":
            store.set_outbox(uow, outbox, state="cancelled", error_code="post_retired")
        # A post that answers a reply nobody has received yet is kept until reconciliation completes that handoff.
        if cleanup and current["message_id"] and not current["handoff_json"]:
            store.enqueue_cleanup(uow, current["message_id"], "signoff_" + reason, now=now)
        return retired

    def release_predecessors(self, uow, now):
        """Delete a replaced or refreshed post once its successor is on screen (or there is no pending successor)."""
        released = False
        for row in uow.connection.execute(
                "SELECT p.* FROM telegram_signoff_posts p WHERE p.state='retired' AND p.retire_reason IN ('replaced','refreshed') "
                "AND p.handoff_json IS NULL AND p.message_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM telegram_cleanup_jobs c WHERE c.message_id=p.message_id) "
                "AND NOT EXISTS (SELECT 1 FROM telegram_signoff_posts q WHERE q.signoff_id=p.signoff_id AND q.state='queued' "
                "AND q.input_form!='status')").fetchall():
            store.enqueue_cleanup(uow, row["message_id"], "signoff_" + row["retire_reason"], now=now)
            released = True
        return released

    def _refreshable(self, uow, now):
        return [store.decode(row) for row in uow.connection.execute(
            "SELECT * FROM telegram_signoff_posts WHERE state='active' AND input_form!='status' AND refresh_at<=? "
            "ORDER BY refresh_at, rowid LIMIT 20", (now,))]

    def _refresh(self, uow, ctx, post, current, now):
        """Telegram deletes bot messages only within 48 hours: replace a 24-hour-old pending post with a new generation."""
        entry = current.get(post["signoff_id"])
        if entry is None:
            return False
        delivery, item = entry
        self.retire(uow, post, "refreshed", now, cleanup=False)
        state = store.get_state(uow)
        return self._create_post(uow, ctx, delivery, item, state, now)

    def admit(self, uow, outbox):
        """Recheck immediately before a post is sent: a resolved or replaced post is never sent."""
        post = self.post_of(uow, outbox_id=outbox["id"])
        if post is None or post["state"] == "retired":
            return "cancelled", "post_retired", "The Signoff post was retired before it was sent"
        if post["input_form"] == "status":
            return None
        row = self.signoff(uow, post["signoff_id"])
        if row is None or row["state"] != "pending" or row["superseded_by_id"] or binding(row) != (
                post["proposal_revision"], post["payload_hash"], post["expected_status_revision"]):
            self.retire(uow, post, "resolved", self.bridge.now(), cleanup=False, cancel_send=False)
            return "cancelled", "post_stale", "The Signoff changed before its post was sent"
        return None

    def sent(self, uow, outbox, provider_id):
        """Receipt hook (same transaction): the post is on screen. A post retired mid-send is deleted by its cleanup job."""
        post = self.post_of(uow, outbox_id=outbox["id"])
        if post is None:
            return
        now = self.bridge.now()
        if post["state"] == "queued":
            refresh = None if post["input_form"] == "status" else store.plus(now, REFRESH_SECONDS)
            uow.repository("telegram_signoff_posts").update(post["id"], {"state": "active", "refresh_at": refresh},
                                                            expected_revision=post["revision"])
        self.release_predecessors(uow, now)

    def _send_failures(self, uow, now):
        changed = False
        for post in self._current_posts(uow):
            outbox = store.outbox(uow, post["outbox_id"]) if post["outbox_id"] else None
            if post["state"] == "queued" and outbox is not None and outbox["state"] in ("failed", "cancelled"):
                self.retire(uow, post, "send_failed", now)
                changed = True
        return changed

    def _status_notices(self, uow, ctx, state, now):
        """A separate, non-actionable notice when a Signoff posted here later failed, conflicted or needs reconciliation.

        Approval counts as resolved; this only reports that what followed did not complete. Nothing is retried and the
        notice is not an actionable post, so no reaction or reply on it ever decides anything.
        """
        rows = uow.connection.execute(
            f"SELECT s.id, {execution_sql('s')} AS outcome FROM signoffs s WHERE s.category!='input_request' "
            "AND s.superseded_by_id IS NULL AND s.state!='rejected' AND EXISTS (SELECT 1 FROM telegram_signoff_posts p "
            "WHERE p.signoff_id=s.id AND p.input_form!='status') "
            f"AND {execution_sql('s')} IN ('failed','conflicted','reconciliation_required')").fetchall()
        changed = False
        for found in rows:
            row = self.signoff(uow, found["id"])
            kind = found["outcome"]
            marker = f"status:{kind}"
            if uow.connection.execute("SELECT 1 FROM telegram_signoff_posts WHERE signoff_id=? AND category=? AND input_form='status' "
                                      "AND expected_status_revision=?", (row["id"], marker, row["status_revision"])).fetchone():
                continue
            dto = self.service._dto(row, uow)
            business = uow.repository("businesses").get(row["review_business_id"]) if row["review_business_id"] else None
            dto.update(business_name=(business or {}).get("name") or "Global", business_slug=(business or {}).get("slug"))
            detail = (row["error_json"] or {}).get("message") or ((dto.get("platform") or {}).get("execution") or {}).get("error")
            detail = Budget().clip(detail if isinstance(detail, str) and detail else "The application did not complete.", 400)
            text = (f"Status: {dto['title']}\n{dto['business_name']}\n\nThis was approved but did not complete "
                    f"({kind.replace('_', ' ')}): {detail}\nNothing was retried automatically.\nDetails are in the Signoffs view: {self.url(dto)}")
            identity = new_id()
            generation = (uow.connection.execute("SELECT max(generation) FROM telegram_signoff_posts WHERE signoff_id=?",
                                                 (row["id"],)).fetchone()[0] or 0) + 1
            message, outbox, _ = enqueue_text(self.bridge, uow, self.bridge.config.chat("signoffs"), text=text[:store.MAX_UNICODE_CHARS],
                                              purpose="signoff_post", source_kind="signoff", source_key=f"post:{identity}")
            uow.repository("telegram_signoff_posts").insert({
                "id": identity, "signoff_id": row["id"], "generation": generation, "category": marker,
                "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"],
                "expected_status_revision": row["status_revision"], "input_form": "status", "state": "queued",
                "message_id": message["id"], "outbox_id": outbox["id"], "activation_at": state["activation_at"],
                "created_at": now, "updated_at": now})
            changed = True
        return changed

    # ----- inbound: reactions ----------------------------------------------------------------------------------

    def _notice(self, uow, update_id, code, text):
        enqueue_notice(self.bridge, uow, "signoffs", text, f"signoff:{update_id}:{code}")

    def route_reaction(self, uow, item, now):
        """A reaction names a chat and a message, never a topic. It is resolved through the bridge's own stored message and accepted
        only for a known Signoff post at exactly the current Signoffs destination: never by guessing a topic, never for another
        topic's message, and without any notice in a place the owner did not react."""
        added = [emoji for emoji in item.get("new") or [] if emoji not in (item.get("old") or [])]
        thumbs = [emoji for emoji in added if emoji in THUMBS]
        if not thumbs:
            return ignored("not_a_decision_reaction")
        ambiguous = len(added) != 1 or (UP in (item.get("new") or []) and DOWN in (item.get("new") or []))
        chat = self.bridge.config.chat("signoffs")
        message = store.message_by_provider(uow, item["chat_id"], item["message_id"])
        if message is not None and not store.in_destination(message, chat):
            return ignored("other_destination")
        post = self.post_of(uow, message_id=message["id"]) if message else None
        if post is None or post["input_form"] == "status":
            return ignored("ambiguous_reaction" if ambiguous else "unknown_post" if post is None else "not_actionable")
        if ambiguous:
            self._notice(uow, item["update_id"], "ambiguous", "Both 👍 and 👎 (or another reaction) are on that post, so nothing was "
                         "decided. Leave exactly one of them to decide, or reply to comment.")
            return ignored("ambiguous_reaction")
        row = self.signoff(uow, post["signoff_id"])
        if (post["state"] != "active" or row is None or row["state"] != "pending" or row["superseded_by_id"]
                or binding(row) != (post["proposal_revision"], post["payload_hash"], post["expected_status_revision"])):
            self._notice(uow, item["update_id"], "stale", self._stale_text(uow, row, "Your reaction decided nothing"))
            return ignored("stale_post")
        if post["decision_json"] is not None:
            return ignored("decision_already_recorded")
        if post["input_form"] == "input":
            self._notice(uow, item["update_id"], "form", "This request needs a written answer, so a reaction cannot answer it. "
                         f"Reply to its post, or answer it in the Signoffs view of the dashboard: {self._record_link(uow, row)}")
            return ignored("reaction_not_supported")
        up = thumbs[0] == UP
        if post["input_form"] == "action":
            decision = "approve" if up else "reject"
            feedback = REACTION_FEEDBACK if not up and not has_owner_note(uow, row["id"]) else None
        else:
            decision, feedback = ("yes" if up else "no"), None
        intent = {"kind": "reaction", "decision": decision, "feedback": feedback, "update_id": item["update_id"], "at": now,
                  "state": "pending", "attempts": 0, "binding": item["binding"]}
        uow.repository("telegram_signoff_posts").update(post["id"], {"decision_json": intent}, expected_revision=post["revision"])
        return "done", None, {"post_id": post["id"], "decision": decision}

    # ----- inbound: replies ------------------------------------------------------------------------------------

    def route_reply(self, uow, chat, item, now):
        update_id = item["update_id"]
        reply = (item.get("reply") or {}).get("message_id")
        parent = store.message_by_provider(uow, chat.chat_id, reply) if reply else None
        post = self.post_of(uow, message_id=parent["id"]) if store.in_destination(parent, chat) else None
        if post is None:
            self._notice(uow, update_id, "hint", "Reply directly to a Signoff post to respond to it; other messages here are not read.")
            return ignored("not_a_signoff_reply")
        raw = item.get("text") or ""
        attachments = item.get("attachments") or []
        text, refusal = clean_text(raw) if raw else (None, None if attachments else "empty")
        if item.get("refused"):  # refused when the update was ingested: only the refusal was kept
            text, refusal = None, item["refused"]
        if refusal is None and attachments:
            text = (text + "\n" if text else "") + attachment_marker(attachments)
            self._notice(uow, update_id, "attachments", "Attachments are not supported: only the text was recorded. "
                         "Files, images and voice are never downloaded. Send text, or use the dashboard upload flow for files.")
        message = store.insert_message(
            uow, direction="inbound", chat=chat, purpose="signoff_reply", body=text if refusal is None else f"[not delivered: {refusal}]",
            now=now, occurred_at=item["sent_at"], reply_to_id=parent["id"], reply_to_telegram_id=reply, update_id=update_id,
            telegram_message_id=item["message_id"])
        if refusal is not None:
            self._notice(uow, update_id, "refused", {"too_long": "That reply is longer than 16000 characters, so it was not recorded.",
                         "control_characters": "That reply contains terminal control characters, so it was not recorded.",
                         "empty": "That reply had no text to record."}[refusal])
            return "done", "refused_" + refusal, {"recorded": False}
        row = self.signoff(uow, post["signoff_id"])
        if row is None:
            return ignored("unknown_signoff")
        live = (post["state"] == "active" and row["state"] == "pending" and not row["superseded_by_id"]
                and post["decision_json"] is None
                and binding(row) == (post["proposal_revision"], post["payload_hash"], post["expected_status_revision"]))
        if live and row["category"] == "input_request" and self.answers_alone(row, text):
            intent = {"kind": "reply", "decision": "respond", "message_id": message["id"], "update_id": update_id, "at": now,
                      "state": "pending", "attempts": 0, "binding": item["binding"]}
            uow.repository("telegram_signoff_posts").update(post["id"], {"decision_json": intent}, expected_revision=post["revision"])
            return "done", None, {"post_id": post["id"], "recorded": "pending_response"}
        note_id = self.comment(uow, row, text, message, now, post=post, retire_post=live, binding=item["binding"])
        if note_id is None:
            return "done", "comment_failed", {"post_id": post["id"], "recorded": False}
        if not live:
            self._notice(uow, update_id, "stale-reply", self._stale_reply_text(uow, row))
        return "done", None, {"post_id": post["id"], "recorded": "comment", "note_id": note_id}

    def _record_link(self, uow, row):
        """The link that opens the Signoffs view of the owning workspace (a superseded record's replacement shares its scope).

        There is no deep link to one proposal, so the link is only ever described as opening the Signoffs view.
        """
        current = (self.signoff(uow, row["superseded_by_id"]) if row["superseded_by_id"] else None) or row
        business = uow.repository("businesses").get(current["review_business_id"]) if current["review_business_id"] else None
        return self.url({"review_business_id": current["review_business_id"], "business_slug": (business or {}).get("slug")})

    def _stale_text(self, uow, row, outcome):
        """Why a response decided nothing, and the Signoffs view where what is current can be seen."""
        if row is None:
            return f"{outcome}: that Signoff no longer exists."
        why = ("it was replaced by a newer proposal" if row["superseded_by_id"] else "it is no longer pending" if row["state"] != "pending"
               else "that post is no longer the current one")
        return f"{outcome}, because {why}. Open the Signoffs view (it lists what is current there): {self._record_link(uow, row)}"

    def _stale_reply_text(self, uow, row):
        return self._stale_text(uow, row, "Your reply was saved as a comment on the original Signoff and decided nothing")

    @staticmethod
    def answers_alone(row, text):
        """Whether the written text alone is a valid response to this input request (never guessing a choice)."""
        form = row["preview_json"]["input"]
        if form.get("selection_required"):
            return False
        try:
            validate_response(form, text, [])
        except DomainError:
            return False
        return True

    def comment(self, uow, row, text, message, now, *, post=None, retire_post=False, binding=None):
        """Record an owner reply as a canonical proposal comment and hand it to the proposer. Returns the note id.

        ``post`` is the post that was answered, live or not: a reply to a stale post whose proposer cannot receive it is held
        with that post too. Only a live post is retired by its own reply (``retire_post``)."""
        note = self._append_note(uow, row, text)
        if note is None:
            self._notice(uow, message["update_id"], "comment-failed", "Your reply could not be saved on the Signoff, so it stays here "
                         "and nothing was deleted. Open the Signoff in the dashboard to comment.")
            return None
        if self.handoff(uow, row, note["id"], message, now, post=post, binding=binding) and retire_post and post is not None:
            self.retire(uow, post, "replied", now)
        return note["id"]

    def _append_note(self, uow, row, text):
        """Save a canonical note under the current owner context, or None when authority or state refuses it. Nothing partial stays."""
        uow.connection.execute("SAVEPOINT telegram_note")
        try:
            note = self.service.notes.append(self.context(uow, row["review_business_id"]), row, text, uow)
        except DomainError:
            uow.connection.execute("ROLLBACK TO telegram_note")
            uow.connection.execute("RELEASE telegram_note")
            return None
        uow.connection.execute("RELEASE telegram_note")
        return note

    def handoff(self, uow, row, note_id, message, now, *, post=None, binding=None):
        """Durable delivery to the proposer. The canonical inbox event exists once the note or response commits.

        Returns whether a handoff now exists. A proposer that cannot receive it is reported once and nothing is deleted:
        the reply and the post it answered are held (``handoff_json`` on the post) until reconciliation finds a valid handoff.
        The queued direct input never expires; an offline agent receives it when its terminal is next available. ``binding`` is
        the identity the reply was accepted under (the current one when omitted): direct input is queued only under it.
        """
        binding = binding or self.bridge.check_binding(uow)
        if not self._can_receive(uow, row):
            self._notice(uow, message["update_id"], "undeliverable",
                         "Your reply was saved on the Signoff, but its proposer is not active, so nobody will receive it yet. "
                         "It is kept here until the proposer is active again; reassign or open it in the dashboard.")
            if post is not None:
                self._hold(uow, post, row, note_id, message, binding)
            return False
        self._queue_input(uow, row, note_id, message, now, binding)
        return True

    @staticmethod
    def _can_receive(uow, row):
        proposer = row["proposer_agent_id"]
        return bool(proposer) and active_global(uow.connection, proposer) is not None

    def _queue_input(self, uow, row, note_id, message, now, binding):
        """Queue the direct terminal input under the identity the reply was accepted under, then clean up the Telegram reply.

        If that identity is no longer current (an owner or binding change since), no input is queued: the canonical note and
        the proposer's inbox event already hold the reply, and old authority is never carried into the new generation.
        """
        proposer = row["proposer_agent_id"]
        if self._still_current(uow, binding) and not uow.connection.execute(
                "SELECT 1 FROM telegram_owner_inputs WHERE correlation_id=? AND agent_id=?", (note_id, proposer)).fetchone():
            profile = uow.repository("agent_profiles", business_id=None).get(proposer) or dict(uow.connection.execute(
                "SELECT * FROM agent_profiles WHERE agent_id=?", (proposer,)).fetchone())
            uow.repository("telegram_owner_inputs").insert({
                "id": new_id(), "purpose": "signoff_reply", "correlation_id": note_id, "message_id": message["id"],
                "agent_id": proposer, "signoff_id": row["id"], "owner_principal_id": binding["owner"],
                "config_generation": binding["generation"], "binding_fingerprint": binding["fingerprint"],
                "target_fingerprint": store.target_fingerprint(profile), "state": "queued", "created_at": now, "updated_at": now})
        store.enqueue_cleanup(uow, message["id"], "signoff_reply_handed_off", now=now)

    def _still_current(self, uow, binding):
        try:
            self.bridge.verify_frozen(uow, binding)
        except DomainError as exc:
            if exc.code in MISMATCH_CODES:
                raise
            return False
        return True

    @staticmethod
    def _hold(uow, post, row, note_id, message, binding):
        current = uow.repository("telegram_signoff_posts").get(post["id"])
        held = list(current["handoff_json"] or [])
        if not any(item["note_id"] == note_id for item in held):
            held.append({"note_id": note_id, "message_id": message["id"], "signoff_id": row["id"], "binding": binding})
            uow.repository("telegram_signoff_posts").update(post["id"], {"handoff_json": held}, expected_revision=current["revision"])

    def _release_held(self, uow, now):
        """Complete held handoffs once the proposer can receive them, then clean up the reply and the post they kept."""
        released = False
        for post in [store.decode(r) for r in uow.connection.execute(
                "SELECT * FROM telegram_signoff_posts WHERE handoff_json IS NOT NULL ORDER BY updated_at, rowid LIMIT 50")]:
            remaining = []
            for item in post["handoff_json"]:
                row = self.signoff(uow, item["signoff_id"])
                message = store.message(uow, item["message_id"])
                if row is None or message is None:
                    continue
                if self._can_receive(uow, row):
                    self._queue_input(uow, row, item["note_id"], message, now, item.get("binding") or self.bridge.check_binding(uow))
                else:
                    remaining.append(item)
            if len(remaining) == len(post["handoff_json"]):
                continue
            released = True
            posts = uow.repository("telegram_signoff_posts")
            current = posts.update(post["id"], {"handoff_json": remaining or None}, expected_revision=post["revision"])
            if remaining:
                continue
            if current["state"] == "retired" and current["message_id"]:
                store.enqueue_cleanup(uow, current["message_id"], "signoff_" + current["retire_reason"], now=now)
            elif current["state"] == "active":
                self.retire(uow, current, "replied", now)
        return released

    # ----- decisions -------------------------------------------------------------------------------------------

    def execute_decisions(self):
        bridge = self.bridge
        now = bridge.now()
        with bridge.read() as uow:
            posts = [store.decode(row) for row in uow.connection.execute(
                "SELECT * FROM telegram_signoff_posts WHERE json_extract(decision_json,'$.state')='pending' "
                "AND COALESCE(json_extract(decision_json,'$.next_attempt_at'),'')<=? ORDER BY updated_at, rowid LIMIT 5", (now,))]
        for post in posts:
            self._execute(post, post["decision_json"], now)
        return bool(posts)

    def _call(self, post, intent, row, ctx, text):
        common = dict(proposal_revision=post["proposal_revision"], payload_hash=post["payload_hash"],
                      expected_status_revision=post["expected_status_revision"])
        key = f"telegram:{post['id']}:{intent['kind']}"
        if intent["kind"] == "reaction" and intent["decision"] in ("approve", "reject"):
            return self.service.review(ctx, row["id"], decision=intent["decision"], idempotency_key=key,
                                       feedback=intent.get("feedback"), **common)
        if intent["kind"] == "reaction":
            return self.service.respond(ctx, row["id"], idempotency_key=key, notes="", selected_choices=[intent["decision"]], **common)
        return self.service.respond(ctx, row["id"], idempotency_key=key + f":{intent['update_id']}", notes=text, selected_choices=[], **common)

    def _execute(self, post, intent, now):
        bridge = self.bridge
        error = ctx = None
        with bridge.read() as uow:
            row = self.signoff(uow, post["signoff_id"])
            try:
                # The decision was accepted under one identity and generation and never runs under another. The owner
                # context is rebuilt for that pinned owner from current rights; a changed owner generation, archived
                # business or inactive identity is a reported failure for this one decision, never a stall of the cycle.
                frozen = intent.get("binding")
                if frozen is None:
                    raise DomainError("binding_changed", "The decision has no frozen Telegram binding")
                self.bridge.verify_frozen(uow, frozen)
                ctx = self.context(uow, row["review_business_id"], owner=frozen["owner"]) if row else None
            except DomainError as exc:
                if exc.code in MISMATCH_CODES:
                    raise
                error = exc
            reply = store.message(uow, intent["message_id"]) if intent.get("message_id") else None
        if row is None:
            self._finish(post, "failed", "unknown_signoff", now)
            return
        result = None
        if error is None:
            try:
                # Once more immediately before the call, so a change after the read above is not applied.
                with bridge.read() as uow:
                    bridge.verify_frozen(uow, intent["binding"])
                result = self._call(post, intent, row, ctx, reply["body"] if reply else "")
            except DomainError as exc:
                if exc.code in MISMATCH_CODES:
                    raise
                error = exc
        if error is not None:
            code, message, ok = error.code, error.message, False
        else:
            ok = result.status in ("ok", "accepted")
            code = None if ok else (result.error or {}).get("code", "operation_failed")
            message = None if ok else (result.error or {}).get("message", "")
        if ok:
            self._finish(post, "done", None, now)
        elif code in RETRYABLE and intent.get("attempts", 0) + 1 < DECISION_ATTEMPTS:
            self._retry(post, intent, code, now)
        elif code in ("stale_signoff", "revision_conflict", "invalid_transition", "not_found"):
            self._finish(post, "stale", code, now)
        else:
            self._finish(post, "failed", code, now, detail=message)

    def _retry(self, post, intent, code, now):
        bridge = self.bridge
        with bridge.write("signoffs.decision.retry") as uow:
            current = uow.repository("telegram_signoff_posts").get(post["id"])
            if current is None or (current["decision_json"] or {}).get("state") != "pending":
                return
            attempts = current["decision_json"].get("attempts", 0) + 1
            updated = {**current["decision_json"], "attempts": attempts, "last_error": code,
                       "next_attempt_at": store.plus(now, bridge.backoff(attempts))}
            uow.repository("telegram_signoff_posts").update(post["id"], {"decision_json": updated}, expected_revision=current["revision"])

    def _finish(self, post, outcome, code, now, *, detail=None):
        """Record the decision result. Success retires the post; a stale binding turns a reply into a comment."""
        bridge = self.bridge
        with bridge.write("signoffs.decision.finish") as uow:
            current = uow.repository("telegram_signoff_posts").get(post["id"])
            if current is None or (current["decision_json"] or {}).get("state") != "pending":
                return
            intent = dict(current["decision_json"])
            row = self.signoff(uow, post["signoff_id"])
            reply = store.message(uow, intent["message_id"]) if intent.get("message_id") else None
            intent.update(state="done" if outcome == "done" else outcome, code=code, finished_at=now)
            note_id = None
            if outcome == "stale" and reply is not None and row is not None:
                # A stale post cannot decide, but the owner's words are still recorded as a comment.
                note = self._append_note(uow, row, reply["body"])
                if note is None:
                    outcome, code = "failed", "comment_failed"
                    intent.update(state="failed", code=code)
                else:
                    note_id = note["id"]
                    intent["note_id"] = note_id
                    self.handoff(uow, row, note_id, reply, now, post=post, binding=intent["binding"])
            elif outcome == "done" and reply is not None and row is not None:
                note_id = ((row["decision_json"] or {}).get("message_id"))
                if note_id:
                    intent["note_id"] = note_id
                    self.handoff(uow, row, note_id, reply, now, post=post, binding=intent["binding"])
                else:
                    store.enqueue_cleanup(uow, reply["id"], "signoff_reply_handed_off", now=now)
            # An unhanded reply holds the post's cleanup (see ``retire``); read the revision after a hold was recorded.
            revision = uow.repository("telegram_signoff_posts").get(post["id"])["revision"]
            uow.repository("telegram_signoff_posts").update(post["id"], {"decision_json": intent}, expected_revision=revision)
            reason = {"done": "resolved", "stale": "stale"}.get(outcome, "decision_failed")
            # A canonical failure retains the post and the owner's reply: nothing is deleted before a save and handoff.
            self.retire(uow, uow.repository("telegram_signoff_posts").get(post["id"]), reason, now, cleanup=outcome != "failed")
            if outcome == "stale" and reply is None and (row or {}).get("state") != "conflicted":
                self._notice(uow, intent["update_id"], "stale-result", self._stale_text(
                    uow, row, "That Signoff changed before your reaction was applied, so nothing was decided") if row else
                    "That Signoff changed before your reaction was applied, so nothing was decided.")
            elif outcome == "failed":
                self._notice(uow, intent["update_id"], "failed", f"Your response could not be applied ({code}). Open the Signoff in the "
                             "dashboard to decide it.")
        bridge.wake()

    # ----- status ------------------------------------------------------------------------------------------------

    def status_counts(self, uow):
        c = uow.connection
        return {"active_posts": c.execute("SELECT count(*) FROM telegram_signoff_posts WHERE state='active' AND input_form!='status'").fetchone()[0],
                "queued_posts": c.execute("SELECT count(*) FROM telegram_signoff_posts WHERE state='queued'").fetchone()[0],
                "pending_decisions": c.execute("SELECT count(*) FROM telegram_signoff_posts "
                                               "WHERE json_extract(decision_json,'$.state')='pending'").fetchone()[0],
                "held_replies": c.execute("SELECT count(*) FROM telegram_signoff_posts WHERE handoff_json IS NOT NULL").fetchone()[0]}
