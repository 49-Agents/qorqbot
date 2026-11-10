"""Observe-only in-process change feed: publish, never delay or fail a commit.

Wired into ``Database._unit`` (capture before ``finalize_events()`` deletes
``inbox_changes``, publish after a successful commit) so dashboard tabs can
be pushed to instead of polling on a timer. Nothing here may raise into the
caller's commit path: `ChangeFeed.publish_commit` swallows and counts any
exception of its own.

Kind mapping is deliberately coarse where the source data does not cleanly
name one dashboard concept (`relationships` links two typed endpoints;
tables with no capture trigger have no row shape to inspect at all). Kinds
are safe to over-bump -- an extra cache miss -- never safe to under-bump,
which would let a versioned read 304 on stale data. Where a table's own
dashboard relevance is not one of the nine kinds the dashboard actually
reads by (``inbox_*`` tables, ``agent_messages``,
``focus_cards``), it is left unmapped rather than guessed onto the nearest
kind: no ETag'd route (PR3) currently declares a dependency on it.

Scope attribution is also incomplete for some trigger-covered tables, not
only for the coarse ones: `signoff_attention`'s capture trigger and
`agent_profiles`/`principals`' capture triggers (migration 035) never
include a `business_id`/`review_business_id` key in the captured row (the
first has none of the signoff's own scope at all; the other two carry
`home_business_id`, a differently-named column `_scope_of` does not look
for) -- confirmed by reading those triggers directly, not assumed. Every
write to those specific tables therefore lands at GLOBAL_SCOPE regardless
of which business the row actually belongs to, while `business_memberships`
and `relationships` (the other tables behind "agents" and "signoffs") do
carry a real `business_id` and scope correctly. PR3's ETag must check both
scopes for "signoffs" and "agents" -- see ALSO_GLOBAL_KINDS and
`route_versions` below. (`signoff_notes` looked like a third instance of
this on first read, since migration 066 defines no capture trigger for it
at all -- but `qorqut/signoffs/notes.py:append` inserts its own
`inbox_changes` row by hand, with a real `business_id` copied from the
signoff it belongs to, so it scopes correctly and needs no such widening.)
"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
import time
from collections import deque

from qorqut_operations.contracts import new_id

LOGGER = logging.getLogger(__name__)

RING_SIZE = 1000
HEARTBEAT_SECONDS = 15
EXTERNAL_WRITER_POLL_SECONDS = 1
GLOBAL_SCOPE = "global"
# Sentinel `scope` for ChangeFeed.route_versions: "every scope currently
# known for this kind," not one business. A real scope is always a string
# (a business id or GLOBAL_SCOPE), so this can't collide with one.
ANY_SCOPE = object()

# Trigger-covered tables (the capture triggers of the committed host DDL and the
# engine and extension contributions) -> the dashboard "kind" they affect. `signoff_notes` has a capture
# trigger but is consumed by `finalize_events` before reaching `targets()`
# uniformly with the others; it still lands in `inbox_changes` and is mapped
# here directly rather than relying on that indirection.
KIND_BY_TABLE = {
    "tasks": "tasks",
    "signoffs": "signoffs", "signoff_attention": "signoffs", "signoff_notes": "signoffs",
    "documents": "documents", "context_entities": "documents",
    "action_logs": "action_logs", "action_businesses": "action_logs",
    "agent_profiles": "agents", "principals": "agents", "business_memberships": "agents",
    "data_tables": "tables", "data_rows": "tables",
    # CRM memberships belong to their Table's views; a recorded observation belongs to its Task's activity.
    "crm_entries": "tables", "crm_activity_events": "tasks",
}
# Engine record tables that name their item (`item_type` is 'task', 'action' or 'table') instead of belonging to one kind.
ITEM_RECORD_TABLES = frozenset({"thread_messages", "thread_follows", "item_tags"})
ITEM_KIND_BY_TYPE = {"task": "tasks", "action": "action_logs", "table": "tables"}
# A relationship names two typed endpoints (`from_type`/`to_type`); bump
# whichever kinds those endpoints belong to rather than invent a tenth
# "relationships" kind no route reads by.
RELATIONSHIP_ENDPOINT_KIND = {
    "task": "tasks", "signoff": "signoffs",
    "material": "documents", "topic": "documents", "person": "documents", "agent": "agents",
}
# Coarse tables (handoff 4.2) have no capture trigger, so there is no row to
# inspect -- only the acting operation's name, via OBSERVATION. Matched by
# prefix against this codebase's actual operation-naming convention
# (`routines_*`/`routine_*`, `jobs_*`/`job_*`, `learnings_*`/`learning_*`;
# confirmed against the registered operation names in
# `qorqut/domains/routines`, `qorqut/domains/learnings`). `agent_messages`,
# the inbox tables and `focus_cards` are coarse-only with no declared kind for
# the same reason as `requests` above: no current ETag'd route depends on them.
#
# The actual job/routine state transitions (a job claimed/succeeded/failed,
# a routine's due slot queued) are written by `qorqut/background/service.py`
# and `qorqut/background/routines.py`, entirely outside `Application.invoke`
# -- discovered wiring PR3's `/live` and `/context`+processes routes, whose
# whole reason for depending on "jobs_routines" is to learn exactly this.
# Both now tag their own transactions (`jobs_run`, `jobs_routines_tick`)
# rather than leaving them unlabelled. `signoffs_notification_*`
# (`qorqut/signoffs/notifications.py`) is the same fix for
# `signoff_notifications`/`signoff_notification_schedule`, also untriggered
# and also written outside `Application.invoke`; it feeds the existing
# trigger-backed "signoffs" kind rather than a new one, since PR3's
# `/me/attention` route already depends on "signoffs" for the trigger-covered
# part of the same read.
COARSE_KIND_BY_OPERATION_PREFIX = (
    (("routine", "job"), "jobs_routines"),
    (("learning",), "learnings"),
    (("signoffs_notification",), "signoffs"),
)

# The Context queue's status (exact input states, the blocked head, publication and completion receipts) lives in tables with no
# capture trigger and is also written outside `Application.invoke`: the queue driver tags its three transactions
# (`context_queue_driver_write`, `_reconcile` and `_start`). It is an ADDITIVE coarse kind -- a matching operation bumps it in
# addition to whatever kind it already maps to (`jobs_routines`, `learnings`, `signoffs`), never instead of it.
# The prefixes name every committed producer that can admit, run, complete or control source work or apply a prepared Context
# mutation: the `context_` and `actions_` operation families, note and journal snapshot uploads, and the existing job, learning and
# Signoff families. A conservative extra bump is acceptable; a missed one is a stale status.
CONTEXT_QUEUE_KIND = "context_queue"
CONTEXT_QUEUE_OPERATION_PREFIXES = ("context_", "actions_", "notes_upload", "journal_upload_to_filer", "job", "learning", "signoff")

# Kinds with no trigger-covered table at all (see COARSE_KIND_BY_OPERATION_PREFIX):
# every bump lands at GLOBAL_SCOPE, so a route depending on one must check
# GLOBAL_SCOPE regardless of the route's own scope -- never the route's business
# scope, which would never see a bump and would 304 forever.
# "context_queue" is global because a blocked head holds back every namespace's later work; what a route then shows of it stays
# authorized and namespace-limited by the route's own read.
GLOBAL_ONLY_KINDS = frozenset({"jobs_routines", "learnings", CONTEXT_QUEUE_KIND})
# Kinds that are mostly trigger-covered with a real per-row business_id, but
# also have at least one contributing source that always lands at
# GLOBAL_SCOPE: "signoffs" via `signoff_attention` (no scope column captured,
# see the module docstring) and the coarse `signoffs_notification_*` writes;
# "agents" via `agent_profiles`/`principals` (captured `home_business_id`/no
# scope column at all, while `business_memberships` does carry a real
# `business_id`). A route depending on one of these must check both its own
# scope and GLOBAL_SCOPE, or it can miss the global-only half of the kind's
# writes and 304 on data that already changed. "action_logs": Global-stored
# owner/ecosystem actions are shared into businesses through memberships.
ALSO_GLOBAL_KINDS = frozenset({"signoffs", "tasks", "agents", "action_logs"})


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _scope_of(after, before):
    data = after or before or {}
    business_id = data.get("business_id", data.get("review_business_id"))
    return business_id or GLOBAL_SCOPE


def _kinds_for_row(source_type, after, before):
    if source_type == "relationships":
        data = after or before or {}
        kinds = set()
        for prefix in ("from", "to"):
            kind = RELATIONSHIP_ENDPOINT_KIND.get(data.get(prefix + "_type"))
            if kind:
                kinds.add(kind)
        return kinds
    if source_type in ITEM_RECORD_TABLES:
        # A thread entry, follow or shared tag belongs to the Task or Action it names.
        kind = ITEM_KIND_BY_TYPE.get((after or before or {}).get("item_type"))
        return {kind} if kind else set()
    kind = KIND_BY_TABLE.get(source_type)
    return {kind} if kind else set()


def _coarse_kind_for_operation(operation):
    if not operation:
        return None
    for prefixes, kind in COARSE_KIND_BY_OPERATION_PREFIX:
        if operation.startswith(prefixes):
            return kind
    return None


def _coarse_kinds_for_operation(operation):
    """Every coarse kind an operation name implies: its first-matching existing kind plus, additively, the Context queue kind."""
    kinds = set()
    first = _coarse_kind_for_operation(operation)
    if first:
        kinds.add(first)
    if operation and operation.startswith(CONTEXT_QUEUE_OPERATION_PREFIXES):
        kinds.add(CONTEXT_QUEUE_KIND)
    return kinds


class Subscriber:
    """One SSE client's outstanding queue. Never blocks a publisher."""

    __slots__ = ("queue", "dropped")

    def __init__(self):
        self.queue: queue.Queue = queue.Queue(maxsize=256)
        self.dropped = False

    def offer(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # A slow reader must not exert backpressure on commits. Mark it
            # for a reset instead of blocking or growing without bound; the
            # client's `Last-Event-ID` resume (or the `reset` sentinel below)
            # recovers it on the next read.
            self.dropped = True


class ChangeFeed:
    """Process-wide. One instance lives on the runtime for its lifetime."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._ring: deque = deque(maxlen=RING_SIZE)
        self._versions: dict[tuple[str, str], int] = {}
        self._wildcard_seq = 0
        self._commits = 0
        self._subscribers: dict[int, Subscriber] = {}
        self._listeners: list = []
        self._commit_listeners: list = []
        self.publish_failures = 0
        self._ticker_stop = None
        self._ticker_thread = None
        # Versions restart at zero every process start, but a browser's ETag
        # cache survives a restart. Mixed into `_etag_for` (transport/http.py)
        # so a tag minted by a previous process can never false-positive
        # against this one's fresh version counters.
        self.nonce = new_id()

    # -- capture -------------------------------------------------------
    @staticmethod
    def capture(connection):
        """Read pending `inbox_changes` rows for this unit. Never raises: this
        runs inside the caller's own commit path (`Database._unit`), sharing
        its transaction, so an exception here would roll back a legitimate
        write for a bookkeeping failure that has nothing to do with it --
        the same reasoning as `publish_commit` swallowing its own errors.

        Must be called before `finalize_events()` deletes them (it reads the
        same table). Read-only; never mutates `inbox_changes`.
        """
        try:
            return ChangeFeed._capture(connection)
        except Exception:
            LOGGER.exception("change_feed_capture_failed")
            return []

    @staticmethod
    def _capture(connection):
        rows = connection.execute(
            "SELECT source_type, before_json, after_json FROM inbox_changes ORDER BY id").fetchall()
        captured = []
        for row in rows:
            after, before = _json(row["after_json"]), _json(row["before_json"])
            captured.append({
                "table": row["source_type"],
                "scope": _scope_of(after, before),
                "kinds": _kinds_for_row(row["source_type"], after, before),
            })
        return captured

    # -- publish ---------------------------------------------------------
    def publish_commit(self, *, captured, operation, business_id, wrote=True):
        """Called once per successful writable commit. Never raises.

        Counts every writable commit unconditionally, including one that
        publishes nothing below -- the external-writer ticker compares this
        count, not the ring's sequence, to tell an untracked in-process write
        (which still moves `PRAGMA data_version`, but was already accounted
        for here) apart from a real external one.
        """
        with self._lock:
            self._commits += 1
            listeners = list(self._commit_listeners) if wrote else ()
        for listener in listeners:
            try:
                listener()
            except Exception:
                with self._lock:
                    self.publish_failures += 1
                LOGGER.exception("commit_listener_failed")
        try:
            self._publish_commit(captured, operation, business_id, wrote)
        except Exception:
            with self._lock:
                self.publish_failures += 1
            LOGGER.exception("change_feed_publish_failed operation=%s", operation)

    def _publish_commit(self, captured, operation, business_id, wrote):
        tables = sorted({row["table"] for row in captured})
        kinds_by_scope: dict[str, set[str]] = {}
        for row in captured:
            kinds_by_scope.setdefault(row["scope"], set()).update(row["kinds"])
            # A Signoff may have dependants in any project. Input state is independent of Task revisions.
            if row['table'] in {'signoffs', 'signoff_source_links'}:
                kinds_by_scope.setdefault(GLOBAL_SCOPE, set()).update({'tasks', 'signoffs'})
        # A coarse kind has no row to prove a write happened (handoff 4.2): the
        # routine scheduler and job admission tag every tick this way whether or
        # not there was anything due, so gate this on the unit actually changing
        # a row (Database._unit's total_changes delta) -- an idle tick otherwise
        # bumps a global kind every few seconds forever, which is a higher idle
        # load than the poll this stage removes. Captured trigger rows above
        # already imply a real write, so that path needs no such gate.
        coarse_kinds = _coarse_kinds_for_operation(operation) if wrote else set()
        if coarse_kinds:
            # Global, always -- not `business_id or GLOBAL_SCOPE`. A coarse
            # kind has no row to read a real business out of, and the
            # `business_id` a caller happens to pass to `database.transaction`
            # is not a reliable substitute: the one central write path nearly
            # everything goes through (`Application.invoke`,
            # qorqut/application/engine.py) never passes it, and the direct
            # callers this coarse path exists for (BackgroundService,
            # RoutineScheduler, SignoffNotifications) do not either. Bumping
            # a specific business here would silently under-bump every other
            # business watching the same coarse kind -- exactly the stale-304
            # the handoff rules out. Global-only over-bumps every business's
            # dependency on this kind instead, which is always safe.
            kinds_by_scope.setdefault(GLOBAL_SCOPE, set()).update(coarse_kinds)
        if not tables and not coarse_kinds:
            return  # A committed unit that touched nothing this feed tracks.
        with self._lock:
            seq = next(self._seq)
            for scope, kinds in kinds_by_scope.items():
                for kind in kinds:
                    self._versions[(kind, scope)] = seq
            event = {
                "seq": seq, "tables": tables,
                "kinds": sorted({kind for kinds in kinds_by_scope.values() for kind in kinds}),
                "business_id": business_id, "operation": operation,
            }
            self._ring.append(event)
            subscribers = list(self._subscribers.values())
            listeners = list(self._listeners)
        for subscriber in subscribers:
            subscriber.offer(event)
        for listener in listeners:
            listener(event)

    def publish_wildcard(self):
        """An external writer changed the database; invalidate everything.

        Bumps every currently-known (kind, scope) pair, plus `_wildcard_seq`
        (`route_versions` folds this into every entry it returns, including
        one whose (kind, scope) was never written in this process and so has
        no key here at all yet) -- a pair with no prior write would otherwise
        keep serving version 0, and a 304 against a tag computed from it,
        forever, no matter how many external writes actually happen.
        """
        with self._lock:
            seq = next(self._seq)
            self._wildcard_seq = seq
            for key in list(self._versions):
                self._versions[key] = seq
            event = {"seq": seq, "tables": ["*"], "kinds": ["*"], "business_id": None, "operation": "external_write"}
            self._ring.append(event)
            subscribers = list(self._subscribers.values())
        for subscriber in subscribers:
            subscriber.offer(event)

    # -- versions (read by PR3's ETag) ------------------------------------
    def version(self, kind, scope):
        with self._lock:
            return self._versions.get((kind, scope or GLOBAL_SCOPE), 0)

    def versions(self, kinds, scope):
        with self._lock:
            return {kind: self._versions.get((kind, scope or GLOBAL_SCOPE), 0) for kind in kinds}

    def route_versions(self, kinds, scope):
        """(kind, scope) -> version for PR3's ETag, expanding GLOBAL_ONLY_KINDS
        and ALSO_GLOBAL_KINDS to the extra scope their bump side actually
        writes at. Every entry this returns must go into the ETag hash: an
        entry left out is a write this route could silently miss.

        `scope=ANY_SCOPE` is for an owner-aggregate route (PR3's /me/attention)
        that reads across every business the owner belongs to -- a set that
        itself can only be discovered with a database read, which an ETag
        computed before any read cannot do. Every known scope is checked for
        each kind instead of one specific business, which is safe (a change
        to a business the owner does not even belong to just costs an extra
        miss) and never under-bumps (any business the owner does belong to is
        necessarily among "every known scope").
        """
        with self._lock:
            wildcard = self._wildcard_seq
            if scope is ANY_SCOPE:
                return {(kind, "any"): max([wildcard, *(v for (k, _), v in self._versions.items() if k == kind)])
                    for kind in kinds}
            resolved_scope = scope or GLOBAL_SCOPE
            out = {}
            for kind in kinds:
                scopes = (GLOBAL_SCOPE,) if kind in GLOBAL_ONLY_KINDS else (
                    (resolved_scope, GLOBAL_SCOPE) if kind in ALSO_GLOBAL_KINDS and resolved_scope != GLOBAL_SCOPE else (resolved_scope,))
                for one in scopes:
                    out[(kind, one)] = max(self._versions.get((kind, one), 0), wildcard)
            return out

    # -- listeners ---------------------------------------------------------
    def add_commit_listener(self, listener):
        """Observe every committed write, including tables without UI capture triggers.

        Callbacks must only schedule work; they run after the transaction closes.
        An observer failure never changes the accepted transaction's result.
        """
        with self._lock:
            if listener not in self._commit_listeners:
                self._commit_listeners.append(listener)

    def add_listener(self, listener):
        """Call ``listener(event)`` after every published commit event, on the committing thread, after the commit.

        A listener is a non-blocking in-process hook (for example setting a ``threading.Event``); it must not take locks,
        read or write the database or call back into the feed. An exception is counted and logged like any other publish
        failure and never reaches the committing caller."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener):
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    # -- subscribers -------------------------------------------------------
    def subscribe(self):
        subscriber = Subscriber()
        with self._lock:
            self._subscribers[id(subscriber)] = subscriber
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.pop(id(subscriber), None)

    def replay_since(self, last_event_id):
        """Events after `last_event_id`, or None if it fell off the ring
        (the caller should send a `reset` and let the client refetch)."""
        with self._lock:
            ring = list(self._ring)
        if last_event_id is None:
            return []
        if not ring:
            return [] if last_event_id == 0 else None
        oldest = ring[0]["seq"]
        if last_event_id < oldest - 1:
            return None
        return [event for event in ring if event["seq"] > last_event_id]

    # -- external-writer ticker --------------------------------------------
    def start_external_writer_ticker(self, connect):
        """Poll `PRAGMA data_version` on a dedicated connection every second.

        `connect` returns a fresh sqlite3 connection this ticker owns
        exclusively for its own lifetime (never pooled: it must never be
        handed to a unit of work, and it never opens a transaction).
        """
        if self._ticker_thread is not None and self._ticker_thread.is_alive():
            return
        self._ticker_stop = threading.Event()

        def run():
            connection = connect()
            try:
                last_data_version = connection.execute("PRAGMA data_version").fetchone()[0]
                last_commits = self._current_commits()
                while not self._ticker_stop.wait(EXTERNAL_WRITER_POLL_SECONDS):
                    try:
                        data_version = connection.execute("PRAGMA data_version").fetchone()[0]
                    except Exception:
                        LOGGER.exception("change_feed_ticker_read_failed")
                        continue
                    if data_version != last_data_version:
                        last_data_version = data_version
                        if self._current_commits() == last_commits:
                            # No writable unit committed in-process since the last
                            # tick (tracked or not -- an untracked in-process write,
                            # such as an agent-activity sweep or a nudge-backend
                            # reconcile pass, still counts here even though it
                            # publishes nothing to the ring): this data_version
                            # bump came from outside. Comparing the ring's own
                            # sequence instead would mistake that untracked write
                            # for an external one and wildcard-invalidate every
                            # known version on every such write.
                            self.publish_wildcard()
                        last_commits = self._current_commits()
            finally:
                connection.close()

        self._ticker_thread = threading.Thread(target=run, name="change-feed-external-writer", daemon=True)
        self._ticker_thread.start()

    def _current_commits(self):
        with self._lock:
            return self._commits

    def stop(self):
        if self._ticker_stop is not None:
            self._ticker_stop.set()
        thread = self._ticker_thread
        if thread is None:
            return
        thread.join(timeout=5)
        if thread.is_alive():
            raise TimeoutError("Change feed external-writer ticker is still draining")
        self._ticker_thread = None
